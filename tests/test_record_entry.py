"""The single writer for v2_positions.

Five lanes each had their own hand-retyped `INSERT INTO v2_positions`. That
duplication is what let the daily lane miss the frozen-quote guard the other
four had, and it is the same shape as the positional INSERT that broke as soon
as a column was added. These tests pin two things: that exactly one INSERT
exists, and that a position which is already broken never reaches the ledger.

The refusal cases matter more than they look. A stop at or above entry is not a
conservative trade, it is a position that exits the instant it is evaluated —
and it lands in v2_trades as a real loss, polluting the record the lanes are
judged on.
"""

from __future__ import annotations

import inspect
import sqlite3
import unittest

from app import v2_live

# The REAL schema, via the engine's own migrator. This used to be a
# hand-copied CREATE TABLE, which meant every column added to the live book had
# to be retyped here too — and when `expiry` was added for the option expiry
# exit, six tests failed on a table that production had and the test did not.
# A test asserting a writer lands columns correctly must run against the schema
# that writer actually targets.
def _schema(con):
    v2_live.ensure_schema(con)

GOOD = dict(market="IN", strategy="swing_meanrev", symbol="TCS", entry_date="2026-07-29",
            entry_price=100.0, shares=10.0, stop=96.0, target=110.0, trail=0.0,
            conviction=0.7, why=None)


class RecordEntryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.db = sqlite3.connect(":memory:")
        _schema(self.db)

    def rows(self):
        return self.db.execute(
            "SELECT market,strategy,symbol,entry_price,shares,stop,target,trail,peak,conviction"
            " FROM v2_positions").fetchall()

    def call(self, **over):
        args = dict(GOOD)
        args.update(over)
        return v2_live.record_entry(self.db, **args)

    def test_a_good_entry_is_written_once(self) -> None:
        self.assertIs(self.call(), True)
        self.assertEqual(len(self.rows()), 1)

    def test_columns_land_in_the_right_order(self) -> None:
        """The bug class this replaces: values silently shifting by one column."""
        self.call()
        self.assertEqual(
            self.rows()[0],
            ("IN", "swing_meanrev", "TCS", 100.0, 10.0, 96.0, 110.0, 0.0, 100.0, 0.7))

    def test_peak_defaults_to_entry_price(self) -> None:
        """All five lanes passed peak == entry; the default must preserve that,
        since exit_monitor trails from peak and a zero peak would trail from 0."""
        self.call()
        self.assertEqual(self.rows()[0][8], 100.0)

    def test_explicit_peak_is_honoured(self) -> None:
        self.call(peak=123.0)
        self.assertEqual(self.rows()[0][8], 123.0)

    def test_opened_at_is_recorded(self) -> None:
        self.call()
        stamp = self.db.execute("SELECT opened_at FROM v2_positions").fetchone()[0]
        self.assertTrue(stamp and stamp.startswith("20"), stamp)

    # ---- refusals: each of these is a position that is broken on arrival ----

    def test_stop_at_or_above_entry_is_refused(self) -> None:
        """Exits instantly at a loss — dead before it opens."""
        for stop in (100.0, 101.0):
            with self.subTest(stop=stop):
                self.assertIs(self.call(stop=stop), False)
        self.assertEqual(self.rows(), [])

    def test_target_at_or_below_entry_is_refused(self) -> None:
        for target in (100.0, 99.0):
            with self.subTest(target=target):
                self.assertIs(self.call(target=target), False)
        self.assertEqual(self.rows(), [])

    def test_non_positive_price_or_size_is_refused(self) -> None:
        for field, value in (("entry_price", 0.0), ("entry_price", -5.0),
                             ("shares", 0.0), ("shares", -3.0)):
            with self.subTest(field=field, value=value):
                self.assertIs(self.call(**{field: value}), False)
        self.assertEqual(self.rows(), [])

    def test_empty_symbol_is_refused(self) -> None:
        self.assertIs(self.call(symbol=""), False)
        self.assertEqual(self.rows(), [])

    def test_zero_stop_and_target_are_allowed(self) -> None:
        """btst carries no target and rides to the next open; a lane legitimately
        passing 0.0 must not be mistaken for a broken one."""
        self.assertIs(self.call(strategy="btst", target=0.0), True)
        self.assertIs(self.call(symbol="INFY", stop=0.0), True)
        self.assertEqual(len(self.rows()), 2)


class SingleWriterTest(unittest.TestCase):
    def test_only_one_insert_statement_exists(self) -> None:
        """If this fails someone added a sixth buy path with its own INSERT, and
        every guard now has to be remembered in one more place."""
        src = inspect.getsource(v2_live)
        self.assertEqual(src.count("INSERT INTO v2_positions"), 1)

    def test_the_insert_lives_in_record_entry(self) -> None:
        self.assertIn("INSERT INTO v2_positions",
                      inspect.getsource(v2_live.record_entry))

    def test_every_lane_routes_through_it(self) -> None:
        for name in ("poll_market", "intraday_news_pass", "volume_surge_pass",
                     "intraday_momentum_pass", "btst_pass"):
            with self.subTest(lane=name):
                self.assertIn("record_entry(",
                              inspect.getsource(getattr(v2_live, name)))

    def test_a_refused_daily_entry_returns_the_cash(self) -> None:
        """poll_market debits cash BEFORE writing the row, so a refusal must
        credit it back — otherwise the book quietly loses buying power for a
        trade that never happened."""
        src = inspect.getsource(v2_live.poll_market)
        idx = src.index("record_entry(")
        window = src[idx:idx + 400]
        self.assertIn("cash +=", window)


if __name__ == "__main__":
    unittest.main()
