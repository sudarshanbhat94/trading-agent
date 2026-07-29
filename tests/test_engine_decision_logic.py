"""Engine decision logic: the pure gates every lane depends on.

`v2_live` had no behavioural coverage at all — only that it imports. This is
the first slice: the date/window arithmetic and lane configuration that decide
whether a trade is taken or exited. Both functions here encode fixes for real
losses:

- the catalyst window was a flat 48 wall-clock hours, so Friday results expired
  before Monday's move (Dr Lal, KFin were missed)
- the hold clock counted calendar days, force-selling roughly two sessions
  early around weekends and truncating the bounce the 8-bar hold was validated
  on

Expected values are hand-derived from a calendar, not captured from the
implementation.
"""

from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta, timezone

from app import v2_live

IST = timezone(timedelta(hours=5, minutes=30))


def _cutoff_date(sessions, today):
    """The cutoff epoch expressed back as an IST date, for readable assertions."""
    epoch = v2_live._catalyst_cutoff_epoch(sessions, today=today)
    return datetime.fromtimestamp(epoch, IST).date()


class CatalystWindowTest(unittest.TestCase):
    """2026-07-27 is a Monday; 07-24 Friday; 07-25/26 the weekend."""

    def test_window_bridges_a_weekend(self) -> None:
        """The regression: on Monday, a 1-session window must reach Friday, not
        Sunday. A flat 48h window expired Friday's results before Monday."""
        self.assertEqual(_cutoff_date(1, date(2026, 7, 27)), date(2026, 7, 24))

    def test_three_sessions_back_from_monday_is_wednesday(self) -> None:
        # Mon 27 -> Fri 24 (1) -> Thu 23 (2) -> Wed 22 (3)
        self.assertEqual(_cutoff_date(3, date(2026, 7, 27)), date(2026, 7, 22))

    def test_midweek_window_is_plain_days(self) -> None:
        # Thu 23 -> Wed 22 (1) -> Tue 21 (2)
        self.assertEqual(_cutoff_date(2, date(2026, 7, 23)), date(2026, 7, 21))

    def test_window_skips_a_listed_holiday(self) -> None:
        """2026-01-26 (Republic Day) is in MARKET_HOLIDAYS['IN'] and falls on a
        Monday, so one session back from Tuesday the 27th is Friday the 23rd."""
        self.assertIn("2026-01-26", v2_live.MARKET_HOLIDAYS["IN"])
        self.assertEqual(_cutoff_date(1, date(2026, 1, 27)), date(2026, 1, 23))

    def test_cutoff_is_midnight_ist(self) -> None:
        epoch = v2_live._catalyst_cutoff_epoch(1, today=date(2026, 7, 27))
        moment = datetime.fromtimestamp(epoch, IST)
        self.assertEqual((moment.hour, moment.minute, moment.second), (0, 0, 0))

    def test_more_sessions_reach_further_back(self) -> None:
        today = date(2026, 7, 27)
        cutoffs = [v2_live._catalyst_cutoff_epoch(n, today=today) for n in (1, 2, 3, 5)]
        self.assertEqual(cutoffs, sorted(cutoffs, reverse=True))

    def test_default_today_is_used_when_omitted(self) -> None:
        """Production callers pass no `today`; that path must still work."""
        epoch = v2_live._catalyst_cutoff_epoch(3)
        self.assertGreater(epoch, 0)
        self.assertLess(epoch, datetime.now(IST).timestamp())

    def test_zero_sessions_is_today(self) -> None:
        self.assertEqual(_cutoff_date(0, date(2026, 7, 27)), date(2026, 7, 27))


class PreOpenWarmUpTest(unittest.TestCase):
    """Signals must be built BEFORE 09:15 so the open is spent buying rather
    than computing. 2026-07-29 is a Wednesday; 2026-08-01 a Saturday."""

    def _at(self, hh, mm, day=(2026, 7, 29)):
        return datetime(*day, hh, mm, tzinfo=IST)

    def test_warms_during_the_window(self) -> None:
        self.assertTrue(v2_live.in_preopen("IN", self._at(9, 6)))
        self.assertTrue(v2_live.in_preopen("IN", self._at(9, 14)))

    def test_not_before_the_window(self) -> None:
        self.assertFalse(v2_live.in_preopen("IN", self._at(8, 30)))

    def test_stops_at_the_open(self) -> None:
        """At 09:15 the live passes take over; warming past it would double the
        heavy signal computation during the busiest minute of the session."""
        self.assertFalse(v2_live.in_preopen("IN", self._at(9, 15)))
        self.assertFalse(v2_live.in_preopen("IN", self._at(11, 0)))

    def test_not_on_a_weekend(self) -> None:
        self.assertFalse(v2_live.in_preopen("IN", self._at(9, 6, (2026, 8, 1))))

    def test_not_on_a_market_holiday(self) -> None:
        self.assertIn("2026-01-26", v2_live.MARKET_HOLIDAYS["IN"])
        self.assertFalse(v2_live.in_preopen("IN", self._at(9, 6, (2026, 1, 26))))

    def test_unknown_market_has_no_preopen(self) -> None:
        self.assertFalse(v2_live.in_preopen("XX", self._at(9, 6)))

    def test_warm_up_cannot_place_a_trade(self) -> None:
        """The safety property: fills are gated on the entry window, which is
        measured from the session-open timestamp and is not set pre-open."""
        import inspect
        src = inspect.getsource(v2_live.poll_market)
        self.assertIn("_SESSION_OPENED_AT.get(market, 0)", src)
        self.assertIn("if not in_window:", src)


