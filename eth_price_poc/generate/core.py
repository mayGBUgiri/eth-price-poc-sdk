"""Pair-agnostic block-snapshot generation: quote Fynd, sweep depth across
trade sizes, and assemble one block's depth snapshot plus a persistence-ready
payload. Shared by the hosted collector and by anyone running their own Fynd
instance (see eth_price_poc.generate.run_local). No persistence here; the
caller decides what to do with the snapshot.

`cfg` is any object exposing the PairConfig fields (token_in/out with
.address/.symbol/.decimals/.atomic(), fynd_base_url, search_*, impact_levels,
sweep_samples_per_side, max_workers, slippage, enable_encoding, rpc_url,
tenderly_from_address, pair_label, collector_version). `state` is an optional
error sink (anything with add_error/add_quote_failure/mid_degraded_count);
pass None or NullSink() when generating data standalone.
"""
from __future__ import annotations

import json
import math
import statistics
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .config import PairConfig as Config
    CollectorState = Any

import requests

from .util import (
    build_tenderly_url,
    derive_gas_costs,
    derive_price_impact_bps,
    known_token,
)

SENDER_DEFAULT = "0x0000000000000000000000000000000000000001"
QUOTE_SOURCE = "fynd"

# Completeness bitflags describing what a persisted block carries.
C_HAS_TX         = 1
C_HAS_ROUTE_LEGS = 2
C_HAS_RAW_JSON   = 4
C_FULL = C_HAS_TX | C_HAS_ROUTE_LEGS | C_HAS_RAW_JSON


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def is_finite_number(x: Any) -> bool:
    return isinstance(x, (int, float)) and math.isfinite(x)


def get_block_number(rpc_url: str, timeout: int = 10) -> int:
    r = requests.post(
        rpc_url,
        json={"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber", "params": []},
        timeout=timeout,
    )
    r.raise_for_status()
    d = r.json()
    if "error" in d:
        raise RuntimeError(f"RPC error: {d['error']}")
    return int(d["result"], 16)


# ── Fynd ─────────────────────────────────────────────────────────────────────


def fynd_health(cfg: Config) -> dict | None:
    try:
        r = requests.get(f"{cfg.fynd_base_url}/v1/health", timeout=5)
        if r.ok:
            return r.json()
    except Exception:
        return None
    return None


def fynd_quote(
    cfg: Config,
    token_in: str,
    token_out: str,
    amount: int,
    state: CollectorState | None = None,
    ctx: dict | None = None,
    split: bool = False,
) -> dict | None:
    """POST /v1/quote.

    When `cfg.enable_encoding` is true, the request asks Fynd to encode the
    executable transaction. The returned order quote carries `transaction`
    ({to, value, data}) and `fee_breakdown`. The outer `solve_time_ms` is
    stashed onto the order dict under `_solve_time_ms` so callers can persist
    it per quote.
    """
    sender = cfg.tenderly_from_address or SENDER_DEFAULT
    # split=True → min_responses=0: wait for ALL solver pools (or timeout) and
    # keep the best route, so the path_frank_wolfe split solver is actually used.
    # We only do this for the handful of headline "anchor" quotes (the depth
    # levels shown on the site). The bulk 400-quote depth sweep and mid probes
    # use min_responses=1 (return on the first/fast bellman_ford response) so the
    # per-block collection stays inside the ~12s block budget. With 0 on every
    # quote the cycle overruns the budget and the collector falls behind.
    options: dict = {"timeout_ms": cfg.fynd_timeout_ms,
                     "min_responses": 0 if split else 1}
    if cfg.enable_encoding:
        # Per OpenAPI: slippage is decimal-string (e.g. "0.001" = 0.1%);
        # transfer_from is the simplest path, no Permit2 signature needed.
        options["encoding_options"] = {"slippage": cfg.slippage, "transfer_type": "transfer_from"}
    payload = {
        "orders": [{
            "token_in": token_in,
            "token_out": token_out,
            "amount": str(amount),
            "side": "sell",
            "sender": sender,
        }],
        "options": options,
    }
    try:
        r = requests.post(
            f"{cfg.fynd_base_url}/v1/quote",
            json=payload,
            timeout=cfg.http_timeout_s,
        )
        r.raise_for_status()
        outer = r.json()
        orders = outer.get("orders", [])
        q = orders[0] if orders else None
        if q and str(q.get("status", "")).lower() == "success":
            # Stash per-call envelope timing so each persisted quote can carry it.
            q["_solve_time_ms"] = outer.get("solve_time_ms")
            return q
        if state is not None:
            state.add_quote_failure({**(ctx or {}), "reason": "status", "raw": (q or {}).get("status")})
        return None
    except Exception as e:
        if state is not None:
            state.add_quote_failure({**(ctx or {}), "reason": "exception", "msg": str(e)[:200]})
        return None


