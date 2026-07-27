"""Local depth feed. Every number this returns was measured by your own Fynd
instance against Tycho-indexed liquidity on your machine: no PropellerHeads
API, no hosted dataset, nothing to trust but the quotes you collected.

    from eth_price_poc import client
    feed = client()                  # ETH/USDC against http://127.0.0.1:3000
    snap = feed.collect()            # one block: sweep, levels, curve, routes
    print(snap["block"], snap["robust_mid"])

`history()` covers the blocks this process has collected. It starts empty and
grows as `collect()` runs; nothing is written to disk, so a new process starts
from zero. Run the collector loop (`python -m eth_price_poc.generate.run_local`)
to accumulate a window.

Every read method returns data you own: mutating a result cannot corrupt the
retained window.
"""
from __future__ import annotations

import copy
import json
from collections import deque
from typing import Any

from .generate.config import NullSink, PairConfig
from .generate.core import collect_snapshot, fynd_health, fynd_quote
from .generate.util import etherscan_address_url

HEAD_PROBE_USD = 1_000.0
DEFAULT_HISTORY_SIZE = 720


class EthPricePoCDataUnavailable(RuntimeError):
    """Raised when a value cannot be produced from local measurements —
    typically because Fynd is unreachable, still warming up, or returned no
    usable quote for the requested pair."""


