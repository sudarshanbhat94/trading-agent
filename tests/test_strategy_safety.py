from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from app.decision_contract import current_decision_rows
from app.db import Database, _compact_decision_details, _paper_exit_action
from app.full_spectrum import _strategy_confirmed_entry_quality
from app.models import Candle, Decision, Quote, utc_now
from app.opportunity_scanner import OpportunityScanner
from app.opportunity_state import opportunity_state_from_signal_details
from app.agent import _auto_follow_idea_fresh_enough
from app.paper_broker import PaperBroker
from app.raw_entry_model import evaluate_raw_entry
from app.signal_quality import auto_follow_quality_gate, fresh_buy_quality_gate
from app.strategy import StrategyEngine, _compact_context, _fresh_market_data_block_reason, _performance_feedback_block
from app.strategy_presets import choose_best_strategy, evaluate_strategy_presets
from app.trade_economics import auto_follow_sizing


class RawEntryModelSafetyTests(unittest.TestCase):
    def test_raw_opportunity_does_not_use_old_positive_setup_blocker(self) -> None:
        context = _raw_entry_context(setup="watchlist_candidate")
        engine = StrategyEngine(_raw_opportunity_settings(), SimpleNamespace(), SimpleNamespace())

        action = engine._action_from_context("RAWBUY", 0.0, {}, context, {})

        self.assertIn(action, {"BUY", "HOLD"})
        self.assertEqual(context["decision_gate_context"]["decision_authority"], "raw_opportunity_v1")
        self.assertEqual(context["raw_entry_model"]["entry_blockers"], [])
        self.assertEqual(context["raw_entry_model"]["diagnostics"]["hard_block_policy"], "invalid_quote_untradeable_or_hard_liquidity_only")

    def test_raw_opportunity_buys_live_india_momentum_without_price_3000_or_news_gate(self) -> None:
        context = _raw_entry_context(
            price=108.0,
            setup="opening_ignition",
            market_region="IN",
            data_readiness={"trade_decision_ready": False, "missing_data": ["legacy_phase2_gap"]},
            technical_score=0.66,
            day_gain_pct=2.4,
            volume_ratio=1.8,
            projected_volume_ratio=2.2,
            day_range_position=0.74,
            day_high_distance_pct=1.2,
        )
        engine = StrategyEngine(_raw_opportunity_settings(), SimpleNamespace(), SimpleNamespace())

        action = engine._action_from_context("RAWBUY", 0.0, {}, context, {})

        self.assertEqual(action, "BUY")
        self.assertEqual(context["decision_gate_context"]["decision_authority"], "raw_opportunity_v1")
        self.assertTrue(context["raw_entry_model"]["passed"])
        self.assertEqual(context["raw_entry_model"]["decision_label"], "ENTRY_READY")
        self.assertEqual(context["raw_entry_model"]["setup_family"], "live_momentum")
        self.assertEqual(context["raw_entry_model"]["entry_blockers"], [])

    def test_raw_entry_model_blocks_only_invalid_or_untradeable_truth_checks(self) -> None:
        context = _raw_entry_context(price=0.0)
        engine = StrategyEngine(_raw_opportunity_settings(), SimpleNamespace(), SimpleNamespace())

        action = engine._action_from_context("BADQUOTE", 0.0, {}, context, {})

        self.assertEqual(action, "HOLD")
        self.assertFalse(context["raw_entry_model"]["passed"])
        self.assertEqual(context["raw_entry_model"]["truth_blocks"][0]["reason"], "invalid_quote_price")

    def test_scanner_quality_reject_keeps_soft_candidates_for_full_decision(self) -> None:
        scanner = OpportunityScanner(SimpleNamespace(dynamic_scan_min_score=0.0, dynamic_scan_require_active_setup=False))

        soft_reason = scanner._quality_reject_reason(
            {"bucket": "Avoid", "score": 0.0, "setup": "watchlist_candidate", "data_quality": {}}
        )
        hard_reason = scanner._quality_reject_reason(
            {"bucket": "Avoid", "score": 0.0, "setup": "watchlist_candidate", "data_quality": {"reject_reason": "invalid_price"}}
        )

        self.assertEqual(soft_reason, "")
        self.assertEqual(hard_reason, "invalid_price")

    def test_raw_entry_model_score_payload_is_explainable(self) -> None:
        model = evaluate_raw_entry(_raw_entry_context(setup="opening_ignition", market_region="US"), _raw_opportunity_settings())

        self.assertTrue(model["passed"])
        self.assertGreaterEqual(model["raw_score"], model["entry_line"])
        self.assertIn("volume_ratio", model["components"])
        self.assertEqual(model["decision_label"], "ENTRY_READY")
        self.assertEqual(model["setup_family"], "live_momentum")
        self.assertTrue(model["legacy_decision_logic_removed"])

    def test_us_live_momentum_accepts_volume_price_confirmation_without_old_filter(self) -> None:
        model = evaluate_raw_entry(
            _raw_entry_context(
                setup="opening_ignition",
                market_region="US",
                technical_score=0.57,
                day_gain_pct=2.1,
                volume_ratio=11.0,
                projected_volume_ratio=11.0,
            ),
            _raw_opportunity_settings(),
        )

        self.assertTrue(model["passed"])
        self.assertEqual(model["action"], "BUY")
        self.assertEqual(model["decision_label"], "ENTRY_READY")
        self.assertEqual(model["setup_family"], "live_momentum")
        self.assertEqual(model["entry_blockers"], [])

    def test_us_live_momentum_accepts_strong_move_volume_confirmation(self) -> None:
        model = evaluate_raw_entry(
            _raw_entry_context(
                setup="intraday_momentum",
                market_region="US",
                technical_score=0.50,
                day_gain_pct=5.5,
                volume_ratio=2.6,
                projected_volume_ratio=2.6,
            ),
            _raw_opportunity_settings(),
        )

        self.assertTrue(model["passed"])
        self.assertEqual(model["decision_label"], "ENTRY_READY")
        self.assertEqual(model["setup_family"], "live_momentum")
        self.assertNotIn(
            "us_live_momentum_confirmation_filter",
            {blocker["reason"] for blocker in model["entry_blockers"]},
        )

    def test_us_smallcap_reclaim_accepts_nuvl_style_volume_rs_setup(self) -> None:
        context = _raw_entry_context(
            price=91.37,
            setup="smallcap_momentum",
            market_region="US",
            technical_score=0.684,
            day_gain_pct=0.0,
            volume_ratio=2.4241,
            projected_volume_ratio=30.301,
            day_range_position=0.0,
            day_high_distance_pct=None,
        )
        context["opportunity_scan"].update(
            {
                "score": 0.4639,
                "bucket": "Watch",
                "turnover": 6_607_787.03,
                "projected_turnover": 82_597_337.88,
                "components": {"live_momentum": 0.0},
                "rally_evidence": {
                    "distance_to_sma20_pct": -11.3764,
                    "distance_to_near_high_pct": 20.1379,
                    "volume_support": True,
                    "return_5d_pct": 6.7931,
                },
                "btst": {"evidence": {"rs_rank": 75.78}},
            }
        )

        model = evaluate_raw_entry(context, _raw_opportunity_settings())

        self.assertTrue(model["passed"])
        self.assertEqual(model["decision_label"], "ENTRY_READY")
        self.assertEqual(model["setup_family"], "smallcap_reclaim")
        self.assertGreaterEqual(model["raw_score"], model["entry_line"])

    def test_us_smallcap_reclaim_without_volume_stays_watch_not_truth_blocked(self) -> None:
        context = _raw_entry_context(
            price=91.37,
            setup="smallcap_momentum",
            market_region="US",
            technical_score=0.684,
            day_gain_pct=0.0,
            volume_ratio=1.2,
            projected_volume_ratio=1.3,
            day_range_position=0.0,
            day_high_distance_pct=None,
        )
        context["opportunity_scan"].update(
            {
                "score": 0.4639,
                "bucket": "Watch",
                "turnover": 1_200_000.0,
                "projected_turnover": 1_500_000.0,
                "components": {"live_momentum": 0.0},
                "rally_evidence": {
                    "distance_to_sma20_pct": -11.3764,
                    "distance_to_near_high_pct": 20.1379,
                    "volume_support": False,
                    "return_5d_pct": 6.7931,
                },
                "btst": {"evidence": {"rs_rank": 75.78}},
            }
        )

        model = evaluate_raw_entry(context, _raw_opportunity_settings())

        self.assertFalse(model["passed"])
        self.assertEqual(model["entry_blockers"], [])
        self.assertIn(model["decision_label"], {"WATCH", "NO_TRADE"})

    def test_india_breakout_does_not_require_positive_news_catalyst(self) -> None:
        model = evaluate_raw_entry(
            _raw_entry_context(
                price=490.0,
                setup="breakout_continuation",
                market_region="IN",
                technical_score=0.94,
                day_gain_pct=5.0,
                volume_ratio=8.0,
                projected_volume_ratio=8.0,
                day_high_distance_pct=0.4,
            ),
            _raw_opportunity_settings(),
        )

        self.assertTrue(model["passed"])
        self.assertEqual(model["decision_label"], "ENTRY_READY")
        self.assertEqual(model["setup_family"], "breakout")
        self.assertEqual(model["entry_blockers"], [])
        self.assertFalse(model["components"]["positive_news_catalyst"])

    def test_india_breakout_accepts_positive_news_catalyst(self) -> None:
        model = evaluate_raw_entry(
            _raw_entry_context(
                price=4900.0,
                setup="breakout_continuation",
                market_region="IN",
                technical_score=0.94,
                day_gain_pct=5.0,
                volume_ratio=8.0,
                projected_volume_ratio=8.0,
                day_high_distance_pct=0.4,
                sentiment={
                    "score": 0.35,
                    "headline_count": 1,
                    "positive_catalyst": True,
                    "events": [{"type": "order_win"}],
                },
            ),
            _raw_opportunity_settings(),
        )

        self.assertTrue(model["passed"])
        self.assertEqual(model["decision_label"], "ENTRY_READY")
        self.assertEqual(model["setup_family"], "breakout")
        self.assertTrue(model["components"]["positive_news_catalyst"])

    def test_india_live_momentum_no_longer_has_cost_adjusted_veto(self) -> None:
        model = evaluate_raw_entry(
            _raw_entry_context(
                price=303.0,
                setup="opening_ignition",
                market_region="IN",
                technical_score=0.88,
                day_gain_pct=3.6,
                volume_ratio=3.1,
                projected_volume_ratio=3.1,
                day_range_position=0.94,
                day_high_distance_pct=0.23,
            ),
            _raw_opportunity_settings(),
        )

        self.assertTrue(model["passed"])
        self.assertEqual(model["decision_label"], "ENTRY_READY")
        self.assertEqual(model["entry_blockers"], [])

    def test_india_live_momentum_accepts_strong_cost_adjusted_shape(self) -> None:
        model = evaluate_raw_entry(
            _raw_entry_context(
                price=4408.0,
                setup="intraday_momentum",
                market_region="IN",
                technical_score=0.61,
                day_gain_pct=6.4,
                volume_ratio=79.0,
                projected_volume_ratio=79.0,
                day_range_position=0.98,
                day_high_distance_pct=0.11,
            ),
            _raw_opportunity_settings(),
        )

        self.assertTrue(model["passed"])
        self.assertEqual(model["decision_label"], "ENTRY_READY")
        self.assertEqual(model["setup_family"], "live_momentum")


