from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.agent import TradingAgentService
from app.db import Database
from app.models import utc_now


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
