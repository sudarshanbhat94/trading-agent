"""Parsing and idempotency for the NSE reference datasets.

Three of these were believed to exist and did not. `delivery_data` had no
script, timer or service anywhere in the repo and had been frozen since
2026-06-17 with nothing reporting it; sector was a single label
("NSE Listed Equity") across 2,594 symbols, so the concentration cap could
never bind; FII/DII flows and bulk deals were absent entirely.

The tests that matter here are the boring ones. NSE writes '-' for delivery
where it does not apply, mixes SME and bond series into the same file, and
formats numbers with thousands separators — each of which silently corrupts a
column rather than raising. And because the original table had no unique key, a
re-run duplicated every row, which is why idempotency is asserted directly.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sqlite3
import sys
import tempfile
import unittest
from datetime import date

_path = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "nse_reference_ingest.py"
_spec = importlib.util.spec_from_file_location("nse_reference_ingest", _path)
ingest = importlib.util.module_from_spec(_spec)
sys.modules["nse_reference_ingest"] = ingest
_spec.loader.exec_module(ingest)

BHAV = """SYMBOL, SERIES, DATE1, PREV_CLOSE, OPEN_PRICE, HIGH_PRICE, LOW_PRICE, LAST_PRICE, CLOSE_PRICE, AVG_PRICE, TTL_TRD_QNTY, TURNOVER_LACS, NO_OF_TRADES, DELIV_QTY, DELIV_PER
 TCS, EQ, 28-Jul-2026, 204.00, 204.05, 206.99, 198.14, 201.10, 201.63, 202.18, 156781, 316.97, 3747, 66172, 42.21
 SOMEBOND, N1, 28-Jul-2026, 100.00, 100.00, 100.00, 100.00, 100.00, 100.00, 100.00, 10, 1.0, 2, 5, 50.00
 NODELIV, EQ, 28-Jul-2026, 50.00, 50.00, 50.00, 50.00, 50.00, 50.00, 50.00, 100, 1.0, 2, -, -
