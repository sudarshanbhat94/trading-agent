from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from app.india_top_gainers import build_playbook_dashboard, evaluate_indian_top_gainer_playbook
from app.models import Candle, Quote


class IndiaTopGainersPlaybookTests(unittest.TestCase):
    def test_strong_stage2_volume_breakout_gets_deterministic_buy(self) -> None:
        candles = _stage2_candles("SUPRAJIT", count=260)
        breakout_price = round(candles[-1].close, 2)
        quote = Quote(
            "SUPRAJIT",
            breakout_price,
            "upstox-live",
            "2026-05-26T11:58:00+05:30",
            open=breakout_price * 0.93,
            high=breakout_price * 1.01,
            low=breakout_price * 0.92,
            volume=3_559_290,
        )
        result = evaluate_indian_top_gainer_playbook(
            row={"symbol": "SUPRAJIT", "name": "SUPRAJIT ENGINEERING", "exchange": "NSE"},
            quote=quote,
            candles=candles,
            market_action={
                "symbol": "SUPRAJIT",
                "event_types": ["TOP_GAINER", "VOLUME_SHOCKER", "52_WEEK_HIGH"],
                "pct_change": 7.24,
                "price": breakout_price,
                "volume": 3_559_290,
                "avg_volume": 200_000,
                "volume_multiplier": 30.83,
                "market_cap_cr": 6545,
                "value_crore": 170,
                "stock_labels": ["Vol Shocker Volume Shocker Above average volume in stock today"],
            },
            sentiment={"headlines": ["Suprajit Engineering reports profit growth and revenue improvement"]},
            rs_context={"rs_rank": 94, "improving": True},
        )

        self.assertTrue(result["available"])
        self.assertEqual(result["tier"], "TIER 2")
        self.assertGreaterEqual(result["quant_score"], 70)
        self.assertIn(result["final_signal"], {"STRONG BUY", "MODERATE BUY"})
        self.assertIsNotNone(result["levels"]["pivot"])
        self.assertLess(result["levels"]["stop"], result["levels"]["entry"])
        self.assertEqual(result["catalyst_review"]["llm_product_term"], "Catalyst Review")

    def test_hard_exclude_blocks_low_market_cap(self) -> None:
        candles = _stage2_candles("KANDARP", count=80)
        result = evaluate_indian_top_gainer_playbook(
            row={"symbol": "KANDARP", "name": "KANDARP", "exchange": "NSE"},
            quote=Quote("KANDARP", 153, "upstox-live", "2026-05-26T11:58:00+05:30", volume=26_000),
            candles=candles,
            market_action={
                "symbol": "KANDARP",
                "event_types": ["TOP_GAINER"],
                "pct_change": 7.37,
                "price": 153,
                "volume": 26_000,
                "avg_volume": 78_400,
                "volume_multiplier": 0.33,
                "market_cap_cr": 137,
            },
            sentiment={"headlines": []},
            rs_context={"rs_rank": 55, "improving": False},
        )

        self.assertTrue(result["hard_excluded"])
        self.assertEqual(result["final_signal"], "AVOID")
        self.assertIn("market_cap_below_200cr", result["hard_excludes"])

    def test_cached_asm_flag_hard_excludes_before_dashboard_buy(self) -> None:
        candles = _stage2_candles("CEMPRO", count=260)
        price = round(candles[-1].close, 2)
        result = evaluate_indian_top_gainer_playbook(
            row={"symbol": "CEMPRO", "name": "Cemindia Projects", "exchange": "NSE", "_asm_surveillance": True},
            quote=Quote("CEMPRO", price, "upstox-live", "2026-05-26T11:58:00+05:30", volume=2_000_000),
            candles=candles,
            market_action={
                "symbol": "CEMPRO",
                "event_types": ["TOP_GAINER", "VOLUME_SHOCKER", "52_WEEK_HIGH"],
                "pct_change": 7.34,
                "price": price,
                "volume": 2_000_000,
                "avg_volume": 300_000,
                "volume_multiplier": 6.67,
                "market_cap_cr": 16_226,
                "stock_labels": ["Vol Shocker Volume Shocker"],
            },
            sentiment={"headlines": ["Cemindia Projects posts profit growth"]},
            rs_context={"rs_rank": 90, "improving": True},
        )

        self.assertTrue(result["hard_excluded"])
        self.assertEqual(result["final_signal"], "AVOID")
        self.assertIn("asm_or_gsm_surveillance", result["hard_excludes"])

    def test_dashboard_orders_buy_signals_first(self) -> None:
        dashboard = build_playbook_dashboard(
            [
                {"available": True, "symbol": "A", "final_signal": "WATCH", "quant_score": 60, "gain_pct": 8, "catalyst_review": {"catalyst_type": "TECHNICAL_BREAKOUT"}},
                {"available": True, "symbol": "B", "final_signal": "STRONG BUY", "quant_score": 85, "gain_pct": 7, "catalyst_review": {"catalyst_type": "EARNINGS_BEAT"}},
            ]
        )

        self.assertEqual(dashboard["records"][0]["symbol"], "B")
        self.assertEqual(dashboard["signal_summary"]["strong_buy"], 1)
        self.assertEqual(dashboard["tomorrow_watchlist"][0]["symbol"], "A")


def _stage2_candles(symbol: str, count: int) -> list[Candle]:
    start = datetime(2025, 5, 1, tzinfo=timezone.utc)
    candles: list[Candle] = []
    price = 90.0
    for index in range(count):
        contraction = 1.0 - min(index / max(count, 1), 0.75) * 0.45
        drift = 0.0028 if index > count // 3 else 0.0007
        price = price * (1 + drift)
        width = max(0.012, 0.07 * contraction)
        high = price * (1 + width)
        low = price * (1 - width)
        if index == count - 1:
            high = max(high, price * 1.10)
            price = high * 0.99
        candles.append(
            Candle(
                symbol=symbol,
                ts=(start + timedelta(days=index)).isoformat(),
                open=price * 0.99,
                high=high,
                low=low,
                close=price,
                volume=900_000 + index * 4_000,
                source="upstox-live:day",
            )
        )
    return candles


if __name__ == "__main__":
    unittest.main()
