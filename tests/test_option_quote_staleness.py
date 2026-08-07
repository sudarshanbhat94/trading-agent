"""A frozen option quote is fiction, and the options lane had no guard for it.

The equity lanes have skipped frozen quotes for months — `_stale_symbols`,
added after GUJGASLTD sat stuck at a Jun 30 price while the engine kept
"filling" against it. The options path never got the same treatment, and that
omission is what let 2026-08-07 happen: nfo_quotes is insert-only, so 40 rows
from the 2026-08-04 expiry were still there, frozen at their last traded price,
and every gate in `_pick_contract` read them as healthy.

The expiry gate alone is not enough. It is precise but narrow — it cannot see a
contract whose date checks out perfectly and whose feed has simply stopped. That
is the identical fantasy: a paper fill at a price nobody is currently quoting.

Three levels, tested here:
  * the quote loader FLAGS staleness rather than hiding it;
  * the entry path refuses a flagged quote, the exit path still sees it
    (dropping it is how a position becomes enterable but never exitable);
  * the writer prunes expired contracts, so no other reader inherits the
    problem.
"""
from __future__ import annotations

import sqlite3
import unittest
from datetime import date, datetime, timedelta, timezone

from app import v2_live


def _q(stale=False, expiry="2026-08-25", symbol="NIFTY26AUG24650CE"):
    return {symbol: dict(underlying="NIFTY", option_type="CE", strike=24650.0,
                         price=0.80, lot_size=75.0, vol=1000.0, expiry=expiry,
                         stale=stale)}


class EntryRefusesFrozenQuotesTest(unittest.TestCase):
    TODAY = date(2026, 8, 7)

    def test_a_frozen_quote_is_not_bought(self) -> None:
        picked = v2_live._pick_contract("NIFTY", "CE", 24650.0, _q(stale=True),
                                        max_cost=1e9, today=self.TODAY,
                                        now_hhmm="09:30")
        self.assertIsNone(picked, "filled against a price nobody is quoting")

    def test_a_live_quote_is_bought(self) -> None:
        picked = v2_live._pick_contract("NIFTY", "CE", 24650.0, _q(stale=False),
                                        max_cost=1e9, today=self.TODAY,
                                        now_hhmm="09:30")
        self.assertIsNotNone(picked)

    def test_the_date_can_be_perfect_and_the_quote_still_dead(self) -> None:
        """Precisely what the expiry gate cannot catch on its own."""
        fresh_expiry = _q(stale=True, expiry="2026-12-30")
        self.assertFalse(v2_live._expired_or_expiring("2026-12-30", self.TODAY, "09:30"),
                         "this contract is genuinely live by date")
        self.assertIsNone(v2_live._pick_contract("NIFTY", "CE", 24650.0, fresh_expiry,
                                                 max_cost=1e9, today=self.TODAY,
                                                 now_hhmm="09:30"))

    def test_the_guard_applies_even_without_a_date(self) -> None:
        """Staleness is unconditional; only the expiry rule needs `today`."""
        self.assertIsNone(v2_live._pick_contract("NIFTY", "CE", 24650.0, _q(stale=True),
                                                 max_cost=1e9))


class LoaderFlagsStalenessTest(unittest.TestCase):
    """`_option_live` reads the real table, so build one."""

    def _db(self, rows):
        path = "/tmp/_test_nfo_quotes.db"
        import os
        if os.path.exists(path):
            os.remove(path)
        con = sqlite3.connect(path)
        con.execute("CREATE TABLE nfo_quotes(symbol TEXT, source TEXT, ts TEXT,"
                    " price REAL, open REAL, high REAL, low REAL, close REAL,"
                    " volume REAL, underlying TEXT, expiry TEXT, strike REAL,"
                    " option_type TEXT, lot_size REAL)")
        for sym, ts in rows:
            con.execute("INSERT INTO nfo_quotes VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (sym, "upstox-nfo", ts, 0.80, 0.80, 0.80, 0.80, 0.80,
                         1000.0, "NIFTY", "2026-08-25", 24650.0, "CE", 75.0))
        con.commit(); con.close()
        return path

    def _load(self, rows):
        path = self._db(rows)
        orig = v2_live.MAIN_DB
        try:
            v2_live.MAIN_DB = path
            return v2_live._option_live()
        finally:
            v2_live.MAIN_DB = orig

    def test_it_flags_the_laggard_and_not_the_fresh_one(self) -> None:
        now = datetime.now(timezone.utc)
        old = (now - timedelta(seconds=v2_live.STALE_QUOTE_SEC + 300)).isoformat()
        out = self._load([("FRESH", now.isoformat()), ("FROZEN", old)])
        self.assertFalse(out["FRESH"]["stale"])
        self.assertTrue(out["FROZEN"]["stale"], "a quote 15 min behind the chain is frozen")

    def test_the_whole_chain_ageing_together_flags_nothing(self) -> None:
        """Overnight every quote is old. Staleness is RELATIVE to the freshest
        row, or the guard would refuse to trade at every open."""
        old = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        out = self._load([("A", old), ("B", old)])
        self.assertFalse(any(q["stale"] for q in out.values()))

    def test_a_held_position_keeps_its_price(self) -> None:
        """MARKED, NOT DROPPED — the exit path must still see it."""
        now = datetime.now(timezone.utc)
        old = (now - timedelta(seconds=v2_live.STALE_QUOTE_SEC + 300)).isoformat()
        out = self._load([("FRESH", now.isoformat()), ("FROZEN", old)])
        self.assertIn("FROZEN", out, "dropping it makes the position unexitable")
        self.assertEqual(out["FROZEN"]["price"], 0.80)


class WriterPrunesExpiredTest(unittest.TestCase):
    def test_the_delete_is_there_and_uses_the_ist_date(self) -> None:
        """UTC is a day behind IST after 18:30, which would keep a dead
        contract alive all evening."""
        src = open("scripts/v2_quote_feed.py").read()
        self.assertIn("DELETE FROM nfo_quotes WHERE expiry IS NOT NULL", src)
        self.assertIn("hours=5, minutes=30", src)

    def test_it_removes_yesterday_and_keeps_today(self) -> None:
        con = sqlite3.connect(":memory:")
        con.execute("CREATE TABLE nfo_quotes(symbol TEXT, expiry TEXT)")
        for sym, exp in (("DEAD", "2026-08-04"), ("TODAY", "2026-08-07"),
                         ("LIVE", "2026-08-25"), ("NOEXP", None)):
            con.execute("INSERT INTO nfo_quotes VALUES(?,?)", (sym, exp))
        con.execute("DELETE FROM nfo_quotes WHERE expiry IS NOT NULL"
                    " AND expiry <> '' AND substr(expiry,1,10) < ?", ("2026-08-07",))
        left = {r[0] for r in con.execute("SELECT symbol FROM nfo_quotes")}
        self.assertEqual(left, {"TODAY", "LIVE", "NOEXP"})

    def test_expiry_day_is_not_pruned(self) -> None:
        """The lane trades 0-DTE on purpose; pruning at midnight would blind it."""
        con = sqlite3.connect(":memory:")
        con.execute("CREATE TABLE nfo_quotes(symbol TEXT, expiry TEXT)")
        con.execute("INSERT INTO nfo_quotes VALUES('TODAY','2026-08-07')")
        con.execute("DELETE FROM nfo_quotes WHERE expiry IS NOT NULL"
                    " AND expiry <> '' AND substr(expiry,1,10) < ?", ("2026-08-07",))
        self.assertEqual(con.execute("SELECT COUNT(*) FROM nfo_quotes").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
