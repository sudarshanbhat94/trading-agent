from __future__ import annotations

import unittest

from app.market_action_radar import build_market_action_event, build_yahoo_market_action_event, parse_moneycontrol_market_stats


class MarketActionRadarTests(unittest.TestCase):
    def test_moneycontrol_next_data_parser_extracts_stock_rows(self) -> None:
        html = """
        <html><body>
        <script id="__NEXT_DATA__" type="application/json">
        {"props":{"pageProps":{"initialData":{"stocks":[
          {"stockName":"HFCL","symbol":"HFCL","currentPrice":"193.75","perChange":"10.00%",
           "currPerChange":"10.00","high":"193.75","low":"178.00","open":"180.00",
           "prevClose":"176.15","volume":"65,000,000","avgVol":"12,000,000",
           "volMultiplier":"5.42","value":"1,260","vwap":"190.40",
           "shareUrl":"https://www.moneycontrol.com/hfcl",
           "stockLabel":[{"shortname":"Vol Shocker","name":"Volume Shocker"}]}
        ]}}}}
        </script>
        </body></html>
        """

        rows = parse_moneycontrol_market_stats(html, "top-gainers")

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["symbol"], "HFCL")
        self.assertEqual(rows[0]["_market_stats_category"], "top-gainers")

    def test_build_event_classifies_52_week_volume_breakout(self) -> None:
        event = build_market_action_event(
            {
                "stockName": "HFCL",
                "symbol": "HFCL",
                "currPerChange": "10.00",
                "currentPrice": "193.75",
                "high": "193.75",
                "volume": "65,000,000",
                "avgVol": "12,000,000",
                "volMultiplier": "5.42",
                "value": "1,260",
                "stockLabel": [
                    {"shortname": "Vol Shocker", "name": "Volume Shocker"},
                    {"shortname": "52WK H", "name": "52 Week High"},
                ],
            },
            "top-gainers",
        )

        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(event.symbol, "HFCL")
        self.assertEqual(event.strategy, "52_week_high_volume_breakout")
        self.assertIn("VOLUME_SHOCKER", event.event_types)
        self.assertIn("52_WEEK_HIGH", event.event_types)
        self.assertGreaterEqual(event.market_action_score, 80)

    def test_only_buyers_classifies_as_circuit_demand_lock(self) -> None:
        event = build_market_action_event(
            {
                "stockName": "EMMVEE",
                "symbol": "EMMVEE",
                "currPerChange": "10.00",
                "volMultiplier": "2.40",
                "stockLabel": [{"shortname": "Only Buyers", "name": "Only Buyers"}],
            },
            "only-buyers",
        )

        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(event.strategy, "circuit_demand_lock")
        self.assertEqual(event.trade_window, "watch_for_pullback")

    def test_yahoo_quote_classifies_us_52_week_volume_breakout(self) -> None:
        event = build_yahoo_market_action_event(
            {
                "symbol": "NVDA",
                "shortName": "NVIDIA Corporation",
                "regularMarketPrice": 198.5,
                "regularMarketDayHigh": 199.0,
                "regularMarketDayLow": 187.4,
                "regularMarketOpen": 188.0,
                "regularMarketPreviousClose": 187.0,
                "regularMarketChangePercent": 6.15,
                "regularMarketVolume": 82_000_000,
                "averageDailyVolume10Day": 32_000_000,
                "fiftyTwoWeekHigh": 200.0,
                "regularMarketTime": 1_779_720_000,
            },
            {"symbol": "NVDA", "exchange": "NASDAQ", "sector": "Technology"},
            ["TOP_GAINER"],
        )

        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(event.market_region, "US")
        self.assertEqual(event.strategy, "52_week_high_volume_breakout")
        self.assertIn("52_WEEK_HIGH", event.event_types)
        self.assertIn("VOLUME_SHOCKER", event.event_types)
        self.assertGreaterEqual(event.market_action_score, 80)

    def test_yahoo_most_active_without_move_is_not_market_action(self) -> None:
        event = build_yahoo_market_action_event(
            {
                "symbol": "AAPL",
                "regularMarketPrice": 190.0,
                "regularMarketChangePercent": 0.4,
                "regularMarketVolume": 50_000_000,
                "averageDailyVolume10Day": 48_000_000,
                "fiftyTwoWeekHigh": 260.0,
            },
            {"symbol": "AAPL", "exchange": "NASDAQ"},
            ["MOST_ACTIVE"],
        )

        self.assertIsNone(event)


if __name__ == "__main__":
    unittest.main()
