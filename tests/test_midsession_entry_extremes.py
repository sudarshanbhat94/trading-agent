"""A position must never be exited — or locked — off a price it never traded at.

A trade opened mid-session inherits a day high and a day low that were set
BEFORE it existed. Using either is a phantom: it books an exit against a price
the position never saw. `use_live` was written for exactly this and covered the
equity intraday lanes plus manual/btst, but it missed `index_options`, which
also always enters mid-session, and it was never applied to `peak` at all.

The cost, on 2026-07-30, in one contract:

  NIFTY 24300 CE bought at 10:52 for 87.75. Day high 139.45, day low 67.05.
  The 3-ATR breakeven lock armed off the HIGH, lifting the stop from 57.04 to
  breakeven; the stop then fired against the LOW. Booked -0.40%. The contract
  closed at 106.75 — up 21.7%, or +Rs 1,210 on 65 lots.

Both halves are tested here, because fixing only the exit test would leave the
lock still arming off an extreme the trade never reached.
"""

from __future__ import annotations

import unittest
from datetime import date

from app import v2_live


TODAY = date(2026, 7, 30)
TODAY_S = "2026-07-30"


def _pos(strategy, entry, stop, edate=TODAY_S, peak=None, target=0.0, trail=0.0):
    return dict(id=1, strategy=strategy, entry=entry, shares=65.0, stop=stop,
                target=target, trail=trail, peak=entry if peak is None else peak,
                edate=edate)


def _quote(price, high, low):
    return {"price": price, "high": high, "low": low}


def _ev(p, lq, sess_row=None, today_s=TODAY_S, now_hhmm="11:00"):
    return v2_live.evaluate_exit(p, lq, sess_row, TODAY, today_s, "IN", now_hhmm)


class IndexOptionEntryDayTest(unittest.TestCase):
    """The exact position, prices and outcome from 30 Jul."""

    def ce(self, **kw):
        # stop_pct 0.35 -> 87.75 * 0.65
        return _pos("index_options", 87.75, 57.0375, **kw)

    def test_the_day_low_does_not_close_the_position(self) -> None:
        """67.05 was below the entry, but the contract was at 106.75 when this
        ran. Exiting on it books a loss the trade never took."""
        peak, eff, ex, reason = _ev(self.ce(), _quote(106.75, 139.45, 67.05))
        self.assertIsNone(ex)
        self.assertIsNone(reason)

    def test_the_day_high_does_not_arm_the_breakeven_lock(self) -> None:
        """3-ATR arms at 87.75 + 3*(87.75-57.0375)/2 = 133.82. The day high of
        139.45 clears it; the price we actually saw, 106.75, does not."""
        peak, eff, ex, reason = _ev(self.ce(), _quote(106.75, 139.45, 67.05))
        self.assertEqual(peak, 106.75)
        self.assertAlmostEqual(eff, 57.0375, places=4)
        self.assertLess(eff, 87.75, "stop was lifted to breakeven off a high we never held")

    def test_a_genuine_move_through_the_stop_still_exits(self) -> None:
        """The guard must not make the position unclosable — a LIVE price below
        the stop still books, at the live price."""
        peak, eff, ex, reason = _ev(self.ce(), _quote(50.0, 139.45, 50.0))
        self.assertEqual(reason, "stop")
        self.assertEqual(ex, 50.0)

    def test_peak_is_read_from_prices_we_actually_held(self) -> None:
        """peak is persisted from prices seen SINCE entry, which is what this
        test is named for and still guards.

        It used to assert the ATR breakeven lock armed off that peak. The lock
        is now disabled — measured net-negative, identical worst trade with and
        without it — so a run-up no longer ratchets the stop, and the position
        is governed by the stop it opened with. The peak itself is unchanged.
        """
        p = self.ce(peak=140.0)                 # ratcheted while we held it
        peak, eff, ex, reason = _ev(p, _quote(80.0, 139.45, 67.05))
        self.assertEqual(peak, 140.0)
        self.assertLess(eff, v2_live.breakeven_price("IN", 112.90))
        self.assertIsNone(reason, "no lock, so an 80.0 print is above the stop")

    def test_the_day_extremes_are_valid_again_the_next_day(self) -> None:
        """Held overnight, the position was open for the whole session, so the
        day low is a price it genuinely traded through."""
        p = self.ce(edate="2026-07-29")
        peak, eff, ex, reason = _ev(p, _quote(106.75, 139.45, 50.0))
        self.assertEqual(reason, "stop")


