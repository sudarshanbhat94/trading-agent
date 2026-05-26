from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

from app.market_data import UpstoxMarketDataProvider, _nse_market_date


class UpstoxCandleTests(unittest.TestCase):
    def test_intraday_endpoint_is_merged_for_current_day_candles(self) -> None:
        today = _nse_market_date().isoformat()
        provider = UpstoxMarketDataProvider(
            SimpleNamespace(
                upstox_access_token="token",
                upstox_api_base_url="https://api.upstox.com/v2",
                upstox_candle_interval="30minute",
                upstox_candle_lookback_days=3,
                enable_upstox_multi_timeframe_candles=False,
                upstox_daily_candle_lookback_days=220,
                upstox_weekly_candle_lookback_days=220,
                upstox_candle_concurrency=1,
                upstox_candle_fetch_timeout_seconds=5,
            )
        )
        client = _FakeUpstoxClient(today)

        async def fetch() -> tuple:
            return await provider._fetch_candle_series(
                client,
                asyncio.Semaphore(1),
                {"symbol": "IDEA", "upstox_instrument_key": "NSE_EQ|INE669E01016"},
                {"interval": "30minute", "lookback_days": 3, "source": "upstox-live:30minute"},
            )

        result = asyncio.run(fetch())

        self.assertIsNotNone(result)
        symbol, candles, meta = result
        self.assertEqual(symbol, "IDEA")
        self.assertTrue(any("/historical-candle/intraday/" in url for url in client.urls))
        self.assertEqual(meta["historical_count"], 2)
        self.assertEqual(meta["intraday_count"], 2)
        self.assertEqual([candle.ts for candle in candles], [f"{today}T09:15:00+05:30", f"{today}T09:45:00+05:30"])
        self.assertEqual(candles[0].close, 11.2)
        self.assertEqual(candles[-1].source, "upstox-live:30minute")


class _FakeUpstoxClient:
    def __init__(self, today: str) -> None:
        self.today = today
        self.urls: list[str] = []

    async def get(self, url: str) -> "_FakeResponse":
        self.urls.append(url)
        if "/historical-candle/intraday/" in url:
            return _FakeResponse(
                {
                    "data": {
                        "candles": [
                            [f"{self.today}T09:15:00+05:30", 10.9, 11.3, 10.8, 11.2, 2_100_000],
                            [f"{self.today}T09:45:00+05:30", 11.2, 11.6, 11.1, 11.5, 2_300_000],
                        ]
                    }
                }
            )
        return _FakeResponse(
            {
                "data": {
                    "candles": [
                        [f"{self.today}T09:15:00+05:30", 10.7, 11.0, 10.6, 10.8, 800_000],
                        [f"{self.today}T09:15:00+05:30", 10.8, 11.1, 10.7, 11.0, 900_000],
                    ]
                }
            }
        )


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


if __name__ == "__main__":
    unittest.main()
