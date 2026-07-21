from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import eth_price_poc.generate.core as core
from eth_price_poc.generate.config import NullSink, PairConfig


class RecordingState:
    def __init__(self) -> None:
        self.errors: list[tuple[str, str]] = []
        self.quote_failures: list[dict] = []
        self.mid_degraded_count = 0
        self.mixed_blocks = 0

    def add_error(self, msg: str, phase: str) -> None:
        self.errors.append((phase, msg))

    def add_quote_failure(self, failure: dict) -> None:
        self.quote_failures.append(failure)


def _raw_quote(cfg: PairConfig, block: int, side: str, suffix: str) -> dict:
    if side == "buy":
        token_in = cfg.token_in.address
        token_out = cfg.token_out.address
        amount_in = "2000000000"
        amount_out = "1000000000000000000"
        amount_out_net_gas = "999000000000000000"
    else:
        token_in = cfg.token_out.address
        token_out = cfg.token_in.address
        amount_in = "1000000000000000000"
        amount_out = "2000000000"
        amount_out_net_gas = "1999000000"

    return {
        "order_id": f"{block}-{side}-{suffix}",
        "amount_in": amount_in,
        "amount_out": amount_out,
        "amount_out_net_gas": amount_out_net_gas,
        "gas_estimate": "21000",
        "gas_price": "1000000000",
        "block": {
            "number": block,
            "hash": f"0x{block:064x}",
            "timestamp": 1_700_000_000 + block,
        },
        "route": {
            "swaps": [{
                "protocol": "unit-test",
                "component_id": f"pool-{block}-{side}-{suffix}",
                "token_in": token_in,
                "token_out": token_out,
                "amount_in": amount_in,
                "amount_out": amount_out,
                "gas_estimate": "21000",
                "split": "1",
            }],
        },
        "transaction": {
            "to": "0x1111111111111111111111111111111111111111",
            "data": "0xabcdef",
            "value": "0",
        },
        "fee_breakdown": {
            "router_fee": "0",
            "client_fee": "0",
            "max_slippage": "0",
            "min_amount_received": amount_out_net_gas,
        },
        "_solve_time_ms": 12,
    }


def _sweep_entry(cfg: PairConfig, block: int, side: str, idx: int) -> dict:
    raw = _raw_quote(cfg, block, side, f"sweep-{idx}")
    return {
        "amount_usd": 1_000.0 + idx,
        "price": 2_000.0 + idx,
        "impact_pct": 0.5 + idx,
        "amount_in": raw["amount_in"],
        "amount_out": raw["amount_out"],
        "amount_out_net_gas": raw["amount_out_net_gas"],
        "gas_estimate": raw["gas_estimate"],
        "route": core._route_meta_of(raw),
        "_raw": raw,
        "_solve_time_ms": raw["_solve_time_ms"],
    }


