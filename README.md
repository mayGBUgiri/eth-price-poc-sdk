# eth-price-poc-sdk

Tiny Python client for the Price-of-Ethereum PoC dataset.

Pulls live or static depth/route snapshots from the public deployment
(or any compatible API base) so you can analyse the data locally:
plot it, write it to a notebook, run your own metrics on top.

The landing page shows the headline charts; everything else
(per-block route metadata, the dense per-block curve, per-target
capping flags, etc.) is intended to be explored through this SDK
against the same API.

## Install

```bash
pip install "eth-price-poc-sdk @ git+https://github.com/propeller-heads/eth-price-poc-sdk.git"
# or, from a clone of this repo:  pip install -e .
```

## Quickstart

```python
from eth_price_poc import client

# Default base is the live deployment (https://marketprice.xyz), which serves
# the API and the site from one origin. Pass base=... to point at your own.
c = client()                       # hosted deployment currently serves ETH/USDC

snap   = c.latest()       # most recent block's full snapshot
status = c.status()       # mode (live/static), blocks_behind, fynd health
cov    = c.coverage()     # indexed protocols, components, last update
hist   = c.history(limit=720)  # rolling window

print(snap["block"], snap["spot_price"])

# Optional pandas integration (install with: pip install "eth-price-poc-sdk[pandas]")
df = c.history_as_dataframe(limit=720)
print(df.head())
```

## Generate your own data

The hosted dataset at [marketprice.xyz](https://marketprice.xyz) serves the
latest collected ETH/USDC depth plus its rolling retained history. Check
`client().status()` for freshness before treating a snapshot as live. No key is
needed for reads.

Want your own independent feed (other token pairs, lower latency, or no
dependency on our uptime)? Run the generator against your own Fynd instance.
The hosted API gives you retained history; your machine produces an independent
live feed.

```bash
pip install "eth-price-poc-sdk[generate]"
```

**1. Get a Fynd (Tycho) API key.** Open the Fynd portal bot on Telegram,
[t.me/FyndPortalBot](https://t.me/FyndPortalBot), and follow the prompts.

**2. Run Fynd locally** with the key (see Fynd's own docs for the binary):

```bash
export TYCHO_API_KEY=<your key>
```

**3. Generate snapshots** from your local Fynd:

```bash
python -m eth_price_poc.generate.run_local --fynd-base http://127.0.0.1:3000
```

Or from Python, for any pair Tycho indexes:

```python
from eth_price_poc.generate import PairConfig, TokenSpec, collect_snapshot, NullSink

cfg = PairConfig(fynd_base_url="http://127.0.0.1:3000")   # ETH/USDC by default
snap, _payload = collect_snapshot(cfg, NullSink())
print(snap["block"], snap["spot_price"], snap["robust_mid"])
```

`PairConfig` is the only thing that changes per token pair. Swap `token_in` and
`token_out` (`TokenSpec(address, symbol, decimals)`) and the same
depth/curve/route data falls out. The download client (`client()`) needs no key:
it only reads our server. The key is solely for running your own Fynd.

## What you can do with this that the website can't show you

- Plot the **full bookmap** at custom resolution (the site renders 176
  rows; the data lets you pick any number).
- Extract **the per-block route metadata** (which Uniswap V3 pool the
  best $5M route actually used at block N).
- Backtest a fill strategy: "if I'd traded $X at this block, what would
  it have cost vs the next 10 blocks?"
- Cross-reference against your own dataset (CEX prints, on-chain
  events, etc.).
- Pull the dense `curve` array (~200 measured points per side on the hosted deployment; latest block via `latest()`, recent historical blocks via `curve_for_block()` / `/api/curve`)
  and build a custom depth chart.
- Tap into `route_meta_by_level` to compare which protocols win at
  which trade sizes block by block.

## API surface

| Method | Endpoint | Notes |
|---|---|---|
| `client.latest()` | `GET /api/latest` | Single most recent block snapshot |
| `client.history(limit=N)` | `GET /api/history?limit=N` | Rolling window; server caps N at 2,000 |
| `client.status()` | `GET /api/status` | Live/static, blocks_behind, fynd health |
| `client.coverage()` | `GET /api/coverage` | Indexed protocols, components |
| `client.history_as_dataframe(limit=N)` | derived | Convenience pandas wrapper |

If the live API is unreachable, `client()` will transparently fall back
to the static `data.json` and `coverage_static.json` snapshots served
from the same origin. Same shape, frozen at the last refresh.

## Schema reference

Each block in `history` (and the same shape under `latest`) carries:

```
block:        int          # Ethereum block number
time:         str          # ISO-8601 UTC, when this snapshot was collected
spot_price:   float        # marginal-trade price (~$1K probe)
robust_mid:   float        # median shallow two-sided mid from existing sweep quotes
duration_ms:  int          # how long this snapshot took to assemble
pair:         "ETH/USDC"
token_in / token_out:      # { address, symbol, decimals }
impact_levels: [float]     # the per-target rows present under `levels`
levels: {                  # per-target depth (anchored or sweep-derived)
  "1.0": {
    "buy":  { amount_usd, price, actual_impact_pct, target_reached,
              bound, amount_in, amount_out, gas_estimate, route,
              derived_from }
    "sell": { ... }
  }, ...
}
curve: {                   # dense measured points (~200 per side hosted)
  "buy":  [ { amount_usd, price, impact_pct, amount_in, amount_out,
              gas_estimate, route }, ... ]
  "sell": [ ... ]
  samples_per_side, search_min_usd, search_max_usd
}
route_meta:           # 1% probe route
route_meta_by_level:  # routes per key impact target
```

Capped values are marked explicitly: `bound:"max"` when the search
ceiling can't reach the target impact. `derived_from` says which real
quote supplies the row (`anchored_bisection` for headline targets,
`nearest_real_quote` for sweep-derived targets). For nearest-quote rows,
`amount_usd`, `price`, and `actual_impact_pct` are copied from that measured
sweep entry; `target_impact_pct` remains the requested rung.

## Pair support

The hosted deployment serves **ETH/USDC only** today. The generator and client
can target another pair when used with a compatible Fynd/API deployment; that
does not imply that pair is available from `marketprice.xyz`.
