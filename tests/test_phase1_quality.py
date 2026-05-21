from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.config import CONFIG_SCHEMA, Settings, settings_from_overrides
from app.db import Database
from app.models import Decision, utc_now
from app.signal_quality import fresh_buy_quality_gate


class Phase1QualityGateTests(unittest.TestCase):
    def test_fresh_buy_gate_blocks_non_tradeable_ideas(self) -> None:
        cases = [
            (
                {"signal_type": "WATCH", "status": "WATCH", "overall_score_pct": 82, "overall_grade": "A"},
                "not_fresh_buy_signal",
            ),
            (
                {"signal_type": "BUY", "status": "ACTIVE", "overall_score_pct": 69, "overall_grade": "A"},
                "overall_score_below_70",
            ),
            (
                {"signal_type": "BUY", "status": "ACTIVE", "overall_score_pct": 78, "overall_grade": "C"},
                "grade_not_a_or_b",
            ),
            (
                {
                    "signal_type": "BUY",
                    "status": "ACTIVE",
                    "overall_score_pct": 78,
                    "overall_grade": "A",
                    "details": {"breakout_quality": {"breakout_quality": "suspect"}},
                },
                "suspect_breakout_without_volume",
            ),
        ]

        for item, reason in cases:
            with self.subTest(reason=reason):
                gate = fresh_buy_quality_gate(item)
                self.assertFalse(gate["passed"])
                self.assertEqual(gate["reason"], reason)

    def test_fresh_buy_gate_allows_strong_confirmed_buy(self) -> None:
        gate = fresh_buy_quality_gate(
            {
                "signal_type": "BUY",
                "status": "ACTIVE",
                "overall_score_pct": 76,
                "overall_grade": "B",
                "details": {
                    "breakout_quality": {"breakout_quality": "suspect", "volume_confirmation": True},
                    "data_readiness": {"trade_decision_ready": True},
                },
            }
        )

        self.assertTrue(gate["passed"])
        self.assertEqual(gate["reason"], "fresh_buy_quality_passed")

    def test_runtime_overrides_are_clamped_to_phase1_minimums(self) -> None:
        settings = settings_from_overrides(
            Settings(),
            {"llm_max_symbols_per_cycle": "1", "auto_follow_reentry_cooldown_hours": "12"},
        )
        schema_by_key = {item["key"]: item for item in CONFIG_SCHEMA}

        self.assertEqual(settings.llm_max_symbols_per_cycle, 8)
        self.assertEqual(settings.auto_follow_reentry_cooldown_hours, 48)
        self.assertEqual(schema_by_key["llm_max_symbols_per_cycle"]["min"], 8)
        self.assertEqual(schema_by_key["auto_follow_reentry_cooldown_hours"]["min"], 48)


