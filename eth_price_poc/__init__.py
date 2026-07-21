"""eth_price_poc: tiny Python client for the Price-of-Ethereum PoC dataset.

See README for the full schema. Quickstart:

    from eth_price_poc import client
    c = client()
    snap = c.latest()
    df   = c.history_as_dataframe(limit=720)
"""
from .client import EthPricePoCClient, EthPricePoCDataUnavailable, client

__all__ = ["EthPricePoCClient", "EthPricePoCDataUnavailable", "client"]
__version__ = "0.1.0"