class MixedBlockPersistenceTest(unittest.TestCase):
    def test_mixed_cycle_drops_minority_block_quote_rows(self) -> None:
        cfg = PairConfig(
            impact_levels=[1.0, 1.2],
            sweep_samples_per_side=3,
            max_workers=2,
            tenderly_from_address="0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        )
        state = RecordingState()
        sweep_by_side = {
            "buy": [
                _sweep_entry(cfg, 100, "buy", 0),
                _sweep_entry(cfg, 100, "buy", 1),
            ],
            "sell": [
                _sweep_entry(cfg, 101, "sell", 0),
            ],
        }

        # A target between measured impacts must carry one complete real quote,
        # never a blend of its adjacent entries. 1.2 is closer to the second
        # buy entry's 1.5% measured impact than the first entry's 0.5%.
        nearest = core.derive_level_from_sweep(
            sweep_by_side["buy"], 1.2, "buy", 10.0, 50_000_000.0,
        )
        expected = sweep_by_side["buy"][1]
        self.assertEqual(nearest["derived_from"], "nearest_real_quote")
        self.assertTrue(nearest["target_reached"])
        self.assertEqual(nearest["bound"], "none")
        self.assertEqual(
            (nearest["amount_usd"], nearest["price"], nearest["actual_impact_pct"]),
            (expected["amount_usd"], expected["price"], expected["impact_pct"]),
        )
        self.assertIs(nearest["_raw"], expected["_raw"])

        def fake_anchor(
            cfg_arg: PairConfig,
            side: str,
            target_pct: float,
            sweep: list[dict],
            spot: float,
            state_arg: RecordingState,
            max_iters: int = 5,
            tolerance: float = 0.02,
        ) -> dict | None:
            if target_pct != 1.0:
                return None
            block = 100 if side == "buy" else 101
            raw = _raw_quote(cfg_arg, block, side, "anchor")
            return {
                "q": raw,
                "price": 2_020.0 if side == "buy" else 1_980.0,
                "impact": 1.0,
                "usd": 1_000.0,
            }

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
            snap, payload = core.collect_snapshot(cfg, state)
        finally:
            for name, original in originals.items():
                setattr(core, name, original)

        self.assertIsNotNone(snap)
        self.assertIsNotNone(payload)
        assert payload is not None

        self.assertEqual(payload["block_row"]["block"], 100)
        self.assertEqual(payload["block_row"]["mixed_block"], 1)
        self.assertEqual(state.mixed_blocks, 1)
        self.assertTrue(any(phase == "block_identity" for phase, _msg in state.errors))

        for table in ("levels", "curve_points", "route_legs", "quote_responses"):
            self.assertTrue(payload[table], table)
            self.assertEqual({row["block"] for row in payload[table]}, {100})

        self.assertEqual({row["side"] for row in payload["levels"]}, {"buy"})
        self.assertEqual({row["side"] for row in payload["curve_points"]}, {"buy"})
        self.assertEqual({row["side"] for row in payload["route_legs"]}, {"buy"})
        self.assertEqual({row["side"] for row in payload["quote_responses"]}, {"buy"})

        nearest_rows = [
            row for row in payload["levels"]
            if row["target_impact_pct"] == 1.2
        ]
        self.assertEqual(len(nearest_rows), 1)
        nearest_row = nearest_rows[0]
        self.assertEqual(nearest_row["derived_from"], "nearest_real_quote")
        self.assertEqual(
            (
                nearest_row["amount_usd"],
                nearest_row["effective_price"],
                nearest_row["actual_impact_pct"],
            ),
            (expected["amount_usd"], expected["price"], expected["impact_pct"]),
        )

        for response in payload["quote_responses"]:
            raw = json.loads(response["raw_response_json"])
            self.assertEqual(raw["block"]["number"], response["block"])

        persisted = json.dumps(payload, sort_keys=True)
        self.assertNotIn("101-sell", persisted)
        self.assertNotIn("pool-101", persisted)


class NullSinkMixedBlockTest(unittest.TestCase):
    def test_null_sink_survives_a_mixed_block_cycle(self) -> None:
        # A sweep straddling a block boundary runs state.mixed_blocks += 1 in
        # collect_snapshot. A NullSink without that counter raised AttributeError
        # and aborted the standalone generate flow; this exercises it directly.
        cfg = PairConfig(
            impact_levels=[1.0, 1.2],
            sweep_samples_per_side=3,
            max_workers=2,
            tenderly_from_address="0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        )
        state = NullSink()
        sweep_by_side = {
            "buy": [_sweep_entry(cfg, 100, "buy", 0), _sweep_entry(cfg, 100, "buy", 1)],
            "sell": [_sweep_entry(cfg, 101, "sell", 0)],
        }

        def fake_anchor(cfg_arg, side, target_pct, sweep, spot, state_arg,
                        max_iters=5, tolerance=0.02):
            if target_pct != 1.0:
                return None
            block = 100 if side == "buy" else 101
            return {"q": _raw_quote(cfg_arg, block, side, "anchor"),
                    "price": 2_020.0 if side == "buy" else 1_980.0,
                    "impact": 1.0, "usd": 1_000.0}

        originals = {name: getattr(core, name) for name in (
            "get_block_number", "fynd_spot", "sweep_side",
            "anchor_target_from_sweep", "compute_robust_mid")}
        core.get_block_number = lambda rpc_url: 99
        core.fynd_spot = lambda cfg_arg, state_arg: 2_000.0
        core.sweep_side = lambda cfg_arg, side, spot, state_arg, num_samples: sweep_by_side[side]
        core.anchor_target_from_sweep = fake_anchor
        core.compute_robust_mid = lambda cfg_arg, spot, max_depth_usd, state_arg: (2_000.0, 1_000.0)
        try:
            _snap, payload = core.collect_snapshot(cfg, state)
        finally:
            for name, original in originals.items():
                setattr(core, name, original)

        # The increment ran against a real NullSink without AttributeError.
        self.assertEqual(state.mixed_blocks, 1)
        self.assertIsNotNone(payload)
        self.assertEqual(payload["block_row"]["mixed_block"], 1)


if __name__ == "__main__":
    unittest.main()
