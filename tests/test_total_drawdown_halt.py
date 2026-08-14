"""The lanes that actually trade had no cumulative drawdown brake.

A 15% all-time drawdown limit already existed — inside `poll_market`, so it
covered the daily equity lanes, which barely trade. Every lane that does the
trading (index_options, volume_surge, btst) checks `_risk_halt` and nothing
else, and `_risk_halt` looked only at TODAY's peak, which resets each morning.

A book could therefore bleed indefinitely provided no single session was bad
enough to trip the 6% intraday guard. That is precisely what happened:

    2026-08-03   cumulative  +Rs 25,788    one day, three trades
    08-04 -1,590 · 08-05 -914 · 08-06 -1,916 · 08-07 -6,624 · 08-10 -6,375
    08-11 -3,383 · 08-12 -9,725 · 08-13 -7,736 · 08-14 -5,109
    2026-08-14   cumulative  -Rs 17,584    equity 82,354, -34.9% off peak

Nine consecutive losing sessions, none individually alarming, no brake. And the
whole positive story was three trades: top 3 = +Rs 30,642, other 189 = -Rs 48,226.

The halt never touches open positions — exits always keep running. It only
refuses to open NEW risk.
"""
from __future__ import annotations

import sqlite3
import unittest

from app import v2_live


class TotalDrawdownHaltTest(unittest.TestCase):
    def _book(self, equities):
        con = sqlite3.connect(":memory:")
        con.execute("CREATE TABLE v2_equity(market TEXT, date TEXT, equity REAL)")
        con.execute("CREATE TABLE v2_trades(market TEXT, exit_date TEXT, reason TEXT)")
        for i, e in enumerate(equities):
            con.execute("INSERT INTO v2_equity VALUES('IN',?,?)",
                        (f"LIVE_2026-08-{i+1:02d}T10:00:00", e))
        return con

    def test_the_live_book_that_exposed_it(self) -> None:
        """126,583 peak -> 82,354 is -34.9%; the limit is 15%."""
        con = self._book([100_000, 126_583, 100_000, 82_354])
        halt, reason = v2_live._risk_halt(con, "IN")
        self.assertTrue(halt, "a 35% drawdown must stop new entries")
        self.assertIn("total-drawdown", reason)
        self.assertIn("34.9", reason)

    def test_a_shallow_drawdown_still_trades(self) -> None:
        con = self._book([100_000, 110_000, 104_000])   # -5.5%
        self.assertFalse(v2_live._risk_halt(con, "IN")[0])

    def test_the_boundary(self) -> None:
        con = self._book([100_000, 100_000, 85_100])    # -14.9%, just inside
        self.assertFalse(v2_live._risk_halt(con, "IN")[0])
        con = self._book([100_000, 100_000, 84_900])    # -15.1%, just outside
        self.assertTrue(v2_live._risk_halt(con, "IN")[0])

    def test_a_book_at_its_peak_trades(self) -> None:
        con = self._book([90_000, 95_000, 100_000])
        self.assertFalse(v2_live._risk_halt(con, "IN")[0])

    def test_an_empty_equity_series_does_not_halt(self) -> None:
        """A fresh book must not be born halted."""
        con = self._book([])
        self.assertFalse(v2_live._risk_halt(con, "IN")[0])

    def test_the_reason_names_the_limit(self) -> None:
        con = self._book([100_000, 120_000, 60_000])
        _halt, reason = v2_live._risk_halt(con, "IN")
        self.assertIn("15%", reason, "the operator must see what it tripped")


class EveryTradingLaneInheritsItTest(unittest.TestCase):
    """The point of putting it in _risk_halt rather than in one pass."""

    def test_the_lanes_that_trade_call_risk_halt(self) -> None:
        import inspect
        for fn in (v2_live.index_options_pass, v2_live.volume_surge_pass,
                   v2_live.btst_pass):
            with self.subTest(fn=fn.__name__):
                self.assertIn("_risk_halt", inspect.getsource(fn))

    def test_it_is_configurable_and_separate_from_the_daily_guard(self) -> None:
        self.assertIn("maxdd_total", v2_live.RISK)
        self.assertIn("maxdd_halt", v2_live.RISK)
        self.assertGreater(v2_live.RISK["maxdd_total"], v2_live.RISK["maxdd_halt"],
                           "the all-time limit must be looser than the intraday one")

    def test_exits_are_untouched(self) -> None:
        """A halt that stranded open positions would be far worse than the
        drawdown it is guarding against."""
        import inspect
        self.assertNotIn("_risk_halt", inspect.getsource(v2_live.exit_monitor))


if __name__ == "__main__":
    unittest.main()
