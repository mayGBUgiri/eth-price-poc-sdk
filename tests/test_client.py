from __future__ import annotations

import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import requests

from eth_price_poc.client import EthPricePoCClient, EthPricePoCDataUnavailable


class FakeResponse:
    def __init__(self, status_code: int, payload=None):
        self.status_code = status_code
        self._payload = payload

    @property
    def ok(self) -> bool:
        return self.status_code < 400

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")

    def json(self):
        return self._payload


class FakeSession:
    """Records the URL of the last GET and returns a queued response."""

    def __init__(self, response=None, exc: Exception | None = None):
        self.response = response
        self.exc = exc
        self.last_url: str | None = None

    def get(self, url, timeout=None):
        self.last_url = url
        if self.exc is not None:
            raise self.exc
        return self.response


class RoutingSession:
    """Returns a response based on which URL substring the GET matches."""

    def __init__(self, routes):
        self.routes = routes  # list of (url_substring, FakeResponse | Exception)
        self.urls: list[str] = []

    def get(self, url, timeout=None):
        self.urls.append(url)
        for substring, response in self.routes:
            if substring in url:
                if isinstance(response, Exception):
                    raise response
                return response
        raise AssertionError(f"no route for {url}")


class GetOptionalTest(unittest.TestCase):
    def test_200_returns_parsed_body(self) -> None:
        session = FakeSession(FakeResponse(200, {"block": 42}))
        client = EthPricePoCClient("https://example.test", session=session)
        self.assertEqual(client.detail(42, "buy", 1.0), {"block": 42})
        self.assertEqual(
            session.last_url,
            "https://example.test/api/detail?block=42&side=buy&target_impact_pct=1.0",
        )

    def test_404_returns_none(self) -> None:
        session = FakeSession(FakeResponse(404))
        client = EthPricePoCClient("https://example.test", session=session)
        self.assertIsNone(client.export(42, "sell", 5.0))

    def test_transport_failure_raises(self) -> None:
        session = FakeSession(exc=requests.ConnectionError("boom"))
        client = EthPricePoCClient("https://example.test", session=session)
        with self.assertRaises(EthPricePoCDataUnavailable):
            client.detail(42, "buy", 1.0)

    def test_server_error_raises(self) -> None:
        session = FakeSession(FakeResponse(500))
        client = EthPricePoCClient("https://example.test", session=session)
        with self.assertRaises(EthPricePoCDataUnavailable):
            client.detail(42, "buy", 1.0)

    def test_invalid_side_rejected_before_request(self) -> None:
        session = FakeSession()
        client = EthPricePoCClient("https://example.test", session=session)
        with self.assertRaises(ValueError):
            client.detail(42, "up", 1.0)
        self.assertIsNone(session.last_url)


class TokensTest(unittest.TestCase):
    def test_returns_isolated_copy(self) -> None:
        client = EthPricePoCClient("https://example.test")
        first = client.tokens()
        first["token_in"]["decimals"] = 999
        second = client.tokens()
        self.assertEqual(second["token_in"]["decimals"], 6)

    def test_unknown_pair_raises(self) -> None:
        client = EthPricePoCClient("https://example.test", pair="FOO/BAR")
        with self.assertRaises(EthPricePoCDataUnavailable):
            client.tokens()