# ── Pricing helpers ──────────────────────────────────────────────────────────


def quote_price_in_per_out(quote: dict, amount_in_units: float, decimals_out: int) -> float | None:
    """USDC per ETH for buy, USDC per ETH for sell (sell side passes units of in)."""
    ao = quote.get("amount_out")
    if not ao:
        return None
    # float64 is exact to 2^53; max search size is 5e13 atomic units ($50M
    # USDC), comfortably inside. Display-grade math only; never reuse this
    # for anything that signs or settles a transaction.
    out_units = int(ao) / 10 ** decimals_out
    return amount_in_units / out_units if out_units > 0 else None


def impact_pct(observed_price: float, spot: float, direction: str) -> float:
    if direction == "buy":
        return (observed_price / spot - 1.0) * 100.0
    return (1.0 - observed_price / spot) * 100.0


# ── Spot / robust mid ────────────────────────────────────────────────────────


ROBUST_MID_MIN_DEPTH_USD = 2_500.0
ROBUST_MID_MAX_DEPTH_USD = 10_000.0
ROBUST_MID_TARGET_DEPTH_USD = 5_000.0
ROBUST_MID_SAMPLES = 5


def fynd_spot(cfg: Config, state: CollectorState) -> float | None:
    """Spot in token_in-per-token_out terms (USDC/ETH for ETH/USDC pair)."""
    probe_usd = 1_000.0
    q = fynd_quote(
        cfg,
        cfg.token_in.address,
        cfg.token_out.address,
        cfg.token_in.atomic(probe_usd),
        state,
        {"phase": "spot"},
    )
    if not q:
        return None
    return quote_price_in_per_out(q, probe_usd, cfg.token_out.decimals)


def _mid_at_depth(cfg: Config, depth_usd: float, spot: float, state: CollectorState) -> float | None:
    buy = fynd_quote(
        cfg,
        cfg.token_in.address,
        cfg.token_out.address,
        cfg.token_in.atomic(depth_usd),
        state,
        {"phase": "mid_buy", "depth_usd": depth_usd},
    )
    sell = fynd_quote(
        cfg,
        cfg.token_out.address,
        cfg.token_in.address,
        cfg.token_out.atomic(depth_usd / spot),
        state,
        {"phase": "mid_sell", "depth_usd": depth_usd},
    )
    if not buy or not sell:
        return None
    bp = quote_price_in_per_out(buy, depth_usd, cfg.token_out.decimals)
    # For sell: amount_in is token_out units; amount_out is token_in units (USDC).
    ao = sell.get("amount_out")
    if ao is None:
        return None
    in_units_out = int(ao) / 10 ** cfg.token_in.decimals  # USDC out
    out_units_in = depth_usd / spot                       # ETH in
    sp = in_units_out / out_units_in if out_units_in > 0 else None
    if bp is None or sp is None:
        return None
    return (bp + sp) / 2.0


def _choose_robust_mid(pairs: list[tuple[float, float]]) -> tuple[float, float] | None:
    clean = [
        (float(depth), float(mid))
        for depth, mid in pairs
        if is_finite_number(depth) and depth > 0 and is_finite_number(mid)
    ]
    if not clean:
        return None

    band = [
        pair for pair in clean
        if ROBUST_MID_MIN_DEPTH_USD <= pair[0] <= ROBUST_MID_MAX_DEPTH_USD
    ]
    if len(band) < 3:
        band = sorted(
            clean,
            key=lambda dm: abs(math.log(dm[0] / ROBUST_MID_TARGET_DEPTH_USD)),
        )[:ROBUST_MID_SAMPLES]

    mids = [mid for _, mid in band]
    median_mid = statistics.median(mids)
    median_depth = min(band, key=lambda dm: abs(dm[1] - median_mid))[0]
    return round(median_mid, 6), round(median_depth, 2)


def compute_robust_mid_from_sweeps(
    sweep_buy: list[dict],
    sweep_sell: list[dict],
) -> tuple[float, float] | None:
    """Near-marginal two-sided mid from already-collected on-chain sweep quotes.

    The price line should represent the current clearing price, not liquidity at
    six- or seven-figure depth. Pair same-notional buy and sell quotes in the
    shallow reliable band and take their median midpoint.
    """
    buy_by_depth: dict[float, float] = {}
    for rec in sweep_buy:
        depth = rec.get("amount_usd")
        price = rec.get("price")
        if is_finite_number(depth) and is_finite_number(price):
            buy_by_depth[round(float(depth), 2)] = float(price)

    pairs: list[tuple[float, float]] = []
    for rec in sweep_sell:
        depth = rec.get("amount_usd")
        sell_price = rec.get("price")
        if not is_finite_number(depth) or not is_finite_number(sell_price):
            continue
        buy_price = buy_by_depth.get(round(float(depth), 2))
        if buy_price is None:
            continue
        pairs.append((float(depth), (buy_price + float(sell_price)) / 2.0))

    return _choose_robust_mid(pairs)


