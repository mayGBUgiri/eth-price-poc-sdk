"""Local data generation: produce the same block-by-block depth snapshots the
hosted dataset serves, from your own Fynd instance. Requires the `generate`
extra (`pip install "eth-price-poc-sdk[generate]"`) and a running Fynd with a
Tycho API key. See the README "Generate your own data" section.

    from eth_price_poc.generate import PairConfig, collect_snapshot, NullSink
    snap, _ = collect_snapshot(PairConfig(), NullSink())
"""
from .config import NullSink, PairConfig, TokenSpec, USDC, WETH
from .util import (
    KNOWN_TOKENS,
    build_tenderly_url,
    derive_gas_costs,
    derive_price_impact_bps,
    etherscan_address_url,
)
from .core import (
    C_FULL,
    C_HAS_RAW_JSON,
    C_HAS_ROUTE_LEGS,
    C_HAS_TX,
    QUOTE_SOURCE,
    SENDER_DEFAULT,
    anchor_target_from_sweep,
    collect_snapshot,
    compute_robust_mid,
    compute_robust_mid_from_sweeps,
    derive_level_from_sweep,
    extract_route_meta,
    fynd_health,
    fynd_quote,
    fynd_spot,
    get_block_number,
    impact_pct,
    is_finite_number,
    known_token,
    quote_price_in_per_out,
    sweep_side,
    utcnow_iso,
)

__all__ = [
    "PairConfig", "TokenSpec", "NullSink", "USDC", "WETH",
    "KNOWN_TOKENS", "build_tenderly_url", "derive_gas_costs",
    "derive_price_impact_bps", "etherscan_address_url",
    "collect_snapshot", "fynd_health", "fynd_quote", "fynd_spot",
    "sweep_side", "anchor_target_from_sweep", "derive_level_from_sweep",
    "compute_robust_mid", "compute_robust_mid_from_sweeps",
    "extract_route_meta", "get_block_number",
    "impact_pct", "quote_price_in_per_out", "known_token",
    "is_finite_number", "utcnow_iso",
    "C_HAS_TX", "C_HAS_ROUTE_LEGS", "C_HAS_RAW_JSON", "C_FULL",
    "QUOTE_SOURCE", "SENDER_DEFAULT",
]