@unittest.skip("Legacy strategy gate tests were intentionally retired for the raw opportunity model.")
class StrategySafetyTests(unittest.TestCase):
    def test_fresh_gate_pass_overrides_stale_probe_marker(self) -> None:
        reason = _fresh_market_data_block_reason(
            {
                "data_readiness": {
                    "trade_decision_ready": True,
                    "fresh_market_data_gate": {"passed": True, "reason": "current_session_data"},
                },
                "opportunity_scan": {
                    "bucket": "Actionable",
                    "data_quality": {"missing": ["stale_intraday_candles"]},
                },
            }
        )

        self.assertEqual(reason, "")

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

        self.assertEqual(action, "HOLD")
        self.assertIn("session_momentum_gate", {gate["gate"] for gate in context["decision_gate_context"]["failed_gates"]})

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

    def test_auto_follow_blocks_entries_safety_manager_would_exit(self) -> None:
        gate = auto_follow_quality_gate(
            {
                "signal_type": "BUY",
                "status": "ACTIVE",
                "fresh_action": "BUY_NOW",
                "overall_score_pct": 64,
                "overall_grade": "C",
                "confluence": 17,
                "details": {
                    "risk_flags": ["false_breakout_risk_no_new_longs"],
                    "data_readiness": {"trade_decision_ready": True},
                    "opportunity_scan": {
                        "bucket": "Actionable",
                        "setup": "52_week_high_volume_breakout",
                        "score": 0.82,
                        "turnover": 120_000_000,
                    },
                },
            }
        )

        self.assertFalse(gate["passed"])
        self.assertEqual(gate["reason"], "overall_score_below_70")

    def test_auto_follow_freshness_blocks_current_cycle_low_quality_probe_buy_symbol(self) -> None:
        fresh = _auto_follow_idea_fresh_enough(
            {
                "symbol": "WOCKPHARMA",
                "signal_type": "BUY",
                "status": "ACTIVE",
                "fresh_action": "BUY_NOW",
                "setup_bucket": "SMALL_SIZE_ONLY",
                "overall_score_pct": 30,
                "overall_grade": "F",
                "current_return_pct": 0.1,
            },
            {"WOCKPHARMA"},
        )

        self.assertFalse(fresh)

    def test_auto_follow_freshness_blocks_low_quality_active_buy_now_probe(self) -> None:
        fresh = _auto_follow_idea_fresh_enough(
            {
                "symbol": "ATGL",
                "signal_type": "BUY",
                "status": "ACTIVE",
                "fresh_action": "BUY_NOW",
                "setup_bucket": "SMALL_SIZE_ONLY",
                "overall_score_pct": 50,
                "overall_grade": "D",
                "current_return_pct": 0.4,
                "last_seen_at": datetime.now(timezone.utc).isoformat(),
            },
            set(),
        )

        self.assertFalse(fresh)

    def test_auto_follow_freshness_blocks_stale_buy_now_probe(self) -> None:
        fresh = _auto_follow_idea_fresh_enough(
            {
                "symbol": "ATGL",
                "signal_type": "BUY",
                "status": "ACTIVE",
                "fresh_action": "BUY_NOW",
                "setup_bucket": "SMALL_SIZE_ONLY",
                "overall_score_pct": 80,
                "overall_grade": "A",
                "current_return_pct": 0.4,
                "last_seen_at": (datetime.now(timezone.utc) - timedelta(minutes=45)).isoformat(),
            },
            set(),
        )

        self.assertFalse(fresh)

    def test_auto_follow_freshness_blocks_risk_review_buy_now_probe(self) -> None:
        fresh = _auto_follow_idea_fresh_enough(
            {
                "symbol": "RISKY",
                "signal_type": "BUY",
                "status": "ACTIVE",
                "fresh_action": "BUY_NOW",
                "setup_bucket": "RISK_REVIEW",
                "overall_score_pct": 80,
                "overall_grade": "A",
                "current_return_pct": 0.1,
            },
            set(),
        )

        self.assertFalse(fresh)

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

    def test_recent_buy_decision_without_active_idea_does_not_suppress_fresh_buy(self) -> None:
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

        self.assertEqual(suppressed.action, "BUY")
        self.assertEqual(suppressed.reason, "repeat buy")

    def test_opportunity_probe_does_not_absorb_low_quality_or_late_chase(self) -> None:
        engine = StrategyEngine.__new__(StrategyEngine)
        profile = {"ready": True, "min_quality_score": 62.0}

        self.assertFalse(
            engine._opportunity_probe_can_absorb_gate(
                {
                    "gate": "overall_quality_gate",
                    "value": {"overall_score_pct": 50.0, "overall_grade": "D"},
                    "reason": "overall_score_below_70_no_new_longs",
                },
                profile,
            )
        )
        self.assertTrue(
            engine._opportunity_probe_can_absorb_gate(
                {
                    "gate": "overall_quality_gate",
                    "value": {"overall_score_pct": 70.0, "overall_grade": "B"},
                    "reason": "overall_score_below_70_no_new_longs",
                },
                profile,
            )
        )
        self.assertFalse(
            engine._opportunity_probe_can_absorb_gate(
                {
                    "gate": "session_momentum_gate",
                    "value": {"late_chase": True, "day_gain_pct": 8.0},
                    "reason": "late_intraday_momentum_wait_for_pullback",
                },
                profile,
            )
        )

    def test_compact_decision_context_keeps_opportunity_scan_for_auto_follow(self) -> None:
        compact = _compact_context(
            {
                "symbol": "ANGELONE",
                "quote": {"price": 347.8, "source": "upstox-live"},
                "opportunity_scan": {
                    "bucket": "Actionable",
                    "setup": "52_week_high_volume_breakout",
                    "score": 0.91,
                    "turnover": 120_000_000,
                },
            }
        )

        self.assertEqual(compact["opportunity_scan"]["setup"], "52_week_high_volume_breakout")

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

    def test_opportunity_scan_reserves_final_universe_for_playbook_buys(self) -> None:
        scanner = OpportunityScanner(_scanner_settings())
        scored = [
            {
                "symbol": f"FAST{i}",
                "setup": "intraday_momentum",
                "market_region": "IN",
                "metrics": {"day_gain_pct": 9.0, "turnover": 500_000_000, "volume_ratio": 5.0},
                "components": {"live_momentum": 1.0, "rally_radar": 1.0},
                "market_action": {"available": True, "score": 1.0},
            }
            for i in range(5)
        ]
        scored.append(
            {
                "symbol": "PLAYBOOK",
                "setup": "earnings_beat_gap_and_go",
                "market_region": "IN",
                "metrics": {"day_gain_pct": 6.0, "turnover": 120_000_000, "volume_ratio": 2.2},
                "components": {"live_momentum": 0.1, "rally_radar": 0.1},
                "market_action": {"available": True, "score": 0.4},
                "top_gainers_playbook": {
                    "final_signal": "STRONG BUY",
                    "quant_score": 72,
                    "gain_pct": 6.0,
                    "hard_excluded": False,
                    "hard_excludes": [],
                    "anti_patterns": [],
                },
            }
        )

        selected = scanner._select_rally_radar_then_diverse(scored, 3)

        self.assertIn("PLAYBOOK", {item["symbol"] for item in selected})

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

    def test_opportunity_scan_promotes_live_intraday_fast_movers(self) -> None:
        scanner = OpportunityScanner(_scanner_settings())
        candles = _flat_candles_with_old_high()
        result = scanner.rank(
            [{"symbol": "FASTMOVE", "exchange": "NSE", "sector": "Industrials"}],
            {
                "FASTMOVE": Quote(
                    symbol="FASTMOVE",
                    price=105.8,
                    source="upstox-live",
                    asof=utc_now(),
                    open=100.0,
                    high=106.2,
                    low=99.0,
                    volume=2_000_000,
                )
            },
            {"FASTMOVE": {"daily": candles, "analysis": candles}},
        )

        self.assertEqual(result.candidates[0]["symbol"], "FASTMOVE")
        self.assertEqual(result.candidates[0]["setup"], "intraday_momentum")
        self.assertGreaterEqual(result.candidates[0]["components"]["live_momentum"], 0.70)
        self.assertEqual(result.summary["top_fast_movers"][0]["symbol"], "FASTMOVE")

    def test_opportunity_scan_detects_pre_rally_fuel_before_the_move(self) -> None:
        scanner = OpportunityScanner(_scanner_settings())
        candles = _pre_rally_fuel_candles()
        result = scanner.rank(
            [{"symbol": "FUEL", "exchange": "NSE", "sector": "Industrials"}],
            {
                "FUEL": Quote(
                    symbol="FUEL",
                    price=103.0,
                    source="upstox-live",
                    asof=utc_now(),
                    open=102.5,
                    high=103.2,
                    low=101.6,
                    volume=1_400_000,
                )
            },
            {"FUEL": {"daily": candles, "analysis": candles}},
        )

        self.assertEqual(result.candidates[0]["symbol"], "FUEL")
        self.assertEqual(result.candidates[0]["setup"], "pre_rally_fuel")
        self.assertEqual(result.candidates[0]["trade_window"], "watch_for_ignition")

    def test_opportunity_scan_detects_opening_ignition_before_big_move(self) -> None:
        scanner = OpportunityScanner(_scanner_settings())
        candles = _flat_candles_with_old_high()
        result = scanner.rank(
            [{"symbol": "IGNITE", "exchange": "NSE", "sector": "Industrials"}],
            {
                "IGNITE": Quote(
                    symbol="IGNITE",
                    price=102.6,
                    source="upstox-live",
                    asof=utc_now(),
                    open=100.0,
                    high=102.9,
                    low=99.8,
                    volume=2_000_000,
                )
            },
            {"IGNITE": {"daily": candles, "analysis": candles}},
        )

        self.assertEqual(result.candidates[0]["symbol"], "IGNITE")
        self.assertEqual(result.candidates[0]["setup"], "opening_ignition")
        self.assertEqual(result.candidates[0]["trade_window"], "early_actionable")

    def test_india_rally_scan_keeps_early_relative_volume_under_absolute_floor(self) -> None:
        scanner = OpportunityScanner(_scanner_settings())
        candles = _flat_candles_with_old_high()
        result = scanner.rank(
            [{"symbol": "INEARLY", "exchange": "NSE", "sector": "Industrials"}],
            {
                "INEARLY": Quote(
                    symbol="INEARLY",
                    price=102.6,
                    source="upstox-live",
                    asof=_recent_session_asof("IN"),
                    open=100.0,
                    high=102.9,
                    low=99.8,
                    volume=100_000,
                )
            },
            {"INEARLY": {"daily": candles, "analysis": candles}},
        )

        self.assertEqual(result.candidates[0]["symbol"], "INEARLY")
        self.assertLess(result.candidates[0]["turnover"], 50_000_000)
        self.assertGreater(result.candidates[0]["projected_turnover"], 50_000_000)
        self.assertTrue(result.candidates[0]["liquidity_profile"]["adaptive_pass"])
        self.assertEqual(result.candidates[0]["setup"], "opening_ignition")

    def test_india_rally_scan_rejects_weak_adaptive_liquidity(self) -> None:
        scanner = OpportunityScanner(_scanner_settings())
        candles = _flat_candles_with_old_high()
        result = scanner.rank(
            [{"symbol": "INILLQ", "exchange": "NSE", "sector": "Industrials"}],
            {
                "INILLQ": Quote(
                    symbol="INILLQ",
                    price=102.6,
                    source="upstox-live",
                    asof=_recent_session_asof("IN"),
                    open=100.0,
                    high=102.9,
                    low=99.8,
                    volume=5_000,
                )
            },
            {"INILLQ": {"daily": candles, "analysis": candles}},
        )

        self.assertEqual(result.candidates, [])
        self.assertEqual(result.rejected_counts["below_adaptive_liquidity"], 1)

    def test_us_rally_scan_uses_usd_turnover_floor(self) -> None:
        scanner = OpportunityScanner(_scanner_settings())
        candles = _flat_candles_with_old_high()
        result = scanner.rank(
            [{"symbol": "USIGNITE", "exchange": "NASDAQ", "sector": "Technology"}],
            {
                "USIGNITE": Quote(
                    symbol="USIGNITE",
                    price=102.6,
                    source="yahoo-delayed",
                    asof=utc_now(),
                    open=100.0,
                    high=102.9,
                    low=99.8,
                    volume=100_000,
                )
            },
            {"USIGNITE": {"daily": candles, "analysis": candles}},
        )

        self.assertEqual(result.candidates[0]["symbol"], "USIGNITE")
        self.assertEqual(result.candidates[0]["market_region"], "US")
        self.assertEqual(result.candidates[0]["setup"], "opening_ignition")
        self.assertEqual(result.candidates[0]["trade_window"], "early_actionable")
        self.assertGreater(result.candidates[0]["turnover"], 2_000_000)
        self.assertLess(result.candidates[0]["turnover"], 50_000_000)
        self.assertEqual(result.summary["filters"]["min_turnover_usd"], 2_000_000)

    def test_us_rally_scan_keeps_early_relative_volume_under_absolute_floor(self) -> None:
        scanner = OpportunityScanner(_scanner_settings())
        candles = _flat_candles_with_old_high()
        result = scanner.rank(
            [{"symbol": "USEARLY", "exchange": "NASDAQ", "sector": "Technology"}],
            {
                "USEARLY": Quote(
                    symbol="USEARLY",
                    price=20.4,
                    source="yahoo-delayed",
                    asof=_recent_session_asof("US"),
                    open=20.0,
                    high=20.5,
                    low=19.8,
                    volume=50_000,
                )
            },
            {"USEARLY": {"daily": candles, "analysis": candles}},
        )

        self.assertEqual(result.candidates[0]["symbol"], "USEARLY")
        self.assertLess(result.candidates[0]["turnover"], 2_000_000)
        self.assertGreater(result.candidates[0]["projected_turnover"], 2_000_000)
        self.assertTrue(result.candidates[0]["liquidity_profile"]["adaptive_pass"])
        self.assertEqual(result.candidates[0]["setup"], "opening_ignition")

    def test_us_rally_scan_rejects_weak_adaptive_liquidity(self) -> None:
        scanner = OpportunityScanner(_scanner_settings())
        candles = _flat_candles_with_old_high()
        result = scanner.rank(
            [{"symbol": "USILLQ", "exchange": "NASDAQ", "sector": "Technology"}],
            {
                "USILLQ": Quote(
                    symbol="USILLQ",
                    price=20.4,
                    source="yahoo-delayed",
                    asof=_recent_session_asof("US"),
                    open=20.0,
                    high=20.5,
                    low=19.8,
                    volume=5_000,
                )
            },
            {"USILLQ": {"daily": candles, "analysis": candles}},
        )

        self.assertEqual(result.candidates, [])
        self.assertEqual(result.rejected_counts["below_adaptive_liquidity"], 1)

    def test_market_action_radar_promotes_52_week_volume_breakout(self) -> None:
        scanner = OpportunityScanner(_scanner_settings())
        candles = _flat_candles_with_old_high()
        result = scanner.rank(
            [
                {
                    "symbol": "HFCL",
                    "exchange": "NSE",
                    "sector": "Telecom",
                    "_market_action": {
                        "symbol": "HFCL",
                        "event_types": ["TOP_GAINER", "VOLUME_SHOCKER", "52_WEEK_HIGH"],
                        "market_action_score": 92,
                        "strategy": "52_week_high_volume_breakout",
                        "trade_window": "actionable_if_vwap_holds",
                        "reason": "52 week high, volume shocker, 5.2% move",
                        "pct_change": 5.2,
                        "volume_multiplier": 4.2,
                    },
                }
            ],
            {
                "HFCL": Quote(
                    symbol="HFCL",
                    price=104.8,
                    source="upstox-live",
                    asof=utc_now(),
                    open=100.0,
                    high=105.0,
                    low=99.5,
                    volume=2_200_000,
                )
            },
            {"HFCL": {"daily": candles, "analysis": candles}},
        )

        self.assertEqual(result.candidates[0]["symbol"], "HFCL")
        self.assertEqual(result.candidates[0]["setup"], "52_week_high_volume_breakout")
        self.assertEqual(result.candidates[0]["trade_window"], "actionable_if_vwap_holds")
        self.assertTrue(result.candidates[0]["market_action"]["available"])
        self.assertEqual(result.summary["top_market_action"][0]["symbol"], "HFCL")

    def test_wait_for_pullback_market_action_is_watch_not_actionable(self) -> None:
        scanner = OpportunityScanner(_scanner_settings())
        candles = _flat_candles_with_old_high()
        result = scanner.rank(
            [
                {
                    "symbol": "PULLWAIT",
                    "exchange": "NSE",
                    "sector": "Industrials",
                    "_market_action": {
                        "symbol": "PULLWAIT",
                        "event_types": ["TOP_GAINER", "PRICE_SHOCKER"],
                        "market_action_score": 94,
                        "strategy": "market_action_momentum",
                        "trade_window": "wait_for_pullback",
                        "reason": "moved fast; wait for VWAP pullback",
                        "pct_change": 5.5,
                        "volume_multiplier": 2.4,
                    },
                }
            ],
            {
                "PULLWAIT": Quote(
                    symbol="PULLWAIT",
                    price=111.5,
                    source="upstox-live",
                    asof=utc_now(),
                    open=105.7,
                    high=112.0,
                    low=105.2,
                    volume=2_200_000,
                )
            },
            {"PULLWAIT": {"daily": candles, "analysis": candles}},
        )

        self.assertEqual(result.candidates[0]["trade_window"], "wait_for_pullback")
        self.assertEqual(result.candidates[0]["bucket"], "ACTIONABLE_WATCH")
        self.assertEqual(result.candidates[0]["label"], "ACTIONABLE_WATCH")

    def test_circuit_demand_lock_is_visible_but_waits_for_pullback(self) -> None:
        scanner = OpportunityScanner(_scanner_settings())
        candles = _flat_candles_with_old_high()
        result = scanner.rank(
            [
                {
                    "symbol": "EMMVEE",
                    "exchange": "NSE",
                    "sector": "Renewables",
                    "_market_action": {
                        "symbol": "EMMVEE",
                        "event_types": ["TOP_GAINER", "ONLY_BUYERS", "VOLUME_SHOCKER"],
                        "market_action_score": 88,
                        "strategy": "circuit_demand_lock",
                        "trade_window": "watch_for_pullback",
                        "reason": "upper circuit with volume shocker",
                        "pct_change": 10.0,
                        "volume_multiplier": 2.5,
                    },
                }
            ],
            {
                "EMMVEE": Quote(
                    symbol="EMMVEE",
                    price=110.0,
                    source="upstox-live",
                    asof=utc_now(),
                    open=100.0,
                    high=110.0,
                    low=99.8,
                    volume=1_600_000,
                )
            },
            {"EMMVEE": {"daily": candles, "analysis": candles}},
        )

        self.assertEqual(result.candidates[0]["setup"], "circuit_demand_lock")
        self.assertEqual(result.candidates[0]["trade_window"], "watch_for_pullback")
        self.assertEqual(result.candidates[0]["bucket"], "LATE_CHASE_AVOID")
        self.assertIn("demand locked", " ".join(result.candidates[0]["reasons"]))

    def test_market_action_with_results_news_becomes_earnings_gap_and_go(self) -> None:
        scanner = OpportunityScanner(_scanner_settings())
        candles = _flat_candles_with_old_high()
        result = scanner.rank(
            [
                {
                    "symbol": "BLUEJET",
                    "exchange": "NSE",
                    "sector": "Healthcare",
                    "_market_action": {
                        "symbol": "BLUEJET",
                        "event_types": ["TOP_GAINER", "VOLUME_SHOCKER"],
                        "market_action_score": 82,
                        "strategy": "market_action_momentum",
                        "trade_window": "actionable_if_not_extended",
                        "reason": "top gainer with volume shocker",
                        "pct_change": 6.2,
                        "volume_multiplier": 2.6,
                    },
                }
            ],
            {
                "BLUEJET": Quote(
                    symbol="BLUEJET",
                    price=106.2,
                    source="upstox-live",
                    asof=utc_now(),
                    open=100.0,
                    high=106.5,
                    low=99.8,
                    volume=1_700_000,
                )
            },
            {"BLUEJET": {"daily": candles, "analysis": candles}},
            sentiment_by_symbol={
                "BLUEJET": {
                    "score": 0.42,
                    "confidence": 0.65,
                    "headline_count": 1,
                    "headlines": ["Blue Jet profit rises sharply in quarterly results"],
                    "events": [{"event_type": "earnings", "confidence": 0.7, "source_weight": 0.8}],
                }
            },
        )

        self.assertEqual(result.candidates[0]["setup"], "earnings_beat_gap_and_go")
        self.assertEqual(result.candidates[0]["trade_window"], "actionable_if_vwap_holds")

    def test_live_rally_probe_is_not_dropped_before_history_prefetch(self) -> None:
        scanner = OpportunityScanner(_scanner_settings())
        result = scanner.rank(
            [{"symbol": "NOHISTORY", "exchange": "NSE", "sector": "Industrials"}],
            {
                "NOHISTORY": Quote(
                    symbol="NOHISTORY",
                    price=105.6,
                    source="upstox-live",
                    asof=utc_now(),
                    open=100.0,
                    high=106.0,
                    low=99.8,
                    volume=2_500_000,
                )
            },
            {"NOHISTORY": {"daily": [], "analysis": []}},
        )

        self.assertEqual(result.candidates[0]["symbol"], "NOHISTORY")
        self.assertEqual(result.candidates[0]["setup"], "intraday_momentum")
        self.assertTrue(result.candidates[0]["data_quality"]["probe_only"])
        self.assertIn("daily_history", result.candidates[0]["data_quality"]["missing"])
        self.assertEqual(result.candidates[0]["trade_window"], "actionable_momentum")

    def test_llm_event_trigger_includes_rally_radar_setups(self) -> None:
        engine = StrategyEngine.__new__(StrategyEngine)
        row = {
            "symbol": "IGNITE",
            "exchange": "NSE",
            "_opportunity_scan": {
                "setup": "opening_ignition",
                "bucket": "Actionable",
                "data_quality": {"actionable_data_ready": True},
            },
        }

        self.assertEqual(engine._llm_opportunity_trigger(row), "opportunity_scan_opening_ignition")

    def test_us_live_intraday_strategy_uses_usd_turnover_confirmation(self) -> None:
        engine = StrategyEngine(
            SimpleNamespace(
                max_position_pct=0.1,
                dynamic_scan_min_turnover_inr=50_000_000,
                dynamic_scan_min_turnover_usd=2_000_000,
            ),
            SimpleNamespace(),
            SimpleNamespace(),
        )
        context = _momentum_gate_context(
            session_momentum={
                "available": True,
                "day_gain_pct": 5.2,
                "day_range_position": 0.82,
                "day_high_distance_pct": 0.6,
                "confirmed": True,
                "fast_mover": True,
            }
        )
        context["market_region"] = "US"
        context["best_strategy"] = {"name": "volume_price_accumulation", "score": 0.42}
        context["opportunity_scan"] = {
            "market_region": "US",
            "setup": "intraday_momentum",
            "day_gain_pct": 5.2,
            "day_range_position": 0.82,
            "day_high_distance_pct": 0.6,
            "volume_ratio": 0.0,
            "turnover": 12_000_000,
            "components": {"live_momentum": 0.86},
        }

        engine._apply_live_momentum_strategy(context)

        review = context["full_spectrum_analysis"]["live_momentum_review"]
        self.assertTrue(review["strategy_ready"])
        self.assertEqual(review["turnover_floor"], 10_000_000)
        self.assertEqual(context["best_strategy"]["name"], "live_intraday_momentum")

    def test_reset_mode_keeps_btst_diagnostic_only(self) -> None:
        engine = StrategyEngine(
            SimpleNamespace(
                decision_authority_mode="reset_v2",
                max_position_pct=0.1,
                dynamic_scan_min_turnover_inr=50_000_000,
            ),
            SimpleNamespace(),
            SimpleNamespace(),
        )
        context = _momentum_gate_context(session_momentum={"available": True, "confirmed": False})
        context["best_strategy"] = {"name": "no_actionable_strategy", "score": 0.0}
        context["full_spectrum_analysis"]["entry_quality"] = {
            "entry_grade": "WATCH",
            "volume_confirmation": False,
        }
        context["full_spectrum_analysis"]["trade_plan"] = {}
        context["opportunity_scan"] = _btst_scan_payload()

        engine._apply_btst_strategy(context)

        full = context["full_spectrum_analysis"]
        self.assertEqual(context["best_strategy"]["name"], "no_actionable_strategy")
        self.assertEqual(full["entry_quality"]["entry_grade"], "WATCH")
        self.assertEqual(full["trade_plan"], {})
        self.assertIn("btst_review", full)
        self.assertEqual(full["decision_authority_reset"]["btst_buy_candidate"]["status"], "diagnostic_only")

    def test_reset_mode_keeps_live_momentum_diagnostic_only(self) -> None:
        engine = StrategyEngine(
            SimpleNamespace(
                decision_authority_mode="reset_v2",
                max_position_pct=0.1,
                dynamic_scan_min_turnover_inr=50_000_000,
            ),
            SimpleNamespace(),
            SimpleNamespace(),
        )
        context = _momentum_gate_context(
            session_momentum={
                "available": True,
                "day_gain_pct": 3.4,
                "day_range_position": 0.88,
                "day_high_distance_pct": 0.3,
                "confirmed": True,
                "fast_mover": True,
            }
        )
        context["best_strategy"] = {"name": "no_actionable_strategy", "score": 0.0}
        context["strategy_signals"] = []
        context["full_spectrum_analysis"]["entry_quality"] = {
            "entry_grade": "WATCH",
            "volume_confirmation": False,
        }
        context["opportunity_scan"] = {
            "setup": "opening_ignition",
            "bucket": "Actionable",
            "score": 0.88,
            "day_gain_pct": 3.4,
            "day_range_position": 0.88,
            "day_high_distance_pct": 0.3,
            "volume_ratio": 2.3,
            "turnover": 260_000_000,
            "components": {"live_momentum": 0.86},
            "data_quality": {"actionable_data_ready": True},
        }

        engine._apply_live_momentum_strategy(context)

        full = context["full_spectrum_analysis"]
        self.assertTrue(full["live_momentum_review"]["strategy_ready"])
        self.assertEqual(context["best_strategy"]["name"], "no_actionable_strategy")
        self.assertEqual(context["strategy_signals"], [])
        self.assertEqual(full["entry_quality"]["entry_grade"], "WATCH")
        self.assertEqual(full["decision_authority_reset"]["live_momentum"]["status"], "diagnostic_only")

    def test_fresh_authority_uses_live_quote_without_opportunity_probe(self) -> None:
        engine = StrategyEngine(
            SimpleNamespace(
                decision_authority_mode="reset_v2",
                max_position_pct=0.1,
                dynamic_scan_min_turnover_inr=50_000_000,
            ),
            SimpleNamespace(),
            SimpleNamespace(),
        )
        context = _momentum_gate_context(
            session_momentum={
                "available": True,
                "day_gain_pct": 3.5,
                "day_range_position": 0.92,
                "day_high_distance_pct": 0.3,
                "confirmed": True,
                "fast_mover": True,
            }
        )
        context["full_spectrum_analysis"]["institutional_scorecard"]["buy_ready"] = False
        context["full_spectrum_analysis"]["institutional_scorecard"]["total_score"] = 54
        context["full_spectrum_analysis"]["institutional_scorecard"]["score"] = 54
        context["opportunity_scan"] = {
            "setup": "opening_ignition",
            "bucket": "Actionable",
            "score": 0.84,
            "day_gain_pct": 3.5,
            "day_range_position": 0.92,
            "day_high_distance_pct": 0.3,
            "volume_ratio": 0.45,
            "projected_volume_ratio": 2.5,
            "components": {"live_momentum": 0.66},
            "data_quality": {"actionable_data_ready": False, "missing": ["stale_intraday_candles"]},
        }

        engine._apply_live_momentum_strategy(context)
        action = engine._action_from_context("LIVEPROBE", 0.24, {}, context, {})

        gates = context["decision_gate_context"]
        self.assertEqual(action, "BUY")
        self.assertEqual(gates["decision_authority"], "fresh_authority_v1")
        self.assertTrue(gates["legacy_logic_deleted"])
        self.assertTrue(gates["fresh_trade_authority"]["passed"])
        self.assertNotIn("opportunity_probe", gates)
        self.assertEqual(gates["blocking_failed_gates"], [])

    def test_fresh_authority_blocks_stale_quote(self) -> None:
        engine = StrategyEngine(
            SimpleNamespace(
                decision_authority_mode="reset_v2",
                max_position_pct=0.1,
                dynamic_scan_min_turnover_inr=50_000_000,
            ),
            SimpleNamespace(),
            SimpleNamespace(),
        )
        context = _momentum_gate_context(
            session_momentum={
                "available": True,
                "day_gain_pct": 3.5,
                "day_range_position": 0.92,
                "day_high_distance_pct": 0.3,
                "confirmed": True,
                "fast_mover": True,
            }
        )
        context["quote"]["source"] = "yahoo-delayed"
        context["data_readiness"]["trade_decision_ready"] = False
        context["data_readiness"]["fresh_market_data_gate"] = {"passed": False, "reason": "stale_quote_prior_session"}
        context["opportunity_scan"] = {
            "setup": "opening_ignition",
            "bucket": "Actionable",
            "score": 0.92,
            "day_gain_pct": 3.5,
            "day_range_position": 0.92,
            "day_high_distance_pct": 0.3,
            "volume_ratio": 2.5,
            "components": {"live_momentum": 0.88},
            "data_quality": {"actionable_data_ready": False, "missing": ["stale_quote"]},
        }

        action = engine._action_from_context("STALEQUOTE", 0.5, {}, context, {})

        gates = context["decision_gate_context"]
        self.assertEqual(action, "HOLD")
        self.assertFalse(gates["fresh_trade_authority"]["passed"])
        self.assertIn("stale_or_delayed_quote", {gate["reason"] for gate in gates["fresh_trade_authority"]["blockers"]})

    def test_fresh_authority_generates_clean_trade_contract(self) -> None:
        engine = StrategyEngine(
            SimpleNamespace(
                decision_authority_mode="reset_v2",
                max_position_pct=0.1,
                dynamic_scan_min_turnover_inr=50_000_000,
            ),
            SimpleNamespace(),
            SimpleNamespace(),
        )
        context = _momentum_gate_context(
            session_momentum={
                "available": True,
                "day_gain_pct": 3.2,
                "day_range_position": 0.86,
                "day_high_distance_pct": 0.2,
                "confirmed": True,
                "fast_mover": True,
            }
        )
        context["full_spectrum_analysis"]["entry_quality"]["entry_grade"] = "A"
        context["opportunity_scan"] = {
            "setup": "opening_ignition",
            "bucket": "Actionable",
            "score": 0.91,
            "day_gain_pct": 3.2,
            "day_range_position": 0.86,
            "day_high_distance_pct": 0.2,
            "volume_ratio": 2.4,
            "turnover": 280_000_000,
            "components": {"live_momentum": 0.88},
            "data_quality": {"actionable_data_ready": True},
        }

        engine._apply_live_momentum_strategy(context)
        gate = engine._fresh_trade_authority_gate(context)

        self.assertTrue(gate["passed"])
        self.assertEqual(gate["mode"], "fresh_authority_v1")
        self.assertEqual(gate["fresh_grade"], "A")
        self.assertGreaterEqual(gate["fresh_score"], 84)
        self.assertGreaterEqual(gate["fresh_confluence"], 18)
        self.assertGreater(gate["trade_plan"]["stop_loss"], 0)
        self.assertGreater(gate["trade_plan"]["targets"][0]["price"], context["quote"]["price"])

        context["opportunity_scan"]["volume_ratio"] = 0.2
        context["opportunity_scan"]["turnover"] = 10_000
        context["opportunity_scan"]["projected_turnover"] = 10_000
        blocked = engine._fresh_trade_authority_gate(context)

        self.assertFalse(blocked["passed"])
        self.assertIn("volume_or_turnover_not_confirmed", {item["reason"] for item in blocked["blockers"]})

    def test_broad_momentum_buy_requires_current_session_confirmation(self) -> None:
        engine = StrategyEngine(SimpleNamespace(max_position_pct=0.1), SimpleNamespace(), SimpleNamespace())
        context = _momentum_gate_context(
            session_momentum={
                "available": True,
                "day_gain_pct": 0.5,
                "day_range_position": 0.42,
                "day_high_distance_pct": 3.2,
                "confirmed": False,
                "failed_drive": True,
            }
        )

        action = engine._action_from_context("ENTERO", 0.52, {}, context, {})

        self.assertEqual(action, "HOLD")
        failed = {item["gate"] for item in context["decision_gate_context"]["failed_gates"]}
        self.assertIn("session_momentum_gate", failed)

    def test_confirmed_session_momentum_can_pass_broad_momentum_gate(self) -> None:
        engine = StrategyEngine(SimpleNamespace(max_position_pct=0.1), SimpleNamespace(), SimpleNamespace())
        context = _momentum_gate_context(
            session_momentum={
                "available": True,
                "day_gain_pct": 5.4,
                "day_range_position": 0.86,
                "day_high_distance_pct": 0.5,
                "confirmed": True,
                "fast_mover": True,
            }
        )

        action = engine._action_from_context("FASTMOVE", 0.52, {}, context, {})

        self.assertEqual(action, "BUY")

    def test_tomorrow_plan_live_confirmation_lowers_entry_threshold(self) -> None:
        engine = StrategyEngine(SimpleNamespace(max_position_pct=0.1), SimpleNamespace(), SimpleNamespace())
        context = _momentum_gate_context(
            session_momentum={
                "available": True,
                "day_gain_pct": 3.8,
                "day_range_position": 0.82,
                "day_high_distance_pct": 0.7,
                "confirmed": True,
                "fast_mover": True,
            }
        )
        context["tomorrow_plan_context"] = {
            "active": True,
            "plan_date": "2026-05-28",
            "section": "ready_at_open",
            "action": "READY",
            "score": 91,
            "strategy": "prepared_breakout",
        }

        action = engine._action_from_context("READYPLAN", 0.27, {}, context, {})

        plan = context["tomorrow_plan_decision"]
        self.assertEqual(action, "BUY")
        self.assertTrue(plan["eligible_for_entry_boost"])
        self.assertEqual(plan["section"], "ready_at_open")
        self.assertEqual(context["decision_gate_context"]["buy_threshold"], 0.25)

    def test_tomorrow_plan_waits_without_live_confirmation(self) -> None:
        engine = StrategyEngine(SimpleNamespace(max_position_pct=0.1), SimpleNamespace(), SimpleNamespace())
        context = _momentum_gate_context(
            session_momentum={
                "available": True,
                "day_gain_pct": 0.6,
                "day_range_position": 0.52,
                "day_high_distance_pct": 2.4,
                "confirmed": False,
                "fast_mover": False,
            }
        )
        context["best_strategy"] = {"name": "volume_price_accumulation", "score": 0.70}
        context["full_spectrum_analysis"]["session_momentum"] = {
            "available": True,
            "confirmed": False,
            "fast_mover": False,
        }
        context["full_spectrum_analysis"]["breakout_quality"] = {
            "breakout_quality": "not_breakout",
            "two_day_rule_failed": False,
            "volume_confirmation": False,
            "volume_expansion": False,
        }
        context["full_spectrum_analysis"]["strategy_logic_filters"]["breakout_volume"] = {
            "volume_confirmed": False,
            "confirmed": False,
            "suspect_without_volume": False,
        }
        context["tomorrow_plan_context"] = {
            "active": True,
            "plan_date": "2026-05-28",
            "section": "ready_at_open",
            "action": "READY",
            "score": 91,
            "strategy": "prepared_breakout",
        }

        action = engine._action_from_context("WAITPLAN", 0.27, {}, context, {})

        plan = context["tomorrow_plan_decision"]
        self.assertEqual(action, "HOLD")
        self.assertFalse(plan["eligible_for_entry_boost"])
        self.assertEqual(plan["reason"], "waiting_for_live_confirmation")
        self.assertEqual(context["decision_gate_context"]["buy_threshold"], 0.30)

    def test_monthly_expiry_eve_allows_confirmed_probe_size_buy(self) -> None:
        engine = StrategyEngine(SimpleNamespace(max_position_pct=0.1), SimpleNamespace(), SimpleNamespace())
        candles = _trend_candles(volume_spike=True)
        quote = Quote(
            "EXPIRYPROBE",
            candles[-1].close,
            "upstox-live",
            utc_now(),
            open=candles[-1].open,
            high=candles[-1].high,
            low=candles[-1].low,
            volume=candles[-1].volume,
        )
        macro_event = {
            "enabled": True,
            "date": "2026-05-27",
            "symbol": "EXPIRYPROBE",
            "is_expiry_day": False,
            "is_monthly_expiry_day": False,
            "is_monthly_expiry_eve": True,
            "expiry_type": None,
            "event_risk_score": 0.35,
            "recommended_action": "reduce_size",
        }
        pre_filter = engine._pre_filter_context(
            "EXPIRYPROBE",
            {},
            quote,
            candles,
            {},
            {"available": True, "delivery_score": 0.8, "net_bias": "accumulation", "source": "unit-test"},
            {"breadth_regime": "bull_confirmed"},
            {},
            macro_event,
        )
        context = _momentum_gate_context(
            session_momentum={
                "available": True,
                "day_gain_pct": 5.4,
                "day_range_position": 0.86,
                "day_high_distance_pct": 0.5,
                "confirmed": True,
                "fast_mover": True,
            }
        )
        context["macro_event_context"] = macro_event
        context["pre_filter"] = pre_filter

        action = engine._action_from_context("EXPIRYPROBE", 0.52, {}, context, {})

        self.assertEqual(action, "BUY")
        self.assertFalse(pre_filter["buy_blocked"])
        self.assertEqual(pre_filter["buy_threshold"], 0.40)
        self.assertEqual(macro_event["expiry_risk_policy"], "probe_size_only")
        self.assertEqual(context["decision_gate_context"]["blocking_failed_gates"], [])
        self.assertLessEqual(context["sizing_grade"]["final_multiplier"], 0.35)

    def test_monthly_expiry_day_still_blocks_fresh_buy(self) -> None:
        engine = StrategyEngine(SimpleNamespace(max_position_pct=0.1), SimpleNamespace(), SimpleNamespace())
        candles = _trend_candles(volume_spike=True)
        quote = Quote(
            "EXPIRYDAY",
            candles[-1].close,
            "upstox-live",
            utc_now(),
            open=candles[-1].open,
            high=candles[-1].high,
            low=candles[-1].low,
            volume=candles[-1].volume,
        )
        macro_event = {
            "enabled": True,
            "date": "2026-05-28",
            "symbol": "EXPIRYDAY",
            "is_expiry_day": True,
            "is_monthly_expiry_day": True,
            "is_monthly_expiry_eve": False,
            "expiry_type": "monthly",
            "event_risk_score": 0.4,
            "recommended_action": "reduce_size",
        }
        pre_filter = engine._pre_filter_context(
            "EXPIRYDAY",
            {},
            quote,
            candles,
            {},
            {"available": True, "delivery_score": 0.8, "net_bias": "accumulation", "source": "unit-test"},
            {"breadth_regime": "bull_confirmed"},
            {},
            macro_event,
        )
        context = _momentum_gate_context(
            session_momentum={
                "available": True,
                "day_gain_pct": 5.4,
                "day_range_position": 0.86,
                "day_high_distance_pct": 0.5,
                "confirmed": True,
                "fast_mover": True,
            }
        )
        context["macro_event_context"] = macro_event
        context["pre_filter"] = pre_filter

        action = engine._action_from_context("EXPIRYDAY", 0.52, {}, context, {})

        self.assertEqual(action, "HOLD")
        self.assertTrue(pre_filter["buy_blocked"])
        self.assertEqual(pre_filter["elimination_reason"], "monthly_expiry_no_new_longs")
        failed = {item["gate"] for item in context["decision_gate_context"]["failed_gates"]}
        self.assertIn("macro_calendar_gate", failed)

    def test_late_intraday_momentum_is_detected_but_not_auto_buy(self) -> None:
        engine = StrategyEngine(SimpleNamespace(max_position_pct=0.1), SimpleNamespace(), SimpleNamespace())
        context = _momentum_gate_context(
            session_momentum={
                "available": True,
                "day_gain_pct": 8.4,
                "day_range_position": 0.90,
                "day_high_distance_pct": 0.6,
                "confirmed": True,
                "fast_mover": True,
            }
        )
        context["best_strategy"] = {"name": "volume_price_accumulation", "score": 0.42}
        context["opportunity_scan"] = {
            "setup": "extended_momentum_watch",
            "day_gain_pct": 8.4,
            "day_range_position": 0.90,
            "day_high_distance_pct": 0.6,
            "volume_ratio": 4.2,
            "turnover": 900_000_000,
            "components": {"live_momentum": 0.95},
        }

        engine._apply_live_momentum_strategy(context)
        action = engine._action_from_context("CHASE", 0.55, {}, context, {})

        self.assertEqual(action, "HOLD")
        self.assertEqual(context["full_spectrum_analysis"]["live_momentum_review"]["reason"], "late chase blocked; wait for pullback")

    def test_market_action_breakout_can_become_rule_based_buy_strategy(self) -> None:
        engine = StrategyEngine(
            SimpleNamespace(max_position_pct=0.1, dynamic_scan_min_turnover_inr=50_000_000),
            SimpleNamespace(),
            SimpleNamespace(),
        )
        context = _momentum_gate_context(
            session_momentum={
                "available": True,
                "day_gain_pct": 4.8,
                "day_range_position": 0.84,
                "day_high_distance_pct": 0.4,
                "confirmed": True,
                "fast_mover": True,
            }
        )
        context["best_strategy"] = {"name": "volume_price_accumulation", "score": 0.42}
        context["opportunity_scan"] = {
            "setup": "52_week_high_volume_breakout",
            "day_gain_pct": 4.8,
            "day_range_position": 0.84,
            "day_high_distance_pct": 0.4,
            "volume_ratio": 2.6,
            "turnover": 240_000_000,
            "components": {"live_momentum": 0.82},
        }

        engine._apply_live_momentum_strategy(context)

        review = context["full_spectrum_analysis"]["live_momentum_review"]
        self.assertTrue(review["market_action_breakout_ready"])
        self.assertEqual(context["best_strategy"]["name"], "52_week_high_volume_breakout")

    def test_opportunity_probe_can_buy_without_institutional_scorecard_master_gate(self) -> None:
        engine = StrategyEngine(
            SimpleNamespace(max_position_pct=0.1, dynamic_scan_min_turnover_inr=50_000_000),
            SimpleNamespace(),
            SimpleNamespace(),
        )
        context = _momentum_gate_context(
            session_momentum={
                "available": True,
                "day_gain_pct": 4.8,
                "day_range_position": 0.84,
                "day_high_distance_pct": 0.4,
                "confirmed": True,
                "fast_mover": True,
            }
        )
        context["full_spectrum_analysis"]["institutional_scorecard"]["buy_ready"] = False
        context["best_strategy"] = {"name": "volume_price_accumulation", "score": 0.42}
        context["opportunity_scan"] = {
            "setup": "top_gainer_momentum",
            "bucket": "Actionable",
            "score": 0.88,
            "day_gain_pct": 4.8,
            "day_range_position": 0.84,
            "day_high_distance_pct": 0.4,
            "volume_ratio": 2.6,
            "turnover": 240_000_000,
            "components": {"live_momentum": 0.82},
            "data_quality": {"actionable_data_ready": True},
        }

        engine._apply_live_momentum_strategy(context)
        action = engine._action_from_context("OPPROBE", 0.24, {}, context, {})

        self.assertEqual(action, "BUY")
        self.assertTrue(context["decision_gate_context"]["opportunity_probe"]["ready"])
        self.assertFalse(context["full_spectrum_analysis"]["institutional_scorecard"]["buy_ready"])

    def test_opportunity_probe_wait_for_pullback_never_becomes_buy(self) -> None:
        engine = StrategyEngine(
            SimpleNamespace(max_position_pct=0.1, dynamic_scan_min_turnover_inr=50_000_000),
            SimpleNamespace(),
            SimpleNamespace(),
        )
        context = _momentum_gate_context(
            session_momentum={
                "available": True,
                "day_gain_pct": 5.2,
                "day_range_position": 0.84,
                "day_high_distance_pct": 0.4,
                "confirmed": True,
                "fast_mover": True,
            }
        )
        context["full_spectrum_analysis"]["institutional_scorecard"]["buy_ready"] = False
        context["opportunity_scan"] = {
            "setup": "52_week_high_volume_breakout",
            "bucket": "Actionable",
            "trade_window": "wait_for_pullback",
            "score": 0.88,
            "day_gain_pct": 5.2,
            "day_range_position": 0.84,
            "day_high_distance_pct": 0.4,
            "volume_ratio": 2.6,
            "turnover": 260_000_000,
            "components": {"live_momentum": 0.82},
            "data_quality": {"actionable_data_ready": True},
        }

        engine._apply_live_momentum_strategy(context)
        action = engine._action_from_context("PULLWAIT", 0.42, {}, context, {})

        self.assertEqual(action, "HOLD")
        self.assertFalse(context["decision_gate_context"]["opportunity_probe"]["ready"])
        self.assertIn("opportunity_scan_entry_window", {gate["gate"] for gate in context["decision_gate_context"]["blocking_failed_gates"]})

    def test_top_gainers_playbook_late_low_score_stays_hold(self) -> None:
        engine = StrategyEngine(
            SimpleNamespace(max_position_pct=0.1, dynamic_scan_min_turnover_inr=50_000_000),
            SimpleNamespace(),
            SimpleNamespace(),
        )
        context = _momentum_gate_context(
            session_momentum={
                "available": True,
                "day_gain_pct": 8.2,
                "confirmed": False,
                "fast_mover": True,
            }
        )
        context["full_spectrum_analysis"]["institutional_scorecard"]["buy_ready"] = False
        context["full_spectrum_analysis"]["institutional_scorecard"]["total_score"] = 40
        context["full_spectrum_analysis"]["institutional_scorecard"]["score"] = 40
        context["full_spectrum_analysis"]["confluence_score"]["total"] = 12
        context["full_spectrum_analysis"]["stage_analysis"] = {"stage": "Stage 1", "buy_permitted": False}
        context["full_spectrum_analysis"]["entry_quality"] = {"entry_grade": "D", "distance_from_pivot_pct": 29.6}
        context["full_spectrum_analysis"]["risk_overrides"] = {
            "flags": [
                "price_extended_from_pivot",
                "scorecard_possible_circuit_risk_no_new_longs",
                "scorecard_extreme_atr_volatility_no_new_longs",
            ],
            "no_new_longs": True,
        }
        context["full_spectrum_analysis"]["live_momentum_review"] = {
            "setup": "market_action_momentum",
            "late_chase": True,
            "strategy_ready": False,
            "day_gain_pct": 8.2,
            "volume_ratio": 3.2,
            "reason": "late chase blocked; wait for pullback",
        }
        context["opportunity_scan"] = {
            "setup": "earnings_beat_gap_and_go",
            "bucket": "Small Size Only",
            "score": 1.0,
            "data_quality": {"actionable_data_ready": True},
            "top_gainers_playbook": {
                "available": True,
                "market_region": "US",
                "final_signal": "MODERATE BUY",
                "quant_score": 62,
                "hard_excluded": False,
                "hard_excludes": [],
                "anti_patterns": [],
                "cmp": 108.0,
                "levels": {
                    "pivot": 105.0,
                    "entry": 108.0,
                    "max_entry": 110.25,
                    "stop": 100.44,
                    "target1": 129.6,
                },
                "catalyst_review": {"catalyst_confirmed": True, "catalyst_strength": "MODERATE"},
            },
        }

        action = engine._action_from_context("PLAYBUY", 0.02, {}, context, {})

        probe = context["decision_gate_context"]["opportunity_probe"]
        self.assertEqual(action, "HOLD")
        self.assertFalse(probe["ready"])
        self.assertEqual(probe["reason"], "top_gainers_playbook_quant_below_signal_floor")

    def test_top_gainers_playbook_chasing_stays_hold(self) -> None:
        engine = StrategyEngine(
            SimpleNamespace(max_position_pct=0.1, dynamic_scan_min_turnover_inr=50_000_000),
            SimpleNamespace(),
            SimpleNamespace(),
        )
        context = _momentum_gate_context(
            session_momentum={
                "available": True,
                "day_gain_pct": 9.0,
                "confirmed": False,
                "fast_mover": True,
            }
        )
        context["full_spectrum_analysis"]["institutional_scorecard"]["buy_ready"] = False
        context["opportunity_scan"] = {
            "setup": "earnings_beat_gap_and_go",
            "bucket": "Small Size Only",
            "score": 1.0,
            "data_quality": {"actionable_data_ready": True},
            "top_gainers_playbook": {
                "available": True,
                "final_signal": "MODERATE BUY",
                "quant_score": 62,
                "hard_excluded": False,
                "hard_excludes": [],
                "anti_patterns": [{"code": "CHASING"}],
                "cmp": 118.0,
                "levels": {
                    "pivot": 105.0,
                    "entry": 105.0,
                    "max_entry": 110.25,
                    "stop": 97.65,
                    "target1": 126.0,
                },
                "catalyst_review": {"catalyst_confirmed": True, "catalyst_strength": "MODERATE"},
            },
        }

        action = engine._action_from_context("PLAYCHASE", 0.50, {}, context, {})

        self.assertEqual(action, "HOLD")
        self.assertFalse(context["decision_gate_context"]["opportunity_probe"]["ready"])

    def test_top_gainers_playbook_mtf_d_needs_playbook_stage2(self) -> None:
        engine = StrategyEngine(
            SimpleNamespace(max_position_pct=0.1, dynamic_scan_min_turnover_inr=50_000_000),
            SimpleNamespace(),
            SimpleNamespace(),
        )
        context = _momentum_gate_context(
            session_momentum={
                "available": True,
                "day_gain_pct": 6.1,
                "confirmed": False,
                "fast_mover": True,
            }
        )
        context["full_spectrum_analysis"]["trend_context"]["timeframe_alignment"] = {"alignment_grade": "D"}
        context["full_spectrum_analysis"]["institutional_scorecard"]["buy_ready"] = False
        context["opportunity_scan"] = {
            "setup": "earnings_beat_gap_and_go",
            "bucket": "Small Size Only",
            "score": 1.0,
            "data_quality": {"actionable_data_ready": True},
            "top_gainers_playbook": {
                "available": True,
                "market_region": "US",
                "final_signal": "MODERATE BUY",
                "quant_score": 66,
                "hard_excluded": False,
                "hard_excludes": [],
                "anti_patterns": [],
                "cmp": 108.0,
                "volume": 8_000_000,
                "volume_ratio": 2.2,
                "weinstein": {"stage": "Stage 1"},
                "levels": {
                    "pivot": 105.0,
                    "entry": 108.0,
                    "max_entry": 110.25,
                    "stop": 100.44,
                    "target1": 129.6,
                },
                "catalyst_review": {"catalyst_confirmed": True, "catalyst_strength": "MODERATE"},
            },
        }

        action = engine._action_from_context("PLAYMTF", 0.02, {}, context, {})

        self.assertEqual(action, "HOLD")
        blocking = {gate["gate"] for gate in context["decision_gate_context"]["blocking_failed_gates"]}
        self.assertIn("system_rule_MTF_HARD_BLOCK", blocking)
        self.assertIn("timeframe_alignment_gate", blocking)

    def test_top_gainers_playbook_stage2_can_absorb_legacy_mtf_conflict(self) -> None:
        engine = StrategyEngine(
            SimpleNamespace(max_position_pct=0.1, dynamic_scan_min_turnover_inr=50_000_000),
            SimpleNamespace(),
            SimpleNamespace(),
        )
        context = _momentum_gate_context(
            session_momentum={
                "available": True,
                "day_gain_pct": 6.1,
                "confirmed": False,
                "fast_mover": True,
            }
        )
        context["full_spectrum_analysis"]["trend_context"]["timeframe_alignment"] = {"alignment_grade": "D"}
        context["full_spectrum_analysis"]["institutional_scorecard"]["buy_ready"] = False
        context["opportunity_scan"] = {
            "setup": "earnings_beat_gap_and_go",
            "bucket": "Small Size Only",
            "score": 1.0,
            "data_quality": {"actionable_data_ready": True},
            "top_gainers_playbook": {
                "available": True,
                "market_region": "US",
                "final_signal": "MODERATE BUY",
                "quant_score": 66,
                "hard_excluded": False,
                "hard_excludes": [],
                "anti_patterns": [],
                "cmp": 108.0,
                "volume": 8_000_000,
                "volume_ratio": 2.2,
                "weinstein": {"stage": "Stage 2"},
                "levels": {
                    "pivot": 105.0,
                    "entry": 108.0,
                    "max_entry": 110.25,
                    "stop": 100.44,
                    "target1": 129.6,
                },
                "catalyst_review": {"catalyst_confirmed": True, "catalyst_strength": "MODERATE"},
            },
        }

        action = engine._action_from_context("PLAYMTF2", 0.02, {}, context, {})

        self.assertEqual(action, "HOLD")
        self.assertTrue(context["decision_gate_context"]["blocking_failed_gates"])

    def test_top_gainers_playbook_suspect_breakout_without_volume_stays_hold(self) -> None:
        engine = StrategyEngine(
            SimpleNamespace(max_position_pct=0.1, dynamic_scan_min_turnover_inr=50_000_000),
            SimpleNamespace(),
            SimpleNamespace(),
        )
        context = _momentum_gate_context(
            session_momentum={
                "available": True,
                "day_gain_pct": 6.1,
                "confirmed": True,
                "fast_mover": True,
            }
        )
        context["full_spectrum_analysis"]["strategy_logic_filters"]["breakout_volume"] = {
            "suspect_without_volume": True,
            "volume_confirmed": False,
        }
        context["opportunity_scan"] = {
            "setup": "earnings_beat_gap_and_go",
            "bucket": "Small Size Only",
            "score": 1.0,
            "data_quality": {"actionable_data_ready": True},
            "top_gainers_playbook": {
                "available": True,
                "market_region": "US",
                "final_signal": "MODERATE BUY",
                "quant_score": 66,
                "hard_excluded": False,
                "hard_excludes": [],
                "anti_patterns": [],
                "cmp": 108.0,
                "volume": 8_000_000,
                "volume_ratio": 2.2,
                "weinstein": {"stage": "Stage 2"},
                "levels": {
                    "pivot": 105.0,
                    "entry": 108.0,
                    "max_entry": 110.25,
                    "stop": 100.44,
                    "target1": 129.6,
                },
                "catalyst_review": {"catalyst_confirmed": True, "catalyst_strength": "MODERATE"},
            },
        }

        action = engine._action_from_context("PLAYVOLUME", 0.30, {}, context, {})

        self.assertEqual(action, "HOLD")
        blocking = {gate["gate"] for gate in context["decision_gate_context"]["blocking_failed_gates"]}
        self.assertIn("breakout_volume_gate", blocking)

    def test_us_yahoo_playbook_can_buy_as_reduced_reference_mode(self) -> None:
        engine = StrategyEngine(
            SimpleNamespace(max_position_pct=0.1, dynamic_scan_min_turnover_inr=50_000_000),
            SimpleNamespace(),
            SimpleNamespace(),
        )
        context = _momentum_gate_context(
            session_momentum={
                "available": True,
                "day_gain_pct": 6.1,
                "confirmed": False,
                "fast_mover": True,
            }
        )
        context["market_region"] = "US"
        context["quote"]["source"] = "yahoo-delayed"
        context["data_readiness"] = {
            "market_region": "US",
            "trade_decision_ready": False,
            "grade": "C",
            "hard_gaps": [
                {"key": "us_realtime_quote", "label": "US consolidated real-time quote"},
                {"key": "us_minute_bars", "label": "US minute bars"},
            ],
            "sources": {"quote": "yahoo-delayed", "daily": "yahoo-delayed"},
        }
        context["full_spectrum_analysis"]["institutional_scorecard"]["buy_ready"] = False
        context["opportunity_scan"] = {
            "setup": "earnings_beat_gap_and_go",
            "bucket": "Small Size Only",
            "score": 1.0,
            "data_quality": {"actionable_data_ready": True},
            "top_gainers_playbook": {
                "available": True,
                "market_region": "US",
                "final_signal": "STRONG BUY",
                "quant_score": 74,
                "hard_excluded": False,
                "hard_excludes": [],
                "anti_patterns": [],
                "cmp": 108.0,
                "volume": 8_000_000,
                "volume_ratio": 2.6,
                "weinstein": {"stage": "Stage 2"},
                "levels": {
                    "pivot": 105.0,
                    "entry": 108.0,
                    "max_entry": 110.25,
                    "stop": 100.44,
                    "target1": 129.6,
                },
                "catalyst_review": {"catalyst_confirmed": True, "catalyst_strength": "STRONG"},
            },
        }

        action = engine._action_from_context("PLAYYHOO", 0.02, {}, context, {})

        probe = context["decision_gate_context"]["opportunity_probe"]
        self.assertEqual(action, "BUY")
        self.assertEqual(probe["data_quality_override"], "us_yahoo_reference_reduced_size")
        self.assertEqual(context["decision_gate_context"]["blocking_failed_gates"], [])

    def test_auto_follow_quality_gate_blocks_low_score_us_playbook_reference_only(self) -> None:
        gate = auto_follow_quality_gate(
            {
                "symbol": "PLAYYHOO",
                "signal_type": "BUY",
                "action": "BUY",
                "latest_price": 108.0,
                "risk_flags": [
                    "scorecard_possible_circuit_risk_no_new_longs",
                    "scorecard_extreme_atr_volatility_no_new_longs",
                ],
                "details": {
                    "action": "BUY",
                    "overall_score_pct": 45,
                    "overall_grade": "D",
                    "confluence": 0,
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
                            "volume": 8_000_000,
                            "volume_ratio": 2.6,
                            "weinstein": {"stage": "Stage 2"},
                            "levels": {
                                "entry": 108.0,
                                "max_entry": 110.25,
                                "stop": 100.44,
                            },
                            "catalyst_review": {"catalyst_confirmed": True, "catalyst_strength": "STRONG"},
                        },
                    },
                    "targets": [{"label": "T1", "distance_pct": 12.0, "probability": "likely"}],
                    "stop_status": {"price": 100.44},
                },
            }
        )

        self.assertFalse(gate["passed"])
        self.assertEqual(gate["reason"], "overall_score_below_70")

    def test_live_confirmed_probe_absorbs_stale_intraday_marker(self) -> None:
        engine = StrategyEngine(
            SimpleNamespace(max_position_pct=0.1, dynamic_scan_min_turnover_inr=50_000_000),
            SimpleNamespace(),
            SimpleNamespace(),
        )
        context = _momentum_gate_context(
            session_momentum={
                "available": True,
                "day_gain_pct": 3.5,
                "day_range_position": 0.92,
                "day_high_distance_pct": 0.3,
                "confirmed": True,
                "fast_mover": True,
            }
        )
        context["full_spectrum_analysis"]["institutional_scorecard"]["buy_ready"] = False
        context["full_spectrum_analysis"]["institutional_scorecard"]["total_score"] = 54
        context["full_spectrum_analysis"]["institutional_scorecard"]["score"] = 54
        context["best_strategy"] = {"name": "volume_price_accumulation", "score": 0.42}
        context["opportunity_scan"] = {
            "setup": "opening_ignition",
            "bucket": "Actionable",
            "score": 0.84,
            "day_gain_pct": 3.5,
            "day_range_position": 0.92,
            "day_high_distance_pct": 0.3,
            "volume_ratio": 0.45,
            "projected_volume_ratio": 2.5,
            "components": {"live_momentum": 0.66},
            "data_quality": {"actionable_data_ready": False, "missing": ["stale_intraday_candles"]},
        }

        engine._apply_live_momentum_strategy(context)
        action = engine._action_from_context("LIVEPROBE", 0.24, {}, context, {})

        probe = context["decision_gate_context"]["opportunity_probe"]
        self.assertEqual(action, "BUY")
        self.assertTrue(probe["ready"])
        self.assertEqual(probe["source"], "live_momentum_review")
        self.assertEqual(probe["data_quality_override"], "live_momentum_review_with_trade_ready_data")
        self.assertIn("fresh_market_data_gate", {gate["gate"] for gate in context["decision_gate_context"]["failed_gates"]})
        self.assertIn("fresh_market_data_gate", {gate["gate"] for gate in probe["absorbed_gates"]})
        self.assertEqual(context["decision_gate_context"]["blocking_failed_gates"], [])

    def test_live_intraday_probe_uses_starter_confluence_floor(self) -> None:
        engine = StrategyEngine(
            SimpleNamespace(max_position_pct=0.1, dynamic_scan_min_turnover_inr=50_000_000),
            SimpleNamespace(),
            SimpleNamespace(),
        )
        context = _momentum_gate_context(
            session_momentum={
                "available": True,
                "day_gain_pct": 3.0,
                "day_range_position": 0.86,
                "day_high_distance_pct": 0.5,
                "confirmed": True,
                "fast_mover": True,
            }
        )
        context["full_spectrum_analysis"]["confluence_score"] = {"total": 6.0, "tier": "NO_SIGNAL"}
        context["full_spectrum_analysis"]["institutional_scorecard"]["buy_ready"] = False
        context["full_spectrum_analysis"]["institutional_scorecard"]["total_score"] = 48
        context["full_spectrum_analysis"]["institutional_scorecard"]["score"] = 48
        context["full_spectrum_analysis"]["risk_overrides"] = {
            "flags": ["confluence_below_watch_threshold", "institutional_scorecard_below_entry_threshold"],
            "no_new_longs": True,
        }
        context["best_strategy"] = {"name": "live_intraday_momentum", "score": 0.84}
        context["opportunity_scan"] = {
            "setup": "intraday_momentum",
            "bucket": "Actionable",
            "score": 0.96,
            "day_gain_pct": 3.0,
            "day_range_position": 0.86,
            "day_high_distance_pct": 0.5,
            "volume_ratio": 1.8,
            "turnover": 220_000_000,
            "components": {"live_momentum": 0.82},
            "data_quality": {"actionable_data_ready": False, "missing": ["stale_intraday_candles"]},
        }

        engine._apply_live_momentum_strategy(context)
        action = engine._action_from_context("STARTER", 0.24, {}, context, {})

        probe = context["decision_gate_context"]["opportunity_probe"]
        self.assertEqual(action, "BUY")
        self.assertTrue(probe["ready"])
        self.assertEqual(probe["min_confluence"], 6.0)
        self.assertEqual(context["decision_gate_context"]["blocking_failed_gates"], [])

    def test_scan_probe_uses_live_quote_ohlcv_when_only_intraday_candles_are_stale(self) -> None:
        engine = StrategyEngine(
            SimpleNamespace(max_position_pct=0.1, dynamic_scan_min_turnover_inr=50_000_000),
            SimpleNamespace(),
            SimpleNamespace(),
        )
        context = _momentum_gate_context(
            session_momentum={
                "available": True,
                "day_gain_pct": 3.2,
                "day_range_position": 0.82,
                "day_high_distance_pct": 0.7,
                "confirmed": True,
                "fast_mover": True,
            }
        )
        context["data_readiness"]["trade_decision_ready"] = False
        context["data_readiness"]["hard_gaps"] = [
            {"key": "in_intraday_candles", "label": "India intraday candles", "source": "upstox-live"}
        ]
        context["full_spectrum_analysis"]["institutional_scorecard"]["buy_ready"] = False
        context["full_spectrum_analysis"]["risk_overrides"] = {
            "flags": [
                "institutional_scorecard_below_entry_threshold",
                "phase3_weak_volume_ratio_reduce_size",
                "false_breakout_risk_no_new_longs",
            ],
            "no_new_longs": True,
        }
        context["opportunity_scan"] = {
            "setup": "top_gainer_momentum",
            "bucket": "Actionable",
            "score": 0.84,
            "day_gain_pct": 3.2,
            "day_range_position": 0.82,
            "day_high_distance_pct": 0.7,
            "volume_ratio": 1.4,
            "turnover": 240_000_000,
            "components": {"live_momentum": 0.74},
            "data_quality": {"actionable_data_ready": False, "missing": ["stale_intraday_candles"]},
        }

        action = engine._action_from_context("LIVEQUOTE", 0.24, {}, context, {})

        probe = context["decision_gate_context"]["opportunity_probe"]
        self.assertEqual(action, "BUY")
        self.assertTrue(probe["ready"])
        self.assertEqual(probe["source"], "live_quote_opportunity_scan")
        self.assertEqual(probe["data_quality_override"], "live_quote_ohlcv_used_for_probe")
        self.assertIn("fresh_market_data_gate", {gate["gate"] for gate in probe["absorbed_gates"]})
        self.assertEqual(context["decision_gate_context"]["blocking_failed_gates"], [])

    def test_high_score_scan_probe_absorbs_watch_grade_without_hiding_hard_risks(self) -> None:
        engine = StrategyEngine(
            SimpleNamespace(max_position_pct=0.1, dynamic_scan_min_turnover_inr=50_000_000),
            SimpleNamespace(),
            SimpleNamespace(),
        )
        context = _momentum_gate_context(
            session_momentum={
                "available": True,
                "day_gain_pct": 4.1,
                "day_range_position": 0.86,
                "day_high_distance_pct": 0.4,
                "confirmed": True,
                "fast_mover": True,
            }
        )
        context["sentiment"] = {"score": 0.0, "confidence": 0.0, "status": "DATA_MISSING"}
        context["full_spectrum_analysis"]["institutional_scorecard"]["buy_ready"] = False
        context["full_spectrum_analysis"]["institutional_scorecard"]["total_score"] = 42
        context["full_spectrum_analysis"]["institutional_scorecard"]["score"] = 42
        context["full_spectrum_analysis"]["entry_quality"] = {
            "entry_grade": "WATCH",
            "distance_from_pivot_pct": 1.8,
            "volume_confirmation": True,
        }
        context["opportunity_scan"] = {
            "setup": "52_week_high_volume_breakout",
            "bucket": "Actionable",
            "score": 0.86,
            "day_gain_pct": 4.1,
            "day_range_position": 0.86,
            "day_high_distance_pct": 0.4,
            "volume_ratio": 2.4,
            "turnover": 260_000_000,
            "components": {"live_momentum": 0.78},
            "data_quality": {"actionable_data_ready": True},
        }

        action = engine._action_from_context("GRADEPROBE", 0.24, {}, context, {})

        probe = context["decision_gate_context"]["opportunity_probe"]
        self.assertEqual(action, "BUY")
        self.assertTrue(probe["ready"])
        self.assertEqual(context["decision_gate_context"]["blocking_failed_gates"], [])
        self.assertFalse(context["system_gate_audit"]["hard_blocked"])
        self.assertNotIn("GRADE_VIOLATION", context["system_gate_audit"]["active_flags"])
        self.assertGreaterEqual(context["system_gate_audit"]["overall_score_pct"], 86.0)

    def test_scan_probe_still_blocks_hard_risk_flags(self) -> None:
        engine = StrategyEngine(
            SimpleNamespace(max_position_pct=0.1, dynamic_scan_min_turnover_inr=50_000_000),
            SimpleNamespace(),
            SimpleNamespace(),
        )
        context = _momentum_gate_context(
            session_momentum={
                "available": True,
                "day_gain_pct": 3.2,
                "day_range_position": 0.82,
                "day_high_distance_pct": 0.7,
                "confirmed": True,
                "fast_mover": True,
            }
        )
        context["full_spectrum_analysis"]["institutional_scorecard"]["buy_ready"] = False
        context["full_spectrum_analysis"]["risk_overrides"] = {
            "flags": ["scorecard_asm_surveillance_no_new_longs"],
            "no_new_longs": True,
        }
        context["opportunity_scan"] = {
            "setup": "top_gainer_momentum",
            "bucket": "Actionable",
            "score": 0.84,
            "day_gain_pct": 3.2,
            "day_range_position": 0.82,
            "day_high_distance_pct": 0.7,
            "volume_ratio": 1.4,
            "turnover": 240_000_000,
            "components": {"live_momentum": 0.74},
            "data_quality": {"actionable_data_ready": True},
        }

        action = engine._action_from_context("HARDSTOP", 0.24, {}, context, {})

        self.assertEqual(action, "HOLD")
        blocking = context["decision_gate_context"]["blocking_failed_gates"]
        self.assertEqual([gate["gate"] for gate in blocking], ["risk_overrides"])

    def test_live_confirmed_probe_still_respects_phase2_data_readiness(self) -> None:
        engine = StrategyEngine(
            SimpleNamespace(max_position_pct=0.1, dynamic_scan_min_turnover_inr=50_000_000),
            SimpleNamespace(),
            SimpleNamespace(),
        )
        context = _momentum_gate_context(
            session_momentum={
                "available": True,
                "day_gain_pct": 3.5,
                "day_range_position": 0.92,
                "day_high_distance_pct": 0.3,
                "confirmed": True,
                "fast_mover": True,
            }
        )
        context["data_readiness"]["trade_decision_ready"] = False
        context["best_strategy"] = {"name": "volume_price_accumulation", "score": 0.42}
        context["opportunity_scan"] = {
            "setup": "opening_ignition",
            "bucket": "Actionable",
            "score": 0.84,
            "day_gain_pct": 3.5,
            "day_range_position": 0.92,
            "day_high_distance_pct": 0.3,
            "components": {"live_momentum": 0.66},
            "data_quality": {"actionable_data_ready": False, "missing": ["stale_quote"]},
        }

        engine._apply_live_momentum_strategy(context)
        action = engine._action_from_context("LIVEPROBE", 0.24, {}, context, {})

        self.assertEqual(action, "HOLD")
        self.assertFalse(context["decision_gate_context"]["opportunity_probe"]["ready"])

    def test_btst_candidate_can_become_buy_action(self) -> None:
        engine = StrategyEngine(
            SimpleNamespace(max_position_pct=0.1, dynamic_scan_min_turnover_inr=50_000_000),
            SimpleNamespace(),
            SimpleNamespace(),
        )
        context = _momentum_gate_context(
            session_momentum={"available": True, "confirmed": False, "fast_mover": False}
        )
        context["best_strategy"] = {"name": "volume_price_accumulation", "score": 0.40}
        context["full_spectrum_analysis"]["institutional_scorecard"]["buy_ready"] = False
        context["full_spectrum_analysis"]["institutional_scorecard"]["total_score"] = 42
        context["full_spectrum_analysis"]["institutional_scorecard"]["score"] = 42
        context["opportunity_scan"] = _btst_scan_payload()

        action = engine._action_from_context("BTSTBUY", 0.19, {}, context, {})

        probe = context["decision_gate_context"]["opportunity_probe"]
        self.assertEqual(action, "BUY")
        self.assertTrue(probe["ready"])
        self.assertEqual(probe["source"], "btst_buy_candidate")
        self.assertEqual(probe["size_policy"], "btst_guarded_buy")

    def test_us_btst_reference_data_can_become_guarded_paper_buy(self) -> None:
        engine = StrategyEngine(
            SimpleNamespace(max_position_pct=0.1, dynamic_scan_min_turnover_inr=50_000_000),
            SimpleNamespace(),
            SimpleNamespace(),
        )
        context = _momentum_gate_context(
            session_momentum={"available": True, "confirmed": False, "fast_mover": False}
        )
        context["market_region"] = "US"
        context["quote"].update({"source": "yahoo-delayed"})
        context["data_readiness"] = {
            "market_region": "US",
            "trade_decision_ready": True,
            "grade": "B",
            "hard_gaps": [],
            "soft_gaps": [{"key": "us_consolidated_tape"}],
            "sources": {"quote": "yahoo-delayed", "intraday": "alpaca-iex-live:1minute"},
            "fresh_market_data_gate": {"passed": True, "reason": "current_session_data"},
        }
        context["best_strategy"] = {"name": "volume_price_accumulation", "score": 0.40}
        context["full_spectrum_analysis"]["institutional_scorecard"]["buy_ready"] = False
        context["full_spectrum_analysis"]["institutional_scorecard"]["total_score"] = 42
        context["full_spectrum_analysis"]["institutional_scorecard"]["score"] = 42
        scan = _btst_scan_payload()
        scan["score"] = 0.95
        scan["data_quality"] = {
            "actionable_data_ready": False,
            "missing": ["us_realtime_intraday_for_actionable_trade"],
        }
        scan["btst"]["score"] = 0.95
        context["opportunity_scan"] = scan

        action = engine._action_from_context("USBTST", 0.19, {}, context, {})

        probe = context["decision_gate_context"]["opportunity_probe"]
        self.assertEqual(action, "BUY")
        self.assertTrue(probe["ready"])
        self.assertEqual(probe["source"], "btst_buy_candidate")
        self.assertEqual(probe["data_quality_override"], "phase2_fresh_reference_data")

    def test_btst_quality_gate_keeps_buy_action_with_guarded_size(self) -> None:
        gate = fresh_buy_quality_gate(
            {
                "signal_type": "BUY",
                "status": "ACTIVE",
                "action": "BUY",
                "overall_score_pct": 72,
                "overall_grade": "B",
                "confluence": 18,
                "details": {
                    "action": "BUY",
                    "latest_price": 108.0,
                    "overall_score_pct": 72,
                    "overall_grade": "B",
                    "setup_score_pct": 76,
                    "data_readiness": {"trade_decision_ready": True, "grade": "A"},
                    "entry_zone": [107.0, 109.0],
                    "stop_loss": 104.0,
                    "targets": [{"label": "BTST-T1", "price": 112.0, "distance_pct": 3.7}],
                    "opportunity_scan": _btst_scan_payload(),
                },
            }
        )

        self.assertTrue(gate["passed"])
        self.assertTrue(gate["opportunity_probe"])
        self.assertGreaterEqual(gate["size_multiplier"], 0.75)

    def test_circuit_demand_lock_does_not_become_rule_based_buy(self) -> None:
        engine = StrategyEngine(
            SimpleNamespace(max_position_pct=0.1, dynamic_scan_min_turnover_inr=50_000_000),
            SimpleNamespace(),
            SimpleNamespace(),
        )
        context = _momentum_gate_context(
            session_momentum={
                "available": True,
                "day_gain_pct": 10.0,
                "day_range_position": 1.0,
                "day_high_distance_pct": 0.0,
                "confirmed": True,
                "fast_mover": True,
            }
        )
        context["best_strategy"] = {"name": "volume_price_accumulation", "score": 0.42}
        context["opportunity_scan"] = {
            "setup": "circuit_demand_lock",
            "day_gain_pct": 10.0,
            "day_range_position": 1.0,
            "day_high_distance_pct": 0.0,
            "volume_ratio": 3.0,
            "turnover": 300_000_000,
            "components": {"live_momentum": 0.92},
        }

        engine._apply_live_momentum_strategy(context)

        review = context["full_spectrum_analysis"]["live_momentum_review"]
        self.assertFalse(review["strategy_ready"])
        self.assertIn("wait for circuit unlock", review["reason"])
        self.assertEqual(context["best_strategy"]["name"], "volume_price_accumulation")

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

    def test_paper_exit_skips_tiny_breakeven_reduce_after_costs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(Path(tmpdir) / "agent.db")
            db.init()
            idea_id = _insert_trade_economics_idea(
                db,
                symbol="SCHNEIDER",
                entry_price=1384.0,
                latest_price=1387.10,
                peak_return_pct=2.32,
                details={"lifecycle_status": "active", "highest_target_hit": "NONE", "stop_loss": 1328.0},
            )
            _insert_trade_economics_follow(
                db,
                idea_id,
                qty=4,
                entry_price=1384.0,
                latest_price=1387.10,
                details={"mark_state": {"peak_return_pct": 2.32, "worst_return_pct": -0.2}},
            )

            result = db.manage_user_follow_exits(1, cost_settings=_economics_settings())
            [follow] = db.user_followed_signal_ideas(1, 10)

        self.assertEqual(result["action_count"], 0)
        self.assertEqual(result["skipped_count"], 1)
        self.assertEqual(result["skipped"][0]["reason"].split(":")[0], "Skipped low-value profit exit")
        self.assertEqual(follow["qty"], 4)
        self.assertEqual(follow["follow_status"], "ACTIVE")

    def test_paper_exit_allows_economic_target_partial(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(Path(tmpdir) / "agent.db")
            db.init()
            idea_id = _insert_trade_economics_idea(
                db,
                symbol="BIGWIN",
                entry_price=100.0,
                latest_price=110.0,
                details={
                    "lifecycle_status": "target_1_hit",
                    "highest_target_hit": "T1",
                    "stop_loss": 96.0,
                    "target_status": [{"label": "T1", "hit": True, "suggested_exit_pct": 35}],
                },
            )
            _insert_trade_economics_follow(db, idea_id, qty=100, entry_price=100.0, latest_price=110.0)

            result = db.manage_user_follow_exits(1, cost_settings=_economics_settings())
            [follow] = db.user_followed_signal_ideas(1, 10)

        self.assertEqual(result["action_count"], 1)
        self.assertEqual(result["actions"][0]["label"], "Reduce")
        self.assertEqual(result["actions"][0]["exit_qty"], 35)
        self.assertEqual(follow["qty"], 65)

    def test_paper_exit_is_pending_when_market_is_closed(self) -> None:
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
            idea_id = _insert_trade_economics_idea(
                db,
                symbol="FINCABLES",
                entry_price=1152.5,
                latest_price=1177.25,
                details={
                    "lifecycle_status": "target_1_hit",
                    "highest_target_hit": "T1",
                    "stop_loss": 1100.0,
                    "target_status": [{"label": "T1", "hit": True, "suggested_exit_pct": 100}],
                },
            )
            _insert_trade_economics_follow(db, idea_id, qty=5, entry_price=1152.5, latest_price=1177.25)

            result = db.manage_user_follow_exits(1, cost_settings=_economics_settings(), now_utc=closed_at)
            [follow] = db.user_followed_signal_ideas(1, 10)

        self.assertEqual(result["action_count"], 0)
        self.assertEqual(result["skipped_count"], 1)
        self.assertEqual(result["skipped"][0]["label"], "Pending Market Open")
        self.assertEqual(follow["follow_status"], "ACTIVE")
        self.assertEqual(follow["qty"], 5)
        self.assertIn("pending_after_hours_exit", follow["follow_details"]["exit_management"])

    def test_stop_loss_exit_is_not_blocked_by_trade_economics_floor(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(Path(tmpdir) / "agent.db")
            db.init()
            idea_id = _insert_trade_economics_idea(
                db,
                symbol="STOPME",
                entry_price=100.0,
                latest_price=94.0,
                details={"lifecycle_status": "active", "highest_target_hit": "NONE", "stop_loss": 95.0},
            )
            _insert_trade_economics_follow(db, idea_id, qty=10, entry_price=100.0, latest_price=94.0)

            result = db.manage_user_follow_exits(1, cost_settings=_economics_settings())

        self.assertEqual(result["action_count"], 1)
        self.assertEqual(result["actions"][0]["action"], "EXIT_FULL")
        self.assertEqual(result["actions"][0]["realized_pnl"], -60.0)

    def test_paper_follow_rejects_tiny_trade_notional(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(Path(tmpdir) / "agent.db")
            db.init()
            idea_id = _insert_trade_economics_idea(
                db,
                symbol="TINYQTY",
                entry_price=1384.0,
                latest_price=1384.0,
                details={
                    "action": "BUY",
                    "overall_score_pct": 88,
                    "overall_grade": "A",
                    "data_readiness": {"trade_decision_ready": True},
                },
            )

            with self.assertRaisesRegex(ValueError, "trade_economics_min_notional"):
                db.follow_signal_idea(1, idea_id, mode="PAPER", amount=5_000, cost_settings=_economics_settings())

    def test_paper_follow_amount_does_not_round_floor_qty_down_one_share(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(Path(tmpdir) / "agent.db")
            db.init()
            idea_id = _insert_trade_economics_idea(
                db,
                symbol="FLOORQTY",
                entry_price=454.70,
                latest_price=454.70,
                details={
                    "action": "BUY",
                    "overall_score_pct": 88,
                    "overall_grade": "A",
                    "data_readiness": {"trade_decision_ready": True},
                },
            )

            follow = db.follow_signal_idea(
                1,
                idea_id,
                mode="PAPER",
                amount=17 * 454.70,
                cost_settings=_economics_settings(),
            )

        self.assertEqual(follow["qty"], 17)
        self.assertGreaterEqual(follow["invested_amount"], 7_500.0)

    def test_auto_follow_sizing_uses_minimum_economic_size_when_cash_allows(self) -> None:
        sizing = auto_follow_sizing(
            25_000.0,
            100.0,
            max_position_pct=0.15,
            size_multiplier=1.0,
            market_region="IN",
            settings=_economics_settings(),
        )

        self.assertTrue(sizing["passed"])
        self.assertEqual(sizing["qty"], 75)
        self.assertEqual(sizing["amount"], 7_500.0)
        self.assertTrue(sizing["economics_floor_applied"])

    def test_auto_follow_sizing_upsizes_reduced_quality_probe_to_floor_when_fundable(self) -> None:
        sizing = auto_follow_sizing(
            25_000.0,
            100.0,
            max_position_pct=0.15,
            size_multiplier=0.35,
            market_region="IN",
            settings=_economics_settings(),
        )

        self.assertTrue(sizing["passed"])
        self.assertEqual(sizing["qty"], 75)
        self.assertEqual(sizing["amount"], 7_500.0)
        self.assertTrue(sizing["economics_floor_applied"])

    def test_auto_follow_sizing_uses_normal_risk_budget_for_fundable_floor(self) -> None:
        sizing = auto_follow_sizing(
            92_457.0,
            454.70,
            max_position_pct=0.25,
            size_multiplier=0.35,
            market_region="IN",
            settings=_economics_settings(),
            stop_loss=429.70,
            confidence=0.35,
            avg_daily_turnover=160_000_000.0,
        )

        self.assertTrue(sizing["passed"], sizing)
        self.assertEqual(sizing["qty"], 17)
        self.assertGreaterEqual(sizing["amount"], 7_500.0)
        self.assertEqual(sizing["risk_qty"], 12)
        self.assertGreaterEqual(sizing["floor_risk_qty"], sizing["minimum_qty"])

    def test_auto_follow_sizing_rejects_floor_when_risk_qty_cannot_fund_minimum(self) -> None:
        sizing = auto_follow_sizing(
            25_000.0,
            100.0,
            max_position_pct=0.15,
            size_multiplier=0.35,
            market_region="IN",
            settings=_economics_settings(),
            stop_loss=80.0,
        )

        self.assertFalse(sizing["passed"])
        self.assertEqual(sizing["reason"], "position_size_below_minimum_trade_economics")

    def test_paper_broker_uses_minimum_economic_size_for_conviction_buy(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(Path(tmpdir) / "agent.db")
            db.init()
            broker = PaperBroker(_paper_broker_settings(initial_cash=25_000.0), db)
            decision = _paper_buy_decision("ECONBUY", score=82.0, grade="A", confidence=0.82)

            filled = broker.execute(decision, portfolio_equity=25_000.0)
            [position] = db.positions()
            with db.connect() as conn:
                order = conn.execute("select * from orders where symbol = 'ECONBUY'").fetchone()

        self.assertTrue(filled)
        self.assertEqual(position["qty"], 75)
        self.assertEqual(order["status"], "FILLED")
        details = json.loads(order["details_json"])
        economics = details["execution"]["sizing"]["trade_economics"]
        self.assertTrue(economics["applied"])
        self.assertEqual(economics["reason"], "minimum_economic_trade_floor_applied")
        self.assertGreaterEqual(order["notional"], 7_500.0)

    def test_paper_broker_vetoes_conviction_buy_when_only_tiny_size_possible(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(Path(tmpdir) / "agent.db")
            db.init()
            broker = PaperBroker(_paper_broker_settings(initial_cash=10_000.0), db)
            decision = _paper_buy_decision("TOOSMALL", score=82.0, grade="A", confidence=0.82)

            filled = broker.execute(decision, portfolio_equity=10_000.0)
            positions = db.positions()
            with db.connect() as conn:
                order = conn.execute("select * from orders where symbol = 'TOOSMALL'").fetchone()

        self.assertFalse(filled)
        self.assertEqual(positions, [])
        self.assertEqual(order["status"], "VETOED")
        self.assertEqual(order["reason"], "position_size_below_minimum_trade_economics")

    def test_paper_broker_vetoes_reduced_quality_probe_below_minimum(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(Path(tmpdir) / "agent.db")
            db.init()
            broker = PaperBroker(_paper_broker_settings(initial_cash=25_000.0), db)
            decision = _paper_buy_decision(
                "PROBESIZE",
                score=82.0,
                grade="A",
                confidence=0.82,
                sizing_multiplier=0.35,
                setup_bucket="SMALL_SIZE_ONLY",
            )

            filled = broker.execute(decision, portfolio_equity=25_000.0)
            positions = db.positions()
            with db.connect() as conn:
                order = conn.execute("select * from orders where symbol = 'PROBESIZE'").fetchone()

        self.assertFalse(filled)
        self.assertEqual(positions, [])
        self.assertEqual(order["status"], "VETOED")
        self.assertEqual(order["reason"], "position_size_below_minimum_trade_economics")
        details = json.loads(order["details_json"])
        self.assertEqual(details["execution"]["veto_gate"], "trade_economics_min_notional")

    def test_paper_broker_blocks_tiny_profit_partial_exit_after_costs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(Path(tmpdir) / "agent.db")
            db.init()
            broker = PaperBroker(_paper_broker_settings(initial_cash=25_000.0), db)
            _insert_paper_position(db, symbol="SCHNEIDER", qty=4, avg_price=1384.0, market_price=1387.10)
            decision = _paper_sell_decision(
                "SCHNEIDER",
                price=1387.10,
                reason="profit tier1: price 1387.10 >= target1 1387.00; tighten stop to break-even",
                partial_sell_pct=0.33,
            )

            sold = broker.execute(decision, portfolio_equity=25_000.0)
            [position] = db.positions()
            with db.connect() as conn:
                order = conn.execute("select * from orders where symbol = 'SCHNEIDER'").fetchone()

        self.assertFalse(sold)
        self.assertEqual(position["qty"], 4)
        self.assertEqual(order["status"], "VETOED")
        self.assertEqual(order["reason"], "low_value_profit_exit_blocked")
        details = json.loads(order["details_json"])
        self.assertEqual(details["execution"]["veto_gate"], "trade_economics_min_exit_profit")

    def test_paper_broker_still_allows_stop_loss_exit(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(Path(tmpdir) / "agent.db")
            db.init()
            broker = PaperBroker(_paper_broker_settings(initial_cash=25_000.0), db)
            _insert_paper_position(db, symbol="STOPSELL", qty=10, avg_price=100.0, market_price=94.0)
            decision = _paper_sell_decision(
                "STOPSELL",
                price=94.0,
                reason="risk exit: price 94.00 <= stop 95.00",
                stop_triggered=True,
            )

            sold = broker.execute(decision, portfolio_equity=25_000.0)
            positions = db.positions()
            with db.connect() as conn:
                order = conn.execute("select * from orders where symbol = 'STOPSELL'").fetchone()

        self.assertTrue(sold)
        self.assertEqual(positions, [])
        self.assertEqual(order["status"], "FILLED")


def _economics_settings() -> SimpleNamespace:
    return SimpleNamespace(
        brokerage_bps=0.0,
        slippage_bps=5.0,
        taxes_bps=1.0,
        stt_bps=10.0,
        paper_min_auto_follow_notional_inr=7_500.0,
        paper_min_auto_follow_notional_usd=250.0,
        paper_min_exit_net_profit_inr=75.0,
        paper_min_exit_net_profit_usd=2.0,
        paper_min_exit_net_profit_bps=15.0,
    )


def _paper_broker_settings(initial_cash: float) -> SimpleNamespace:
    return SimpleNamespace(
        initial_cash_inr=initial_cash,
        max_positions=10,
        max_position_pct=0.15,
        max_order_value_pct=0.10,
        daily_loss_limit_pct=0.05,
        llm_decision_mode="offline",
        llm_provider="offline",
        brokerage_bps=0.0,
        slippage_bps=5.0,
        taxes_bps=1.0,
        stt_bps=10.0,
        paper_min_auto_follow_notional_inr=7_500.0,
        paper_min_auto_follow_notional_usd=250.0,
        paper_min_exit_net_profit_inr=75.0,
        paper_min_exit_net_profit_usd=2.0,
        paper_min_exit_net_profit_bps=15.0,
    )


def _paper_buy_decision(
    symbol: str,
    *,
    score: float,
    grade: str,
    confidence: float,
    sizing_multiplier: float = 1.0,
    setup_bucket: str = "ACTIONABLE",
) -> Decision:
    return Decision(
        symbol=symbol,
        action="BUY",
        confidence=confidence,
        price=100.0,
        technical_score=0.8,
        sentiment_score=0.2,
        reason="conviction setup",
        asof=utc_now(),
        strategy="unit_test_strategy",
        details_json=json.dumps(
            {
                "overall_score_pct": score,
                "overall_grade": grade,
                "setup_bucket": setup_bucket,
                "sizing_grade": {"final_multiplier": sizing_multiplier},
                "system_gate_audit": {
                    "hard_blocked": False,
                    "overall_score_pct": score,
                    "overall_grade": grade,
                    "allocation_cap_multiplier": sizing_multiplier,
                },
                "context": {
                    "market_region": "IN",
                    "full_spectrum_analysis": {
                        "trade_plan": {
                            "stop_loss": 99.0,
                            "position_sizing": {"max_capital_at_risk_pct": 0.01},
                        }
                    },
                },
            }
        ),
    )


def _btst_scan_payload() -> dict:
    return {
        "setup": "btst_buy_candidate",
        "bucket": "Actionable",
        "score": 0.76,
        "data_quality": {"actionable_data_ready": True, "missing": []},
        "btst": {
            "detected": True,
            "score": 0.76,
            "confidence": 0.79,
            "action_bias": "BUY",
            "next_day_bias": "positive_follow_through",
            "entry_zone": {"low": 107.0, "high": 109.0},
            "stop_loss": 104.0,
            "target1": 112.0,
            "checks": {
                "liquidity_ok": True,
                "trend_ok": True,
                "range_ok": True,
                "day_move_ok": True,
                "not_extended": True,
                "volume_ok": True,
                "overnight_risk_ok": True,
                "sentiment_ok": True,
            },
        },
    }


def _paper_sell_decision(
    symbol: str,
    *,
    price: float,
    reason: str,
    partial_sell_pct: float | None = None,
    stop_triggered: bool = False,
) -> Decision:
    return Decision(
        symbol=symbol,
        action="SELL",
        confidence=0.99,
        price=price,
        technical_score=0.0,
        sentiment_score=0.0,
        reason=reason,
        asof=utc_now(),
        strategy="risk_exit",
        details_json=json.dumps(
            {
                "decision_path": "risk_exit",
                "final_action": "SELL",
                "action_reason": reason,
                "risk_gates": {
                    "stop_triggered": stop_triggered,
                    "take_profit_triggered": partial_sell_pct is not None,
                    "partial_sell_pct": partial_sell_pct,
                },
                "context": {"market_region": "IN"},
            }
        ),
    )


def _insert_paper_position(
    db: Database,
    *,
    symbol: str,
    qty: int,
    avg_price: float,
    market_price: float,
) -> None:
    with db.connect() as conn:
        conn.execute(
            """
            insert into positions (symbol, strategy, qty, avg_price, market_price, realized_pnl, updated_at, details_json)
            values (?, 'unit_test_strategy', ?, ?, ?, 0, ?, '{}')
            """,
            (symbol, qty, avg_price, market_price, utc_now()),
        )


def _insert_trade_economics_idea(
    db: Database,
    *,
    symbol: str,
    entry_price: float,
    latest_price: float,
    peak_return_pct: float | None = None,
    details: dict | None = None,
) -> int:
    now = utc_now()
    return_pct = ((latest_price - entry_price) / entry_price) * 100 if entry_price else 0.0
    payload = {
        "action": "BUY",
        "overall_score_pct": 88,
        "overall_grade": "A",
        "hard_blocked": False,
        "hard_blocks": [],
        "data_readiness": {"trade_decision_ready": True},
    }
    if details:
        payload.update(details)
    with db.connect() as conn:
        conn.execute(
            """
            insert into signal_ideas (
                first_seen_at, last_seen_at, symbol, strategy, plan_code, signal_type, status,
                entry_price, latest_price, current_return_pct, peak_return_pct, worst_return_pct,
                confidence, combined_score, confluence, overall_score_pct, overall_grade,
                reason, details_json
            )
            values (?, ?, ?, 'trade_economics_test', 'trade_economics_test',
                'BUY', 'ACTIVE', ?, ?, ?, ?, 0, 0.9, 0.7, 24, 88, 'A',
                'trade economics test', ?)
            """,
            (
                now,
                now,
                symbol,
                entry_price,
                latest_price,
                return_pct,
                peak_return_pct if peak_return_pct is not None else max(return_pct, 0.0),
                json.dumps(payload),
            ),
        )
        row = conn.execute("select last_insert_rowid() as id").fetchone()
    return int(row["id"])


def _insert_trade_economics_follow(
    db: Database,
    idea_id: int,
    *,
    qty: int,
    entry_price: float,
    latest_price: float,
    details: dict | None = None,
) -> None:
    now = utc_now()
    payload = details or {}
    return_pct = ((latest_price - entry_price) / entry_price) * 100 if entry_price else 0.0
    with db.connect() as conn:
        conn.execute(
            """
            insert into user_idea_follows (
                user_id, idea_id, mode, status, qty, entry_price, latest_price,
                invested_amount, unrealized_pnl, return_pct, created_at, updated_at, details_json
            )
            values (1, ?, 'PAPER', 'ACTIVE', ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                idea_id,
                qty,
                entry_price,
                latest_price,
                qty * entry_price,
                (latest_price - entry_price) * qty,
                return_pct,
                now,
                now,
                json.dumps(payload),
            ),
        )


def _scanner_settings() -> SimpleNamespace:
    return SimpleNamespace(
        dynamic_scan_candidate_limit=60,
        dynamic_scan_min_score=0.58,
        dynamic_scan_require_active_setup=True,
        dynamic_scan_min_price=10,
        dynamic_scan_min_turnover_inr=50_000_000,
        dynamic_scan_min_turnover_usd=2_000_000,
        dynamic_scan_breakout_distance_pct=3.0,
        dynamic_scan_sentiment_enabled=True,
        dynamic_scan_sentiment_weight=0.12,
    )


def _recent_session_asof(market_region: str) -> str:
    now = datetime.now(timezone.utc)
    if str(market_region).upper() == "US":
        local_zone = ZoneInfo("America/New_York")
        session_hour = 10
        session_minute = 0
    else:
        local_zone = ZoneInfo("Asia/Kolkata")
        session_hour = 9
        session_minute = 45
    local_day = now.astimezone(local_zone).date()
    while local_day.weekday() >= 5:
        local_day += timedelta(days=1)
    local_dt = datetime(
        local_day.year,
        local_day.month,
        local_day.day,
        session_hour,
        session_minute,
        tzinfo=local_zone,
    )
    return local_dt.astimezone(timezone.utc).isoformat()


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


def _flat_candles_with_old_high() -> list[Candle]:
    candles: list[Candle] = []
    for index in range(90):
        close = 98.0 + (index % 5) * 0.3
        high = 130.0 if index == 20 else close * 1.01
        candles.append(
            Candle(
                symbol="FASTMOVE",
                ts=f"2026-02-{(index % 28) + 1:02d}T00:00:00+00:00",
                open=close * 0.995,
                high=high,
                low=close * 0.99,
                close=close,
                volume=100_000,
                source="unit-test",
            )
        )
    return candles


def _pre_rally_fuel_candles() -> list[Candle]:
    candles: list[Candle] = []
    close = 80.0
    for index in range(90):
        close *= 1.002
        if index > 75:
            close *= 1.003
        volume = 700_000
        if index >= 84:
            volume = 1_200_000
        candles.append(
            Candle(
                symbol="FUEL",
                ts=f"2026-03-{(index % 28) + 1:02d}T00:00:00+00:00",
                open=close * 0.992,
                high=close * (1.005 if index != 40 else 1.01),
                low=close * 0.988,
                close=close,
                volume=volume,
                source="unit-test",
            )
        )
    return candles


def _momentum_gate_context(session_momentum: dict) -> dict:
    return {
        "symbol": "FASTMOVE",
        "quote": {
            "price": 108.0,
            "source": "upstox-live",
            "asof": utc_now(),
            "open": 100.0,
            "high": 109.0,
            "low": 99.0,
            "volume": 2_000_000,
        },
        "sentiment": {"score": 0.1, "confidence": 0.2, "status": "AVAILABLE"},
        "position": {"qty": 0},
        "best_strategy": {"name": "time_series_momentum_trend", "score": 0.92},
        "data_readiness": {
            "market_region": "IN",
            "trade_decision_ready": True,
            "grade": "A",
            "hard_gaps": [],
            "sources": {"quote": "upstox-live", "daily": "upstox-live:day"},
        },
        "risk_limits": {"portfolio_equity": 100_000, "max_position_pct": 0.1},
        "market_breadth_context": {"breadth_regime": "bull_confirmed"},
        "full_spectrum_analysis": {
            "confluence_score": {"total": 22, "tier": "MAXIMUM_CONVICTION"},
            "risk_overrides": {"flags": [], "no_new_longs": False},
            "institutional_scorecard": {"total_score": 78, "score": 78, "buy_ready": True, "hard_veto": {"failed": []}},
            "stage_analysis": {"stage": "Stage2_Markup", "buy_permitted": True},
            "entry_quality": {"entry_grade": "B", "distance_from_pivot_pct": 3.0, "volume_confirmation": True},
            "breakout_quality": {"breakout_quality": "not_breakout", "two_day_rule_failed": False, "volume_confirmation": True},
            "strategy_logic_filters": {
                "passed": True,
                "hard_blocks": [],
                "penalties": [],
                "sizing": {"max_multiplier": 0.85},
                "institutional_sponsorship": {"supported": True, "evidence": ["delivery accumulation"]},
                "breakout_volume": {"volume_confirmed": True, "suspect_without_volume": False},
            },
            "price_volume_divergence": {"climax_volume_top": False},
            "trend_context": {"timeframe_alignment": {"alignment_grade": "B"}},
            "options_oi": {},
            "sector_rotation": {},
            "delivery_accumulation": {"bias": "accumulation", "net_bias": "accumulation", "delivery_score": 0.8},
            "fundamental_quality": {"quality_bucket": "reference_ratios_available", "metrics": {"reference_data_available": True}},
            "liquidity_profile": {"liquidity_tier": "strong", "tradeable": True, "avg_traded_value_20": 150_000_000},
            "indicator_suite": {"atr_pct": 3.2},
            "trade_plan": {"entry_zone": [107.0, 109.0], "stop_loss": 103.0, "targets": [{"price": 116.0}]},
            "session_momentum": session_momentum,
        },
    }


def _raw_entry_context(
    *,
    price: float = 108.0,
    setup: str = "opening_ignition",
    market_region: str = "IN",
    data_readiness: dict | None = None,
    technical_score: float = 0.78,
    day_gain_pct: float = 3.2,
    volume_ratio: float = 2.1,
    projected_volume_ratio: float = 2.4,
    day_range_position: float = 0.82,
    day_high_distance_pct: float = 0.8,
    sentiment: dict | None = None,
) -> dict:
    return {
        "symbol": "RAWBUY",
        "market_region": market_region,
        "quote": {
            "price": price,
            "source": "upstox-live",
            "asof": utc_now(),
            "open": 100.0,
            "high": 110.0,
            "low": 99.0,
            "volume": 2_500_000,
        },
        "sentiment": sentiment or {"score": 0.2, "confidence": 0.4, "status": "AVAILABLE"},
        "position": {"qty": 0},
        "technical_math": {"score": technical_score},
        "data_readiness": data_readiness or {"trade_decision_ready": True},
        "opportunity_scan": {
            "setup": setup,
            "bucket": "Actionable",
            "market_region": market_region,
            "score": 0.82,
            "day_gain_pct": day_gain_pct,
            "day_range_position": day_range_position,
            "day_high_distance_pct": day_high_distance_pct,
            "volume_ratio": volume_ratio,
            "projected_volume_ratio": projected_volume_ratio,
            "turnover": 160_000_000,
            "projected_turnover": 220_000_000,
            "components": {"live_momentum": 0.78},
        },
        "full_spectrum_analysis": {
            "liquidity_profile": {"liquidity_tier": "strong", "tradeable": True},
            "entry_quality": {},
            "confluence_score": {},
            "trade_plan": {},
        },
        "risk_limits": {"portfolio_equity": 100_000},
    }


def _raw_opportunity_settings() -> SimpleNamespace:
    return SimpleNamespace(entry_authority_min_score=64, entry_authority_watch_score=52, raw_entry_min_score=64)


if __name__ == "__main__":
    unittest.main()
