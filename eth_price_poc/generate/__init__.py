"""Local data generation: produce the same block-by-block depth snapshots the
hosted dataset serves, from your own Fynd instance. Requires the `generate`
extra (`pip install "eth-price-poc-sdk[generate]"`) and a running Fynd with a
Tycho API key. See the README "Generate your own data" section.

    from eth_price_poc.generate import PairConfig, collect_snapshot, NullSink
    snap, _ = collect_snapshot(PairConfig(), NullSink())
"""
from .config import NullSink, PairConfig, TokenSpec, USDC, WETH
from .core import collect_snapshot

__all__ = [
    "PairConfig", "TokenSpec", "NullSink", "USDC", "WETH", "collect_snapshot",
]
