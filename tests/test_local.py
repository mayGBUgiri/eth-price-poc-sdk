"""LocalFeed serves reads from blocks it collected itself. These tests drive it
with fake Fynd quotes so the whole path — collect, window, detail, export,
status — is exercised without a running Fynd.
"""
from __future__ import annotations

import contextlib
import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import eth_price_poc.generate.core as core
import eth_price_poc.local as local_mod
from eth_price_poc.generate.config import PairConfig
from eth_price_poc.local import EthPricePoCDataUnavailable, LocalFeed

from test_mixed_blocks import RecordingState, _raw_quote, _sweep_entry


CFG = PairConfig(
    impact_levels=[1.0, 5.0],
    sweep_samples_per_side=3,
    max_workers=2,
    tenderly_from_address="0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
)


@contextlib.contextmanager
def patched_core(blocks: list[int], *, anchored: float | None = 1.0,
                 spot: float | None = 2_000.0, sides: tuple[str, ...] = ("buy", "sell")):
    """Patch core so each collect_snapshot() call consumes the next block
    number from `blocks`. `anchored` is the one target that gets an anchored
    bisection quote (and therefore a stored raw response). `sides` restricts
    which directions produce quotes at all, so a side can be left unmeasured."""
    remaining = list(blocks)

    def fake_sweep(cfg_arg, side, spot_arg, state_arg, num_samples):
        if side not in sides:
            return []
        block = remaining[0]
        return [_sweep_entry(cfg_arg, block, side, i) for i in range(2)]

    def fake_anchor(cfg_arg, side, target_pct, sweep, spot_arg, state_arg,
                    max_iters=5, tolerance=0.02):
        if anchored is None or target_pct != anchored or side not in sides:
            return None
        return {"q": _raw_quote(cfg_arg, remaining[0], side, "anchor"),
                "price": 2_020.0 if side == "buy" else 1_980.0,
                "impact": target_pct, "usd": 1_000.0}

    def fake_spot(cfg_arg, state_arg):
        return spot

    originals = {name: getattr(core, name) for name in (
        "fynd_spot", "sweep_side", "anchor_target_from_sweep", "compute_robust_mid")}
    core.fynd_spot = fake_spot
    core.sweep_side = fake_sweep
    core.anchor_target_from_sweep = fake_anchor
    core.compute_robust_mid = lambda cfg_arg, spot_arg, max_depth_usd, state_arg: (2_000.0, 5_000.0)
    try:
        yield lambda: remaining.pop(0)
    finally:
        for name, original in originals.items():
            setattr(core, name, original)


def collect_blocks(feed: LocalFeed, blocks: list[int]) -> None:
    with patched_core(blocks) as advance:
        for _ in blocks:
            feed.collect()
            advance()