"""

FIIDII = [
    {"buyValue": "18,256.88", "category": "DII", "date": "28-Jul-2026",
     "netValue": "1664.16", "sellValue": "16592.72"},
    {"buyValue": "17530.97", "category": "FII/FPI", "date": "28-Jul-2026",
     "netValue": "-938.25", "sellValue": "18469.22"},
]

BULK = """Date,Symbol,Security Name,Client Name,Buy/Sell,Quantity Traded,Trade Price / Wght. Avg. Price,Remarks
28-JUL-2026,AASTHA,Aastha Spintex Limited,D3 STOCK VISION LLP,BUY,222230,83.00,-
"""

INDEX_CSV = """Company Name,Industry,Symbol,Series,ISIN Code
Tata Consultancy,Information Technology,TCS,EQ,INE467B01029
Reliance,Oil Gas & Consumable Fuels,RELIANCE,EQ,INE002A01018
"""


class FakeResponse:
    def __init__(self, text="", status=200, payload=None):
        self.text = text
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload


class FakeHttp:
    def __init__(self, mapping):
        self.mapping = mapping

    def get(self, url):
        for key, value in self.mapping.items():
            if key in url:
                return value
        return FakeResponse("", 404)


def memdb():
    con = sqlite3.connect(":memory:")
    ingest._schema(con)
    return con


class DeliveryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.con = memdb()
        self.http = FakeHttp({"sec_bhavdata_full": FakeResponse(BHAV)})

    def rows(self):
        return self.con.execute("SELECT symbol,date,close,total_volume,delivery_volume,"
                                "delivery_pct FROM delivery_data ORDER BY symbol").fetchall()

    def test_equity_rows_are_ingested(self) -> None:
        n = ingest.ingest_delivery(self.con, self.http, date(2026, 7, 28))
        self.assertEqual(n, 1)
        self.assertEqual(self.rows()[0][0], "TCS")

    def test_volume_and_delivery_columns_land_in_the_right_places(self) -> None:
        """Column alignment is the failure that would corrupt silently: the
        bhavcopy is unquoted, so any stray comma shifts every later field."""
        ingest.ingest_delivery(self.con, self.http, date(2026, 7, 28))
        _sym, _d, close, total, deliv, pct = self.rows()[0]
        self.assertEqual((close, total, deliv, pct), (201.63, 156781.0, 66172.0, 42.21))

    def test_non_equity_series_are_excluded(self) -> None:
        """Bonds and SME series share the file and would pollute the universe."""
        ingest.ingest_delivery(self.con, self.http, date(2026, 7, 28))
        self.assertNotIn("SOMEBOND", [r[0] for r in self.rows()])

    def test_rows_without_delivery_are_skipped(self) -> None:
        """NSE writes '-' where delivery does not apply. Storing that as 0 would
        read as 'nobody took delivery', the opposite of 'unknown'."""
        ingest.ingest_delivery(self.con, self.http, date(2026, 7, 28))
        self.assertNotIn("NODELIV", [r[0] for r in self.rows()])

    def test_a_rerun_does_not_duplicate(self) -> None:
        """The original table had no unique key, so re-running doubled it."""
        ingest.ingest_delivery(self.con, self.http, date(2026, 7, 28))
        ingest.ingest_delivery(self.con, self.http, date(2026, 7, 28))
        self.assertEqual(len(self.rows()), 1)

    def test_a_missing_file_is_not_an_error(self) -> None:
        """Weekends and holidays have no bhavcopy; that is normal, not a fault."""
        http = FakeHttp({"sec_bhavdata_full": FakeResponse("", 404)})
        self.assertEqual(ingest.ingest_delivery(self.con, http, date(2026, 7, 26)), 0)


class FlowsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.con = memdb()
        self.http = FakeHttp({"fiidiiTradeReact": FakeResponse(payload=FIIDII)})

    def test_both_categories_are_stored(self) -> None:
        self.assertEqual(ingest.ingest_fii_dii(self.con, self.http), 2)
        cats = [r[0] for r in self.con.execute("SELECT category FROM fii_dii_flows ORDER BY category")]
        self.assertEqual(cats, ["DII", "FII/FPI"])

    def test_negative_net_is_preserved(self) -> None:
        """An FII outflow is the informative case; losing the sign inverts it."""
        ingest.ingest_fii_dii(self.con, self.http)
        net = self.con.execute("SELECT net_value FROM fii_dii_flows WHERE category='FII/FPI'").fetchone()[0]
        self.assertLess(net, 0)

    def test_dates_are_normalised_to_iso(self) -> None:
        ingest.ingest_fii_dii(self.con, self.http)
        self.assertEqual(self.con.execute("SELECT date FROM fii_dii_flows LIMIT 1").fetchone()[0],
                         "2026-07-28")

    def test_a_rerun_does_not_duplicate(self) -> None:
        ingest.ingest_fii_dii(self.con, self.http)
        ingest.ingest_fii_dii(self.con, self.http)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM fii_dii_flows").fetchone()[0], 2)


class BulkDealsTest(unittest.TestCase):
    def test_parsed_and_normalised(self) -> None:
        con = memdb()
        http = FakeHttp({"bulk.csv": FakeResponse(BULK)})
        self.assertEqual(ingest.ingest_bulk_deals(con, http), 1)
        row = con.execute("SELECT date,symbol,side,quantity FROM bulk_deals").fetchone()
        self.assertEqual(row[:3], ("2026-07-28", "AASTHA", "BUY"))
        self.assertEqual(row[3], 222230.0)


class SectorTest(unittest.TestCase):
    def test_real_industries_replace_the_single_label(self) -> None:
        con = memdb()
        con.execute("CREATE TABLE universe(symbol TEXT, exchange TEXT, sector TEXT)")
        con.executemany("INSERT INTO universe VALUES(?,?,?)",
                        [("TCS", "NSE", "NSE Listed Equity"),
                         ("RELIANCE", "NSE", "NSE Listed Equity"),
                         ("AAPL", "US", "Technology")])
        http = FakeHttp({"ind_nifty": FakeResponse(INDEX_CSV)})
        ingest.ingest_sectors(con, http)
        got = dict(con.execute("SELECT symbol, sector FROM universe"))
        self.assertEqual(got["TCS"], "Information Technology")
        self.assertEqual(got["RELIANCE"], "Oil Gas & Consumable Fuels")

    def test_us_rows_are_untouched(self) -> None:
        con = memdb()
        con.execute("CREATE TABLE universe(symbol TEXT, exchange TEXT, sector TEXT)")
        con.execute("INSERT INTO universe VALUES('TCS','US','Technology')")
        http = FakeHttp({"ind_nifty": FakeResponse(INDEX_CSV)})
        ingest.ingest_sectors(con, http)
        self.assertEqual(con.execute("SELECT sector FROM universe").fetchone()[0], "Technology")


class NumberParsingTest(unittest.TestCase):
    def test_handles_the_shapes_nse_actually_sends(self) -> None:
        self.assertEqual(ingest._num("1,56,781"), 156781.0)
        self.assertEqual(ingest._num(" 42.21 "), 42.21)
        self.assertEqual(ingest._num("-"), None)
        self.assertEqual(ingest._num(""), None)
        self.assertEqual(ingest._num(None, 0.0), 0.0)


if __name__ == "__main__":
    unittest.main()