class LocalFeed:
    """Collects and serves block-by-block depth snapshots from a local Fynd.

    `cfg` is a PairConfig (ETH/USDC by default). `history_size` caps the
    in-process rolling window of retained blocks. `sink` receives collection
    errors and per-quote failures; the default swallows them, and
    `run_local.StderrSink` prints them.
    """

    def __init__(self, cfg: PairConfig | None = None, *,
                 history_size: int = DEFAULT_HISTORY_SIZE, sink: Any | None = None):
        if isinstance(cfg, str):
            raise TypeError(
                "LocalFeed takes a PairConfig, not a base URL. This SDK no longer "
                "reads the hosted API; it measures depth with your own Fynd. Pass "
                "PairConfig(fynd_base_url=...) to point at a non-default Fynd."
            )
        self.cfg = cfg or PairConfig()
        self.sink = sink if sink is not None else NullSink()
        self._collected: deque[tuple[dict, dict]] = deque(maxlen=history_size)

    # ── collection ────────────────────────────────────────────────────

    def collect(self) -> dict:
        """Measure one block and append it to the rolling window. Returns the
        block's snapshot. Raises when Fynd produced nothing usable."""
        snap, payload = collect_snapshot(self.cfg, self.sink)
        if snap is None or payload is None:
            raise EthPricePoCDataUnavailable(
                f"no snapshot from Fynd at {self.cfg.fynd_base_url} — check that it "
                "is running, warmed up, and indexing the pair's liquidity"
            )
        self._collected.append((snap, payload))
        return copy.deepcopy(snap)

    # ── core reads ────────────────────────────────────────────────────

    def latest(self) -> dict:
        """Most recent collected block. Collects one if the window is empty."""
        if not self._collected:
            return self.collect()
        return copy.deepcopy(self._collected[-1][0])

    def history(self, limit: int | None = None) -> dict:
        """Blocks collected by this process, oldest first.

        The window lives in this process: it holds what `collect()` has
        produced since construction, capped at `history_size`, and is never
        read from disk. `limit` keeps only the most recent N blocks; 0 keeps
        none.

        Copying the whole window is proportional to its size (a few seconds
        once it holds hundreds of blocks with dense curves). Pass `limit`, or
        use `history_as_dataframe()`, when that cost matters in a loop.
        """
        return {
            "pair": self.cfg.pair_label,
            "token_in": self._token_dict(self.cfg.token_in),
            "token_out": self._token_dict(self.cfg.token_out),
            "blocks_retained": len(self._collected),
            "blocks": copy.deepcopy(self._window_blocks(limit)),
        }

    def status(self) -> dict:
        """Collector state: Fynd health, the block Fynd is currently solving
        against, and how far the retained window trails it.

        `mode` is one of starting (nothing collected yet), live (level with
        Fynd), stale (window trails Fynd), degraded (Fynd unhealthy or not
        answering). Never issues a collection cycle; it costs one probe quote.
        """
        health = fynd_health(self.cfg)
        last_block = self._collected[-1][0].get("block") if self._collected else None
        last_time = self._collected[-1][0].get("time") if self._collected else None
        head_block = self._head_block() if health and health.get("healthy") else None

        if not health or not health.get("healthy"):
            mode = "degraded"
        elif not self._collected:
            mode = "starting"
        elif head_block is None:
            mode = "degraded"
        else:
            behind = head_block - int(last_block)
            mode = "live" if behind <= 1 else "stale"

        blocks_behind = (
            head_block - int(last_block)
            if head_block is not None and last_block is not None else None
        )
        return {
            "mode": mode,
            "pair": self.cfg.pair_label,
            "last_block": last_block,
            "last_time": last_time,
            "head_block": head_block,
            "blocks_behind": blocks_behind,
            "blocks_retained": len(self._collected),
            "history_size": self._collected.maxlen,
            "fynd": {"base_url": self.cfg.fynd_base_url, **health} if health else None,
        }

    def coverage(self) -> dict:
        """Protocols and pools Fynd actually routed through in the retained
        window, plus Fynd's health block. Collects one block if the window is
        empty.

        This is narrower than the "indexed liquidity universe" figure on
        marketprice.xyz: that counts everything Tycho indexes for the run,
        which no Fynd endpoint exposes. What is reported here is strictly what
        your own quotes touched, which is the part you can check.
        """
        if not self._collected:
            self.collect()
        protocols: set[str] = set()
        components: set[str] = set()
        for _snap, payload in self._collected:
            for leg in payload.get("route_legs") or []:
                if leg.get("protocol"):
                    protocols.add(leg["protocol"])
                if leg.get("component_id"):
                    components.add(leg["component_id"])
        health = fynd_health(self.cfg)
        return {
            "pair": self.cfg.pair_label,
            "blocks_observed": len(self._collected),
            "protocols_routed": sorted(protocols),
            "components_routed": sorted(components),
            "component_count": len(components),
            "fynd": {"base_url": self.cfg.fynd_base_url, **health} if health else None,
        }

    def tokens(self) -> dict:
        """token_in / token_out for the configured pair, each
        {address, symbol, decimals}.

        A sell record's amount_in / amount_out use the swapped tokens'
        decimals (it routes token_out -> token_in); detail() carries per-leg
        decimals.
        """
        return {
            "token_in": self._token_dict(self.cfg.token_in),
            "token_out": self._token_dict(self.cfg.token_out),
        }

    def curve_for_block(self, block_index: int = -1, side: str = "buy") -> list[dict]:
        """Dense sweep curve for one retained block. `block_index` is a list
        index into the window (-1 = most recent, default)."""
        if side not in ("buy", "sell"):
            raise ValueError("side must be 'buy' or 'sell'")
        if not self._collected:
            self.collect()
        try:
            snap = self._collected[block_index][0]
        except IndexError:
            raise EthPricePoCDataUnavailable(
                f"window has no block at index {block_index} "
                f"({len(self._collected)} blocks retained)"
            ) from None
        curve = ((snap.get("curve") or {}).get(side)) or []
        if not curve:
            raise EthPricePoCDataUnavailable(
                f"no {side} curve measured for block {snap.get('block')}"
            )
        return copy.deepcopy(curve)

    # ── per-rung detail (route + execution) ───────────────────────────

    def detail(self, block: int, side: str, target_impact_pct: float) -> dict | None:
        """Per-rung route + execution detail for one depth cell: the per-leg
        route (protocol, pool, token in/out, split, gas), a tooltip of the
        measured execution metrics, and a Tenderly simulation URL.

        Returns None when the block is not in the retained window, or when no
        route legs were measured for that side.
        """
        if side not in ("buy", "sell"):
            raise ValueError("side must be 'buy' or 'sell'")
        found = self._payload_for_block(block)
        if found is None:
            return None
        payload = found
        legs_target = self._nearest_target(
            payload.get("route_legs") or [], side, float(target_impact_pct))
        if legs_target is None:
            return None
        legs = sorted(
            (leg for leg in payload["route_legs"]
             if leg["side"] == side and leg["target_impact_pct"] == legs_target),
            key=lambda leg: leg["leg_index"],
        )
        level = next(
            (row for row in payload.get("levels") or []
             if row["side"] == side and row["target_impact_pct"] == legs_target),
            {},
        )
        response = next(
            (row for row in payload.get("quote_responses") or []
             if row["side"] == side and row["target_impact_pct"] == legs_target),
            {},
        )
        block_row = payload["block_row"]
        return {
            "block": block_row["block"],
            "side": side,
            "target_impact_pct": float(target_impact_pct),
            "block_hash": block_row.get("block_hash"),
            "block_ts_ms": block_row.get("ts"),
            "route_legs": [self._leg_dict(leg) for leg in legs],
            "legs_from_target": None if legs_target == float(target_impact_pct) else legs_target,
            "tooltip": {
                "actual_impact_pct": level.get("actual_impact_pct"),
                "effective_price": level.get("effective_price"),
                "mid": block_row.get("robust_mid"),
                "price_impact_bps": level.get("price_impact_bps"),
                "amount_usd": level.get("amount_usd"),
                "bound": level.get("bound"),
                "target_reached": level.get("target_reached"),
                "gas_cost_eth": level.get("gas_cost_eth"),
                "gas_cost_token_out": level.get("gas_cost_token_out"),
                "gas_estimate_units": level.get("gas_estimate_units"),
                "derived_from": level.get("derived_from"),
            },
            "tenderly": {
                "url": response.get("tenderly_url"),
                "status": response.get("tenderly_status") or "no_transaction",
            },
            "raw_response_available": bool(response.get("raw_response_json")),
        }

    def export(self, block: int, side: str, target_impact_pct: float) -> dict | None:
        """Raw stored Fynd quote for one depth cell: the full response, the
        executable transaction (to, calldata, value), the fee breakdown, and a
        Tenderly URL.

        Raw responses are retained for the anchored headline targets only, so
        this returns None for a block outside the window or a side with no
        anchored quote.
        """
        if side not in ("buy", "sell"):
            raise ValueError("side must be 'buy' or 'sell'")
        payload = self._payload_for_block(block)
        if payload is None:
            return None
        raw_target = self._nearest_target(
            payload.get("quote_responses") or [], side, float(target_impact_pct))
        if raw_target is None:
            return None
        row = next(
            row for row in payload["quote_responses"]
            if row["side"] == side and row["target_impact_pct"] == raw_target
        )
        return {
            "block": row["block"],
            "side": side,
            "target_impact_pct": float(target_impact_pct),
            "order_id": row.get("order_id"),
            "solve_time_ms": row.get("solve_time_ms"),
            "response": json.loads(row["raw_response_json"]) if row.get("raw_response_json") else None,
            "executable": {
                "to": row.get("executable_to"),
                "calldata": row.get("executable_calldata"),
                "value": row.get("executable_value"),
            },
            "fee_breakdown": {
                "router_fee": row.get("fee_router_atomic"),
                "client_fee": row.get("fee_client_atomic"),
                "max_slippage": row.get("fee_max_slippage_atomic"),
                "min_amount_received": row.get("fee_min_received_atomic"),
            },
            "tenderly": {
                "url": row.get("tenderly_url"),
                "status": row.get("tenderly_status") or "no_transaction",
            },
            "raw_from_target": None if raw_target == float(target_impact_pct) else raw_target,
        }

    # ── pandas convenience ────────────────────────────────────────────

    def history_as_dataframe(self, limit: int | None = None):
        """Return a pandas.DataFrame keyed on (block, time). Requires
        the `pandas` extra: pip install eth-price-poc-sdk[pandas]
        """
        try:
            import pandas as pd  # type: ignore
        except ImportError as e:
            raise ImportError(
                "pandas is required: install with `pip install eth-price-poc-sdk[pandas]`"
            ) from e
        rows = []
        # Reads the retained snapshots without copying them: every value below
        # is a scalar lifted into a fresh row, so nothing aliases the window.
        for b in self._window_blocks(limit):
            row = {
                "block": b.get("block"),
                "time": b.get("time"),
                "spot_price": b.get("spot_price"),
                "robust_mid": b.get("robust_mid"),
                "duration_ms": b.get("duration_ms"),
            }
            # Pull headline depth at each anchored target
            for k in ("0.5", "1.0", "5.0", "10.0", "25.0", "50.0"):
                for side in ("buy", "sell"):
                    r = (b.get("levels") or {}).get(k, {}).get(side, {})
                    row[f"depth_{side}_{k}pct"] = r.get("amount_usd")
                    row[f"price_{side}_{k}pct"] = r.get("price")
                    row[f"bound_{side}_{k}pct"] = r.get("bound")
            rows.append(row)
        df = pd.DataFrame(rows)
        if "time" in df.columns:
            # ISO8601 + utc: timestamps mix whole-second and fractional forms
            # (and Z vs +00:00); without an explicit format pandas infers from
            # the first row and coerces mismatches to NaT, which breaks df.plot.
            df["time"] = pd.to_datetime(df["time"], errors="coerce", utc=True, format="ISO8601")
        return df

    # ── internals ─────────────────────────────────────────────────────

    def _window_blocks(self, limit: int | None) -> list[dict]:
        """Retained snapshots, oldest first, uncopied. Anything handed to a
        caller must be copied first; internal readers that only consume the
        values can use these directly."""
        blocks = [snap for snap, _payload in self._collected]
        if limit is None:
            return blocks
        if limit < 0:
            raise ValueError(f"limit must be >= 0, got {limit}")
        return blocks[-limit:] if limit > 0 else []

    @staticmethod
    def _token_dict(token) -> dict:
        return {"address": token.address, "symbol": token.symbol, "decimals": token.decimals}

    @staticmethod
    def _leg_dict(leg: dict) -> dict:
        return {
            "leg_index": leg["leg_index"],
            "protocol": leg["protocol"],
            "component_id": leg["component_id"],
            "split": leg.get("split"),
            "token_in": {
                "address": leg.get("token_in_address"),
                "symbol": leg.get("token_in_symbol"),
                "decimals": leg.get("token_in_decimals"),
            },
            "token_out": {
                "address": leg.get("token_out_address"),
                "symbol": leg.get("token_out_symbol"),
                "decimals": leg.get("token_out_decimals"),
            },
            "amount_in_atomic": leg.get("amount_in_atomic"),
            "amount_out_atomic": leg.get("amount_out_atomic"),
            "gas_estimate_units": leg.get("gas_estimate_units"),
            "etherscan_url": etherscan_address_url(
                leg["component_id"] if _looks_like_address(leg.get("component_id")) else None
            ),
        }

    @staticmethod
    def _nearest_target(rows: list[dict], side: str, target: float) -> float | None:
        """Closest target_impact_pct present for `side`, or None if that side
        has no rows at all."""
        candidates = [row["target_impact_pct"] for row in rows if row["side"] == side]
        if not candidates:
            return None
        return min(candidates, key=lambda stored: abs(stored - target))

    def _payload_for_block(self, block: int) -> dict | None:
        for _snap, payload in reversed(self._collected):
            if payload["block_row"]["block"] == int(block):
                return payload
        return None

    def _head_block(self) -> int | None:
        """Block Fynd is currently solving against, read from a probe quote.
        Fynd exposes no block endpoint, so this costs one quote."""
        q = fynd_quote(
            self.cfg,
            self.cfg.token_in.address,
            self.cfg.token_out.address,
            self.cfg.token_in.atomic(HEAD_PROBE_USD),
            self.sink,
            {"phase": "head"},
        )
        number = ((q or {}).get("block") or {}).get("number")
        return number if isinstance(number, int) and number > 0 else None


def _looks_like_address(value: str | None) -> bool:
    if not value or not value.startswith("0x") or len(value) != 42:
        return False
    return all(c in "0123456789abcdefABCDEF" for c in value[2:])


def client(cfg: PairConfig | None = None, **kw: Any) -> LocalFeed:
    """Shortcut: `from eth_price_poc import client; feed = client()`."""
    return LocalFeed(cfg, **kw)
