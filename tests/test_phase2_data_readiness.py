from __future__ import annotations

import unittest
from datetime import datetime, timezone

from app.analysis_tools import _should_drop_partial_us_yahoo_daily_candle
from app.data_readiness import assess_phase2_data_readiness
from app.models import Candle, Quote
from app.strategy import _llm_buy_block_reason, should_call_llm
from app.trading_rules import evaluate_rules_for_context


class Phase2DataReadinessTests(unittest.TestCase):
    def test_us_yahoo_partial_daily_candle_is_not_used_as_completed_volume(self) -> None:
        candles = _candles("AAPL", "yahoo-delayed", 80)
        candles[-1] = Candle(
            symbol="AAPL",
            ts="2026-05-22T13:30:00+00:00",
            open=190,
            high=191,
            low=189,
            close=190.5,
            volume=50_000,
            source="yahoo-delayed",
        )

        self.assertTrue(
            _should_drop_partial_us_yahoo_daily_candle(
                candles,
                now=datetime(2026, 5, 22, 14, 15, tzinfo=timezone.utc),
            )
        )
        self.assertFalse(
            _should_drop_partial_us_yahoo_daily_candle(
                candles,
                now=datetime(2026, 5, 22, 21, 15, tzinfo=timezone.utc),
            )
        )

    def test_us_fresh_yahoo_reference_data_can_drive_buy_signal_readiness(self) -> None:
        readiness = assess_phase2_data_readiness(
            row={"symbol": "AAPL", "exchange": "NASDAQ", "name": "Apple"},
            quote=Quote(symbol="AAPL", price=190, source="yahoo-delayed", asof=_now_iso(), volume=10_000_000),
            timeframe_candles={"daily": _candles("AAPL", "yahoo-delayed", 80), "intraday": []},
            sentiment={"status": "AVAILABLE", "score": 0.1, "source": "news", "headlines": ["Apple analyst raises target"]},
            delivery_data={},
            options_data={},
            sector_context={},
            market_breadth={},
            macro_event_context={"source": "calendar"},
            institutional_context={},
            full_spectrum={"liquidity_profile": {"volume_ratio_20": 1.2}},
        )

        self.assertTrue(readiness["trade_decision_ready"])
        self.assertNotIn("us_realtime_quote", readiness["missing_data"])
        self.assertNotIn("us_minute_bars", readiness["missing_data"])
        self.assertIn("us_sec_filings", readiness["missing_data"])
        self.assertIn("us_sec_filings", [item["key"] for item in readiness["soft_gaps"]])

    def test_stale_yahoo_reference_data_is_not_trade_decision_ready(self) -> None:
        readiness = assess_phase2_data_readiness(
            row={"symbol": "AAPL", "exchange": "NASDAQ", "name": "Apple"},
            quote=Quote(symbol="AAPL", price=190, source="yahoo-delayed", asof="2026-05-20T14:30:00+00:00", volume=10_000_000),
            timeframe_candles={"daily": _candles("AAPL", "yahoo-delayed", 80), "intraday": []},
            sentiment={"status": "AVAILABLE", "score": 0.1, "source": "news", "headlines": ["Apple analyst raises target"]},
            delivery_data={},
            options_data={},
            sector_context={},
            market_breadth={},
            macro_event_context={"source": "calendar"},
            institutional_context={},
            full_spectrum={"liquidity_profile": {"volume_ratio_20": 1.2}},
            execution_mode="paper",
        )

        self.assertFalse(readiness["trade_decision_ready"])
        self.assertEqual(readiness["mode"], "strict")
        self.assertIn("us_realtime_quote", readiness["missing_data"])
        self.assertIn("us_minute_bars", [item["key"] for item in readiness["hard_gaps"]])
        self.assertIn("us_sec_filings", [item["key"] for item in readiness["soft_gaps"]])

    def test_live_execution_blocks_yahoo_reference_mode_for_us_signals(self) -> None:
        readiness = assess_phase2_data_readiness(
            row={"symbol": "AAPL", "exchange": "NASDAQ", "name": "Apple"},
            quote=Quote(symbol="AAPL", price=190, source="yahoo-delayed", asof=_now_iso(), volume=10_000_000),
            timeframe_candles={"daily": _candles("AAPL", "yahoo-delayed", 80), "intraday": []},
            sentiment={"status": "AVAILABLE", "score": 0.1, "source": "news", "headlines": ["Apple analyst raises target"]},
            delivery_data={},
            options_data={},
            sector_context={},
            market_breadth={},
            macro_event_context={"source": "calendar"},
            institutional_context={},
            full_spectrum={"liquidity_profile": {"volume_ratio_20": 1.2}},
            execution_mode="upstox_live",
        )

        self.assertFalse(readiness["trade_decision_ready"])
        self.assertEqual(readiness["mode"], "strict")
        self.assertIn("us_realtime_quote", [item["key"] for item in readiness["hard_gaps"]])

    def test_us_trade_grade_quote_and_bars_pass_even_when_sec_context_is_soft_missing(self) -> None:
        readiness = assess_phase2_data_readiness(
            row={"symbol": "MSFT", "exchange": "NASDAQ", "name": "Microsoft"},
            quote=Quote(symbol="MSFT", price=430, source="alpaca-sip-live", asof="2026-05-20T14:30:00+00:00", volume=8_000_000),
            timeframe_candles={
                "daily": _candles("MSFT", "alpaca-sip-live:day", 80),
                "intraday": _candles("MSFT", "alpaca-sip-live:1minute", 40),
            },
            sentiment={"status": "AVAILABLE", "score": 0.2, "source": "news", "headlines": ["Microsoft analyst upgrade"]},
            delivery_data={},
            options_data={"source": "alpaca_options", "flow_available": True},
            sector_context={},
            market_breadth={},
            macro_event_context={"source": "earnings_calendar"},
            institutional_context={},
            full_spectrum={"liquidity_profile": {"volume_ratio_20": 1.4}},
        )

        self.assertTrue(readiness["trade_decision_ready"])
        self.assertIn("us_sec_filings", readiness["missing_data"])
        self.assertIn("us_sec_filings", [item["key"] for item in readiness["soft_gaps"]])

    def test_us_alpaca_sip_polygon_style_data_passes_hard_trade_checks(self) -> None:
        readiness = assess_phase2_data_readiness(
            row={"symbol": "MSFT", "exchange": "NASDAQ", "name": "Microsoft", "cik": "789019"},
            quote=Quote(symbol="MSFT", price=430, source="alpaca-sip-live", asof="2026-05-20T14:30:00+00:00", volume=8_000_000),
            timeframe_candles={
                "daily": _candles("MSFT", "alpaca-sip-live:day", 80),
                "intraday": _candles("MSFT", "alpaca-sip-live:1minute", 40),
            },
            sentiment={"status": "AVAILABLE", "score": 0.2, "source": "news", "headlines": ["Microsoft files 10-Q", "analyst upgrade"]},
            delivery_data={},
            options_data={"source": "alpaca_options", "flow_available": True},
            sector_context={},
            market_breadth={},
            macro_event_context={"source": "earnings_calendar"},
            institutional_context={},
            full_spectrum={"liquidity_profile": {"volume_ratio_20": 1.4}},
        )

        self.assertTrue(readiness["trade_decision_ready"])
        self.assertNotIn("us_realtime_quote", readiness["missing_data"])
        self.assertNotIn("us_sec_filings", readiness["missing_data"])

    def test_us_alpaca_iex_is_paper_reference_grade_with_soft_consolidated_tape_gap(self) -> None:
        readiness = assess_phase2_data_readiness(
            row={"symbol": "MSFT", "exchange": "NASDAQ", "name": "Microsoft", "cik": "789019"},
            quote=Quote(symbol="MSFT", price=430, source="alpaca-iex-live", asof="2026-05-20T14:30:00+00:00", volume=8_000_000),
            timeframe_candles={
                "daily": _candles("MSFT", "alpaca-iex-live:day", 80),
                "intraday": _candles("MSFT", "alpaca-iex-live:1minute", 40),
            },
            sentiment={"status": "AVAILABLE", "score": 0.2, "source": "news", "headlines": ["Microsoft files 10-Q", "analyst upgrade"]},
            delivery_data={},
            options_data={"source": "alpaca_options", "flow_available": True},
            sector_context={},
            market_breadth={},
            macro_event_context={"source": "earnings_calendar"},
            institutional_context={},
            full_spectrum={"liquidity_profile": {"volume_ratio_20": 1.4}},
            execution_mode="paper",
        )

        self.assertTrue(readiness["trade_decision_ready"])
        self.assertNotIn("us_realtime_quote", [item["key"] for item in readiness["hard_gaps"]])
        self.assertNotIn("us_minute_bars", [item["key"] for item in readiness["hard_gaps"]])
        self.assertIn("us_consolidated_tape", [item["key"] for item in readiness["soft_gaps"]])

    def test_us_alpaca_iex_is_not_live_execution_grade(self) -> None:
        readiness = assess_phase2_data_readiness(
            row={"symbol": "MSFT", "exchange": "NASDAQ", "name": "Microsoft", "cik": "789019"},
            quote=Quote(symbol="MSFT", price=430, source="alpaca-iex-live", asof="2026-05-20T14:30:00+00:00", volume=8_000_000),
            timeframe_candles={
                "daily": _candles("MSFT", "alpaca-iex-live:day", 80),
                "intraday": _candles("MSFT", "alpaca-iex-live:1minute", 40),
            },
            sentiment={"status": "AVAILABLE", "score": 0.2, "source": "news", "headlines": ["Microsoft files 10-Q", "analyst upgrade"]},
            delivery_data={},
            options_data={"source": "alpaca_options", "flow_available": True},
            sector_context={},
            market_breadth={},
            macro_event_context={"source": "earnings_calendar"},
            institutional_context={},
            full_spectrum={"liquidity_profile": {"volume_ratio_20": 1.4}},
            execution_mode="live",
        )

        self.assertFalse(readiness["trade_decision_ready"])
        self.assertIn("us_realtime_quote", [item["key"] for item in readiness["hard_gaps"]])
        self.assertIn("us_minute_bars", [item["key"] for item in readiness["hard_gaps"]])

    def test_india_missing_delivery_and_event_feeds_are_soft_sizing_gaps(self) -> None:
        readiness = assess_phase2_data_readiness(
            row={"symbol": "RELIANCE", "exchange": "NSE", "name": "Reliance"},
            quote=Quote(symbol="RELIANCE", price=2800, source="upstox-live", asof="2026-05-20T04:30:00+00:00", volume=2_000_000),
            timeframe_candles={
                "daily": _candles("RELIANCE", "upstox-live:day", 80),
                "intraday": _candles("RELIANCE", "upstox-live:1minute", 40),
            },
            sentiment={"status": "AVAILABLE", "score": 0.1, "source": "news", "headlines": ["Reliance result update"]},
            delivery_data={"available": False},
            options_data={"status": "ok", "source": "nse_option_chain_stock_level"},
            sector_context={},
            market_breadth={"breadth_regime": "bull_confirmed"},
            macro_event_context={},
            institutional_context={"feeds": {"fii_dii": {"status": "ok"}, "indices": {"status": "ok", "items": {"INDIA VIX": {"last": 13}}}}, "symbol_flags": {}},
            full_spectrum={"liquidity_profile": {"volume_ratio_20": 1.3}},
        )

        self.assertTrue(readiness["trade_decision_ready"])
        self.assertIn("in_delivery_pct", readiness["missing_data"])
        self.assertIn("in_corporate_announcements", readiness["missing_data"])
        self.assertIn("in_delivery_pct", [item["key"] for item in readiness["soft_gaps"]])
        self.assertNotIn("in_delivery_pct", [item["key"] for item in readiness["hard_gaps"]])

    def test_paper_execution_keeps_india_supporting_context_as_soft_gaps(self) -> None:
        readiness = assess_phase2_data_readiness(
            row={"symbol": "RELIANCE", "exchange": "NSE", "name": "Reliance"},
            quote=Quote(symbol="RELIANCE", price=2800, source="upstox-live", asof="2026-05-20T04:30:00+00:00", volume=2_000_000),
            timeframe_candles={
                "daily": _candles("RELIANCE", "upstox-live:day", 80),
                "intraday": _candles("RELIANCE", "upstox-live:1minute", 40),
            },
            sentiment={"status": "AVAILABLE", "score": 0.1, "source": "news", "headlines": ["Reliance result update"]},
            delivery_data={"available": False},
            options_data={"status": "data_missing", "source": "nse_option_chain_stock_level"},
            sector_context={},
            market_breadth={},
            macro_event_context={},
            institutional_context={"feeds": {}, "symbol_flags": {}},
            full_spectrum={"liquidity_profile": {"volume_ratio_20": 1.3}},
            execution_mode="paper",
        )

        self.assertTrue(readiness["trade_decision_ready"])
        self.assertIn("in_delivery_pct", readiness["missing_data"])
        self.assertIn("in_sector_breadth", readiness["missing_data"])
        self.assertIn("in_corporate_announcements", [item["key"] for item in readiness["soft_gaps"]])
        self.assertEqual(readiness["hard_gaps"], [])

    def test_phase2_hard_gaps_block_new_buy_rule_audit(self) -> None:
        context = {
            "quote": {"price": 100, "source": "yahoo-delayed"},
            "sentiment": {"score": 0.2, "status": "AVAILABLE", "headline_count": 2, "source": "news"},
            "position": {"qty": 0},
            "data_readiness": {
                "market_region": "US",
                "trade_decision_ready": False,
                "policy": "test policy",
                "hard_gaps": [{"key": "us_realtime_quote", "label": "US real-time quote", "source": "yahoo-delayed"}],
                "soft_gaps": [],
            },
            "full_spectrum_analysis": {
                "entry_quality": {"entry_grade": "A"},
                "trend_context": {"timeframe_alignment": {"alignment_grade": "A"}},
                "breakout_quality": {"breakout_quality": "confirmed"},
                "price_volume_divergence": {},
                "delivery_accumulation": {"net_bias": "neutral"},
                "sector_rotation": {"sector": "Technology", "industry": "Software"},
                "fundamental_quality": {"metrics": {"pe": 30, "market_cap": 1_000_000_000}},
            },
        }

        audit = evaluate_rules_for_context(context, {}, 100_000)

        self.assertTrue(audit["hard_blocked"])
        self.assertIn("DATA_READINESS_BLOCK", audit["active_flags"])

    def test_blocked_shortlist_still_gets_ai_review_but_cannot_buy(self) -> None:
        context = {
            "position": {"qty": 0},
            "system_gate_audit": {
                "hard_blocked": True,
                "active_flags": ["DATA_READINESS_BLOCK"],
            },
            "data_readiness": {
                "trade_decision_ready": False,
                "hard_gaps": [{"key": "us_realtime_quote", "label": "US real-time quote"}],
            },
            "decision_gate_context": {
                "failed_gates": [{"gate": "phase2_data_readiness", "reason": "phase2_data_not_trade_ready"}]
            },
            "full_spectrum_analysis": {
                "stage_analysis": {"stage": "Stage1_Base"},
                "entry_quality": {"entry_grade": "WATCH"},
                "strategy_logic_filters": {"hard_blocks": [{"flag": "SUSPECT_BREAKOUT_WITHOUT_VOLUME"}]},
            },
        }

        self.assertTrue(should_call_llm({"context": context}))
        self.assertEqual(_llm_buy_block_reason(context), "system_rules_hard_blocked_llm_buy")


def _candles(symbol: str, source: str, count: int) -> list[Candle]:
    return [
        Candle(
            symbol=symbol,
            ts=f"2026-01-{(index % 28) + 1:02d}T00:00:00+00:00",
            open=100 + index,
            high=102 + index,
            low=99 + index,
            close=101 + index,
            volume=1_000_000 + index * 1000,
            source=source,
        )
        for index in range(count)
    ]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


if __name__ == "__main__":
    unittest.main()
