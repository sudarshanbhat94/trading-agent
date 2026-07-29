"""Price levels.

A candle pattern without a level is noise — the same rejection wick means
nothing mid-range and a great deal at yesterday's high. These supply the
location that conditions everything else.

The cases that matter are the boundaries: a gap is only "unfilled" until some
later bar overlaps it, a swing high is only confirmed once price has failed to
exceed it, and "at a level" has to mean CLOSE to it, because a wick two percent
away is not a reaction to anything.
"""

from __future__ import annotations

import unittest

from app import levels


def bar(date, o, h, l, c):
    return (date, o, h, l, c)


def flat_series(n=30, base=100.0):
    return [bar(f"d{i}", base, base + 1, base - 1, base) for i in range(n)]


class PreviousDayTest(unittest.TestCase):
    def test_reads_the_bar_before_last_not_the_last(self) -> None:
        """PDH is YESTERDAY's high. Using today's would leak the session that
        has not finished."""
        bars = [bar("d1", 100, 110, 90, 105), bar("d2", 105, 120, 100, 115)]
        out = levels.previous_day(bars)
        self.assertEqual((out["pdh"], out["pdl"], out["pdc"]), (110.0, 90.0, 105.0))

    def test_single_bar_has_no_previous_day(self) -> None:
        self.assertIsNone(levels.previous_day([bar("d1", 1, 2, 0.5, 1.5)]))


class RoundLevelsTest(unittest.TestCase):
    def test_brackets_spot_at_each_step(self) -> None:
        out = levels.round_levels(24_037, "NIFTY")
        self.assertEqual(out[100]["below"], 24_000.0)
        self.assertEqual(out[100]["above"], 24_100.0)
        self.assertEqual(out[500]["below"], 24_000.0)
        self.assertEqual(out[500]["above"], 24_500.0)

    def test_nearest_picks_the_closer_side(self) -> None:
        self.assertEqual(levels.round_levels(24_037, "NIFTY")[100]["nearest"], 24_000.0)
        self.assertEqual(levels.round_levels(24_078, "NIFTY")[100]["nearest"], 24_100.0)

    def test_banknifty_uses_wider_steps(self) -> None:
        """Bank Nifty strikes are 100 apart, Nifty's are 50 — the levels that
        matter differ because the option chain differs."""
        self.assertIn(1000, levels.round_levels(56_700, "BANKNIFTY"))
        self.assertNotIn(1000, levels.round_levels(24_000, "NIFTY"))

    def test_zero_spot_is_empty(self) -> None:
        self.assertEqual(levels.round_levels(0), {})


class PeriodExtremesTest(unittest.TestCase):
    def test_uses_only_the_window(self) -> None:
        bars = [bar("old", 1, 999, 1, 1)] + flat_series(5, 100)
        out = levels.period_extremes(bars, 5)
        self.assertEqual(out["high"], 101.0)      # the 999 is outside the window

    def test_empty_is_none(self) -> None:
        self.assertIsNone(levels.period_extremes([], 5))


class GapTest(unittest.TestCase):
    def test_an_unfilled_up_gap_is_reported(self) -> None:
        bars = [bar("d1", 100, 101, 99, 100),
                bar("d2", 110, 112, 109, 111),      # gap: 101 -> 109
                bar("d3", 111, 113, 110, 112)]
        gaps = levels.unfilled_gaps(bars)
        self.assertEqual(len(gaps), 1)
        self.assertEqual((gaps[0]["side"], gaps[0]["low"], gaps[0]["high"]), ("up", 101.0, 109.0))

    def test_a_filled_gap_is_not_reported(self) -> None:
        """The later bar trades back through the span, so it is closed."""
        bars = [bar("d1", 100, 101, 99, 100),
                bar("d2", 110, 112, 109, 111),
                bar("d3", 111, 113, 100, 102)]      # reaches back down to 100
        self.assertEqual(levels.unfilled_gaps(bars), [])

    def test_a_down_gap_is_detected(self) -> None:
        bars = [bar("d1", 100, 101, 99, 100),
                bar("d2", 90, 91, 89, 90),
                bar("d3", 90, 92, 89, 91)]
        gaps = levels.unfilled_gaps(bars)
        self.assertEqual(gaps[0]["side"], "down")

    def test_tiny_gaps_are_ignored(self) -> None:
        """A few ticks is rounding, not a level."""
        bars = [bar("d1", 100, 100.05, 99, 100),
                bar("d2", 100.06, 100.2, 100.06, 100.1),
                bar("d3", 100.1, 100.3, 100.07, 100.2)]
        self.assertEqual(levels.unfilled_gaps(bars, min_pct=0.15), [])

    def test_no_gap_in_continuous_trade(self) -> None:
        self.assertEqual(levels.unfilled_gaps(flat_series(10)), [])


class SwingTest(unittest.TestCase):
    def test_finds_a_confirmed_peak(self) -> None:
        highs = [10, 11, 15, 11, 10, 9, 8]
        bars = [bar(f"d{i}", h, h, h - 2, h) for i, h in enumerate(highs)]
        self.assertIn(15.0, levels.swing_levels(bars, span=2)["highs"])

    def test_the_newest_bars_cannot_be_swings(self) -> None:
        """A high is only a high once price has failed to exceed it — the lag is
        the definition, not a defect."""
        highs = [10, 11, 12, 13, 99]
        bars = [bar(f"d{i}", h, h, h - 2, h) for i, h in enumerate(highs)]
        self.assertNotIn(99.0, levels.swing_levels(bars, span=2)["highs"])

    def test_short_series_is_empty(self) -> None:
        self.assertEqual(levels.swing_levels(flat_series(3))["highs"], [])


class NearestLevelTest(unittest.TestCase):
    def test_returns_the_closest_within_range(self) -> None:
        out = levels.nearest_level(24_010, [("PDH", 24_000), ("PDL", 23_500)])
        self.assertEqual(out["name"], "PDH")
        self.assertEqual(out["side"], "below")

    def test_far_levels_are_not_reported(self) -> None:
        """A wick two percent away is not a reaction to the level."""
        self.assertIsNone(levels.nearest_level(24_000, [("PDH", 20_000)], within_pct=1.0))

    def test_side_is_correct_above_spot(self) -> None:
        out = levels.nearest_level(23_990, [("PDH", 24_000)])
        self.assertEqual(out["side"], "above")

    def test_no_levels_is_none(self) -> None:
        self.assertIsNone(levels.nearest_level(100, []))


class SummariseTest(unittest.TestCase):
    def test_returns_every_group(self) -> None:
        out = levels.summarise(flat_series(40), spot=100, symbol="NIFTY")
        for key in ("previous_day", "weekly", "monthly", "swings", "rounds",
                    "gaps", "at_level"):
            self.assertIn(key, out)

    def test_at_level_identifies_proximity(self) -> None:
        bars = flat_series(40, 100)
        out = levels.summarise(bars, spot=100.5, symbol="NIFTY")
        self.assertIsNotNone(out["at_level"])

    def test_survives_junk_bars(self) -> None:
        junk = [bar("d1", None, "x", None, ""), bar("d2", 1, 2, 0, 1)]
        levels.summarise(junk, spot=1)      # must not raise


if __name__ == "__main__":
    unittest.main()
