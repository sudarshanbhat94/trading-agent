"""The Indian market calendar: expiry, Budget, policy, results season.

Options are priced against events, not around them. Implied volatility is
elevated INTO a known event and collapses the moment it passes, so a call that
is directionally right can still lose on an IV crush. Knowing what is coming is
therefore part of pricing, not merely scheduling.

Most of this is deterministic and needs no feed:

  weekly expiry    Thursday, rolled back when it falls on a holiday
  monthly expiry   last Thursday of the month, same rollback
  Budget           1 February, the single largest volatility day of the year
  results season   the four clustered windows Indian companies report in

RBI MPC dates are NOT derivable — the schedule is published and moves. They are
listed explicitly and must be refreshed each year; a stale list is worse than
none, so `mpc_dates_stale()` says when the data has run out rather than
silently reporting "no event".
"""
from __future__ import annotations

from datetime import date, timedelta

# Published RBI Monetary Policy Committee decision dates. Refresh annually —
# see mpc_dates_stale().
MPC_DATES = {
    2026: [date(2026, 2, 6), date(2026, 4, 8), date(2026, 6, 5),
           date(2026, 8, 6), date(2026, 10, 1), date(2026, 12, 5)],
}

# Results cluster hard in India rather than spreading across the quarter.
RESULTS_WINDOWS = ((1, 10, 2, 15), (4, 10, 5, 31), (7, 10, 8, 14), (10, 10, 11, 14))

BUDGET_DAY = (2, 1)


def _weekday_on_or_before(day, weekday, holidays=()):
    """Roll back to `weekday`, then further back off any holiday."""
    while day.weekday() != weekday:
        day -= timedelta(days=1)
    while day.isoformat() in holidays or day.weekday() >= 5:
        day -= timedelta(days=1)
    return day


def weekly_expiry(day, holidays=()):
    """The Thursday of this week, rolled back off holidays.

    NSE has moved some contracts to other weekdays; this is the classic Nifty
    convention and the caller should override where a contract differs rather
    than have this guess.
    """
    thursday = day + timedelta(days=(3 - day.weekday()) % 7)
    return _weekday_on_or_before(thursday, 3, holidays)


def monthly_expiry(year, month, holidays=()):
    """Last Thursday of the month, rolled back off holidays."""
    if month == 12:
        last = date(year, 12, 31)
    else:
        last = date(year, month + 1, 1) - timedelta(days=1)
    return _weekday_on_or_before(last, 3, holidays)


def is_expiry_week(day, holidays=()):
    """True in the week containing a MONTHLY expiry — the week where rollover
    flow and pinning dominate, and weekly behaviour stops applying."""
    expiry = monthly_expiry(day.year, day.month, holidays)
    monday = expiry - timedelta(days=expiry.weekday())
    return monday <= day <= expiry


def days_to_expiry(day, holidays=()):
    """Calendar days to the next weekly expiry. Theta is negligible at five and
    brutal at one, so this is the number that sets the hold limit."""
    expiry = weekly_expiry(day, holidays)
    if expiry < day:
        expiry = weekly_expiry(day + timedelta(days=7), holidays)
    return (expiry - day).days


def is_budget_day(day):
    return (day.month, day.day) == BUDGET_DAY


def is_results_season(day):
    for start_m, start_d, end_m, end_d in RESULTS_WINDOWS:
        start = date(day.year, start_m, start_d)
        end = date(day.year, end_m, end_d)
        if start <= day <= end:
            return True
    return False


def next_mpc(day):
    """Next RBI policy date, or None once the published list runs out."""
    upcoming = [d for year in MPC_DATES for d in MPC_DATES[year] if d >= day]
    return min(upcoming) if upcoming else None


def mpc_dates_stale(day):
    """True when the MPC list no longer covers `day`.

    Reported explicitly because a stale calendar returns "no event" — which
    reads identically to "nothing scheduled" and would let the engine buy
    premium straight into a policy decision.
    """
    return next_mpc(day) is None


def events(day, holidays=()):
    """Everything known about one session."""
    dte = days_to_expiry(day, holidays)
    mpc = next_mpc(day)
    return dict(
        days_to_expiry=dte,
        weekly_expiry=weekly_expiry(day, holidays).isoformat(),
        monthly_expiry=monthly_expiry(day.year, day.month, holidays).isoformat(),
        expiry_week=is_expiry_week(day, holidays),
        is_expiry_day=dte == 0,
        budget_day=is_budget_day(day),
        results_season=is_results_season(day),
        next_mpc=mpc.isoformat() if mpc else None,
        days_to_mpc=(mpc - day).days if mpc else None,
        mpc_calendar_stale=mpc_dates_stale(day),
        # Event risk is what makes premium expensive; buying into it is how a
        # directionally correct trade still loses to an IV crush.
        event_risk=bool(is_budget_day(day) or (mpc and (mpc - day).days <= 1) or dte == 0),
    )
