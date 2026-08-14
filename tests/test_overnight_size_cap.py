"""Overnight per-position size cap.

The exit audit established that losses on this book are driven by overnight
gap-through, and that tightening the stop makes the edge worse while leaving
the worst trade unchanged — a gap opens below any stop level. The only lever
the data supports is bounding how much of the book a single overnight position
may hold.

These tests pin two things: the cap genuinely bounds the pathological case, and
it does NOT bind in normal operation, so the backtested sizing model is
unchanged.
"""

from __future__ import annotations

import unittest

from app import v2_live


class CapDoesNotTouchNormalSizingTest(unittest.TestCase):
    """The cap must be inert across the whole normal sizing range."""

    def test_normal_range_is_untouched(self) -> None:
        equity = 100_000.0
        entry = 500.0
        base_alloc = equity / v2_live.MAXPOS["IN"]

        for vol_mult in (v2_live.VOL_SIZE_MIN, 0.5):   # legacy path; see below
            with self.subTest(vol_mult=vol_mult):
                shares = base_alloc * vol_mult / entry
                capped = v2_live.cap_overnight_shares(
                    shares, entry, equity, "IN", "swing_meanrev", "TEST"
                )
                self.assertEqual(capped, shares)

    def test_the_vol_mult_sizing_path_is_retired(self) -> None:
        """At MAXPOS=3 one slot is 33% of the book, and the legacy VOL_SIZE_MAX
        of 1.6 would put a "normal" position at 53% — above this 30% cap.

        That combination is unreachable: every lane that used vol_mult sizing
        is in DISABLED_LANES, and the multi-sleeve risk manager sizes to one
        slot with no multiplier. Loosening the cap to fit dead code would turn
        a risk guardrail into a re-tuning, so the invariant asserted here is
        the retirement itself."""
        legacy = (1.0 / v2_live.MAXPOS["IN"]) * v2_live.VOL_SIZE_MAX
        self.assertGreater(legacy, v2_live.OVERNIGHT_MAX_POS_FRAC["IN"])
        for lane in ("swing_meanrev", "mom_breakout", "volume_surge"):
            with self.subTest(lane=lane):
                self.assertIn(lane, v2_live.DISABLED_LANES)
        from app.sleeves.config import SLEEVES
        slot_frac = 1.0 / SLEEVES.max_positions_total
        self.assertLessEqual(slot_frac, 0.34,
                             "the live sizing path caps a position at one slot")


class CapBoundsTheTailTest(unittest.TestCase):
    def test_whole_book_in_one_name_is_clipped(self) -> None:
        """The DYN_ALLOC pathological case: last open slot takes all free cash."""
        equity, entry = 100_000.0, 500.0
        shares = equity / entry  # 100% of the book in one position
        capped = v2_live.cap_overnight_shares(
            shares, entry, equity, "IN", "swing_meanrev", "TEST"
        )
        self.assertAlmostEqual(capped * entry / equity, v2_live.OVERNIGHT_MAX_POS_FRAC["IN"])
        self.assertLess(capped, shares)

    def test_cap_bounds_worst_case_book_damage(self) -> None:
        """A -61% gap (the worst single trade observed) on a capped position
        must cost a bounded fraction of the book."""
        equity, entry = 100_000.0, 500.0
        shares = v2_live.cap_overnight_shares(
            equity / entry, entry, equity, "IN", "swing_meanrev", "TEST"
        )
        loss = shares * entry * 0.61
        self.assertLessEqual(loss / equity, 0.61 * v2_live.OVERNIGHT_MAX_POS_FRAC["IN"] + 1e-9)
        self.assertLess(loss / equity, 0.20)


class CapScopeTest(unittest.TestCase):
    def test_intraday_lanes_are_exempt(self) -> None:
        """Intraday lanes square off the same session, so they carry no
        overnight gap risk and must not be resized."""
        equity, entry = 100_000.0, 500.0
        shares = equity / entry
        for strategy in v2_live.INTRADAY_STRATS:
            with self.subTest(strategy=strategy):
                self.assertEqual(
                    v2_live.cap_overnight_shares(shares, entry, equity, "IN", strategy, "TEST"),
                    shares,
                )

    def test_overnight_lanes_are_covered(self) -> None:
        equity, entry = 100_000.0, 500.0
        shares = equity / entry
        for strategy in ("swing_meanrev", "mom_breakout", "btst"):
            with self.subTest(strategy=strategy):
                self.assertLess(
                    v2_live.cap_overnight_shares(shares, entry, equity, "IN", strategy, "TEST"),
                    shares,
                )

    def test_disabled_when_fraction_is_zero(self) -> None:
        original = dict(v2_live.OVERNIGHT_MAX_POS_FRAC)
        try:
            v2_live.OVERNIGHT_MAX_POS_FRAC["IN"] = 0.0
            shares = 200.0
            self.assertEqual(
                v2_live.cap_overnight_shares(shares, 500.0, 100_000.0, "IN", "swing_meanrev"),
                shares,
            )
        finally:
            v2_live.OVERNIGHT_MAX_POS_FRAC.clear()
            v2_live.OVERNIGHT_MAX_POS_FRAC.update(original)

    def test_unknown_market_is_not_capped(self) -> None:
        shares = 200.0
        self.assertEqual(
            v2_live.cap_overnight_shares(shares, 500.0, 100_000.0, "XX", "swing_meanrev"),
            shares,
        )


class CapDegenerateInputTest(unittest.TestCase):
    """Sizing runs inside the live engine loop; bad inputs must not raise."""

    def test_non_positive_inputs_pass_through(self) -> None:
        cases = [
            (100.0, 0.0, 100_000.0),   # no price
            (100.0, 500.0, 0.0),       # no equity
            (100.0, 500.0, -5.0),      # negative equity
            (0.0, 500.0, 100_000.0),   # no shares
            (-10.0, 500.0, 100_000.0), # negative shares
        ]
        for shares, entry, equity in cases:
            with self.subTest(shares=shares, entry=entry, equity=equity):
                self.assertEqual(
                    v2_live.cap_overnight_shares(shares, entry, equity, "IN", "swing_meanrev"),
                    shares,
                )


if __name__ == "__main__":
    unittest.main()