class Phase1FollowSafetyTests(unittest.TestCase):
    def test_weak_buy_decisions_are_downgraded_to_watch_ideas(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(Path(tmpdir) / "agent.db")
            db.init()
            decision = Decision(
                symbol="WEAKBUY",
                action="BUY",
                confidence=0.72,
                price=100,
                technical_score=0.4,
                sentiment_score=0.2,
                reason="weak buy should only be watched",
                asof=utc_now(),
                details_json=json.dumps(
                    {
                        "action_reason": "weak buy should only be watched",
                        "score_breakdown": {"combined": 0.32},
                        "system_gate_audit": {"overall_score_pct": 52, "overall_grade": "D", "hard_blocked": False},
                        "context": {
                            "full_spectrum_analysis": {
                                "confluence_score": {"total": 22},
                                "trade_plan": {"entry_zone": [98, 102], "stop_loss": 94, "targets": []},
                                "risk_overrides": {"flags": []},
                            }
                        },
                    }
                ),
            )

            db.insert_decisions([decision])
            db.upsert_signal_ideas_from_decisions([decision])
            with db.connect() as conn:
                row = conn.execute("select * from signal_ideas where symbol = 'WEAKBUY'").fetchone()

        self.assertIsNotNone(row)
        self.assertEqual(row["signal_type"], "WATCH")
        self.assertEqual(row["status"], "WATCH")
        details = json.loads(row["details_json"])
        self.assertEqual(details["quality_downgrade"]["from"], "BUY")
        self.assertEqual(details["quality_gate"]["reason"], "overall_score_below_70")

    def test_manual_paper_follow_rejects_watch_or_weak_ideas(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(Path(tmpdir) / "agent.db")
            db.init()
            idea_id = self._insert_signal_idea(
                db,
                signal_type="WATCH",
                status="WATCH",
                score=52,
                grade="D",
            )

            with self.assertRaisesRegex(ValueError, "phase1_quality_gate:not_fresh_buy_signal"):
                db.follow_signal_idea(1, idea_id, mode="PAPER", amount=10_000)

    def test_legacy_watch_follow_still_displays_as_watch_not_trade(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(Path(tmpdir) / "agent.db")
            db.init()
            idea_id = self._insert_signal_idea(
                db,
                signal_type="WATCH",
                status="WATCH",
                score=72,
                grade="B",
            )
            now = utc_now()
            with db.connect() as conn:
                conn.execute(
                    """
                    insert into user_idea_follows (
                        user_id, idea_id, mode, status, qty, entry_price, latest_price,
                        invested_amount, unrealized_pnl, return_pct, created_at, updated_at, details_json
                    )
                    values (?, ?, 'PAPER', 'ACTIVE', 10, 100, 100, 1000, 0, 0, ?, ?, '{}')
                    """,
                    (1, idea_id, now, now),
                )
            latest = db.latest_signal_ideas(5, user_id=1)[0]

        self.assertEqual(latest["display_signal"], "Watch")
        self.assertEqual(latest["trade_state"], "WATCH")
        self.assertEqual(latest["execution_state"], "WATCH")

    def test_safety_cleanup_exits_existing_watch_or_weak_paper_follows(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(Path(tmpdir) / "agent.db")
            db.init()
            watch_id = self._insert_signal_idea(
                db,
                signal_type="WATCH",
                status="WATCH",
                score=84,
                grade="A",
            )
            weak_id = self._insert_signal_idea(
                db,
                signal_type="BUY",
                status="ACTIVE",
                score=52,
                grade="D",
            )
            now = utc_now()
            with db.connect() as conn:
                for idea_id in (watch_id, weak_id):
                    conn.execute(
                        """
                        insert into user_idea_follows (
                            user_id, idea_id, mode, status, qty, entry_price, latest_price,
                            invested_amount, unrealized_pnl, return_pct, created_at, updated_at, details_json
                        )
                        values (1, ?, 'PAPER', 'ACTIVE', 10, 100, 100, 1000, 0, 0, ?, ?, '{}')
                        """,
                        (idea_id, now, now),
                    )

            exited = db.exit_unsafe_active_follows()
            active = [
                item
                for item in db.user_followed_signal_ideas(1, 20)
                if item["follow_status"] == "ACTIVE" and item["mode"] == "PAPER" and item["qty"] > 0
            ]

        self.assertEqual(len(exited), 2)
        self.assertEqual(active, [])
        self.assertEqual({item["status"] for item in exited}, {"EXITED"})

    def test_manual_paper_follow_allows_strong_buy_ideas(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(Path(tmpdir) / "agent.db")
            db.init()
            idea_id = self._insert_signal_idea(
                db,
                signal_type="BUY",
                status="ACTIVE",
                score=82,
                grade="A",
            )

            follow = db.follow_signal_idea(1, idea_id, mode="PAPER", amount=10_000)
            latest = db.latest_signal_ideas(5, user_id=1)[0]

        self.assertEqual(follow["mode"], "PAPER")
        self.assertEqual(follow["status"], "ACTIVE")
        self.assertGreater(follow["qty"], 0)
        self.assertEqual(latest["display_signal"], "Paper Entered")
        self.assertEqual(latest["trade_state"], "PAPER_ENTERED")
        self.assertEqual(latest["execution_state"], "PAPER_ENTERED")

    def test_duplicate_active_buy_is_labeled_as_already_active_monitor(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(Path(tmpdir) / "agent.db")
            db.init()
            self._insert_signal_idea(
                db,
                signal_type="BUY",
                status="ACTIVE",
                score=84,
                grade="A",
                details_extra={
                    "latest_system_action": "BUY",
                    "signal_continuity": {"duplicate_active_buy": True},
                    "why_changed": {"summary": "Already active. Repeated BUY is monitor only."},
                },
            )
            latest = db.latest_signal_ideas(5)[0]

        self.assertEqual(latest["display_signal"], "Already Active")
        self.assertEqual(latest["trade_state"], "POSITION_MONITOR")
        self.assertEqual(latest["fresh_action_label"], "No Fresh Add")

    @staticmethod
    def _insert_signal_idea(
        db: Database,
        *,
        signal_type: str,
        status: str,
        score: float,
        grade: str,
        details_extra: dict | None = None,
    ) -> int:
        now = utc_now()
        details = {
            "action": signal_type,
            "overall_score_pct": score,
            "overall_grade": grade,
            "hard_blocked": False,
            "hard_blocks": [],
            "data_readiness": {"trade_decision_ready": True},
        }
        if details_extra:
            details.update(details_extra)
        with db.connect() as conn:
            conn.execute(
                """
                insert into signal_ideas (
                    first_seen_at, last_seen_at, symbol, strategy, plan_code, signal_type, status,
                    entry_price, latest_price, current_return_pct, peak_return_pct, worst_return_pct,
                    confidence, combined_score, confluence, overall_score_pct, overall_grade,
                    decision_id, latest_decision_id, reason, details_json
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 0, ?, ?, ?, ?, ?, null, null, ?, ?)
                """,
                (
                    now,
                    now,
                    f"{signal_type}{grade}",
                    "phase1_test",
                    "phase1_test",
                    signal_type,
                    status,
                    100.0,
                    100.0,
                    0.8,
                    0.4,
                    22.0,
                    score,
                    grade,
                    "phase1 test idea",
                    json.dumps(details),
                ),
            )
            row = conn.execute("select last_insert_rowid() as id").fetchone()
            return int(row["id"])


if __name__ == "__main__":
    unittest.main()
