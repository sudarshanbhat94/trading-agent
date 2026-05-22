from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from app.decision_contract import current_decision_rows
from app.db import Database, _compact_decision_details, _paper_exit_action
from app.full_spectrum import _strategy_confirmed_entry_quality
from app.models import Candle, Decision, Quote, utc_now
from app.opportunity_scanner import OpportunityScanner
from app.opportunity_state import opportunity_state_from_signal_details
from app.signal_quality import auto_follow_quality_gate, fresh_buy_quality_gate
from app.strategy import StrategyEngine, _performance_feedback_block
from app.strategy_presets import choose_best_strategy, evaluate_strategy_presets


class StrategySafetyTests(unittest.TestCase):
    def test_current_decision_rows_keeps_latest_per_symbol_before_ranking(self) -> None:
        rows = current_decision_rows(
            [
                {"id": 1, "ts": "2026-05-20T01:00:00+00:00", "symbol": "XOM", "action": "BUY", "confidence": 0.96},
                {"id": 2, "ts": "2026-05-20T01:04:00+00:00", "symbol": "XOM", "action": "HOLD", "confidence": 0.4},
                {"id": 3, "ts": "2026-05-20T01:02:00+00:00", "symbol": "COST", "action": "BUY", "confidence": 0.8},
            ]
        )

        by_symbol = {row["symbol"]: row for row in rows}
        self.assertEqual(len(by_symbol), 2)
        self.assertEqual(by_symbol["XOM"]["id"], 2)

    def test_broad_momentum_without_volume_stays_watch_not_actionable(self) -> None:
        candles = _trend_candles(volume_spike=False)
        signals = evaluate_strategy_presets(candles, candles[-1].close)
        broad = {
            signal.name: signal
            for signal in signals
            if signal.name
            in {
                "normalized_momentum_factor",
                "time_series_momentum_trend",
                "fifty_two_week_high_momentum",
                "minervini_trend_template",
                "donchian_momentum_breakout",
            }
        }

        self.assertTrue(broad)
        self.assertTrue(all(signal.direction == "HOLD" for signal in broad.values()))
        self.assertEqual(choose_best_strategy(signals).name, "no_actionable_strategy")

    def test_volume_confirmed_breakout_can_still_be_actionable(self) -> None:
        candles = _trend_candles(volume_spike=True)
        signals = evaluate_strategy_presets(candles, candles[-1].close)

        self.assertTrue(any(signal.direction == "BUY" for signal in signals))

    def test_strategy_confirmed_entry_can_upgrade_below_pivot_watch_grade(self) -> None:
        upgraded = _strategy_confirmed_entry_quality(
            {
                "entry_grade": "WATCH",
                "distance_from_pivot_pct": -1.65,
                "volume_confirmation": False,
                "quality_score": 0.0,
            },
            [
                {
                    "name": "minervini_trend_template",
                    "direction": "BUY",
                    "score": 1.0,
                    "metadata": {"fresh_entry_confirmed": True, "volume_ratio_20": 1.27},
                }
            ],
        )

        self.assertEqual(upgraded["entry_grade"], "B")
        self.assertEqual(upgraded["setup_type"], "strategy_confirmed_entry")
        self.assertTrue(upgraded["volume_confirmation"])

    def test_us_yahoo_reference_momentum_can_emit_buy_without_institutional_flow(self) -> None:
        engine = StrategyEngine(SimpleNamespace(max_position_pct=0.1), SimpleNamespace(), SimpleNamespace())
        context = {
            "symbol": "DDOG",
            "market_region": "US",
            "sector": "Technology",
            "industry": "Software",
            "quote": {"price": 142.0, "source": "yahoo-delayed", "asof": "2026-05-22T14:15:00+00:00"},
            "sentiment": {},
            "position": {"qty": 0},
            "data_readiness": {
                "market_region": "US",
                "trade_decision_ready": True,
                "grade": "B",
                "sources": {"quote": "yahoo-delayed", "daily": "yahoo-delayed"},
            },
            "risk_limits": {"portfolio_equity": 100_000},
            "full_spectrum_analysis": {
                "confluence_score": {"total": 18, "tier": "HIGH_CONVICTION"},
                "risk_overrides": {"flags": [], "no_new_longs": False},
                "institutional_scorecard": {
                    "total_score": 56,
                    "buy_ready": True,
                    "us_reference_momentum_ready": True,
                    "must_pass_failed": [],
                    "hard_veto": {"failed": []},
                },
                "stage_analysis": {"stage": "Stage2_Markup", "buy_permitted": True},
                "entry_quality": {"entry_grade": "B", "setup_type": "pullback_buy_zone", "distance_from_pivot_pct": -1.4},
                "breakout_quality": {"breakout_quality": "not_breakout", "two_day_rule_failed": False},
                "strategy_logic_filters": {
                    "passed": True,
                    "hard_blocks": [],
                    "penalties": [{"flag": "US_REFERENCE_PRICE_VOLUME_ONLY", "score_penalty": 0.0, "size_multiplier": 0.5}],
                    "sizing": {"max_multiplier": 0.5},
                    "institutional_sponsorship": {"supported": False, "evidence": []},
                    "breakout_volume": {"suspect_without_volume": False},
                },
                "price_volume_divergence": {"climax_volume_top": False},
                "trend_context": {"timeframe_alignment": {"alignment_grade": "B"}},
                "options_oi": {},
                "sector_rotation": {},
                "delivery_accumulation": {
                    "market_region": "US",
                    "source": "us_price_volume_proxy_no_delivery_data",
                    "data_gap": "delivery_not_applicable_us_equities",
                    "net_bias": "neutral",
                    "bias": "neutral",
                    "delivery_score": 0.0,
                },
                "fundamental_quality": {
                    "quality_bucket": "reference_ratios_available",
                    "metrics": {"reference_data_available": True, "market_cap": 55_000_000_000},
                },
                "liquidity_profile": {"liquidity_tier": "strong", "tradeable": True},
                "indicator_suite": {"atr_pct": 2.4},
                "trade_plan": {"entry_zone": [141.0, 143.0], "stop_loss": 136.0, "targets": [{"price": 152.0}]},
            },
        }

        action = engine._action_from_context("DDOG", 0.31, {}, context, {})

        self.assertEqual(action, "BUY")
        self.assertGreaterEqual(context["system_gate_audit"]["overall_score_pct"], 70)
        self.assertEqual(context["decision_gate_context"]["failed_gates"], [])

    def test_fresh_buy_requires_data_readiness(self) -> None:
        gate = fresh_buy_quality_gate(
            {
                "signal_type": "BUY",
                "status": "ACTIVE",
                "overall_score_pct": 88,
                "overall_grade": "A",
                "confluence": 22,
            }
        )

        self.assertFalse(gate["passed"])
        self.assertEqual(gate["reason"], "data_readiness_missing")

    def test_auto_follow_requires_actionable_fresh_state(self) -> None:
        gate = auto_follow_quality_gate(
            {
                "signal_type": "BUY",
                "status": "ACTIVE",
                "fresh_action": "WATCH",
                "overall_score_pct": 88,
                "overall_grade": "A",
                "confluence": 22,
                "details": {"data_readiness": {"trade_decision_ready": True}},
            }
        )

        self.assertFalse(gate["passed"])
        self.assertEqual(gate["reason"], "not_actionable_fresh_state")

    def test_repeated_active_buy_decision_is_suppressed_to_monitor(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(Path(tmpdir) / "agent.db")
            db.init()
            now = utc_now()
            with db.connect() as conn:
                conn.execute(
                    """
                    insert into signal_ideas (
                        first_seen_at, last_seen_at, symbol, strategy, plan_code, signal_type, status,
                        entry_price, latest_price, current_return_pct, peak_return_pct, worst_return_pct,
                        confidence, combined_score, confluence, overall_score_pct, overall_grade,
                        reason, details_json
                    )
                    values (?, ?, 'ACTIVEBUY', 'normalized_momentum_factor', 'institutional_quality_swing',
                        'BUY', 'ACTIVE', 100, 101, 1, 1, 0, 0.8, 0.4, 22, 82, 'A',
                        'original buy', ?)
                    """,
                    (
                        now,
                        now,
                        json.dumps(
                            {
                                "action": "BUY",
                                "overall_score_pct": 82,
                                "overall_grade": "A",
                                "data_readiness": {"trade_decision_ready": True},
                            }
                        ),
                    ),
                )
            decision = Decision(
                symbol="ACTIVEBUY",
                action="BUY",
                confidence=0.91,
                price=102,
                technical_score=0.8,
                sentiment_score=0.2,
                reason="repeat buy",
                asof=now,
                strategy="normalized_momentum_factor",
                details_json=json.dumps({"score_breakdown": {"combined": 0.5}}),
            )

            [suppressed] = db.suppress_repeated_buy_decisions([decision])

        self.assertEqual(suppressed.action, "HOLD")
        self.assertIn("Already active", suppressed.reason)
        audit = json.loads(suppressed.details_json)
        self.assertTrue(audit["duplicate_buy_suppression"]["suppressed"])

    def test_recent_buy_decision_suppresses_repeat_even_without_active_idea(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(Path(tmpdir) / "agent.db")
            db.init()
            first = Decision(
                symbol="REPEATBUY",
                action="BUY",
                confidence=0.91,
                price=100,
                technical_score=0.8,
                sentiment_score=0.2,
                reason="first buy",
                asof=utc_now(),
                strategy="normalized_momentum_factor",
                details_json=json.dumps({"score_breakdown": {"combined": 0.5}}),
            )
            db.insert_decisions([first])
            second = Decision(
                symbol="REPEATBUY",
                action="BUY",
                confidence=0.94,
                price=101,
                technical_score=0.82,
                sentiment_score=0.22,
                reason="repeat buy",
                asof=utc_now(),
                strategy="normalized_momentum_factor",
                details_json=json.dumps({"score_breakdown": {"combined": 0.55}}),
            )

            [suppressed] = db.suppress_repeated_buy_decisions([second])

        self.assertEqual(suppressed.action, "HOLD")
        self.assertIn("Already active", suppressed.reason)

    def test_cleanup_downgrades_non_tradeable_active_buy_to_watch(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(Path(tmpdir) / "agent.db")
            db.init()
            now = utc_now()
            with db.connect() as conn:
                conn.execute(
                    """
                    insert into signal_ideas (
                        first_seen_at, last_seen_at, symbol, strategy, plan_code, signal_type, status,
                        entry_price, latest_price, current_return_pct, peak_return_pct, worst_return_pct,
                        confidence, combined_score, confluence, overall_score_pct, overall_grade,
                        reason, details_json
                    )
                    values (?, ?, 'STALEBUY', 'normalized_momentum_factor', 'institutional_quality_swing',
                        'BUY', 'ACTIVE', 100, 100, 0, 0, 0, 0.8, 0.4, 22, 88, 'A',
                        'missing phase2 readiness', ?)
                    """,
                    (
                        now,
                        now,
                        json.dumps(
                            {
                                "action": "BUY",
                                "overall_score_pct": 88,
                                "overall_grade": "A",
                                "confluence": 22,
                            }
                        ),
                    ),
                )

            downgraded = db.downgrade_non_tradeable_buy_ideas()
            with db.connect() as conn:
                row = conn.execute("select * from signal_ideas where symbol = 'STALEBUY'").fetchone()

        self.assertEqual(len(downgraded), 1)
        self.assertEqual(row["signal_type"], "WATCH")
        self.assertEqual(row["status"], "WATCH")
        details = json.loads(row["details_json"])
        self.assertEqual(details["quality_downgrade"]["reason"], "data_readiness_missing")

    def test_many_low_confidence_headlines_do_not_create_news_catalyst(self) -> None:
        scanner = OpportunityScanner(_scanner_settings())

        sentiment = scanner._sentiment_metrics(
            {
                "score": 0.55,
                "confidence": 0.17,
                "headline_count": 12,
                "headlines": [f"generic headline {index}" for index in range(12)],
                "events": [
                    {
                        "event_type": "neutral",
                        "score": 0.55,
                        "confidence": 0.17,
                        "source_weight": 0.55,
                    }
                ],
            }
        )

        self.assertFalse(sentiment["positive_catalyst"])
        self.assertLess(sentiment["boost"], 0.012)

    def test_high_confidence_verified_event_can_create_news_catalyst(self) -> None:
        scanner = OpportunityScanner(_scanner_settings())

        sentiment = scanner._sentiment_metrics(
            {
                "score": 0.42,
                "confidence": 0.58,
                "headline_count": 2,
                "events": [
                    {
                        "event_type": "order_win",
                        "score": 0.42,
                        "confidence": 0.62,
                        "source_weight": 0.85,
                    }
                ],
            }
        )

        self.assertTrue(sentiment["positive_catalyst"])
        self.assertGreater(sentiment["boost"], 0.02)

    def test_opportunity_scan_reports_news_coverage_and_verified_catalysts(self) -> None:
        scanner = OpportunityScanner(_scanner_settings())
        candles = _trend_candles(volume_spike=True)
        result = scanner.rank(
            [{"symbol": "NEWSWIN", "exchange": "NSE", "sector": "Industrials"}],
            {
                "NEWSWIN": Quote(
                    symbol="NEWSWIN",
                    price=120,
                    source="upstox-live",
                    asof=utc_now(),
                    high=122,
                    low=116,
                    volume=1_200_000,
                )
            },
            {"NEWSWIN": {"daily": candles, "analysis": candles}},
            sentiment_by_symbol={
                "NEWSWIN": {
                    "score": 0.42,
                    "confidence": 0.58,
                    "headline_count": 2,
                    "headlines": ["NEWSWIN wins a large order"],
                    "events": [
                        {
                            "event_type": "order_win",
                            "score": 0.42,
                            "confidence": 0.62,
                            "source_weight": 0.85,
                        }
                    ],
                }
            },
        )

        self.assertEqual(result.summary["news_covered_candidates"], 1)
        self.assertEqual(result.summary["verified_catalyst_candidates"], 1)
        self.assertEqual(result.summary["positive_news_candidates"], 1)

    def test_llm_shortlist_prioritizes_opportunity_scan_rank(self) -> None:
        engine = StrategyEngine.__new__(StrategyEngine)
        engine.settings = SimpleNamespace(
            llm_max_symbols_per_cycle=3,
            dynamic_scan_candidate_limit=5,
            llm_event_triggered_cycles=False,
        )

        def item(symbol: str, rank: int, combined: float) -> dict[str, object]:
            return {
                "symbol": symbol,
                "action": "HOLD",
                "combined": combined,
                "sentiment_score": 0.0,
                "technical": SimpleNamespace(score=combined),
                "context": {"best_strategy": {"score": combined}, "position": {}},
                "row": {
                    "_opportunity_rank": rank,
                    "_opportunity_scan": {
                        "rank": rank,
                        "score": 0.95,
                        "bucket": "Actionable",
                        "setup": "breakout_continuation",
                        "data_quality": {"actionable_data_ready": True},
                    },
                },
            }

        ranked = sorted(
            [
                item("SCAN1", 1, 0.10),
                item("SCAN2", 2, 0.20),
                item("SCAN3", 3, 0.30),
                item("LOWERRANKHIGHCOMBINED", 5, 0.95),
            ],
            key=engine._scan_priority,
            reverse=True,
        )

        self.assertEqual([row["symbol"] for row in ranked[:3]], ["SCAN1", "SCAN2", "SCAN3"])
        self.assertEqual(engine._llm_candidate_symbols(ranked), {"SCAN1", "SCAN2", "SCAN3"})

    def test_event_triggered_llm_shortlist_selects_only_trade_grade_event(self) -> None:
        engine = StrategyEngine.__new__(StrategyEngine)
        engine.settings = SimpleNamespace(
            llm_max_symbols_per_cycle=3,
            llm_event_triggered_cycles=True,
            llm_max_reviews_per_market_day=12,
            llm_symbol_cooldown_minutes=240,
            llm_open_position_review_interval_minutes=15,
            llm_min_trigger_score_pct=70,
            llm_min_trigger_confluence=16,
            llm_material_score_delta=0.08,
            dynamic_scan_candidate_limit=5,
        )
        engine.sentiment = SimpleNamespace(db=_FakeStateDb())

        ranked = [
            {
                "symbol": "READY",
                "action": "HOLD",
                "combined": 0.38,
                "sentiment_score": 0.0,
                "technical": SimpleNamespace(score=0.38),
                "context": {
                    "best_strategy": {"name": "volume_price_accumulation", "score": 0.38},
                    "position": {},
                    "data_readiness": {"trade_decision_ready": True},
                    "decision_gate_context": {"failed_gates": []},
                    "full_spectrum_analysis": {
                        "confluence_score": {"total": 19},
                        "entry_quality": {"entry_grade": "A"},
                        "breakout_quality": {"breakout_quality": "confirmed", "volume_confirmation": True},
                        "strategy_logic_filters": {"breakout_volume": {"volume_confirmed": True}},
                    },
                    "system_gate_audit": {"overall_score_pct": 82, "overall_grade": "B", "hard_blocked": False},
                },
                "row": {
                    "symbol": "READY",
                    "exchange": "NSE",
                    "sector": "Industrials",
                    "_opportunity_rank": 1,
                    "_opportunity_scan": {
                        "rank": 1,
                        "score": 0.95,
                        "bucket": "Actionable",
                        "setup": "breakout_continuation",
                        "data_quality": {"actionable_data_ready": True},
                    },
                },
            }
        ]

        self.assertEqual(engine._llm_candidate_symbols(ranked), {"READY"})
        self.assertEqual(engine._last_llm_selection_details["READY"]["reason"], "event_triggered")

    def test_event_triggered_llm_shortlist_skips_ordinary_hold_symbols(self) -> None:
        engine = StrategyEngine.__new__(StrategyEngine)
        engine.settings = SimpleNamespace(
            llm_max_symbols_per_cycle=3,
            llm_event_triggered_cycles=True,
            llm_max_reviews_per_market_day=40,
            llm_symbol_cooldown_minutes=60,
            llm_open_position_review_interval_minutes=15,
            llm_min_trigger_score_pct=70,
            llm_min_trigger_confluence=16,
            llm_material_score_delta=0.08,
            dynamic_scan_candidate_limit=5,
        )
        engine.sentiment = SimpleNamespace(db=_FakeStateDb())

        ranked = [
            {
                "symbol": "QUIET",
                "action": "HOLD",
                "combined": 0.18,
                "sentiment_score": 0.0,
                "technical": SimpleNamespace(score=0.18),
                "context": {
                    "best_strategy": {"name": "no_actionable_strategy", "score": 0.18},
                    "position": {},
                    "full_spectrum_analysis": {"confluence_score": {"total": 9}},
                    "system_gate_audit": {"overall_score_pct": 48, "overall_grade": "C"},
                },
                "row": {"symbol": "QUIET", "exchange": "NSE", "sector": "Industrials"},
            }
        ]

        self.assertEqual(engine._llm_candidate_symbols(ranked), set())
        self.assertEqual(engine._last_llm_selection_details["QUIET"]["reason"], "no_material_llm_event")

    def test_event_triggered_llm_shortlist_blocks_untradeable_trigger(self) -> None:
        engine = StrategyEngine.__new__(StrategyEngine)
        engine.settings = SimpleNamespace(
            llm_max_symbols_per_cycle=3,
            llm_event_triggered_cycles=True,
            llm_max_reviews_per_market_day=12,
            llm_symbol_cooldown_minutes=240,
            llm_open_position_review_interval_minutes=15,
            llm_min_trigger_score_pct=70,
            llm_min_trigger_confluence=16,
            llm_material_score_delta=0.08,
            dynamic_scan_candidate_limit=5,
        )
        engine.sentiment = SimpleNamespace(db=_FakeStateDb())

        ranked = [
            {
                "symbol": "BLOCKED",
                "action": "BUY",
                "combined": 0.45,
                "sentiment_score": 0.0,
                "technical": SimpleNamespace(score=0.45),
                "context": {
                    "best_strategy": {"name": "volume_price_accumulation", "score": 0.45},
                    "position": {},
                    "data_readiness": {"trade_decision_ready": False},
                    "decision_gate_context": {"failed_gates": [{"gate": "phase2_data_readiness"}]},
                    "full_spectrum_analysis": {
                        "confluence_score": {"total": 20},
                        "entry_quality": {"entry_grade": "A"},
                        "breakout_quality": {"breakout_quality": "confirmed", "volume_confirmation": True},
                    },
                    "system_gate_audit": {"overall_score_pct": 84, "overall_grade": "A", "hard_blocked": True},
                },
                "row": {"symbol": "BLOCKED", "exchange": "NSE", "sector": "Industrials"},
            }
        ]

        self.assertEqual(engine._llm_candidate_symbols(ranked), set())
        self.assertEqual(engine._last_llm_selection_details["BLOCKED"]["reason"], "entry_not_trade_ready")

    def test_event_triggered_llm_shortlist_respects_symbol_cooldown(self) -> None:
        now = utc_now()
        db = _FakeStateDb(
            {
                "llm_symbol_review_state": {
                    "symbols": {
                        "COOL": {
                            "last_reviewed_at": now,
                            "last_score": 0.42,
                            "last_action": "BUY",
                        }
                    },
                    "daily_budget": {"date": now[:10], "reviews": 1},
                }
            }
        )
        engine = StrategyEngine.__new__(StrategyEngine)
        engine.settings = SimpleNamespace(
            llm_max_symbols_per_cycle=3,
            llm_event_triggered_cycles=True,
            llm_max_reviews_per_market_day=40,
            llm_symbol_cooldown_minutes=60,
            llm_open_position_review_interval_minutes=15,
            llm_min_trigger_score_pct=70,
            llm_min_trigger_confluence=16,
            llm_material_score_delta=0.08,
            dynamic_scan_candidate_limit=5,
        )
        engine.sentiment = SimpleNamespace(db=db)

        ranked = [
            {
                "symbol": "COOL",
                "action": "BUY",
                "combined": 0.42,
                "sentiment_score": 0.0,
                "technical": SimpleNamespace(score=0.42),
                "context": {
                    "best_strategy": {"name": "volume_price_accumulation", "score": 0.42},
                    "position": {},
                    "full_spectrum_analysis": {"confluence_score": {"total": 18}},
                    "system_gate_audit": {"overall_score_pct": 76, "overall_grade": "B"},
                },
                "row": {"symbol": "COOL", "exchange": "NSE", "sector": "Industrials"},
            }
        ]

        self.assertEqual(engine._llm_candidate_symbols(ranked), set())
        self.assertEqual(engine._last_llm_selection_details["COOL"]["reason"], "symbol_llm_cooldown_active")

    def test_opportunity_scan_rejects_stale_quotes(self) -> None:
        scanner = OpportunityScanner(_scanner_settings())
        stale_asof = datetime(2026, 1, 1, tzinfo=timezone.utc).isoformat()
        result = scanner.rank(
            [{"symbol": "STALE", "exchange": "NSE", "sector": "Industrials"}],
            {
                "STALE": Quote(
                    symbol="STALE",
                    price=100,
                    source="upstox-live",
                    asof=stale_asof,
                    high=101,
                    low=98,
                    volume=1_000_000,
                )
            },
            {"STALE": {"daily": _trend_candles(volume_spike=True), "analysis": _trend_candles(volume_spike=True)}},
        )

        self.assertEqual(result.candidates, [])
        self.assertEqual(result.rejected_counts["stale_quote"], 1)

    def test_compacted_decision_preserves_phase2_readiness(self) -> None:
        readiness = {"trade_decision_ready": True, "grade": "A"}
        raw = json.dumps(
            {
                "score_breakdown": {"combined": 0.51, "score_percent": 82},
                "overall_score_pct": 84,
                "overall_grade": "A",
                "system_gate_audit": {"overall_score_pct": 84, "overall_grade": "A", "hard_blocked": False},
                "context": {
                    "data_readiness": readiness,
                    "full_spectrum_analysis": _full_spectrum_summary(),
                    "performance_feedback": {"sample_size": 4},
                },
                "padding": ["x" * 200 for _ in range(80)],
            }
        )

        compacted = json.loads(_compact_decision_details({"action": "HOLD", "symbol": "READY"}, raw))

        self.assertTrue(compacted["data_readiness"]["trade_decision_ready"])
        self.assertTrue(compacted["context_summary"]["data_readiness"]["trade_decision_ready"])
        self.assertIn("performance_feedback", compacted)

    def test_compacted_signal_idea_reads_system_gate_data_readiness(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(Path(tmpdir) / "agent.db")
            db.init()
            decision = Decision(
                symbol="READYBUY",
                action="BUY",
                confidence=0.91,
                price=100,
                technical_score=0.8,
                sentiment_score=0.2,
                reason="trade ready compacted buy",
                asof=utc_now(),
                strategy="normalized_momentum_factor",
                details_json=json.dumps(
                    {
                        "score_breakdown": {"combined": 0.51, "score_percent": 82},
                        "overall_score_pct": 84,
                        "overall_grade": "A",
                        "system_gate_audit": {
                            "overall_score_pct": 84,
                            "overall_grade": "A",
                            "hard_blocked": False,
                            "data_readiness": {"trade_decision_ready": True, "grade": "A"},
                        },
                        "context_summary": {
                            "full_spectrum_summary": _full_spectrum_summary(),
                        },
                    }
                ),
            )

            db.upsert_signal_ideas_from_decisions([decision])
            with db.connect() as conn:
                row = conn.execute("select * from signal_ideas where symbol = 'READYBUY'").fetchone()

        self.assertIsNotNone(row)
        self.assertEqual(row["signal_type"], "BUY")
        details = json.loads(row["details_json"])
        self.assertTrue(details["data_readiness"]["trade_decision_ready"])
        self.assertTrue(details["quality_gate"]["passed"])

    def test_legacy_buy_without_readiness_displays_as_watch(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(Path(tmpdir) / "agent.db")
            db.init()
            now = utc_now()
            with db.connect() as conn:
                conn.execute(
                    """
                    insert into signal_ideas (
                        first_seen_at, last_seen_at, symbol, strategy, plan_code, signal_type, status,
                        entry_price, latest_price, current_return_pct, peak_return_pct, worst_return_pct,
                        confidence, combined_score, confluence, overall_score_pct, overall_grade,
                        reason, details_json
                    )
                    values (?, ?, 'LEGACYBUY', 'normalized_momentum_factor', 'institutional_quality_swing',
                        'BUY', 'ACTIVE', 100, 100, 0, 0, 0, 0.8, 0.5, 22, 88, 'A',
                        'legacy buy missing readiness', ?)
                    """,
                    (
                        now,
                        now,
                        json.dumps(
                            {
                                "action": "BUY",
                                "overall_score_pct": 88,
                                "overall_grade": "A",
                                "confluence": 22,
                            }
                        ),
                    ),
                )

            [row] = db.latest_signal_ideas(5)

        self.assertEqual(row["display_signal"], "Watch")
        self.assertEqual(row["fresh_action"], "WATCH")
        self.assertEqual(row["setup_bucket"], "WATCH")
        self.assertEqual(row["opportunity_label"], "Missing market evidence")
        self.assertIn("required market evidence is missing", row["display_reason"])

    def test_extended_high_confluence_setup_becomes_pullback_watch(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(Path(tmpdir) / "agent.db")
            db.init()
            decision = Decision(
                symbol="PULLBACK",
                action="HOLD",
                confidence=0.62,
                price=112.0,
                technical_score=0.42,
                sentiment_score=0.0,
                reason="extended setup should wait for pullback",
                asof=utc_now(),
                strategy="volume_price_accumulation",
                details_json=json.dumps(
                    {
                        "score_breakdown": {"combined": 0.24, "score_percent": 68},
                        "system_gate_audit": {
                            "overall_score_pct": 68,
                            "overall_grade": "B",
                            "hard_blocked": True,
                            "active_flags": ["PRICE_EXTENDED_FROM_PIVOT"],
                            "hard_blocks": [{"flag": "PRICE_EXTENDED_FROM_PIVOT", "reason": "price_extended_from_pivot"}],
                            "data_readiness": {"trade_decision_ready": True, "grade": "A"},
                        },
                        "context": {
                            "data_readiness": {"trade_decision_ready": True, "grade": "A"},
                            "decision_gate_context": {
                                "failed_gates": [
                                    {"gate": "entry_grade_gate", "reason": "extended_entry_no_new_longs"}
                                ]
                            },
                            "full_spectrum_analysis": {
                                "confluence_score": {"total": 19, "tier": "HIGH_CONVICTION"},
                                "trade_plan": {"entry_zone": [104, 107], "stop_loss": 99, "targets": [{"price": 118}]},
                                "entry_quality": {"entry_grade": "D", "distance_from_pivot_pct": 6.4},
                                "breakout_quality": {"breakout_quality": "confirmed", "volume_confirmation": True},
                                "strategy_logic_filters": {"passed": True},
                                "risk_overrides": {"flags": []},
                            },
                        },
                    }
                ),
            )

            db.upsert_signal_ideas_from_decisions([decision])
            [row] = db.latest_signal_ideas(5)

        self.assertEqual(row["signal_type"], "WATCH")
        self.assertEqual(row["opportunity_state"], "PULLBACK_BUY_ZONE")
        self.assertEqual(row["opportunity_label"], "Wait for pullback")
        self.assertIn("wrong price", row["opportunity_summary"])
        self.assertNotIn("PULLBACK_BUY_ZONE", row["setup_bucket_label"])

    def test_suspect_breakout_setup_gets_confirmation_explanation(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(Path(tmpdir) / "agent.db")
            db.init()
            decision = Decision(
                symbol="CONFIRM",
                action="HOLD",
                confidence=0.58,
                price=88.0,
                technical_score=0.35,
                sentiment_score=0.0,
                reason="breakout needs volume",
                asof=utc_now(),
                strategy="breakout_continuation",
                details_json=json.dumps(
                    {
                        "score_breakdown": {"combined": 0.22, "score_percent": 64},
                        "system_gate_audit": {
                            "overall_score_pct": 64,
                            "overall_grade": "B",
                            "hard_blocked": True,
                            "active_flags": ["SUSPECT_BREAKOUT_WITHOUT_VOLUME"],
                            "hard_blocks": [{"flag": "SUSPECT_BREAKOUT_WITHOUT_VOLUME"}],
                            "data_readiness": {"trade_decision_ready": True, "grade": "A"},
                        },
                        "context": {
                            "data_readiness": {"trade_decision_ready": True, "grade": "A"},
                            "decision_gate_context": {
                                "failed_gates": [
                                    {"gate": "breakout_volume_gate", "reason": "suspect_breakout_without_volume"}
                                ]
                            },
                            "full_spectrum_analysis": {
                                "confluence_score": {"total": 18, "tier": "HIGH"},
                                "trade_plan": {"entry_zone": [88, 90], "stop_loss": 84, "targets": [{"price": 96}]},
                                "entry_quality": {"entry_grade": "B", "distance_from_pivot_pct": 1.8},
                                "breakout_quality": {"breakout_quality": "suspect", "volume_confirmation": False},
                                "strategy_logic_filters": {"breakout_volume": {"suspect_without_volume": True}},
                                "risk_overrides": {"flags": []},
                            },
                        },
                    }
                ),
            )

            db.upsert_signal_ideas_from_decisions([decision])
            [row] = db.latest_signal_ideas(5)

        self.assertEqual(row["signal_type"], "WATCH")
        self.assertEqual(row["opportunity_state"], "BREAKOUT_CONFIRMATION_NEEDED")
        self.assertEqual(row["opportunity_label"], "Needs breakout confirmation")
        self.assertIn("volume", row["opportunity_next_step"].lower())

    def test_near_ready_setup_becomes_buy_candidate_not_tradeable_buy(self) -> None:
        state = opportunity_state_from_signal_details(
            {
                "action": "HOLD",
                "quality_gate": {"passed": False},
                "overall_score_pct": 66,
                "overall_grade": "C",
                "confluence": 19,
                "data_readiness": {"trade_decision_ready": True, "grade": "A"},
                "entry_quality": {"entry_grade": "B", "distance_from_pivot_pct": 1.6},
                "breakout_quality": {"breakout_quality": "confirmed", "volume_confirmation": True},
                "strategy_logic_filters": {"passed": True, "hard_blocks": [], "penalties": []},
                "failed_gates": [{"gate": "overall_quality_gate", "reason": "overall_score_below_70_no_new_longs"}],
                "hard_blocks": [],
                "active_flags": [],
                "risk_flags": [],
            }
        )

        self.assertEqual(state["state"], "BUY_CANDIDATE")
        self.assertEqual(state["label"], "Buy candidate")
        self.assertTrue(state["publish_as_watch"])
        self.assertIn("no paper/live entry", state["next_step"])

    def test_us_data_needed_candidate_is_published_without_becoming_buy(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(Path(tmpdir) / "agent.db")
            db.init()
            db.upsert_universe_rows(
                [
                    {
                        "symbol": "ALAB",
                        "name": "Astera Labs",
                        "exchange": "NASDAQ",
                        "yahoo_symbol": "ALAB",
                        "sector": "US Equity",
                        "industry": "Semiconductors",
                        "base_price": 100,
                        "enabled": 1,
                    }
                ]
            )
            decision = Decision(
                symbol="ALAB",
                action="HOLD",
                strategy="volume_price_accumulation",
                confidence=0.28,
                price=312.25,
                technical_score=0.65,
                sentiment_score=0.31,
                reason="failed_gates=['system_rule_DATA_READINESS_BLOCK']",
                asof=utc_now(),
                details_json=json.dumps(
                    {
                        "final_action": "HOLD",
                        "score_breakdown": {"combined": 0.284, "score_percent": 64.2},
                        "system_gate_audit": {
                            "overall_score_pct": 0.0,
                            "overall_grade": "F",
                            "hard_blocked": True,
                            "active_flags": ["DATA_READINESS_BLOCK"],
                            "hard_blocks": [{"flag": "DATA_READINESS_BLOCK", "reason": "missing trade-grade US data"}],
                            "data_readiness": {
                                "market_region": "US",
                                "trade_decision_ready": False,
                                "hard_gaps": [
                                    {"key": "us_realtime_quote", "label": "US consolidated real-time quote"},
                                    {"key": "us_minute_bars", "label": "US minute bars"},
                                ],
                                "soft_gaps": [{"key": "us_sec_filings", "label": "SEC filings / EDGAR event check"}],
                                "missing_data": ["us_realtime_quote", "us_minute_bars", "us_sec_filings"],
                            },
                        },
                        "context_summary": {
                            "data_readiness": {
                                "market_region": "US",
                                "trade_decision_ready": False,
                                "hard_gaps": [
                                    {"key": "us_realtime_quote", "label": "US consolidated real-time quote"},
                                    {"key": "us_minute_bars", "label": "US minute bars"},
                                ],
                                "missing_data": ["us_realtime_quote", "us_minute_bars", "us_sec_filings"],
                            },
                            "full_spectrum_summary": {
                                "confluence_score": {"total": 20.0, "tier": "HIGH_CONVICTION"},
                                "trade_plan": {"entry_zone": [310.0, 314.0], "stop_loss": 287.0, "targets": []},
                                "entry_quality": {"entry_grade": "B", "distance_from_pivot_pct": 4.6},
                                "breakout_quality": {"breakout_quality": "not_breakout", "two_day_rule_failed": False},
                                "strategy_logic_filters": {"passed": True, "hard_blocks": [], "breakout_volume": {}},
                                "risk_overrides": {"flags": []},
                            },
                        },
                    }
                ),
            )

            db.upsert_signal_ideas_from_decisions([decision])
            [row] = db.latest_signal_ideas(5, market_region="US")

        self.assertEqual(row["signal_type"], "WATCH")
        self.assertEqual(row["status"], "WATCH")
        self.assertEqual(row["opportunity_state"], "DATA_NEEDED")
        self.assertEqual(row["opportunity_label"], "Missing market evidence")
        self.assertEqual(row["overall_score_pct"], 64.2)
        self.assertEqual(row["details"]["tradeability_score_pct"], 0.0)
        self.assertIn("US consolidated real-time quote", row["opportunity_next_step"])

    def test_performance_feedback_requires_strong_sample_before_hard_block(self) -> None:
        self.assertIsNone(
            _performance_feedback_block(
                {
                    "selected_strategy_market": {
                        "key": "IN:volume_price_accumulation",
                        "closed_trades": 9,
                        "expectancy_pct": 0.34,
                        "stop_hit_rate": 0.67,
                        "win_rate": 0.44,
                    }
                }
            )
        )
        self.assertIsNone(
            _performance_feedback_block(
                {
                    "selected_market": {
                        "key": "IN",
                        "closed_trades": 48,
                        "expectancy_pct": -1.4,
                        "stop_hit_rate": 0.78,
                        "win_rate": 0.21,
                    }
                }
            )
        )
        self.assertIsNotNone(
            _performance_feedback_block(
                {
                    "selected_strategy_market": {
                        "key": "IN:volume_price_accumulation",
                        "closed_trades": 22,
                        "expectancy_pct": -0.4,
                        "stop_hit_rate": 0.68,
                        "win_rate": 0.32,
                    }
                }
            )
        )

    def test_mfe_profit_protection_blocks_winner_round_trip(self) -> None:
        action = _paper_exit_action(
            {
                "idea_status": "ACTIVE",
                "entry_price": 100,
                "idea_latest_price": 100.4,
                "peak_return_pct": 6.8,
            },
            {"lifecycle_status": "active", "highest_target_hit": "NONE", "stop_loss": 96},
            {"mark_state": {"peak_return_pct": 6.8, "worst_return_pct": -0.2}},
        )

        self.assertIsNotNone(action)
        self.assertEqual(action["key"], "MFE_PROFIT_PROTECT")
        self.assertTrue(action["full"])


def _scanner_settings() -> SimpleNamespace:
    return SimpleNamespace(
        dynamic_scan_candidate_limit=60,
        dynamic_scan_min_score=0.58,
        dynamic_scan_require_active_setup=True,
        dynamic_scan_min_price=10,
        dynamic_scan_min_turnover_inr=50_000_000,
        dynamic_scan_breakout_distance_pct=3.0,
        dynamic_scan_sentiment_enabled=True,
        dynamic_scan_sentiment_weight=0.12,
    )


class _FakeStateDb:
    def __init__(self, state: dict | None = None) -> None:
        self.state = state or {}

    def get_state(self, key: str, default: object = None) -> object:
        return self.state.get(key, default)

    def set_state(self, key: str, value: object) -> None:
        self.state[key] = value


def _full_spectrum_summary() -> dict:
    return {
        "confluence_score": {"total": 22, "tier": "HIGH_CONVICTION"},
        "signal_plan": {"direction": "BUY", "decision_readiness": "actionable"},
        "trade_plan": {
            "entry_zone": [98, 102],
            "stop_loss": 94,
            "targets": [{"price": 108}, {"price": 112}, {"price": 118}],
        },
        "risk_overrides": {"flags": []},
        "strategy_logic_filters": {"passed": True, "hard_blocks": []},
        "breakout_quality": {"breakout_quality": "confirmed", "volume_confirmation": True},
        "entry_quality": {"grade": "A"},
    }


def _trend_candles(volume_spike: bool) -> list[Candle]:
    candles: list[Candle] = []
    close = 50.0
    for index in range(260):
        close *= 1.0035
        volume = 1_000_000
        if volume_spike and index == 259:
            volume = 1_700_000
            close *= 1.015
        candles.append(
            Candle(
                symbol="TREND",
                ts=f"2026-01-{(index % 28) + 1:02d}T00:00:00+00:00",
                open=close * 0.992,
                high=close * 1.004,
                low=close * 0.988,
                close=close,
                volume=volume,
                source="unit-test",
            )
        )
    return candles


if __name__ == "__main__":
    unittest.main()