class HistoryTest(unittest.TestCase):
    @staticmethod
    def _compact_payload() -> dict:
        return {"blocks": [{
            "block": 100,
            "robust_mid": 2000.0,
            "curve": {"buy": {"a": [50, 500], "p": [10.0, 11.0]},
                      "sell": {"a": [50, 500], "p": [9.0, 8.0]}},
        }]}

    def test_compact_curves_normalized_to_lists(self) -> None:
        session = FakeSession(FakeResponse(200, self._compact_payload()))
        client = EthPricePoCClient("https://example.test", session=session)
        curve = client.history()["blocks"][0]["curve"]
        self.assertEqual(curve["buy"], [{"amount_usd": 50, "price": 10.0},
                                        {"amount_usd": 500, "price": 11.0}])
        self.assertEqual(curve["sell"], [{"amount_usd": 50, "price": 9.0},
                                         {"amount_usd": 500, "price": 8.0}])

    def test_list_curves_pass_through_unchanged(self) -> None:
        rich_point = {"amount_usd": 1.0, "price": 2.0, "gas_estimate": "3", "impact_pct": 0.1}
        payload = {"blocks": [{"block": 1, "curve": {"buy": [dict(rich_point)]}}]}
        session = FakeSession(FakeResponse(200, payload))
        client = EthPricePoCClient("https://example.test", session=session)
        self.assertEqual(client.history()["blocks"][0]["curve"]["buy"], [rich_point])

    def test_blocks_without_curve_tolerated(self) -> None:
        session = FakeSession(FakeResponse(200, {"blocks": [{"block": 1}]}))
        client = EthPricePoCClient("https://example.test", session=session)
        self.assertEqual(client.history()["blocks"], [{"block": 1}])

    def test_query_parameters(self) -> None:
        session = FakeSession(FakeResponse(200, {"blocks": []}))
        client = EthPricePoCClient("https://example.test", session=session)
        client.history(limit=10, curve_n=48)
        self.assertEqual(session.last_url,
                         "https://example.test/api/history?limit=10&curve_n=48")
        client.history(limit=10, full=True)
        self.assertEqual(session.last_url, "https://example.test/api/history?limit=10&full=1")
        client.history(curve_n=200)
        self.assertEqual(session.last_url, "https://example.test/api/history?curve_n=200")
        client.history()
        self.assertEqual(session.last_url, "https://example.test/api/history")

    def test_mismatched_compact_arrays_raise(self) -> None:
        payload = {"blocks": [{"block": 1, "curve": {"buy": {"a": [50, 500], "p": [10.0]}}}]}
        session = FakeSession(FakeResponse(200, payload))
        client = EthPricePoCClient("https://example.test", session=session)
        with self.assertRaises(EthPricePoCDataUnavailable):
            client.history()

    def test_malformed_compact_curve_raises(self) -> None:
        bad_sides = (
            {"a": 5, "p": None},        # non-list a/p values
            {"a": "50", "p": "10"},     # strings are iterable but not curves
            {"a": [50]},                # missing p
            {"p": [10.0]},              # missing a
            {"a": [50, 500], "p": [10.0]},  # length mismatch
            42,                         # scalar curve side
            "garbage",                  # string curve side
        )
        for bad_side in bad_sides:
            with self.subTest(bad_side=bad_side):
                payload = {"blocks": [{"block": 1, "curve": {"buy": bad_side}}]}
                session = FakeSession(FakeResponse(200, payload))
                client = EthPricePoCClient("https://example.test", session=session)
                with self.assertRaises(EthPricePoCDataUnavailable):
                    client.history()

    def test_empty_compact_curve_normalizes_to_empty_list(self) -> None:
        payload = {"blocks": [{"block": 1, "curve": {"buy": {"a": [], "p": []}}}]}
        session = FakeSession(FakeResponse(200, payload))
        client = EthPricePoCClient("https://example.test", session=session)
        self.assertEqual(client.history()["blocks"][0]["curve"]["buy"], [])

    def test_non_dict_block_raises(self) -> None:
        session = FakeSession(FakeResponse(200, {"blocks": ["garbage"]}))
        client = EthPricePoCClient("https://example.test", session=session)
        with self.assertRaises(EthPricePoCDataUnavailable):
            client.history()

    def test_curve_n_and_full_mutually_exclusive(self) -> None:
        session = FakeSession(FakeResponse(200, {"blocks": []}))
        client = EthPricePoCClient("https://example.test", session=session)
        with self.assertRaises(ValueError):
            client.history(curve_n=48, full=True)
        self.assertIsNone(session.last_url)

    def test_static_fallback_slices_and_normalizes(self) -> None:
        static = {"blocks": [
            {"block": i, "curve": {"buy": {"a": [50], "p": [float(i)]}}}
            for i in (1, 2, 3)
        ]}
        session = RoutingSession([
            ("/api/history", requests.ConnectionError("down")),
            ("/data.json", FakeResponse(200, static)),
        ])
        client = EthPricePoCClient("https://example.test", session=session)
        hist = client.history(limit=2)
        self.assertEqual([b["block"] for b in hist["blocks"]], [2, 3])
        self.assertEqual(hist["blocks"][0]["curve"]["buy"],
                         [{"amount_usd": 50, "price": 2.0}])


class CurveForBlockTest(unittest.TestCase):
    def _client(self, routes):
        return EthPricePoCClient("https://example.test", session=RoutingSession(routes))

    def test_dense_curve_preferred_over_embedded(self) -> None:
        history = {"blocks": [{"block": 100, "curve": {"buy": [{"amount_usd": 1, "price": 2}]}}]}
        dense = {"curve": {"buy": [{"amount_usd": 1, "price": 2}, {"amount_usd": 3, "price": 4}]}}
        client = self._client([
            ("/api/history", FakeResponse(200, history)),
            ("/api/curve", FakeResponse(200, dense)),
        ])
        self.assertEqual(len(client.curve_for_block(0, "buy")), 2)

    def test_embedded_curve_used_when_dense_unavailable(self) -> None:
        history = {"blocks": [{"block": 100, "curve": {"buy": [{"amount_usd": 1, "price": 2}]}}]}
        client = self._client([
            ("/api/history", FakeResponse(200, history)),
            ("/api/curve", FakeResponse(500)),
        ])
        self.assertEqual(client.curve_for_block(0, "buy"), [{"amount_usd": 1, "price": 2}])

    def test_embedded_compact_curve_normalized(self) -> None:
        history = {"blocks": [{"block": 100, "curve": {"buy": {"a": [50], "p": [10.0]}}}]}
        client = self._client([
            ("/api/history", FakeResponse(200, history)),
            ("/api/curve", FakeResponse(500)),
        ])
        self.assertEqual(client.curve_for_block(0, "buy"),
                         [{"amount_usd": 50, "price": 10.0}])

    def test_embedded_empty_compact_curve_raises(self) -> None:
        history = {"blocks": [{"block": 100, "curve": {"buy": {"a": [], "p": []}}}]}
        client = self._client([
            ("/api/history", FakeResponse(200, history)),
            ("/api/curve", FakeResponse(404)),
        ])
        with self.assertRaises(EthPricePoCDataUnavailable):
            client.curve_for_block(0, "buy")

    def test_raises_when_neither_dense_nor_embedded(self) -> None:
        client = self._client([
            ("/api/history", FakeResponse(200, {"blocks": [{"block": 100}]})),
            ("/api/curve", FakeResponse(404)),
        ])
        with self.assertRaises(EthPricePoCDataUnavailable):
            client.curve_for_block(0, "buy")

    def test_out_of_range_index_raises(self) -> None:
        client = self._client([("/api/history", FakeResponse(200, {"blocks": [{"block": 100}]}))])
        with self.assertRaises(EthPricePoCDataUnavailable):
            client.curve_for_block(5, "buy")

    def test_invalid_side_rejected_before_request(self) -> None:
        client = self._client([])
        with self.assertRaises(ValueError):
            client.curve_for_block(0, "up")


if __name__ == "__main__":
    unittest.main()
