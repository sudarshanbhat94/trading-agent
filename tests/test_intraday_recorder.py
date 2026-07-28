"""5-minute intraday bar recorder.

The subtle part is volume. `latest_quotes.volume` is the day's CUMULATIVE
volume, so a bar's own volume is the growth across that bar. Recording the raw
figure would make every bar look identical and enormous, and any volume-based
signal built on it would be meaningless.

Bucket boundaries are hand-computed against the IST clock.
"""

from __future__ import annotations

import os
import pathlib
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import intraday_recorder as rec  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))


class BucketTest(unittest.TestCase):
    def test_bucket_is_floored_to_five_minutes(self) -> None:
        moment = datetime(2026, 7, 28, 9, 17, 42, tzinfo=IST)
        self.assertEqual(rec.bar_start(moment).strftime("%H:%M:%S"), "09:15:00")

    def test_exact_boundary_belongs_to_its_own_bar(self) -> None:
        moment = datetime(2026, 7, 28, 9, 20, 0, tzinfo=IST)
        self.assertEqual(rec.bar_start(moment).strftime("%H:%M"), "09:20")

    def test_buckets_align_to_the_hour_not_to_start_time(self) -> None:
        """Starting the recorder at 09:17 must not shift every bucket by 2
        minutes for the rest of the day."""
        for minute, expected in ((3, "09:00"), (7, "09:05"), (14, "09:10"),
                                 (59, "09:55")):
            with self.subTest(minute=minute):
                moment = datetime(2026, 7, 28, 9, minute, tzinfo=IST)
                self.assertEqual(rec.bar_start(moment).strftime("%H:%M"), expected)

    def test_utc_input_is_converted_to_ist(self) -> None:
        """09:26 UTC is 14:56 IST, which floors to the 14:55 bar."""
        moment = datetime(2026, 7, 28, 9, 26, tzinfo=timezone.utc)
        self.assertEqual(rec.bar_start(moment).strftime("%H:%M"), "14:55")


class FoldTest(unittest.TestCase):
    def test_first_tick_seeds_the_bar(self) -> None:
        bar = rec.fold_tick(None, 100.0, 5000)
        self.assertEqual((bar["open"], bar["high"], bar["low"], bar["close"]),
                         (100.0, 100.0, 100.0, 100.0))

    def test_open_never_changes(self) -> None:
        bar = rec.fold_tick(None, 100.0, 5000)
        for price in (105.0, 95.0, 102.0):
            bar = rec.fold_tick(bar, price, 6000)
        self.assertEqual(bar["open"], 100.0)

    def test_high_low_and_close_track(self) -> None:
        bar = rec.fold_tick(None, 100.0, 5000)
        for price in (105.0, 95.0, 102.0):
            bar = rec.fold_tick(bar, price, 6000)
        self.assertEqual(bar["high"], 105.0)
        self.assertEqual(bar["low"], 95.0)
        self.assertEqual(bar["close"], 102.0)

    def test_bar_volume_is_the_growth_not_the_cumulative_total(self) -> None:
        """The whole point: 5,000 -> 8,200 across the bar is 3,200 traded,
        not 8,200."""
        bar = rec.fold_tick(None, 100.0, 5000)
        bar = rec.fold_tick(bar, 101.0, 8200)
        self.assertEqual(rec.bar_volume(bar), 3200.0)

    def test_flat_volume_gives_zero(self) -> None:
        bar = rec.fold_tick(None, 100.0, 5000)
        bar = rec.fold_tick(bar, 101.0, 5000)
        self.assertEqual(rec.bar_volume(bar), 0.0)

    def test_session_rollover_rebases_instead_of_going_negative(self) -> None:
        """Cumulative volume resets each morning. A drop means a new session,
        not negative trading."""
        bar = rec.fold_tick(None, 100.0, 900000)
        bar = rec.fold_tick(bar, 101.0, 1200)
        self.assertGreaterEqual(rec.bar_volume(bar), 0.0)

    def test_missing_volume_is_tolerated(self) -> None:
        bar = rec.fold_tick(None, 100.0, None)
        bar = rec.fold_tick(bar, 101.0, None)
        self.assertEqual(rec.bar_volume(bar), 0.0)
        self.assertEqual(bar["close"], 101.0)


class WriteTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = str(pathlib.Path(self._tmp.name) / "agent.db")
        self.con = sqlite3.connect(self.path)
        rec.ensure_schema(self.con)
        self.con.execute("CREATE TABLE IF NOT EXISTS latest_quotes(symbol TEXT, ts TEXT, "
                         "price REAL, open REAL, high REAL, low REAL, close REAL, "
                         "volume REAL, source TEXT)")
        self.con.commit()

    def tearDown(self) -> None:
        self.con.close()
        self._tmp.cleanup()

    def test_bars_are_written_under_the_intraday_source(self) -> None:
        bucket = datetime(2026, 7, 28, 9, 15, tzinfo=IST)
        bars = {"ABC": rec.fold_tick(rec.fold_tick(None, 100.0, 1000), 103.0, 4000)}
        self.assertEqual(rec.flush(self.con, bars, bucket), 1)
        row = self.con.execute(
            "SELECT symbol,open,high,low,close,volume,source FROM candles").fetchone()
        self.assertEqual(row[0], "ABC")
        self.assertEqual((row[1], row[2], row[3], row[4]), (100.0, 103.0, 100.0, 103.0))
        self.assertEqual(row[5], 3000.0)
        self.assertEqual(row[6], "intraday:5m")

    def test_daily_candles_are_untouched(self) -> None:
        """Intraday bars share the table with daily ones; the primary key
        includes source, so they cannot collide."""
        self.con.execute("INSERT INTO candles VALUES('ABC','2026-07-27',1,2,0.5,1.5,10,"
                         "'upstox-live:day')")
        self.con.commit()
        rec.flush(self.con, {"ABC": rec.fold_tick(None, 100.0, 1000)},
                  datetime(2026, 7, 28, 9, 15, tzinfo=IST))
        daily = self.con.execute(
            "SELECT COUNT(*) FROM candles WHERE source='upstox-live:day'").fetchone()[0]
        intraday = self.con.execute(
            "SELECT COUNT(*) FROM candles WHERE source='intraday:5m'").fetchone()[0]
        self.assertEqual((daily, intraday), (1, 1))

    def test_rewriting_a_bucket_replaces_rather_than_duplicates(self) -> None:
        bucket = datetime(2026, 7, 28, 9, 15, tzinfo=IST)
        rec.flush(self.con, {"ABC": rec.fold_tick(None, 100.0, 1000)}, bucket)
        rec.flush(self.con, {"ABC": rec.fold_tick(None, 111.0, 1000)}, bucket)
        rows = self.con.execute("SELECT close FROM candles WHERE source='intraday:5m'").fetchall()
        self.assertEqual(rows, [(111.0,)])

    def test_empty_bucket_writes_nothing(self) -> None:
        self.assertEqual(rec.flush(self.con, {}, datetime.now(IST)), 0)

    def test_reads_the_quote_snapshot(self) -> None:
        self.con.execute("INSERT INTO latest_quotes(symbol,price,volume) VALUES('ABC',100.0,5000)")
        self.con.execute("INSERT INTO latest_quotes(symbol,price,volume) VALUES('XYZ',50.0,NULL)")
        self.con.commit()
        quotes = rec.read_quotes(self.con)
        self.assertEqual(quotes["ABC"], (100.0, 5000.0))
        self.assertEqual(quotes["XYZ"], (50.0, None))

    def test_rows_without_a_price_are_skipped(self) -> None:
        self.con.execute("INSERT INTO latest_quotes(symbol,price,volume) VALUES('ABC',NULL,1)")
        self.con.commit()
        self.assertEqual(rec.read_quotes(self.con), {})


if __name__ == "__main__":
    unittest.main()
