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

    def test_us_top_mover_uses_us_playbook_not_indian_rules(self) -> None:
        scanner = OpportunityScanner(_settings())
        candles = _candles("NVDA", "polygon:day", 260, datetime(2025, 5, 1, tzinfo=timezone.utc))
        pivot = max(candle.high for candle in candles[-41:-1])
        price = round(pivot * 1.02, 2)
        candles[-1] = Candle("NVDA", candles[-1].ts, price * 0.98, price * 1.01, price * 0.97, price, 8_000_000, "polygon:day")
        row = {
            "symbol": "NVDA",
            "name": "NVIDIA Corporation",
            "exchange": "NASDAQ",
            "sector": "Technology",
            "index_membership": "NASDAQ100",
            "market_cap": 4_000_000_000_000,
            "_market_action": {
                "symbol": "NVDA",
                "market_region": "US",
                "event_types": ["TOP_GAINER", "VOLUME_SHOCKER", "52_WEEK_HIGH"],
                "pct_change": 6.0,
                "price": price,
                "volume": 82_000_000,
                "avg_volume": 32_000_000,
                "volume_multiplier": 2.56,
            },
        }

        item = scanner._score_row(
            row,
            Quote("NVDA", price, "polygon-live", "2026-05-26T21:00:00+00:00", open=price * 0.96, high=price * 1.01, low=price * 0.95, volume=82_000_000),
            {"analysis": candles, "daily": candles},
            False,
            {"headlines": ["Nvidia earnings beat estimates and guidance raised"], "headline_count": 1, "score": 0.5, "confidence": 0.8, "events": [{"event_type": "earnings", "confidence": 0.8, "source_weight": 1.0}]},
            {"rs_rank": 96, "improving": True},
        )

        playbook = item["top_gainers_playbook"]
        self.assertTrue(playbook["available"])
        self.assertEqual(playbook["source"], "yahoo_us_top_movers_playbook")
        self.assertEqual(playbook["market_region"], "US")
        self.assertNotIn("delivery_pct", playbook["data_gaps"])
        self.assertEqual(playbook["delivery"]["trend"], "not_applicable_us")

    def test_btst_buy_candidate_scores_next_day_follow_through_setup(self) -> None:
        scanner = OpportunityScanner(_settings())
        row = {"symbol": "BTSTWIN", "exchange": "NSE", "sector": "Industrials"}
        daily = _candles("BTSTWIN", "upstox-live:day", 90, datetime(2026, 2, 1, tzinfo=timezone.utc))
        intraday = _candles("BTSTWIN", "upstox-live:30minute", 24, datetime(2026, 5, 26, 3, 45, tzinfo=timezone.utc))
        quote = Quote(
            "BTSTWIN",
            114.0,
            "upstox-live",
            "2026-05-26T15:10:00+05:30",
            open=111.0,
            high=114.4,
            low=110.6,
            volume=2_200_000,
        )

        result = scanner.rank(
            [row],
            {"BTSTWIN": quote},
            {"BTSTWIN": {"analysis": daily, "daily": daily, "intraday": intraday}},
            sentiment_by_symbol={
                "BTSTWIN": {
                    "score": 0.3,
                    "confidence": 0.4,
                    "headline_count": 1,
                    "events": [{"event_type": "order_win", "confidence": 0.4, "source_weight": 0.8}],
                }
            },
        )

        self.assertEqual(result.candidates[0]["setup"], "btst_buy_candidate")
        self.assertEqual(result.candidates[0]["bucket"], "Actionable")
        self.assertTrue(result.candidates[0]["btst"]["detected"])
        self.assertEqual(result.candidates[0]["btst"]["action_bias"], "BUY")
        self.assertEqual(result.summary["btst_buy_candidates"][0]["symbol"], "BTSTWIN")

    def test_btst_rejects_late_chase_gap_risk(self) -> None:
        scanner = OpportunityScanner(_settings())
        row = {"symbol": "BTSTLATE", "exchange": "NSE", "sector": "Industrials"}
        daily = _candles("BTSTLATE", "upstox-live:day", 90, datetime(2026, 2, 1, tzinfo=timezone.utc))
        intraday = _candles("BTSTLATE", "upstox-live:30minute", 24, datetime(2026, 5, 26, 3, 45, tzinfo=timezone.utc))
        quote = Quote(
            "BTSTLATE",
            122.0,
            "upstox-live",
            "2026-05-26T15:10:00+05:30",
            open=112.0,
            high=122.5,
            low=111.8,
            volume=5_500_000,
        )

        item = scanner._score_row(
            row,
            quote,
            {"analysis": daily, "daily": daily, "intraday": intraday},
            False,
            {},
            {"rs_rank": 82, "improving": True},
        )

        self.assertFalse(item["btst"]["detected"])
        self.assertFalse(item["btst"]["checks"]["day_move_ok"])
        self.assertNotEqual(item["setup"], "btst_buy_candidate")


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
