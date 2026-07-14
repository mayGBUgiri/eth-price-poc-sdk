"""HTTP client for the Price-of-Ethereum PoC dataset.

Wraps the public API at /api/* (live mode) and falls back to the static
data.json / coverage_static.json snapshots when the live API is unreachable.
No fake data: if the live endpoint and the static fallback both fail
for a given resource, the method raises.
"""
from __future__ import annotations

import json
import urllib.parse
from typing import Any

import requests


DEFAULT_BASE = "https://marketprice.xyz"


class EthPricePoCDataUnavailable(RuntimeError):
    """Raised when both the live API and the static fallback are unreachable."""


class EthPricePoCClient:
    """Minimal client. Construct with a base URL like:

        EthPricePoCClient("https://marketprice.xyz")
        EthPricePoCClient("http://localhost:8000")            # local dev server

    The default points at the live deployment, which serves both the API and
    the static fallback. Override for local / private deployments.
    """

    def __init__(self, base: str = DEFAULT_BASE, *, pair: str = "ETH/USDC",
                 timeout: float = 10.0, session: requests.Session | None = None):
        self.base = base.rstrip("/")
        # The public deployment currently exposes ETH/USDC. Compatible private
        # deployments may expose another pair through the same query parameter.
        self.pair = pair
        self.timeout = timeout
        self.session = session or requests.Session()

    def _with_pair(self, path: str) -> str:
        # ETH/USDC is the server default; only a non-primary pair needs ?pair=.
        if not self.pair or self.pair == "ETH/USDC":
            return path
        sep = "&" if "?" in path else "?"
        return f"{path}{sep}pair={urllib.parse.quote(self.pair, safe='')}"

    # ── core endpoints ────────────────────────────────────────────────

    def latest(self) -> dict:
        """Most recent block's full snapshot."""
        return self._get_with_static_fallback(
            api_path="/api/latest",
            static_path="/data.json",
            static_transform=lambda d: (d.get("blocks") or [{}])[-1],
        )

    def history(self, limit: int | None = None) -> dict:
        """Rolling history. `limit` truncates from the most recent end."""
        q = f"?limit={int(limit)}" if limit else ""
        return self._get_with_static_fallback(
            api_path=f"/api/history{q}",
            static_path="/data.json",
            static_transform=lambda d: (
                {**d, "blocks": (d.get("blocks") or [])[-int(limit):]} if limit
                else d
            ),
        )

    def status(self) -> dict:
        """Backend mode (live/static), blocks_behind, fynd health, etc."""
        return self._get_with_static_fallback(
            api_path="/api/status",
            static_path=None,
            static_transform=None,
            allow_static=False,
        )

    def coverage(self) -> dict:
        """Indexed protocols + components from Fynd's current run."""
        return self._get_with_static_fallback(
            api_path="/api/coverage",
            static_path="/coverage_static.json",
            static_transform=None,
        )

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
        hist = self.history(limit=limit)
        rows = []
        for b in hist.get("blocks", []):
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

    def curve_for_block(self, block_index: int = -1, side: str = "buy") -> list[dict]:
        """Return the dense sweep curve for a single block. `block_index`
        is a list index into history (-1 = latest, default).

        Historical curves come from /api/curve (persisted per block on the
        backend). Raises EthPricePoCDataUnavailable for blocks collected
        before curve persistence existed.
        """
        if side not in ("buy", "sell"):
            raise ValueError("side must be 'buy' or 'sell'")
        if block_index == -1:
            snap = self.latest()
            return ((snap.get("curve") or {}).get(side)) or []
        hist = self.history()
        blocks = hist.get("blocks") or []
        snap = blocks[block_index]
        curve = ((snap.get("curve") or {}).get(side)) or []
        if curve:
            return curve
        block_num = snap.get("block")
        try:
            r = self.session.get(
                self.base + self._with_pair(f"/api/curve?block={block_num}"), timeout=self.timeout)
            if r.ok:
                return (((r.json() or {}).get("curve") or {}).get(side)) or []
        except (requests.RequestException, json.JSONDecodeError):
            pass
        raise EthPricePoCDataUnavailable(
            f"no dense curve stored for block {block_num} "
            "(collected before curve persistence, or backend unreachable)"
        )

    # ── internals ─────────────────────────────────────────────────────

    def _get_with_static_fallback(self, *, api_path: str, static_path: str | None,
                                  static_transform, allow_static: bool = True) -> dict:
        url = self.base + self._with_pair(api_path)
        # data.json / coverage_static.json are the primary pair's snapshots,
        # never serve them for another pair.
        if self.pair and self.pair != "ETH/USDC":
            allow_static = False
        try:
            r = self.session.get(url, timeout=self.timeout)
            if r.ok:
                return r.json()
        except (requests.RequestException, json.JSONDecodeError):
            pass
        if not allow_static or not static_path:
            raise EthPricePoCDataUnavailable(
                f"live API at {url} unreachable and no static fallback available"
            )
        # Static fallback: same origin, lowercase file. Generous timeout,
        # the fallback bundle is bigger than a JSON API response.
        try:
            r = self.session.get(self.base + static_path, timeout=max(self.timeout, 30.0))
            r.raise_for_status()
            data = r.json()
            return static_transform(data) if static_transform else data
        except (requests.RequestException, json.JSONDecodeError) as e:
            raise EthPricePoCDataUnavailable(
                f"both {url} and {self.base + static_path} unreachable"
            ) from e


def client(base: str = DEFAULT_BASE, **kw: Any) -> EthPricePoCClient:
    """Shortcut: `from eth_price_poc import client; c = client()`."""
    return EthPricePoCClient(base, **kw)