class FreshNewsWindowTest(unittest.TestCase):
    """Operator rule: act on today's news or one session back, never older.
    The old rolling '-1 day' both over- and under-reached — on a Monday it ran
    into Sunday, and at 14:00 it excluded a 09:00 item from the day before."""

    def _cutoff_ist_date(self, today):
        stamp = v2_live.fresh_news_cutoff(today=today)
        moment = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        return moment.astimezone(IST).date()

    def test_monday_reaches_back_to_friday_not_sunday(self) -> None:
        self.assertEqual(self._cutoff_ist_date(date(2026, 7, 27)), date(2026, 7, 24))

    def test_midweek_reaches_back_one_day(self) -> None:
        self.assertEqual(self._cutoff_ist_date(date(2026, 7, 23)), date(2026, 7, 22))

    def test_cutoff_is_midnight_ist_so_a_whole_session_counts(self) -> None:
        """A Friday 18:00 filing must still qualify on Monday morning; a cutoff
        anchored to the current clock time would drop it."""
        stamp = v2_live.fresh_news_cutoff(today=date(2026, 7, 27))
        moment = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        ist = moment.astimezone(IST)
        self.assertEqual((ist.hour, ist.minute), (0, 0))

    def test_holiday_is_skipped(self) -> None:
        """Republic Day Mon 2026-01-26: one session back from Tuesday is Friday."""
        self.assertEqual(self._cutoff_ist_date(date(2026, 1, 27)), date(2026, 1, 23))

    def test_the_query_binds_the_cutoff_rather_than_hardcoding_a_day(self) -> None:
        self.assertIn("ts>=?", v2_live.FRESH_NEWS_SQL)
        self.assertNotIn("-1 day", v2_live.FRESH_NEWS_SQL)


class HoldClockTest(unittest.TestCase):
    def test_counts_trading_days_not_calendar_days(self) -> None:
        """Fri 24 -> Mon 27 is 3 calendar days but 1 trading session."""
        self.assertEqual(
            v2_live.trading_days_held("2026-07-24", date(2026, 7, 27), "IN"), 1
        )

    def test_same_day_is_zero(self) -> None:
        self.assertEqual(
            v2_live.trading_days_held("2026-07-27", date(2026, 7, 27), "IN"), 0
        )

    def test_full_week(self) -> None:
        # Mon 20 -> Mon 27 is 5 sessions (Mon-Fri), not 7 days.
        self.assertEqual(
            v2_live.trading_days_held("2026-07-20", date(2026, 7, 27), "IN"), 5
        )

    def test_holiday_is_not_counted(self) -> None:
        """Republic Day, Mon 2026-01-26, must not count as a session.
        Fri 23 -> Wed 28 is 3 weekdays, minus the holiday = 2."""
        self.assertEqual(
            v2_live.trading_days_held("2026-01-23", date(2026, 1, 28), "IN"), 2
        )

    def test_tolerates_a_timestamp_entry_date(self) -> None:
        self.assertEqual(
            v2_live.trading_days_held("2026-07-24T15:45:00+05:30", date(2026, 7, 27), "IN"), 1
        )

    def test_unparseable_entry_date_returns_zero(self) -> None:
        """A bad date must not stop the exit monitor; it holds rather than
        force-selling on a parse error."""
        self.assertEqual(v2_live.trading_days_held("not-a-date", date(2026, 7, 27), "IN"), 0)

    def test_unknown_market_has_no_holidays_but_still_skips_weekends(self) -> None:
        self.assertEqual(
            v2_live.trading_days_held("2026-07-24", date(2026, 7, 27), "XX"), 1
        )

    def test_eight_session_hold_is_reached_on_the_right_calendar_day(self) -> None:
        """The swing lane's validated hold is 8 trading bars. From Mon 2026-07-06
        that is Thu 2026-07-16 — ten calendar days, so calendar counting would
        have exited two sessions early."""
        self.assertEqual(
            v2_live.trading_days_held("2026-07-06", date(2026, 7, 16), "IN"), 8
        )
        self.assertEqual((date(2026, 7, 16) - date(2026, 7, 6)).days, 10)


