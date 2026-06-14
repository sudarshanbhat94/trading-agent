from __future__ import annotations

import json
import unittest

from app.market_day_regime import REGIME_BROAD_RALLY
from scripts.backtest_executable_trade_contract import (
    _count_by_filtered,
    _has_usable_regime,
    _infer_replay_regimes,
    _paper_probe_blocker_counts_by,
    _suppression_classification_details,
)


class ExecutableReplayCalibrationTests(unittest.TestCase):
    def test_replay_regime_inference_requires_enough_context_rows(self) -> None:
        rows = [
            {
                "ts": "2026-06-01T14:35:00+00:00",
                "symbol": f"SYM{i}",
                "details_json": json.dumps(
                    {
                        "context": {
                            "market_region": "US",
                            "opportunity_scan": {
                                "day_gain_pct": 2.2,
                                "day_range_position": 0.82,
                                "volume_ratio": 2.1,
                            },
                        }
                    }
                ),
            }
            for i in range(10)
        ]

        self.assertEqual(_infer_replay_regimes(rows, {}), {})

    def test_replay_regime_inference_marks_broad_rally_without_live_gate_mutation(self) -> None:
        rows = [
            {
                "ts": "2026-06-01T14:35:00+00:00",
                "symbol": f"SYM{i}",
                "details_json": json.dumps(
                    {
                        "context": {
                            "market_region": "US",
                            "opportunity_scan": {
                                "day_gain_pct": 2.4 if i < 18 else -0.2,
                                "day_range_position": 0.84 if i < 18 else 0.48,
                                "day_high_distance_pct": 0.6,
                                "volume_ratio": 2.6,
                            },
                        }
                    }
                ),
            }
            for i in range(24)
        ]

        regimes = _infer_replay_regimes(rows, {})
        [regime] = list(regimes.values())

        self.assertEqual(regime["state"], REGIME_BROAD_RALLY)
        self.assertTrue(regime["momentum_allowed"])
        self.assertTrue(regime["replay_inferred"])

    def test_no_live_regime_is_not_usable_for_live_gate(self) -> None:
        self.assertFalse(
            _has_usable_regime(
                {
                    "market_day_regime": {
                        "state": "no_live_regime",
                        "checked_symbols": 0,
                    }
                }
            )
        )

    def test_paper_probe_blocker_counts_group_by_market(self) -> None:
        rows = [
            {"market": "US", "paper_probe_blockers": ["paper_probe_score_below_minimum", "paper_probe_no_live_regime"]},
            {"market": "US", "paper_probe_blockers": ["paper_probe_score_below_minimum"]},
            {"market": "IN", "paper_probe_blockers": ["paper_probe_us_only"]},
        ]

        self.assertEqual(
            _paper_probe_blocker_counts_by(rows, "market"),
            {
                "IN": {"paper_probe_us_only": 1},
                "US": {"paper_probe_score_below_minimum": 2, "paper_probe_no_live_regime": 1},
            },
        )

    def test_filtered_counts_surface_false_negative_blocker_table(self) -> None:
        rows = [
            {"primary_blocker": "technical_score_below_0_50", "suppression_classification": "candidate_false_negative"},
            {"primary_blocker": "technical_score_below_0_50", "suppression_classification": "candidate_false_negative"},
            {"primary_blocker": "watch_only_risk_flags_present", "suppression_classification": "context_dependent_blocked"},
            {"primary_blocker": "severe_risk_flags_present", "suppression_classification": "correctly_blocked"},
        ]

        self.assertEqual(
            _count_by_filtered(rows, "primary_blocker", "suppression_classification", "candidate_false_negative"),
            {"technical_score_below_0_50": 2},
        )

    def test_context_only_probe_blockers_are_not_counted_as_hard_false_negative(self) -> None:
        classification, reason = _suppression_classification_details(
            "watch_only_risk_flags_present",
            {
                "paper_probe_blockers": [
                    "paper_probe_requires_realtime_us_quote",
                    "paper_probe_no_live_regime",
                    "paper_probe_regime_momentum_not_allowed",
                ],
            },
            {"exit_reason": "target", "net_pct": 2.4},
        )

        self.assertEqual(classification, "context_dependent_blocked")
        self.assertIn("needs_live_context", reason)

    def test_no_future_rows_remain_ambiguous_even_with_negative_cost(self) -> None:
        classification, reason = _suppression_classification_details(
            "watch_only_risk_flags_present",
            {},
            {"exit_reason": "no_future_candles", "net_pct": -0.32},
        )

        self.assertEqual(classification, "ambiguous")
        self.assertEqual(reason, "future_outcome_missing")


if __name__ == "__main__":
    unittest.main()
