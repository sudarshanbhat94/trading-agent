"""Order fills must show the time they actually happened, or no time at all.

The order log rendered "30 Jul 05:30 IST" against four of six fills on 30 Jul.
Nothing happened at 05:30 — the market opens at 09:15. The value came from
`entry_date` / `exit_date`, which hold a DATE and no time: parsed as midnight,
assumed to be UTC, then shifted +5:30 into IST. A field that never held a time
was producing a precise-looking one, and it was wrong for every closed trade in
the book.

Two things are tested here, and the second matters more than the first:

  * a date-only value renders as a DATE — inventing a clock time is worse than
    admitting there is none, because "05:30" reads as evidence;
  * closed trades now carry real opened_at / closed_at timestamps, so the sell
    side of the ledger has a time of day at all. It never did: exiting threw
    away the position's opened_at and recorded only today's date.
"""

from __future__ import annotations

import sqlite3
import unittest

from app.v2_web import _col, _ist


class DateOnlyRenderingTest(unittest.TestCase):
    def test_a_date_never_grows_a_clock_time(self) -> None:
        """The bug, stated directly: 05:30 IST is midnight UTC shifted, and it
        appeared on every date-only row in the order log."""
        self.assertEqual(_ist("2026-07-30"), "30 Jul")
        self.assertNotIn("05:30", _ist("2026-07-30"))
        self.assertNotIn("IST", _ist("2026-07-30"))

    def test_a_real_utc_timestamp_still_converts_to_ist(self) -> None:
        """09:19 IST is 03:49 UTC. Positions store UTC, and those rows were
        always right — the fix must not disturb them."""
        self.assertEqual(_ist("2026-07-30T03:49:00+00:00"), "30 Jul 09:19 IST")

    def test_a_naive_timestamp_is_still_read_as_utc(self) -> None:
        self.assertEqual(_ist("2026-07-30T05:22:00"), "30 Jul 10:52 IST")

    def test_the_live_prefix_is_stripped(self) -> None:
        self.assertEqual(_ist("LIVE_2026-07-30T03:49:00+00:00"), "30 Jul 09:19 IST")

    def test_unparseable_values_pass_through(self) -> None:
        self.assertEqual(_ist("not a date"), "not a date")
        self.assertEqual(_ist(None), "")
        self.assertEqual(_ist(""), "")


class TradeTimestampColumnsTest(unittest.TestCase):
    """A closed trade used to record dates only, so the SELL side of the order
    log had no time of day anywhere in it — there was nothing to render."""

    def setUp(self) -> None:
        self.con = sqlite3.connect(":memory:")
        from app import v2_live
        v2_live.ensure_schema(self.con)

    def tearDown(self) -> None:
        self.con.close()

    def cols(self):
        return {r[1] for r in self.con.execute("PRAGMA table_info(v2_trades)")}

    def test_trades_carry_both_timestamps(self) -> None:
        self.assertIn("opened_at", self.cols())
        self.assertIn("closed_at", self.cols())

    def test_migration_adds_them_to_an_older_book(self) -> None:
        """The live book predates these columns. CREATE TABLE IF NOT EXISTS does
        nothing to an existing table, so the ALTER has to run."""
        old = sqlite3.connect(":memory:")
        old.execute(
            "CREATE TABLE v2_trades(id INTEGER PRIMARY KEY AUTOINCREMENT, market TEXT,"
            " strategy TEXT, symbol TEXT, entry_date TEXT, entry_price REAL, exit_date TEXT,"
            " exit_price REAL, shares REAL, pnl REAL, return_pct REAL, reason TEXT, conviction REAL)")
        old.execute("INSERT INTO v2_trades(market,symbol,entry_date,exit_date,pnl,return_pct)"
                    " VALUES('IN','SYRMA','2026-07-30','2026-07-30',-80,-0.32)")
        from app import v2_live
        v2_live.ensure_schema(old)
        cols = {r[1] for r in old.execute("PRAGMA table_info(v2_trades)")}
        self.assertIn("opened_at", cols)
        self.assertIn("closed_at", cols)
        # the existing row survives, with the new columns empty rather than faked
        row = old.execute("SELECT symbol,opened_at,closed_at FROM v2_trades").fetchone()
        self.assertEqual((row[0], row[1], row[2]), ("SYRMA", None, None))
        old.close()

    def test_exit_carries_opened_at_across_and_stamps_closed_at(self) -> None:
        """The buy leg's real fill time used to be destroyed on exit: the
        position row held opened_at, the trade row had nowhere to put it."""
        self.con.execute(
            "INSERT INTO v2_positions(market,strategy,symbol,entry_date,entry_price,shares,"
            "stop,target,trail,peak,conviction,opened_at)"
            " VALUES('IN','swing_meanrev','SYRMA','2026-07-30',1410.6,18,1300,1500,0,1410.6,1.0,"
            "'2026-07-30T03:49:00+00:00')")
        pid = self.con.execute("SELECT id FROM v2_positions").fetchone()[0]
        self.con.execute(
            "INSERT INTO v2_trades(market,strategy,symbol,entry_date,entry_price,exit_date,"
            "exit_price,shares,pnl,return_pct,reason,conviction,opened_at,closed_at)"
            " SELECT market,strategy,symbol,entry_date,entry_price,?,?,?,?,?,?,conviction,opened_at,?"
            " FROM v2_positions WHERE id=?",
            ("2026-07-30", 1411.8, 18, -80.0, -0.32, "stop",
             "2026-07-30T06:11:00+00:00", pid))
        oat, cat = self.con.execute("SELECT opened_at,closed_at FROM v2_trades").fetchone()
        self.assertEqual(_ist(oat), "30 Jul 09:19 IST")
        self.assertEqual(_ist(cat), "30 Jul 11:41 IST")


