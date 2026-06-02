from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.db import Database
from app.decision_contract import annotate_decision_row, normalize_trade_targets
from app.models import Decision


class DecisionContractTests(unittest.TestCase):
    def test_decision_summaries_are_ranked_by_backend_score(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(Path(tmpdir) / "agent.db")
            db.init()
            db.insert_decisions(
                [
                    Decision(
                        symbol="LOWCONFHIGHQUALITY",
                        action="BUY",
                        confidence=0.62,
                        price=100,
                        technical_score=0.4,
                        sentiment_score=0.2,
                        reason="best setup",
                        asof="2026-05-19T09:00:00+00:00",
                        details_json=json.dumps({"overall_score_pct": 88, "score_breakdown": {"combined": 0.4}}),
                    ),
                    Decision(
                        symbol="HIGHCONFLOWQUALITY",
                        action="HOLD",
                        confidence=0.99,
                        price=50,
                        technical_score=0.2,
                        sentiment_score=0.1,
                        reason="high confidence hold",
                        asof="2026-05-19T09:01:00+00:00",
                        details_json=json.dumps({"overall_score_pct": 41, "score_breakdown": {"combined": 0.1}}),
                    ),
                    Decision(
                        symbol="MIDSETUP",
                        action="BUY",
                        confidence=0.7,
                        price=75,
                        technical_score=0.3,
                        sentiment_score=0.2,
                        reason="mid setup",
                        asof="2026-05-19T09:02:00+00:00",
                        details_json=json.dumps({"score_breakdown": {"score_percent": 76}}),
                    ),
                ]
            )

            rows = db.latest_decision_summaries(3)

        self.assertEqual([row["symbol"] for row in rows], ["LOWCONFHIGHQUALITY", "MIDSETUP", "HIGHCONFLOWQUALITY"])
        self.assertEqual(rows[0]["rank_score"], 88)
        self.assertEqual(rows[0]["rank_score_source"], "overall quality")
        self.assertNotIn("details_json", rows[0])

    def test_trade_targets_are_normalized_to_sequential_t1_t2_t3(self) -> None:
        targets = normalize_trade_targets(
            [
                {"label": "T1", "price": 100},
                {"label": "T2", "price": 98, "basis": "overhead_structure"},
                {"label": "T3", "price": 97, "rr": "structure"},
            ]
        )

        self.assertEqual([target["label"] for target in targets], ["T1", "T2", "T3"])
        self.assertGreater(targets[1]["price"], targets[0]["price"])
        self.assertGreater(targets[2]["price"], targets[1]["price"])
        self.assertEqual(targets[1]["structure_reference"], 98)
        self.assertEqual(targets[2]["structure_reference"], 97)

    def test_decision_contract_uses_raw_entry_model_for_opportunity_state(self) -> None:
        audit = {
            "final_action": "BUY",
            "overall_score_pct": 88,
            "overall_grade": "A",
            "score_breakdown": {"combined": 0.25, "score_percent": 88},
            "system_gate_audit": {
                "overall_score_pct": 88,
                "overall_grade": "A",
                "hard_blocked": False,
                "hard_blocks": [],
                "active_flags": [],
            },
            "risk_gates": {
                "decision_gate_context": {
                    "raw_entry_model": {
                        "passed": True,
                        "legacy_decision_logic_removed": True,
                        "version": "entry_authority_v2",
                        "raw_score": 88,
                        "grade": "A",
                        "setup": "intraday_momentum",
                        "decision_label": "ENTRY_READY",
                        "auto_follow_ready": True,
                        "setup_family": "live_momentum",
                    }
                },
            },
            "context": {
                "raw_entry_model": {
                    "passed": True,
                    "legacy_decision_logic_removed": True,
                    "version": "entry_authority_v2",
                    "raw_score": 88,
                    "grade": "A",
                    "setup": "intraday_momentum",
                    "decision_label": "ENTRY_READY",
                    "auto_follow_ready": True,
                    "setup_family": "live_momentum",
                },
                "quote": {
                    "price": 834.35,
                    "open": 816.0,
                    "high": 839.0,
                    "low": 812.5,
                    "volume": 4_200_000,
                    "source": "upstox-live",
                },
                "data_readiness": {
                    "trade_decision_ready": True,
                    "fresh_market_data_gate": {
                        "passed": True,
                        "reason": "live_quote_ready_intraday_reference_stale",
                    },
                    "hard_gaps": [],
                    "soft_gaps": [],
                    "sources": {"quote": "upstox-live"},
                },
                "opportunity_scan": {
                    "bucket": "Actionable",
                    "setup": "intraday_momentum",
                    "score": 0.96,
                    "turnover": 350_000_000,
                    "data_quality": {
                        "actionable_data_ready": False,
                        "missing": ["stale_intraday_candles"],
                    },
                },
                "full_spectrum_analysis": {
                    "confluence_score": {"total": 6.0, "tier": "NO_SIGNAL"},
                    "trade_plan": {
                        "entry_zone": [828.0, 838.0],
                        "stop_loss": 806.0,
                        "targets": [{"price": 876.0, "distance_pct": 5.0}],
                    },
                    "risk_overrides": {"flags": []},
                    "live_momentum_review": {
                        "strategy_ready": True,
                        "setup": "intraday_momentum",
                    },
                },
            },
        }

        row = annotate_decision_row(
            {
                "id": 1,
                "symbol": "PROBE",
                "action": "BUY",
                "confidence": 0.34,
                "price": 834.35,
                "details_json": json.dumps(audit),
            }
        )

        self.assertEqual(row["opportunity_state"], "BUY_NOW")
        self.assertEqual(row["opportunity_label"], "Ready to buy")


if __name__ == "__main__":
    unittest.main()