class CollectionTest(unittest.TestCase):
    def test_collect_appends_to_window_and_latest_returns_it(self) -> None:
        feed = LocalFeed(CFG, sink=RecordingState())
        collect_blocks(feed, [100, 101])

        self.assertEqual(feed.latest()["block"], 101)
        self.assertEqual([b["block"] for b in feed.history()["blocks"]], [100, 101])
        self.assertEqual(feed.history()["blocks_retained"], 2)

    def test_history_is_empty_before_any_collection(self) -> None:
        feed = LocalFeed(CFG, sink=RecordingState())
        hist = feed.history()

        self.assertEqual(hist["blocks"], [])
        self.assertEqual(hist["blocks_retained"], 0)
        self.assertEqual(hist["pair"], CFG.pair_label)

    def test_window_evicts_oldest_beyond_history_size(self) -> None:
        feed = LocalFeed(CFG, history_size=2, sink=RecordingState())
        collect_blocks(feed, [100, 101, 102])

        self.assertEqual([b["block"] for b in feed.history()["blocks"]], [101, 102])

    def test_history_limit_truncates_from_most_recent_end(self) -> None:
        feed = LocalFeed(CFG, sink=RecordingState())
        collect_blocks(feed, [100, 101, 102])

        self.assertEqual([b["block"] for b in feed.history(limit=2)["blocks"]], [101, 102])

    def test_history_limit_zero_returns_no_blocks(self) -> None:
        # 0 is a real limit, not "unlimited": it must not fall through to the
        # whole window the way a falsy check would.
        feed = LocalFeed(CFG, sink=RecordingState())
        collect_blocks(feed, [100, 101, 102])

        self.assertEqual(feed.history(limit=0)["blocks"], [])
        self.assertEqual(feed.history(limit=0)["blocks_retained"], 3)

    def test_history_limit_beyond_window_returns_everything(self) -> None:
        feed = LocalFeed(CFG, sink=RecordingState())
        collect_blocks(feed, [100, 101])

        self.assertEqual([b["block"] for b in feed.history(limit=99)["blocks"]], [100, 101])

    def test_history_rejects_a_negative_limit(self) -> None:
        feed = LocalFeed(CFG, sink=RecordingState())
        collect_blocks(feed, [100])

        with self.assertRaises(ValueError):
            feed.history(limit=-1)

    def test_window_survives_successive_evictions(self) -> None:
        feed = LocalFeed(CFG, history_size=2, sink=RecordingState())
        collect_blocks(feed, [100, 101, 102, 103])

        self.assertEqual([b["block"] for b in feed.history()["blocks"]], [102, 103])
        self.assertEqual(feed.history()["blocks_retained"], 2)

    def test_latest_collects_when_window_is_empty(self) -> None:
        feed = LocalFeed(CFG, sink=RecordingState())
        with patched_core([100]):
            self.assertEqual(feed.latest()["block"], 100)
        self.assertEqual(feed.history()["blocks_retained"], 1)

    def test_collect_raises_when_fynd_yields_no_usable_quote(self) -> None:
        feed = LocalFeed(CFG, sink=RecordingState())
        with patched_core([100], spot=None):
            with self.assertRaises(EthPricePoCDataUnavailable):
                feed.collect()
        self.assertEqual(feed.history()["blocks_retained"], 0)


class IsolationTest(unittest.TestCase):
    """Reads hand back data the caller owns. The window keeps serving the
    measurements it collected however the caller treats what it received."""

    def setUp(self) -> None:
        self.feed = LocalFeed(CFG, sink=RecordingState())
        collect_blocks(self.feed, [100, 101])

    def test_mutating_a_latest_snapshot_leaves_the_window_intact(self) -> None:
        snap = self.feed.latest()
        snap["block"] = 999_999
        snap["levels"].clear()

        self.assertEqual(self.feed.latest()["block"], 101)
        self.assertTrue(self.feed.latest()["levels"])

    def test_mutating_a_history_block_leaves_the_window_intact(self) -> None:
        blocks = self.feed.history()["blocks"]
        blocks[0]["spot_price"] = -1.0

        self.assertNotEqual(self.feed.history()["blocks"][0]["spot_price"], -1.0)

    def test_filtering_a_curve_in_place_leaves_the_window_intact(self) -> None:
        # The README tells readers to filter the curve to amount_usd >= 10_000
        # to match what the site charts; doing it in place must not shrink the
        # stored sweep.
        curve = self.feed.curve_for_block(-1, "buy")
        measured = len(curve)
        del curve[:]

        self.assertEqual(len(self.feed.curve_for_block(-1, "buy")), measured)

    def test_mutating_a_collect_result_leaves_the_window_intact(self) -> None:
        with patched_core([102]):
            snap = self.feed.collect()
        snap["robust_mid"] = -1.0

        self.assertNotEqual(self.feed.latest()["robust_mid"], -1.0)

    def test_mutating_tokens_leaves_the_configured_pair_intact(self) -> None:
        tokens = self.feed.tokens()
        tokens["token_in"]["symbol"] = "WRONG"

        self.assertEqual(self.feed.tokens()["token_in"]["symbol"], "USDC")


