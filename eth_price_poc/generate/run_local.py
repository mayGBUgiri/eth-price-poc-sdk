"""Generate your own real-time depth data from a local Fynd instance.

    python -m eth_price_poc.generate.run_local            # ETH/USDC, prints each block
    python -m eth_price_poc.generate.run_local --once     # one snapshot then exit

Pair the hosted rolling history (via EthPricePoCClient) with your own live feed.
This command produces current snapshots from your own infrastructure and key
for any pair available to your Fynd instance.

Prereqs: a running Fynd (with your Tycho API key) reachable at --fynd-base.
See the README "Generate your own data" section.
"""
from __future__ import annotations

import argparse
import json
import time

from .config import NullSink, PairConfig
from .core import collect_snapshot


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Generate ETH/USDC-style depth snapshots from local Fynd.")
    ap.add_argument("--fynd-base", default="http://127.0.0.1:3000", help="Fynd base URL")
    ap.add_argument("--samples", type=int, default=100, help="sweep samples per side")
    ap.add_argument("--interval", type=float, default=12.0, help="seconds between blocks")
    ap.add_argument("--once", action="store_true", help="emit one snapshot then exit")
    args = ap.parse_args(argv)

    cfg = PairConfig(fynd_base_url=args.fynd_base, sweep_samples_per_side=args.samples)
    sink = NullSink()
    while True:
        snap, _payload = collect_snapshot(cfg, sink)
        if snap:
            print(json.dumps({
                "block": snap.get("block"),
                "time": snap.get("time"),
                "spot_price": snap.get("spot_price"),
                "robust_mid": snap.get("robust_mid"),
            }))
        else:
            print('{"error": "no snapshot, is Fynd running and warmed up?"}')
        if args.once:
            return 0 if snap else 1
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
