from __future__ import annotations

import asyncio
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from app.config import Settings
from app.db import Database
from app.sentiment import SentimentService, _upstox_news_items_from_payload


class UpstoxNewsTests(unittest.TestCase):
    def test_upstox_news_payload_maps_instrument_key_to_news_item(self) -> None:
        published_time = int(datetime.now(timezone.utc).timestamp() * 1000)
        payload = {
            "status": "success",
            "data": {
                "NSE_EQ|INE002A01018": [
                    {
                        "heading": "Reliance wins large clean energy order",
                        "summary": "The company announced a fresh order and higher growth outlook.",
                        "thumbnail": "https://assets.upstox.com/news.webp",
                        "article_link": "https://upstox.com/news/market-news/latest-updates/example/",
                        "published_time": published_time,
                    }
                ]
            },
        }

        items = _upstox_news_items_from_payload(
            payload,
            {"NSE_EQ|INE002A01018": "RELIANCE"},
            symbol="RELIANCE",
        )

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "Reliance wins large clean energy order")
        self.assertEqual(items[0]["summary"], "The company announced a fresh order and higher growth outlook.")
        self.assertEqual(items[0]["source"], "Upstox News")
        self.assertEqual(items[0]["instrument_key"], "NSE_EQ|INE002A01018")

    def test_upstox_news_is_primary_for_india_rows(self) -> None:
        published_time = int(datetime.now(timezone.utc).timestamp() * 1000)
        response_payload = {
            "status": "success",
            "data": {
                "NSE_EQ|INE002A01018": [
                    {
                        "heading": "Reliance profit beats estimates",
                        "summary": "Revenue rises after strong retail and energy demand.",
                        "article_link": "https://upstox.com/news/market-news/latest-updates/reliance/",
                        "published_time": published_time,
                    }
                ]
            },
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(Path(tmpdir) / "agent.db")
            db.init()
            settings = replace(
                Settings(),
                upstox_access_token="Bearer test-token",
                upstox_api_base_url="https://api.upstox.com/v2",
                enable_llm_sentiment=False,
            )
            service = SentimentService(settings, db)
            row = {
                "symbol": "RELIANCE",
                "name": "Reliance Industries",
                "exchange": "NSE",
                "upstox_instrument_key": "NSE_EQ|INE002A01018",
            }

            with patch("app.sentiment.httpx.AsyncClient", _fake_async_client(response_payload)) as fake_client:
                result = asyncio.run(service.analyze_symbol_news(row))

        self.assertEqual(fake_client.last_url, "https://api.upstox.com/v2/news")
        self.assertEqual(fake_client.last_params["category"], "instrument_keys")
        self.assertEqual(fake_client.last_params["instrument_keys"], "NSE_EQ|INE002A01018")
        self.assertEqual(result["source"], "Upstox News")
        self.assertEqual(result["headlines"], ["Reliance profit beats estimates"])
        self.assertEqual(result["events"][0]["summary"], "Revenue rises after strong retail and energy demand.")
        self.assertEqual(result["data_status"], "OK")


def _fake_async_client(payload: dict) -> type:
    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return payload

    class FakeAsyncClient:
        last_url: str | None = None
        last_params: dict | None = None

        def __init__(self, *args, **kwargs) -> None:
            self.args = args
            self.kwargs = kwargs

        async def __aenter__(self) -> "FakeAsyncClient":
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def get(self, url: str, params: dict | None = None) -> FakeResponse:
            type(self).last_url = url
            type(self).last_params = params or {}
            return FakeResponse()

    return FakeAsyncClient
