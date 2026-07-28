"""NSE shareholding ingestion.

Fixtures use the real payload shape, copied from a live NSE response: the
percentages arrive as STRINGS under `pr_and_prgrp` and `public_val`, and the
quarter date as "30-JUN-2026".

The parser is deliberately strict — a holding outside 0-100 is corrupt rather
than a datum, and a record with neither promoter nor public figure is not worth
storing. Silently keeping junk here would poison any signal built on the trend.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import shareholding_ingest as si  # noqa: E402


# Verbatim shape from the live API.
LIVE_RECORD = {
    "broadcastDate": "25-JUL-2026 10:08:23", "date": "30-JUN-2026",
    "employeeTrusts": "0", "isin": "INE071D01033", "name": "Menon Bearings Limited",
    "pr_and_prgrp": "68.44", "public_val": "31.56", "recordId": "212982",
    "submissionDate": "25-JUL-2026", "symbol": "MENONBE",
}


class PercentageTest(unittest.TestCase):
    def test_string_percentages(self) -> None:
        self.assertEqual(si._pct("68.44"), 68.44)
        self.assertEqual(si._pct("0"), 0.0)
        self.assertEqual(si._pct("100"), 100.0)

    def test_commas_are_stripped(self) -> None:
        self.assertEqual(si._pct(" 1,0.5 "), 10.5)

    def test_blank_and_placeholder(self) -> None:
        for value in (None, "", "-"):
            with self.subTest(value=value):
                self.assertIsNone(si._pct(value))

    def test_out_of_range_is_rejected(self) -> None:
        """A 'holding' above 100% or below zero is corrupt data, not a value."""
        self.assertIsNone(si._pct("101"))
        self.assertIsNone(si._pct("-5"))

    def test_garbage_is_rejected(self) -> None:
        self.assertIsNone(si._pct("n/a"))
        self.assertIsNone(si._pct(object()))


class DateTest(unittest.TestCase):
    def test_nse_quarter_format(self) -> None:
        self.assertEqual(si._as_of("30-JUN-2026"), "2026-06-30")
        self.assertEqual(si._as_of("31-MAR-2026"), "2026-03-31")

    def test_lower_case_and_whitespace(self) -> None:
        self.assertEqual(si._as_of(" 30-jun-2026 "), "2026-06-30")

    def test_unparseable(self) -> None:
        for value in ("2026-06-30", "", None, "notadate"):
            with self.subTest(value=value):
                self.assertIsNone(si._as_of(value))


class ParseRecordTest(unittest.TestCase):
    def test_parses_the_live_shape(self) -> None:
        record = si.parse_record(LIVE_RECORD)
        self.assertEqual(record["symbol"], "MENONBE")
        self.assertEqual(record["as_of"], "2026-06-30")
        self.assertEqual(record["promoter_pct"], 68.44)
        self.assertEqual(record["public_pct"], 31.56)
        self.assertEqual(record["employee_trust_pct"], 0.0)

    def test_promoter_and_public_are_complementary(self) -> None:
        """Sanity on the real figures: the two sides should account for the
        whole register, give or take employee trusts."""
        record = si.parse_record(LIVE_RECORD)
        self.assertAlmostEqual(record["promoter_pct"] + record["public_pct"], 100.0, places=1)

    def test_symbol_is_upper_cased(self) -> None:
        self.assertEqual(si.parse_record(dict(LIVE_RECORD, symbol="menonbe"))["symbol"], "MENONBE")

    def test_missing_symbol_or_date_is_dropped(self) -> None:
        self.assertIsNone(si.parse_record(dict(LIVE_RECORD, symbol="")))
        self.assertIsNone(si.parse_record(dict(LIVE_RECORD, date="")))

    def test_record_with_no_holdings_is_dropped(self) -> None:
        self.assertIsNone(si.parse_record(dict(LIVE_RECORD, pr_and_prgrp="", public_val="")))

    def test_one_sided_record_is_kept(self) -> None:
        """A company with no promoter holding is legitimate — many aren't
        promoter-led — so public alone is still worth storing."""
        record = si.parse_record(dict(LIVE_RECORD, pr_and_prgrp=""))
        self.assertIsNotNone(record)
        self.assertIsNone(record["promoter_pct"])
        self.assertEqual(record["public_pct"], 31.56)

    def test_non_dict_input(self) -> None:
        for value in (None, "row", 5, []):
            with self.subTest(value=value):
                self.assertIsNone(si.parse_record(value))


class StorageTest(unittest.TestCase):
    def setUp(self) -> None:
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.con = sqlite3.connect(self.path)
        si.ensure_schema(self.con)

    def tearDown(self) -> None:
        self.con.close()
        os.unlink(self.path)

    def _insert(self, record, ingested="2026-07-28T00:00:00Z"):
        self.con.execute(
            "INSERT OR REPLACE INTO shareholding(symbol,as_of,promoter_pct,public_pct,"
            "employee_trust_pct,company,submitted_at,ingested_at) VALUES(?,?,?,?,?,?,?,?)",
            (record["symbol"], record["as_of"], record["promoter_pct"], record["public_pct"],
             record["employee_trust_pct"], record["company"], record["submitted_at"], ingested))
        self.con.commit()

    def test_schema_is_idempotent(self) -> None:
        si.ensure_schema(self.con)
        columns = {r[1] for r in self.con.execute("PRAGMA table_info(shareholding)")}
        self.assertIn("promoter_pct", columns)
        self.assertIn("as_of", columns)

    def test_quarters_accumulate_rather_than_overwrite(self) -> None:
        """History is the point — a promoter stake falling across quarters is
        only visible if the old rows survive."""
        self._insert(si.parse_record(dict(LIVE_RECORD, date="31-MAR-2026", pr_and_prgrp="70.00")))
        self._insert(si.parse_record(LIVE_RECORD))
        rows = self.con.execute(
            "SELECT as_of,promoter_pct FROM shareholding WHERE symbol='MENONBE' ORDER BY as_of"
        ).fetchall()
        self.assertEqual(rows, [("2026-03-31", 70.0), ("2026-06-30", 68.44)])

    def test_a_revision_replaces_the_same_quarter(self) -> None:
        """NSE republishes corrected filings; the newer one must win rather
        than colliding on the primary key."""
        self._insert(si.parse_record(LIVE_RECORD))
        self._insert(si.parse_record(dict(LIVE_RECORD, pr_and_prgrp="69.10")))
        rows = self.con.execute(
            "SELECT promoter_pct FROM shareholding WHERE symbol='MENONBE'").fetchall()
        self.assertEqual(rows, [(69.1,)])

    def test_trend_query_works(self) -> None:
        for date, promoter in (("31-DEC-2025", "72.00"), ("31-MAR-2026", "70.00"),
                               ("30-JUN-2026", "68.44")):
            self._insert(si.parse_record(dict(LIVE_RECORD, date=date, pr_and_prgrp=promoter)))
        trend = [r[0] for r in self.con.execute(
            "SELECT promoter_pct FROM shareholding WHERE symbol='MENONBE' ORDER BY as_of")]
        self.assertEqual(trend, [72.0, 70.0, 68.44])
        self.assertLess(trend[-1], trend[0])       # a falling promoter stake is visible


if __name__ == "__main__":
    unittest.main()
