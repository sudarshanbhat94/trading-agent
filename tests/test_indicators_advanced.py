"""Additional technical indicators.

Expected values are hand-computed from the formulas rather than captured from
the implementation, so these tests can actually catch a wrong formula instead
of just pinning current behaviour.
"""

from __future__ import annotations

import unittest

from app import indicators as ind


class AtrTest(unittest.TestCase):
    def test_atr_matches_hand_computation(self) -> None:
        # Each bar has range 2.0 and no gaps, so every true range is 2.0.
        highs = [11.0] * 20
        lows = [9.0] * 20
        closes = [10.0] * 20
        self.assertAlmostEqual(ind.atr(highs, lows, closes, window=14), 2.0)

    def test_gap_is_included_in_true_range(self) -> None:
        # Last bar gaps up: high 20, low 19, previous close 10 -> TR = 10.
        highs = [11.0] * 14 + [20.0]
        lows = [9.0] * 14 + [19.0]
        closes = [10.0] * 14 + [19.5]
        ranges = ind._true_ranges(highs, lows, closes)
        self.assertAlmostEqual(ranges[-1], 10.0)

    def test_none_when_history_too_short(self) -> None:
        self.assertIsNone(ind.atr([11.0] * 5, [9.0] * 5, [10.0] * 5, window=14))

    def test_none_on_mismatched_lengths(self) -> None:
        self.assertIsNone(ind.atr([11.0] * 20, [9.0] * 19, [10.0] * 20))


class VwapTest(unittest.TestCase):
    def test_weights_by_volume(self) -> None:
        # Typical prices 10 and 20; volumes 1 and 3 -> (10*1 + 20*3)/4 = 17.5
        highs, lows, closes = [10.0, 20.0], [10.0, 20.0], [10.0, 20.0]
        self.assertAlmostEqual(ind.vwap(highs, lows, closes, [1.0, 3.0]), 17.5)

    def test_uses_typical_price_not_close(self) -> None:
        # Typical = (12 + 6 + 9)/3 = 9, not the close of 9... make it distinct:
        # H=12 L=6 C=12 -> typical = 10, close = 12.
        self.assertAlmostEqual(ind.vwap([12.0], [6.0], [12.0], [100.0]), 10.0)

    def test_window_limits_the_lookback(self) -> None:
        highs = lows = closes = [10.0, 10.0, 20.0]
        self.assertAlmostEqual(ind.vwap(highs, lows, closes, [1.0, 1.0, 1.0], window=1), 20.0)

    def test_zero_volume_returns_none(self) -> None:
        self.assertIsNone(ind.vwap([10.0], [10.0], [10.0], [0.0]))

    def test_window_longer_than_history_returns_none(self) -> None:
        self.assertIsNone(ind.vwap([10.0], [10.0], [10.0], [5.0], window=5))


class SupertrendTest(unittest.TestCase):
    def _trend(self, closes, highs=None, lows=None, **kwargs):
        highs = highs or [c + 1 for c in closes]
        lows = lows or [c - 1 for c in closes]
        return ind.supertrend(highs, lows, closes, **kwargs)

    def test_sustained_uptrend_is_up_and_line_sits_below_price(self) -> None:
        closes = [100.0 + i for i in range(40)]
        result = self._trend(closes, period=10, multiplier=3.0)
        self.assertEqual(result["direction"], "up")
        self.assertLess(result["value"], closes[-1])

    def test_sustained_downtrend_is_down_and_line_sits_above_price(self) -> None:
        closes = [100.0 - i for i in range(40)]
        result = self._trend(closes, period=10, multiplier=3.0)
        self.assertEqual(result["direction"], "down")
        self.assertGreater(result["value"], closes[-1])

    def test_direction_flips_after_a_reversal(self) -> None:
        rising = [100.0 + i for i in range(40)]
        self.assertEqual(self._trend(rising)["direction"], "up")
        reversed_series = rising + [rising[-1] - 12 * i for i in range(1, 15)]
        self.assertEqual(self._trend(reversed_series)["direction"], "down")

    def test_none_when_history_too_short(self) -> None:
        result = ind.supertrend([11.0] * 5, [9.0] * 5, [10.0] * 5, period=10)
        self.assertIsNone(result["value"])
        self.assertIsNone(result["direction"])