class BlockIdentityTest(unittest.TestCase):
    def test_snapshot_is_rejected_when_no_quote_reports_a_block(self) -> None:
        # Block identity comes only from the quotes, so a sweep whose quotes
        # carry no block number cannot be labelled at all.
        state = RecordingState()

        def blockless_sweep(cfg_arg, side, spot_arg, state_arg, num_samples):
            entries = [_sweep_entry(cfg_arg, 100, side, i) for i in range(2)]
            for entry in entries:
                entry["_raw"] = {k: v for k, v in entry["_raw"].items() if k != "block"}
            return entries

        with patched_core([100], anchored=None):
            core.sweep_side = blockless_sweep
            snap, payload = core.collect_snapshot(CFG, state)

        self.assertIsNone(snap)
        self.assertIsNone(payload)
        self.assertTrue(any(phase == "block_identity" for phase, _msg in state.errors))


class DetailTest(unittest.TestCase):
    def setUp(self) -> None:
        self.feed = LocalFeed(CFG, sink=RecordingState())
        collect_blocks(self.feed, [100])

    def test_detail_exposes_route_legs_for_a_measured_target(self) -> None:
        detail = self.feed.detail(100, "buy", 1.0)

        self.assertIsNotNone(detail)
        assert detail is not None
        self.assertIsNone(detail["legs_from_target"])
        self.assertEqual(detail["block"], 100)
        self.assertEqual([leg["leg_index"] for leg in detail["route_legs"]], [0])
        leg = detail["route_legs"][0]
        self.assertEqual(leg["protocol"], "unit-test")
        self.assertEqual(leg["token_in"]["symbol"], "USDC")
        self.assertEqual(leg["token_out"]["symbol"], "WETH")
        # The tooltip quotes the block's own robust mid, not a recomputed one.
        self.assertEqual(detail["tooltip"]["mid"], self.feed.latest()["robust_mid"])
        self.assertTrue(detail["raw_response_available"])
        self.assertEqual(detail["tenderly"]["status"], "ready")

    def test_detail_snaps_to_nearest_stored_target_and_reports_it(self) -> None:
        detail = self.feed.detail(100, "buy", 4.0)

        self.assertIsNotNone(detail)
        assert detail is not None
        self.assertEqual(detail["legs_from_target"], 5.0)
        self.assertEqual(detail["target_impact_pct"], 4.0)

    def test_detail_returns_none_for_a_block_outside_the_window(self) -> None:
        self.assertIsNone(self.feed.detail(999, "buy", 1.0))

    def test_detail_rejects_an_invalid_side_before_lookup(self) -> None:
        with self.assertRaises(ValueError):
            self.feed.detail(100, "long", 1.0)

    def test_component_id_that_is_not_an_address_gets_no_etherscan_url(self) -> None:
        # The fake quotes use "pool-100-buy-…" component ids, which are not
        # addresses; only real addresses should produce an Etherscan link.
        detail = self.feed.detail(100, "buy", 1.0)
        assert detail is not None
        self.assertIsNone(detail["route_legs"][0]["etherscan_url"])


class ExportTest(unittest.TestCase):
    def setUp(self) -> None:
        self.feed = LocalFeed(CFG, sink=RecordingState())
        collect_blocks(self.feed, [100])

    def test_export_returns_executable_quote_for_an_anchored_target(self) -> None:
        exported = self.feed.export(100, "buy", 1.0)

        self.assertIsNotNone(exported)
        assert exported is not None
        self.assertIsNone(exported["raw_from_target"])
        self.assertEqual(exported["executable"]["to"],
                         "0x1111111111111111111111111111111111111111")
        self.assertEqual(exported["executable"]["calldata"], "0xabcdef")
        self.assertEqual(exported["response"]["block"]["number"], 100)
        self.assertEqual(exported["fee_breakdown"]["router_fee"], "0")

    def test_export_falls_back_to_the_nearest_anchored_target(self) -> None:
        # Raw responses persist for anchored targets only, so a request for the
        # un-anchored 5% rung resolves to the 1% anchor and says so.
        exported = self.feed.export(100, "buy", 5.0)

        self.assertIsNotNone(exported)
        assert exported is not None
        self.assertEqual(exported["raw_from_target"], 1.0)

    def test_export_returns_none_for_a_block_outside_the_window(self) -> None:
        self.assertIsNone(self.feed.export(999, "buy", 1.0))

    def test_export_rejects_an_invalid_side_before_lookup(self) -> None:
        with self.assertRaises(ValueError):
            self.feed.export(100, "long", 1.0)


