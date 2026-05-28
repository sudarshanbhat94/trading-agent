from __future__ import annotations

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

from app.models import Candle, Quote
from app.pre_catalyst_engine import (
    EARNINGS_VCP_BREAKOUT,
    DATA_STALE_WATCH,
    LATE_CHASE_AVOID,
    LOW_QUALITY_SHORT_COVERING,
    OpportunityCandidate,
    OVERHANG_REMOVAL_RERATE,
    PRE_MOMENTUM_EXPANSION_WATCH,
    PRE_CATALYST_WATCH,
    SECTOR_ROTATION_LEADER,
    UC_PRE_BREAKOUT_WATCH,
    _balanced_candidate_selection,
    build_pre_catalyst_watchlist,
    confirm_live_breakout,
    review_missed_moves,
)


class PreCatalystEngineTests(unittest.TestCase):
    def test_pre_rally_compression_uses_daily_close_when_quote_is_missing(self) -> None:
        candles = _vcp_candles("COIL", start=78, end=98)
        universe = [{"symbol": "COIL", "name": "Compression Leader", "exchange": "NSE", "sector": "Industrials"}]

        result = build_pre_catalyst_watchlist(
            universe,
            {},
            {"COIL": {"daily": candles, "analysis": candles}},
            settings=_settings(),
            now=datetime(2026, 5, 24, tzinfo=timezone.utc),
        )

        self.assertEqual(result["analysis_quote_fallback_symbols"], 1)
        self.assertEqual(result["candidates"][0]["symbol"], "COIL")
        self.assertEqual(result["candidates"][0]["label"], PRE_CATALYST_WATCH)
        self.assertEqual(result["candidates"][0]["catalyst_type"], "unknown")
        self.assertTrue(result["candidates"][0]["supporting_signals"]["setup"]["pre_rally_compression"])
        self.assertIn("pre-rally compression", " ".join(result["candidates"][0]["key_reasons"]))

    def test_earnings_vcp_candidate_is_pre_catalyst_watch(self) -> None:
        candles = _vcp_candles("AREM", start=78, end=98)
        universe = [{"symbol": "AREM", "name": "Amara Raja", "exchange": "NSE", "sector": "Auto Components"}]
        quotes = {"AREM": Quote("AREM", 98.0, "upstox-live", "2026-05-24T10:00:00+00:00", open=97.0, high=99.0, low=96.0, volume=900_000)}

        result = build_pre_catalyst_watchlist(
            universe,
            quotes,
            {"AREM": {"daily": candles, "analysis": candles}},
            macro_calendar_context={"events": [{"date": "2026-05-26", "type": "earnings", "symbols": ["AREM"]}]},
            settings=_settings(),
            now=datetime(2026, 5, 25, tzinfo=timezone.utc),
        )

        self.assertEqual(result["candidates"][0]["symbol"], "AREM")
        self.assertEqual(result["candidates"][0]["label"], PRE_CATALYST_WATCH)
        self.assertEqual(result["candidates"][0]["catalyst_type"], "earnings")
        self.assertTrue(result["candidates"][0]["supporting_signals"]["setup"]["volume_dryup"])

        live = confirm_live_breakout(
            result["candidates"][0],
            Quote("AREM", 101.2, "upstox-live", "2026-05-26T04:15:00+00:00", open=100.5, high=102.0, low=99.8, volume=2_200_000),
            {"daily": candles, "analysis": candles, "intraday": _intraday_hold("AREM", 100.0, 101.2)},
            {"event_types": ["TOP_GAINER", "VOLUME_SHOCKER"], "strategy": "market_action_momentum"},
            {"score": 0.45, "confidence": 0.7, "events": [{"event_type": "earnings", "confidence": 0.8, "source_weight": 0.8}]},
        )
        self.assertEqual(live["label"], EARNINGS_VCP_BREAKOUT)

    def test_overhang_removal_is_detected(self) -> None:
        candles = _trend_candles("ADANI", start=130, end=92, volume=1_200_000)
        universe = [{"symbol": "ADANI", "name": "Adani Enterprises", "exchange": "NSE", "sector": "Infrastructure"}]
        quotes = {"ADANI": Quote("ADANI", 94.0, "upstox-live", "2026-05-25T10:00:00+00:00", open=91.0, high=95.0, low=90.5, volume=2_000_000)}
        sentiment = {
            "ADANI": {
                "score": 0.45,
                "confidence": 0.75,
                "headlines": ["Adani probe charges dropped after settlement"],
                "events": [{"event_type": "legal_regulatory", "score": 0.5, "confidence": 0.8, "source_weight": 0.8, "title": "probe charges dropped"}],
            }
        }

        result = build_pre_catalyst_watchlist(
            universe,
            quotes,
            {"ADANI": {"daily": candles, "analysis": candles}},
            sentiment_by_symbol=sentiment,
            settings=_settings(),
        )

        self.assertEqual(result["candidates"][0]["label"], OVERHANG_REMOVAL_RERATE)
        self.assertTrue(result["candidates"][0]["supporting_signals"]["overhang_removal"]["detected"])

    def test_sector_rotation_leader_uses_macro_beneficiary_mapping(self) -> None:
        universe = [
            {"symbol": "PAINT1", "exchange": "NSE", "sector": "Paints"},
            {"symbol": "PAINT2", "exchange": "NSE", "sector": "Paints"},
            {"symbol": "BANK1", "exchange": "NSE", "sector": "Banks"},
        ]
        candles = {
            "PAINT1": _trend_candles("PAINT1", start=70, end=105, volume=900_000),
            "PAINT2": _trend_candles("PAINT2", start=70, end=82, volume=700_000),
            "BANK1": _trend_candles("BANK1", start=90, end=88, volume=700_000),
        }
        quotes = {
            "PAINT1": Quote("PAINT1", 105.0, "upstox-live", "2026-05-25T10:00:00+00:00", open=103.0, high=106.0, low=102.0, volume=1_200_000),
            "PAINT2": Quote("PAINT2", 82.0, "upstox-live", "2026-05-25T10:00:00+00:00", open=81.0, high=83.0, low=80.0, volume=800_000),
            "BANK1": Quote("BANK1", 88.0, "upstox-live", "2026-05-25T10:00:00+00:00", open=88.0, high=89.0, low=87.0, volume=800_000),
        }

        result = build_pre_catalyst_watchlist(
            universe,
            quotes,
            {symbol: {"daily": items, "analysis": items} for symbol, items in candles.items()},
            macro_context={"markets": [{"symbol": "CL=F", "label": "Crude Oil", "change_pct": -2.2}]},
            settings=_settings(),
        )

        top = result["candidates"][0]
        self.assertEqual(top["symbol"], "PAINT1")
        self.assertEqual(top["label"], SECTOR_ROTATION_LEADER)
        self.assertIn("crude_down", top["supporting_signals"]["sector_rotation"]["drivers"])

    def test_low_quality_short_covering_bounce_is_conservative(self) -> None:
        candles = _trend_candles("OLOW", start=160, end=48, volume=1_000_000)
        universe = [{"symbol": "OLOW", "exchange": "NSE", "sector": "EV Auto", "short_interest": 12.0}]
        quotes = {"OLOW": Quote("OLOW", 50.5, "upstox-live", "2026-05-25T10:00:00+00:00", open=47.0, high=51.0, low=46.8, volume=2_500_000)}
        sentiment = {
            "OLOW": {
                "score": -0.35,
                "confidence": 0.7,
                "headlines": ["Brokerage maintains sell rating on cash burn concerns"],
                "events": [{"event_type": "analyst_downgrade", "score": -0.5, "confidence": 0.8, "source_weight": 0.8}],
            }
        }

        result = build_pre_catalyst_watchlist(
            universe,
            quotes,
            {"OLOW": {"daily": candles, "analysis": candles}},
            sentiment_by_symbol=sentiment,
            market_action_summary={"events_by_symbol": {"OLOW": {"strategy": "top_gainer_momentum"}}},
            settings=_settings(),
        )

        candidate = result["candidates"][0]
        self.assertEqual(candidate["label"], LOW_QUALITY_SHORT_COVERING)
        self.assertEqual(candidate["supporting_signals"]["short_covering"]["position_size_hint"], "tiny_only")

    def test_uc_pre_breakout_watch_uses_price_band_history(self) -> None:
        candles = _uc_setup_candles("UCLEAD")
        universe = [{"symbol": "UCLEAD", "exchange": "NSE", "sector": "Specialty Chemicals", "market_cap_cr": 1200}]
        quotes = {"UCLEAD": Quote("UCLEAD", 99.2, "upstox-live", "2026-05-25T10:00:00+00:00", open=98.8, high=100.0, low=98.4, volume=400_000)}

        result = build_pre_catalyst_watchlist(
            universe,
            quotes,
            {"UCLEAD": {"daily": candles, "analysis": candles}},
            previous_state={
                "market_action_history": {
                    "by_symbol": {
                        "UCLEAD": {
                            "only_buyers_days": 1,
                            "strong_mover_days": 1,
                            "active_dates": ["2026-05-20"],
                        }
                    }
                }
            },
            settings=_settings(),
        )

        candidate = result["candidates"][0]
        self.assertEqual(candidate["label"], UC_PRE_BREAKOUT_WATCH)
        self.assertEqual(candidate["catalyst_type"], "price_band_demand")
        self.assertTrue(candidate["supporting_signals"]["uc_pre_breakout"]["detected"])
        self.assertIn("UC/price-band", " ".join(candidate["key_reasons"]))

    def test_pre_move_expansion_watch_catches_before_5_to_15_pct_move(self) -> None:
        candles = _expansion_candles("EXPAND")
        universe = [{"symbol": "EXPAND", "exchange": "NSE", "sector": "Capital Goods"}]
        quotes = {"EXPAND": Quote("EXPAND", 118.0, "upstox-live", "2026-05-25T10:00:00+00:00", open=117.0, high=119.0, low=116.7, volume=1_400_000)}

        result = build_pre_catalyst_watchlist(
            universe,
            quotes,
            {"EXPAND": {"daily": candles, "analysis": candles}},
            settings=_settings(),
        )

        candidate = result["candidates"][0]
        self.assertEqual(candidate["label"], PRE_MOMENTUM_EXPANSION_WATCH)
        self.assertEqual(candidate["catalyst_type"], "technical_expansion")
        self.assertTrue(candidate["supporting_signals"]["pre_move_expansion"]["detected"])
        self.assertIn("pre-move expansion", " ".join(candidate["key_reasons"]))

    def test_only_buyers_current_event_is_late_chase_avoid(self) -> None:
        candles = _uc_setup_candles("LOCKED")
        universe = [{"symbol": "LOCKED", "exchange": "NSE", "sector": "Industrials", "market_cap_cr": 1500}]
        quotes = {"LOCKED": Quote("LOCKED", 120.0, "upstox-live", "2026-05-25T10:00:00+00:00", open=100.0, high=120.0, low=100.0, volume=2_000_000)}

        result = build_pre_catalyst_watchlist(
            universe,
            quotes,
            {"LOCKED": {"daily": candles, "analysis": candles}},
            market_action_summary={"events": [{"symbol": "LOCKED", "event_types": ["TOP_GAINER", "ONLY_BUYERS"], "strategy": "circuit_demand_lock"}]},
            settings=_settings(),
        )

        candidate = result["candidates"][0]
        self.assertEqual(candidate["label"], LATE_CHASE_AVOID)
        self.assertEqual(candidate["supporting_signals"]["uc_pre_breakout"]["position_size_hint"], "none_until_tradable_pullback")
        self.assertIn("do not chase", " ".join(candidate["key_reasons"]))

    def test_late_chase_avoid_blocks_extended_live_breakout(self) -> None:
        candles = _vcp_candles("CHASE", start=80, end=98)
        candidate = {
            "symbol": "CHASE",
            "label": PRE_CATALYST_WATCH,
            "confidence": 0.75,
            "pivot": 100.0,
            "catalyst_type": "earnings",
        }

        live = confirm_live_breakout(
            candidate,
            Quote("CHASE", 110.0, "upstox-live", "2026-05-26T05:00:00+00:00", open=101.0, high=111.0, low=100.8, volume=3_000_000),
            {"daily": candles, "analysis": candles, "intraday": _intraday_hold("CHASE", 101.0, 110.0)},
            {"event_types": ["TOP_GAINER", "VOLUME_SHOCKER"]},
            {"score": 0.5, "confidence": 0.8, "events": [{"event_type": "earnings", "confidence": 0.8, "source_weight": 0.8}]},
        )

        self.assertEqual(live["label"], LATE_CHASE_AVOID)
        self.assertIn("late chase risk", " ".join(live["key_reasons"]))

    def test_live_breakout_waits_when_intraday_candles_are_stale(self) -> None:
        candles = _vcp_candles("FRESH", start=80, end=98)
        candidate = {
            "symbol": "FRESH",
            "label": PRE_CATALYST_WATCH,
            "confidence": 0.75,
            "pivot": 100.0,
            "catalyst_type": "earnings",
        }

        live = confirm_live_breakout(
            candidate,
            Quote("FRESH", 101.5, "upstox-live", "2026-05-26T10:00:00+05:30", open=100.5, high=102.0, low=100.1, volume=2_800_000),
            {"daily": candles, "analysis": candles, "intraday": _intraday_hold("FRESH", 100.0, 101.5, day="2026-05-23")},
            {"event_types": ["TOP_GAINER", "VOLUME_SHOCKER"], "strategy": "market_action_momentum"},
            {"score": 0.45, "confidence": 0.7, "events": [{"event_type": "earnings", "confidence": 0.8, "source_weight": 0.8}]},
        )

        self.assertEqual(live["label"], DATA_STALE_WATCH)
        self.assertFalse(live["intraday_fresh"])
        self.assertTrue(live["data_stale"])
        self.assertIn("waiting for fresh intraday", " ".join(live["key_reasons"]))

    def test_missed_move_review_marks_absent_and_watched_movers(self) -> None:
        review = review_missed_moves(
            {
                "events": [
                    {"symbol": "MISSED", "event_types": ["TOP_GAINER"], "pct_change": 9.4, "strategy": "top_gainer_momentum"},
                    {"symbol": "READY", "event_types": ["VOLUME_SHOCKER"], "pct_change": 5.8, "strategy": "market_action_momentum"},
                ]
            },
            previous_state={"candidates": [{"symbol": "READY", "label": PRE_CATALYST_WATCH}]},
        )

        by_symbol = {item["symbol"]: item for item in review["items"]}
        self.assertEqual(by_symbol["MISSED"]["status"], "absent_from_prior_watchlist")
        self.assertEqual(by_symbol["READY"]["status"], "correctly_watched_before_move")
        self.assertIn("absent_from_prior_watchlist", review["status_counts"])

    def test_candidate_limit_keeps_india_and_us_replay_coverage(self) -> None:
        candidates = [
            _candidate("US1", "US", 0.95),
            _candidate("US2", "US", 0.94),
            _candidate("US3", "US", 0.93),
            _candidate("US4", "US", 0.92),
            _candidate("IN1", "IN", 0.71),
            _candidate("IN2", "IN", 0.70),
        ]

        selected = _balanced_candidate_selection(candidates, 4)
        markets = [item.market_region for item in selected]

        self.assertEqual(len(selected), 4)
        self.assertEqual(markets.count("IN"), 2)
        self.assertEqual(markets.count("US"), 2)


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        pre_catalyst_engine_enabled=True,
        pre_catalyst_candidate_limit=10,
        pre_catalyst_min_score=0.50,
        dynamic_scan_min_turnover_inr=40_000_000,
        dynamic_scan_min_turnover_usd=2_000_000,
    )


