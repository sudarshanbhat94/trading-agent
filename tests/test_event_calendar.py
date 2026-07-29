"""The Indian market event calendar.

Options are priced against events. Implied volatility is elevated INTO a known
event and collapses once it passes, so a directionally correct trade can still
lose to the IV crush — which makes knowing what is coming part of pricing.

The case that matters most is the stale one. When the published MPC list runs
out, `next_mpc` returns None, and None reads identically to "nothing
scheduled". That would let the engine buy premium straight into a policy
decision, so staleness is reported as its own flag.

2026 reference: Jan 1 is a Thursday, so Jan 29 is also a Thursday.
"""

from __future__ import annotations

import unittest
from datetime import date

from app import event_calendar as ec


class ExpiryTest(unittest.TestCase):
    def test_weekly_expiry_is_the_thursday_of_that_week(self) -> None:
        self.assertEqual(ec.weekly_expiry(date(2026, 7, 28)), date(2026, 7, 30))

    def test_thursday_itself_is_its_own_expiry(self) -> None:
        self.assertEqual(ec.weekly_expiry(date(2026, 7, 30)), date(2026, 7, 30))

    def test_a_holiday_expiry_rolls_back(self) -> None:
        """NSE settles the session BEFORE a holiday, never after."""
        out = ec.weekly_expiry(date(2026, 7, 28), holidays={"2026-07-30"})
        self.assertEqual(out, date(2026, 7, 29))

    def test_monthly_expiry_is_the_last_thursday(self) -> None:
        self.assertEqual(ec.monthly_expiry(2026, 7), date(2026, 7, 30))

    def test_december_monthly_expiry_does_not_overflow_the_year(self) -> None:
        """month + 1 breaks in December if handled naively."""
        self.assertEqual(ec.monthly_expiry(2026, 12).month, 12)

    def test_monthly_expiry_rolls_back_off_a_holiday(self) -> None:
        out = ec.monthly_expiry(2026, 7, holidays={"2026-07-30"})
        self.assertEqual(out, date(2026, 7, 29))


class DaysToExpiryTest(unittest.TestCase):
    def test_counts_forward_to_thursday(self) -> None:
        self.assertEqual(ec.days_to_expiry(date(2026, 7, 28)), 2)

    def test_expiry_day_is_zero(self) -> None:
        self.assertEqual(ec.days_to_expiry(date(2026, 7, 30)), 0)

    def test_friday_rolls_to_the_next_week(self) -> None:
        """After Thursday the near contract is gone; counting to a past expiry
        would report a negative hold budget."""
        self.assertGreater(ec.days_to_expiry(date(2026, 7, 31)), 0)


class ExpiryWeekTest(unittest.TestCase):
    def test_the_monthly_expiry_week_is_flagged(self) -> None:
        self.assertTrue(ec.is_expiry_week(date(2026, 7, 28)))

    def test_an_ordinary_week_is_not(self) -> None:
        self.assertFalse(ec.is_expiry_week(date(2026, 7, 8)))


class EventTest(unittest.TestCase):
    def test_budget_day(self) -> None:
        self.assertTrue(ec.is_budget_day(date(2026, 2, 1)))
        self.assertFalse(ec.is_budget_day(date(2026, 2, 2)))

    def test_results_season_windows(self) -> None:
        self.assertTrue(ec.is_results_season(date(2026, 7, 25)))
        self.assertFalse(ec.is_results_season(date(2026, 6, 15)))

    def test_next_mpc_is_the_soonest_upcoming(self) -> None:
        self.assertEqual(ec.next_mpc(date(2026, 7, 29)), date(2026, 8, 6))

    def test_an_mpc_date_is_its_own_next(self) -> None:
        self.assertEqual(ec.next_mpc(date(2026, 8, 6)), date(2026, 8, 6))

    def test_staleness_is_reported_rather_than_silently_empty(self) -> None:
        """The dangerous case: past the published list, next_mpc is None, which
        reads exactly like 'nothing scheduled'."""
        beyond = date(2027, 6, 1)
        self.assertIsNone(ec.next_mpc(beyond))
        self.assertTrue(ec.mpc_dates_stale(beyond))

    def test_not_stale_inside_the_published_range(self) -> None:
        self.assertFalse(ec.mpc_dates_stale(date(2026, 7, 29)))


class EventsSummaryTest(unittest.TestCase):
    def test_returns_every_field(self) -> None:
        out = ec.events(date(2026, 7, 28))
        for key in ("days_to_expiry", "weekly_expiry", "monthly_expiry",
                    "expiry_week", "is_expiry_day", "budget_day",
                    "results_season", "next_mpc", "days_to_mpc",
                    "mpc_calendar_stale", "event_risk"):
            self.assertIn(key, out)

    def test_event_risk_fires_on_budget_day(self) -> None:
        self.assertTrue(ec.events(date(2026, 2, 1))["event_risk"])

    def test_event_risk_fires_on_expiry_day(self) -> None:
        self.assertTrue(ec.events(date(2026, 7, 30))["event_risk"])

    def test_event_risk_fires_the_day_before_policy(self) -> None:
        """IV is already bid the day before; entering then pays for the crush."""
        self.assertTrue(ec.events(date(2026, 8, 5))["event_risk"])

    def test_a_quiet_day_carries_no_event_risk(self) -> None:
        self.assertFalse(ec.events(date(2026, 6, 16))["event_risk"])


if __name__ == "__main__":
    unittest.main()