class UnmeasuredSideTest(unittest.TestCase):
    """A side Fynd could not quote leaves no legs, no raw response and no
    curve, and each read says so rather than inventing an answer."""

    def setUp(self) -> None:
        self.feed = LocalFeed(CFG, sink=RecordingState())
        with patched_core([100], sides=("buy",)):
            self.feed.collect()

    def test_detail_returns_none_for_the_unmeasured_side(self) -> None:
        self.assertIsNone(self.feed.detail(100, "sell", 1.0))
        self.assertIsNotNone(self.feed.detail(100, "buy", 1.0))

    def test_export_returns_none_for_the_unmeasured_side(self) -> None:
        self.assertIsNone(self.feed.export(100, "sell", 1.0))

    def test_curve_raises_for_the_unmeasured_side(self) -> None:
        with self.assertRaises(EthPricePoCDataUnavailable) as ctx:
            self.feed.curve_for_block(-1, "sell")

        self.assertIn("sell", str(ctx.exception))


class StatusTest(unittest.TestCase):
    @contextlib.contextmanager
    def _fynd(self, health: dict | None, head: int | None):
        originals = (local_mod.fynd_health, local_mod.fynd_quote)
        local_mod.fynd_health = lambda cfg_arg: health
        local_mod.fynd_quote = (
            lambda *a, **k: {"block": {"number": head}} if head is not None else None
        )
        try:
            yield
        finally:
            local_mod.fynd_health, local_mod.fynd_quote = originals

    def test_degraded_when_fynd_is_unreachable(self) -> None:
        feed = LocalFeed(CFG, sink=RecordingState())
        with self._fynd(None, None):
            status = feed.status()

        self.assertEqual(status["mode"], "degraded")
        self.assertIsNone(status["fynd"])
        self.assertIsNone(status["blocks_behind"])

    def test_starting_when_healthy_but_nothing_collected(self) -> None:
        feed = LocalFeed(CFG, sink=RecordingState())
        with self._fynd({"healthy": True}, 100):
            self.assertEqual(feed.status()["mode"], "starting")

    def test_live_when_window_is_level_with_fynd(self) -> None:
        feed = LocalFeed(CFG, sink=RecordingState())
        collect_blocks(feed, [100])
        with self._fynd({"healthy": True}, 100):
            status = feed.status()

        self.assertEqual(status["mode"], "live")
        self.assertEqual(status["blocks_behind"], 0)
        self.assertEqual(status["fynd"]["base_url"], CFG.fynd_base_url)

    def test_stale_when_window_trails_fynd(self) -> None:
        feed = LocalFeed(CFG, sink=RecordingState())
        collect_blocks(feed, [100])
        with self._fynd({"healthy": True}, 140):
            status = feed.status()

        self.assertEqual(status["mode"], "stale")
        self.assertEqual(status["blocks_behind"], 40)

    def test_one_block_behind_still_counts_as_live(self) -> None:
        # A collection cycle spans roughly a block, so trailing by one is the
        # steady state, not a fault. Two is the first genuinely stale value.
        feed = LocalFeed(CFG, sink=RecordingState())
        collect_blocks(feed, [100])
        with self._fynd({"healthy": True}, 101):
            self.assertEqual(feed.status()["mode"], "live")

    def test_two_blocks_behind_is_the_first_stale_value(self) -> None:
        feed = LocalFeed(CFG, sink=RecordingState())
        collect_blocks(feed, [100])
        with self._fynd({"healthy": True}, 102):
            self.assertEqual(feed.status()["mode"], "stale")

    def test_degraded_when_fynd_is_healthy_but_reports_no_usable_head(self) -> None:
        # Health says ready while the probe quote comes back without a block:
        # freshness is unknown, so the window must not be claimed as live.
        feed = LocalFeed(CFG, sink=RecordingState())
        collect_blocks(feed, [100])
        with self._fynd({"healthy": True}, None):
            status = feed.status()

        self.assertEqual(status["mode"], "degraded")
        self.assertIsNone(status["head_block"])
        self.assertIsNone(status["blocks_behind"])

    def test_a_zero_block_number_is_not_a_usable_head(self) -> None:
        feed = LocalFeed(CFG, sink=RecordingState())
        collect_blocks(feed, [100])
        originals = local_mod.fynd_quote
        local_mod.fynd_quote = lambda *a, **k: {"block": {"number": 0}}
        local_health = local_mod.fynd_health
        local_mod.fynd_health = lambda cfg_arg: {"healthy": True}
        try:
            status = feed.status()
        finally:
            local_mod.fynd_quote = originals
            local_mod.fynd_health = local_health

        self.assertIsNone(status["head_block"])
        self.assertEqual(status["mode"], "degraded")


