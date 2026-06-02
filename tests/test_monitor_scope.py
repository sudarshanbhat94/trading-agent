from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from app.agent import TradingAgentService
from app.db import Database
from app.models import Decision, Quote, utc_now


class MonitorScopeTests(unittest.TestCase):
    def test_latest_signal_ideas_can_be_limited_to_monitor_symbols(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(Path(tmpdir) / "agent.db")
            db.init()
            self._insert_idea(db, "GOOGL")
            self._insert_idea(db, "TSLA")
            self._insert_idea(db, "NVDA")

            rows = db.latest_signal_ideas(10, symbols=["GOOGL", "NVDA"])

        self.assertEqual({row["symbol"] for row in rows}, {"GOOGL", "NVDA"})
        self.assertNotIn("TSLA", {row["symbol"] for row in rows})

    def test_latest_decisions_can_be_limited_to_monitor_symbols(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(Path(tmpdir) / "agent.db")
            db.init()
            db.insert_decisions(
                [
                    Decision("GOOGL", "BUY", 0.8, 190, 0.7, 0.1, "allowed", utc_now(), "unit"),
                    Decision("TSLA", "BUY", 0.9, 300, 0.8, 0.1, "outside", utc_now(), "unit"),
                    Decision("NVDA", "HOLD", 0.5, 210, 0.6, 0.1, "allowed", utc_now(), "unit"),
                ]
            )

            rows = db.latest_decision_summaries(10, symbols=["GOOGL", "NVDA"])

        self.assertEqual({row["symbol"] for row in rows}, {"GOOGL", "NVDA"})
        self.assertNotIn("TSLA", {row["symbol"] for row in rows})

    def test_followed_ideas_can_be_limited_to_monitor_symbols(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(Path(tmpdir) / "agent.db")
            db.init()
            user = db.create_user("inder", "hash", role="user", active=True)
            googl = self._insert_idea(db, "GOOGL")
            tsla = self._insert_idea(db, "TSLA")
            db.follow_signal_idea(int(user["id"]), googl, mode="TRACK")
            db.follow_signal_idea(int(user["id"]), tsla, mode="TRACK")

            rows = db.user_followed_signal_ideas(int(user["id"]), 10, symbols=["GOOGL"])

        self.assertEqual([row["symbol"] for row in rows], ["GOOGL"])

    def test_follow_history_can_be_limited_to_monitor_symbols(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(Path(tmpdir) / "agent.db")
            db.init()
            user = db.create_user("inder", "hash", role="user", active=True)
            googl = self._insert_idea(db, "GOOGL")
            tsla = self._insert_idea(db, "TSLA")
            now = utc_now()
            with db.connect() as conn:
                conn.executemany(
                    """
                    insert into user_idea_follows (
                        user_id, idea_id, mode, status, qty, entry_price, latest_price,
                        invested_amount, unrealized_pnl, return_pct, created_at, updated_at, details_json
                    )
                    values (?, ?, 'PAPER', 'ACTIVE', 1, 100, 100, 100, 0, 0, ?, ?, '{}')
                    """,
                    [
                        (int(user["id"]), googl, now, now),
                        (int(user["id"]), tsla, now, now),
                    ],
                )

            rows = db.user_follow_history(int(user["id"]), 10, symbols=["GOOGL"])

        self.assertEqual([row["symbol"] for row in rows], ["GOOGL"])

    def test_monitor_watchlist_shows_symbols_before_signal_ideas_exist(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(Path(tmpdir) / "agent.db")
            db.init()
            user = db.create_user("inder", "hash", role="user", active=True)
            db.update_user_monitor_symbols(int(user["id"]), ["GOOGL", "AMZN", "NVDA"])
            with db.connect() as conn:
                conn.executemany(
                    """
                    insert into universe (symbol, name, exchange, sector, enabled)
                    values (?, ?, 'NASDAQ', 'US Equity', 1)
                    """,
                    [
                        ("GOOGL", "Alphabet Inc."),
                        ("AMZN", "Amazon.com Inc."),
                        ("NVDA", "NVIDIA Corporation"),
                    ],
                )
            db.upsert_quotes(
                {
                    "GOOGL": Quote("GOOGL", 190.0, "unit", utc_now(), close=188.0),
                    "AMZN": Quote("AMZN", 180.0, "unit", utc_now(), close=181.0),
                }
            )

            rows = db.monitor_watchlist_rows(
                db.user_monitor_symbols(int(user["id"])),
                user_id=int(user["id"]),
                market_region="US",
            )

        self.assertEqual([row["symbol"] for row in rows], ["GOOGL", "AMZN", "NVDA"])
        self.assertTrue(all(row["watchlist_source"] == "monitor_symbols" for row in rows))
        self.assertTrue(all(row["status"] == "MONITORING" for row in rows))
        self.assertEqual(rows[0]["latest_price"], 190.0)
        self.assertEqual(rows[0]["current_return_pct"], 1.0638)

    def test_shared_auto_paper_respects_user_monitor_symbols(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(Path(tmpdir) / "agent.db")
            db.init()
            user = db.create_user(
                "inder",
                "hash",
                role="user",
                active=True,
                signal_execution_mode="AUTO_PAPER",
            )
            db.update_user_monitor_symbols(int(user["id"]), ["GOOGL", "AMZN", "NVDA", "AMD", "NFLX", "RKLB", "PL"])
            self._insert_idea(db, "TSLA")
            service = TradingAgentService(
                db=db,
                market_data=SimpleNamespace(),
                broker=SimpleNamespace(),
                strategy=SimpleNamespace(settings=SimpleNamespace()),
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

            summary = service._auto_follow_buy_ideas_for_signal_users(
                [SimpleNamespace(symbol="TSLA", action="BUY")]
            )
            followed = db.user_followed_signal_ideas(int(user["id"]), 20)

        self.assertEqual(summary["followed"], 0)
        self.assertEqual(followed, [])
        self.assertTrue(
            any(item.get("reason") == "outside_custom_monitor_list" for item in summary["skipped"]),
            summary,
        )

    def test_shared_auto_paper_follows_dynamic_india_buy_with_fresh_live_quote(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(Path(tmpdir) / "agent.db")
            db.init()
            user = db.create_user(
                "sudarshan",
                "hash",
                role="user",
                active=True,
                signal_execution_mode="AUTO_PAPER",
            )
            user_id = int(user["id"])
            db.update_user_paper_cash(user_id, cash_in=100_000)
            with db.connect() as conn:
                conn.execute(
                    """
                    insert into universe (symbol, name, exchange, sector, enabled)
                    values ('IFCI', 'IFCI Ltd', 'NSE', 'Financial Services', 1)
                    """
                )
            decision = self._fresh_live_quote_india_buy("IFCI")
            db.insert_decisions([decision])
            db.upsert_signal_ideas_from_decisions([decision])
            service = TradingAgentService(
                db=db,
                market_data=SimpleNamespace(),
                broker=SimpleNamespace(),
                strategy=SimpleNamespace(
                    settings=SimpleNamespace(
                        initial_cash_inr=100_000,
                        max_position_pct=0.25,
                        paper_min_auto_follow_notional_inr=7_500.0,
                        paper_min_auto_follow_notional_usd=250.0,
                    )
                ),
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

            [idea] = db.latest_signal_ideas(10, user_id=user_id, market_region="IN")
            summary = service._auto_follow_buy_ideas_for_signal_users([decision])
            followed = db.user_followed_signal_ideas(user_id, 20, market_region="IN")

        self.assertEqual(idea["symbol"], "IFCI")
        self.assertEqual(idea["signal_type"], "BUY")
        self.assertEqual(idea["status"], "ACTIVE")
        self.assertEqual(idea["fresh_action"], "BUY_NOW")
        self.assertEqual(summary["followed"], 1, summary)
        self.assertEqual(len(followed), 1)
        self.assertEqual(followed[0]["symbol"], "IFCI")
        self.assertEqual(followed[0]["mode"], "PAPER")
        self.assertEqual(followed[0]["follow_status"], "ACTIVE")
        self.assertGreaterEqual(float(followed[0]["invested_amount"]), 7_500.0)

    def test_shared_auto_trade_explains_stale_active_buy_monitor_without_follow(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(Path(tmpdir) / "agent.db")
            db.init()
            user = db.create_user(
                "sudarshan",
                "hash",
                role="user",
                active=True,
                signal_execution_mode="AUTO_PAPER",
            )
            db.update_user_paper_cash(int(user["id"]), cash_in=100_000)
            idea_id = self._insert_idea(db, "OLDMONITOR")
            stale = (datetime.now(timezone.utc) - timedelta(minutes=60)).isoformat()
            with db.connect() as conn:
                conn.execute("update signal_ideas set last_seen_at = ? where id = ?", (stale, idea_id))
            service = TradingAgentService(
                db=db,
                market_data=SimpleNamespace(),
                broker=SimpleNamespace(),
                strategy=SimpleNamespace(settings=SimpleNamespace()),
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

            summary = service._auto_follow_buy_ideas_for_signal_users([])

        self.assertEqual(summary["followed"], 0)
        self.assertTrue(
            any(
                item.get("symbol") == "OLDMONITOR"
                and item.get("reason") == "active_buy_not_fresh_enough_for_auto_follow"
                for item in summary["skipped"]
            ),
            summary,
        )

    @staticmethod
    def _insert_idea(db: Database, symbol: str) -> int:
        now = utc_now()
        details = {
            "action": "BUY",
            "overall_score_pct": 84,
            "overall_grade": "A",
            "hard_blocked": False,
            "hard_blocks": [],
            "data_readiness": {"trade_decision_ready": True},
        }
        with db.connect() as conn:
            conn.execute(
                """
                insert into signal_ideas (
                    first_seen_at, last_seen_at, symbol, strategy, plan_code, signal_type, status,
                    entry_price, latest_price, current_return_pct, peak_return_pct, worst_return_pct,
                    confidence, combined_score, confluence, overall_score_pct, overall_grade,
                    decision_id, latest_decision_id, reason, details_json
                )
                values (?, ?, ?, 'monitor_scope_test', 'monitor_scope_test', 'BUY', 'ACTIVE',
                    100, 100, 0, 0, 0, 0.86, 0.7, 24, 84, 'A', null, null, 'test buy', ?)
                """,
                (now, now, symbol, json.dumps(details)),
            )
            row = conn.execute("select last_insert_rowid() as id").fetchone()
            return int(row["id"])

    @staticmethod
    def _fresh_live_quote_india_buy(symbol: str) -> Decision:
        details = {
            "action_reason": "fresh live India quote buy",
            "score_breakdown": {"combined": 0.38, "score_percent": 68.8},
            "system_gate_audit": {
                "hard_blocked": False,
                "hard_blocks": [],
                "overall_score_pct": 88.44,
                "overall_grade": "A",
                "data_readiness": {
                    "market_region": "IN",
                    "trade_decision_ready": True,
                    "fresh_market_data_gate": {
                        "passed": True,
                        "reason": "live_quote_ready_intraday_reference_stale",
                    },
                    "hard_gaps": [],
                    "soft_gaps": [],
                    "sources": {"quote": "upstox-live"},
                },
            },
            "context": {
                "quote": {
                    "price": 69.45,
                    "open": 67.0,
                    "high": 72.0,
                    "low": 66.61,
                    "volume": 47_546_371,
                    "source": "upstox-live",
                },
                "data_readiness": {
                    "market_region": "IN",
                    "trade_decision_ready": True,
                    "fresh_market_data_gate": {
                        "passed": True,
                        "reason": "live_quote_ready_intraday_reference_stale",
                    },
                    "hard_gaps": [],
                    "soft_gaps": [],
                    "sources": {"quote": "upstox-live"},
                },
                "decision_gate_context": {
                    "failed_gates": [
                        {"gate": "session_momentum_gate", "reason": "broad_momentum_entry_needs_current_session_confirmation"},
                        {"gate": "overall_quality_gate", "reason": "overall_score_below_70_no_new_longs"},
                    ]
                },
                "opportunity_scan": {
                    "bucket": "Actionable",
                    "setup": "52_week_high_volume_breakout",
                    "score": 0.8844,
                    "turnover": 3_302_095_465,
                    "avg20_turnover": 1_990_521_527,
                    "data_quality": {
                        "actionable_data_ready": False,
                        "missing": ["stale_intraday_candles"],
                    },
                },
                "full_spectrum_analysis": {
                    "confluence_score": {"total": 16, "tier": "TRADE_SIGNAL"},
                    "signal_plan": {"direction": "BUY", "decision_readiness": "actionable"},
                    "trade_plan": {
                        "entry_zone": [68.5, 70.0],
                        "stop_loss": 68.5,
                        "targets": [{"price": 73.0, "distance_pct": 5.0}],
                    },
                    "risk_overrides": {"flags": []},
                    "strategy_logic_filters": {"passed": True, "hard_blocks": []},
                    "breakout_quality": {"breakout_quality": "not_breakout", "volume_confirmation": True},
                    "entry_quality": {"entry_grade": "A"},
                },
            },
        }
        return Decision(
            symbol=symbol,
            action="BUY",
            confidence=0.91,
            price=69.45,
            technical_score=0.81,
            sentiment_score=0.0,
            reason="fresh live India quote buy",
            asof=utc_now(),
            strategy="aggressive_relative_strength_breakout",
            details_json=json.dumps(details),
        )


if __name__ == "__main__":
    unittest.main()
