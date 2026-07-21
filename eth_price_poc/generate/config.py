"""Pair configuration for local data generation.

The hosted collector passes its own richer config object (these fields are a
structural subset). Standalone users build a PairConfig, ETH/USDC by default.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class TokenSpec:
    address: str
    symbol: str
    decimals: int

    def atomic(self, units: float) -> int:
        return int(units * 10 ** self.decimals)


# Ethereum mainnet ETH/USDC: the default pair the hosted dataset serves.
USDC = TokenSpec("0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48", "USDC", 6)
WETH = TokenSpec("0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2", "WETH", 18)


@dataclass(frozen=True)
class PairConfig:
    token_in: TokenSpec = USDC
    token_out: TokenSpec = WETH
    pair_label: str = "ETH/USDC"

    fynd_base_url: str = "http://127.0.0.1:3000"
    fynd_timeout_ms: int = 8000
    http_timeout_s: int = 12
    rpc_url: str = "https://ethereum.publicnode.com"

    # Anchoring and the hosted dataset key levels on "1.0"-style strings.
    # core.py canonicalizes keys via str(float(level)); these literals are
    # floats too so the intended key form is obvious at a glance.
    impact_levels: list[float] = field(default_factory=lambda: [
        0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 5.0,
        7.5, 10.0, 15.0, 25.0, 35.0, 50.0,
    ])
    sweep_samples_per_side: int = 100
    max_workers: int = 6
    search_min_usd: float = 50.0
    search_max_usd: float = 50_000_000.0
    search_iters: int = 18
    search_tol: float = 0.05

    enable_encoding: bool = True
    slippage: str = "0.001"
    tenderly_from_address: str | None = None
    collector_version: str = "sdk-generate"


class NullSink:
    """No-op error sink so generation runs without the collector's state."""
    mid_degraded_count = 0
    mixed_blocks = 0

    def add_error(self, *_a, **_k) -> None: ...
    def add_quote_failure(self, *_a, **_k) -> None: ...