class IchimokuTest(unittest.TestCase):
    def test_components_match_hand_computation(self) -> None:
        # 60 bars, highs 100..159, lows 50..109.
        highs = [100.0 + i for i in range(60)]
        lows = [50.0 + i for i in range(60)]
        closes = [75.0 + i for i in range(60)]
        result = ind.ichimoku(highs, lows, closes)

        # tenkan: last 9 bars -> high 159, low 101 -> 130
        self.assertAlmostEqual(result["tenkan"], (159 + 101) / 2)
        # kijun: last 26 bars -> high 159, low 84 -> 121.5
        self.assertAlmostEqual(result["kijun"], (159 + 84) / 2)
        # senkou_a: mean of the two
        self.assertAlmostEqual(result["senkou_a"], (result["tenkan"] + result["kijun"]) / 2)
        # senkou_b: last 52 bars -> high 159, low 58
        self.assertAlmostEqual(result["senkou_b"], (159 + 58) / 2)
        self.assertAlmostEqual(result["chikou"], closes[-1])

    def test_degrades_field_by_field_on_short_history(self) -> None:
        highs = [100.0 + i for i in range(10)]
        lows = [50.0 + i for i in range(10)]
        closes = [75.0 + i for i in range(10)]
        result = ind.ichimoku(highs, lows, closes)
        self.assertIsNotNone(result["tenkan"])   # needs 9
        self.assertIsNone(result["kijun"])       # needs 26
        self.assertIsNone(result["senkou_b"])    # needs 52
        self.assertIsNone(result["senkou_a"])    # needs kijun


class PivotPointTest(unittest.TestCase):
    def test_classic_levels(self) -> None:
        # H=110 L=90 C=100 -> PP = 100, span = 20
        levels = ind.pivot_points(110.0, 90.0, 100.0)
        self.assertAlmostEqual(levels["pivot"], 100.0)
        self.assertAlmostEqual(levels["r1"], 110.0)   # 2*100 - 90
        self.assertAlmostEqual(levels["s1"], 90.0)    # 2*100 - 110
        self.assertAlmostEqual(levels["r2"], 120.0)   # PP + span
        self.assertAlmostEqual(levels["s2"], 80.0)    # PP - span
        self.assertAlmostEqual(levels["r3"], 130.0)   # H + 2*(PP-L)
        self.assertAlmostEqual(levels["s3"], 70.0)    # L - 2*(H-PP)

    def test_levels_are_ordered(self) -> None:
        levels = ind.pivot_points(118.0, 91.0, 103.0)
        self.assertLess(levels["s3"], levels["s2"])
        self.assertLess(levels["s2"], levels["s1"])
        self.assertLess(levels["s1"], levels["pivot"])
        self.assertLess(levels["pivot"], levels["r1"])
        self.assertLess(levels["r1"], levels["r2"])
        self.assertLess(levels["r2"], levels["r3"])

    def test_fibonacci_method(self) -> None:
        levels = ind.pivot_points(110.0, 90.0, 100.0, method="fibonacci")
        self.assertAlmostEqual(levels["pivot"], 100.0)
        self.assertAlmostEqual(levels["r1"], 100 + 0.382 * 20)
        self.assertAlmostEqual(levels["s2"], 100 - 0.618 * 20)

    def test_unknown_method_raises(self) -> None:
        with self.assertRaises(ValueError):
            ind.pivot_points(110.0, 90.0, 100.0, method="woodie")

    def test_inverted_range_returns_empty(self) -> None:
        self.assertEqual(ind.pivot_points(90.0, 110.0, 100.0), {})


class FibonacciTest(unittest.TestCase):
    def test_uptrend_retracement_measures_down_from_the_high(self) -> None:
        levels = ind.fibonacci_levels(200.0, 100.0, uptrend=True)
        self.assertAlmostEqual(levels["0.0%"], 200.0)
        self.assertAlmostEqual(levels["50.0%"], 150.0)
        self.assertAlmostEqual(levels["61.8%"], 200 - 61.8)
        self.assertAlmostEqual(levels["100.0%"], 100.0)

    def test_downtrend_retracement_measures_up_from_the_low(self) -> None:
        levels = ind.fibonacci_levels(200.0, 100.0, uptrend=False)
        self.assertAlmostEqual(levels["0.0%"], 100.0)
        self.assertAlmostEqual(levels["50.0%"], 150.0)
        self.assertAlmostEqual(levels["100.0%"], 200.0)

    def test_inverted_range_returns_empty(self) -> None:
        self.assertEqual(ind.fibonacci_levels(100.0, 200.0), {})


