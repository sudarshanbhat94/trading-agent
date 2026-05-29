from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.db import Database
from app.tomorrow_plan import build_tomorrow_plan


class TomorrowPlanTests(unittest.TestCase):
    def _sample_plan(self) -> dict:
        return build_tomorrow_plan(
            market_region="IN",
            prepared_at="2026-05-22T12:00:00+05:30",
            signal_ideas=[
                {
                    "id": 11,
                    "symbol": "READY",
                    "company_name": "Ready Industries",
                    "market_region": "IN",
                    "signal_type": "BUY",
                    "status": "ACTIVE",
                    "strategy": "weininstein_stage2_breakout",
                    "latest_price": 102,
                    "confidence": 0.82,
                    "overall_score_pct": 78,
                    "details": {
                        "quality_gate": {"passed": True, "message": "Stage 2 breakout with volume confirmation."},
                        "entry_zone": [100, 105],
                        "stop_loss": 93,
                        "target_status": [{"price": 120}],
                    },
                },
                {
                    "id": 12,
                    "symbol": "WATCH",
                    "company_name": "Watch Breakouts",
                    "market_region": "IN",
                    "signal_type": "HOLD",
                    "status": "WATCH",
                    "strategy": "darvas_breakout_watch",
                    "latest_price": 210,
                    "confidence": 0.55,
                    "overall_score_pct": 61,
                    "details": {
                        "opportunity_scan": {"setup": "near breakout"},
                        "entry_zone": [214, 220],
                        "stop_loss": 198,
                    },
                },
                {
                    "id": 13,
                    "symbol": "BAD",
                    "company_name": "Bad Setup",
                    "market_region": "IN",
                    "signal_type": "NO_TRADE",
                    "status": "REJECTED",
                    "strategy": "operator_risk",
                    "latest_price": 30,
                    "details": {"risk_flags": ["operator_risk"]},
                },
                {
                    "id": 14,
                    "symbol": "LOWGRADE",
                    "company_name": "Low Grade Ready Trap",
                    "market_region": "IN",
                    "signal_type": "BUY",
                    "status": "ACTIVE",
                    "strategy": "live_intraday_momentum",
                    "latest_price": 144,
                    "confidence": 0.48,
                    "overall_score_pct": 57,
                    "overall_grade": "C",
                    "details": {
                        "quality_gate": {"passed": False, "reason": "overall_score_below_70"},
                        "failed_gates": [{"gate": "overall_quality_gate"}],
                    },
                },
                {
                    "id": 15,
                    "symbol": "NOSETUP",
                    "company_name": "No Setup Industries",
                    "market_region": "IN",
                    "signal_type": "BUY",
                    "status": "ACTIVE",
                    "strategy": "no_actionable_strategy",
                    "latest_price": 88,
                    "confidence": 0.9,
                    "overall_score_pct": 91,
                    "overall_grade": "A",
                    "details": {"quality_gate": {"passed": True}},
                },
            ],
            positions=[
                {
                    "symbol": "IDEA",
                    "market_region": "IN",
                    "qty": 100,
                    "avg_price": 10,
                    "market_price": 9.7,
                    "stop_loss": 9.3,
                }
            ],
            pre_catalyst={
                "candidates": [
                    {
                        "symbol": "NEWS",
                        "market_region": "IN",
                        "name": "News Energy",
                        "score": 0.72,
                        "trigger_price": 155,
                        "reason": "Fresh order-win headline to verify before open.",
                    }
                ]
            },
        )

    def test_builds_market_scoped_tomorrow_plan_sections(self) -> None:
        plan = self._sample_plan()

        self.assertEqual(plan["market_region"], "IN")
        self.assertEqual(plan["plan_date"], "2026-05-25")
        self.assertEqual(plan["summary"]["ready_at_open"], 1)
        self.assertEqual(plan["summary"]["near_breakout"], 1)
        self.assertEqual(plan["summary"]["news_watch"], 1)
        self.assertEqual(plan["summary"]["position_actions"], 1)
        self.assertTrue(all(item["market_region"] == "IN" for item in plan["items"]))
        self.assertEqual(plan["sections"]["ready_at_open"][0]["symbol"], "READY")
        self.assertEqual(plan["sections"]["ready_at_open"][0]["section"], "ready_at_open")
        ready_symbols = {item["symbol"] for item in plan["sections"]["ready_at_open"]}
        self.assertNotIn("LOWGRADE", ready_symbols)
        self.assertNotIn("NOSETUP", ready_symbols)

    def test_db_persists_latest_plan_with_sections(self) -> None:
        plan = self._sample_plan()
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(Path(tmpdir) / "agent.db")
            db.init()
            db.upsert_tomorrow_plan(plan)
            db.set_state("tomorrow_plan_context", {})

            restored = db.latest_tomorrow_plan("IN")

        self.assertTrue(restored["enabled"])
        self.assertEqual(restored["plan_date"], "2026-05-25")
        self.assertEqual(restored["summary"]["ready_at_open"], 1)
        ready = restored["sections"]["ready_at_open"][0]
        self.assertEqual(ready["symbol"], "READY")
        self.assertEqual(ready["name"], "Ready Industries")
        self.assertEqual(ready["idea_id"], 11)


if __name__ == "__main__":
    unittest.main()
