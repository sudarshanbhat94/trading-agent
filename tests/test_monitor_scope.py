from __future__ import annotations

import json
import tempfile
import unittest
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


if __name__ == "__main__":
    unittest.main()