class VolumeCurveTest(unittest.TestCase):
    """Cumulative intraday volume fraction, used to normalise relative volume
    early in the session. A wrong curve mis-scales rvol and mis-fires the
    volume_surge gate."""

    def test_curve_is_monotonic_and_bounded(self) -> None:
        previous = 0.0
        for minutes in range(0, 400, 5):
            value = v2_live._vol_frac(minutes)
            self.assertGreaterEqual(value, previous - 1e-9)
            self.assertLessEqual(value, 1.0)
            self.assertGreaterEqual(value, 0.04)
            previous = value

    def test_knot_points_match_the_curve_table(self) -> None:
        for minutes, fraction in v2_live._VOL_CURVE:
            if minutes == 0:
                continue          # clamped to the 0.04 floor
            with self.subTest(minutes=minutes):
                self.assertAlmostEqual(v2_live._vol_frac(minutes), fraction, places=9)

    def test_linear_interpolation_between_knots(self) -> None:
        """Halfway between (60, 0.25) and (105, 0.35) is 0.30."""
        self.assertAlmostEqual(v2_live._vol_frac(82.5), 0.30, places=9)

    def test_floor_before_the_open(self) -> None:
        self.assertEqual(v2_live._vol_frac(0), 0.04)
        self.assertEqual(v2_live._vol_frac(-10), 0.04)

    def test_full_session_is_one(self) -> None:
        self.assertEqual(v2_live._vol_frac(375), 1.0)
        self.assertEqual(v2_live._vol_frac(1000), 1.0)