def _candidate(symbol: str, market_region: str, score: float) -> OpportunityCandidate:
    return OpportunityCandidate(
        symbol=symbol,
        label=PRE_CATALYST_WATCH,
        confidence=score,
        score=score,
        market_region=market_region,
        catalyst_type="technical_expansion",
        catalyst_date=None,
        setup_summary="unit-test",
        entry_zone={"low": 99.0, "high": 101.0},
        pivot=100.0,
        invalidation_level=94.0,
        key_reasons=["unit-test"],
        supporting_signals={"setup": {"near_pivot": True}},
    )


def _vcp_candles(symbol: str, start: float, end: float) -> list[Candle]:
    candles: list[Candle] = []
    count = 66
    for index in range(count):
        progress = index / (count - 1)
        center = start + (end - start) * progress
        width = 18.0 if index < 22 else 11.0 if index < 44 else 5.8
        high = center + width / 2
        low = center - width / 2
        volume = 1_200_000 if index < 22 else 820_000 if index < 44 else 420_000
        candles.append(
            Candle(
                symbol=symbol,
                ts=f"2026-03-{(index % 28) + 1:02d}",
                open=center - 0.4,
                high=high,
                low=low,
                close=center,
                volume=volume,
                source="unit-test",
            )
        )
    return candles