class CandlestickPatternTest(unittest.TestCase):
    def test_doji(self) -> None:
        found = ind.candlestick_patterns([100.0], [105.0], [95.0], [100.2])
        self.assertIn("doji", found)

    def test_hammer_needs_a_long_lower_shadow(self) -> None:
        # body 1 (100->101), lower shadow 5, upper shadow ~0
        found = ind.candlestick_patterns([100.0], [101.2], [95.0], [101.0])
        self.assertIn("hammer", found)

    def test_shooting_star(self) -> None:
        # bearish body, long upper shadow
        found = ind.candlestick_patterns([101.0], [107.0], [99.8], [100.0])
        self.assertIn("shooting_star", found)

    def test_bullish_engulfing(self) -> None:
        opens = [105.0, 99.0]
        closes = [100.0, 106.0]
        highs = [105.5, 106.5]
        lows = [99.5, 98.5]
        self.assertIn("bullish_engulfing", ind.candlestick_patterns(opens, highs, lows, closes))

    def test_bearish_engulfing(self) -> None:
        opens = [100.0, 106.0]
        closes = [105.0, 99.0]
        highs = [105.5, 106.5]
        lows = [99.5, 98.5]
        self.assertIn("bearish_engulfing", ind.candlestick_patterns(opens, highs, lows, closes))

    def test_engulfing_requires_opposite_colours(self) -> None:
        # two bullish bars, second larger: must NOT be engulfing
        opens = [100.0, 99.0]
        closes = [101.0, 106.0]
        highs = [101.5, 106.5]
        lows = [99.5, 98.5]
        self.assertNotIn("bullish_engulfing", ind.candlestick_patterns(opens, highs, lows, closes))

    def test_morning_star(self) -> None:
        opens = [110.0, 99.0, 100.0]
        closes = [100.0, 98.5, 107.0]
        highs = [110.5, 99.5, 107.5]
        lows = [99.5, 98.0, 99.5]
        self.assertIn("morning_star", ind.candlestick_patterns(opens, highs, lows, closes))

    def test_evening_star(self) -> None:
        opens = [100.0, 111.0, 110.0]
        closes = [110.0, 111.5, 103.0]
        highs = [110.5, 112.0, 110.5]
        lows = [99.5, 110.5, 102.5]
        self.assertIn("evening_star", ind.candlestick_patterns(opens, highs, lows, closes))

    def test_marubozu(self) -> None:
        found = ind.candlestick_patterns([100.0], [110.0], [100.0], [110.0])
        self.assertIn("bullish_marubozu", found)

    def test_empty_and_mismatched_input(self) -> None:
        self.assertEqual(ind.candlestick_patterns([], [], [], []), [])
        self.assertEqual(ind.candlestick_patterns([1.0], [2.0], [0.5], []), [])

    def test_zero_range_bar_is_not_a_pattern(self) -> None:
        self.assertEqual(ind.candlestick_patterns([100.0], [100.0], [100.0], [100.0]), [])


class AdvancedSnapshotTest(unittest.TestCase):
    def _series(self, n: int):
        closes = [100.0 + i * 0.5 for i in range(n)]
        opens = [c - 0.2 for c in closes]
        highs = [c + 1.0 for c in closes]
        lows = [c - 1.0 for c in closes]
        volumes = [1000.0 + i for i in range(n)]
        return opens, highs, lows, closes, volumes

    def test_full_history_populates_everything(self) -> None:
        snapshot = ind.advanced_snapshot(*self._series(60))
        self.assertIsNotNone(snapshot["atr"])
        self.assertIsNotNone(snapshot["vwap"])
        self.assertIsNotNone(snapshot["supertrend"]["value"])
        self.assertIsNotNone(snapshot["ichimoku"]["kijun"])
        self.assertTrue(snapshot["pivot_points"])
        self.assertTrue(snapshot["fibonacci"])

    def test_short_history_degrades_field_by_field(self) -> None:
        snapshot = ind.advanced_snapshot(*self._series(3))
        self.assertIsNone(snapshot["atr"])                 # needs 15
        self.assertIsNotNone(snapshot["vwap"])             # works on any bars
        self.assertTrue(snapshot["pivot_points"])          # needs 2
        self.assertEqual(snapshot["fibonacci"], {})        # needs 20

    def test_empty_input_does_not_raise(self) -> None:
        snapshot = ind.advanced_snapshot([], [], [], [], [])
        self.assertIsNone(snapshot["atr"])
        self.assertEqual(snapshot["candlestick_patterns"], [])


if __name__ == "__main__":
    unittest.main()
