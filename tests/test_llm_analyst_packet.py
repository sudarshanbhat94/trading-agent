from __future__ import annotations

import asyncio
import json
import unittest

from app.config import Settings
from app.db import _compact_decision_details
from app.llm_brain import LLMBrain, _groq_budget_context, _llm_prompt_context


class LLMAnalystPacketTests(unittest.TestCase):
    def test_groq_context_sends_detailed_analyst_packet(self) -> None:
        context = _analyst_context()

        payload = _groq_budget_context(context)
        packet = payload["analyst_packet"]

        self.assertIn("Company wins export order", payload["sentiment"]["headlines"])
        self.assertEqual(packet["news_and_events"]["events"][0]["event_type"], "order_win")
        self.assertTrue(packet["data_readiness"]["trade_decision_ready"])
        self.assertEqual(packet["flow_delivery_options"]["delivery_accumulation"]["delivery_pct"], 58.2)
        self.assertEqual(packet["flow_delivery_options"]["options_oi"]["max_pain_distance_pct"], 4.1)
        self.assertEqual(packet["entry_breakout_volume"]["breakout_quality"]["failed_breakout_count"], 2)
        self.assertIn("bulk_deals", packet["flow_delivery_options"]["institutional_flow"])

    def test_deepseek_context_sends_decision_audit_and_rich_evidence(self) -> None:
        context = _analyst_context()

        payload = _llm_prompt_context(context, profile="rich")
        packet = payload["analyst_packet"]

        self.assertEqual(payload["tool_protocol"], "openstocks-rich-decision-context-v1")
        self.assertEqual(payload["score_breakdown"]["combined"], 0.52)
        self.assertEqual(payload["pre_filter"]["block_gate"], "entry_gate")
        self.assertEqual(payload["decision_gate_context"]["failed_gates"][0]["gate"], "overall_quality_gate")
        self.assertEqual(payload["sizing_grade"]["final_multiplier"], 0.5)
        self.assertEqual(packet["decision_audit"]["pre_filter"]["block_gate"], "entry_gate")
        self.assertEqual(packet["decision_audit"]["sizing_grade"]["recommended_max_position_pct"], 0.075)

    def test_compacted_decision_keeps_prompt_audit_reviewable(self) -> None:
        raw = json.dumps(
            {
                "decision_path": "llm_primary",
                "llm_prompt_audit": {
                    "market_region": "IN",
                    "currency": "INR",
                    "model": "qwen/qwen3-32b",
                    "mode": "exact_context_sent_to_llm",
                    "system_prompt_chars": 100,
                    "context_chars": 2000,
                    "estimated_input_tokens": 630,
                    "included_sections": ["analyst_packet", "sentiment"],
                    "context_sha256": "abc123",
                    "system_prompt": "Use analyst_packet news and data readiness.",
                    "user_context": {
                        "symbol": "PACKET",
                        "analyst_packet": {
                            "news_and_events": {
                                "headlines": ["Company wins export order"],
                                "events": [{"title": "Company wins export order", "event_type": "order_win"}],
                            }
                        },
                    },
                },
                "context": {
                    "symbol": "PACKET",
                    "data_readiness": {"trade_decision_ready": True, "grade": "A"},
                    "full_spectrum_analysis": {},
                },
                "padding": ["x" * 200 for _ in range(80)],
            }
        )

        compacted = json.loads(_compact_decision_details({"action": "HOLD", "symbol": "PACKET"}, raw))

        self.assertEqual(compacted["llm_prompt_audit"]["market_region"], "IN")
        self.assertTrue(compacted["llm_prompt_audit"]["storage_compacted"])
        self.assertIn("analyst_packet", compacted["llm_prompt_audit"]["user_context"])
        self.assertIn(
            "Company wins export order",
            compacted["llm_prompt_audit"]["user_context"]["analyst_packet"]["news_and_events"]["headlines"],
        )

    def test_compact_cycle_profile_skips_rolling_summary_calls(self) -> None:
        settings = Settings(
            llm_provider="deepseek",
            llm_rolling_context_enabled=True,
            llm_rolling_context_threshold_chars=10,
        )
        brain = LLMBrain(settings)
        context = _analyst_context()
        context["llm_prompt_profile"] = "compact"
        context["large_nonessential_payload"] = ["x" * 1000 for _ in range(30)]

        payload, meta = asyncio.run(brain._decision_prompt_context(context))

        self.assertEqual(meta["_llm_analysis_mode"], "compact_cycle_context")
        self.assertEqual(payload["tool_protocol"], "openstocks-compact-decision-context-v1")
        self.assertNotIn("rolling_context_coverage", payload)