def _trend_candles(symbol: str, start: float, end: float, volume: float) -> list[Candle]:
    candles: list[Candle] = []
    count = 90
    for index in range(count):
        progress = index / (count - 1)
        close = start + (end - start) * progress
        candles.append(
            Candle(
                symbol=symbol,
                ts=f"2026-02-{(index % 28) + 1:02d}",
                open=close * 0.995,
                high=close * 1.015,
                low=close * 0.985,
                close=close,
                volume=volume,
                source="unit-test",
            )
        )
    return candles


def _uc_setup_candles(symbol: str) -> list[Candle]:
    candles: list[Candle] = []
    price = 80.0
    for index in range(70):
        if index == 48:
            price *= 1.052
        else:
            price *= 1.004 if index > 35 else 1.001
        width = 5.0 if index < 45 else 2.2
        volume = 650_000 if index < 45 else 260_000
        candles.append(
            Candle(
                symbol=symbol,
                ts=f"2026-04-{(index % 28) + 1:02d}",
                open=price - width * 0.35,
                high=price + width * 0.45,
                low=price - width * 0.55,
                close=price + width * 0.35,
                volume=volume,
                source="unit-test",
            )
        )
    return candles


def _expansion_candles(symbol: str) -> list[Candle]:
    candles: list[Candle] = []
    price = 82.0
    for index in range(76):
        progress = index / 75
        price = 82.0 + 36.0 * progress
        width = 7.5 if index < 40 else 3.8 if index < 62 else 2.0
        volume = 700_000
        if index >= 58:
            volume = 1_100_000 if index % 3 != 0 else 820_000
        close = price + (width * 0.30 if index >= 58 else 0.0)
        candles.append(
            Candle(
                symbol=symbol,
                ts=f"2026-04-{(index % 28) + 1:02d}",
                open=price - width * 0.20,
                high=price + width * 0.45,
                low=price - width * 0.55,
                close=close,
                volume=volume,
                source="unit-test",
            )
        )
    return candles


def _intraday_hold(symbol: str, start: float, end: float, day: str = "2026-05-26") -> list[Candle]:
    candles: list[Candle] = []
    for index in range(30):
        progress = index / 29
        close = start + (end - start) * progress
        candles.append(
            Candle(
                symbol=symbol,
                ts=f"{day}T09:{index:02d}:00+05:30",
                open=close - 0.15,
                high=close + 0.35,
                low=close - 0.35,
                close=close,
                volume=20_000 + index * 500,
                source="unit-test:1minute",
            )
        )
    return candles


if __name__ == "__main__":
    unittest.main()
