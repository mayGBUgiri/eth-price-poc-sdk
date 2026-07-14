from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import eth_price_poc.generate.core as core
from eth_price_poc.generate.config import PairConfig


class RecordingState:
    def __init__(self) -> None:
        self.errors: list[tuple[str, str]] = []
        self.quote_failures: list[dict] = []
        self.mid_degraded_count = 0

    def add_error(self, msg: str, phase: str) -> None:
        self.errors.append((phase, msg))

    def add_quote_failure(self, failure: dict) -> None:
        self.quote_failures.append(failure)


def _sweep_with_shallow_price(price: float) -> tuple[list[dict], list[dict]]:
    buy: list[dict] = []
    sell: list[dict] = []
    for depth in (50.0, 1_000.0, 2_500.0, 5_000.0, 10_000.0, 250_000.0, 5_000_000.0):
        mid = price if 2_500.0 <= depth <= 10_000.0 else 2_000.0
        buy.append({"amount_usd": depth, "price": mid + 1.0})
        sell.append({"amount_usd": depth, "price": mid - 1.0})
    return buy, sell


def _quote_for_price(cfg: PairConfig, token_in: str, amount: int, price: float) -> dict:
    if token_in.lower() == cfg.token_in.address.lower():
        amount_in_units = amount / 10 ** cfg.token_in.decimals
        amount_out_units = amount_in_units / price
        amount_out = int(amount_out_units * 10 ** cfg.token_out.decimals)
    else:
        amount_in_units = amount / 10 ** cfg.token_out.decimals
        amount_out_units = amount_in_units * price
        amount_out = int(amount_out_units * 10 ** cfg.token_in.decimals)
    return {"amount_out": str(amount_out)}


class RobustMidMovementTest(unittest.TestCase):
    def test_sweep_mid_tracks_moving_shallow_prices_not_flat_deep_quotes(self) -> None:
        mids: list[float] = []
        depths: list[float] = []

        for price in (2_000.0, 2_012.0, 1_994.0):
            buy, sell = _sweep_with_shallow_price(price)
            mid, depth = core.compute_robust_mid_from_sweeps(buy, sell)
            mids.append(mid)
            depths.append(depth)

        self.assertEqual(mids, [2_000.0, 2_012.0, 1_994.0])
        self.assertTrue(all(2_500.0 <= depth <= 10_000.0 for depth in depths))
        self.assertGreater(abs(mids[1] - mids[0]), 10.0)
        self.assertGreater(abs(mids[2] - mids[1]), 15.0)

    def test_probe_mid_uses_shallow_depths_and_moves_with_input_price(self) -> None:
        cfg = PairConfig(max_workers=4)
        state = RecordingState()
        requested_depths: list[float] = []
        mids: list[float] = []

        for price in (2_000.0, 2_012.0, 1_994.0):
            requested_depths.clear()

            def fake_quote(
                cfg_arg: PairConfig,
                token_in: str,
                token_out: str,
                amount: int,
                state_arg: RecordingState,
                ctx: dict | None = None,
                split: bool = False,
            ) -> dict:
                depth = float((ctx or {}).get("depth_usd", 0.0))
                requested_depths.append(depth)
                side_price = price + 1.0 if token_in == cfg_arg.token_in.address else price - 1.0
                return _quote_for_price(cfg_arg, token_in, amount, side_price)

            with mock.patch.object(core, "fynd_quote", side_effect=fake_quote):
                mid, depth = core.compute_robust_mid(cfg, price, 50_000_000.0, state)

            mids.append(mid)
            self.assertLessEqual(max(requested_depths), core.ROBUST_MID_MAX_DEPTH_USD + 1e-6)
            self.assertGreaterEqual(depth, core.ROBUST_MID_MIN_DEPTH_USD)
            self.assertLessEqual(depth, core.ROBUST_MID_MAX_DEPTH_USD)

        self.assertAlmostEqual(mids[1] - mids[0], 12.0, delta=0.01)
        self.assertAlmostEqual(mids[2] - mids[1], -18.0, delta=0.01)
        self.assertEqual(state.mid_degraded_count, 0)


if __name__ == "__main__":
    unittest.main()