def _analyst_context() -> dict:
    return {
        "symbol": "PACKET",
        "company": "Packet Industries",
        "market_region": "IN",
        "currency": "INR",
        "sector": "Industrials",
        "exchange": "NSE",
        "quote": {"price": 100.0, "close": 99.0, "volume": 1500000, "source": "upstox-live"},
        "position": {"qty": 0, "avg_price": 0, "market_price": 100.0},
        "technical_math": {"score": 0.82, "trend": "uptrend", "rsi": 61, "sma_fast": 94, "sma_slow": 88, "momentum_pct": 7.5},
        "best_strategy": {"name": "volume_price_accumulation", "score": 0.86, "direction": "BUY", "confidence": 0.86},
        "strategy_signals": [
            {"name": "volume_price_accumulation", "score": 0.86, "direction": "BUY", "confidence": 0.86, "notes": ["volume expansion"]}
        ],
        "sentiment": {
            "score": 0.42,
            "status": "AVAILABLE",
            "confidence": 0.74,
            "headline_count": 2,
            "headlines": ["Company wins export order", "Promoter buying reported"],
            "events": [
                {
                    "title": "Company wins export order",
                    "source": "nseindia.com",
                    "published_at": "2026-05-21T09:30:00+05:30",
                    "event_type": "order_win",
                    "score": 0.7,
                    "confidence": 0.82,
                    "weighted_score": 0.57,
                }
            ],
        },
        "data_readiness": {
            "phase": 2,
            "market_region": "IN",
            "trade_decision_ready": True,
            "screening_ready": True,
            "score_pct": 91,
            "grade": "A",
            "available": [{"key": "in_live_quote", "label": "India live quote", "available": True, "source": "upstox-live"}],
            "hard_gaps": [],
            "soft_gaps": [],
            "missing_data": [],
            "sources": {"quote": "upstox-live", "intraday": "upstox-live:minute"},
        },
        "score_breakdown": {"combined": 0.52},
        "pre_filter": {
            "pre_filter_stage": "completed",
            "buy_threshold": 0.35,
            "buy_blocked": True,
            "block_gate": "entry_gate",
            "block_value": {"entry_grade": "WATCH"},
            "elimination_reason": "watch_entry_needs_confirmation",
            "gates": [{"gate": "entry_gate", "passed": False, "value": {"entry_grade": "WATCH"}}],
        },
        "decision_gate_context": {
            "buy_threshold": 0.35,
            "breadth_regime": "bull_confirmed",
            "failed_gates": [
                {"gate": "overall_quality_gate", "value": {"overall_score_pct": 62}, "reason": "overall_score_below_70_no_new_longs"}
            ],
            "evaluated_gates": [{"gate": "overall_quality_gate", "passed": False}],
        },
        "sizing_grade": {
            "base_multiplier": 1.0,
            "final_multiplier": 0.5,
            "recommended_max_position_pct": 0.075,
            "modifier_details": ["entry WATCH x0.5"],
            "classification": {"classification": "MOMENTUM", "max_allocation_multiplier": 0.5},
        },
        "portfolio_correlation_gate": {"block_buy": False, "warning": "sector exposure moderate"},
        "llm_primary_selection": {"selected": True, "candidate_limit": 8, "prefilter_passed": True},
        "timeframe_data": {"daily_candle_count": 120, "intraday_candle_count": 64, "daily_source": "upstox-live:day"},
        "recent_candles": [
            {"ts": f"2026-05-{day:02d}", "open": 90 + day, "high": 92 + day, "low": 89 + day, "close": 91 + day, "volume": 1000000 + day}
            for day in range(10, 18)
        ],
        "full_spectrum_analysis": {
            "indicator_suite": {"atr_pct": 3.1, "adx": 32, "rsi_14": 61, "volume_ratio_20": 2.2},
            "entry_quality": {"entry_grade": "A", "distance_from_pivot_pct": 1.4, "pivot": 98.6, "volume_confirmation": True},
            "breakout_quality": {
                "breakout_quality": "confirmed",
                "two_day_rule_failed": False,
                "volume_expansion": True,
                "failed_breakout_count": 2,
                "repeated_failed_breakouts": True,
                "failed_breakout_events": [{"ts": "2026-05-01", "breakout_close": 101, "prior_resistance": 99}],
            },
            "strategy_logic_filters": {
                "passed": True,
                "hard_blocks": [],
                "penalties": [{"flag": "REPEATED_FAILED_BREAKOUTS", "reason": "multiple recent failed attempts"}],
                "breakout_volume": {"volume_confirmed": True, "volume_ratio_20": 2.2},
                "institutional_sponsorship": {"supported": True, "evidence": ["delivery accumulation", "bulk deal evidence"]},
            },
            "price_volume_divergence": {"divergence_score": 0, "climax_volume_top": False, "volume_ratio_20": 2.2},
            "delivery_accumulation": {
                "available": True,
                "delivery_pct": 58.2,
                "delivery_score": 0.64,
                "bias": "accumulation",
                "institutional_fingerprint": True,
                "source": "nse_delivery_bhavcopy",
            },
            "institutional_flow": {
                "available": True,
                "source_quality": "official_public",
                "market_bias": {"score": 0.18, "rationale": ["FII net buying"]},
                "official_announcements": [{"title": "Company wins export order", "source": "NSE"}],
                "bulk_deals": [{"buyer": "Fund A", "qty": 120000}],
                "fii_dii_flow": {"score": 0.2, "net_buy": 1500},
            },
            "options_oi": {
                "available": True,
                "status": "ok",
                "source": "nse_option_chain_stock_level",
                "pcr_oi": 1.28,
                "max_pain": 104,
                "max_pain_distance_pct": 4.1,
                "buy_suppressed": False,
                "bias": "max_pain_above_supportive",
            },
            "corporate_event_risk": {"available": True, "high_impact_risk": False, "events": [{"title": "Company wins export order"}]},
            "liquidity_profile": {"avg_traded_value_20": 120000000, "volume_ratio_20": 2.2, "liquidity_tier": "strong"},
            "trend_context": {"timeframe_alignment": {"alignment_grade": "A"}},
            "sector_rotation": {"sector_tailwind": True, "sector_stage": "leadership"},
            "market_breadth": {"breadth_regime": "bull_confirmed"},
            "macro_event_context": {"is_expiry_day": False},
            "performance_feedback": {"available": True, "selected_strategy": {"win_rate": 0.58, "expectancy_pct": 1.2}},
            "institutional_scorecard": {"total_score": 82, "grade": "A", "buy_ready": True, "reasons": ["delivery accumulation"]},
            "confluence_score": {"total": 21, "tier": "HIGH_CONVICTION"},
            "trade_plan": {"entry_zone": [99, 101], "stop_loss": 94, "targets": [{"label": "T1", "price": 108}]},
            "data_quality": {"coverage": "strong"},
        },
    }


if __name__ == "__main__":
    unittest.main()
