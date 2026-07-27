"""eth_price_poc: build block-by-block on-chain depth data locally.

Quotes come from your own Fynd instance over Tycho-indexed liquidity. Nothing
here reads a PropellerHeads API, so every number is one you measured and can
re-measure. See README for the full schema. Quickstart:

    from eth_price_poc import client
    feed = client()
    snap = feed.collect()
    df   = feed.history_as_dataframe()
"""
from .local import EthPricePoCDataUnavailable, LocalFeed, client

__all__ = ["LocalFeed", "EthPricePoCDataUnavailable", "client"]
__version__ = "0.3.0"
