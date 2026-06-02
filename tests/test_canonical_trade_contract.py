from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.canonical_trade import CANONICAL_TRADE_CONTRACT_VERSION, canonical_trade_contract
from app.db import Database
from app.models import Decision, utc_now
from app.signal_quality import auto_follow_quality_gate


class CanonicalTradeContractTests(unittest.TestCase):
    def test_india_live_probe_uses_one_contract_for_entry_state_and_follow(self) -> None:
        contract = canonical_trade_contract(
            {
                "symbol": "TATAELXSI",
                "action": "BUY",
                "signal_type": "BUY",
                "status": "ACTIVE",
                "last_seen_at": utc_now(),
                "overall_score_pct": 88,
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
                    "market_region": "IN",
                    "trade_decision_ready": False,
                    "hard_gaps": [{"key": "in_intraday_candles", "label": "India intraday candles"}],
                    "soft_gaps": [],
                    "sources": {"quote": "upstox-live"},
                },
                "details": {
                    "action": "BUY",
                    "entry_zone": [4485.0, 4520.0],
                    "stop_loss": 4346.35,
                    "targets": [{"price": 4727.0, "distance_pct": 5.0}],
                    "risk_gates": {
                        "decision_gate_context": {
                            "opportunity_probe": {
                                "ready": True,
                                "source": "live_momentum_review",
                                "setup": "intraday_momentum",
                                "scan_score": 0.96,
                                "min_confluence": 6.0,
                                "data_quality_override": "live_momentum_review_with_trade_ready_data",
                            }
                        }
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

        self.assertEqual(contract["version"], CANONICAL_TRADE_CONTRACT_VERSION)
        self.assertTrue(contract["quality_gate"]["passed"], contract)
        self.assertEqual(contract["quality_gate"]["min_confluence"], 6.0)
        self.assertEqual(contract["fresh_action"], "BUY_NOW")
        self.assertEqual(contract["trade_state"], "ACTIONABLE")
        self.assertEqual(contract["setup_bucket"]["bucket"], "SMALL_SIZE_ONLY")
        self.assertTrue(contract["paper_follow_eligible"], contract)
        self.assertIsNone(contract["primary_blocker"])

    def test_us_yahoo_reference_playbook_is_reduced_size_not_full_size(self) -> None:
        contract = canonical_trade_contract(
            {
                "symbol": "PLAYYHOO",
                "action": "BUY",
                "signal_type": "BUY",
                "status": "ACTIVE",
                "last_seen_at": utc_now(),
                "latest_price": 108.0,
                "overall_score_pct": 82,
                "overall_grade": "A",
                "confluence": 18,
                "details": {
                    "action": "BUY",
                    "market_region": "US",
                    "quote": {"price": 108.0, "source": "yahoo-delayed"},
                    "data_readiness": {
                        "market_region": "US",
                        "trade_decision_ready": False,
                        "hard_gaps": [
                            {"key": "us_realtime_quote", "label": "US consolidated real-time quote"},
                            {"key": "us_minute_bars", "label": "US minute bars"},
                        ],
                        "sources": {"quote": "yahoo-delayed", "daily": "yahoo-delayed"},
                    },
                    "opportunity_scan": {
                        "setup": "earnings_beat_gap_and_go",
                        "top_gainers_playbook": {
                            "available": True,
                            "market_region": "US",
                            "final_signal": "STRONG BUY",
                            "quant_score": 74,
                            "hard_excluded": False,
                            "hard_excludes": [],
                            "anti_patterns": [],
                            "cmp": 108.0,
                            "weinstein": {"stage": "Stage 2"},
                            "levels": {"entry": 108.0, "max_entry": 110.25, "stop": 100.44},
                            "catalyst_review": {"catalyst_confirmed": True, "catalyst_strength": "STRONG"},
                        },
                    },
                    "targets": [{"label": "T1", "distance_pct": 12.0, "probability": "likely"}],
                    "stop_status": {"price": 100.44},
                },
            }
        )

        self.assertTrue(contract["quality_gate"]["passed"], contract)
        self.assertEqual(contract["market_region"], "US")
        self.assertLessEqual(contract["quality_gate"]["size_multiplier"], 0.35)
        self.assertEqual(contract["setup_bucket"]["bucket"], "SMALL_SIZE_ONLY")

    def test_blocked_contract_has_one_primary_blocker_and_secondary_diagnostics(self) -> None:
        contract = canonical_trade_contract(
            {
                "symbol": "STALE",
                "action": "BUY",
                "signal_type": "BUY",
                "status": "ACTIVE",
                "overall_score_pct": 88,
                "overall_grade": "A",
                "confluence": 20,
                "data_readiness": {
                    "trade_decision_ready": False,
                    "hard_gaps": [{"key": "in_live_quote", "label": "India live quote"}],
                    "soft_gaps": [],
                },
                "details": {
                    "action": "BUY",
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

        self.assertFalse(contract["quality_gate"]["passed"])
        self.assertEqual(contract["primary_blocker"], "stale_market_data")
        self.assertEqual(contract["quality_gate"]["primary_blocker"], "stale_market_data")
        self.assertIn("in_live_quote", contract["secondary_blockers"])
        self.assertNotIn("stale_market_data", contract["secondary_blockers"])

    def test_database_decoration_exposes_canonical_contract_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(Path(tmpdir) / "agent.db")
            db.init()
            now = utc_now()
            details = {
                "action": "BUY",
                "overall_score_pct": 86,
                "overall_grade": "A",
                "confluence": 22,
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
                    values (?, ?, 'CANON', 'contract_test', 'contract_test', 'BUY', 'ACTIVE',
                        100, 100, 0, 0, 0, 0.8, 0.4, 22, 86, 'A',
                        null, null, 'contract test idea', ?)
                    """,
                    (now, now, json.dumps(details)),
                )

            row = db.latest_signal_ideas(1)[0]

        self.assertEqual(row["canonical_trade"]["version"], CANONICAL_TRADE_CONTRACT_VERSION)
        self.assertEqual(row["fresh_action"], "BUY_NOW")
        self.assertEqual(row["setup_bucket"], "ACTIONABLE")
        self.assertTrue(row["paper_follow_eligible"])

    def test_canonical_passed_hold_decision_persists_as_buy_idea(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(Path(tmpdir) / "agent.db")
            db.init()
            decision = Decision(
                symbol="CANONBUY",
                action="HOLD",
                confidence=0.72,
                price=100,
                technical_score=0.7,
                sentiment_score=0.1,
                reason="canonical contract passed",
                asof=utc_now(),
                strategy="canonical_contract",
                details_json=json.dumps(
                    {
                        "score_breakdown": {"combined": 0.24, "score_percent": 88},
                        "system_gate_audit": {"overall_score_pct": 88, "overall_grade": "A", "hard_blocked": False},
                        "risk_gates": {
                            "decision_gate_context": {
                                "canonical_trade_gate": {
                                    "passed": True,
                                    "canonical_version": CANONICAL_TRADE_CONTRACT_VERSION,
                                    "primary_blocker": None,
                                    "reason": "fresh_buy_quality_passed",
                                    "size_multiplier": 0.35,
                                }
                            }
                        },
                        "context": {
                            "data_readiness": {"trade_decision_ready": True},
                            "full_spectrum_analysis": {
                                "confluence_score": {"total": 18},
                                "trade_plan": {"entry_zone": [99, 101], "stop_loss": 95, "targets": [{"price": 105}]},
                                "risk_overrides": {"flags": []},
                            },
                        },
                    }
                ),
            )

            db.insert_decisions([decision])
            db.upsert_signal_ideas_from_decisions([decision])
            row = db.latest_signal_ideas(1)[0]

        self.assertEqual(row["symbol"], "CANONBUY")
        self.assertEqual(row["signal_type"], "BUY")
        self.assertEqual(row["status"], "ACTIVE")
        self.assertEqual(row["fresh_action"], "BUY_NOW")

    def test_duplicate_suppressed_canonical_refresh_preserves_active_buy_idea(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(Path(tmpdir) / "agent.db")
            db.init()
            now = utc_now()
            details = {
                "action": "BUY",
                "overall_score_pct": 86,
                "overall_grade": "A",
                "confluence": 22,
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
                    values (?, ?, 'DUPEBUY', 'canonical_contract', 'contract_test', 'BUY', 'ACTIVE',
                        100, 100, 0, 0, 0, 0.8, 0.4, 22, 86, 'A',
                        null, null, 'original buy', ?)
                    """,
                    (now, now, json.dumps(details)),
                )
            decision = Decision(
                symbol="DUPEBUY",
                action="HOLD",
                confidence=0.5,
                price=101,
                technical_score=0.7,
                sentiment_score=0.1,
                reason="Already active; repeated BUY is position monitoring, not a new entry.",
                asof=now,
                strategy="canonical_contract",
                details_json=json.dumps(
                    {
                        "action_reason": "Already active; repeated BUY is position monitoring, not a new entry.",
                        "duplicate_buy_suppression": {"suppressed": True, "reason": "already_active_buy_cooldown"},
                        "score_breakdown": {"combined": 0.24, "score_percent": 88},
                        "system_gate_audit": {"overall_score_pct": 88, "overall_grade": "A", "hard_blocked": False},
                        "risk_gates": {
                            "decision_gate_context": {
                                "canonical_trade_gate": {
                                    "passed": True,
                                    "canonical_version": CANONICAL_TRADE_CONTRACT_VERSION,
                                    "primary_blocker": None,
                                    "reason": "fresh_buy_quality_passed",
                                }
                            }
                        },
                        "context": {
                            "data_readiness": {"trade_decision_ready": True},
                            "full_spectrum_analysis": {
                                "confluence_score": {"total": 18},
                                "trade_plan": {"entry_zone": [99, 101], "stop_loss": 95, "targets": [{"price": 105}]},
                                "risk_overrides": {"flags": []},
                            },
                        },
                    }
                ),
            )

            db.insert_decisions([decision])
            db.upsert_signal_ideas_from_decisions([decision])
            row = db.latest_signal_ideas(1)[0]

        self.assertEqual(row["symbol"], "DUPEBUY")
        self.assertEqual(row["signal_type"], "BUY")
        self.assertEqual(row["status"], "ACTIVE")
        self.assertEqual(row["fresh_action"], "NO_FRESH_ADD")
        self.assertIn("Already active", row["display_reason"])

    def test_unfollowed_active_buy_monitor_can_still_auto_follow(self) -> None:
        gate = auto_follow_quality_gate(
            {
                "symbol": "MONITORBUY",
                "signal_type": "BUY",
                "status": "ACTIVE",
                "fresh_action": "NO_FRESH_ADD",
                "last_seen_at": utc_now(),
                "overall_score_pct": 86,
                "overall_grade": "A",
                "confluence": 22,
                "details": {
                    "action": "BUY",
                    "data_readiness": {"trade_decision_ready": True},
                    "signal_continuity": {
                        "duplicate_active_buy": True,
                        "already_active_buy": True,
                    },
                },
            }
        )

        self.assertTrue(gate["passed"], gate)
        self.assertTrue(gate["active_monitor_follow_allowed"])


if __name__ == "__main__":
    unittest.main()
