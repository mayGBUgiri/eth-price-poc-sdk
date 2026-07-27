"""Collect real-time depth data from your own Fynd instance.

    python -m eth_price_poc.generate.run_local            # ETH/USDC, prints each block
    python -m eth_price_poc.generate.run_local --once     # one snapshot then exit

Fynd is the only service contacted: block identity, prices and routes all come
from its quote responses. Snapshots accumulate in memory for the life of the
process and are printed one JSON line per block; redirect stdout to keep them.

Prereqs: a running Fynd (with your Tycho API key) reachable at --fynd-base.
See the README "Run it yourself" section.
"""
from __future__ import annotations

import argparse
import json
import sys
import time

from ..local import EthPricePoCDataUnavailable, LocalFeed
from .config import NullSink, PairConfig


class StderrSink(NullSink):
    """Report collection errors and per-quote failures on stderr so a degraded
    snapshot is diagnosable. Sweep quotes that fail are dropped by the sweep
    rather than raised, so without surfacing them a badly degraded curve looks
    like a normal snapshot.
    """

    def add_error(self, msg, phase="") -> None:
        print(f"error [{phase}]: {msg}", file=sys.stderr)

    def add_quote_failure(self, failure) -> None:
        info = failure or {}
        detail = info.get("msg") or info.get("raw") or ""
        print(f"quote failure [{info.get('reason', '?')}]: {detail}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Collect ETH/USDC-style depth snapshots from local Fynd.")
    ap.add_argument("--fynd-base", default=PairConfig.fynd_base_url, help="Fynd base URL")
    ap.add_argument("--samples", type=int, default=PairConfig.sweep_samples_per_side,
                    help="sweep samples per side")
    ap.add_argument("--interval", type=float, default=12.0, help="seconds between blocks")
    ap.add_argument("--once", action="store_true", help="emit one snapshot then exit")
    args = ap.parse_args(argv)

    cfg = PairConfig(fynd_base_url=args.fynd_base, sweep_samples_per_side=args.samples)
    feed = LocalFeed(cfg, sink=StderrSink())
    while True:
        try:
            snap = feed.collect()
        except EthPricePoCDataUnavailable as e:
            print(json.dumps({"error": str(e)}))
            if args.once:
                return 1
            time.sleep(args.interval)
            continue
        one_pct = (snap.get("levels") or {}).get("1.0", {})
        print(json.dumps({
            "block": snap.get("block"),
            "time": snap.get("time"),
            "spot_price": snap.get("spot_price"),
            "robust_mid": snap.get("robust_mid"),
            "depth_buy_1pct_usd": (one_pct.get("buy") or {}).get("amount_usd"),
            "depth_sell_1pct_usd": (one_pct.get("sell") or {}).get("amount_usd"),
            "duration_ms": snap.get("duration_ms"),
            "mixed_block": snap.get("mixed_block"),
        }))
        if args.once:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
