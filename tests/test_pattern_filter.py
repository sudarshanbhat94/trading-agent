"""Pattern-alert filtering.

Measured on 300 real symbols in one session, 32% showed *some* candlestick
pattern — 11% a doji alone. An alert that fires on any pattern is therefore
noisy enough to be ignored, which is worse than not having it. This lets an
alert name the patterns it cares about.

An unknown pattern name is REJECTED rather than dropped: a typo that silently
matches nothing looks exactly like a broken alert.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest

from app import indicators as ta
from app import v2_web
from app.v2_web import parse_pattern_filter, pattern_hit


def _bar(date, o, h, l, c):
    return (date, float(o), float(h), float(l), float(c))


def _flat(n, price=100.0):
    return [_bar(f"2026-07-{i + 1:02d}", price, price + 1.2, price - 1.2, price + 0.8)
            for i in range(n)]


BULLISH_ENGULFING = [_bar("2026-07-20", 105.0, 105.5, 99.5, 100.0),
                     _bar("2026-07-21", 99.0, 106.5, 98.5, 106.0)]


class VocabularyTest(unittest.TestCase):
    def test_vocabulary_lives_with_the_detector(self) -> None:
        self.assertIs(v2_web.KNOWN_PATTERNS, ta.CANDLESTICK_PATTERNS)

    def test_every_ui_label_has_a_canonical_pattern(self) -> None:
        """The label map and the detector must not drift apart."""
        self.assertEqual(set(v2_web._PATTERN_LABELS), set(ta.CANDLESTICK_PATTERNS))

    def test_detector_only_emits_known_names(self) -> None:
        """Run the detector over shapes that trigger several patterns and
        confirm nothing outside the declared vocabulary comes back."""
        cases = [
            ([100.0], [105.0], [95.0], [100.2]),                      # doji
            ([100.0], [101.2], [95.0], [101.0]),                      # hammer
            ([100.0], [110.0], [100.0], [110.0]),                     # marubozu
            ([105.0, 99.0], [105.5, 106.5], [99.5, 98.5], [100.0, 106.0]),
        ]
        for opens, highs, lows, closes in cases:
            for name in ta.candlestick_patterns(opens, highs, lows, closes):
                self.assertIn(name, ta.CANDLESTICK_PATTERNS)


class ParseFilterTest(unittest.TestCase):
    def test_empty_means_any_pattern(self) -> None:
        for raw in (None, "", []):
            with self.subTest(raw=raw):
                self.assertEqual(parse_pattern_filter(raw), ([], None))

    def test_valid_names_are_kept(self) -> None:
        wanted, error = parse_pattern_filter(["bullish_engulfing", "hammer"])
        self.assertIsNone(error)
        self.assertEqual(wanted, ["bullish_engulfing", "hammer"])

    def test_a_bare_string_is_accepted(self) -> None:
        self.assertEqual(parse_pattern_filter("doji"), (["doji"], None))

    def test_case_and_whitespace_are_normalised(self) -> None:
        self.assertEqual(parse_pattern_filter([" Doji "]), (["doji"], None))

    def test_duplicates_are_collapsed(self) -> None:
        self.assertEqual(parse_pattern_filter(["doji", "doji"]), (["doji"], None))

    def test_unknown_name_is_rejected_not_dropped(self) -> None:
        wanted, error = parse_pattern_filter(["bullish_engulfing", "moon_star"])
        self.assertIsNone(wanted)
        self.assertIn("moon_star", error)

    def test_non_list_is_rejected(self) -> None:
        wanted, error = parse_pattern_filter({"a": 1})
        self.assertIsNone(wanted)
        self.assertIn("list", error)


class FilteredHitTest(unittest.TestCase):
    def _bars(self):
        return _flat(5) + BULLISH_ENGULFING

    def test_no_filter_fires_on_anything(self) -> None:
        self.assertIn("bullish_engulfing", pattern_hit(self._bars(), "2026-07-20"))

    def test_matching_filter_fires(self) -> None:
        found = pattern_hit(self._bars(), "2026-07-20", ["bullish_engulfing"])
        self.assertEqual(found, ["bullish_engulfing"])

    def test_non_matching_filter_stays_quiet(self) -> None:
        """The noise fix: a doji-only watcher must not be woken by an
        engulfing bar."""
        self.assertEqual(pattern_hit(self._bars(), "2026-07-20", ["doji"]), [])

    def test_filter_narrows_a_multi_pattern_bar(self) -> None:
        bars = self._bars()
        everything = pattern_hit(bars, "2026-07-20")
        if len(everything) < 2:
            self.skipTest("fixture bar matched only one pattern")
        narrowed = pattern_hit(bars, "2026-07-20", [everything[0]])
        self.assertEqual(narrowed, [everything[0]])

    def test_filter_does_not_bypass_the_freshness_rule(self) -> None:
        """A matching pattern on an OLD bar still must not fire."""
        self.assertEqual(
            pattern_hit(self._bars(), "2026-07-21", ["bullish_engulfing"]), [])


class MigrationTest(unittest.TestCase):
    """The live book predates the params column."""

    def setUp(self) -> None:
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        con = sqlite3.connect(self.path)
        # Exactly the pre-migration shape, with a row in it.
        con.execute("CREATE TABLE v2_alerts(id INTEGER PRIMARY KEY AUTOINCREMENT, "
                    "symbol TEXT, market TEXT, kind TEXT, value REAL, created_at TEXT, "
                    "triggered_at TEXT, triggered_price REAL, active INTEGER DEFAULT 1)")
        con.execute("INSERT INTO v2_alerts(symbol,market,kind,value,created_at,active) "
                    "VALUES ('RELIANCE','IN','above',1500.0,'2026-07-01T00:00:00',1)")
        con.commit()
        self.con = con

    def tearDown(self) -> None:
        self.con.close()
        os.unlink(self.path)

    def _columns(self):
        return {r[1] for r in self.con.execute("PRAGMA table_info(v2_alerts)")}

    def test_params_column_is_added_to_an_existing_table(self) -> None:
        self.assertNotIn("params", self._columns())
        v2_web._uwl(self.con)
        self.assertIn("params", self._columns())

    def test_existing_rows_survive_the_migration(self) -> None:
        v2_web._uwl(self.con)
        row = self.con.execute("SELECT symbol,kind,value,params FROM v2_alerts").fetchone()
        self.assertEqual(row[0], "RELIANCE")
        self.assertEqual(row[1], "above")
        self.assertEqual(row[2], 1500.0)
        self.assertIn(row[3], ("", None))     # defaults to "any pattern"

    def test_migration_is_idempotent(self) -> None:
        v2_web._uwl(self.con)
        v2_web._uwl(self.con)                 # must not raise "duplicate column"
        self.assertIn("params", self._columns())


if __name__ == "__main__":
    unittest.main()
