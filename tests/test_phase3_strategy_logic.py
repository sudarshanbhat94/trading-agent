from __future__ import annotations

import unittest

from app.full_spectrum import _false_breakout_filter, _phase3_strategy_logic_filters
from app.models import Candle
from app.trading_rules import evaluate_rules_for_context


class Phase3StrategyLogicTests(unittest.TestCase):
    def test_phase3_blocks_extended_suspect_breakout_before_earnings(self) -> None:
        audit = _phase3_strategy_logic_filters(
            entry_quality={"entry_grade": "C", "distance_from_pivot_pct": 6.2, "pivot": 100, "volume_confirmation": False},
            breakout_quality={"breakout_quality": "suspect", "volume_expansion": False, "two_day_rule_failed": False},
            indicators={"volume_ratio_20": 0.7},
            delivery={"bias": "unknown", "delivery_score": 0.0},
            institutional_flow={"market_bias": {"score": 0.0}, "official_announcements": [], "bulk_deals": []},
            options_oi={"bias": "balanced", "pcr_oi": 0.9},
            macro_event_context={"earnings_trading_days_away": 5},
            fundamental={"quality_bucket": "reference_ratios_available"},
        )

        hard_flags = {item["flag"] for item in audit["hard_blocks"]}
        penalty_flags = {item["flag"] for item in audit["penalties"]}
        self.assertFalse(audit["passed"])
        self.assertIn("PRICE_EXTENDED_FROM_PIVOT", hard_flags)
        self.assertIn("SUSPECT_BREAKOUT_WITHOUT_VOLUME", hard_flags)
        self.assertIn("EARNINGS_LOCKOUT_NOT_EVENT_DRIVEN", hard_flags)
        self.assertIn("LOW_VOLUME_RATIO", penalty_flags)
        self.assertIn("INSTITUTIONAL_SPONSORSHIP_MISSING", penalty_flags)

    def test_event_driven_earnings_requires_tiny_size_but_not_hard_block(self) -> None:
        audit = _phase3_strategy_logic_filters(
            entry_quality={"entry_grade": "A", "distance_from_pivot_pct": 1.1, "pivot": 100, "volume_confirmation": True},
            breakout_quality={"breakout_quality": "confirmed", "volume_expansion": True, "two_day_rule_failed": False},
            indicators={"volume_ratio_20": 1.8},
            delivery={"bias": "accumulation", "delivery_score": 0.5, "delivery_pct": 58},
            institutional_flow={"market_bias": {"score": 0.2}, "official_announcements": [], "bulk_deals": []},
            options_oi={"bias": "balanced", "pcr_oi": 1.0},
            macro_event_context={"earnings_trading_days_away": 3, "event_driven": True},
            fundamental={"quality_bucket": "event_positive_with_ratios"},
        )

        penalty_flags = {item["flag"] for item in audit["penalties"]}
        self.assertTrue(audit["passed"])
        self.assertEqual(audit["hard_blocks"], [])
        self.assertIn("EARNINGS_EVENT_DRIVEN_TINY_SIZE", penalty_flags)
        self.assertTrue(audit["institutional_sponsorship"]["supported"])
        self.assertLessEqual(audit["sizing"]["max_multiplier"], 0.25)

    def test_repeated_failed_breakouts_are_counted(self) -> None:
        breakout = _false_breakout_filter(_failed_breakout_candles(), 104)

        self.assertGreaterEqual(breakout["failed_breakout_count"], 2)
        self.assertTrue(breakout["repeated_failed_breakouts"])

    def test_rule_audit_turns_phase3_hard_block_into_no_trade(self) -> None:
        context = _base_context()
        context["full_spectrum_analysis"]["strategy_logic_filters"] = {
            "passed": False,
            "hard_blocks": [
                {
                    "flag": "PRICE_EXTENDED_FROM_PIVOT",
                    "reason": "fresh long is more than 5% above pivot",
                    "value": {"distance_from_pivot_pct": 7.1},
                }
            ],
            "penalties": [],
            "sizing": {"max_multiplier": 1.0},
            "institutional_sponsorship": {"supported": True, "evidence": ["delivery accumulation"]},
        }

        audit = evaluate_rules_for_context(context, {}, 100_000)

        self.assertTrue(audit["hard_blocked"])
        self.assertIn("PRICE_EXTENDED_FROM_PIVOT", audit["active_flags"])

    def test_speculative_names_are_capped_at_tiny_size(self) -> None:
        context = _base_context()
        context["full_spectrum_analysis"]["fundamental_quality"] = {"quality_bucket": "unknown", "metrics": {}}

        audit = evaluate_rules_for_context(context, {}, 100_000)

        self.assertFalse(audit["hard_blocked"])
        self.assertIn("SPECULATIVE_TINY_SIZE_ONLY", audit["active_flags"])
        self.assertLessEqual(audit["allocation_cap_multiplier"], 0.15)


def _base_context() -> dict:
    return {
        "quote": {"price": 100, "source": "unit-test"},
        "sentiment": {"score": 0.25, "status": "AVAILABLE", "headline_count": 2, "source": "news"},
        "position": {"qty": 0},
        "macro_event_context": {},
        "full_spectrum_analysis": {
            "entry_quality": {"entry_grade": "A", "distance_from_pivot_pct": 1.2},
            "breakout_quality": {"breakout_quality": "confirmed", "two_day_rule_failed": False},
            "strategy_logic_filters": {
                "passed": True,
                "hard_blocks": [],
                "penalties": [],
                "sizing": {"max_multiplier": 1.0},
                "institutional_sponsorship": {"supported": True, "evidence": ["delivery accumulation"]},
            },
            "price_volume_divergence": {},
            "delivery_accumulation": {"bias": "accumulation", "delivery_score": 0.5, "delivery_pct": 55},
            "sector_rotation": {"sector": "Technology", "industry": "Software"},
            "trend_context": {"timeframe_alignment": {"alignment_grade": "A"}},
            "options_oi": {"status": "not_fno_no_stock_options"},
            "fundamental_quality": {
                "metrics": {
                    "revenue_growth_yoy_pct": 18,
                    "pat_growth_yoy_pct": 15,
                    "operating_cash_flow_positive": True,
                }
            },
        },
    }


def _failed_breakout_candles() -> list[Candle]:
    candles = []
    for index in range(60):
        close = 100.0
        high = 101.0
        low = 99.0
        if index == 25:
            close, high, low = 102.0, 103.0, 100.5
        elif index == 26:
            close, high, low = 99.8, 101.0, 99.0
        elif index == 45:
            close, high, low = 104.0, 105.0, 102.5
        elif index == 46:
            close, high, low = 101.5, 103.0, 101.0
        candles.append(
            Candle(
                symbol="FAIL",
                ts=f"2026-03-{(index % 28) + 1:02d}T00:00:00+00:00",
                open=100.0,
                high=high,
                low=low,
                close=close,
                volume=1_000_000,
                source="unit-test",
            )
        )
    return candles


if __name__ == "__main__":
    unittest.main()