class MidSessionEquityEntryTest(unittest.TestCase):
    """The same rule, on the lane that took six of yesterday's seven trades."""

    def test_a_pre_entry_high_does_not_arm_the_intraday_lock(self) -> None:
        """volume_surge locks to breakeven once up 1.5%. A stock that ran
        before we bought it would arm the lock on the first poll and exit the
        trade flat, having never been up at all."""
        p = _pos("volume_surge", 100.0, 97.5)
        peak, eff, ex, reason = _ev(p, _quote(100.2, 108.0, 99.0))
        self.assertEqual(peak, 100.2)
        self.assertEqual(eff, 97.5, "lock armed off a high that predates the entry")
        self.assertIsNone(ex)

    def test_the_lock_still_arms_once_we_are_genuinely_up(self) -> None:
        """Unchanged behaviour: peak carries what we saw while holding."""
        p = _pos("volume_surge", 100.0, 97.5, peak=101.6)
        peak, eff, ex, reason = _ev(p, _quote(100.0, 108.0, 99.0))
        self.assertAlmostEqual(
            eff, v2_live.breakeven_price("IN", 100.0, 65, "volume_surge"), places=4)
        self.assertEqual(reason, "stop")

    def test_a_session_high_from_before_entry_is_also_excluded(self) -> None:
        """sess_row[1] accumulates from the session start, so it carries the
        same pre-entry prices the day high does."""
        p = _pos("volume_surge", 100.0, 97.5)
        peak, eff, ex, reason = _ev(p, _quote(100.2, 100.2, 99.0), sess_row=[99.0, 108.0, 98.0])
        self.assertEqual(peak, 100.2)
        self.assertEqual(eff, 97.5)


class OpeningEntryUnaffectedTest(unittest.TestCase):
    """swing/momentum enter at the OPEN, so every extreme of the day is theirs.
    None of this may change for them."""

    def test_the_day_low_still_stops_a_swing_position(self) -> None:
        p = _pos("swing_meanrev", 100.0, 95.0, edate="2026-07-20")
        peak, eff, ex, reason = _ev(p, _quote(101.0, 102.0, 94.0))
        self.assertEqual(reason, "stop")
        self.assertEqual(ex, 95.0)

    def test_the_day_high_still_feeds_peak_for_a_swing_position(self) -> None:
        p = _pos("swing_meanrev", 100.0, 95.0, edate="2026-07-20")
        peak, eff, ex, reason = _ev(p, _quote(101.0, 108.0, 99.0))
        self.assertEqual(peak, 108.0)

    def test_an_opening_entry_uses_day_extremes_on_its_own_entry_day(self) -> None:
        """The entry-day case specifically: a swing position opened at today's
        open owns today's low, so the guard must not extend to it."""
        p = _pos("swing_meanrev", 100.0, 95.0, edate=TODAY_S)
        peak, eff, ex, reason = _ev(p, _quote(101.0, 102.0, 94.0))
        self.assertEqual(reason, "stop")


class GuardMembershipTest(unittest.TestCase):
    def test_every_mid_session_lane_is_covered(self) -> None:
        """index_options was the one missing, and it is the one that cost
        Rs 1,210 on 30 Jul."""
        for strat in ("intraday_news", "volume_surge", "intraday_momentum",
                      "index_options", "manual", "btst"):
            self.assertIn(strat, v2_live.MIDSESSION_STRATS, strat)

    def test_opening_lanes_are_not_covered(self) -> None:
        for strat in ("swing_meanrev", "mom_breakout", "gap_momentum"):
            self.assertNotIn(strat, v2_live.MIDSESSION_STRATS, strat)


if __name__ == "__main__":
    unittest.main()
