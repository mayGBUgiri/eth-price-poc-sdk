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


if __name__ == "__main__":
    unittest.main()
