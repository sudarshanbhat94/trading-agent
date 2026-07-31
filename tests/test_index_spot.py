"""A live index level, derived from option quotes, and the candles built on it.

There was no index price anywhere in the system: `latest_quotes` holds
equities, `nfo_quotes` holds contracts, and every index level came from
`fo_bhav.underlying` — the PREVIOUS session's bhavcopy close. That is why the
direction call read yesterday's candle, why the ATM strike was picked against
yesterday's spot, and why the dashboard could not draw an index chart at all.

Put-call parity recovers it from data already polled every 8 seconds:

    forward = strike + call - put

The tests below pin the parity arithmetic against hand-computed values, and
pin the two properties that decide whether the result is honest: a thin or
far-from-the-money reading must be REFUSED rather than reported as a price,
and a bar must widen rather than restart when the process samples it again.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from app import index_spot

IST = timezone(timedelta(hours=5, minutes=30))


def q(underlying, strike, opt_type, price, expiry="2026-08-04"):
    return (underlying, strike, opt_type, price, expiry)


class ParityTest(unittest.TestCase):
    def test_the_forward_is_strike_plus_call_minus_put(self) -> None:
        """Hand-computed: 24300 + 167.65 - 79.10 = 24388.55."""
        rows = [q("NIFTY", 24300, "CE", 167.65), q("NIFTY", 24300, "PE", 79.10)]
        self.assertAlmostEqual(index_spot.spot("NIFTY", rows)["price"], 24388.55, places=2)

    def test_the_nearest_strikes_are_averaged(self) -> None:
        """Three nearest pairs, so one wide quote cannot move the level far.
        24388.55, 24390.00 and 24387.00 -> 24388.52."""
        rows = [q("NIFTY", 24300, "CE", 167.65), q("NIFTY", 24300, "PE", 79.10),
                q("NIFTY", 24350, "CE", 140.00), q("NIFTY", 24350, "PE", 100.00),
                q("NIFTY", 24400, "CE", 112.00), q("NIFTY", 24400, "PE", 125.00)]
        self.assertAlmostEqual(index_spot.spot("NIFTY", rows)["price"], 24388.52, places=1)

    def test_a_strike_far_from_the_money_is_refused(self) -> None:
        """Only a deep OTM pair is quoted: both legs are spread-dominated and
        the estimate is not worth reporting as a price."""
        rows = [q("NIFTY", 30000, "CE", 0.50), q("NIFTY", 30000, "PE", 5600.0)]
        self.assertIsNone(index_spot.spot("NIFTY", rows))

    def test_an_unpaired_strike_yields_nothing(self) -> None:
        """Parity needs BOTH legs — a call alone says nothing about spot."""
        self.assertIsNone(index_spot.spot("NIFTY", [q("NIFTY", 24300, "CE", 167.65)]))

    def test_other_underlyings_are_ignored(self) -> None:
        rows = [q("BANKNIFTY", 57300, "CE", 400.0), q("BANKNIFTY", 57300, "PE", 300.0),
                q("NIFTY", 24300, "CE", 167.65), q("NIFTY", 24300, "PE", 79.10)]
        self.assertAlmostEqual(index_spot.spot("NIFTY", rows)["price"], 24388.55, places=2)
        self.assertAlmostEqual(index_spot.spot("BANKNIFTY", rows)["price"], 57400.0, places=2)

    def test_the_reading_reports_how_well_backed_it_is(self) -> None:
        """A number alone would hide the difference between one strike 2% away
        and twelve straddling the money."""
        rows = [q("NIFTY", 24300, "CE", 167.65), q("NIFTY", 24300, "PE", 79.10),
                q("NIFTY", 24350, "CE", 140.00), q("NIFTY", 24350, "PE", 100.00)]
        out = index_spot.spot("NIFTY", rows)
        self.assertEqual(out["pairs"], 2)
        self.assertLess(out["atm_distance_pct"], 1.0)

    def test_junk_prices_do_not_raise(self) -> None:
        rows = [q("NIFTY", 0, "CE", 10), q("NIFTY", 24300, "CE", None),
                q("NIFTY", 24300, "PE", "x"), (None, None, None, None, None)]
        self.assertIsNone(index_spot.spot("NIFTY", rows))

    def test_empty_input(self) -> None:
        self.assertIsNone(index_spot.spot("NIFTY", []))


class BucketTest(unittest.TestCase):
    def test_a_bar_is_named_for_the_interval_it_opens(self) -> None:
        """Floor, not round: 09:47 belongs to the 09:45 bar."""
        self.assertEqual(index_spot.bucket(datetime(2026, 7, 31, 9, 47, tzinfo=IST)),
                         "2026-07-31T09:45")
        self.assertEqual(index_spot.bucket(datetime(2026, 7, 31, 9, 45, tzinfo=IST)),
                         "2026-07-31T09:45")
        self.assertEqual(index_spot.bucket(datetime(2026, 7, 31, 9, 44, 59, tzinfo=IST)),
                         "2026-07-31T09:40")


class BarFoldingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "m.db")
        con = sqlite3.connect(self.path)
        con.execute("CREATE TABLE nfo_quotes(symbol TEXT, source TEXT, ts TEXT, price REAL,"
                    " open REAL, high REAL, low REAL, close REAL, volume REAL,"
                    " underlying TEXT, expiry TEXT, strike REAL, option_type TEXT, lot_size REAL)")
        index_spot.ensure_schema(con)
        con.commit(); con.close()
        self.orig = index_spot.MAIN_DB
        index_spot.MAIN_DB = self.path

    def tearDown(self) -> None:
        index_spot.MAIN_DB = self.orig
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)

    def write(self, call, put, strike=24300):
        con = sqlite3.connect(self.path)
        con.execute("DELETE FROM nfo_quotes")
        for opt, price in (("CE", call), ("PE", put)):
            con.execute("INSERT INTO nfo_quotes(symbol,price,underlying,expiry,strike,option_type)"
                        " VALUES(?,?,?,?,?,?)",
                        (f"NIFTY{strike}{opt}", price, "NIFTY", "2026-08-04", strike, opt))
        con.commit(); con.close()

    def bar(self):
        con = sqlite3.connect(self.path)
        row = con.execute("SELECT open,high,low,close,n FROM index_bars_5m").fetchone()
        con.close()
        return row

    def test_a_second_sample_widens_the_bar_rather_than_replacing_it(self) -> None:
        """A restart mid-bar must keep the high and low already seen, not
        restart the candle from the current price."""
        when = datetime(2026, 7, 31, 9, 47, tzinfo=IST)
        self.write(167.65, 79.10)                     # -> 24388.55
        index_spot.observe(["NIFTY"], now=when)
        self.write(200.00, 50.00)                     # -> 24450.00, a new high
        index_spot.observe(["NIFTY"], now=when)
        self.write(150.00, 100.00)                    # -> 24350.00, a new low
        index_spot.observe(["NIFTY"], now=when)
        open_, high, low, close, n = self.bar()
        self.assertAlmostEqual(open_, 24388.55, places=2)   # first sample kept
        self.assertAlmostEqual(high, 24450.00, places=2)
        self.assertAlmostEqual(low, 24350.00, places=2)
        self.assertAlmostEqual(close, 24350.00, places=2)   # latest sample
        self.assertEqual(n, 3)

    def test_a_new_interval_starts_a_new_bar(self) -> None:
        self.write(167.65, 79.10)
        index_spot.observe(["NIFTY"], now=datetime(2026, 7, 31, 9, 47, tzinfo=IST))
        index_spot.observe(["NIFTY"], now=datetime(2026, 7, 31, 9, 52, tzinfo=IST))
        con = sqlite3.connect(self.path)
        stamps = [r[0] for r in con.execute("SELECT ts FROM index_bars_5m ORDER BY ts")]
        con.close()
        self.assertEqual(stamps, ["2026-07-31T09:45", "2026-07-31T09:50"])

    def test_bars_come_back_oldest_first(self) -> None:
        """Chart order — a reversed series would draw the session backwards."""
        self.write(167.65, 79.10)
        for minute in (9, 14, 19):
            index_spot.observe(["NIFTY"], now=datetime(2026, 7, 31, 10, minute, tzinfo=IST))
        rows = index_spot.bars("NIFTY", con=sqlite3.connect(self.path))
        self.assertEqual([r["ts"] for r in rows],
                         ["2026-07-31T10:05", "2026-07-31T10:10", "2026-07-31T10:15"])

    def test_an_unquotable_index_records_nothing(self) -> None:
        """No pair, no bar — an empty candle would look like a flat market."""
        self.assertEqual(index_spot.observe(["BANKNIFTY"],
                                            now=datetime(2026, 7, 31, 9, 47, tzinfo=IST)), 0)

    def test_prune_drops_old_bars_only(self) -> None:
        self.write(167.65, 79.10)
        index_spot.observe(["NIFTY"], now=datetime.now(IST))
        con = sqlite3.connect(self.path)
        con.execute("INSERT INTO index_bars_5m(symbol,ts,open,high,low,close)"
                    " VALUES('NIFTY','2020-01-01T09:15',1,1,1,1)")
        con.commit(); con.close()
        index_spot.prune(days=30)
        con = sqlite3.connect(self.path)
        stamps = [r[0] for r in con.execute("SELECT ts FROM index_bars_5m")]
        con.close()
        self.assertNotIn("2020-01-01T09:15", stamps)
        self.assertEqual(len(stamps), 1)

    def test_a_missing_table_reads_as_no_bars(self) -> None:
        con = sqlite3.connect(":memory:")
        self.assertEqual(index_spot.bars("NIFTY", con=con), [])
        con.close()


if __name__ == "__main__":
    unittest.main()
