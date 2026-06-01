from __future__ import annotations

import json
import unittest

from app.decision_diagnostics import build_cycle_decision_diagnostics
from app.models import Decision, utc_now


class DecisionDiagnosticsTests(unittest.TestCase):
    def test_cycle_diagnostics_explains_no_buy_funnel_and_stale_intraday_blocker(self) -> None:
        diagnostics = build_cycle_decision_diagnostics(
            {
                "mode": "dynamic_opportunity_scan",
                "raw_symbols": 2657,
                "quoted_symbols": 2657,
                "tradeable_screening_symbols": 900,
                "selected_symbols": 90,
                "candidate_limit": 60,
                "rejected_counts": {"below_adaptive_liquidity": 1400, "below_opportunity_score": 300},
                "setup_counts": {"top_gainer_momentum": 12},
            },
            [
                _decision(
                    "SENORES",
                    "HOLD",
                    details={
                        "score_breakdown": {"combined": 0.31},
                        "risk_gates": {
                            "decision_gate_context": {
                                "blocking_failed_gates": [{"gate": "fresh_market_data_gate"}],
                                "opportunity_probe": {
                                    "ready": True,
                                    "source": "live_momentum_review",
                                    "scan_score": 0.8506,
                                    "data_quality_override": "live_quote_ohlcv_used_for_probe",
                                },
                            }
                        },
                    },
                ),
                _decision(
                    "BHEL",
                    "HOLD",
                    technical_score=0.2,
                    details={
                        "risk_gates": {
                            "decision_gate_context": {
                                "blocking_failed_gates": [
                                    {"gate": "technical_score_gate"},
                                    {"gate": "overall_quality_gate"},
                                ]
                            }
                        }
                    },
                ),
            ],
            market_region="IN",
        )

        self.assertEqual(diagnostics["funnel"]["raw_symbols"], 2657)
        self.assertEqual(diagnostics["funnel"]["scanner_selected_symbols"], 90)
        self.assertEqual(diagnostics["funnel"]["decision_buy_rate_pct"], 0.0)
        self.assertEqual(diagnostics["top_blockers"][0]["gate"], "fresh_market_data_gate")
        self.assertEqual(diagnostics["live_quote_stale_intraday"]["only_blocker_symbols"], 1)
        self.assertIn("SENORES", diagnostics["live_quote_stale_intraday"]["sample_only_blocker_symbols"])
        self.assertIn("scanner_shortlist_too_narrow", {flag["code"] for flag in diagnostics["health_flags"]})
        self.assertIn("live_quote_blocked_by_stale_intraday_only", {flag["code"] for flag in diagnostics["health_flags"]})
        json.dumps(diagnostics)

    def test_cycle_diagnostics_surfaces_buy_decisions_not_auto_followed(self) -> None:
        diagnostics = build_cycle_decision_diagnostics(
            {"raw_symbols": 100, "quoted_symbols": 100, "selected_symbols": 40},
            [_decision("ACMESOLAR", "BUY")],
            shared_auto_trade={
                "users_checked": 2,
                "followed": 0,
                "skipped": [
                    {"symbol": "ACMESOLAR", "reason": "position_size_below_minimum_trade_economics"},
                    {"symbol": "ACMESOLAR", "reason": "position_size_below_minimum_trade_economics"},
                ],
            },
            market_region="IN",
        )

        self.assertEqual(diagnostics["funnel"]["buy_decisions"], 1)
        self.assertEqual(diagnostics["funnel"]["auto_followed"], 0)
        self.assertEqual(
            diagnostics["auto_follow"]["skip_reasons"]["position_size_below_minimum_trade_economics"],
            2,
        )
        self.assertIn("buy_decisions_not_followed", {flag["code"] for flag in diagnostics["health_flags"]})


def _decision(
    symbol: str,
    action: str,
    *,
    confidence: float = 0.62,
    technical_score: float = 0.84,
    details: dict | None = None,
) -> Decision:
    return Decision(
        symbol=symbol,
        action=action,  # type: ignore[arg-type]
        confidence=confidence,
        price=100.0,
        technical_score=technical_score,
        sentiment_score=0.1,
        reason="test decision",
        asof=utc_now(),
        strategy="test_strategy",
        details_json=json.dumps(details or {}),
    )


if __name__ == "__main__":
    unittest.main()
