from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from app.decision_contract import current_decision_rows
from app.db import Database, _compact_decision_details, _paper_exit_action
from app.full_spectrum import _strategy_confirmed_entry_quality
from app.models import Candle, Decision, Quote, utc_now
from app.opportunity_scanner import OpportunityScanner
from app.opportunity_state import opportunity_state_from_signal_details
from app.agent import _auto_follow_idea_fresh_enough
from app.signal_quality import auto_follow_quality_gate, fresh_buy_quality_gate
from app.strategy import StrategyEngine, _compact_context, _performance_feedback_block
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
        self.assertEqual(gate["reason"], "auto_follow_severe_risk_flags")

    def test_auto_follow_freshness_allows_current_cycle_probe_buy_symbol(self) -> None:
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

        self.assertTrue(fresh)

    def test_auto_follow_freshness_allows_active_buy_now_probe(self) -> None:
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

        self.assertTrue(fresh)

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
                    asof="2026-05-25T04:15:00+00:00",
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
                    asof="2026-05-25T04:15:00+00:00",
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
                    asof="2026-05-25T14:00:00+00:00",
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
                    asof="2026-05-25T14:00:00+00:00",
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

    def test_live_confirmed_probe_can_use_trade_ready_data_when_scan_quality_lags(self) -> None:
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

    def test_scan_probe_can_use_live_quote_when_only_intraday_candles_are_stale(self) -> None:
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
        self.assertEqual(context["decision_gate_context"]["blocking_failed_gates"], [])

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


if __name__ == "__main__":
    unittest.main()