def _robust_mid_probe_depths(max_depth_usd: float) -> list[float]:
    max_d = max(ROBUST_MID_MIN_DEPTH_USD, min(max_depth_usd, ROBUST_MID_MAX_DEPTH_USD))
    if max_d <= ROBUST_MID_MIN_DEPTH_USD:
        return [ROBUST_MID_MIN_DEPTH_USD]
    return [
        math.exp(
            math.log(ROBUST_MID_MIN_DEPTH_USD)
            + i * (math.log(max_d) - math.log(ROBUST_MID_MIN_DEPTH_USD))
            / (ROBUST_MID_SAMPLES - 1)
        )
        for i in range(ROBUST_MID_SAMPLES)
    ]


def compute_robust_mid(cfg: Config, spot: float, max_depth_usd: float, state: CollectorState) -> tuple[float, float]:
    depths = _robust_mid_probe_depths(max_depth_usd)
    pairs: list[tuple[float, float]] = []
    with ThreadPoolExecutor(max_workers=min(cfg.max_workers, len(depths))) as ex:
        futs = {ex.submit(_mid_at_depth, cfg, d, spot, state): d for d in depths}
        for fut in as_completed(futs):
            m = fut.result()
            if m is not None:
                pairs.append((futs[fut], m))
    robust = _choose_robust_mid(pairs)
    if robust is None:
        state.mid_degraded_count += 1
        state.add_error("robust mid degraded: every mid probe failed; using spot", "mid")
        return spot, ROBUST_MID_MIN_DEPTH_USD
    return robust


def _route_meta_of(quote: dict | None) -> dict:
    route = (quote or {}).get("route") or {}
    swaps = route.get("swaps") or []
    protos = sorted({s.get("protocol") for s in swaps if s.get("protocol")})
    pools = sorted({s.get("component_id") for s in swaps if s.get("component_id")})
    return {"protocols": protos, "pool_count": len(pools), "hop_count": len(swaps), "pools": pools}


def _quote_buy(cfg: Config, usd: float, state: CollectorState, split: bool = False) -> tuple[dict | None, float | None]:
    q = fynd_quote(cfg, cfg.token_in.address, cfg.token_out.address,
                   cfg.token_in.atomic(usd), state, {"phase": "sweep", "side": "buy", "usd": usd}, split=split)
    if not q:
        return None, None
    price = quote_price_in_per_out(q, usd, cfg.token_out.decimals)
    return q, price


def _quote_sell(cfg: Config, usd: float, spot: float, state: CollectorState, split: bool = False) -> tuple[dict | None, float | None]:
    q = fynd_quote(cfg, cfg.token_out.address, cfg.token_in.address,
                   cfg.token_out.atomic(usd / spot), state, {"phase": "sweep", "side": "sell", "usd": usd}, split=split)
    if not q:
        return None, None
    ao = q.get("amount_out")
    if ao is None:
        return None, None
    in_out_units = int(ao) / 10 ** cfg.token_in.decimals
    out_in_units = usd / spot
    price = in_out_units / out_in_units if out_in_units > 0 else None
    return q, price


def sweep_side(cfg: Config, side: str, spot: float, state: CollectorState, num_samples: int) -> list[dict]:
    """Log-spaced sweep of `num_samples` trade sizes from SEARCH_MIN_USD to
    SEARCH_MAX_USD. Each entry is one Fynd quote. Sorted ascending by
    amount_usd. Failed quotes are skipped.
    """
    lo = math.log(cfg.search_min_usd)
    hi = math.log(cfg.search_max_usd)
    sizes = [math.exp(lo + (hi - lo) * i / max(num_samples - 1, 1)) for i in range(num_samples)]

    def one(usd: float) -> dict | None:
        q, price = (_quote_buy(cfg, usd, state) if side == "buy"
                    else _quote_sell(cfg, usd, spot, state))
        if not q or price is None or not is_finite_number(price):
            return None
        return {
            "amount_usd": round(usd, 2),
            "price": round(price, 6),
            "impact_pct": round(impact_pct(price, spot, side), 6),
            "amount_in": q["amount_in"],
            "amount_out": q["amount_out"],
            "amount_out_net_gas": q.get("amount_out_net_gas"),
            "gas_estimate": q.get("gas_estimate"),
            "route": _route_meta_of(q),
            # Full Fynd order quote, used for SQLite persistence (transaction,
            # fee_breakdown, gas_price, block.hash, per-leg swaps). Stripped
            # before this entry flows into latest.json.
            "_raw": q,
            "_solve_time_ms": q.get("_solve_time_ms"),
        }

    points: list[dict] = []
    with ThreadPoolExecutor(max_workers=cfg.max_workers) as ex:
        futs = {ex.submit(one, s): s for s in sizes}
        for fut in as_completed(futs):
            try:
                rec = fut.result()
                if rec is not None:
                    points.append(rec)
            except Exception as e:
                state.add_error(f"sweep {side} error: {e}", "sweep")
    points.sort(key=lambda p: p["amount_usd"])
    return points


