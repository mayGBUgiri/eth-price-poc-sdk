"""eth_price_poc: retired. Use https://github.com/propeller-heads/price-of-ethereum.

See README for the migration table. The 0.2.0 surface is unchanged:

    from eth_price_poc import client
    c = client()
    snap = c.latest()
    df   = c.history_as_dataframe(limit=720)
"""
import warnings

from .client import EthPricePoCClient, EthPricePoCDataUnavailable, client

warnings.warn(
    "eth_price_poc is retired and will receive no further updates; it is superseded by "
    "price-of-ethereum (https://github.com/propeller-heads/price-of-ethereum), which "
    "measures the same data from a Fynd you run yourself.",
    DeprecationWarning,
    stacklevel=2,
)

__all__ = ["EthPricePoCClient", "EthPricePoCDataUnavailable", "client"]
__version__ = "0.3.0"
