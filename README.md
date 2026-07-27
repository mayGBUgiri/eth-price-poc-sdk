# eth-price-poc-sdk — retired

This package is retired. It receives no further updates.

It is superseded by
**[propeller-heads/price-of-ethereum](https://github.com/propeller-heads/price-of-ethereum)**,
which measures the same data — the block-level on-chain price and depth behind
[marketprice.xyz](https://marketprice.xyz/) — from a Fynd you run yourself.

## Why this one is retired

This SDK was meant to be how anyone reproduces what marketprice.xyz shows. But every
read in `0.2.0` goes through marketprice.xyz itself: the `/api/*` endpoints, with a
static `data.json` fallback served from the same origin. Verifying the site against
data served by the site is not verification, and it left the SDK useless whenever the
deployment was down.

`price-of-ethereum` closes that loop. It quotes a local Fynd directly, so the numbers
you get are ones your own machine measured.

## The numbers do not change

`price-of-ethereum` is pinned to this collector bit-for-bit by a golden parity test
(`tests/test_golden_parity.py`), against a fixture generated from this repository.
Same measurement method, same results — migrating does not move your data.

It also ships what this one never had: on-disk JSONL/parquet history that survives a
restart, a live dashboard, a frozen HTML report, a `poe` CLI, and a notebook
walkthrough.

## Install the successor

Neither package is on PyPI, so this is a one-line change from the install command the
old README documented:

```bash
pip install "price-of-ethereum @ git+https://github.com/propeller-heads/price-of-ethereum.git"
```

## Migration

| `eth_price_poc` (retired) | `price_of_ethereum` |
|---|---|
| `client()` / `EthPricePoCClient` | `FyndClient` + `collect_snapshot` — quotes your local Fynd instead of the hosted API |
| `c.latest()` | `poe snapshot` — collect one block now |
| `c.history(limit=N)` | `poe collect` to record, then `load_jsonl` / `load_parquet` to read back |
| `c.history_as_dataframe(limit=N)` | `load_parquet` — returns a DataFrame directly, no optional extra |
| `c.status()` / `c.coverage()` | reported by the collector against your own Fynd |
| `c.detail(block, side, target)` | per-rung route legs, recorded per quote |
| `c.export(block, side, target)` | executable calldata, fee breakdown and Tenderly URL, recorded per quote |
| `c.curve_for_block(...)` | the per-block dense curve is in the recorded history |
| `c.tokens()` | `resolve_tokens` |

Two behavioural differences worth knowing before you port:

- **History is yours, not ours.** The hosted API served a rolling retained window that
  existed whether or not you were running. `price-of-ethereum` records to disk from the
  moment you start collecting; there is no backfill of what you did not measure.
- **You need a Fynd.** Reads here needed no key. Running your own collector does: get
  one from the Fynd portal bot on Telegram, [t.me/FyndPortalBot](https://t.me/FyndPortalBot).

## If you are pinned to this package

`v0.1.0` and `v0.2.0` remain installable from their git tags, and this repository stays
public and read-only, so existing pins keep resolving:

```bash
pip install "eth-price-poc-sdk @ git+https://github.com/propeller-heads/eth-price-poc-sdk.git@v0.2.0"
```

Nothing about `0.2.0`'s behaviour changed in `0.3.0` — it adds this notice and a
`DeprecationWarning` on import, and nothing else.

Note that anything reading through this package still depends on marketprice.xyz being
up, which is the limitation that retired it.