def anchor_target_from_sweep(cfg: Config, side: str, target_pct: float,
                              sweep: list[dict], spot: float, state: CollectorState,
                              max_iters: int = 5, tolerance: float = 0.02) -> dict | None:
    """Tight bisection seeded by the sweep's bracket for the given impact
    target. Returns a real Fynd quote at (or close to) the target, no
    interpolated quote bytes. None when the sweep can't straddle the
    target (capped or failed)."""
    if not sweep or len(sweep) < 2:
        return None
    bracket = None
    for i in range(len(sweep) - 1):
        da = sweep[i]["impact_pct"] - target_pct
        db = sweep[i + 1]["impact_pct"] - target_pct
        if da == 0 or (da < 0 <= db) or (da > 0 >= db):
            bracket = (sweep[i], sweep[i + 1])
            break
    if bracket is None:
        return None
    lo_usd = bracket[0]["amount_usd"]
    hi_usd = bracket[1]["amount_usd"]
    best: dict | None = None
    best_diff = float("inf")
    for _ in range(max_iters):
        mid_usd = math.exp((math.log(lo_usd) + math.log(hi_usd)) / 2)
        # Anchors are the headline depth levels shown on the site; run them
        # through the split (path_frank_wolfe) solver via min_responses=0.
        q, price = (_quote_buy(cfg, mid_usd, state, split=True) if side == "buy"
                    else _quote_sell(cfg, mid_usd, spot, state, split=True))
        if not q or price is None or not is_finite_number(price):
            break
        imp = impact_pct(price, spot, side)
        diff = abs(imp - target_pct)
        if diff < best_diff:
            best = {"q": q, "price": price, "impact": imp, "usd": mid_usd}
            best_diff = diff
        if diff / max(target_pct, 0.001) < tolerance:
            break
        if imp < target_pct:
            lo_usd = mid_usd
        else:
            hi_usd = mid_usd
    return best


def derive_level_from_sweep(sweep: list[dict], target_pct: float, side: str,
                             search_min_usd: float, search_max_usd: float) -> dict:
    """Reconstruct the old per-target level record from the nearest real
    sweep quote. bound=max if sweep tops out below target, bound=min if
    even the smallest sweep size already exceeds target, bound=failed if
    the sweep is empty.
    """
    record: dict = {
        "target_impact_pct": target_pct,
        "actual_impact_pct": None,
        "target_reached": False,
        "bound": "failed",
        "amount_usd": None,
        "amount_in": None,
        "amount_out": None,
        "amount_out_net_gas": None,
        "price": None,
        "gas_estimate": None,
        "route": None,
        "search_min_usd": search_min_usd,
        "search_max_usd": search_max_usd,
        "quote_source": QUOTE_SOURCE,
        "direction": side,
        "derived_from": "nearest_real_quote",
    }
    if not sweep:
        return record

    def _from_entry(entry: dict, *, bound: str, target_reached: bool) -> dict:
        record.update({
            "actual_impact_pct": entry["impact_pct"],
            "target_reached": target_reached,
            "bound": bound,
            "amount_usd": entry["amount_usd"],
            "amount_in": entry["amount_in"],
            "amount_out": entry["amount_out"],
            "amount_out_net_gas": entry["amount_out_net_gas"],
            "price": entry["price"],
            "gas_estimate": entry["gas_estimate"],
            "route": entry["route"],
            "_raw": entry.get("_raw"),
            "_solve_time_ms": entry.get("_solve_time_ms"),
        })
        return record

    # Direction-agnostic crossing scan: measured impact is not strictly
    # monotonic in size (route recomposition can dip impact as size grows), so
    # look for ANY sign change of (impact - target) between adjacent points and
    # take the crossing at the smallest size: "how much can you trade before
    # X%". Use the closer endpoint's complete measured values; anchored targets
    # can still replace it with a tighter real quote via bisection.
    for i in range(len(sweep) - 1):
        a, b = sweep[i], sweep[i + 1]
        da = a["impact_pct"] - target_pct
        db = b["impact_pct"] - target_pct
        if da > 0 and i == 0:
            return _from_entry(a, bound="min", target_reached=False)
        if da == 0 or (da < 0 <= db) or (da > 0 >= db):
            di = b["impact_pct"] - a["impact_pct"]
            t = (target_pct - a["impact_pct"]) / di if di else 0.0
            closer = a if t < 0.5 else b
            return _from_entry(closer, bound="none", target_reached=True)
    top = sweep[-1]
    if top["impact_pct"] < target_pct:
        return _from_entry(top, bound="max", target_reached=False)
    if sweep[0]["impact_pct"] > target_pct:
        return _from_entry(sweep[0], bound="min", target_reached=False)
    return record