class OrdersEndpointTest(unittest.TestCase):
    """End-to-end through /api/orders, because the helper being right does not
    prove the endpoint is: the SELECT gained two columns and the row unpack has
    to match, and it must still run against a book that lacks them."""

    def setUp(self) -> None:
        import tempfile, os
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "v2.db")
        con = sqlite3.connect(self.path)
        from app import v2_live
        v2_live.ensure_schema(con)
        con.execute(
            "INSERT INTO v2_trades(market,strategy,symbol,entry_date,entry_price,exit_date,"
            "exit_price,shares,pnl,return_pct,reason,conviction,opened_at,closed_at)"
            " VALUES('IN','swing_meanrev','SYRMA','2026-07-30',1410.6,'2026-07-30',1411.8,18,"
            "-80.0,-0.32,'stop',1.0,'2026-07-30T03:49:00+00:00','2026-07-30T06:11:00+00:00')")
        con.commit(); con.close()
        self.orig = None

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)

    def orders(self):
        from app import v2_web
        import json
        orig = v2_web.V2_DB
        v2_web.V2_DB = self.path
        try:
            return json.loads(v2_web.api_orders(limit=50).body)
        finally:
            v2_web.V2_DB = orig

    def test_both_legs_show_their_real_time(self) -> None:
        rows = {o["side"]: o for o in self.orders()}
        self.assertEqual(rows["BUY"]["when"], "30 Jul 09:19 IST")
        self.assertEqual(rows["SELL"]["when"], "30 Jul 11:41 IST")
        for o in rows.values():
            self.assertNotIn("05:30", o["when"])

    def test_the_buy_leg_of_a_same_day_round_trip_counts_as_today(self) -> None:
        """It carried no `today` flag, so a stock bought and sold the same day
        appeared in the home list as a sell with no matching buy."""
        from datetime import datetime as _dt
        from app.v2_web import IST
        rows = {o["side"]: o for o in self.orders()}
        expected = _dt.now(IST).date().isoformat() == "2026-07-30"
        self.assertEqual(rows["BUY"]["today"], expected)
        self.assertEqual(rows["SELL"]["today"], expected)

    def test_a_book_without_the_columns_still_renders(self) -> None:
        con = sqlite3.connect(self.path)
        con.execute("DROP TABLE v2_trades")
        con.execute(
            "CREATE TABLE v2_trades(id INTEGER PRIMARY KEY AUTOINCREMENT, market TEXT,"
            " strategy TEXT, symbol TEXT, entry_date TEXT, entry_price REAL, exit_date TEXT,"
            " exit_price REAL, shares REAL, pnl REAL, return_pct REAL, reason TEXT, conviction REAL)")
        con.execute("INSERT INTO v2_trades(market,strategy,symbol,entry_date,entry_price,exit_date,"
                    "exit_price,shares,pnl,return_pct,reason,conviction)"
                    " VALUES('IN','swing_meanrev','SYRMA','2026-07-30',1410.6,'2026-07-30',"
                    "1411.8,18,-80.0,-0.32,'stop',1.0)")
        con.commit(); con.close()
        rows = {o["side"]: o for o in self.orders()}
        # no time was recorded, so none is shown — the date alone, not a fiction
        self.assertEqual(rows["BUY"]["when"], "30 Jul")
        self.assertEqual(rows["SELL"]["when"], "30 Jul")


class MissingColumnFallbackTest(unittest.TestCase):
    """The web process opens the book read-only, so if it starts before the
    engine has migrated it cannot add the column and must not 500."""

    def test_absent_column_becomes_a_null_literal(self) -> None:
        con = sqlite3.connect(":memory:")
        con.execute("CREATE TABLE v2_trades(id INTEGER, symbol TEXT)")
        self.assertEqual(_col(con, "v2_trades", "closed_at"), "NULL")
        self.assertEqual(_col(con, "v2_trades", "symbol"), "symbol")
        # and the generated SQL is valid either way
        row = con.execute("SELECT symbol," + _col(con, "v2_trades", "closed_at")
                          + " FROM v2_trades").fetchall()
        self.assertEqual(row, [])
        con.close()

    def test_a_missing_table_does_not_raise(self) -> None:
        con = sqlite3.connect(":memory:")
        self.assertEqual(_col(con, "nope", "closed_at"), "NULL")
        con.close()


if __name__ == "__main__":
    unittest.main()
