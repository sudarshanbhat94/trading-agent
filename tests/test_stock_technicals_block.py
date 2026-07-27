"""The technicals block served by /v2/api/stock.

This block is informational and sits inside the stock-detail endpoint, so the
requirements are: correct numbers, readable prose, JSON-serialisable output,
and — most importantly — it must never be able to break the page it lives on.
"""

from __future__ import annotations

import json
import unittest

from app.v2_web import _technical_block


def _series(n: int, start: float = 100.0, step: float = 0.5):
    closes = [start + i * step for i in range(n)]
    opens = [c - 0.2 for c in closes]
    highs = [c + 1.0 for c in closes]
    lows = [c - 1.0 for c in closes]
    volumes = [1000.0 + i for i in range(n)]
    return opens, highs, lows, closes, volumes


class PayloadShapeTest(unittest.TestCase):
    def test_full_history_populates_every_field(self) -> None:
        opens, highs, lows, closes, volumes = _series(120)
        payload, summary = _technical_block(opens, highs, lows, closes, volumes, closes[-1])

        for key in ("atr", "vwap", "supertrend", "ichimoku", "pivot_points", "fibonacci", "patterns"):
            self.assertIn(key, payload)
        self.assertIsNotNone(payload["atr"])
        self.assertIsNotNone(payload["vwap"])
        self.assertEqual(payload["supertrend"]["direction"], "up")
        self.assertIsNotNone(payload["ichimoku"]["kijun"])
        self.assertTrue(payload["pivot_points"])
        self.assertTrue(summary)

    def test_payload_is_json_serialisable(self) -> None:
        """It goes straight into a JSONResponse, so numpy/NaN types would 500."""
        opens, highs, lows, closes, volumes = _series(120)
        payload, _ = _technical_block(opens, highs, lows, closes, volumes, closes[-1])
        encoded = json.dumps(payload)
        self.assertIn("supertrend", encoded)

    def test_values_are_rounded_for_display(self) -> None:
        opens, highs, lows, closes, volumes = _series(120, start=100.123456)
        payload, _ = _technical_block(opens, highs, lows, closes, volumes, closes[-1])
        self.assertEqual(payload["vwap"], round(payload["vwap"], 2))
        self.assertEqual(payload["atr"], round(payload["atr"], 2))

    def test_empty_history_returns_empty_block(self) -> None:
        payload, summary = _technical_block([], [], [], [], [], 0.0)
        self.assertEqual(payload, {})
        self.assertEqual(summary, "")

    def test_short_history_degrades_instead_of_failing(self) -> None:
        opens, highs, lows, closes, volumes = _series(3)
        payload, summary = _technical_block(opens, highs, lows, closes, volumes, closes[-1])
        self.assertIsNone(payload["atr"])            # needs 15 bars
        self.assertIsNotNone(payload["vwap"])        # works on any bars
        self.assertIsNone(payload["ichimoku"]["kijun"])
        self.assertIsInstance(summary, str)


