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


class HeldContractLookupTest(unittest.TestCase):
    """A position must stay priced after the ATM window moves past it.

    _nfo_held filtered the WATCH LIST by held symbols, so a contract that
    drifted out of the money simply stopped being quoted. On 2026-08-03
    BANKNIFTY26AUG57500CE had not been priced since 07-31 — the book kept
    marking it at a three-day-old price and showing 0%, and every exit rule was
    being evaluated against that stale number.
    """

    ROWS = [
        {"tradingsymbol": "BANKNIFTY26AUG57500CE", "instrument_key": "NSE_FO|1",
         "expiry": "2026-08-26", "strike": "57500", "option_type": "CE",
         "lot_size": "30", "name": "BANKNIFTY"},
        {"tradingsymbol": "BANKNIFTY26AUG57400PE", "instrument_key": "NSE_FO|2",
         "expiry": "2026-08-26", "strike": "57400", "option_type": "PE",
         "lot_size": "30", "name": "BANKNIFTY"},
    ]

    def test_a_contract_is_found_by_name(self) -> None:
        from app import nfo_contracts
        out = nfo_contracts.by_symbols(["BANKNIFTY26AUG57500CE"], rows=self.ROWS)
        self.assertEqual(len(out), 1)
        row = out[0]
        self.assertEqual(row["symbol"], "BANKNIFTY26AUG57500CE")
        self.assertEqual(row["upstox_instrument_key"], "NSE_FO|1")
        self.assertEqual(row["lot_size"], 30.0)
        self.assertEqual(row["underlying"], "BANKNIFTY")
        self.assertEqual(row["exchange"], "NSE")   # routes to the India provider

    def test_lookup_is_case_insensitive_and_trims(self) -> None:
        from app import nfo_contracts
        self.assertEqual(len(nfo_contracts.by_symbols([" banknifty26aug57500ce "],
                                                      rows=self.ROWS)), 1)

    def test_unknown_symbols_are_skipped_not_fatal(self) -> None:
        from app import nfo_contracts
        out = nfo_contracts.by_symbols(["NOPE", "BANKNIFTY26AUG57500CE"], rows=self.ROWS)
        self.assertEqual([r["symbol"] for r in out], ["BANKNIFTY26AUG57500CE"])

    def test_empty_input_returns_nothing(self) -> None:
        from app import nfo_contracts
        for empty in ([], None, ["", "  "]):
            self.assertEqual(nfo_contracts.by_symbols(empty, rows=self.ROWS), [])

    def test_a_row_without_an_instrument_key_is_unusable(self) -> None:
        from app import nfo_contracts
        rows = [dict(self.ROWS[0], instrument_key="")]
        self.assertEqual(nfo_contracts.by_symbols(["BANKNIFTY26AUG57500CE"], rows=rows), [])

    def test_the_feed_falls_back_to_the_lookup(self) -> None:
        """The fix itself: held symbols missing from the watch list must be
        looked up, not dropped."""
        import pathlib
        src = (pathlib.Path(__file__).resolve().parent.parent
               / "scripts" / "v2_quote_feed.py").read_text(encoding="utf-8")
        self.assertIn("nfo_contracts.by_symbols(missing)", src)
        self.assertNotIn('return [c for c in contracts if c["symbol"].upper() in held]', src)
