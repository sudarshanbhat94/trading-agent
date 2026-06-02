from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from app.models import Candle, Quote
from app.opportunity_scanner import OpportunityScanner


class OpportunityScannerTests(unittest.TestCase):
    def test_stale_intraday_candles_are_visible_and_not_actionable(self) -> None:
        scanner = OpportunityScanner(_settings())
        row = {"symbol": "HFCL", "exchange": "NSE", "sector": "Telecom"}
        quote = Quote("HFCL", 103.0, "upstox-live", _india_session_iso(10, 30), open=100.0, high=104.0, low=99.5, volume=2_000_000)
        daily = _candles("HFCL", "upstox-live:day", 70, datetime(2026, 2, 1, tzinfo=timezone.utc))
        intraday = _candles("HFCL", "upstox-live:30minute", 24, datetime(2026, 5, 23, 9, 15, tzinfo=timezone.utc))

        item = scanner._score_row(
            row,
            quote,
            {"analysis": daily, "daily": daily, "intraday": intraday},
            False,
            {"score": 0.3, "confidence": 0.7, "events": [{"event_type": "earnings", "confidence": 0.6, "source_weight": 0.8}]},
        )

        missing = item["data_quality"]["missing"]
        self.assertIn("stale_intraday_candles", missing)
        self.assertTrue(item["data_quality"]["tradeable_screening"])
        self.assertFalse(item["data_quality"]["actionable_data_ready"])

    def test_current_session_intraday_candles_pass_actionable_freshness(self) -> None:
        scanner = OpportunityScanner(_settings())
        row = {"symbol": "HFCL", "exchange": "NSE", "sector": "Telecom"}
        quote = Quote("HFCL", 103.0, "upstox-live", _india_session_iso(10, 30), open=100.0, high=104.0, low=99.5, volume=2_000_000)
        daily = _candles("HFCL", "upstox-live:day", 70, datetime(2026, 2, 1, tzinfo=timezone.utc))
        intraday = _candles("HFCL", "upstox-live:30minute", 24, _india_intraday_start_utc())

        item = scanner._score_row(
            row,
            quote,
            {"analysis": daily, "daily": daily, "intraday": intraday},
            False,
            {"score": 0.3, "confidence": 0.7, "events": [{"event_type": "earnings", "confidence": 0.6, "source_weight": 0.8}]},
        )

        self.assertNotIn("stale_intraday_candles", item["data_quality"]["missing"])
        self.assertTrue(item["data_quality"]["actionable_data_ready"])

    def test_us_top_mover_uses_us_playbook_not_indian_rules(self) -> None:
        scanner = OpportunityScanner(_settings())
        candles = _candles("NVDA", "polygon:day", 260, datetime(2025, 5, 1, tzinfo=timezone.utc))
        pivot = max(candle.high for candle in candles[-41:-1])
        price = round(pivot * 1.02, 2)
        candles[-1] = Candle("NVDA", candles[-1].ts, price * 0.98, price * 1.01, price * 0.97, price, 8_000_000, "polygon:day")
        row = {
            "symbol": "NVDA",
            "name": "NVIDIA Corporation",
            "exchange": "NASDAQ",
            "sector": "Technology",
            "index_membership": "NASDAQ100",
            "market_cap": 4_000_000_000_000,
            "_market_action": {
                "symbol": "NVDA",
                "market_region": "US",
                "event_types": ["TOP_GAINER", "VOLUME_SHOCKER", "52_WEEK_HIGH"],
                "pct_change": 6.0,
                "price": price,
                "volume": 82_000_000,
                "avg_volume": 32_000_000,
                "volume_multiplier": 2.56,
            },
        }

        item = scanner._score_row(
            row,
            Quote("NVDA", price, "polygon-live", "2026-05-26T21:00:00+00:00", open=price * 0.96, high=price * 1.01, low=price * 0.95, volume=82_000_000),
            {"analysis": candles, "daily": candles},
            False,
            {"headlines": ["Nvidia earnings beat estimates and guidance raised"], "headline_count": 1, "score": 0.5, "confidence": 0.8, "events": [{"event_type": "earnings", "confidence": 0.8, "source_weight": 1.0}]},
            {"rs_rank": 96, "improving": True},
        )

        playbook = item["top_gainers_playbook"]
        self.assertTrue(playbook["available"])
        self.assertEqual(playbook["source"], "yahoo_us_top_movers_playbook")
        self.assertEqual(playbook["market_region"], "US")
        self.assertNotIn("delivery_pct", playbook["data_gaps"])
        self.assertEqual(playbook["delivery"]["trend"], "not_applicable_us")

    def test_btst_buy_candidate_scores_next_day_follow_through_setup(self) -> None:
        scanner = OpportunityScanner(_settings())
        row = {"symbol": "BTSTWIN", "exchange": "NSE", "sector": "Industrials"}
        daily = _candles("BTSTWIN", "upstox-live:day", 90, datetime(2026, 2, 1, tzinfo=timezone.utc))
        intraday = _candles("BTSTWIN", "upstox-live:30minute", 24, _india_intraday_start_utc())
        quote = Quote(
            "BTSTWIN",
            114.0,
            "upstox-live",
            _india_session_iso(15, 10),
            open=111.0,
            high=114.4,
            low=110.6,
            volume=2_200_000,
        )

        result = scanner.rank(
            [row],
            {"BTSTWIN": quote},
            {"BTSTWIN": {"analysis": daily, "daily": daily, "intraday": intraday}},
            sentiment_by_symbol={
                "BTSTWIN": {
                    "score": 0.3,
                    "confidence": 0.4,
                    "headline_count": 1,
                    "events": [{"event_type": "order_win", "confidence": 0.4, "source_weight": 0.8}],
                }
            },
        )

        self.assertEqual(result.candidates[0]["setup"], "btst_buy_candidate")
        self.assertEqual(result.candidates[0]["bucket"], "Actionable")
        self.assertTrue(result.candidates[0]["btst"]["detected"])
        self.assertEqual(result.candidates[0]["btst"]["action_bias"], "BUY")
        self.assertEqual(result.summary["btst_buy_candidates"][0]["symbol"], "BTSTWIN")

    def test_btst_rejects_late_chase_gap_risk(self) -> None:
        scanner = OpportunityScanner(_settings())
        row = {"symbol": "BTSTLATE", "exchange": "NSE", "sector": "Industrials"}
        daily = _candles("BTSTLATE", "upstox-live:day", 90, datetime(2026, 2, 1, tzinfo=timezone.utc))
        intraday = _candles("BTSTLATE", "upstox-live:30minute", 24, _india_intraday_start_utc())
        quote = Quote(
            "BTSTLATE",
            122.0,
            "upstox-live",
            _india_session_iso(15, 10),
            open=112.0,
            high=122.5,
            low=111.8,
            volume=5_500_000,
        )

        item = scanner._score_row(
            row,
            quote,
            {"analysis": daily, "daily": daily, "intraday": intraday},
            False,
            {},
            {"rs_rank": 82, "improving": True},
        )

        self.assertFalse(item["btst"]["detected"])
        self.assertFalse(item["btst"]["checks"]["day_move_ok"])
        self.assertNotEqual(item["setup"], "btst_buy_candidate")

    def test_india_slot_budgeting_refills_to_full_decision_target(self) -> None:
        scanner = OpportunityScanner(
            SimpleNamespace(
                **_settings().__dict__,
                india_full_decision_target=200,
                india_scanner_slot_budgets="live_rally=45,volume_price=40,breakout=35,delivery_btst=35,sector_rs=25,diverse=20",
            )
        )
        scored = []
        for index in range(250):
            if index < 20:
                setup = "opening_ignition"
                components = {"live_momentum": 0.78}
            elif index < 45:
                setup = "near_breakout"
                components = {"trend": 0.6}
            elif index < 70:
                setup = "btst_buy_candidate"
                components = {"btst": 0.75}
            else:
                setup = "trend_momentum"
                components = {"trend": 0.75}
            scored.append(
                {
                    "symbol": f"NSE{index}",
                    "market_region": "IN",
                    "score": 0.95 - index * 0.001,
                    "setup": setup,
                    "sector": f"Sector{index % 12}",
                    "metrics": {
                        "projected_turnover": 100_000_000 + index,
                        "volume_ratio": 1.0 + (index % 4) * 0.4,
                        "distance_to_55d_high_pct": 2.0 if setup == "near_breakout" else 8.0,
                    },
                    "components": components,
                    "market_action": {},
                    "btst": {"detected": setup == "btst_buy_candidate"},
                    "top_gainers_playbook": {},
                }
            )
        universe = [{"symbol": item["symbol"], "exchange": "NSE"} for item in scored]

        selected, summary = scanner._select_items(scored, 200, universe)

        self.assertEqual(len(selected), 200)
        self.assertEqual(len({item["symbol"] for item in selected}), 200)
        self.assertEqual(summary["mode"], "market_slot_budgeted")
        self.assertEqual(summary["target"], 200)
        self.assertEqual(summary["targets_by_market"]["IN"], 200)
        self.assertGreater(summary["fills_by_market"]["IN"].get("refill", 0), 0)
        self.assertLess(summary["fills_by_market"]["IN"]["live_rally"], summary["budgets_by_market"]["IN"]["live_rally"])

    def test_slot_budget_prioritizes_actionable_entries_over_watch_states(self) -> None:
        scanner = OpportunityScanner(
            SimpleNamespace(
                **_settings().__dict__,
                india_full_decision_target=200,
                india_scanner_slot_budgets="live_rally=45,volume_price=40,breakout=35,delivery_btst=35,sector_rs=25,diverse=20",
            )
        )
        scored = []
        for index in range(60):
            actionable = index < 15
            scored.append(
                {
                    "symbol": f"LIVE{index}",
                    "market_region": "IN",
                    "score": 0.70 if actionable else 0.95 - index * 0.001,
                    "setup": "intraday_momentum" if actionable else "extended_momentum_watch",
                    "bucket": "Actionable" if actionable else "Watch",
                    "sector": "Momentum",
                    "metrics": {"projected_turnover": 150_000_000 + index, "volume_ratio": 2.0},
                    "components": {"live_momentum": 0.82},
                    "market_action": {},
                    "rally_radar": {
                        "phase": "intraday_momentum" if actionable else "extended_momentum_watch",
                        "trade_window": "actionable_momentum" if actionable else "wait_for_pullback",
                    },
                    "btst": {"detected": False},
                    "top_gainers_playbook": {},
                }
            )

        selected, summary = scanner._select_market_slot_budgeted("IN", scored, 45)

        selected_symbols = {item["symbol"] for item in selected}
        self.assertEqual(summary["fills"]["live_rally"], 45)
        self.assertEqual(
            {f"LIVE{index}" for index in range(15)} & selected_symbols,
            {f"LIVE{index}" for index in range(15)},
        )

    def test_delivery_slot_prioritizes_btst_entries_over_pre_rally_watch(self) -> None:
        scanner = OpportunityScanner(
            SimpleNamespace(
                **_settings().__dict__,
                india_full_decision_target=200,
                india_scanner_slot_budgets="live_rally=45,volume_price=40,breakout=35,delivery_btst=35,sector_rs=25,diverse=20",
            )
        )
        scored = []
        for index in range(55):
            btst = index < 10
            scored.append(
                {
                    "symbol": f"BTST{index}",
                    "market_region": "IN",
                    "score": 0.72 if btst else 0.96 - index * 0.001,
                    "setup": "btst_buy_candidate" if btst else "pre_rally_fuel",
                    "bucket": "Actionable" if btst else "Watch",
                    "sector": "Delivery",
                    "metrics": {"projected_turnover": 120_000_000 + index, "volume_ratio": 1.4},
                    "components": {"btst": 0.75 if btst else 0.0},
                    "market_action": {},
                    "rally_radar": {
                        "phase": "none" if btst else "pre_rally_fuel",
                        "trade_window": "not_ready" if btst else "watch_for_ignition",
                    },
                    "btst": {"detected": btst},
                    "top_gainers_playbook": {},
                }
            )

        selected, summary = scanner._select_market_slot_budgeted("IN", scored, 35)

        selected_symbols = {item["symbol"] for item in selected}
        self.assertEqual(summary["fills"]["delivery_btst"], 35)
        self.assertEqual(
            {f"BTST{index}" for index in range(10)} & selected_symbols,
            {f"BTST{index}" for index in range(10)},
        )

    def test_india_scan_keeps_soft_quality_rejects_for_full_decisioning(self) -> None:
        base_settings = _settings().__dict__.copy()
        base_settings.update(
            {
                "dynamic_scan_min_score": 0.99,
                "dynamic_scan_require_active_setup": True,
                "dynamic_scan_sentiment_enabled": False,
                "dynamic_scan_min_turnover_inr": 1_000_000.0,
                "india_full_decision_target": 200,
            }
        )
        settings = SimpleNamespace(**base_settings)
        scanner = OpportunityScanner(settings)
        daily = _candles("SOFT", "upstox-live:day", 70, datetime(2026, 2, 1, tzinfo=timezone.utc))
        universe = [{"symbol": f"SOFT{index}", "exchange": "NSE", "sector": f"Sector{index % 10}"} for index in range(220)]
        quotes = {
            row["symbol"]: Quote(
                row["symbol"],
                100.0,
                "upstox-live",
                _india_session_iso(11, 0),
                open=100.0,
                high=101.0,
                low=99.5,
                volume=50_000,
            )
            for row in universe
        }
        candle_sets = {row["symbol"]: {"daily": daily, "analysis": daily} for row in universe}

        result = scanner.rank(universe, quotes, candle_sets)

        self.assertEqual(len(result.selected_universe), 200)
        self.assertNotIn("below_opportunity_score", result.summary["rejected_counts"])
        self.assertEqual(result.summary["soft_predecision_reject_counts"], {})
        self.assertEqual(result.summary["target_decision_symbols"], 200)

    def test_us_slot_budgeting_refills_to_full_decision_target(self) -> None:
        base_settings = _settings().__dict__.copy()
        base_settings.update(
            {
                "us_full_decision_target": 200,
                "us_scanner_slot_budgets": "live_rally=45,volume_price=40,breakout=40,earnings_news=30,sector_rs=25,diverse=20",
            }
        )
        scanner = OpportunityScanner(SimpleNamespace(**base_settings))
        scored = []
        for index in range(260):
            if index < 35:
                setup = "earnings_beat_gap_and_go"
                sentiment = {"positive_catalyst": True, "events": [{"event_type": "earnings_beat"}]}
                components = {"live_momentum": 0.55}
            elif index < 70:
                setup = "near_breakout"
                sentiment = {}
                components = {"trend": 0.6}
            elif index < 105:
                setup = "market_action_momentum"
                sentiment = {}
                components = {"live_momentum": 0.78}
            else:
                setup = "trend_momentum"
                sentiment = {}
                components = {"trend": 0.76}
            scored.append(
                {
                    "symbol": f"US{index}",
                    "market_region": "US",
                    "score": 0.96 - index * 0.001,
                    "setup": setup,
                    "sector": f"Sector{index % 15}",
                    "metrics": {
                        "projected_turnover": 20_000_000 + index,
                        "volume_ratio": 1.0 + (index % 5) * 0.35,
                        "distance_to_55d_high_pct": 1.5 if setup == "near_breakout" else 6.0,
                        "return_60d_pct": 18.0 if setup == "trend_momentum" else 4.0,
                    },
                    "components": components,
                    "market_action": {"event_types": ["TOP_GAINER"]} if setup == "market_action_momentum" else {},
                    "sentiment": sentiment,
                    "btst": {},
                    "top_gainers_playbook": {},
                }
            )
        universe = [{"symbol": item["symbol"], "exchange": "NASDAQ"} for item in scored]

        selected, summary = scanner._select_items(scored, 200, universe)

        self.assertEqual(len(selected), 200)
        self.assertEqual(len({item["symbol"] for item in selected}), 200)
        self.assertEqual(summary["targets_by_market"]["US"], 200)
        self.assertEqual(summary["budgets_by_market"]["US"]["earnings_news"], 30)
        self.assertGreater(summary["fills_by_market"]["US"].get("refill", 0), 0)
        self.assertGreater(summary["fills_by_market"]["US"].get("earnings_news", 0), 0)

    def test_mixed_india_us_scan_targets_both_markets(self) -> None:
        base_settings = _settings().__dict__.copy()
        base_settings.update(
            {
                "india_full_decision_target": 200,
                "us_full_decision_target": 200,
                "dynamic_scan_min_score": 0.99,
                "dynamic_scan_require_active_setup": True,
                "dynamic_scan_sentiment_enabled": False,
                "dynamic_scan_min_turnover_inr": 1_000_000.0,
                "dynamic_scan_min_turnover_usd": 100_000.0,
            }
        )
        scanner = OpportunityScanner(SimpleNamespace(**base_settings))
        india_rows = [{"symbol": f"INMIX{index}", "exchange": "NSE", "sector": f"IN{index % 8}"} for index in range(220)]
        us_rows = [{"symbol": f"USMIX{index}", "exchange": "NASDAQ", "sector": f"US{index % 8}"} for index in range(220)]
        universe = [*india_rows, *us_rows]
        daily_in = _candles("INMIX", "upstox-live:day", 70, datetime(2026, 2, 1, tzinfo=timezone.utc))
        daily_us = _candles("USMIX", "yahoo-delayed", 70, datetime(2026, 2, 1, tzinfo=timezone.utc))
        quotes = {
            row["symbol"]: Quote(
                row["symbol"],
                100.0,
                "upstox-live" if row["exchange"] == "NSE" else "yahoo-delayed",
                _india_session_iso(11, 0),
                open=100.0,
                high=101.0,
                low=99.5,
                volume=1_000_000,
            )
            for row in universe
        }
        candle_sets = {
            row["symbol"]: {"daily": daily_in if row["exchange"] == "NSE" else daily_us, "analysis": daily_in if row["exchange"] == "NSE" else daily_us}
            for row in universe
        }

        result = scanner.rank(universe, quotes, candle_sets)

        self.assertEqual(len(result.selected_universe), 400)
        self.assertEqual(result.summary["target_decision_symbols"], 400)
        self.assertEqual(result.summary["target_decision_symbols_by_market"], {"IN": 200, "US": 200})
        selected_by_market = {
            "IN": sum(1 for row in result.selected_universe if row["exchange"] == "NSE"),
            "US": sum(1 for row in result.selected_universe if row["exchange"] == "NASDAQ"),
        }
        self.assertEqual(selected_by_market, {"IN": 200, "US": 200})


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        dynamic_scan_candidate_limit=60,
        dynamic_scan_min_score=0.50,
        dynamic_scan_require_active_setup=False,
        dynamic_scan_min_price=10.0,
        dynamic_scan_min_turnover_inr=40_000_000.0,
        dynamic_scan_min_turnover_usd=2_000_000.0,
        dynamic_scan_breakout_distance_pct=3.0,
        dynamic_scan_sentiment_enabled=True,
        dynamic_scan_sentiment_weight=0.12,
    )


def _india_session_iso(hour: int, minute: int) -> str:
    local = datetime.now(ZoneInfo("Asia/Kolkata")).replace(hour=hour, minute=minute, second=0, microsecond=0)
    return local.isoformat()


def _india_intraday_start_utc() -> datetime:
    local = datetime.now(ZoneInfo("Asia/Kolkata")).replace(hour=9, minute=15, second=0, microsecond=0)
    return local.astimezone(timezone.utc)


def _candles(symbol: str, source: str, count: int, start: datetime) -> list[Candle]:
    output: list[Candle] = []
    for index in range(count):
        ts = start + timedelta(days=index if "day" in source else 0, minutes=30 * index if "minute" in source else 0)
        close = 80.0 + index * 0.35
        output.append(
            Candle(
                symbol=symbol,
                ts=ts.isoformat(),
                open=close * 0.995,
                high=close * 1.015,
                low=close * 0.985,
                close=close,
                volume=900_000 + index * 1_000,
                source=source,
            )
        )
    return output


if __name__ == "__main__":
    unittest.main()
