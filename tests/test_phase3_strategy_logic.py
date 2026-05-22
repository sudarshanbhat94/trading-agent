from __future__ import annotations

import unittest

from app.full_spectrum import _delivery_accumulation, _false_breakout_filter, _phase3_strategy_logic_filters
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

    def test_missing_fundamentals_with_strong_price_volume_is_momentum_not_speculative(self) -> None:
        context = _base_context()
        context["full_spectrum_analysis"]["fundamental_quality"] = {"quality_bucket": "unknown", "metrics": {}}
        context["full_spectrum_analysis"]["confluence_score"] = {"total": 18, "tier": "HIGH"}
        context["full_spectrum_analysis"]["liquidity_profile"] = {
            "tradeable": True,
            "liquidity_tier": "strong",
            "avg_traded_value_20": 75_000_000,
        }

        audit = evaluate_rules_for_context(context, {}, 100_000)

        self.assertEqual(audit["classification"]["classification"], "MOMENTUM")
        self.assertNotIn("SPECULATIVE_TINY_SIZE_ONLY", audit["active_flags"])
        self.assertGreater(audit["allocation_cap_multiplier"], 0.15)

    def test_missing_fundamentals_with_illiquid_profile_stays_speculative(self) -> None:
        context = _base_context()
        context["full_spectrum_analysis"]["fundamental_quality"] = {"quality_bucket": "unknown", "metrics": {}}
        context["full_spectrum_analysis"]["confluence_score"] = {"total": 18, "tier": "HIGH"}
        context["full_spectrum_analysis"]["liquidity_profile"] = {
            "tradeable": False,
            "liquidity_tier": "illiquid",
            "avg_traded_value_20": 500_000,
        }

        audit = evaluate_rules_for_context(context, {}, 100_000)

        self.assertEqual(audit["classification"]["classification"], "SPECULATIVE")
        self.assertIn("SPECULATIVE_TINY_SIZE_ONLY", audit["active_flags"])

    def test_missing_sentiment_does_not_downgrade_clean_price_volume_setup_to_watch(self) -> None:
        context = _base_context()
        context["sentiment"] = {}
        context["full_spectrum_analysis"]["entry_quality"] = {
            "entry_grade": "B",
            "distance_from_pivot_pct": 1.2,
            "volume_confirmation": True,
        }
        context["full_spectrum_analysis"]["confluence_score"] = {"total": 19, "tier": "HIGH"}
        context["full_spectrum_analysis"]["breakout_quality"] = {
            "breakout_quality": "confirmed",
            "two_day_rule_failed": False,
            "volume_confirmation": True,
        }

        audit = evaluate_rules_for_context(context, {}, 100_000)

        self.assertEqual(audit["sentiment"]["status"], "DATA_MISSING")
        self.assertEqual(audit["entry"]["effective_entry_grade"], "B")
        self.assertNotIn("GRADE_VIOLATION", audit["active_flags"])

    def test_us_price_volume_proxy_counts_as_accumulation_support(self) -> None:
        delivery = _delivery_accumulation(
            {},
            _us_accumulation_candles(),
            {
                "available": False,
                "market_region": "US",
                "source": "not_applicable_to_us_market",
                "delivery_score": 0.0,
                "net_bias": "neutral",
            },
        )

        audit = _phase3_strategy_logic_filters(
            entry_quality={"entry_grade": "A", "distance_from_pivot_pct": 1.2, "pivot": 100, "volume_confirmation": True},
            breakout_quality={"breakout_quality": "confirmed", "volume_expansion": True, "two_day_rule_failed": False},
            indicators={"volume_ratio_20": 2.3},
            delivery=delivery,
            institutional_flow={"market_bias": {"score": 0.0}, "official_announcements": [], "bulk_deals": []},
            options_oi={"bias": "balanced", "pcr_oi": 1.0},
            macro_event_context={},
            fundamental={"quality_bucket": "reference_ratios_available"},
        )

        penalty_flags = {item["flag"] for item in audit["penalties"]}
        self.assertEqual(delivery["bias"], "volume_accumulation_proxy")
        self.assertTrue(audit["institutional_sponsorship"]["supported"])
        self.assertIn("price-volume accumulation proxy", audit["institutional_sponsorship"]["evidence"])
        self.assertNotIn("INSTITUTIONAL_SPONSORSHIP_MISSING", penalty_flags)

    def test_us_price_volume_proxy_prevents_missing_news_from_crushing_entry(self) -> None:
        context = _base_context()
        context["market_region"] = "US"
        context["sentiment"] = {}
        context["full_spectrum_analysis"]["entry_quality"] = {
            "entry_grade": "B",
            "distance_from_pivot_pct": 1.2,
            "volume_confirmation": True,
        }
        context["full_spectrum_analysis"]["confluence_score"] = {"total": 19, "tier": "HIGH"}
        context["full_spectrum_analysis"]["delivery_accumulation"] = {
            "bias": "volume_accumulation_proxy",
            "net_bias": "volume_accumulation_proxy",
            "delivery_score": 0.35,
            "source": "us_price_volume_proxy_no_delivery_data",
        }
        context["full_spectrum_analysis"]["strategy_logic_filters"]["institutional_sponsorship"] = {
            "supported": True,
            "evidence": ["price-volume accumulation proxy"],
        }
        context["full_spectrum_analysis"]["fundamental_quality"] = {"quality_bucket": "unknown", "metrics": {}}

        audit = evaluate_rules_for_context(context, {}, 100_000)

        self.assertEqual(audit["sentiment"]["status"], "DATA_MISSING")
        self.assertEqual(audit["entry"]["effective_entry_grade"], "B")
        self.assertNotIn("GRADE_VIOLATION", audit["active_flags"])
        self.assertNotIn("INSTITUTIONAL_SPONSORSHIP_MISSING", audit["active_flags"])


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


def _us_accumulation_candles() -> list[Candle]:
    candles = []
    for index in range(25):
        close = 100.0 + max(index - 19, 0) * 0.8
        volume = 1_000_000
        if index == 24:
            volume = 2_500_000
            close = 105.0
        candles.append(
            Candle(
                symbol="USACC",
                ts=f"2026-04-{(index % 28) + 1:02d}T00:00:00+00:00",
                open=max(close - 0.4, 1.0),
                high=close + 0.8,
                low=close - 1.0,
                close=close,
                volume=volume,
                source="alpaca-sip-live:day",
            )
        )
    return candles


if __name__ == "__main__":
    unittest.main()
