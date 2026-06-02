from __future__ import annotations

import json
import unittest

from app.canonical_trade import CANONICAL_TRADE_CONTRACT_VERSION
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
                "target_decision_symbols": 200,
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

    def test_cycle_diagnostics_does_not_flag_explained_economic_auto_follow_skips(self) -> None:
        diagnostics = build_cycle_decision_diagnostics(
            {"raw_symbols": 100, "quoted_symbols": 100, "selected_symbols": 40},
            [_decision("ACMESOLAR", "BUY")],
            shared_auto_trade={
                "users_checked": 2,
                "followed": 0,
                "active_buy_ideas_checked": 2,
                "skipped": [
                    {"symbol": "ACMESOLAR", "reason": "position_size_below_minimum_trade_economics"},
                    {"symbol": "ACMESOLAR", "reason": "position_size_below_minimum_trade_economics"},
                ],
            },
            market_region="IN",
        )

        self.assertEqual(diagnostics["funnel"]["buy_decisions"], 1)
        self.assertEqual(diagnostics["funnel"]["buy_symbols"], 1)
        self.assertEqual(diagnostics["funnel"]["auto_followed_user_actions"], 0)
        self.assertEqual(diagnostics["funnel"]["auto_follow_user_conversion_pct"], 0.0)
        self.assertEqual(
            diagnostics["auto_follow"]["skip_reasons"]["position_size_below_minimum_trade_economics"],
            2,
        )
        self.assertNotIn("buy_decisions_not_followed", {flag["code"] for flag in diagnostics["health_flags"]})

    def test_cycle_diagnostics_surfaces_buy_decisions_not_mapped_to_active_ideas(self) -> None:
        diagnostics = build_cycle_decision_diagnostics(
            {"raw_symbols": 100, "quoted_symbols": 100, "selected_symbols": 40},
            [_decision("ACMESOLAR", "BUY")],
            shared_auto_trade={
                "users_checked": 2,
                "followed": 0,
                "active_buy_ideas_checked": 0,
                "skipped": [{"symbol": "ACMESOLAR", "reason": "outside_custom_monitor_list"}],
            },
            market_region="IN",
        )

        self.assertIn("buy_decisions_not_followed", {flag["code"] for flag in diagnostics["health_flags"]})

    def test_cycle_diagnostics_does_not_report_multi_user_follows_as_over_100_percent(self) -> None:
        diagnostics = build_cycle_decision_diagnostics(
            {"raw_symbols": 100, "quoted_symbols": 100, "selected_symbols": 40},
            [_decision("GTES", "BUY")],
            shared_auto_trade={"users_checked": 7, "followed": 6, "skipped": []},
            market_region="US",
        )

        self.assertEqual(diagnostics["funnel"]["auto_followed_user_actions"], 6)
        self.assertEqual(diagnostics["funnel"]["auto_follow_user_conversion_pct"], 85.71)
        self.assertEqual(diagnostics["funnel"]["auto_follows_per_buy_symbol"], 6.0)

    def test_diagnostics_separates_duplicate_active_buy_monitors_from_fresh_buys(self) -> None:
        diagnostics = build_cycle_decision_diagnostics(
            {
                "mode": "dynamic_opportunity_scan",
                "raw_symbols": 2658,
                "quoted_symbols": 2658,
                "selected_symbols": 200,
                "target_decision_symbols": 200,
            },
            [
                _decision(
                    "IFCI",
                    "HOLD",
                    details={
                        "duplicate_buy_suppression": {
                            "suppressed": True,
                            "reason": "already_active_buy_cooldown",
                        },
                        "risk_gates": {
                            "decision_gate_context": {
                                "blocking_failed_gates": [],
                                "opportunity_probe": {"ready": True, "source": "live_quote_opportunity_scan"},
                            }
                        },
                    },
                )
            ]
            + [_decision(f"HOLD{i}", "HOLD", technical_score=0.1) for i in range(199)],
            market_region="IN",
            missed_move_review_row_id=47,
        )

        self.assertEqual(diagnostics["funnel"]["buy_decisions"], 0)
        self.assertEqual(diagnostics["funnel"]["buy_intent_decisions"], 1)
        self.assertEqual(diagnostics["funnel"]["duplicate_active_buy_monitors"], 1)
        codes = {flag["code"] for flag in diagnostics["health_flags"]}
        self.assertIn("all_buy_intents_already_active", codes)
        self.assertNotIn("no_buys_from_large_decision_set", codes)
        self.assertIn("already-active BUY monitors", diagnostics["summary"])

    def test_india_diagnostics_require_target_decisions_and_missed_move_row(self) -> None:
        diagnostics = build_cycle_decision_diagnostics(
            {
                "mode": "dynamic_opportunity_scan",
                "raw_symbols": 2657,
                "quoted_symbols": 2650,
                "selected_symbols": 139,
                "target_decision_symbols": 200,
                "slot_fill_counts": {"live_rally": 45, "refill": 20},
                "slot_budgets": {"live_rally": 45, "diverse": 20},
            },
            [_decision(f"SYM{index}", "HOLD") for index in range(139)],
            shared_auto_trade={"users_checked": 1, "followed": 0, "skipped": []},
            market_region="IN",
        )

        codes = {flag["code"] for flag in diagnostics["health_flags"]}
        self.assertEqual(diagnostics["funnel"]["target_decision_symbols"], 200)
        self.assertEqual(diagnostics["funnel"]["decision_target_shortfall"], 61)
        self.assertEqual(diagnostics["slot_fill_counts"]["live_rally"], 45)
        self.assertIn("nse_full_decision_target_missed", codes)
        self.assertIn("missed_move_review_not_persisted", codes)

    def test_us_diagnostics_require_target_decisions_and_missed_move_row(self) -> None:
        diagnostics = build_cycle_decision_diagnostics(
            {
                "mode": "dynamic_opportunity_scan",
                "raw_symbols": 3100,
                "quoted_symbols": 3050,
                "selected_symbols": 160,
                "target_decision_symbols": 200,
                "target_decision_symbols_by_market": {"US": 200},
                "slot_fill_counts_by_market": {"US": {"live_rally": 45, "earnings_news": 20}},
                "slot_budgets_by_market": {"US": {"live_rally": 45, "earnings_news": 30}},
            },
            [_decision(f"US{index}", "HOLD") for index in range(160)],
            shared_auto_trade={"users_checked": 1, "followed": 0, "skipped": []},
            market_region="US",
        )

        codes = {flag["code"] for flag in diagnostics["health_flags"]}
        self.assertEqual(diagnostics["funnel"]["decision_target_shortfall"], 40)
        self.assertEqual(diagnostics["target_decision_symbols_by_market"], {"US": 200})
        self.assertEqual(diagnostics["slot_fill_counts_by_market"]["US"]["earnings_news"], 20)
        self.assertIn("us_full_decision_target_missed", codes)
        self.assertIn("missed_move_review_not_persisted", codes)

    def test_diagnostics_separates_user_paper_follows_from_central_orders(self) -> None:
        diagnostics = build_cycle_decision_diagnostics(
            {"raw_symbols": 2500, "quoted_symbols": 2500, "selected_symbols": 200, "target_decision_symbols": 200},
            [_decision("RELIANCE", "BUY")],
            shared_auto_trade={"users_checked": 1, "followed": 1, "skipped": []},
            executed_orders=0,
            market_region="IN",
            missed_move_review_row_id=12,
        )

        self.assertEqual(diagnostics["funnel"]["paper_trade_source"], "user_idea_follows")
        self.assertEqual(diagnostics["funnel"]["paper_followed_user_actions"], 1)
        self.assertEqual(diagnostics["funnel"]["central_broker_orders"], 0)

    def test_diagnostics_reports_canonical_trade_blockers(self) -> None:
        diagnostics = build_cycle_decision_diagnostics(
            {"raw_symbols": 2500, "quoted_symbols": 2500, "selected_symbols": 200, "target_decision_symbols": 200},
            [
                _decision(
                    "STALELIVE",
                    "HOLD",
                    details={
                        "risk_gates": {
                            "decision_gate_context": {
                                "blocking_failed_gates": [{"gate": "canonical_trade_contract"}],
                                "canonical_trade_gate": {
                                    "passed": False,
                                    "canonical_version": CANONICAL_TRADE_CONTRACT_VERSION,
                                    "primary_blocker": "stale_market_data",
                                    "reason": "stale_market_data",
                                    "secondary_blockers": ["in_intraday_candles"],
                                },
                                "opportunity_probe": {
                                    "ready": True,
                                    "source": "live_quote_opportunity_scan",
                                    "data_quality_override": "live_quote_ohlcv_used_for_probe",
                                },
                            }
                        }
                    },
                )
            ],
            market_region="IN",
            missed_move_review_row_id=99,
        )

        self.assertEqual(diagnostics["canonical_trade"]["gate_seen"], 1)
        self.assertEqual(diagnostics["canonical_trade"]["gate_blocked"], 1)
        self.assertEqual(diagnostics["canonical_trade"]["version_counts"][CANONICAL_TRADE_CONTRACT_VERSION], 1)
        self.assertEqual(diagnostics["canonical_trade"]["primary_blocker_counts"]["stale_market_data"], 1)
        self.assertEqual(diagnostics["top_hold_candidates"][0]["canonical_trade"]["primary_blocker"], "stale_market_data")
        self.assertEqual(diagnostics["live_quote_stale_intraday"]["only_blocker_symbols"], 1)


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
