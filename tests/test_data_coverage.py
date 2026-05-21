from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from app.agent import TradingAgentService, _opportunity_scan_by_market
from app.db import Database
from app.models import Candle, Decision, utc_now
from scripts.update_us_universe import build_us_universe_rows


class DataCoverageTests(unittest.TestCase):
    def test_db_candle_coverage_groups_timeframes_without_per_symbol_fetch(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            db = Database(Path(tempdir) / "coverage.db")
            db.init()
            db.upsert_candles(
                {
                    "AAA": [
                        *_candles("AAA", "upstox-live:30minute", 25),
                        *_candles("AAA", "upstox-live:day", 70),
                        *_candles("AAA", "upstox-live:week", 30),
                    ],
                    "MSFT": [
                        *_candles("MSFT", "yahoo-delayed", 80),
                        *_candles("MSFT", "alpaca-live:1minute", 35),
                    ],
                }
            )

            coverage = db.candle_coverage_by_symbol(["AAA", "MSFT", "MISSING"])

        self.assertEqual(coverage["AAA"]["intraday"]["count"], 25)
        self.assertEqual(coverage["AAA"]["daily"]["count"], 70)
        self.assertEqual(coverage["AAA"]["weekly"]["count"], 30)
        self.assertEqual(coverage["MSFT"]["daily"]["count"], 80)
        self.assertEqual(coverage["MSFT"]["intraday"]["count"], 35)
        self.assertEqual(coverage["MISSING"]["analysis"]["count"], 0)

    def test_backfill_selects_missing_history_outside_opportunity_shortlist(self) -> None:
        db = _FakeCoverageDb(
            {
                "READY": _coverage(daily=80, intraday=25, weekly=30),
                "MISS1": _coverage(daily=0, intraday=0, weekly=0),
                "MISS2": _coverage(daily=80, intraday=0, weekly=30),
                "MISS3": _coverage(daily=80, intraday=25, weekly=0),
            }
        )
        agent = _agent(db, provider="upstox-live", backfill_limit=2)
        universe = [
            {"symbol": "READY", "exchange": "NSE"},
            {"symbol": "MISS1", "exchange": "NSE"},
            {"symbol": "MISS2", "exchange": "NSE"},
            {"symbol": "MISS3", "exchange": "NSE"},
        ]

        selected, plan = agent._candle_backfill_universe(universe, excluded_symbols={"READY"})

        self.assertEqual([row["symbol"] for row in selected], ["MISS1", "MISS2"])
        self.assertEqual(plan["selected_symbols"], 2)
        self.assertGreater(plan["missing_or_short_history"], 0)
        self.assertEqual(db.state["candle_backfill_cursor"], 3)

    def test_yahoo_us_backfill_does_not_chase_unavailable_minute_bars(self) -> None:
        db = _FakeCoverageDb({"AAPL": _coverage(daily=80, intraday=0, weekly=0)})
        agent = _agent(db, provider="yahoo-delayed", backfill_limit=5)

        selected, plan = agent._candle_backfill_universe([{"symbol": "AAPL", "exchange": "NASDAQ"}], set())

        self.assertEqual(selected, [])
        self.assertEqual(plan["cache_ready"], 1)

    def test_alpaca_us_backfill_requires_minute_bars_for_actionable_data(self) -> None:
        db = _FakeCoverageDb({"AAPL": _coverage(daily=80, intraday=0, weekly=0)})
        agent = _agent(db, provider="alpaca-live", backfill_limit=5)

        selected, plan = agent._candle_backfill_universe([{"symbol": "AAPL", "exchange": "NASDAQ"}], set())

        self.assertEqual([row["symbol"] for row in selected], ["AAPL"])
        self.assertEqual(plan["missing_or_short_history"], 1)

    def test_us_universe_builder_filters_tests_and_non_equity_noise(self) -> None:
        nasdaq_text = "\n".join(
            [
                "Symbol|Security Name|Market Category|Test Issue|Financial Status|Round Lot Size|ETF|NextShares",
                "MSFT|Microsoft Corporation Common Stock|Q|N|N|100|N|N",
                "BADW|Bad Co Warrant|Q|N|N|100|N|N",
                "TEST|Test Company Common Stock|Q|Y|N|100|N|N",
            ]
        )
        other_text = "\n".join(
            [
                "ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot Size|Test Issue|NASDAQ Symbol",
                "BRK.B|Berkshire Hathaway Inc. Class B Common Stock|N|BRK.B|N|100|N|BRK.B",
                "SPY|SPDR S&P 500 ETF Trust|P|SPY|Y|100|N|SPY",
            ]
        )

        rows = build_us_universe_rows(nasdaq_text, other_text)

        self.assertEqual([row["symbol"] for row in rows], ["SPY", "MSFT", "BRK-B"])
        self.assertEqual(rows[0]["sector"], "ETF")
        self.assertNotIn("BADW", {row["symbol"] for row in rows})
        self.assertNotIn("TEST", {row["symbol"] for row in rows})

    def test_shared_ai_cycle_billing_splits_llm_credits_across_active_users(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            db = Database(Path(tempdir) / "billing.db")
            db.init()
            first = db.create_user("first", "hash", role="user", active=True)
            second = db.create_user("second", "hash", role="user", active=True)
            db.create_user("admin", "hash", role="admin", active=True)
            for user in (first, second):
                db.adjust_user_credits(int(user["id"]), 1_000, "seed credits")
                db.update_user_daily_credit_limit(int(user["id"]), 1_000)
            agent = _billing_agent(db)
            usage = {"calls": 1, "cost_usd": 0.01, "total_tokens": 1000, "input_chars": 800, "output_chars": 200}
            decision = Decision(
                symbol="AAA",
                action="HOLD",
                confidence=0.5,
                price=100,
                technical_score=0.4,
                sentiment_score=0,
                reason="reviewed",
                asof=utc_now(),
                strategy="unit",
                details_json='{"decision_path":"llm_primary_review"}',
            )

            summary = agent._charge_shared_ai_cycle_to_users(usage, [decision], [{"symbol": "AAA"}], "shared:test")

            self.assertEqual(summary["participants"], 2)
            self.assertEqual(summary["charged_users"], 2)
            self.assertAlmostEqual(summary["per_user_credits"], 50.0)
            ledger = db.user_credit_summary(int(first["id"]))["ledger"]
            self.assertEqual(ledger[0]["description"], "Shared AI opportunity cycle")
            self.assertAlmostEqual(-ledger[0]["amount"], 50.0)

    def test_opportunity_scan_summary_splits_enabled_counts_by_market(self) -> None:
        summary = {
            "enabled": True,
            "mode": "market_closed_tomorrow_prep",
            "scan_paused": True,
            "scanned_at": utc_now(),
            "raw_scan_limit": 500,
            "top_candidates": [],
        }
        full_universe = [
            {"symbol": "RELIANCE", "exchange": "NSE"},
            {"symbol": "INFY", "exchange": "NSE"},
            {"symbol": "AAPL", "exchange": "NASDAQ"},
        ]
        news_summary = {
            "symbols": [
                {"symbol": "RELIANCE", "headline_count": 2, "event_count": 2},
                {"symbol": "AAPL", "headline_count": 1, "event_count": 1},
            ]
        }

        by_market = _opportunity_scan_by_market(summary, full_universe, [], [], [], news_summary)

        self.assertEqual(by_market["IN"]["enabled_universe_symbols"], 2)
        self.assertEqual(by_market["US"]["enabled_universe_symbols"], 1)
        self.assertEqual(by_market["IN"]["news_screened_symbols"], 1)
        self.assertEqual(by_market["US"]["news_screened_symbols"], 1)


class _FakeCoverageDb:
    def __init__(self, coverage: dict[str, dict]) -> None:
        self.coverage = coverage
        self.state: dict[str, object] = {}

    def candle_coverage_by_symbol(self, symbols: list[str]) -> dict[str, dict]:
        return {symbol: self.coverage.get(symbol, _coverage()) for symbol in symbols}

    def get_state(self, key: str, default: object = None) -> object:
        return self.state.get(key, default)

    def set_state(self, key: str, value: object) -> None:
        self.state[key] = value


def _agent(db: _FakeCoverageDb, provider: str, backfill_limit: int) -> TradingAgentService:
    settings = SimpleNamespace(
        dynamic_scan_candidate_limit=60,
        dynamic_scan_min_score=0.58,
        dynamic_scan_require_active_setup=True,
        dynamic_scan_min_price=10.0,
        dynamic_scan_min_turnover_inr=50_000_000.0,
        dynamic_scan_breakout_distance_pct=3.0,
        dynamic_scan_sentiment_enabled=True,
        dynamic_scan_sentiment_weight=0.12,
        candle_backfill_enabled=True,
        candle_backfill_symbols_per_cycle=backfill_limit,
        candle_backfill_min_daily_candles=55,
        candle_backfill_min_intraday_candles=20,
        candle_backfill_min_weekly_candles=20,
        candle_backfill_retry_hours=6,
    )
    return TradingAgentService(
        db=db,
        market_data=SimpleNamespace(source_name=provider),
        broker=SimpleNamespace(),
        strategy=SimpleNamespace(settings=settings),
        macro=None,
        institutional_feeds=None,
        delivery_service=None,
        market_breadth=None,
        sector_rotation=None,
        macro_calendar=None,
        options_intelligence=None,
        interval_seconds=60,
        cycle_timeout_seconds=60,
    )


def _billing_agent(db: Database) -> TradingAgentService:
    settings = SimpleNamespace(
        dynamic_scan_candidate_limit=60,
        dynamic_scan_min_score=0.58,
        dynamic_scan_require_active_setup=True,
        dynamic_scan_min_price=10.0,
        dynamic_scan_min_turnover_inr=50_000_000.0,
        dynamic_scan_breakout_distance_pct=3.0,
        dynamic_scan_sentiment_enabled=True,
        dynamic_scan_sentiment_weight=0.12,
        credit_tokens_per_credit=10,
        credit_platform_margin_pct=0.20,
        initial_cash_inr=100_000,
        max_position_pct=0.25,
    )
    return TradingAgentService(
        db=db,
        market_data=SimpleNamespace(source_name="unit-test"),
        broker=SimpleNamespace(),
        strategy=SimpleNamespace(settings=settings),
        macro=None,
        institutional_feeds=None,
        delivery_service=None,
        market_breadth=None,
        sector_rotation=None,
        macro_calendar=None,
        options_intelligence=None,
        interval_seconds=60,
        cycle_timeout_seconds=60,
    )


def _coverage(daily: int = 0, intraday: int = 0, weekly: int = 0) -> dict[str, dict]:
    latest = datetime.now(timezone.utc).isoformat()
    analysis_count = max(daily, intraday)
    sources = {}
    if daily:
        sources["upstox-live:day"] = {"count": daily, "latest_ts": latest}
    if intraday:
        sources["upstox-live:30minute"] = {"count": intraday, "latest_ts": latest}
    if weekly:
        sources["upstox-live:week"] = {"count": weekly, "latest_ts": latest}
    return {
        "daily": {"count": daily, "latest_ts": latest if daily else None},
        "intraday": {"count": intraday, "latest_ts": latest if intraday else None},
        "weekly": {"count": weekly, "latest_ts": latest if weekly else None},
        "analysis": {"count": analysis_count, "latest_ts": latest if analysis_count else None},
        "sources": sources,
    }


def _candles(symbol: str, source: str, count: int) -> list[Candle]:
    start = datetime(2026, 1, 1, 9, 30, tzinfo=timezone.utc)
    return [
        Candle(
            symbol=symbol,
            ts=(start + timedelta(days=index)).isoformat(),
            open=100 + index,
            high=101 + index,
            low=99 + index,
            close=100.5 + index,
            volume=1_000_000 + index,
            source=source,
        )
        for index in range(count)
    ]


if __name__ == "__main__":
    unittest.main()
