"""Index F&O bhavcopy parsing.

Two traps in this file. NSE leaves OptnTp BLANK on futures rather than marking
them, so the instrument type has to be derived — read naively, every future
looks like an option with no type. And the primary key must include strike and
option type, or a call and a put on the same strike overwrite each other and
half the chain silently disappears.
"""

from __future__ import annotations

import csv
import importlib.util
import io
import pathlib
import sqlite3
import sys
import unittest
import zipfile
from datetime import date

_path = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "fo_ingest.py"
_spec = importlib.util.spec_from_file_location("fo_ingest", _path)
fo = importlib.util.module_from_spec(_spec)
sys.modules["fo_ingest"] = fo
_spec.loader.exec_module(fo)

COLS = ["TradDt", "TckrSymb", "XpryDt", "StrkPric", "OptnTp", "OpnPric", "HghPric",
        "LwPric", "ClsPric", "SttlmPric", "UndrlygPric", "OpnIntrst",
        "ChngInOpnIntrst", "TtlTradgVol", "TtlTrfVal", "NewBrdLotQty"]


def zipped(rows):
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=COLS)
    writer.writeheader()
    for row in rows:
        writer.writerow({c: row.get(c, "") for c in COLS})
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("BhavCopy_NSE_FO.csv", buf.getvalue())
    return out.getvalue()


FUT = {"TradDt": "2026-07-28", "TckrSymb": "BANKNIFTY", "XpryDt": "2026-08-25",
       "OptnTp": "", "ClsPric": "57009.00", "OpnIntrst": "2163360",
       "TtlTradgVol": "29240", "NewBrdLotQty": "30", "UndrlygPric": "56756"}
CE = {"TradDt": "2026-07-28", "TckrSymb": "BANKNIFTY", "XpryDt": "2026-08-25",
      "StrkPric": "58000.00", "OptnTp": "CE", "ClsPric": "461.60",
      "OpnIntrst": "1796490", "TtlTradgVol": "46639", "NewBrdLotQty": "30"}
PE = dict(CE, OptnTp="PE", ClsPric="812.30")
STOCK = dict(CE, TckrSymb="RELIANCE")


class ParseTest(unittest.TestCase):
    def test_futures_are_identified_despite_a_blank_option_type(self) -> None:
        rows = fo.parse(zipped([FUT]))
        self.assertEqual(rows[0][3], "FUT")
        self.assertIsNone(rows[0][4])       # no strike on a future
        self.assertIsNone(rows[0][5])

    def test_options_keep_strike_and_type(self) -> None:
        rows = fo.parse(zipped([CE]))
        self.assertEqual(rows[0][3], "OPT")
        self.assertEqual(rows[0][4], 58000.0)
        self.assertEqual(rows[0][5], "CE")

    def test_non_index_symbols_are_excluded(self) -> None:
        """Stock derivatives are 90% of the file and are not what this trades."""
        self.assertEqual(fo.parse(zipped([STOCK])), [])

    def test_lot_size_is_captured(self) -> None:
        """Position sizing is impossible without it — one lot IS the minimum."""
        self.assertEqual(fo.parse(zipped([FUT]))[0][-1], 30.0)

    def test_a_corrupt_archive_returns_nothing(self) -> None:
        self.assertEqual(fo.parse(b"not a zip"), [])

    def test_rows_without_dates_are_skipped(self) -> None:
        self.assertEqual(fo.parse(zipped([dict(CE, XpryDt="")])), [])


class StorageTest(unittest.TestCase):
    def setUp(self) -> None:
        self.con = sqlite3.connect(":memory:")
        fo.schema(self.con)

    def insert(self, rows):
        self.con.executemany(
            "INSERT OR REPLACE INTO fo_bhav(date,symbol,expiry,instrument,strike,opt_type,"
            "open,high,low,close,settle,underlying,oi,oi_change,volume,value,lot_size)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        self.con.commit()

    def test_a_call_and_put_on_one_strike_both_survive(self) -> None:
        """If opt_type were left out of the key, one would overwrite the other
        and half the chain would vanish without any error."""
        self.insert(fo.parse(zipped([CE, PE])))
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM fo_bhav").fetchone()[0], 2)

    def test_reingesting_a_session_does_not_duplicate(self) -> None:
        rows = fo.parse(zipped([FUT, CE, PE]))
        self.insert(rows)
        self.insert(rows)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM fo_bhav").fetchone()[0], 3)

    def test_futures_and_options_coexist_on_the_same_expiry(self) -> None:
        self.insert(fo.parse(zipped([FUT, CE])))
        kinds = [r[0] for r in self.con.execute(
            "SELECT instrument FROM fo_bhav ORDER BY instrument")]
        self.assertEqual(kinds, ["FUT", "OPT"])


class NumberTest(unittest.TestCase):
    def test_blank_and_dash_become_none(self) -> None:
        self.assertIsNone(fo._num(""))
        self.assertIsNone(fo._num("-"))
        self.assertEqual(fo._num("-", 0.0), 0.0)
        self.assertEqual(fo._num("1,234.5"), 1234.5)


if __name__ == "__main__":
    unittest.main()
