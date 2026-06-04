from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from app.india_top_gainers import evaluate_indian_top_gainer_playbook
from app.models import Candle, Quote
from app.us_top_movers import build_us_playbook_dashboard, evaluate_us_top_mover_playbook


class UsTopMoversPlaybookTests(unittest.TestCase):
    def test_strong_us_52_week_volume_breakout_gets_deterministic_buy(self) -> None:
        candles = _stage2_candles("NVDA", count=260)
        breakout_price = round(candles[-1].close, 2)
        quote = Quote(
            "NVDA",
            breakout_price,
            "polygon-live",
            "2026-05-26T21:00:00+00:00",
            open=breakout_price * 0.96,
            high=breakout_price * 1.01,
            low=breakout_price * 0.95,
            volume=82_000_000,
        )

        result = evaluate_us_top_mover_playbook(
            row={
                "symbol": "NVDA",
                "name": "NVIDIA Corporation",
                "exchange": "NASDAQ",
                "sector": "Technology",
                "index_membership": "NASDAQ100",
                "market_cap": 4_000_000_000_000,
            },
            quote=quote,
            candles=candles,
            market_action={
                "symbol": "NVDA",
                "market_region": "US",
                "event_types": ["TOP_GAINER", "VOLUME_SHOCKER", "52_WEEK_HIGH"],
                "pct_change": 6.15,
                "price": breakout_price,
                "volume": 82_000_000,
                "avg_volume": 32_000_000,
                "volume_multiplier": 2.56,
                "reason": "top gainer, volume shocker, near 52-week high",
            },
            sentiment={"headlines": ["Nvidia earnings beat estimates with revenue growth and guidance raised"], "headline_count": 1},
            rs_context={"rs_rank": 96, "improving": True},
        )

        self.assertTrue(result["available"])
        self.assertEqual(result["source"], "yahoo_us_top_movers_playbook")
        self.assertEqual(result["market_region"], "US")
        self.assertEqual(result["tier"], "TIER 1")
        self.assertGreaterEqual(result["quant_score"], 70)
        self.assertIn(result["final_signal"], {"STRONG BUY", "MODERATE BUY"})
        self.assertEqual(result["levels"]["stop_rule"], "atr_aware_2_4_to_4_2_pct_below_entry")
        self.assertLess(result["levels"]["stop"], result["levels"]["entry"])
        self.assertLessEqual(((result["levels"]["entry"] - result["levels"]["stop"]) / result["levels"]["entry"]) * 100, 4.3)

    def test_us_playbook_blocks_otc_low_liquidity_movers(self) -> None:
        candles = _stage2_candles("PUMP", count=90)
        result = evaluate_us_top_mover_playbook(
            row={"symbol": "PUMP", "name": "Pump Co", "exchange": "OTC", "market_cap": 120_000_000},
            quote=Quote("PUMP", 1.8, "yahoo", "2026-05-26T21:00:00+00:00", volume=200_000),
            candles=candles,
            market_action={
                "symbol": "PUMP",
                "market_region": "US",
                "event_types": ["TOP_GAINER"],
                "pct_change": 18.0,
                "price": 1.8,
                "volume": 200_000,
                "avg_volume": 80_000,
                "volume_multiplier": 2.5,
            },
            sentiment={"headlines": []},
            rs_context={"rs_rank": 65, "improving": True},
        )

        self.assertTrue(result["hard_excluded"])
        self.assertEqual(result["final_signal"], "AVOID")
        self.assertIn("otc_or_pink_sheet", result["hard_excludes"])
        self.assertIn("price_below_2", result["hard_excludes"])

    def test_indian_playbook_does_not_process_us_yahoo_top_gainers(self) -> None:
        candles = _stage2_candles("NVDA", count=80)
        result = evaluate_indian_top_gainer_playbook(
            row={"symbol": "NVDA", "exchange": "NASDAQ"},
            quote=Quote("NVDA", 198.5, "yahoo", "2026-05-26T21:00:00+00:00", volume=82_000_000),
            candles=candles,
            market_action={"symbol": "NVDA", "market_region": "US", "event_types": ["TOP_GAINER"], "pct_change": 6.15},
            sentiment={},
            rs_context={},
        )

        self.assertFalse(result["available"])
        self.assertEqual(result["reason"], "not_indian_market")

    def test_dashboard_orders_us_buy_signals_first(self) -> None:
        dashboard = build_us_playbook_dashboard(
            [
                {"available": True, "market_region": "US", "symbol": "A", "final_signal": "WATCH", "quant_score": 60, "gain_pct": 8, "catalyst_review": {"catalyst_type": "TECHNICAL_BREAKOUT"}},
                {"available": True, "market_region": "US", "symbol": "B", "final_signal": "STRONG BUY", "quant_score": 86, "gain_pct": 7, "catalyst_review": {"catalyst_type": "EARNINGS_BEAT"}},
            ]
        )

        self.assertEqual(dashboard["label"], "US Top Movers Playbook")
        self.assertEqual(dashboard["records"][0]["symbol"], "B")
        self.assertEqual(dashboard["signal_summary"]["strong_buy"], 1)
        self.assertEqual(dashboard["tomorrow_watchlist"][0]["symbol"], "A")


def _stage2_candles(symbol: str, count: int) -> list[Candle]:
    start = datetime(2025, 5, 1, tzinfo=timezone.utc)
    candles: list[Candle] = []
    price = 120.0
    for index in range(count):
        contraction = 1.0 - min(index / max(count, 1), 0.75) * 0.45
        drift = 0.0026 if index > count // 3 else 0.0007
        price = price * (1 + drift)
        width = max(0.012, 0.06 * contraction)
        candles.append(
            Candle(
                symbol=symbol,
                ts=(start + timedelta(days=index)).isoformat(),
                open=price * 0.995,
                high=price * (1 + width),
                low=price * (1 - width),
                close=price,
                volume=3_000_000 + max(count - index, 0) * 8_000,
                source="polygon:day",
            )
        )
    if len(candles) >= 45:
        pivot = max(candle.high for candle in candles[-41:-1])
        close = pivot * 1.025
        candles[-1] = Candle(
            symbol=symbol,
            ts=candles[-1].ts,
            open=close * 0.97,
            high=close * 1.006,
            low=close * 0.965,
            close=close,
            volume=8_000_000,
            source="polygon:day",
        )
    return candles


if __name__ == "__main__":
    unittest.main()
