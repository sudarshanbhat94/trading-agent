"""The intraday backtest loader reads two different schemas.

The recorder originally wrote a scratch `candles(source, ts ISO)` table; the
retained data in var/intraday_yahoo.db is a `bars` table with no source column
and epoch-second timestamps. The loader assumed the first and crashed on the
second, which meant the only surviving intraday history could not be
backtested at all. Both shapes are pinned here.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sqlite3
import sys
import unittest
from datetime import datetime, timedelta, timezone

_path = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "intraday_momentum_bt.py"
_spec = importlib.util.spec_from_file_location("intraday_momentum_bt", _path)
imbt = importlib.util.module_from_spec(_spec)
sys.modules["intraday_momentum_bt"] = imbt
_spec.loader.exec_module(imbt)

IST = timezone(timedelta(hours=5, minutes=30))


def at(hour, minute):
    """A 2026-03-02 IST session time, as (iso, epoch_seconds)."""
    moment = datetime(2026, 3, 2, hour, minute, tzinfo=IST)
    return moment.isoformat(), str(int(moment.timestamp()))


class LoaderSchemaTest(unittest.TestCase):
    def test_reads_the_candles_schema_with_iso_timestamps(self) -> None:
        con = sqlite3.connect(":memory:")
        con.execute("CREATE TABLE candles(symbol TEXT, ts TEXT, open REAL, high REAL,"
                    " low REAL, close REAL, volume REAL, source TEXT)")
        iso, _ = at(9, 45)
        con.execute("INSERT INTO candles VALUES(?,?,?,?,?,?,?,?)",
                    ("TCS", iso, 100, 101, 99, 100.5, 1000, "yahoo:5m"))
        days = imbt.load(con)
        self.assertIn("2026-03-02", days)
        self.assertEqual(days["2026-03-02"]["TCS"][0][0], 30)   # 30 min after 09:15

    def test_reads_the_bars_schema_with_epoch_timestamps(self) -> None:
        """The regression: epoch seconds hit datetime.fromisoformat and raised
        ValueError, so var/intraday_yahoo.db — the only retained intraday
        history — was unreadable."""
        con = sqlite3.connect(":memory:")
        con.execute("CREATE TABLE bars(market TEXT, symbol TEXT, ts TEXT, open REAL,"
                    " high REAL, low REAL, close REAL, volume REAL)")
        _, epoch = at(9, 45)
        con.execute("INSERT INTO bars VALUES(?,?,?,?,?,?,?,?)",
                    ("IN", "TCS", epoch, 100, 101, 99, 100.5, 1000))
        days = imbt.load(con)
        self.assertIn("2026-03-02", days)
        self.assertEqual(days["2026-03-02"]["TCS"][0][0], 30)

    def test_candles_is_preferred_when_both_exist(self) -> None:
        con = sqlite3.connect(":memory:")
        con.execute("CREATE TABLE candles(symbol TEXT, ts TEXT, open REAL, high REAL,"
                    " low REAL, close REAL, volume REAL, source TEXT)")
        con.execute("CREATE TABLE bars(market TEXT, symbol TEXT, ts TEXT, open REAL,"
                    " high REAL, low REAL, close REAL, volume REAL)")
        iso, epoch = at(9, 45)
        con.execute("INSERT INTO candles VALUES(?,?,?,?,?,?,?,?)",
                    ("FROMCANDLES", iso, 100, 101, 99, 100.5, 1000, "yahoo:5m"))
        con.execute("INSERT INTO bars VALUES(?,?,?,?,?,?,?,?)",
                    ("IN", "FROMBARS", epoch, 100, 101, 99, 100.5, 1000))
        days = imbt.load(con)
        self.assertIn("FROMCANDLES", days["2026-03-02"])
        self.assertNotIn("FROMBARS", days["2026-03-02"])

    def test_bars_outside_the_session_are_dropped(self) -> None:
        con = sqlite3.connect(":memory:")
        con.execute("CREATE TABLE bars(market TEXT, symbol TEXT, ts TEXT, open REAL,"
                    " high REAL, low REAL, close REAL, volume REAL)")
        for hour, minute in ((8, 0), (17, 0)):
            moment = datetime(2026, 3, 2, hour, minute, tzinfo=IST)
            con.execute("INSERT INTO bars VALUES(?,?,?,?,?,?,?,?)",
                        ("IN", "TCS", str(int(moment.timestamp())),
                         100, 101, 99, 100.5, 1000))
        self.assertEqual(dict(imbt.load(con)), {})


if __name__ == "__main__":
    unittest.main()
