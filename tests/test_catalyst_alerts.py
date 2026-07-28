"""Catalyst alerts: fire on the next material NSE filing for a symbol.

Unlike the price kinds this has no threshold — it watches the corporate-filing
feed and fires once something material is published *after* the alert was set.
The "after" is the part worth pinning: an alert that fired on filings already
in the table would trigger the instant it was created.

Materiality is deliberately the same vocabulary the engine's catalyst gate
uses, so an alert cannot fire on something the engine calls noise.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from app import v2_web


class CatalystSinceTest(unittest.TestCase):
    def setUp(self) -> None:
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        con = sqlite3.connect(self.path)
        con.execute("CREATE TABLE nse_announcements (symbol TEXT, an_epoch INTEGER, "
                    "an_dt TEXT, category TEXT, subject TEXT, text TEXT, ingested_at TEXT)")
        con.commit()
        con.close()
        self._original = v2_web.CATALYST_DB
        v2_web.CATALYST_DB = self.path
        self.now = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)

    def tearDown(self) -> None:
        v2_web.CATALYST_DB = self._original
        os.unlink(self.path)

    def _add(self, symbol, when, category="results", subject="Financial Results"):
        con = sqlite3.connect(self.path)
        con.execute("INSERT INTO nse_announcements VALUES (?,?,?,?,?,?,?)",
                    (symbol, int(when.timestamp()), when.isoformat(), category,
                     subject, "", ""))
        con.commit()
        con.close()

    def _since(self, when):
        return when.isoformat()

    def test_fires_on_a_filing_published_after_the_alert(self) -> None:
        created = self.now
        self._add("KFINTECH", created + timedelta(hours=1))
        found = v2_web.catalyst_since("KFINTECH", self._since(created))
        self.assertIsNotNone(found)
        self.assertEqual(found[0], "results")

    def test_ignores_filings_that_predate_the_alert(self) -> None:
        """Otherwise every catalyst alert would fire the moment it was set."""
        created = self.now
        self._add("KFINTECH", created - timedelta(hours=1))
        self.assertIsNone(v2_web.catalyst_since("KFINTECH", self._since(created)))

    def test_noise_filings_do_not_count(self) -> None:
        """Same materiality vocabulary as the engine's gate — an ESOP allotment
        classified 'noise' must not fire an alert."""
        created = self.now
        self._add("CARTRADE", created + timedelta(hours=1), category="noise",
                  subject="Updates")
        self.assertIsNone(v2_web.catalyst_since("CARTRADE", self._since(created)))

    def test_each_material_category_fires(self) -> None:
        # Symbols are stored upper-case by the ingester, so the fixture must be
        # too — the lookup upper-cases its argument, not the stored column.
        for category in v2_web.MATERIAL_CATEGORIES:
            with self.subTest(category=category):
                symbol = f"SYM{category}".upper()
                self._add(symbol, self.now + timedelta(hours=1), category=category)
                self.assertIsNotNone(
                    v2_web.catalyst_since(symbol, self._since(self.now)))

    def test_returns_the_newest_filing(self) -> None:
        created = self.now
        self._add("ABC", created + timedelta(hours=1), subject="older")
        self._add("ABC", created + timedelta(hours=5), subject="newer")
        self.assertEqual(v2_web.catalyst_since("ABC", self._since(created))[1], "newer")

    def test_other_symbols_are_not_matched(self) -> None:
        self._add("ABC", self.now + timedelta(hours=1))
        self.assertIsNone(v2_web.catalyst_since("XYZ", self._since(self.now)))

    def test_symbol_is_upper_cased(self) -> None:
        self._add("ABC", self.now + timedelta(hours=1))
        self.assertIsNotNone(v2_web.catalyst_since("abc", self._since(self.now)))

    def test_unparseable_timestamp_returns_none(self) -> None:
        self._add("ABC", self.now + timedelta(hours=1))
        for bad in ("", "not-a-date", None):
            with self.subTest(value=bad):
                self.assertIsNone(v2_web.catalyst_since("ABC", bad))

    def test_missing_database_is_not_an_exception(self) -> None:
        """The alert loop must never be broken by the catalyst feed being
        absent — a missing file means 'no catalyst', not a crash."""
        v2_web.CATALYST_DB = "/nonexistent/path/catalysts.db"
        self.assertIsNone(v2_web.catalyst_since("ABC", self._since(self.now)))


class CatalystKindTest(unittest.TestCase):
    def test_price_rules_ignore_the_catalyst_kind(self) -> None:
        """alert_hit handles price comparisons only; catalyst is evaluated
        separately, so it must not accidentally satisfy a price branch."""
        self.assertFalse(v2_web.alert_hit("catalyst", 0.0, 100.0))

    def test_material_categories_match_the_engine(self) -> None:
        from app import v2_live
        import inspect
        source = inspect.getsource(v2_live._nse_catalyst_symbols)
        for category in v2_web.MATERIAL_CATEGORIES:
            self.assertIn(category, source,
                          f"{category} is not in the engine's catalyst gate")


if __name__ == "__main__":
    unittest.main()
