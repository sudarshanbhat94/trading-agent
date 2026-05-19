from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.analysis_tools import _select_performance_feedback
from app.db import Database


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


if __name__ == "__main__":
    unittest.main()
