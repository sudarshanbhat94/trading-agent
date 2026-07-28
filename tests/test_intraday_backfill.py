"""Yahoo 5-minute backfill.

The parser is the part that matters. Yahoo pads its series with nulls for bars
it has no data for, and storing those as zeros would create fake trades at a
price of nothing — which an intraday backtest would happily "buy". The fixture
below is the real response shape, including the padding.
"""

from __future__ import annotations

import os
import pathlib
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import intraday_backfill as bf  # noqa: E402


def _payload(timestamps, opens, highs, lows, closes, volumes):
    return {"chart": {"result": [{
        "timestamp": timestamps,
        "indicators": {"quote": [{"open": opens, "high": highs, "low": lows,
                                  "close": closes, "volume": volumes}]},
    }]}}


class ParseTest(unittest.TestCase):
    def test_parses_a_clean_series(self) -> None:
        bars = bf.parse_chart(_payload(
            [1785000000, 1785000300],
            [100.0, 101.0], [102.0, 103.0], [99.0, 100.5], [101.0, 102.5], [5000, 6000]))
        self.assertEqual(len(bars), 2)
        self.assertEqual(bars[0][1:], (100.0, 102.0, 99.0, 101.0, 5000.0))

    def test_null_padded_bars_are_dropped_not_zeroed(self) -> None:
        """Yahoo pads gaps with nulls. Storing them as zeros would invent
        trades at a price of nothing, which a backtest would buy."""
        bars = bf.parse_chart(_payload(
            [1785000000, 1785000300, 1785000600],
            [100.0, None, 102.0], [102.0, None, 104.0], [99.0, None, 101.0],
            [101.0, None, 103.0], [5000, None, 7000]))
        self.assertEqual(len(bars), 2)
        self.assertNotIn(0.0, [b[4] for b in bars])

    def test_missing_volume_becomes_zero_not_none(self) -> None:
        bars = bf.parse_chart(_payload(
            [1785000000], [100.0], [102.0], [99.0], [101.0], [None]))
        self.assertEqual(bars[0][5], 0.0)

    def test_timestamps_are_iso_and_ordered(self) -> None:
        bars = bf.parse_chart(_payload(
            [1785000000, 1785000300], [1.0, 2.0], [1.0, 2.0], [1.0, 2.0],
            [1.0, 2.0], [1, 2]))
        self.assertLess(bars[0][0], bars[1][0])
        self.assertIn("T", bars[0][0])

    def test_malformed_payloads_return_empty(self) -> None:
        for payload in ({}, {"chart": {}}, {"chart": {"result": []}}, None, "nonsense"):
            with self.subTest(payload=payload):
                self.assertEqual(bf.parse_chart(payload), [])

    def test_short_quote_arrays_do_not_raise(self) -> None:
        bars = bf.parse_chart(_payload([1785000000, 1785000300], [100.0], [102.0],
                                       [99.0], [101.0], [5000]))
        self.assertEqual(len(bars), 1)


class StorageTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = str(pathlib.Path(self._tmp.name) / "agent.db")
        self.con = sqlite3.connect(self.path)
        bf.ensure_schema(self.con)

    def tearDown(self) -> None:
        self.con.close()
        self._tmp.cleanup()

    def test_bars_are_stored_under_the_yahoo_source(self) -> None:
        stored = bf.store(self.con, "RELIANCE",
                          [("2026-07-28T04:00:00+00:00", 100.0, 102.0, 99.0, 101.0, 5000.0)])
        self.assertEqual(stored, 1)
        row = self.con.execute("SELECT symbol,close,volume,source FROM candles").fetchone()
        self.assertEqual(row, ("RELIANCE", 101.0, 5000.0, "yahoo:5m"))

    def test_reruns_replace_rather_than_duplicate(self) -> None:
        bar = ("2026-07-28T04:00:00+00:00", 100.0, 102.0, 99.0, 101.0, 5000.0)
        bf.store(self.con, "RELIANCE", [bar])
        bf.store(self.con, "RELIANCE", [bar])
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM candles").fetchone()[0], 1)

    def test_daily_candles_are_not_disturbed(self) -> None:
        """Intraday and daily share the table; source is part of the key."""
        self.con.execute("INSERT INTO candles VALUES('RELIANCE','2026-07-27',1,2,0.5,1.5,"
                         "10,'upstox-live:day')")
        self.con.commit()
        bf.store(self.con, "RELIANCE",
                 [("2026-07-28T04:00:00+00:00", 100.0, 102.0, 99.0, 101.0, 5000.0)])
        counts = dict(self.con.execute("SELECT source, COUNT(*) FROM candles GROUP BY source"))
        self.assertEqual(counts, {"upstox-live:day": 1, "yahoo:5m": 1})

    def test_empty_bar_list_stores_nothing(self) -> None:
        self.assertEqual(bf.store(self.con, "RELIANCE", []), 0)

    def test_liquid_symbols_ranks_by_turnover(self) -> None:
        """Illiquid names are not tradeable intraday, so they should not
        consume requests."""
        from datetime import date, timedelta
        recent = (date.today() - timedelta(days=3)).isoformat()
        rows = [("BIG", 1000.0, 10000.0), ("SMALL", 10.0, 100.0), ("MID", 100.0, 1000.0)]
        for symbol, close, volume in rows:
            self.con.execute("INSERT INTO candles VALUES(?,?,?,?,?,?,?,'upstox-live:day')",
                             (symbol, recent, close, close, close, close, volume))
        self.con.commit()
        self.assertEqual(bf.liquid_symbols(self.con, 2), ["BIG", "MID"])

    def test_liquid_symbols_on_an_empty_table(self) -> None:
        self.assertEqual(bf.liquid_symbols(self.con, 10), [])


if __name__ == "__main__":
    unittest.main()
