from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from app.analysis_tools import _select_performance_feedback
from app.config import Settings
from app.db import Database
from app.indicators import technical_snapshot
from app.models import Candle, Quote
from app.strategy import StrategyEngine


class Phase4PerformanceFeedbackTests(unittest.TestCase):
    def test_strategy_feedback_tracks_required_closed_trade_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(Path(tmpdir) / "agent.db")
            db.init()
            _seed_trade(
                db,
                idea_id=1,
                follow_id=1,
                symbol="ALPHA",
                strategy="vcp_breakout",
                market="NASDAQ",
                return_pct=4.0,
                realized_pnl=40.0,
                mfe=5.0,
                mae=-1.0,
                t1_hit=True,
                stop_hit=False,
            )
            _seed_trade(
                db,
                idea_id=2,
                follow_id=2,
                symbol="BETA",
                strategy="vcp_breakout",
                market="NASDAQ",
                return_pct=-2.0,
                realized_pnl=-20.0,
                mfe=1.0,
                mae=-3.0,
                t1_hit=False,
                stop_hit=True,
            )

            feedback = db.strategy_performance_feedback(user_id=7)

        strategy = feedback["by_strategy"][0]
        market = feedback["by_market"][0]
        self.assertEqual(strategy["key"], "vcp_breakout")
        self.assertEqual(strategy["closed_trades"], 2)
        self.assertEqual(strategy["win_rate"], 0.5)
        self.assertEqual(strategy["average_gain_pct"], 4.0)
        self.assertEqual(strategy["average_loss_pct"], -2.0)
        self.assertEqual(strategy["stop_hit_rate"], 0.5)
        self.assertEqual(strategy["target_1_hit_rate"], 0.5)
        self.assertEqual(strategy["avg_time_to_target_1_hours"], 2.0)
        self.assertEqual(strategy["max_adverse_excursion_pct"], -3.0)
        self.assertEqual(strategy["max_favorable_excursion_pct"], 5.0)
        self.assertEqual(strategy["expectancy_pct"], 1.0)
        self.assertEqual(market["key"], "US")
        self.assertEqual(market["expectancy_pct"], 1.0)

    def test_performance_summary_exposes_phase4_feedback(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(Path(tmpdir) / "agent.db")
            db.init()
            _seed_trade(
                db,
                idea_id=1,
                follow_id=1,
                symbol="ALPHA",
                strategy="darvas_box_breakout",
                market="NASDAQ",
                return_pct=3.0,
                realized_pnl=30.0,
                mfe=4.0,
                mae=-0.8,
                t1_hit=True,
                stop_hit=False,
            )

            summary = db.performance_summary(user_id=7)

        phase4 = summary["strategy_performance_feedback"]
        self.assertEqual(phase4["version"], "phase4-performance-feedback-v1")
        self.assertEqual(phase4["overall"]["closed_trades"], 1)
        self.assertEqual(phase4["by_strategy"][0]["key"], "darvas_box_breakout")

    def test_symbol_context_selects_matching_strategy_and_market_feedback(self) -> None:
        feedback = {
            "version": "phase4-performance-feedback-v1",
            "scope": "all_users",
            "overall": {"closed_trades": 4, "expectancy_pct": 0.2},
            "by_market": [{"key": "US", "closed_trades": 3, "expectancy_pct": 1.2, "feedback_score": 0.3}],
            "by_strategy": [{"key": "vcp_breakout", "closed_trades": 3, "expectancy_pct": 1.5, "feedback_score": 0.4}],
            "by_strategy_market": [
                {
                    "key": "vcp_breakout|US",
                    "strategy": "vcp_breakout",
                    "market_region": "US",
                    "closed_trades": 3,
                    "expectancy_pct": 1.6,
                    "feedback_score": 0.45,
                }
            ],
        }

        selected = _select_performance_feedback(feedback, market_region="US", strategy_name="vcp_breakout")

        self.assertTrue(selected["available"])
        self.assertEqual(selected["selected_strategy"]["expectancy_pct"], 1.5)
        self.assertEqual(selected["selected_market"]["expectancy_pct"], 1.2)
        self.assertEqual(selected["selected_strategy_market"]["expectancy_pct"], 1.6)

    def test_news_refresh_rebuild_keeps_performance_feedback_in_scope(self) -> None:
        settings = replace(Settings(), enable_news_sentiment=True, news_symbols_per_cycle=2)
        engine = StrategyEngine(settings, _FakeSentiment(), _FakeLLM())
        candles = _candles("SCOPE", "alpaca-live:day", 80)
        quote = Quote(
            symbol="SCOPE",
            price=182.0,
            source="alpaca-live",
            asof="2026-05-20T14:30:00+00:00",
            volume=5_000_000,
        )
        technical = technical_snapshot(
            [candle.close for candle in candles],
            [candle.high for candle in candles],
            [candle.low for candle in candles],
            [candle.volume for candle in candles],
        )
        scan_items = [
            {
                "row": {"symbol": "SCOPE", "name": "Scope Inc", "exchange": "NASDAQ"},
                "symbol": "SCOPE",
                "quote": quote,
                "technical": technical,
                "sentiment_score": 0.0,
                "sentiment_detail": {},
                "candles": candles,
                "timeframe_candles": {"daily": candles, "intraday": candles[-40:]},
                "delivery_data": {},
                "options_data": {"flow_available": True},
                "sector_context": {},
                "macro_event_context": {"source": "earnings_calendar"},
                "context": {"sentiment": {}, "best_strategy": {"score": 0.4}, "full_spectrum_analysis": {}},
                "combined": 0.42,
                "score_breakdown": {},
                "action": "BUY",
                "confidence": 0.8,
            }
        ]

        import asyncio

        asyncio.run(
            engine._refresh_candidate_sentiment(
                scan_items,
                positions={},
                candles_by_symbol={"SCOPE": candles},
                risk_limits={
                    "global_risk_weight": settings.global_risk_weight,
                    "institutional_risk_weight": settings.institutional_risk_weight,
                    "llm_candidate_limit": settings.llm_max_symbols_per_cycle,
                    "execution_cost_bps": 0.0,
                },
                global_context={},
                institutional_context={},
                market_breadth={},
                performance_feedback={
                    "version": "phase4-performance-feedback-v1",
                    "scope": "unit",
                    "overall": {"closed_trades": 4, "expectancy_pct": -0.4},
                    "by_market": [{"key": "US", "closed_trades": 4, "expectancy_pct": -0.4}],
                },
            )
        )

        feedback = scan_items[0]["context"]["performance_feedback"]
        self.assertTrue(feedback["available"])
        self.assertEqual(feedback["selected_market"]["expectancy_pct"], -0.4)


def _seed_trade(
    db: Database,
    idea_id: int,
    follow_id: int,
    symbol: str,
    strategy: str,
    market: str,
    return_pct: float,
    realized_pnl: float,
    mfe: float,
    mae: float,
    t1_hit: bool,
    stop_hit: bool,
) -> None:
    opened = "2026-05-20T09:00:00+00:00"
    t1_hit_at = "2026-05-20T11:00:00+00:00" if t1_hit else None
    closed = "2026-05-20T15:00:00+00:00"
    idea_details = {
        "target_status": [
            {"label": "T1", "price": 104.0, "hit": t1_hit, "hit_at": t1_hit_at},
        ],
        "highest_target_hit": "T1" if t1_hit else "NONE",
        "lifecycle_status": "stopped" if stop_hit else "target_1_hit",
    }
    follow_details = {
        "mark_state": {"peak_return_pct": mfe, "worst_return_pct": mae, "last_mark_at": closed},
        "exit_management": {
            "realized_pnl_total": realized_pnl,
            "closed_qty_total": 10,
            "last_action_at": closed,
            "last_reason": "Stop loss hit" if stop_hit else "Target reached",
            "events": [
                {
                    "key": "STOP_LOSS" if stop_hit else "TARGET_1_PARTIAL",
                    "return_pct": return_pct,
                    "realized_pnl": realized_pnl,
                    "at": closed,
                }
            ],
        },
    }
    with db.connect() as conn:
        conn.execute(
            """
            insert into universe (symbol, name, exchange, base_price, enabled)
            values (?, ?, ?, 100, 1)
            """,
            (symbol, symbol.title(), market),
        )
        conn.execute(
            """
            insert into signal_ideas (
                id, first_seen_at, last_seen_at, symbol, strategy, plan_code,
                signal_type, status, entry_price, latest_price, current_return_pct,
                peak_return_pct, worst_return_pct, confidence, combined_score,
                confluence, overall_score_pct, overall_grade, reason, details_json
            )
            values (?, ?, ?, ?, ?, 'confirmed_breakout', 'BUY', ?, 100, ?, ?, ?, ?, 0.8, 0.5, 18, 78, 'B', 'test', ?)
            """,
            (
                idea_id,
                opened,
                closed,
                symbol,
                strategy,
                "STOP_HIT" if stop_hit else "TARGET_1_HIT",
                100 * (1 + return_pct / 100),
                return_pct,
                mfe,
                mae,
                json.dumps(idea_details),
            ),
        )
        conn.execute(
            """
            insert into user_idea_follows (
                id, user_id, idea_id, mode, status, qty, entry_price, latest_price,
                invested_amount, unrealized_pnl, return_pct, created_at, updated_at, details_json
            )
            values (?, 7, ?, 'PAPER', 'EXITED', 0, 100, ?, 0, ?, ?, ?, ?, ?)
            """,
            (
                follow_id,
                idea_id,
                100 * (1 + return_pct / 100),
                realized_pnl,
                return_pct,
                opened,
                closed,
                json.dumps(follow_details),
            ),
        )


def _candles(symbol: str, source: str, count: int) -> list[Candle]:
    return [
        Candle(
            symbol=symbol,
            ts=f"2026-01-{(index % 28) + 1:02d}T00:00:00+00:00",
            open=100 + index,
            high=102 + index,
            low=99 + index,
            close=101 + index,
            volume=1_000_000 + index * 1000,
            source=source,
        )
        for index in range(count)
    ]


class _FakeSentiment:
    db = None

    async def analyze_symbol_news(self, row: dict) -> dict:
        return {
            "status": "AVAILABLE",
            "score": 0.18,
            "confidence": 0.44,
            "headline_count": 3,
            "headlines": [f"{row['symbol']} analyst upgrade"],
        }


class _FakeLLM:
    enabled = False


if __name__ == "__main__":
    unittest.main()
