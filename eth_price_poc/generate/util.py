"""Pure helpers used by the collector + the API: tenderly URL,
side-aware price-impact derivation, gas-cost derivation, and a tiny
token registry.
"""
from __future__ import annotations

from urllib.parse import urlencode


KNOWN_TOKENS: dict[str, tuple[str, int]] = {
    "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48": ("USDC", 6),
    "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2": ("WETH", 18),
    "0x0000000000000000000000000000000000000000": ("ETH", 18),
    "0x2260fac5e5542a773aa44fbcfedf7c193bc2c599": ("WBTC", 8),
    "0xdac17f958d2ee523a2206206994597c13d831ec7": ("USDT", 6),
    "0x6b175474e89094c44da98b954eedeac495271d0f": ("DAI", 18),
    "0x853d955acef822db058eb8505911ed77f175b99e": ("FRAX", 18),
    "0x5e8422345238f34275888049021821e8e08caa1f": ("frxETH", 18),
    "0xae78736cd615f374d3085123a210448e74fc6393": ("rETH", 18),
    "0x7f39c581f595b53c5cb19bd0b3f8da6c935e2ca0": ("wstETH", 18),
}


def known_token(address: str | None) -> tuple[str | None, int | None]:
    if not address:
        return None, None
    e = KNOWN_TOKENS.get(address.lower())
    return (e[0], e[1]) if e else (None, None)


def derive_price_impact_bps(side: str, effective_price: float | None, mid_price: float | None) -> float | None:
    """Side-aware, signed price impact in basis points.

    Buy ETH with USDC → effective_price = USDC paid / ETH received.
    Sell ETH to USDC → effective_price = USDC received / ETH sold.

    Convention from the spec: buy above mid is positive cost, sell below
    mid is negative cost. Same formula across sides keeps the sign right.
    """
    if not effective_price or not mid_price:
        return None
    try:
        return round((effective_price / mid_price - 1.0) * 10000.0, 1)
    except Exception:
        return None


def derive_gas_costs(
    *,
    side: str,
    amount_out_atomic: str | None,
    amount_out_net_gas_atomic: str | None,
    token_out_decimals: int,
    gas_estimate_units: int | str | None,
    gas_price_wei: str | None,
    mid_price: float | None,
) -> tuple[float | None, float | None]:
    """Returns (gas_cost_eth, gas_cost_token_out).

    Two independent derivations:
      - gas_cost_token_out from (amount_out − amount_out_net_gas) / 10^decimals.
        Authoritative because both come from the same Fynd response.
      - gas_cost_eth from gas_estimate × gas_price / 1e18. Independent check.
        For sell side (token_out=USDC), falls back to gas_cost_token_out / mid.
        For buy side (token_out=WETH), gas_cost_token_out IS gas_cost_eth.
    """
    gas_cost_token_out: float | None = None
    if amount_out_atomic is not None and amount_out_net_gas_atomic is not None:
        try:
            diff = int(amount_out_atomic) - int(amount_out_net_gas_atomic)
            if diff > 0:
                gas_cost_token_out = diff / (10 ** token_out_decimals)
        except (TypeError, ValueError):
            pass

    gas_cost_eth: float | None = None
    if gas_estimate_units is not None and gas_price_wei is not None:
        try:
            gas_cost_eth = int(gas_estimate_units) * int(gas_price_wei) / 1e18
        except (TypeError, ValueError):
            pass

    if gas_cost_eth is None and gas_cost_token_out is not None:
        if side == "buy":
            gas_cost_eth = gas_cost_token_out
        elif side == "sell" and mid_price:
            gas_cost_eth = gas_cost_token_out / mid_price

    return gas_cost_eth, gas_cost_token_out


def build_tenderly_url(
    from_addr: str | None,
    tx: dict | None,
    block_num: int | None,
    *,
    network: str = "1",
) -> tuple[str | None, str]:
    """Returns (url, status). status ∈ {ready, missing_sender, no_transaction}."""
    if not tx or not tx.get("to") or not tx.get("data"):
        return None, "no_transaction"
    if not from_addr:
        return None, "missing_sender"
    params = {
        "network": network,
        "from": from_addr,
        "contractAddress": tx["to"],
        "rawFunctionInput": tx["data"],
        "value": tx.get("value") or "0",
    }
    if block_num is not None:
        params["block"] = str(block_num)
    return f"https://dashboard.tenderly.co/simulator/new?{urlencode(params)}", "ready"
