from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.db import Database
from app.decision_contract import normalize_trade_targets
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


if __name__ == "__main__":
    unittest.main()
