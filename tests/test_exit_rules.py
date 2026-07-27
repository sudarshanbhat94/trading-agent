"""Exit rules: when an open position is closed, and at what price.

This is the logic that turns an open position into a booked P&L, so a defect
here costs real book value. It had no coverage. Every expected value below is
hand-computed from the rule being tested.

The precedence is the part most worth pinning: stop is evaluated BEFORE the
BTST next-open exit, so a bad overnight down-gap books as a stop rather than as
a gap capture. And a poll-based stop fills at the gapped price, not the stop
level — the displayed stop is honest but is not a resting order.
"""

from __future__ import annotations

import unittest
from datetime import date

from app import v2_live


def _pos(strategy="swing_meanrev", entry=100.0, stop=95.0, target=0.0,
         trail=0.0, peak=100.0, edate="2026-07-20", shares=10.0, pid=1):
    return dict(id=pid, strategy=strategy, entry=entry, shares=shares, stop=stop,
                target=target, trail=trail, peak=peak, edate=edate)


def _quote(price, high=None, low=None):
    return {"price": price, "high": high if high is not None else price,
            "low": low if low is not None else price}


TODAY = date(2026, 7, 27)
TODAY_S = "2026-07-27"


def _evaluate(p, lq, sess_row=None, today=TODAY, today_s=TODAY_S, market="IN", now_hhmm="11:00"):
    return v2_live.evaluate_exit(p, lq, sess_row, today, today_s, market, now_hhmm)


class StopTest(unittest.TestCase):
    def test_price_through_the_stop_exits(self) -> None:
        _, _, ex, reason = _evaluate(_pos(stop=95.0), _quote(94.0))
        self.assertEqual(reason, "stop")
        self.assertEqual(ex, 94.0)      # min(eff, price) — fills at the worse price

    def test_day_low_through_the_stop_exits_at_the_stop(self) -> None:
        """A swing entered at the open: the day's low is a legitimate reference,
        so a wick through the stop fills at the stop, not the current price."""
        _, _, ex, reason = _evaluate(_pos(stop=95.0), _quote(97.0, high=98.0, low=94.0))
        self.assertEqual(reason, "stop")
        self.assertEqual(ex, 95.0)

    def test_gap_below_the_stop_fills_at_the_gapped_price(self) -> None:
        """The documented limitation: a poll-based stop cannot defend a gap. It
        fills where the market actually is, exactly as a broker stop would."""
        _, _, ex, reason = _evaluate(_pos(stop=95.0), _quote(80.0))
        self.assertEqual(reason, "stop")
        self.assertEqual(ex, 80.0)
        self.assertLess(ex, 95.0)

    def test_untouched_stop_holds(self) -> None:
        _, _, ex, reason = _evaluate(_pos(stop=95.0), _quote(101.0, high=102.0, low=99.0))
        self.assertIsNone(ex)
        self.assertIsNone(reason)


class TrailTest(unittest.TestCase):
    def test_trailing_stop_ratchets_with_the_peak(self) -> None:
        # peak 120, 10% trail -> effective stop 108
        _, eff, ex, reason = _evaluate(
            _pos(trail=0.10, peak=120.0, stop=95.0), _quote(107.0, high=107.0, low=107.0)
        )
        self.assertAlmostEqual(eff, 108.0)
        self.assertEqual(reason, "trail")
        self.assertEqual(ex, 107.0)

    def test_trail_reported_as_stop_when_it_has_not_risen(self) -> None:
        """If the trail is still below the original stop, the exit is a plain
        stop — the reason label tracks which level actually bound."""
        _, _, _, reason = _evaluate(
            _pos(trail=0.10, peak=100.0, stop=95.0), _quote(90.0)
        )
        self.assertEqual(reason, "stop")

    def test_peak_ratchets_from_the_session_high(self) -> None:
        peak, _, _, _ = _evaluate(_pos(peak=100.0), _quote(105.0, high=106.0),
                                  sess_row=("X", 112.0))
        self.assertEqual(peak, 112.0)

    def test_peak_never_decreases(self) -> None:
        peak, _, _, _ = _evaluate(_pos(peak=130.0), _quote(105.0, high=106.0))
        self.assertEqual(peak, 130.0)


class BreakevenLockTest(unittest.TestCase):
    def test_big_winner_locks_the_stop_at_entry(self) -> None:
        """swing atr_stop is 2.0, so entry 100 / stop 90 implies ATR 5.
        BE_TRIGGER_ATR is 3, so a peak at or above 115 arms breakeven."""
        self.assertEqual(v2_live.PLAN["swing_meanrev"]["atr_stop"], 2.0)
        self.assertEqual(v2_live.BE_TRIGGER_ATR, 3.0)
        _, eff, _, _ = _evaluate(_pos(entry=100.0, stop=90.0, peak=115.0), _quote(112.0))
        self.assertEqual(eff, 100.0)

    def test_below_the_trigger_the_stop_is_untouched(self) -> None:
        _, eff, _, _ = _evaluate(_pos(entry=100.0, stop=90.0, peak=114.0), _quote(112.0))
        self.assertEqual(eff, 90.0)

    def test_intraday_lock_arms_just_above_entry(self) -> None:
        lock = v2_live.INTRA["lock"]
        _, eff, _, _ = _evaluate(
            _pos(strategy="volume_surge", entry=100.0, stop=97.0,
                 peak=100.0 * (1 + lock), edate=TODAY_S),
            _quote(100.5),
        )
        self.assertAlmostEqual(eff, 100.1)     # entry * 1.001, never allowed to go red


