"""Daily candle freshness targeting.

The freshness target decides whether the nightly ingest does any work at all.
It has been wrong twice: first a rolling calendar window that always looked
satisfied, then a target read from the candle table itself, which deadlocked —
once the table sat at day D every symbol matched D, nothing was fetched, and
the table stayed at D. Observed live on 2026-07-23 (both runs fetched nothing)
and again when Friday's bar did not land until Monday afternoon.

These tests pin the property that matters: when the stored data is behind the
last closed session, the run must have work to do.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import candle_ingest as ci  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))


def _ist(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=IST)


class ExpectedSessionTest(unittest.TestCase):
    def test_after_close_on_a_weekday_is_today(self) -> None:
        # Monday 2026-07-27, 16:00 IST — after the 15:30 close.
        self.assertEqual(ci.expected_session(_ist(2026, 7, 27, 16, 0)), "2026-07-27")

    def test_before_close_on_a_weekday_is_the_previous_session(self) -> None:
        # Monday 09:00 IST — Monday has not closed, so Friday is the target.
        self.assertEqual(ci.expected_session(_ist(2026, 7, 27, 9, 0)), "2026-07-24")

    def test_exactly_at_the_close_counts_as_closed(self) -> None:
        self.assertEqual(ci.expected_session(_ist(2026, 7, 27, 15, 30)), "2026-07-27")

    def test_one_minute_before_close_does_not(self) -> None:
        self.assertEqual(ci.expected_session(_ist(2026, 7, 27, 15, 29)), "2026-07-24")

    def test_weekend_walks_back_to_friday(self) -> None:
        self.assertEqual(ci.expected_session(_ist(2026, 7, 25, 20, 0)), "2026-07-24")  # Sat
        self.assertEqual(ci.expected_session(_ist(2026, 7, 26, 20, 0)), "2026-07-24")  # Sun

    def test_monday_before_close_skips_the_weekend(self) -> None:
        self.assertEqual(ci.expected_session(_ist(2026, 7, 27, 6, 0)), "2026-07-24")

    def test_the_0200_ist_run_targets_the_previous_day(self) -> None:
        """The 20:30 UTC timer fires at 02:00 IST the next calendar day. It must
        target the session that just closed, not the not-yet-open one."""
        run = datetime(2026, 7, 27, 20, 30, tzinfo=timezone.utc)   # 02:00 IST Tue
        self.assertEqual(ci.expected_session(run), "2026-07-27")

    def test_accepts_utc_input(self) -> None:
        # 10:30 UTC Monday = 16:00 IST Monday, after the close.
        run = datetime(2026, 7, 27, 10, 30, tzinfo=timezone.utc)
        self.assertEqual(ci.expected_session(run), "2026-07-27")


class _FakeDB:
    def __init__(self, path: str) -> None:
        self.path = path


class FreshSymbolsTest(unittest.TestCase):
    def setUp(self) -> None:
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        con = sqlite3.connect(self.path)
        con.execute("CREATE TABLE candles (symbol TEXT, ts TEXT, source TEXT)")
        con.commit()
        con.close()
        self.db = _FakeDB(self.path)

    def tearDown(self) -> None:
        os.unlink(self.path)

    def _add(self, symbol: str, day: str, source: str = "upstox-live:day") -> None:
        con = sqlite3.connect(self.path)
        con.execute("INSERT INTO candles VALUES (?,?,?)", (symbol, f"{day}T00:00:00+05:30", source))
        con.commit()
        con.close()

    def test_symbol_at_target_is_fresh(self) -> None:
        self._add("RELIANCE", "2026-07-27")
        self.assertEqual(ci._fresh_symbols(self.db, "IN", "2026-07-27"), {"RELIANCE"})

    def test_symbol_behind_target_is_stale(self) -> None:
        self._add("RELIANCE", "2026-07-24")
        self.assertEqual(ci._fresh_symbols(self.db, "IN", "2026-07-27"), set())

    def test_no_deadlock_when_whole_table_is_a_session_behind(self) -> None:
        """The regression. Under the old self-referential target every symbol
        counted as fresh and the run fetched nothing, forever."""
        for i in range(50):
            self._add(f"SYM{i}", "2026-07-24")
        stale_target = ci._fresh_symbols(self.db, "IN", "2026-07-27")
        self.assertEqual(stale_target, set(), "a table one session behind must have work to do")

    def test_other_sources_are_ignored(self) -> None:
        self._add("RELIANCE", "2026-07-27", source="yahoo:day")
        self.assertEqual(ci._fresh_symbols(self.db, "IN", "2026-07-27"), set())

    def test_symbols_are_uppercased(self) -> None:
        self._add("reliance", "2026-07-27")
        self.assertIn("RELIANCE", ci._fresh_symbols(self.db, "IN", "2026-07-27"))

    def test_missing_table_fails_towards_refetching(self) -> None:
        """A broken freshness check must make the run do MORE work, not less."""
        broken = _FakeDB("/nonexistent/path/to.db")
        self.assertEqual(ci._fresh_symbols(broken, "IN", "2026-07-27"), set())

    def test_defaults_to_the_calendar_target(self) -> None:
        self._add("RELIANCE", "1999-01-01")
        self.assertEqual(ci._fresh_symbols(self.db, "IN"), set())

    def test_source_max_ts_reports_the_newest_day(self) -> None:
        self._add("A", "2026-07-22")
        self._add("B", "2026-07-24")
        self.assertEqual(ci._source_max_ts(self.db, "IN"), "2026-07-24")

    def test_source_max_ts_on_empty_table(self) -> None:
        self.assertIsNone(ci._source_max_ts(self.db, "IN"))


if __name__ == "__main__":
    unittest.main()
