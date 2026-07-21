"""Regression test for level-key canonicalization. core.py keys the levels
dict via str(float(level)), so whole-number impact levels passed as ints
(1, 5, …) land under the same "1.0"-style keys the anchor targets, route
aggregation, and the hosted dataset use. This test passes int levels on
purpose: it fails if that canonicalization regresses to str(level), which
would key int 1 as "1" and miss the "1.0" anchor lookup.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import eth_price_poc.generate.core as core
from eth_price_poc.generate.config import NullSink, PairConfig

from test_mixed_blocks import _sweep_entry


class IntLevelKeysTest(unittest.TestCase):
    def test_int_levels_reach_anchors_and_route_meta(self) -> None:
        # Int whole-number levels (1, 5, …) are the case the canonicalization
        # exists for: str(1) is "1" but str(float(1)) is "1.0".
        cfg = PairConfig(
            impact_levels=[0.5, 1, 5, 10, 25, 50],
            sweep_samples_per_side=3,
            max_workers=2,
            tenderly_from_address="0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        )
        self.assertTrue(any(isinstance(lvl, int) for lvl in cfg.impact_levels))

        state = NullSink()
        sweep = [_sweep_entry(cfg, 100, "buy", i) for i in range(3)]
        sweep_by_side = {
            "buy": sweep,
            "sell": [_sweep_entry(cfg, 100, "sell", i) for i in range(3)],
        }

        def fake_anchor(cfg_arg, side, target_pct, sweep_arg, spot, state_arg,
                        max_iters=5, tolerance=0.02):
            if target_pct != 1.0:
                return None
            raw = dict(sweep_arg[0]["_raw"])
            raw["order_id"] = f"100-{side}-anchor"
            return {"q": raw, "price": 2_020.0, "impact": 1.0, "usd": 1_000.0}

        originals = {
            "get_block_number": core.get_block_number,
            "fynd_spot": core.fynd_spot,
            "sweep_side": core.sweep_side,
            "anchor_target_from_sweep": core.anchor_target_from_sweep,
            "compute_robust_mid": core.compute_robust_mid,
        }
        core.get_block_number = lambda rpc_url: 99
        core.fynd_spot = lambda cfg_arg, state_arg: 2_000.0
        core.sweep_side = (
            lambda cfg_arg, side, spot, state_arg, num_samples: sweep_by_side[side]
        )
        core.anchor_target_from_sweep = fake_anchor
        core.compute_robust_mid = (
            lambda cfg_arg, spot, max_depth_usd, state_arg: (2_000.0, 1_000.0)
        )
        try:
            snap, _payload = core.collect_snapshot(cfg, state)
        finally:
            for name, original in originals.items():
                setattr(core, name, original)

        self.assertIsNotNone(snap)
        levels = snap["levels"]
        # Every level lands under its canonical float-string key; the int 1
        # becomes "1.0" (not "1"), which is the whole point of the fix.
        self.assertEqual(set(levels), {str(float(lvl)) for lvl in cfg.impact_levels})
        self.assertIn("1.0", levels)
        self.assertNotIn("1", levels)
        # The 1% anchor lands on the int-configured level only because it was
        # canonicalized to "1.0"; a str(level) regression keys it "1" and misses.
        one_pct = levels["1.0"]["buy"]
        self.assertEqual(one_pct["derived_from"], "anchored_bisection")
        # The headline route_meta is built from the 1% buy record.
        self.assertTrue(snap["route_meta"]["protocols"])
        self.assertIn("1.0", snap["route_meta_by_level"])


if __name__ == "__main__":
    unittest.main()
