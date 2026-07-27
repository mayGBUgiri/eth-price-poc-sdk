# eth-price-poc-sdk

Build block-by-block on-chain depth data for ETH — or any pair Tycho indexes —
on your own machine.

Every number this SDK produces comes from a quote your own [Fynd](https://t.me/FyndPortalBot)
instance solved over Tycho-indexed liquidity. It does not read
[marketprice.xyz](https://marketprice.xyz/) or any other PropellerHeads API.
That is the point: the site shows one instance of this data, and this SDK is
how you generate it independently and check the numbers for yourself.

## What you get per block

A sweep of real Fynd quotes across trade sizes in both directions, and from it:

- **the price curve** — effective price at each size, ~200 measured points per side
- **depth at each impact target** — how much you can trade before price moves 0.5%, 1%, 5%, …
- **a robust mid** — median two-sided midpoint from shallow quotes, not a single pool's spot
- **the route behind every rung** — protocol, pool, split and gas, per leg
- **executable calldata** for the headline rungs, plus a Tenderly URL to simulate it

## Install

```bash
pip install "eth-price-poc-sdk @ git+https://github.com/propeller-heads/eth-price-poc-sdk.git"
# or, from a clone of this repo:  pip install -e .
```

## Run it yourself

**1. Get a Fynd (Tycho) API key.** Open the Fynd portal bot on Telegram,
[t.me/FyndPortalBot](https://t.me/FyndPortalBot), and follow the prompts.

**2. Run Fynd locally** with the key:

```bash
cargo install fynd --locked   # --locked matters: unlocked dep resolution can break the build
export TYCHO_API_KEY=<your key>
fynd serve --http-host 127.0.0.1 --http-port 3000
```

**3. Collect.** One JSON line per block on stdout:

```bash
python -m eth_price_poc.generate.run_local            # ETH/USDC, one block every ~12s
python -m eth_price_poc.generate.run_local --once     # a single snapshot, then exit
```

Or from Python:

```python
from eth_price_poc import client

feed = client()                 # ETH/USDC against http://127.0.0.1:3000
snap = feed.collect()           # measure one block

print(snap["block"], snap["spot_price"], snap["robust_mid"])
print(feed.status()["mode"])    # live / stale / degraded / starting
print(feed.coverage()["protocols_routed"])

# Per-rung route + executable quote for one depth cell
detail = feed.detail(snap["block"], "buy", 1.0)
print([leg["protocol"] for leg in detail["route_legs"]])

# Keep collecting; history covers what this process has measured
for _ in range(60):
    feed.collect()
df = feed.history_as_dataframe()      # needs the pandas extra
```

Point it at another pair by swapping `token_in` / `token_out` on `PairConfig` —
that is the only thing that changes:

```python
from eth_price_poc.generate import PairConfig, TokenSpec
from eth_price_poc import client

cfg = PairConfig(
    token_in=TokenSpec("0xdAC17F958D2ee523a2206206994597C13D831ec7", "USDT", 6),
    token_out=TokenSpec("0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599", "WBTC", 8),
    pair_label="WBTC/USDT",
)
feed = client(cfg)
```

## History is in-process

`history()` returns the blocks **this process has collected**, oldest first,
capped by `history_size` (720 by default). It starts empty. Nothing is written
to disk, so a fresh process starts from zero — leave the collector running to
build a window. Durable local storage is not implemented yet.

Everything a read method returns is yours: modify it freely, and the retained
window keeps serving the measurements it collected. The one cost to know about
is that copying a large window takes a moment, so pass `history(limit=N)` or
use `history_as_dataframe()` (which copies nothing) inside a per-block loop.

## Network surface

| Contacted | Why |
|---|---|
| your Fynd instance (`http://127.0.0.1:3000` by default) | every quote, price, route and block number |
| Tycho, indirectly | Fynd streams indexed liquidity from it using your API key |

No Ethereum RPC endpoint is used: block identity, hash, timestamp and gas price
all come out of the Fynd quote responses, so a snapshot is always labelled with
the block Fynd actually solved against. When a sweep straddles a block
boundary, the majority block wins and `mixed_block` is set rather than the
snapshot being silently relabelled.

## API surface

`client()` returns a `LocalFeed`.

| Method | Notes |
|---|---|
| `feed.collect()` | Measure one block, append it to the window, return the snapshot |
| `feed.latest()` | Most recently collected block; collects one if the window is empty |
| `feed.history(limit=N)` | Blocks collected by this process, oldest first |
| `feed.status()` | `mode`, `last_block`, `head_block`, `blocks_behind`, Fynd health. Costs one probe quote |
| `feed.coverage()` | Protocols and pools your quotes actually routed through, plus Fynd health |
| `feed.detail(block, side, target_impact_pct)` | Per-rung route legs, execution tooltip, Tenderly URL |
| `feed.export(block, side, target_impact_pct)` | Raw Fynd response, calldata, fee breakdown |
| `feed.curve_for_block(block_index, side)` | Dense curve for one retained block (`-1` = latest) |
| `feed.tokens()` | `token_in` / `token_out` metadata for the configured pair |
| `feed.history_as_dataframe(limit=N)` | pandas wrapper; install with `pip install "eth-price-poc-sdk[pandas]"` |

`detail()` and `export()` return `None` for a block outside the retained
window. Both snap to the nearest measured target and report the distance
(`legs_from_target`, `raw_from_target`; `None` when the target matched
exactly). Route legs are kept for every rung; raw responses and calldata only
for the anchored headline targets (0.5, 1, 5, 10, 25, 50%), which is why
`export()` for another rung resolves to the nearest anchor.

## Schema reference

Every retained block is a full snapshot — there is no slim-vs-rich distinction,
because nothing is being trimmed for transport.

```
block, time, duration_ms, pair, block_hash, block_ts, mixed_block
spot_price       # marginal-trade price (~$1K probe)
robust_mid       # median shallow two-sided mid from the sweep quotes
median_depth     # notional the mid was taken at
gas_price_wei, quote_source
token_in / token_out: { address, symbol, decimals }
impact_levels: [float]
levels: {
  "1.0": {
    "buy":  { target_impact_pct, actual_impact_pct, target_reached, bound,
              amount_usd, amount_in, amount_out, amount_out_net_gas, price,
              gas_estimate, route, search_min_usd, search_max_usd,
              direction, quote_source, derived_from }
    "sell": { ... }
  }, ...
}
curve: {
  "buy":  [ { amount_usd, price, impact_pct, amount_in, amount_out,
              amount_out_net_gas, gas_estimate, route }, ... ]
  "sell": [ ... ]
  samples_per_side, search_min_usd, search_max_usd
}
route_meta            # route behind the 1% buy probe
route_meta_by_level   # routes at 0.1, 1, 10, 25, 50%
```

Capped values are marked explicitly: `bound:"max"` when the search ceiling
can't reach the target impact, `bound:"min"` when even the smallest probed size
already exceeds it. `derived_from` is `anchored_bisection` for the headline
targets (a real quote bisected onto the target) and `nearest_real_quote` for
rungs taken from the sweep crossing. No value is ever interpolated.

### `detail(block, side, target_impact_pct)`

```
block, side, target_impact_pct, block_hash, block_ts_ms
route_legs: [ { leg_index, protocol, component_id, split,
                token_in:  { address, symbol, decimals },
                token_out: { address, symbol, decimals },
                amount_in_atomic, amount_out_atomic, gas_estimate_units,
                etherscan_url } ]
legs_from_target: float | null
tooltip: { actual_impact_pct, effective_price, mid, price_impact_bps,
           amount_usd, bound, target_reached, gas_cost_eth,
           gas_cost_token_out, gas_estimate_units, derived_from }
tenderly: { url, status }
raw_response_available: bool
```

`tenderly.status` is `ready`, or `missing_sender` when no
`tenderly_from_address` is configured, or `no_transaction` when the quote
carried no encoded calldata.

`split` carries Fynd's convention verbatim: `0` means "the remainder of the
input", not "zero percent". A single-leg route therefore reports `split: 0.0`
while routing the whole trade.

### `export(block, side, target_impact_pct)`

```
block, side, target_impact_pct, order_id, solve_time_ms
response:      { ... }        # raw Fynd order quote
executable:    { to, calldata, value }
fee_breakdown: { router_fee, client_fee, max_slippage, min_amount_received }
tenderly:      { url, status }
raw_from_target: float | null
```

## Reproducing what the site shows

The defaults collect 200 quotes per side per block, the resolution
marketprice.xyz publishes. The sweep floor is deliberately lower than the $10K
the site charts from: the shallow rungs of `impact_levels` and the robust-mid
band both need measurements below $10K. Filter the curve to `amount_usd >=
10_000` to compare like for like.

Two differences worth knowing about when your numbers don't match the site's:

- **Liquidity is not deterministic.** Two Fynd instances on the same block can
  route differently if they are indexing different protocol sets or TVL
  thresholds. Compare `coverage()["protocols_routed"]` first.
- **The site's headline figures are computed in its frontend** — extra cost vs
  spot, liquidity bias, market condition, the bookmap's bucketing. This SDK
  gives you the measurements those are derived from, not the derivations
  themselves.

## Not implemented

- **Durable storage.** History lives in the process that collected it.
- **The site's derived indicators.** See above.
- **Full indexed-universe coverage.** `coverage()` reports what your quotes
  routed through. The "indexed liquidity universe" count on the site comes from
  Fynd's startup logs; no Fynd endpoint exposes it.
