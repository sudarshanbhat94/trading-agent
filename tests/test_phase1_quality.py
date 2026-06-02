from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.config import CONFIG_SCHEMA, Settings, settings_from_overrides
from app.db import Database
from app.models import Decision, utc_now
from app.signal_quality import auto_follow_quality_gate, fresh_buy_quality_gate


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

    def test_fresh_buy_gate_blocks_us_etf_fund_entries(self) -> None:
        gate = fresh_buy_quality_gate(
            {
                "signal_type": "BUY",
                "status": "ACTIVE",
                "overall_score_pct": 86,
                "overall_grade": "A",
                "details": {
                    "market_region": "US",
                    "data_readiness": {"trade_decision_ready": True, "market_region": "US"},
                    "full_spectrum_analysis": {
                        "fundamental_quality": {"security_type": "ETF", "quality_bucket": "etf_reference_available"}
                    },
                },
            }
        )

        self.assertFalse(gate["passed"])
        self.assertEqual(gate["reason"], "us_etf_or_fund_watch_only")

    def test_fresh_buy_gate_caps_monthly_expiry_eve_to_probe_size(self) -> None:
        gate = fresh_buy_quality_gate(
            {
                "signal_type": "BUY",
                "status": "ACTIVE",
                "overall_score_pct": 82,
                "overall_grade": "A",
                "confluence": 22,
                "details": {
                    "data_readiness": {"trade_decision_ready": True},
                    "macro_event_context": {
                        "is_monthly_expiry_eve": True,
                        "event_risk_score": 0.35,
                        "recommended_action": "reduce_size",
                        "expiry_risk_policy": "probe_size_only",
                        "expiry_size_multiplier": 0.35,
                    },
                },
            }
        )

        self.assertTrue(gate["passed"])
        self.assertEqual(gate["reason"], "fresh_buy_quality_passed")
        self.assertLessEqual(gate["size_multiplier"], 0.35)
        self.assertIn("monthly expiry eve", " ".join(gate["risk_warnings"]))

    def test_fresh_buy_gate_reduces_size_when_sentiment_is_missing(self) -> None:
        gate = fresh_buy_quality_gate(
            {
                "signal_type": "BUY",
                "status": "ACTIVE",
                "overall_score_pct": 82,
                "overall_grade": "A",
                "confluence": 22,
                "details": {
                    "data_readiness": {
                        "trade_decision_ready": True,
                        "soft_gaps": [{"key": "sentiment_news", "label": "News/sentiment source checked"}],
                    },
                },
            }
        )

        self.assertTrue(gate["passed"])
        self.assertEqual(gate["reason"], "fresh_buy_quality_passed")
        self.assertEqual(gate["size_multiplier"], 0.5)
        self.assertIn("sentiment_news", gate["missing_data"])

    def test_fresh_buy_gate_uses_probe_size_for_reduce_size_risk_flags(self) -> None:
        gate = fresh_buy_quality_gate(
            {
                "signal_type": "BUY",
                "status": "ACTIVE",
                "overall_score_pct": 82,
                "overall_grade": "A",
                "confluence": 22,
                "details": {
                    "risk_flags": ["phase3_entry_not_fresh_from_pivot_reduce_size"],
                    "data_readiness": {"trade_decision_ready": True},
                },
            }
        )

        self.assertTrue(gate["passed"])
        self.assertEqual(gate["reason"], "fresh_buy_quality_passed")
        self.assertEqual(gate["size_multiplier"], 0.35)
        self.assertIn("risk flags active; use probe size", gate["risk_warnings"])

    def test_fresh_buy_gate_reduces_size_for_stretch_t1_and_moderately_wide_stop(self) -> None:
        stretch_gate = fresh_buy_quality_gate(
            {
                "signal_type": "BUY",
                "status": "ACTIVE",
                "overall_score_pct": 82,
                "overall_grade": "A",
                "confluence": 22,
                "latest_price": 100,
                "details": {
                    "data_readiness": {"trade_decision_ready": True},
                    "target_status": [{"label": "T1", "price": 113, "distance_pct": 13, "probability_label": "stretch"}],
                },
            }
        )
        wide_stop_gate = fresh_buy_quality_gate(
            {
                "signal_type": "BUY",
                "status": "ACTIVE",
                "overall_score_pct": 82,
                "overall_grade": "A",
                "confluence": 22,
                "latest_price": 100,
                "details": {
                    "data_readiness": {"trade_decision_ready": True},
                    "stop_loss": 93,
                    "target_status": [{"label": "T1", "price": 105, "distance_pct": 5, "probability_label": "higher"}],
                },
            }
        )

        self.assertTrue(stretch_gate["passed"])
        self.assertEqual(stretch_gate["size_multiplier"], 0.5)
        self.assertTrue(any("T1 is stretched" in item for item in stretch_gate["risk_warnings"]))
        self.assertTrue(wide_stop_gate["passed"])
        self.assertEqual(wide_stop_gate["size_multiplier"], 0.5)
        self.assertTrue(any("stop risk is 7.0%" in item for item in wide_stop_gate["risk_warnings"]))

    def test_fresh_buy_gate_blocks_only_hard_t1_and_stop_risk(self) -> None:
        far_target_gate = fresh_buy_quality_gate(
            {
                "signal_type": "BUY",
                "status": "ACTIVE",
                "overall_score_pct": 82,
                "overall_grade": "A",
                "confluence": 22,
                "latest_price": 100,
                "details": {
                    "data_readiness": {"trade_decision_ready": True},
                    "target_status": [{"label": "T1", "price": 121, "distance_pct": 21, "probability_label": "stretch"}],
                },
            }
        )
        hard_stop_gate = fresh_buy_quality_gate(
            {
                "signal_type": "BUY",
                "status": "ACTIVE",
                "overall_score_pct": 82,
                "overall_grade": "A",
                "confluence": 22,
                "latest_price": 100,
                "details": {
                    "data_readiness": {"trade_decision_ready": True},
                    "stop_loss": 90,
                    "target_status": [{"label": "T1", "price": 105, "distance_pct": 5, "probability_label": "higher"}],
                },
            }
        )

        self.assertFalse(far_target_gate["passed"])
        self.assertEqual(far_target_gate["reason"], "target_1_too_far_for_fresh_entry")
        self.assertFalse(hard_stop_gate["passed"])
        self.assertEqual(hard_stop_gate["reason"], "stop_risk_too_wide")

    def test_fresh_buy_gate_blocks_opportunity_probe_with_c_grade(self) -> None:
        gate = fresh_buy_quality_gate(
            {
                "signal_type": "BUY",
                "status": "ACTIVE",
                "overall_score_pct": 64,
                "overall_grade": "C",
                "confluence": 17,
                "details": {
                    "data_readiness": {"trade_decision_ready": True},
                    "opportunity_scan": {
                        "bucket": "Actionable",
                        "setup": "top_gainer_momentum",
                        "score": 0.84,
                        "data_quality": {"actionable_data_ready": True},
                    },
                },
            }
        )

        self.assertFalse(gate["passed"])
        self.assertEqual(gate["reason"], "overall_score_below_70")

    def test_fresh_buy_gate_blocks_wait_for_pullback_scan(self) -> None:
        gate = fresh_buy_quality_gate(
            {
                "signal_type": "BUY",
                "status": "ACTIVE",
                "action": "BUY",
                "overall_score_pct": 86,
                "overall_grade": "A",
                "confluence": 24,
                "details": {
                    "action": "BUY",
                    "data_readiness": {"trade_decision_ready": True},
                    "opportunity_scan": {
                        "bucket": "Actionable",
                        "setup": "52_week_high_volume_breakout",
                        "trade_window": "wait_for_pullback",
                        "score": 0.92,
                        "data_quality": {"actionable_data_ready": True},
                    },
                },
            }
        )

        self.assertFalse(gate["passed"])
        self.assertEqual(gate["reason"], "opportunity_scan_wait_state")

    def test_fresh_buy_gate_blocks_actionable_watch_classification(self) -> None:
        gate = fresh_buy_quality_gate(
            {
                "signal_type": "BUY",
                "status": "ACTIVE",
                "action": "BUY",
                "overall_score_pct": 86,
                "overall_grade": "A",
                "confluence": 24,
                "details": {
                    "action": "BUY",
                    "data_readiness": {"trade_decision_ready": True},
                    "opportunity_scan": {
                        "bucket": "ACTIONABLE_WATCH",
                        "label": "ACTIONABLE_WATCH",
                        "setup": "market_action_momentum",
                        "score": 0.92,
                        "data_quality": {"actionable_data_ready": True},
                    },
                },
            }
        )

        self.assertFalse(gate["passed"])
        self.assertEqual(gate["reason"], "actionable_watch")

    def test_fresh_buy_gate_blocks_low_score_top_gainers_playbook_probe(self) -> None:
        item = {
            "signal_type": "BUY",
            "status": "ACTIVE",
            "latest_price": 108.0,
            "overall_score_pct": 30,
            "overall_grade": "D",
            "confluence": 10,
            "fresh_action": "BUY_NOW",
            "details": {
                "latest_price": 108.0,
                "risk_flags": ["price_extended_from_pivot"],
                "hard_blocks": [{"flag": "PRICE_EXTENDED_FROM_PIVOT"}],
                "data_readiness": {"trade_decision_ready": True},
                "stop_loss": 100.44,
                "targets": [
                    {
                        "label": "T1",
                        "price": 129.6,
                        "distance_pct": 20.0,
                        "probability_label": "likely",
                    }
                ],
                "opportunity_scan": {
                    "bucket": "Small Size Only",
                    "setup": "earnings_beat_gap_and_go",
                    "score": 1.0,
                    "data_quality": {"actionable_data_ready": True},
                    "top_gainers_playbook": {
                        "available": True,
                        "final_signal": "MODERATE BUY",
                        "quant_score": 62,
                        "hard_excluded": False,
                        "hard_excludes": [],
                        "anti_patterns": [],
                        "cmp": 108.0,
                        "catalyst_review": {"catalyst_confirmed": True, "catalyst_strength": "MODERATE"},
                        "levels": {
                            "pivot": 105.0,
                            "entry": 108.0,
                            "max_entry": 110.25,
                            "stop": 100.44,
                            "target1": 129.6,
                        },
                    },
                },
            },
        }
        gate = fresh_buy_quality_gate(item)

        self.assertFalse(gate["passed"])
        self.assertEqual(gate["reason"], "hard_blocked")
        self.assertFalse(auto_follow_quality_gate(item)["passed"])

    def test_fresh_buy_gate_blocks_live_quote_probe_when_intraday_candles_lag(self) -> None:
        gate = fresh_buy_quality_gate(
            {
                "signal_type": "BUY",
                "status": "ACTIVE",
                "overall_score_pct": 64,
                "overall_grade": "C",
                "confluence": 17,
                "quote": {
                    "price": 108.0,
                    "open": 100.0,
                    "high": 109.0,
                    "low": 99.0,
                    "volume": 2_000_000,
                    "source": "upstox-live",
                },
                "data_readiness": {
                    "trade_decision_ready": False,
                    "hard_gaps": [
                        {"key": "in_intraday_candles", "label": "India intraday candles", "source": "upstox-live"}
                    ],
                    "soft_gaps": [],
                },
                "details": {
                    "opportunity_scan": {
                        "bucket": "Actionable",
                        "setup": "top_gainer_momentum",
                        "score": 0.84,
                        "turnover": 240_000_000,
                        "data_quality": {"actionable_data_ready": False, "missing": ["stale_intraday_candles"]},
                    },
                },
            }
        )

        self.assertFalse(gate["passed"])
        self.assertEqual(gate["reason"], "stale_market_data")

    def test_stored_signal_idea_probe_blocks_stale_intraday_marker(self) -> None:
        gate = fresh_buy_quality_gate(
            {
                "signal_type": "BUY",
                "status": "ACTIVE",
                "overall_score_pct": 70,
                "overall_grade": "B",
                "confluence": 19,
                "details": {
                    "risk_flags": [
                        "institutional_scorecard_below_entry_threshold",
                        "false_breakout_risk_no_new_longs",
                        "phase3_weak_volume_ratio_reduce_size",
                    ],
                    "data_readiness": {
                        "trade_decision_ready": True,
                        "hard_gaps": [],
                        "soft_gaps": [],
                        "sources": {"quote": "upstox-live", "daily": "upstox-live:day"},
                    },
                    "opportunity_scan": {
                        "bucket": "Actionable",
                        "setup": "52_week_high_volume_breakout",
                        "score": 0.91,
                        "turnover": 120_000_000,
                        "data_quality": {"actionable_data_ready": False, "missing": ["stale_intraday_candles"]},
                    },
                },
            }
        )

        self.assertFalse(gate["passed"])
        self.assertEqual(gate["reason"], "stale_market_data")

    def test_freshness_gate_pass_allows_live_quote_setup_with_stale_intraday_marker(self) -> None:
        gate = fresh_buy_quality_gate(
            {
                "signal_type": "BUY",
                "status": "ACTIVE",
                "overall_score_pct": 88,
                "overall_grade": "A",
                "confluence": 18,
                "quote": {
                    "price": 69.45,
                    "open": 67.0,
                    "high": 72.0,
                    "low": 66.61,
                    "volume": 47_546_371,
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
                "details": {
                    "action": "BUY",
                    "latest_price": 69.45,
                    "entry_zone": [68.5, 70.0],
                    "stop_loss": 66.0,
                    "targets": [{"price": 73.0, "distance_pct": 5.0}],
                    "opportunity_scan": {
                        "bucket": "Actionable",
                        "setup": "52_week_high_volume_breakout",
                        "score": 0.88,
                        "turnover": 3_302_095_465,
                        "data_quality": {
                            "actionable_data_ready": False,
                            "missing": ["stale_intraday_candles"],
                        },
                    },
                },
            }
        )

        self.assertTrue(gate["passed"], gate)
        self.assertTrue(gate["opportunity_probe"])

    def test_live_intraday_opportunity_probe_uses_starter_confluence_floor(self) -> None:
        gate = fresh_buy_quality_gate(
            {
                "signal_type": "BUY",
                "status": "ACTIVE",
                "overall_score_pct": 88,
                "overall_grade": "A",
                "confluence": 6,
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
                "details": {
                    "action": "BUY",
                    "latest_price": 834.35,
                    "entry_zone": [828.0, 838.0],
                    "stop_loss": 806.0,
                    "targets": [{"price": 876.0, "distance_pct": 5.0}],
                    "live_momentum_review": {
                        "strategy_ready": True,
                        "setup": "intraday_momentum",
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
                },
            }
        )

        self.assertTrue(gate["passed"], gate)
        self.assertTrue(gate["opportunity_probe"])
        self.assertEqual(gate["min_confluence"], 6.0)
        self.assertEqual(gate["size_multiplier"], 0.35)

    def test_auto_follow_reuses_opportunity_probe_risk_policy(self) -> None:
        gate = auto_follow_quality_gate(
            {
                "signal_type": "BUY",
                "status": "ACTIVE",
                "fresh_action": "BUY_NOW",
                "overall_score_pct": 100,
                "overall_grade": "A",
                "confluence": 6,
                "quote": {
                    "price": 4501.8,
                    "open": 4440.0,
                    "high": 4520.0,
                    "low": 4410.0,
                    "volume": 1_200_000,
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
                "details": {
                    "action": "BUY",
                    "latest_price": 4501.8,
                    "entry_zone": [4485.0, 4520.0],
                    "stop_loss": 4346.35,
                    "targets": [{"price": 4727.0, "distance_pct": 5.0}],
                    "risk_flags": [
                        "institutional_scorecard_below_entry_threshold",
                        "false_breakout_risk_no_new_longs",
                        "phase3_weak_volume_ratio_reduce_size",
                    ],
                    "live_momentum_review": {
                        "strategy_ready": True,
                        "setup": "intraday_momentum",
                    },
                    "opportunity_scan": {
                        "bucket": "Actionable",
                        "setup": "intraday_momentum",
                        "score": 1.0,
                        "turnover": 320_000_000,
                        "data_quality": {
                            "actionable_data_ready": False,
                            "missing": ["stale_intraday_candles"],
                        },
                    },
                },
            }
        )

        self.assertTrue(gate["passed"], gate)
        self.assertTrue(gate["opportunity_probe"])
        self.assertEqual(gate["min_confluence"], 6.0)
        self.assertEqual(gate["size_multiplier"], 0.35)

    def test_stored_opportunity_probe_min_confluence_is_reused(self) -> None:
        gate = fresh_buy_quality_gate(
            {
                "signal_type": "BUY",
                "status": "ACTIVE",
                "overall_score_pct": 82,
                "overall_grade": "B",
                "confluence": 6,
                "data_readiness": {"trade_decision_ready": True},
                "details": {
                    "action": "BUY",
                    "risk_gates": {
                        "decision_gate_context": {
                            "opportunity_probe": {
                                "ready": True,
                                "source": "live_momentum_review",
                                "setup": "intraday_momentum",
                                "scan_score": 0.96,
                                "min_confluence": 6.0,
                            }
                        }
                    },
                },
            }
        )

        self.assertTrue(gate["passed"], gate)
        self.assertTrue(gate["opportunity_probe"])
        self.assertEqual(gate["min_confluence"], 6.0)

    def test_fresh_buy_gate_rejects_probe_when_quote_is_stale(self) -> None:
        gate = fresh_buy_quality_gate(
            {
                "signal_type": "BUY",
                "status": "ACTIVE",
                "overall_score_pct": 64,
                "overall_grade": "C",
                "confluence": 17,
                "quote": {
                    "price": 108.0,
                    "open": 100.0,
                    "high": 109.0,
                    "low": 99.0,
                    "volume": 2_000_000,
                    "source": "upstox-live",
                },
                "data_readiness": {
                    "trade_decision_ready": False,
                    "hard_gaps": [{"key": "in_live_quote", "label": "India live quote"}],
                    "soft_gaps": [],
                },
                "details": {
                    "opportunity_scan": {
                        "bucket": "Actionable",
                        "setup": "top_gainer_momentum",
                        "score": 0.84,
                        "turnover": 240_000_000,
                        "data_quality": {"actionable_data_ready": False, "missing": ["stale_quote"]},
                    },
                },
            }
        )

        self.assertFalse(gate["passed"])
        self.assertEqual(gate["reason"], "stale_market_data")

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

    def test_hold_refresh_does_not_preserve_old_buy_as_actionable(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(Path(tmpdir) / "agent.db")
            db.init()
            idea_id = self._insert_signal_idea(
                db,
                signal_type="BUY",
                status="ACTIVE",
                score=84,
                grade="A",
                details_extra={"action": "BUY", "quality_gate": {"passed": True}},
            )
            with db.connect() as conn:
                conn.execute("update signal_ideas set symbol = 'OLDFAST' where id = ?", (idea_id,))
            decision = Decision(
                symbol="OLDFAST",
                strategy="phase1_test",
                action="HOLD",
                confidence=0.42,
                price=101,
                technical_score=0.55,
                sentiment_score=0.0,
                reason="latest state is no fresh buy",
                asof=utc_now(),
                details_json=json.dumps(
                    {
                        "action_reason": "latest state is no fresh buy",
                        "score_breakdown": {"combined": 0.34, "score_percent": 72},
                        "system_gate_audit": {"overall_score_pct": 72, "overall_grade": "B", "hard_blocked": False},
                        "context": {
                            "full_spectrum_analysis": {
                                "confluence_score": {"total": 20},
                                "trade_plan": {"entry_zone": [99, 102], "stop_loss": 95, "targets": []},
                                "risk_overrides": {"flags": []},
                            }
                        },
                    }
                ),
            )

            db.upsert_signal_ideas_from_decisions([decision])
            with db.connect() as conn:
                row = conn.execute("select * from signal_ideas where symbol = 'OLDFAST'").fetchone()

        self.assertEqual(row["signal_type"], "WATCH")
        self.assertEqual(row["status"], "WATCH")
        details = json.loads(row["details_json"])
        self.assertEqual(details["action"], "HOLD")
        self.assertFalse(details["quality_gate"]["passed"])

    def test_startup_demotes_non_actionable_active_buy_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(Path(tmpdir) / "agent.db")
            db.init()
            idea_id = self._insert_signal_idea(
                db,
                signal_type="BUY",
                status="ACTIVE",
                score=84,
                grade="A",
                details_extra={
                    "action": "HOLD",
                    "quality_gate": {"passed": False, "reason": "not_buy_action", "message": "Latest engine action is not BUY."},
                },
            )
            db.init()
            with db.connect() as conn:
                row = conn.execute("select * from signal_ideas where id = ?", (idea_id,)).fetchone()

        self.assertEqual(row["signal_type"], "WATCH")
        self.assertEqual(row["status"], "WATCH")
        details = json.loads(row["details_json"])
        self.assertEqual(details["quality_downgrade"]["reason"], "latest_state_not_fresh_buy")

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

    def test_safety_cleanup_holds_watch_but_exits_hard_invalidated_paper_follows(self) -> None:
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
            hard_invalid_id = self._insert_signal_idea(
                db,
                signal_type="BUY",
                status="ACTIVE",
                score=82,
                grade="A",
                details_extra={"hard_blocked": True, "hard_blocks": [{"flag": "FAILED_BREAKOUT_TWO_DAY_RULE"}]},
            )
            weak_but_active_id = self._insert_signal_idea(
                db,
                signal_type="BUY",
                status="ACTIVE",
                score=52,
                grade="D",
            )
            now = utc_now()
            with db.connect() as conn:
                for idea_id in (watch_id, hard_invalid_id, weak_but_active_id):
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

        self.assertEqual(len(exited), 1)
        self.assertEqual({item["symbol"] for item in active}, {"WATCHA", "BUYD"})
        self.assertEqual({item["status"] for item in exited}, {"EXITED"})

    def test_safety_cleanup_exits_stale_or_rejected_paper_follows(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(Path(tmpdir) / "agent.db")
            db.init()
            stale_id = self._insert_signal_idea(
                db,
                signal_type="BUY",
                status="ACTIVE",
                score=84,
                grade="A",
            )
            rejected_id = self._insert_signal_idea(
                db,
                signal_type="WATCH",
                status="REJECTED",
                score=20,
                grade="F",
            )
            current_watch_id = self._insert_signal_idea(
                db,
                signal_type="WATCH",
                status="WATCH",
                score=72,
                grade="B",
            )
            now = datetime.now(timezone.utc)
            stale_seen = (now - timedelta(hours=36)).isoformat()
            with db.connect() as conn:
                conn.execute("update signal_ideas set symbol = 'STALEPAPER', last_seen_at = ? where id = ?", (stale_seen, stale_id))
                conn.execute("update signal_ideas set symbol = 'REJECTPAPER' where id = ?", (rejected_id,))
                conn.execute("update signal_ideas set symbol = 'CURWATCH' where id = ?", (current_watch_id,))
                for idea_id in (stale_id, rejected_id, current_watch_id):
                    conn.execute(
                        """
                        insert into user_idea_follows (
                            user_id, idea_id, mode, status, qty, entry_price, latest_price,
                            invested_amount, unrealized_pnl, return_pct, created_at, updated_at, details_json
                        )
                        values (1, ?, 'PAPER', 'ACTIVE', 10, 100, 96, 1000, -40, -4, ?, ?, '{}')
                        """,
                        (idea_id, now.isoformat(), now.isoformat()),
                    )

            exited = db.exit_unsafe_active_follows(now_utc=now)
            active = [
                item
                for item in db.user_followed_signal_ideas(1, 20)
                if item["follow_status"] == "ACTIVE" and item["mode"] == "PAPER" and item["qty"] > 0
            ]

        self.assertEqual({item["symbol"] for item in exited}, {"STALEPAPER", "REJECTPAPER"})
        self.assertEqual({item["symbol"] for item in active}, {"CURWATCH"})

    def test_safety_cleanup_marks_exit_pending_after_market_close(self) -> None:
        closed_at = datetime(2026, 5, 28, 16, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(Path(tmpdir) / "agent.db")
            db.init()
            db.set_state(
                "market_session_context",
                {
                    "checked_at": closed_at.isoformat(),
                    "sessions": {
                        "IN": {
                            "is_open": False,
                            "status": "closed",
                            "reason": "outside_regular_session_or_weekend",
                            "local_time": "2026-05-28T21:30:00+05:30",
                            "next_open": "2026-05-29T09:15:00+05:30",
                        }
                    },
                },
            )
            idea_id = self._insert_signal_idea(
                db,
                signal_type="BUY",
                status="ACTIVE",
                score=84,
                grade="A",
                details_extra={"hard_blocked": True, "hard_blocks": [{"flag": "FAILED_BREAKOUT_TWO_DAY_RULE"}]},
            )
            with db.connect() as conn:
                conn.execute("update signal_ideas set latest_price = 103 where id = ?", (idea_id,))
                conn.execute(
                    """
                    insert into user_idea_follows (
                        user_id, idea_id, mode, status, qty, entry_price, latest_price,
                        invested_amount, unrealized_pnl, return_pct, created_at, updated_at, details_json
                    )
                    values (1, ?, 'PAPER', 'ACTIVE', 10, 100, 100, 1000, 0, 0, ?, ?, '{}')
                    """,
                    (idea_id, closed_at.isoformat(), closed_at.isoformat()),
                )

            exited = db.exit_unsafe_active_follows(now_utc=closed_at)
            [follow] = db.user_followed_signal_ideas(1, 10)

        self.assertEqual(exited, [])
        self.assertEqual(follow["follow_status"], "ACTIVE")
        self.assertEqual(follow["qty"], 10)
        self.assertEqual(follow["follow_details"]["safety_exit_pending"]["reason"], "market_closed_exit_pending")

    def test_safety_exit_blocks_auto_reentry_cooldown(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(Path(tmpdir) / "agent.db")
            db.init()
            idea_id = self._insert_signal_idea(
                db,
                signal_type="BUY",
                status="ACTIVE",
                score=82,
                grade="A",
                details_extra={"hard_blocked": True, "hard_blocks": [{"flag": "FAILED_BREAKOUT_TWO_DAY_RULE"}]},
            )
            now = utc_now()
            with db.connect() as conn:
                conn.execute("update signal_ideas set latest_price = 96 where id = ?", (idea_id,))
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
            reentry_block = db.recent_user_symbol_exit(1, "BUYA", cooldown_hours=48)
            [history] = db.user_follow_history(1, 10)

        self.assertEqual(len(exited), 1)
        self.assertIsNotNone(reentry_block)
        self.assertEqual(reentry_block["exit_key"], "SAFETY_EXIT")
        self.assertEqual(reentry_block["exit_reason"], "active_follow_hard_blocked")
        self.assertEqual(reentry_block["realized_pnl"], -40.0)
        self.assertEqual(history["closed_qty"], 10)
        self.assertEqual(history["exit_price"], 96.0)
        self.assertEqual(history["realized_pnl"], -40.0)
        self.assertEqual(history["exit_reason"], "active_follow_hard_blocked")

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

    def test_stale_active_buy_is_not_labeled_actionable(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(Path(tmpdir) / "agent.db")
            db.init()
            idea_id = self._insert_signal_idea(
                db,
                signal_type="BUY",
                status="ACTIVE",
                score=84,
                grade="A",
                details_extra={
                    "action": "BUY",
                    "quality_gate": {"passed": True},
                    "data_readiness": {"trade_decision_ready": True},
                },
            )
            stale = (datetime.now(timezone.utc) - timedelta(minutes=45)).isoformat()
            with db.connect() as conn:
                conn.execute("update signal_ideas set last_seen_at = ? where id = ?", (stale, idea_id))
            latest = db.latest_signal_ideas(5)[0]

        self.assertEqual(latest["display_signal"], "No Fresh Add")
        self.assertEqual(latest["fresh_action"], "NO_FRESH_ADD")
        self.assertIn("older than the fresh-entry window", latest["display_reason"])

    def test_latest_signal_ideas_keeps_one_row_per_symbol(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(Path(tmpdir) / "agent.db")
            db.init()
            first = self._insert_signal_idea(
                db,
                signal_type="WATCH",
                status="WATCH",
                score=80,
                grade="A",
            )
            second = self._insert_signal_idea(
                db,
                signal_type="WATCH",
                status="WATCH",
                score=78,
                grade="A",
            )
            with db.connect() as conn:
                conn.execute("update signal_ideas set symbol = 'DUPSYM', confidence = 0.7 where id = ?", (first,))
                conn.execute("update signal_ideas set symbol = 'DUPSYM', confidence = 0.6 where id = ?", (second,))
            latest = db.latest_signal_ideas(5)

        self.assertEqual([row["symbol"] for row in latest].count("DUPSYM"), 1)

    def test_latest_signal_ideas_prefers_current_row_for_symbol(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(Path(tmpdir) / "agent.db")
            db.init()
            old_high_score = self._insert_signal_idea(
                db,
                signal_type="BUY",
                status="ACTIVE",
                score=91,
                grade="A",
            )
            new_watch = self._insert_signal_idea(
                db,
                signal_type="WATCH",
                status="WATCH",
                score=42,
                grade="D",
            )
            now = datetime.now(timezone.utc)
            with db.connect() as conn:
                conn.execute(
                    "update signal_ideas set symbol = 'CURSYM', last_seen_at = ?, latest_decision_id = 10 where id = ?",
                    ((now - timedelta(minutes=30)).isoformat(), old_high_score),
                )
                conn.execute(
                    "update signal_ideas set symbol = 'CURSYM', last_seen_at = ?, latest_decision_id = 11 where id = ?",
                    (now.isoformat(), new_watch),
                )
            latest = db.latest_signal_ideas(5)

        self.assertEqual(len([row for row in latest if row["symbol"] == "CURSYM"]), 1)
        current = next(row for row in latest if row["symbol"] == "CURSYM")
        self.assertEqual(current["signal_type"], "WATCH")
        self.assertEqual(current["overall_grade"], "D")

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
