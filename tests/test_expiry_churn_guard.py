"""The buy/sell churn loop, and the two independent guards against it.

THE BUG, TWICE. An entry rule and an exit rule disagreed about whether a
contract was tradeable, on an 8-second cadence:

  2026-08-04  19 round trips of NIFTY2680424450PE   -Rs 2,610
  2026-08-07  83 round trips of NIFTY2680424650CE   -Rs 7,721

The first fix made the EXIT time-aware on expiry day. That closed the 0-DTE
door: buy a contract expiring today, have the expiry rule shut it seconds
later. It did nothing about the second door, and the loop came straight back
through it three days later.

The second door is a contract that expired DAYS ago. `nfo_quotes` is never
pruned, so 40 rows for the 2026-08-04 expiry were still sitting in the feed
frozen at their last traded price. On a dead contract the underlying, option
type, strike, price, lot size and volume all still look perfectly healthy, so
`_pick_contract` — which had no expiry gate at all — kept selecting it, and the
exit kept (correctly) closing it. Entry price 0.80, exit price 0.80, eight
seconds apart, eighty-three times. No view, no price change, pure cost.

So there are two tests here, at two levels:

  * the ROOT CAUSE: entry now refuses what the exit would immediately close,
    via the SAME function, so the two cannot drift apart a third time;
  * the BLAST RADIUS: nothing capped the loop either time. A per-symbol daily
    round-trip cap turns the next one — whatever its cause — into a bounded
    nuisance instead of an all-day bleed.
"""
from __future__ import annotations

import sqlite3
import unittest
from datetime import date

from app import v2_live


def _chain(expiry, symbol="NIFTY2680424650CE"):
    return {symbol: dict(underlying="NIFTY", option_type="CE", strike=24650.0,
                         price=0.80, lot_size=75.0, vol=1000.0, expiry=expiry)}


class EntryRefusesExpiredContractsTest(unittest.TestCase):
    TODAY = date(2026, 8, 7)

    def test_the_exact_contract_that_ran_83_times(self) -> None:
        """Expired 2026-08-04, still quoted on 2026-08-07. Must not be picked."""
        picked = v2_live._pick_contract(
            "NIFTY", "CE", 24650.0, _chain("2026-08-04"),
            max_cost=1e9, today=self.TODAY, now_hhmm="09:30")
        self.assertIsNone(picked, "bought a contract that expired three days ago")

    def test_a_live_contract_is_still_picked(self) -> None:
        """The guard must not close the lane down."""
        picked = v2_live._pick_contract(
            "NIFTY", "CE", 24650.0, _chain("2026-08-11"),
            max_cost=1e9, today=self.TODAY, now_hhmm="09:30")
        self.assertIsNotNone(picked)
        self.assertEqual(picked["expiry"], "2026-08-11")

    def test_zero_dte_is_still_tradeable_before_squareoff(self) -> None:
        """Expiry day is DELIBERATELY tradeable — the lane buys 0-DTE on
        purpose. The guard must reject dead contracts, not this one."""
        picked = v2_live._pick_contract(
            "NIFTY", "CE", 24650.0, _chain("2026-08-07"),
            max_cost=1e9, today=self.TODAY, now_hhmm="09:30")
        self.assertIsNotNone(picked, "0-DTE entry is intended and was not the bug")

    def test_zero_dte_is_refused_after_squareoff(self) -> None:
        picked = v2_live._pick_contract(
            "NIFTY", "CE", 24650.0, _chain("2026-08-07"),
            max_cost=1e9, today=self.TODAY, now_hhmm="15:20")
        self.assertIsNone(picked)

    def test_entry_and_exit_share_one_definition(self) -> None:
        """The whole point. Two rules that merely RESEMBLE each other is how
        this returned; the entry must call the exit's own function."""
        import inspect
        src = inspect.getsource(v2_live._pick_contract)
        self.assertIn("_expired_or_expiring(q.get(\"expiry\"), today, now_hhmm)", src)

    def test_the_caller_actually_passes_the_date(self) -> None:
        """The gate is a no-op without it, which would be a silent regression."""
        import inspect
        src = inspect.getsource(v2_live.index_options_pass)
        self.assertIn("today=today, now_hhmm=hm", src)

    def test_no_expiry_information_is_not_treated_as_expired(self) -> None:
        picked = v2_live._pick_contract(
            "NIFTY", "CE", 24650.0, _chain(None),
            max_cost=1e9, today=self.TODAY, now_hhmm="09:30")
        self.assertIsNotNone(picked)


class ChurnCircuitBreakerTest(unittest.TestCase):
    """The guard that is not about expiry at all."""

    def _book(self):
        con = sqlite3.connect(":memory:")
        con.execute("CREATE TABLE v2_positions(market,strategy,symbol,entry_date,"
                    "entry_price,shares,stop,target,trail,peak,conviction,opened_at,"
                    "why,expiry,sleeve,regime)")
        con.execute("CREATE TABLE v2_trades(market,symbol,entry_date)")
        return con

    def _enter(self, con, symbol="ACME"):
        return v2_live.record_entry(con, "IN", "volume_surge", symbol, "2026-08-07",
                                    100.0, 10, 97.5, 103.5, 0.0, 0.5, "{}")

    def test_it_stops_the_loop_at_the_cap(self) -> None:
        con = self._book()
        for i in range(v2_live.MAX_ROUND_TRIPS_PER_DAY):
            self.assertTrue(self._enter(con), f"round trip {i+1} is legitimate")
            con.execute("INSERT INTO v2_trades VALUES('IN','ACME','2026-08-07')")
        self.assertFalse(self._enter(con), "the 4th round trip is churn")

    def test_83_becomes_3(self) -> None:
        """Today's actual loop, replayed against the guard."""
        con = self._book()
        taken = 0
        for _ in range(83):
            if self._enter(con, "NIFTY2680424650CE"):
                taken += 1
                con.execute("INSERT INTO v2_trades VALUES"
                            "('IN','NIFTY2680424650CE','2026-08-07')")
        self.assertEqual(taken, 3)

    def test_a_different_symbol_is_unaffected(self) -> None:
        con = self._book()
        for _ in range(v2_live.MAX_ROUND_TRIPS_PER_DAY):
            con.execute("INSERT INTO v2_trades VALUES('IN','ACME','2026-08-07')")
        self.assertTrue(self._enter(con, "OTHER"))

    def test_a_new_day_resets_it(self) -> None:
        con = self._book()
        for _ in range(10):
            con.execute("INSERT INTO v2_trades VALUES('IN','ACME','2026-08-06')")
        self.assertTrue(self._enter(con), "yesterday's trades must not block today")

    def test_the_manual_buy_button_is_never_blocked(self) -> None:
        """The operator's own Buy is a decision, not a loop."""
        con = self._book()
        for _ in range(20):
            con.execute("INSERT INTO v2_trades VALUES('IN','ACME','2026-08-07')")
        self.assertTrue(v2_live.record_entry(
            con, "IN", "manual", "ACME", "2026-08-07", 100.0, 10,
            97.5, 103.5, 0.0, 0.5, "{}"))


if __name__ == "__main__":
    unittest.main()
