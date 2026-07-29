"""Don't buy a dip that is still falling.

On 2026-07-29 swing_meanrev's highest-conviction signals were THANGAMAYL
(-10.75%, pinned at the very low of its day), J&KBANK (-6.55%) and PNGJL (4%
off its low). Only the market-regime gate was keeping them out of the book, and
the operator had asked for that gate to be removed an hour earlier.

The distinction that matters for mean reversion is not how far a name has
dropped but whether it has STOPPED dropping. A stock down 10% that has bounced
into the middle of its range is the setup this lane wants; the same stock at
the low is a knife. Range position separates them; percentage change cannot —
which is the whole reason this guard is written the way it is.
"""

from __future__ import annotations

import inspect
import unittest

from app import v2_live


def quote(price, high, low):
    return {"price": price, "high": high, "low": low}


class DayRangePositionTest(unittest.TestCase):
    def test_at_the_low_is_zero(self) -> None:
        self.assertEqual(v2_live.day_range_position(quote(100, 110, 100)), 0.0)

    def test_at_the_high_is_one(self) -> None:
        self.assertEqual(v2_live.day_range_position(quote(110, 110, 100)), 1.0)

    def test_midpoint_is_a_half(self) -> None:
        self.assertAlmostEqual(v2_live.day_range_position(quote(105, 110, 100)), 0.5)

    def test_zero_width_range_is_unknown(self) -> None:
        self.assertIsNone(v2_live.day_range_position(quote(100, 100, 100)))

    def test_missing_quote_is_unknown(self) -> None:
        self.assertIsNone(v2_live.day_range_position(None))
        self.assertIsNone(v2_live.day_range_position({}))

    def test_a_price_outside_its_own_range_is_unknown(self) -> None:
        """A stale or broken quote, not a real reading — must not be scored."""
        self.assertIsNone(v2_live.day_range_position(quote(120, 110, 100)))
        self.assertIsNone(v2_live.day_range_position(quote(90, 110, 100)))

    def test_non_numeric_values_are_unknown(self) -> None:
        self.assertIsNone(v2_live.day_range_position({"price": "x", "high": 1, "low": 0}))


class KnifeGuardTest(unittest.TestCase):
    def test_a_stock_pinned_at_its_low_is_rejected(self) -> None:
        """THANGAMAYL: -10.75%, sitting at the day low."""
        self.assertFalse(v2_live.clear_of_the_day_low(quote(6452, 7250, 6450)))

    def test_a_stock_that_bounced_is_allowed(self) -> None:
        """Same drop, but it has stopped falling — this IS the mean-reversion
        setup, so the guard must not reject it."""
        self.assertTrue(v2_live.clear_of_the_day_low(quote(6800, 7250, 6450)))

    def test_the_boundary_is_inclusive(self) -> None:
        self.assertTrue(v2_live.clear_of_the_day_low(quote(102.5, 110, 100), floor=0.25))

    def test_just_below_the_boundary_is_rejected(self) -> None:
        self.assertFalse(v2_live.clear_of_the_day_low(quote(102.4, 110, 100), floor=0.25))

    def test_an_unknown_range_is_allowed_not_blocked(self) -> None:
        """A thin or missing quote must not silently shut the whole lane down —
        failing closed here would look identical to 'no signals today'."""
        self.assertTrue(v2_live.clear_of_the_day_low(None))
        self.assertTrue(v2_live.clear_of_the_day_low(quote(100, 100, 100)))

    def test_a_stock_at_its_high_passes(self) -> None:
        self.assertTrue(v2_live.clear_of_the_day_low(quote(110, 110, 100)))


class WiringTest(unittest.TestCase):
    def test_the_guard_is_applied_to_the_dip_buying_lane(self) -> None:
        src = inspect.getsource(v2_live.poll_market)
        self.assertIn("clear_of_the_day_low(", src)
        self.assertIn('s["strategy"] == "swing_meanrev"', src)

    def test_breakouts_are_not_gated_by_it(self) -> None:
        """mom_breakout buys names pressing their highs, where the test is
        trivially true — applying it there would only add a failure mode."""
        src = inspect.getsource(v2_live.poll_market)
        knife = src.index("clear_of_the_day_low(")
        line = src[src.rfind("\n", 0, knife):src.index("\n", knife)]
        self.assertNotIn("mom_breakout", line)

    def test_rejections_are_reported(self) -> None:
        """A silent filter is indistinguishable from a broken signal source."""
        src = inspect.getsource(v2_live.poll_market)
        self.assertIn("knives += 1", src)
        self.assertIn("still-falling", src)


if __name__ == "__main__":
    unittest.main()