class CoverageAndTokensTest(unittest.TestCase):
    def test_coverage_reports_only_what_was_actually_routed(self) -> None:
        feed = LocalFeed(CFG, sink=RecordingState())
        collect_blocks(feed, [100])
        originals = local_mod.fynd_health
        local_mod.fynd_health = lambda cfg_arg: {"healthy": True}
        try:
            cov = feed.coverage()
        finally:
            local_mod.fynd_health = originals

        # The pools named here are exactly those the level records' quotes went
        # through: the 1% anchor on each side, plus the sweep entry each
        # un-anchored rung fell back to. Pools quoted during the sweep but not
        # carried by any level record are not "routed" for coverage purposes.
        self.assertEqual(cov["protocols_routed"], ["unit-test"])
        self.assertEqual(sorted(cov["components_routed"]), [
            "pool-100-buy-anchor",
            "pool-100-buy-sweep-1",
            "pool-100-sell-anchor",
            "pool-100-sell-sweep-1",
        ])
        self.assertEqual(cov["component_count"], 4)
        self.assertEqual(cov["blocks_observed"], 1)

    def test_tokens_follow_the_configured_pair(self) -> None:
        feed = LocalFeed(CFG, sink=RecordingState())
        tokens = feed.tokens()

        self.assertEqual(tokens["token_in"]["symbol"], "USDC")
        self.assertEqual(tokens["token_out"]["decimals"], 18)

    def test_curve_for_block_returns_the_measured_sweep(self) -> None:
        feed = LocalFeed(CFG, sink=RecordingState())
        collect_blocks(feed, [100])
        curve = feed.curve_for_block(-1, "buy")

        self.assertEqual(len(curve), 2)
        self.assertNotIn("_raw", curve[0])
        self.assertIn("amount_usd", curve[0])

    def test_curve_rejects_an_invalid_side(self) -> None:
        with self.assertRaises(ValueError):
            LocalFeed(CFG, sink=RecordingState()).curve_for_block(-1, "long")

    def test_curve_raises_for_an_index_outside_the_window(self) -> None:
        feed = LocalFeed(CFG, sink=RecordingState())
        collect_blocks(feed, [100])

        with self.assertRaises(EthPricePoCDataUnavailable) as ctx:
            feed.curve_for_block(-5, "buy")

        self.assertIn("1 blocks retained", str(ctx.exception))

    def test_curve_collects_when_the_window_is_empty(self) -> None:
        feed = LocalFeed(CFG, sink=RecordingState())
        with patched_core([100]):
            curve = feed.curve_for_block(-1, "buy")

        self.assertEqual(len(curve), 2)
        self.assertEqual(feed.history()["blocks_retained"], 1)

    def test_coverage_collects_when_the_window_is_empty(self) -> None:
        feed = LocalFeed(CFG, sink=RecordingState())
        health = local_mod.fynd_health
        local_mod.fynd_health = lambda cfg_arg: None
        try:
            with patched_core([100]):
                cov = feed.coverage()
        finally:
            local_mod.fynd_health = health

        self.assertEqual(cov["blocks_observed"], 1)
        self.assertIsNone(cov["fynd"])


class ConstructionTest(unittest.TestCase):
    def test_passing_a_base_url_fails_with_a_pointed_message(self) -> None:
        with self.assertRaises(TypeError) as ctx:
            LocalFeed("https://marketprice.xyz")

        self.assertIn("PairConfig", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
