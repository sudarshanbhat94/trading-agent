from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from app.decision_contract import current_decision_rows
from app.db import Database, _compact_decision_details, _paper_exit_action
from app.models import Candle, Decision, Quote, utc_now
from app.opportunity_scanner import OpportunityScanner
from app.signal_quality import auto_follow_quality_gate, fresh_buy_quality_gate
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
        self.assertIn("Phase-2 data readiness", row["display_reason"])

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
