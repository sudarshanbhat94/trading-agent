"""Moving-average cross alerts.

The distinction that matters: a *cross* needs a before and an after. Testing
only whether the live price sits above the average would fire every cycle for
as long as it stayed there — that is a level alert, which already exists as
`above`. These tests pin that difference, because getting it wrong produces an
alert that spams the user's phone rather than one that fires once.

SMA values are hand-computed from the fixture closes.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest

from app import v2_web
from app.v2_web import sma_cross_hit


def _flat(value, n):
    return [float(value)] * n


class CrossUpTest(unittest.TestCase):
    def test_fires_when_price_crosses_from_below(self) -> None:
        # 20 closes at 100 -> SMA20 = 100. Previous close 100, price 101.
        closes = _flat(100.0, 21)
        self.assertTrue(sma_cross_hit(closes, 101.0, 20, "up"))

    def test_silent_when_already_above(self) -> None:
        """The whole point: no repeat firing while price stays across."""
        closes = _flat(100.0, 20) + [105.0]      # previous close already above
        self.assertFalse(sma_cross_hit(closes, 106.0, 20, "up"))

    def test_silent_when_price_stays_below(self) -> None:
        self.assertFalse(sma_cross_hit(_flat(100.0, 21), 99.0, 20, "up"))

    def test_hand_computed_sma(self) -> None:
        """Closes 1..20 average to 10.5; previous close is 20, so a cross up
        cannot be reported — price came from above."""
        closes = [float(i) for i in range(1, 21)]
        self.assertAlmostEqual(sum(closes[-20:]) / 20, 10.5)
        self.assertFalse(sma_cross_hit(closes, 11.0, 20, "up"))


class CrossDownTest(unittest.TestCase):
    def test_fires_when_price_crosses_from_above(self) -> None:
        closes = _flat(100.0, 21)
        self.assertTrue(sma_cross_hit(closes, 99.0, 20, "down"))

    def test_silent_when_already_below(self) -> None:
        closes = _flat(100.0, 20) + [95.0]
        self.assertFalse(sma_cross_hit(closes, 94.0, 20, "down"))

    def test_silent_when_price_stays_above(self) -> None:
        self.assertFalse(sma_cross_hit(_flat(100.0, 21), 101.0, 20, "down"))


class GuardTest(unittest.TestCase):
    def test_insufficient_history_never_fires(self) -> None:
        """Without period+1 closes there is no 'before', so a cross cannot be
        established — failing closed beats guessing."""
        self.assertFalse(sma_cross_hit(_flat(100.0, 20), 101.0, 20, "up"))
        self.assertFalse(sma_cross_hit([], 101.0, 20, "up"))

    def test_longer_periods_need_more_history(self) -> None:
        closes = _flat(100.0, 60)
        self.assertTrue(sma_cross_hit(closes, 101.0, 50, "up"))
        self.assertFalse(sma_cross_hit(closes, 101.0, 200, "up"))

    def test_bad_direction_or_period(self) -> None:
        closes = _flat(100.0, 21)
        self.assertFalse(sma_cross_hit(closes, 101.0, 20, "sideways"))
        self.assertFalse(sma_cross_hit(closes, 101.0, 0, "up"))
        self.assertFalse(sma_cross_hit(closes, 101.0, -5, "up"))

    def test_unparseable_inputs_fail_closed(self) -> None:
        closes = _flat(100.0, 21)
        self.assertFalse(sma_cross_hit(closes, "abc", 20, "up"))
        self.assertFalse(sma_cross_hit(closes, 101.0, "abc", "up"))

    def test_only_supported_periods_are_offered(self) -> None:
        self.assertEqual(v2_web.ALERT_SMA_PERIODS, (20, 50, 200))


class CandleCacheTest(unittest.TestCase):
    def setUp(self) -> None:
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        con = sqlite3.connect(self.path)
        # Mirrors the real candles table, which carries full OHLC — the loader
        # reads open/high/low for the pattern rules as well as close.
        con.execute("CREATE TABLE candles (symbol TEXT, ts TEXT, open REAL, high REAL, "
                    "low REAL, close REAL, source TEXT)")
        for i in range(30):
            close = 100.0 + i
            con.execute("INSERT INTO candles VALUES (?,?,?,?,?,?,?)",
                        ("ABC", f"2026-06-{i + 1:02d}", close - 0.5, close + 1.0,
                         close - 1.0, close, "upstox-live:day"))
        con.commit()
        con.close()
        self._db = v2_web.MAIN_DB
        v2_web.MAIN_DB = self.path
        v2_web._ALERT_CANDLE_CACHE.clear()

    def tearDown(self) -> None:
        v2_web.MAIN_DB = self._db
        v2_web._ALERT_CANDLE_CACHE.clear()
        os.unlink(self.path)

    def test_returns_closes_oldest_first(self) -> None:
        """Order matters: the last element must be the most recent close."""
        closes = v2_web._alert_candles("ABC")
        self.assertEqual(len(closes), 30)
        self.assertEqual(closes[0], 100.0)
        self.assertEqual(closes[-1], 129.0)

    def test_result_is_cached(self) -> None:
        first = v2_web._alert_candles("ABC")
        os.unlink(self.path)              # a second read would now fail
        self.assertEqual(v2_web._alert_candles("ABC"), first)
        # recreate so tearDown's unlink succeeds
        open(self.path, "w").close()

    def test_unknown_symbol_returns_empty(self) -> None:
        self.assertEqual(v2_web._alert_candles("NOPE"), [])

    def test_missing_database_returns_empty_not_raise(self) -> None:
        v2_web.MAIN_DB = "/nonexistent/path.db"
        v2_web._ALERT_CANDLE_CACHE.clear()
        self.assertEqual(v2_web._alert_candles("ABC"), [])

    def test_symbol_is_upper_cased(self) -> None:
        self.assertEqual(len(v2_web._alert_candles("abc")), 30)

    def test_cross_against_the_cached_series(self) -> None:
        """Closes 100..129: SMA20 over the last 20 is 119.5, previous close
        129, so price must fall through 119.5 to fire a cross down."""
        closes = v2_web._alert_candles("ABC")
        self.assertAlmostEqual(sum(closes[-20:]) / 20, 119.5)
        self.assertTrue(sma_cross_hit(closes, 119.0, 20, "down"))
        self.assertFalse(sma_cross_hit(closes, 120.0, 20, "down"))


if __name__ == "__main__":
    unittest.main()
