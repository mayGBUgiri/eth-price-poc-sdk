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
print(c.tokens())         # token_in / token_out (address, symbol, decimals)

# Per-rung route + executable quote for one depth cell
d = c.detail(snap["block"], "buy", 1.0)   # None if not stored for that cell
if d:
    print([leg["protocol"] for leg in d["route_legs"]])

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
- Pull the **per-rung route** with `detail()` (which pools and protocols
  the best route at a given block, side, and impact target actually used,
  per leg).
- Pull the **executable quote** with `export()` (the raw Fynd response,
  the transaction calldata, the fee breakdown, and a Tenderly URL).
- Backtest a fill strategy: "if I'd traded $X at this block, what would
  it have cost vs the next 10 blocks?"
- Cross-reference against your own dataset (CEX prints, on-chain
  events, etc.).
- Pull the dense `curve` array (~200 measured points per side on the hosted deployment; latest block via `latest()`, recent historical blocks via `curve_for_block()` / `/api/curve`)
  and build a custom depth chart.

## API surface

| Method | Endpoint | Notes |
|---|---|---|
| `client.latest()` | `GET /api/latest` | Single most recent block, rich per-rung levels + dense curve |
| `client.history(limit=N)` | `GET /api/history?limit=N` | Rolling window of slim blocks; server caps N at 2,000 |
| `client.status()` | `GET /api/status` | Live/static, blocks_behind, fynd health |
| `client.coverage()` | `GET /api/coverage` | Indexed protocols, components |
| `client.detail(block, side, target_impact_pct)` | `GET /api/detail` | Per-rung route legs, execution tooltip, Tenderly URL; `None` if not stored |
| `client.export(block, side, target_impact_pct)` | `GET /api/export` | Raw quote, calldata, fee breakdown; `None` if not stored |
| `client.curve_for_block(block_index, side)` | `GET /api/curve` | Dense curve for one historical block |
| `client.tokens()` | client-side | `token_in` / `token_out` metadata for the pair |
| `client.history_as_dataframe(limit=N)` | derived | Convenience pandas wrapper |

If the live API is unreachable, `client()` transparently falls back
to the static `data.json` and `coverage_static.json` snapshots served
from the same origin (`latest()`, `history()`, and `coverage()` only),
frozen at the last refresh.

## Schema reference

The hosted API keeps the bulk endpoints slim and serves per-rung detail on
demand. `history` and `latest` therefore have **different shapes**.

### `history()` — slim blocks

The response wraps the window plus pair-level metadata:

```
pair:          "ETH/USDC"
impact_levels: [float]      # the targets present under each block's `levels`
total_blocks:  int
updated_at:    str
blocks: [ {
  block:       int          # Ethereum block number
  time:        str          # ISO-8601 UTC, when this snapshot was collected
  spot_price:  float        # marginal-trade price (~$1K probe)
  robust_mid:  float        # median shallow two-sided mid from sweep quotes
  duration_ms: int          # how long this snapshot took to assemble
  block_hash:  str
  levels: {                 # per-target depth, slimmed
    "1.0": { "buy":  { amount_usd, price, bound },
             "sell": { ... } }, ...
  }
  curve: { "buy": [ { amount_usd, price } ], "sell": [ ... ] }   # downsampled
}, ... ]
```

`curve` here is downsampled (`?curve_n=N`, 12–200); `?full=1` returns the
dense per-block curve.

### `latest()` — one rich block

The most recent block carries the full per-rung level fields and the dense
(~200 pt/side) curve:

```
block, time, spot_price, robust_mid, duration_ms, block_hash, ts_ms
median_depth, gas_price_wei, completeness, collector_version, quote_source
impact_levels: [float]
levels: {
  "1.0": {
    "buy":  { amount_usd, price, actual_impact_pct, target_impact_pct,
              target_reached, bound, amount_in, amount_out,
              amount_out_net_gas, gas_estimate, price_impact_bps,
              gas_cost_eth, gas_cost_token_out, direction, quote_source }
    "sell": { ... }
  }, ...
}
curve: {
  "buy":  [ { amount_usd, price, impact_pct, amount_in, amount_out,
              amount_out_net_gas, gas_estimate, route }, ... ]
  "sell": [ ... ]
  samples_per_side, search_min_usd, search_max_usd
}
```

Capped values are marked explicitly: `bound:"max"` when the search ceiling
can't reach the target impact, `bound:"min"` when even the smallest probed
size already exceeds it.

### `detail(block, side, target_impact_pct)` — per-rung route

```
block, side, target_impact_pct, block_hash, block_ts_ms
route_legs: [ { leg_index, protocol, component_id, split,
                token_in:  { address, symbol, decimals },
                token_out: { address, symbol, decimals },
                amount_in_atomic, amount_out_atomic, gas_estimate_units,
                etherscan_url } ]
legs_from_target: float | null   # nearest stored target, if not exact
tooltip: { actual_impact_pct, effective_price, mid, price_impact_bps,
           bound, target_reached, gas_cost_eth, ... }
tenderly: { url, status }
raw_response_available: bool
```

### `export(block, side, target_impact_pct)` — executable quote

```
block, side, target_impact_pct, order_id, solve_time_ms
response:      { ... }        # raw Fynd order quote
executable:    { to, calldata, value }
fee_breakdown: { router_fee, client_fee, max_slippage, min_amount_received }
tenderly:      { url, status }
raw_from_target: float | null
```

### Generator-only fields

`token_in` / `token_out`, `route_meta` (1% probe route), and
`route_meta_by_level` (pre-aggregated routes per key target) are produced by
the local generator (`collect_snapshot`, see below) but are **not** served by
`marketprice.xyz`. Against the hosted API the same information is available a
different way: `client.tokens()` for token metadata, and `client.detail()` for
per-rung routes (call it per depth cell; `route_meta` is `detail(block, "buy",
1.0)`). `derived_from` (`anchored_bisection` for headline targets,
`nearest_real_quote` for sweep-derived rungs) appears in the generator output;
the hosted `detail()` exposes the equivalent via `legs_from_target`.

## Pair support

The hosted deployment serves **ETH/USDC only** today. The generator and client
can target another pair when used with a compatible Fynd/API deployment; that
does not imply that pair is available from `marketprice.xyz`.