# ── Route metadata aggregation ───────────────────────────────────────────────


def extract_route_meta(quotes: list[dict]) -> dict:
    protocols: set[str] = set()
    pools: set[str] = set()
    for q in quotes:
        route = (q or {}).get("route") or {}
        for swap in route.get("swaps", []) or []:
            p = swap.get("protocol")
            c = swap.get("component_id")
            if p:
                protocols.add(p)
            if c:
                pools.add(c)
    return {"protocols": sorted(protocols), "pools": sorted(pools), "pool_count": len(pools)}


def _raw_quote_block(raw: dict | None) -> int | None:
    blk = (raw or {}).get("block") or {}
    bn = blk.get("number")
    return bn if isinstance(bn, int) and bn > 0 else None


def _quote_record_matches_block(rec: dict, block: int) -> bool:
    bn = _raw_quote_block(rec.get("_raw"))
    return bn is None or bn == block


def _iter_quote_records(levels: dict[str, dict], sweeps: list[list[dict]]) -> list[dict]:
    records: list[dict] = []
    for sides in levels.values():
        records.extend(sides.values())
    for sweep in sweeps:
        records.extend(sweep)
    return records


# ── Snapshot ─────────────────────────────────────────────────────────────────


def collect_snapshot(cfg: Config, state: CollectorState) -> tuple[dict | None, dict | None]:
    """Returns (snap_lean, persist_payload).

    `snap_lean` is the JSON-safe per-block document written to latest.json
    (no `_raw` / `_solve_time_ms` keys).
    `persist_payload` is a persistence-ready dict the caller can store as it sees fit.
    Both are None on failure.
    """
    started = time.monotonic()
    try:
        block = get_block_number(cfg.rpc_url)
    except Exception as e:
        state.add_error(f"rpc_block_number: {e}", "snapshot")
        return None, None

    spot = fynd_spot(cfg, state)
    if spot is None or not is_finite_number(spot):
        state.add_error("fynd_spot returned no usable price", "snapshot")
        return None, None

    # ── Dense sweep (replaces N independent binary searches) ────────────
    # `cfg.sweep_samples_per_side` real Fynd quotes per direction, log-
    # spaced across SEARCH_MIN..SEARCH_MAX_USD. The per-target `levels`
    # rows expected by older clients are then taken from the nearest quote
    # around each target crossing. Every value remains a real measurement.
    sweep_buy_fut: Any = None
    sweep_sell_fut: Any = None
    with ThreadPoolExecutor(max_workers=2) as outer:
        sweep_buy_fut = outer.submit(sweep_side, cfg, "buy", spot, state, cfg.sweep_samples_per_side)
        sweep_sell_fut = outer.submit(sweep_side, cfg, "sell", spot, state, cfg.sweep_samples_per_side)
        sweep_buy = sweep_buy_fut.result()
        sweep_sell = sweep_sell_fut.result()

    levels: dict[str, dict] = {}
    for lvl in cfg.impact_levels:
        key = str(lvl)
        levels[key] = {
            "buy":  derive_level_from_sweep(sweep_buy,  lvl, "buy",  cfg.search_min_usd, cfg.search_max_usd),
            "sell": derive_level_from_sweep(sweep_sell, lvl, "sell", cfg.search_min_usd, cfg.search_max_usd),
        }

    # Anchored bisections for the headline targets. Each one issues a few
    # extra Fynd quotes to land a real measurement near the target instead
    # of carrying quote bytes from a "closer endpoint" of the sweep bracket.
    # If the sweep already shows a target is capped (no bracket), we skip
    # the anchor; the bound=max record stays as-is.
    ANCHOR_TARGETS = [0.5, 1.0, 5.0, 10.0, 25.0, 50.0]
    anchor_tasks: dict[tuple, Any] = {}
    with ThreadPoolExecutor(max_workers=cfg.max_workers) as ex:
        for target in ANCHOR_TARGETS:
            for side in ("buy", "sell"):
                sweep = sweep_buy if side == "buy" else sweep_sell
                # max_iters=3 (not the default 5): each anchor iter is a slow
                # split (path_frank_wolfe) quote, and the sweep already brackets
                # the target tightly, so 3 lands near-target while keeping the
                # per-block cycle inside the ~12s budget on the live machine.
                anchor_tasks[(target, side)] = ex.submit(
                    anchor_target_from_sweep, cfg, side, target, sweep, spot, state, 3,
                )
        for (target, side), fut in anchor_tasks.items():
            try:
                anchor = fut.result()
            except Exception as e:
                state.add_error(f"anchor {side} {target}: {e}", "anchor")
                anchor = None
            if not anchor:
                continue
            key = str(target)
            rec = levels.get(key, {}).get(side)
            if not rec:
                continue
            # Replace the nearest sweep endpoint bytes with the anchored
            # quote bytes. Bound/target_reached recomputed below.
            within = abs(anchor["impact"] - target) / max(target, 0.001) < cfg.search_tol
            rec.update({
                "actual_impact_pct": round(anchor["impact"], 6),
                "target_reached": bool(within),
                "bound": "none" if within else ("max" if anchor["impact"] < target else "min"),
                "amount_usd": round(anchor["usd"], 2),
                "amount_in": anchor["q"]["amount_in"],
                "amount_out": anchor["q"]["amount_out"],
                "amount_out_net_gas": anchor["q"].get("amount_out_net_gas"),
                "price": round(anchor["price"], 6),
                "gas_estimate": anchor["q"].get("gas_estimate"),
                "route": _route_meta_of(anchor["q"]),
                "derived_from": "anchored_bisection",
                "_raw": anchor["q"],
                "_solve_time_ms": anchor["q"].get("_solve_time_ms"),
            })

    # route_meta_by_level: take the route recorded on each level's best
    # quote (now persisted per record), aggregate cross-protocols too.
    KEY_LEVELS = ("0.1", "1.0", "10.0", "25.0", "50.0")
    route_meta_by_level: dict[str, dict] = {}
    union_protocols: set[str] = set()
    union_pools: set[str] = set()
    for k in KEY_LEVELS:
        for side in ("buy", "sell"):
            rec = levels.get(k, {}).get(side) or {}
            r = rec.get("route") or {}
            if not r.get("protocols") and not r.get("pools"):
                continue
            route_meta_by_level.setdefault(k, {})[side] = {
                "protocols": r.get("protocols", []),
                "pool_count": r.get("pool_count"),
                "hop_count": r.get("hop_count"),
                "amount_usd": rec.get("amount_usd"),
                "target_reached": rec.get("target_reached"),
                "bound": rec.get("bound"),
            }
            union_protocols.update(r.get("protocols", []))
            union_pools.update(r.get("pools", []))

    # Headline route_meta: 1% buy probe (legacy field used by older clients)
    one_pct_rec = levels.get("1.0", {}).get("buy") or {}
    one_pct_route = one_pct_rec.get("route") or {}
    route_meta = {
        "probe": "1% USDC→WETH",
        "protocols": one_pct_route.get("protocols", []),
        "pool_count": one_pct_route.get("pool_count"),
        "hop_count": one_pct_route.get("hop_count"),
        "pools": one_pct_route.get("pools", []),
        "union_across_probed_levels": {
            "protocols": sorted(union_protocols),
            "pool_count": len(union_pools),
        },
    }

    quote_records = _iter_quote_records(levels, [sweep_buy, sweep_sell])

    # ── Block identity from the quotes themselves ───────────────────────
    # The RPC head at cycle start can differ from the block Fynd actually
    # solved against (a ~10s sweep straddles boundaries). Majority wins; a
    # split is flagged rather than silently relabelled.
    _bn_counts: dict[int, int] = {}
    for _rec in quote_records:
        _bn = _raw_quote_block(_rec.get("_raw"))
        if _bn is not None:
            _bn_counts[_bn] = _bn_counts.get(_bn, 0) + 1
    mixed_block = False
    if _bn_counts:
        _majority = max(_bn_counts, key=_bn_counts.get)
        mixed_block = len(_bn_counts) > 1
        if mixed_block:
            state.mixed_blocks += 1
            state.add_error(
                f"mixed-block snapshot: {_bn_counts} → labelled {_majority}", "block_identity")
        block = _majority

    sweep_buy_for_mid = [rec for rec in sweep_buy if _quote_record_matches_block(rec, block)]
    sweep_sell_for_mid = [rec for rec in sweep_sell if _quote_record_matches_block(rec, block)]
    robust = compute_robust_mid_from_sweeps(sweep_buy_for_mid, sweep_sell_for_mid)
    if robust is None:
        max_depth_anchor = sweep_buy_for_mid[-1]["amount_usd"] if sweep_buy_for_mid else 500_000.0
        robust = compute_robust_mid(cfg, spot, max_depth_anchor, state)
    robust_mid, median_depth = robust

    duration_ms = int((time.monotonic() - started) * 1000)

    # Block-wide context for SQLite + tenderly URL: gas_price + block hash
    # + block timestamp. In mixed cycles, only use context from the block that
    # will label persisted rows.
    block_hash: str | None = None
    block_ts: int | None = None        # epoch seconds
    block_gas_price_wei: str | None = None
    for rec in quote_records:
        raw = rec.get("_raw")
        if not raw or not _quote_record_matches_block(rec, block):
            continue
        blk = raw.get("block") or {}
        if not block_hash and blk.get("hash"):
            block_hash = blk["hash"]
        if not block_ts and blk.get("timestamp"):
            block_ts = blk["timestamp"]
        if not block_gas_price_wei and raw.get("gas_price"):
            block_gas_price_wei = raw["gas_price"]
        if block_hash and block_ts and block_gas_price_wei:
            break

    # ── Persist payload (SQLite) ────────────────────────────────────────
    completeness = 0
    levels_payload: list[dict] = []
    legs_payload: list[dict] = []
    responses_payload: list[dict] = []
    for key, sides in levels.items():
        target_pct = float(key)
        for side, rec in sides.items():
            if not _quote_record_matches_block(rec, block):
                continue
            # token_out for this side determines the "out" decimals.
            tok_out_dec = (cfg.token_out.decimals if side == "buy" else cfg.token_in.decimals)
            eff_price = rec.get("price")
            actual = rec.get("actual_impact_pct")
            price_impact_bps = derive_price_impact_bps(side, eff_price, robust_mid)
            gas_eth, gas_tok_out = derive_gas_costs(
                side=side,
                amount_out_atomic=rec.get("amount_out"),
                amount_out_net_gas_atomic=rec.get("amount_out_net_gas"),
                token_out_decimals=tok_out_dec,
                gas_estimate_units=rec.get("gas_estimate"),
                gas_price_wei=block_gas_price_wei,
                mid_price=robust_mid,
            )
            levels_payload.append({
                "block": block, "side": side, "target_impact_pct": target_pct,
                "actual_impact_pct": actual,
                "price_impact_bps": price_impact_bps,
                "amount_usd": rec.get("amount_usd"),
                "amount_in_atomic": rec.get("amount_in"),
                "amount_out_atomic": rec.get("amount_out"),
                "amount_out_net_gas_atomic": rec.get("amount_out_net_gas"),
                "effective_price": eff_price,
                "gas_estimate_units": int(rec["gas_estimate"]) if rec.get("gas_estimate") else None,
                "gas_cost_eth": gas_eth,
                "gas_cost_token_out": gas_tok_out,
                "bound": rec.get("bound"),
                "target_reached": rec.get("target_reached"),
                "quote_source": QUOTE_SOURCE,
                "derived_from": rec.get("derived_from"),
            })

            raw = rec.get("_raw")
            if not raw:
                continue

            # Route legs are tiny and feed the per-rung route diagram, so they
            # persist for EVERY rung. Only the raw response JSON (the disk-heavy
            # part) is restricted to anchored targets below.
            full_route = (raw.get("route") or {})
            swaps = full_route.get("swaps") or []
            if swaps:
                completeness |= C_HAS_ROUTE_LEGS
            for i, sw in enumerate(swaps):
                ti_sym, ti_dec = known_token(sw.get("token_in"))
                to_sym, to_dec = known_token(sw.get("token_out"))
                legs_payload.append({
                    "block": block, "side": side, "target_impact_pct": target_pct,
                    "leg_index": i,
                    "protocol": sw.get("protocol") or "",
                    "component_id": sw.get("component_id") or "",
                    "token_in_address":  (sw.get("token_in") or "").lower(),
                    "token_in_symbol":   ti_sym,
                    "token_in_decimals": ti_dec,
                    "token_out_address": (sw.get("token_out") or "").lower(),
                    "token_out_symbol":  to_sym,
                    "token_out_decimals": to_dec,
                    "amount_in_atomic":  sw.get("amount_in") or "0",
                    "amount_out_atomic": sw.get("amount_out") or "0",
                    "gas_estimate_units": int(sw["gas_estimate"]) if sw.get("gas_estimate") else None,
                    "split": float(sw["split"]) if sw.get("split") not in (None, "") else None,
                })

            if rec.get("derived_from") != "anchored_bisection":
                continue
            completeness |= C_HAS_RAW_JSON
            tx = raw.get("transaction") or None
            if tx and tx.get("data"):
                completeness |= C_HAS_TX
            tenderly_url, tenderly_status = build_tenderly_url(
                cfg.tenderly_from_address, tx, block,
            )
            fb = raw.get("fee_breakdown") or {}
            responses_payload.append({
                "block": block, "side": side, "target_impact_pct": target_pct,
                "order_id": raw.get("order_id"),
                "solve_time_ms": rec.get("_solve_time_ms"),
                "raw_response_json": json.dumps(
                    {k: v for k, v in raw.items() if not k.startswith("_")},
                    separators=(",", ":"),
                ),
                "executable_to":       (tx or {}).get("to") if tx else None,
                "executable_calldata": (tx or {}).get("data") if tx else None,
                "executable_value":    (tx or {}).get("value") if tx else None,
                "fee_router_atomic":        fb.get("router_fee"),
                "fee_client_atomic":        fb.get("client_fee"),
                "fee_max_slippage_atomic":  fb.get("max_slippage"),
                "fee_min_received_atomic":  fb.get("min_amount_received"),
                "tenderly_url": tenderly_url,
                "tenderly_status": tenderly_status,
            })

    curve_payload: list[dict] = []
    for _side_name, _swp in (("buy", sweep_buy), ("sell", sweep_sell)):
        for _idx, _e in enumerate(_swp):
            if not _quote_record_matches_block(_e, block):
                continue
            _r = _e.get("route") or {}
            curve_payload.append({
                "block": block, "side": _side_name, "point_index": _idx,
                "amount_usd": _e.get("amount_usd"), "price": _e.get("price"),
                "impact_pct": _e.get("impact_pct"),
                "amount_in_atomic": _e.get("amount_in"),
                "amount_out_atomic": _e.get("amount_out"),
                "amount_out_net_gas_atomic": _e.get("amount_out_net_gas"),
                "gas_estimate_units": int(_e["gas_estimate"]) if _e.get("gas_estimate") else None,
                "protocols": "|".join(_r.get("protocols") or []),
                "pool_count": _r.get("pool_count"), "hop_count": _r.get("hop_count"),
            })

    persist_payload = {
        "block_row": {
            "block": block,
            "mixed_block": 1 if mixed_block else 0,
            "ts": int(block_ts * 1000) if block_ts else int(time.time() * 1000),
            "block_hash": block_hash,
            "spot_price": round(spot, 6),
            "robust_mid": robust_mid,
            "median_depth": median_depth,
            "duration_ms": duration_ms,
            "gas_price_wei": block_gas_price_wei,
            "pair": cfg.pair_label,
            "collector_version": cfg.collector_version,
            "completeness": completeness,
        },
        "levels": levels_payload,
        "curve_points": curve_payload,
        "route_legs": legs_payload,
        "quote_responses": responses_payload,
    }

    # ── Lean snap (JSON-safe, no `_raw` / `_solve_time_ms`) ─────────────
    def _strip(d: dict) -> dict:
        return {k: v for k, v in d.items() if not k.startswith("_")}

    lean_levels = {k: {s: _strip(rec) for s, rec in sides.items()} for k, sides in levels.items()}
    lean_curve_buy  = [_strip(e) for e in sweep_buy]
    lean_curve_sell = [_strip(e) for e in sweep_sell]

    snap = {
        "block": block,
        "time": utcnow_iso(),
        "duration_ms": duration_ms,
        "pair": cfg.pair_label,
        "token_in": {"address": cfg.token_in.address, "symbol": cfg.token_in.symbol, "decimals": cfg.token_in.decimals},
        "token_out": {"address": cfg.token_out.address, "symbol": cfg.token_out.symbol, "decimals": cfg.token_out.decimals},
        "spot_price": round(spot, 6),
        "robust_mid": robust_mid,
        "median_depth": median_depth,
        "impact_levels": cfg.impact_levels,
        "levels": lean_levels,
        "curve": {
            "buy":  lean_curve_buy,
            "sell": lean_curve_sell,
            "samples_per_side": cfg.sweep_samples_per_side,
            "search_min_usd":   cfg.search_min_usd,
            "search_max_usd":   cfg.search_max_usd,
        },
        "route_meta": route_meta,
        "route_meta_by_level": route_meta_by_level,
        "quote_source": QUOTE_SOURCE,
        "block_hash": block_hash,
        "block_ts": block_ts,
        "mixed_block": mixed_block,
        "gas_price_wei": block_gas_price_wei,
    }
    return snap, persist_payload
