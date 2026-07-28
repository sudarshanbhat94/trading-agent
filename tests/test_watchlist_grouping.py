"""Watchlist folders and tags.

Two things are easy to get wrong here and both are covered:

1. The migration. `v2_watch_user` already exists in the live book with rows in
   it, so new columns need an explicit ALTER, and the insert must name its
   columns — the original used a positional `VALUES(?,?,?)` that breaks the
   moment the table grows.
2. Normalisation. "Banks" and "banks" must be the same folder, or the UI shows
   two groups that look identical.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest

from app import v2_web
from app.v2_web import parse_folder, parse_tags


class TagParsingTest(unittest.TestCase):
    def test_empty_means_no_tags(self) -> None:
        for raw in (None, "", []):
            with self.subTest(raw=raw):
                self.assertEqual(parse_tags(raw), ([], None))

    def test_lower_cased_and_deduplicated(self) -> None:
        """'Banks' and 'banks' are one tag, not two identical-looking ones."""
        self.assertEqual(parse_tags(["Banks", "banks", "BANKS"]), (["banks"], None))

    def test_internal_whitespace_is_collapsed(self) -> None:
        self.assertEqual(parse_tags(["  public   sector  "]), (["public sector"], None))

    def test_a_bare_string_is_accepted(self) -> None:
        self.assertEqual(parse_tags("momentum"), (["momentum"], None))

    def test_blank_entries_are_dropped(self) -> None:
        self.assertEqual(parse_tags(["banks", "  ", ""]), (["banks"], None))

    def test_order_is_preserved(self) -> None:
        self.assertEqual(parse_tags(["zeta", "alpha"]), (["zeta", "alpha"], None))

    def test_too_many_tags_is_rejected(self) -> None:
        tags, error = parse_tags([f"tag{i}" for i in range(v2_web.MAX_TAGS + 1)])
        self.assertIsNone(tags)
        self.assertIn("too many", error)

    def test_exactly_the_limit_is_allowed(self) -> None:
        tags, error = parse_tags([f"tag{i}" for i in range(v2_web.MAX_TAGS)])
        self.assertIsNone(error)
        self.assertEqual(len(tags), v2_web.MAX_TAGS)

    def test_overlong_tag_is_rejected(self) -> None:
        tags, error = parse_tags(["x" * (v2_web.MAX_TAG_LENGTH + 1)])
        self.assertIsNone(tags)
        self.assertIn("too long", error)

    def test_non_list_is_rejected(self) -> None:
        tags, error = parse_tags({"a": 1})
        self.assertIsNone(tags)
        self.assertIn("list", error)


class FolderParsingTest(unittest.TestCase):
    def test_empty_is_the_default_group(self) -> None:
        self.assertEqual(parse_folder(None), ("", None))
        self.assertEqual(parse_folder(""), ("", None))

    def test_normalised_like_tags(self) -> None:
        self.assertEqual(parse_folder("  My   Watchlist "), ("my watchlist", None))

    def test_overlong_folder_is_rejected(self) -> None:
        folder, error = parse_folder("x" * (v2_web.MAX_TAG_LENGTH + 1))
        self.assertIsNone(folder)
        self.assertIn("too long", error)


class MigrationTest(unittest.TestCase):
    """The live book's watchlist predates folder and tags."""

    def setUp(self) -> None:
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        con = sqlite3.connect(self.path)
        # Exactly the pre-migration shape, with real rows in it.
        con.execute("CREATE TABLE v2_watch_user(symbol TEXT, market TEXT, added_at TEXT, "
                    "PRIMARY KEY(symbol,market))")
        con.execute("CREATE TABLE v2_alerts(id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, "
                    "market TEXT, kind TEXT, value REAL, created_at TEXT, triggered_at TEXT, "
                    "triggered_price REAL, active INTEGER DEFAULT 1)")
        for symbol in ("CGPOWER", "GRSE"):
            con.execute("INSERT INTO v2_watch_user VALUES (?,?,?)",
                        (symbol, "IN", "2026-07-01T00:00:00"))
        con.commit()
        self.con = con

    def tearDown(self) -> None:
        self.con.close()
        os.unlink(self.path)

    def _columns(self):
        return {r[1] for r in self.con.execute("PRAGMA table_info(v2_watch_user)")}

    def test_columns_are_added(self) -> None:
        self.assertNotIn("folder", self._columns())
        v2_web._uwl(self.con)
        self.assertIn("folder", self._columns())
        self.assertIn("tags", self._columns())

    def test_existing_rows_survive_ungrouped(self) -> None:
        v2_web._uwl(self.con)
        rows = self.con.execute(
            "SELECT symbol,COALESCE(folder,''),COALESCE(tags,'') FROM v2_watch_user "
            "ORDER BY symbol").fetchall()
        self.assertEqual([r[0] for r in rows], ["CGPOWER", "GRSE"])
        for _, folder, tags in rows:
            self.assertEqual(folder, "")
            self.assertEqual(tags, "")

    def test_migration_is_idempotent(self) -> None:
        v2_web._uwl(self.con)
        v2_web._uwl(self.con)
        self.assertIn("folder", self._columns())

    def test_named_column_insert_survives_a_wider_table(self) -> None:
        """The original insert used a positional VALUES(?,?,?), which would
        fail against the migrated five-column table."""
        v2_web._uwl(self.con)
        self.con.execute(
            "INSERT OR IGNORE INTO v2_watch_user(symbol,market,added_at,folder,tags) "
            "VALUES(?,?,?,?,?)", ("TCS", "IN", "2026-07-28T00:00:00", "core", '["it"]'))
        self.con.commit()
        row = self.con.execute(
            "SELECT folder,tags FROM v2_watch_user WHERE symbol='TCS'").fetchone()
        self.assertEqual(row[0], "core")
        self.assertEqual(row[1], '["it"]')

        with self.assertRaises(sqlite3.OperationalError):
            # Proof the old positional form is genuinely broken now.
            self.con.execute("INSERT OR IGNORE INTO v2_watch_user VALUES(?,?,?)",
                             ("INFY", "IN", "2026-07-28T00:00:00"))


if __name__ == "__main__":
    unittest.main()