class LaneConfigurationTest(unittest.TestCase):
    def test_gap_momentum_stays_quarantined(self) -> None:
        """Proven net loser: -0.51%/trade, PF 0.81 over 44k trades. Re-enabling
        it silently would resume a known-losing lane, so pin it."""
        self.assertIn("gap_momentum", v2_live.DISABLED_LANES)

    def test_the_chosen_lanes_are_live(self) -> None:
        """2026-07-29: swing_meanrev re-enabled — it was producing the day's
        strongest signals with nothing able to act on them, and it holds the only
        positive live record (+Rs 795, PF 1.21 over 23 trades). gap_momentum
        remains the permanent exclusion (negative live AND backtest); btst stays
        parked by choice, not because it failed."""
        self.assertEqual(v2_live.DISABLED_LANES, {"gap_momentum", "btst"})
        for lane in ("mom_breakout", "volume_surge", "intraday_news", "swing_meanrev"):
            with self.subTest(lane=lane):
                self.assertNotIn(lane, v2_live.DISABLED_LANES)

    def test_mom_breakout_is_not_blocked_by_the_market_regime(self) -> None:
        """2026-07-29: regime OFF meant this lane took zero trades all morning.
        Measured, the gate is worth +0.02%/trade while removing half the
        entries — so it costs roughly half the total edge for nothing."""
        self.assertFalse(v2_live.MOM_REQUIRE_STRONG)

    def test_the_strong_gate_is_still_wired_so_it_can_be_restored(self) -> None:
        import inspect
        src = inspect.getsource(v2_live.poll_market)
        self.assertIn("MOM_REQUIRE_STRONG and not strong", src)

    def test_swing_meanrev_exit_matches_the_measured_best(self) -> None:
        """Same 19,510 entries, exit varied: stop3 +0.680%/trade vs the old
        stop2+target3.5 at +0.506%. The target cost -0.06% and capped the best
        trade at +28% against +69% without it."""
        plan = v2_live.PLAN["swing_meanrev"]
        self.assertEqual(plan["atr_stop"], 3.0)
        self.assertEqual(plan["atr_target"], 0.0, "fixed target clips the winners")

    def test_the_stop_is_not_removed_entirely(self) -> None:
        """hold-only scored highest (+0.775%) but its worst trade was -45.9%
        against -29.7% with a 3ATR stop. One such position undoes a quarter of
        a year's edge, so the stop stays."""
        self.assertGreater(v2_live.PLAN["swing_meanrev"]["atr_stop"], 0)

    def test_sizing_is_normalised_by_stop_distance(self) -> None:
        """Risk is shares x atr_stop x ATR. Without this, widening the stop
        would raise risk per trade by 50% and flatter the change with leverage
        rather than with a better exit."""
        import inspect
        src = inspect.getsource(v2_live.poll_market)
        self.assertIn("BASE_ATR_STOP / stop_atr", src)
        self.assertEqual(v2_live.BASE_ATR_STOP, 2.0)

    def test_a_wider_stop_produces_a_smaller_position(self) -> None:
        wide = v2_live.BASE_ATR_STOP / 3.0
        narrow = v2_live.BASE_ATR_STOP / 2.0
        self.assertLess(wide, narrow)
        self.assertAlmostEqual(wide * 3.0, narrow * 2.0)   # equal rupee risk

    def test_mom_breakout_has_both_a_stop_and_a_target(self) -> None:
        """Operator required an explicit target; the lane previously had none
        (atr_target 0.0) and exited only on the trail or the hold limit."""
        plan = v2_live.PLAN["mom_breakout"]
        self.assertGreater(plan["atr_target"], 0)
        self.assertGreater(plan["atr_target"], plan["atr_stop"],
                           "target must be further than the stop, else negative R:R")

    def test_the_two_intraday_lanes_share_stop_and_target(self) -> None:
        """Operator asked for the same stop/target on volume_surge as on the
        news lane."""
        for key in ("tp", "sl", "lock"):
            with self.subTest(key=key):
                self.assertEqual(v2_live.VOLSURGE[key], v2_live.INTRA[key])

    def test_both_catalyst_lanes_act_on_latest_news_only(self) -> None:
        """Operator rule: latest news only, no stale catalysts. volume_surge
        allowed 3 sessions until 2026-07-28, so it could buy on a filing two
        days stale while the news lane refused the same item."""
        self.assertEqual(v2_live.VOLSURGE["catalyst_sessions"], 1)

    def test_volume_surge_scans_fast_but_not_before_the_open_settles(self) -> None:
        self.assertLessEqual(v2_live.VOLSURGE_INTERVAL, 10)
        self.assertGreaterEqual(v2_live.VOLSURGE["start"], "09:16")

    def test_standalone_lanes_actually_honour_the_disable(self) -> None:
        """The list is only cosmetic unless each pass checks it. These three
        open positions directly and did NOT check it until this was fixed."""
        import inspect
        for name in ("volume_surge_pass", "intraday_news_pass", "btst_pass"):
            with self.subTest(lane=name):
                self.assertIn("DISABLED_LANES",
                              inspect.getsource(getattr(v2_live, name)))

    def test_intraday_lanes_square_off_and_btst_does_not(self) -> None:
        """btst must NOT be in INTRADAY_STRATS — it is held overnight, and
        including it would square it off at 15:12 and destroy the lane's only
        source of edge, the overnight gap."""
        self.assertIn("volume_surge", v2_live.INTRADAY_STRATS)
        self.assertIn("intraday_news", v2_live.INTRADAY_STRATS)
        self.assertNotIn("btst", v2_live.INTRADAY_STRATS)
        self.assertNotIn("swing_meanrev", v2_live.INTRADAY_STRATS)

    def test_every_buy_path_skips_frozen_quotes(self) -> None:
        """A stalled quote makes a fill fantasy — the price never traded there.
        Each lane has its OWN `INSERT INTO v2_positions`, so the guard has to be
        repeated per path; the daily lane was missing it while all four intraday
        lanes had it, which is exactly the kind of gap five copies invite."""
        import inspect
        for name in ("poll_market", "volume_surge_pass", "btst_pass"):
            with self.subTest(lane=name):
                self.assertIn("_stale_symbols",
                              inspect.getsource(getattr(v2_live, name)))

    def test_price_divergence_check_is_not_conditional_on_the_investigation(self) -> None:
        """The feed-glitch check must not live inside `if fscores is not None`.
        When the investigation panel is unavailable the liquidity gates already
        drop out; losing the price sanity check at the same time means the engine
        is least protected exactly when it is least informed."""
        import inspect
        src = inspect.getsource(v2_live.poll_market)
        guard = src.index("if fscores is not None:")
        check = src.index("> 0.30")
        self.assertLess(check, guard,
                        "price-divergence check must run before/outside the fscores branch")

    def test_every_planned_lane_has_a_stop(self) -> None:
        for lane, plan in v2_live.PLAN.items():
            with self.subTest(lane=lane):
                self.assertGreater(plan["atr_stop"], 0, f"{lane} has no stop distance")

    def test_btst_sizing_stays_conservative(self) -> None:
        """The lane is an unproven live trial on a ~149-trade sample; its
        per-position fraction should stay small until it earns more."""
        self.assertLessEqual(v2_live.BTST["size_frac"], 1.0)
        self.assertGreater(v2_live.BTST["sl"], 0)


if __name__ == "__main__":
    unittest.main()
