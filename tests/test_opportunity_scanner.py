from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.models import Candle, Quote
from app.opportunity_scanner import OpportunityScanner


class OpportunityScannerTests(unittest.TestCase):
    def test_stale_intraday_candles_are_visible_and_not_actionable(self) -> None:
        scanner = OpportunityScanner(_settings())
        row = {"symbol": "HFCL", "exchange": "NSE", "sector": "Telecom"}
        quote = Quote("HFCL", 103.0, "upstox-live", "2026-05-26T10:30:00+05:30", open=100.0, high=104.0, low=99.5, volume=2_000_000)
        daily = _candles("HFCL", "upstox-live:day", 70, datetime(2026, 2, 1, tzinfo=timezone.utc))
        intraday = _candles("HFCL", "upstox-live:30minute", 24, datetime(2026, 5, 23, 9, 15, tzinfo=timezone.utc))

        item = scanner._score_row(
            row,
            quote,
            {"analysis": daily, "daily": daily, "intraday": intraday},
            False,
            {"score": 0.3, "confidence": 0.7, "events": [{"event_type": "earnings", "confidence": 0.6, "source_weight": 0.8}]},
        )

        missing = item["data_quality"]["missing"]
        self.assertIn("stale_intraday_candles", missing)
        self.assertTrue(item["data_quality"]["tradeable_screening"])
        self.assertFalse(item["data_quality"]["actionable_data_ready"])

    def test_current_session_intraday_candles_pass_actionable_freshness(self) -> None:
        scanner = OpportunityScanner(_settings())
        row = {"symbol": "HFCL", "exchange": "NSE", "sector": "Telecom"}
        quote = Quote("HFCL", 103.0, "upstox-live", "2026-05-26T10:30:00+05:30", open=100.0, high=104.0, low=99.5, volume=2_000_000)
        daily = _candles("HFCL", "upstox-live:day", 70, datetime(2026, 2, 1, tzinfo=timezone.utc))
        intraday = _candles("HFCL", "upstox-live:30minute", 24, datetime(2026, 5, 26, 3, 45, tzinfo=timezone.utc))

        item = scanner._score_row(
            row,
            quote,
            {"analysis": daily, "daily": daily, "intraday": intraday},
            False,
            {"score": 0.3, "confidence": 0.7, "events": [{"event_type": "earnings", "confidence": 0.6, "source_weight": 0.8}]},
        )

        self.assertNotIn("stale_intraday_candles", item["data_quality"]["missing"])
        self.assertTrue(item["data_quality"]["actionable_data_ready"])


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        dynamic_scan_candidate_limit=60,
        dynamic_scan_min_score=0.50,
        dynamic_scan_require_active_setup=False,
        dynamic_scan_min_price=10.0,
        dynamic_scan_min_turnover_inr=40_000_000.0,
        dynamic_scan_min_turnover_usd=2_000_000.0,
        dynamic_scan_breakout_distance_pct=3.0,
        dynamic_scan_sentiment_enabled=True,
        dynamic_scan_sentiment_weight=0.12,
    )


def _candles(symbol: str, source: str, count: int, start: datetime) -> list[Candle]:
    output: list[Candle] = []
    for index in range(count):
        ts = start + timedelta(days=index if "day" in source else 0, minutes=30 * index if "minute" in source else 0)
        close = 80.0 + index * 0.35
        output.append(
            Candle(
                symbol=symbol,
                ts=ts.isoformat(),
                open=close * 0.995,
                high=close * 1.015,
                low=close * 0.985,
                close=close,
                volume=900_000 + index * 1_000,
                source=source,
            )
        )
    return output


if __name__ == "__main__":
    unittest.main()
