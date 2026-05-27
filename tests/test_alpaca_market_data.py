from __future__ import annotations

import unittest
from unittest.mock import patch

from app.config import Settings
from app.market_data import AlpacaMarketDataProvider, MarketDataError


class AlpacaMarketDataProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_quotes_use_alpaca_key_secret_headers_and_feed(self) -> None:
        calls: list[dict] = []

        class FakeResponse:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict:
                return {"quotes": {"AAPL": {"ap": 101.0, "bp": 99.0, "t": "2026-05-27T20:00:00Z"}}}

        class FakeAsyncClient:
            def __init__(self, *args, **kwargs) -> None:
                calls.append({"init": kwargs})

            async def __aenter__(self) -> "FakeAsyncClient":
                return self

            async def __aexit__(self, *args) -> None:
                return None

            async def get(self, url: str, params: dict) -> FakeResponse:
                calls.append({"url": url, "params": params})
                return FakeResponse()

        settings = Settings(
            alpaca_api_key="key-123",
            alpaca_api_secret="secret-456",
            alpaca_data_feed="iex",
        )

        with patch("app.market_data.httpx.AsyncClient", FakeAsyncClient):
            quotes = await AlpacaMarketDataProvider(settings).get_quotes([{"symbol": "AAPL", "exchange": "NASDAQ"}])

        self.assertEqual(quotes["AAPL"].price, 100.0)
        self.assertEqual(quotes["AAPL"].source, "alpaca-iex-live")
        self.assertEqual(calls[0]["init"]["headers"]["APCA-API-KEY-ID"], "key-123")
        self.assertEqual(calls[0]["init"]["headers"]["APCA-API-SECRET-KEY"], "secret-456")
        self.assertEqual(calls[1]["url"], "https://data.alpaca.markets/v2/stocks/quotes/latest")
        self.assertEqual(calls[1]["params"]["symbols"], "AAPL")
        self.assertEqual(calls[1]["params"]["feed"], "iex")

    async def test_missing_alpaca_keys_fail_fast(self) -> None:
        provider = AlpacaMarketDataProvider(Settings(alpaca_api_key="", alpaca_api_secret=""))

        with self.assertRaises(MarketDataError):
            await provider.get_quotes([{"symbol": "AAPL", "exchange": "NASDAQ"}])


if __name__ == "__main__":
    unittest.main()
