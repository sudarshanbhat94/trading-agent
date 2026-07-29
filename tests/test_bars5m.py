"""5-minute bar recording, and the janitor that keeps it from growing forever.

The engine could see yesterday's close and this instant's price, with nothing
in between. These bars fill that gap, built from the quote feed already being
polled rather than a new external fetch.

Two classes of bug are pinned here. First, correctness of the bar itself —
volume in the feed is CUMULATIVE for the day, so a bar's volume is the delta
across its window; storing the raw figure would report the whole day's volume
in every bar. Second, growth: DELETE does not shrink a SQLite file, which is
how trading_agent.db reached 12 GB.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest

from app import bars5m

N500 = frozenset({"TCS", "INFY"})


def q(price, vol=0.0):
    return {"price": price, "vol": vol}


class BarWindowTest(unittest.TestCase):
    def test_window_is_aligned_to_five_minutes(self) -> None:
        self.assertEqual(bars5m.bar_start(1000), 900)
        self.assertEqual(bars5m.bar_start(1199), 900)
        self.assertEqual(bars5m.bar_start(1200), 1200)

    def test_window_is_stable_within_the_bar(self) -> None:
        self.assertEqual(bars5m.bar_start(1200), bars5m.bar_start(1499))


class AccumulationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.original = bars5m.DB
        bars5m.DB = os.path.join(self.tmp.name, "bars.db")
        bars5m._ACC.clear()
        bars5m._WINDOW = None

    def tearDown(self) -> None:
        bars5m.DB = self.original
        bars5m._ACC.clear()
        bars5m._WINDOW = None
        self.tmp.cleanup()

    def rows(self):
        con = sqlite3.connect(bars5m.DB)
        out = con.execute("SELECT symbol,ts,open,high,low,close,volume FROM bars "
                          "ORDER BY symbol,ts").fetchall()
        con.close()
        return out

    def test_ohlc_tracks_the_window(self) -> None:
        bars5m.observe({"TCS": q(100)}, now=1200, allowed=N500)
        bars5m.observe({"TCS": q(105)}, now=1260, allowed=N500)
        bars5m.observe({"TCS": q(95)}, now=1320, allowed=N500)
        bars5m.observe({"TCS": q(102)}, now=1440, allowed=N500)
        bars5m.observe({"TCS": q(110)}, now=1500, allowed=N500)   # crosses -> flush
        sym, ts, o, h, l, c, _v = self.rows()[0]
        self.assertEqual((o, h, l, c), (100, 105, 95, 102))
        self.assertEqual(ts, 1200)

    def test_volume_is_the_delta_not_the_running_total(self) -> None:
        """The feed's volume is cumulative for the day. Storing it raw would put
        the whole day's volume into every bar."""
        bars5m.observe({"TCS": q(100, vol=10_000)}, now=1200, allowed=N500)
        bars5m.observe({"TCS": q(101, vol=12_500)}, now=1400, allowed=N500)
        bars5m.observe({"TCS": q(101, vol=12_500)}, now=1500, allowed=N500)
        self.assertEqual(self.rows()[0][6], 2_500)

    def test_volume_never_goes_negative_on_a_feed_reset(self) -> None:
        bars5m.observe({"TCS": q(100, vol=10_000)}, now=1200, allowed=N500)
        bars5m.observe({"TCS": q(101, vol=5)}, now=1400, allowed=N500)
        bars5m.observe({"TCS": q(101, vol=5)}, now=1500, allowed=N500)
        self.assertGreaterEqual(self.rows()[0][6], 0)

    def test_only_index_members_are_recorded(self) -> None:
        """The whole point of scoping to the Nifty 500 is bounded growth."""
        bars5m.observe({"TCS": q(100), "PENNYCO": q(3)}, now=1200, allowed=N500)
        bars5m.observe({"TCS": q(100)}, now=1500, allowed=N500)
        self.assertEqual({r[0] for r in self.rows()}, {"TCS"})

    def test_bad_prices_are_ignored(self) -> None:
        for bad in (0, -1, None, "abc"):
            with self.subTest(price=bad):
                bars5m._ACC.clear()
                bars5m.observe({"TCS": q(bad)}, now=1200, allowed=N500)
                self.assertNotIn("TCS", bars5m._ACC)

    def test_a_new_window_starts_a_fresh_bar(self) -> None:
        bars5m.observe({"TCS": q(100)}, now=1200, allowed=N500)
        bars5m.observe({"TCS": q(200)}, now=1500, allowed=N500)
        bars5m.observe({"TCS": q(300)}, now=1800, allowed=N500)
        opens = [r[2] for r in self.rows()]
        self.assertEqual(opens, [100, 200])

    def test_reflushing_the_same_window_does_not_duplicate(self) -> None:
        bars5m.observe({"TCS": q(100)}, now=1200, allowed=N500)
        bars5m.flush(1200)
        bars5m.observe({"TCS": q(100)}, now=1200, allowed=N500)
        bars5m.flush(1200)
        self.assertEqual(len(self.rows()), 1)


class JanitorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.original = bars5m.DB
        bars5m.DB = os.path.join(self.tmp.name, "bars.db")
        bars5m._ACC.clear()
        bars5m._WINDOW = None

    def tearDown(self) -> None:
        bars5m.DB = self.original
        self.tmp.cleanup()

    def test_prune_drops_only_bars_past_retention(self) -> None:
        now = 200 * 86400
        con = bars5m._connect()
        con.executemany("INSERT INTO bars VALUES(?,?,?,?,?,?,?)", [
            ("TCS", now - 190 * 86400, 1, 1, 1, 1, 0),      # older than 180d
            ("TCS", now - 10 * 86400, 1, 1, 1, 1, 0),       # inside retention
        ])
        con.commit(); con.close()
        self.assertEqual(bars5m.prune(retain_days=180, now=now), 1)
        self.assertEqual(bars5m.stats()["rows"], 1)

    def test_vacuum_runs_without_error(self) -> None:
        bars5m._connect().close()
        self.assertTrue(bars5m.vacuum())

    def test_stats_on_a_missing_database_is_safe(self) -> None:
        bars5m.DB = os.path.join(self.tmp.name, "does-not-exist.db")
        self.assertEqual(bars5m.stats()["rows"], 0)


class MembershipTest(unittest.TestCase):
    def tearDown(self) -> None:
        bars5m._MEMBERS = (0.0, frozenset())

    def test_a_failed_refresh_keeps_the_last_good_set(self) -> None:
        """Otherwise one NSE outage silently shrinks the recorded universe to
        nothing and the day's bars are lost."""
        bars5m._MEMBERS = (0.0, frozenset({"TCS"}))
        original, bars5m.fetch_members = bars5m.fetch_members, lambda: frozenset()
        try:
            self.assertEqual(bars5m.members(now=1e12), frozenset({"TCS"}))
        finally:
            bars5m.fetch_members = original

    def test_membership_is_cached(self) -> None:
        calls = []
        original = bars5m.fetch_members
        bars5m.fetch_members = lambda: (calls.append(1), frozenset({"TCS"}))[1]
        try:
            bars5m._MEMBERS = (0.0, frozenset())
            bars5m.members(now=1000.0)
            bars5m.members(now=1001.0)
        finally:
            bars5m.fetch_members = original
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