class TargetTest(unittest.TestCase):
    def test_target_hit_exits(self) -> None:
        _, _, ex, reason = _evaluate(_pos(target=110.0), _quote(111.0))
        self.assertEqual(reason, "target")
        self.assertEqual(ex, 111.0)            # max(target, price)

    def test_day_high_through_the_target_fills_at_the_target(self) -> None:
        _, _, ex, reason = _evaluate(_pos(target=110.0), _quote(108.0, high=112.0, low=107.0))
        self.assertEqual(reason, "target")
        self.assertEqual(ex, 110.0)

    def test_zero_target_never_triggers(self) -> None:
        _, _, ex, reason = _evaluate(_pos(target=0.0), _quote(500.0, high=500.0, low=499.0))
        self.assertIsNone(reason)
        self.assertIsNone(ex)

    def test_stop_takes_precedence_over_target(self) -> None:
        """Both breached in one bar: the stop wins, which is the conservative
        assumption when intrabar order is unknown."""
        _, _, ex, reason = _evaluate(
            _pos(stop=95.0, target=110.0), _quote(97.0, high=115.0, low=94.0)
        )
        self.assertEqual(reason, "stop")


class BtstExitTest(unittest.TestCase):
    def test_sells_the_morning_after_entry(self) -> None:
        _, _, ex, reason = _evaluate(
            _pos(strategy="btst", entry=950.4, stop=931.39, edate="2026-07-27"),
            _quote(985.0), today=date(2026, 7, 28), today_s="2026-07-28",
        )
        self.assertEqual(reason, "btst")
        self.assertEqual(ex, 985.0)

    def test_holds_on_its_entry_day(self) -> None:
        """Seeded near the close; it must not sell the same session."""
        _, _, ex, reason = _evaluate(
            _pos(strategy="btst", entry=950.4, stop=931.39, edate=TODAY_S), _quote(955.0)
        )
        self.assertIsNone(reason)

    def test_bad_down_gap_books_as_a_stop_not_a_btst_exit(self) -> None:
        """Precedence check. The overnight gap is the lane's edge, but a gap
        THROUGH the stop must be recorded as a stop loss."""
        _, _, ex, reason = _evaluate(
            _pos(strategy="btst", entry=950.4, stop=931.39, edate="2026-07-27"),
            _quote(900.0), today=date(2026, 7, 28), today_s="2026-07-28",
        )
        self.assertEqual(reason, "stop")
        self.assertEqual(ex, 900.0)

    def test_entry_day_uses_the_live_price_not_the_day_low(self) -> None:
        """Entered near the close, so the session's earlier low predates the
        entry and must not trigger a stop the trade never touched."""
        _, _, ex, reason = _evaluate(
            _pos(strategy="btst", entry=950.4, stop=931.39, edate=TODAY_S),
            _quote(955.0, high=960.0, low=900.0),
        )
        self.assertIsNone(reason)


class IntradaySquareOffTest(unittest.TestCase):
    def test_squares_off_at_the_cutoff(self) -> None:
        cutoff = v2_live.INTRA["squareoff"]
        _, _, ex, reason = _evaluate(
            _pos(strategy="volume_surge", entry=100.0, stop=97.0, edate=TODAY_S),
            _quote(101.0), now_hhmm=cutoff,
        )
        self.assertEqual(reason, "eod")
        self.assertEqual(ex, 101.0)

    def test_holds_before_the_cutoff(self) -> None:
        _, _, ex, reason = _evaluate(
            _pos(strategy="volume_surge", entry=100.0, stop=97.0, edate=TODAY_S),
            _quote(101.0), now_hhmm="10:00",
        )
        self.assertIsNone(reason)

    def test_swing_is_not_squared_off(self) -> None:
        _, _, ex, reason = _evaluate(
            _pos(strategy="swing_meanrev", entry=100.0, stop=95.0, edate="2026-07-24"),
            _quote(101.0), now_hhmm="15:59",
        )
        self.assertIsNone(reason)


class TimeExitTest(unittest.TestCase):
    def test_exits_after_the_configured_hold(self) -> None:
        hold = v2_live.HOLD_DAYS.get("swing_meanrev", 10)
        # Walk back `hold` trading sessions from TODAY.
        entry = "2026-07-20" if hold <= 5 else "2026-07-06"
        held = v2_live.trading_days_held(entry, TODAY, "IN")
        if held < hold:
            self.skipTest(f"fixture spans {held} sessions, hold is {hold}")
        _, _, ex, reason = _evaluate(_pos(edate=entry), _quote(101.0, high=102.0, low=100.0))
        self.assertEqual(reason, "time")

    def test_holds_before_the_limit(self) -> None:
        _, _, ex, reason = _evaluate(
            _pos(edate="2026-07-24"), _quote(101.0, high=102.0, low=100.0)
        )
        self.assertIsNone(reason)


class ExtractionFidelityTest(unittest.TestCase):
    """The extraction must be a move, not a rewrite."""

    def test_exit_monitor_delegates(self) -> None:
        import inspect
        src = inspect.getsource(v2_live.exit_monitor)
        self.assertIn("evaluate_exit(", src)
        for leaked in ("BE_TRIGGER_ATR", "eff_tgt", "use_live", "lo_ref"):
            self.assertNotIn(leaked, src, f"{leaked} still duplicated in exit_monitor")

    def test_held_position_returns_no_reason(self) -> None:
        for strategy in ("swing_meanrev", "mom_breakout", "btst", "volume_surge"):
            with self.subTest(strategy=strategy):
                _, _, ex, reason = _evaluate(
                    _pos(strategy=strategy, entry=100.0, stop=95.0, edate=TODAY_S),
                    _quote(101.0), now_hhmm="10:00",
                )
                self.assertIsNone(ex)
                self.assertIsNone(reason)


if __name__ == "__main__":
    unittest.main()