class SummaryProseTest(unittest.TestCase):
    def test_reports_price_above_vwap(self) -> None:
        opens, highs, lows, closes, volumes = _series(120)
        payload, summary = _technical_block(opens, highs, lows, closes, volumes, closes[-1])
        self.assertIn("above its 20-day volume-weighted price", summary)

    def test_reports_price_below_vwap(self) -> None:
        opens, highs, lows, closes, volumes = _series(120)
        _, summary = _technical_block(opens, highs, lows, closes, volumes, 1.0)
        self.assertIn("below its 20-day volume-weighted price", summary)

    def test_uptrend_line_is_described_as_support(self) -> None:
        opens, highs, lows, closes, volumes = _series(120)
        _, summary = _technical_block(opens, highs, lows, closes, volumes, closes[-1])
        self.assertIn("SuperTrend is up", summary)
        self.assertIn("support", summary)

    def test_downtrend_line_is_described_as_resistance(self) -> None:
        opens, highs, lows, closes, volumes = _series(120, start=200.0, step=-0.8)
        _, summary = _technical_block(opens, highs, lows, closes, volumes, closes[-1])
        self.assertIn("SuperTrend is down", summary)
        self.assertIn("resistance", summary)

    def test_currency_symbol_is_honoured(self) -> None:
        opens, highs, lows, closes, volumes = _series(120)
        _, summary = _technical_block(opens, highs, lows, closes, volumes, closes[-1], ccy="$")
        self.assertIn("$", summary)
        self.assertNotIn("₹", summary)

    def test_named_candlestick_pattern_reaches_the_prose(self) -> None:
        opens, highs, lows, closes, volumes = _series(120)
        # Force a bullish engulfing on the final bar.
        opens[-2], closes[-2] = 200.0, 195.0
        highs[-2], lows[-2] = 201.0, 194.0
        opens[-1], closes[-1] = 194.0, 202.0
        highs[-1], lows[-1] = 203.0, 193.0
        payload, summary = _technical_block(opens, highs, lows, closes, volumes, closes[-1])
        self.assertIn("bullish_engulfing", payload["patterns"])
        self.assertIn("bullish engulfing", summary)

    def test_summary_is_one_sentence_with_a_prefix(self) -> None:
        opens, highs, lows, closes, volumes = _series(120)
        _, summary = _technical_block(opens, highs, lows, closes, volumes, closes[-1])
        self.assertTrue(summary.startswith("Technicals: "))
        self.assertTrue(summary.endswith("."))


class StalenessTest(unittest.TestCase):
    """The daily candle feed can lag the live quote by a session. When it does,
    every indicator here is as-of the last candle, and the UI must not imply
    the numbers are current."""

    def test_stale_when_live_price_diverges_from_last_close(self) -> None:
        opens, highs, lows, closes, volumes = _series(120)
        live = closes[-1] * 1.11          # the KFINTECH case: +11% since the candle
        payload, summary = _technical_block(
            opens, highs, lows, closes, volumes, live, as_of="2026-07-24"
        )
        self.assertTrue(payload["stale"])
        self.assertEqual(payload["as_of"], "2026-07-24")
        self.assertIn("stale", summary)
        self.assertIn("2026-07-24", summary)
        self.assertIn("+11.0%", summary)

    def test_not_stale_when_price_is_close_to_last_candle(self) -> None:
        opens, highs, lows, closes, volumes = _series(120)
        payload, summary = _technical_block(
            opens, highs, lows, closes, volumes, closes[-1] * 1.001, as_of="2026-07-27"
        )
        self.assertFalse(payload["stale"])
        self.assertIn("as of 2026-07-27", summary)
        self.assertNotIn("stale", summary)

    def test_no_as_of_means_no_claim_either_way(self) -> None:
        opens, highs, lows, closes, volumes = _series(120)
        payload, summary = _technical_block(opens, highs, lows, closes, volumes, closes[-1])
        self.assertIsNone(payload["as_of"])
        self.assertFalse(payload["stale"])
        self.assertNotIn("as of", summary)


class RobustnessTest(unittest.TestCase):
    """The block must not be able to break the stock page."""

    def test_zero_volume_history(self) -> None:
        opens, highs, lows, closes, _ = _series(120)
        payload, summary = _technical_block(opens, highs, lows, closes, [0.0] * 120, closes[-1])
        self.assertIsNone(payload["vwap"])
        self.assertIsInstance(summary, str)

    def test_flat_series_does_not_divide_by_zero(self) -> None:
        n = 120
        payload, summary = _technical_block(
            [100.0] * n, [100.0] * n, [100.0] * n, [100.0] * n, [1000.0] * n, 100.0
        )
        self.assertIsInstance(payload, dict)
        self.assertIsInstance(summary, str)

    def test_zero_price_does_not_raise(self) -> None:
        opens, highs, lows, closes, volumes = _series(120)
        payload, summary = _technical_block(opens, highs, lows, closes, volumes, 0.0)
        self.assertIsInstance(payload, dict)
        self.assertIsInstance(summary, str)


if __name__ == "__main__":
    unittest.main()
