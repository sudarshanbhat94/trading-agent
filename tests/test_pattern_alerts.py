"""Candlestick-pattern alerts.

The hard part is not detection — `indicators.candlestick_patterns()` already
does that and is tested separately. It is freshness: a daily pattern persists
for the entire session, so an alert that fires on "a pattern exists" would
re-trigger every 20 seconds until midnight. The bar must be newer than the
alert.

Pattern fixtures are constructed from the definitions (a bullish engulfing
needs an up bar whose body swallows the previous down bar), not copied from the
implementation's output.
"""

from __future__ import annotations

import unittest

from app.v2_web import pattern_hit


def _bar(date, o, h, l, c):
    return (date, float(o), float(h), float(l), float(c))


def _flat(n, start_day=1, price=100.0):
    """Doji-free filler: small green bodies, nothing the detector reacts to."""
    return [_bar(f"2026-07-{start_day + i:02d}", price, price + 1.2, price - 1.2, price + 0.8)
            for i in range(n)]


def _bullish_engulfing_tail():
    """Down bar, then an up bar whose body covers it entirely."""
    return [_bar("2026-07-20", 105.0, 105.5, 99.5, 100.0),
            _bar("2026-07-21", 99.0, 106.5, 98.5, 106.0)]


class FreshnessTest(unittest.TestCase):
    """The rule that makes this usable rather than a notification loop."""

    def test_fires_on_a_bar_newer_than_the_alert(self) -> None:
        bars = _flat(5) + _bullish_engulfing_tail()
        found = pattern_hit(bars, "2026-07-20T15:00:00+05:30")
        self.assertIn("bullish_engulfing", found)

    def test_silent_when_the_newest_bar_predates_the_alert(self) -> None:
        """Set an alert today; yesterday's pattern must not fire it."""
        bars = _flat(5) + _bullish_engulfing_tail()
        self.assertEqual(pattern_hit(bars, "2026-07-22T09:15:00+05:30"), [])

    def test_silent_on_the_same_day_the_alert_was_created(self) -> None:
        """Same-day is not newer — otherwise the alert fires the moment it is
        set, on a bar that already existed."""
        bars = _flat(5) + _bullish_engulfing_tail()
        self.assertEqual(pattern_hit(bars, "2026-07-21T09:15:00+05:30"), [])

    def test_fires_once_per_new_bar_not_per_check(self) -> None:
        """Two consecutive evaluations against the same bar give the same
        answer; the loop's dedupe is the alert being deactivated on fire, and
        the freshness rule is what stops a re-armed alert re-firing all day."""
        bars = _flat(5) + _bullish_engulfing_tail()
        created = "2026-07-20T15:00:00+05:30"
        self.assertTrue(pattern_hit(bars, created))
        self.assertTrue(pattern_hit(bars, created))
        # Once the alert is re-created after that bar, it goes quiet.
        self.assertEqual(pattern_hit(bars, "2026-07-21T16:00:00+05:30"), [])


class DetectionTest(unittest.TestCase):
    def test_no_pattern_means_no_fire(self) -> None:
        self.assertEqual(pattern_hit(_flat(10), "2026-07-01"), [])

    def test_bearish_engulfing_is_detected(self) -> None:
        bars = _flat(5) + [
            _bar("2026-07-20", 100.0, 105.5, 99.5, 105.0),
            _bar("2026-07-21", 106.0, 106.5, 98.5, 99.0),
        ]
        self.assertIn("bearish_engulfing", pattern_hit(bars, "2026-07-20"))

    def test_hammer_is_detected(self) -> None:
        bars = _flat(5) + [_bar("2026-07-21", 100.0, 101.2, 95.0, 101.0)]
        self.assertIn("hammer", pattern_hit(bars, "2026-07-20"))

    def test_returns_every_matching_pattern(self) -> None:
        """Single- and multi-bar patterns can legitimately coincide."""
        bars = _flat(5) + _bullish_engulfing_tail()
        found = pattern_hit(bars, "2026-07-20")
        self.assertIsInstance(found, list)
        self.assertGreaterEqual(len(found), 1)


class RobustnessTest(unittest.TestCase):
    def test_empty_bars(self) -> None:
        self.assertEqual(pattern_hit([], "2026-07-20"), [])

    def test_unparseable_created_at_fails_closed(self) -> None:
        bars = _flat(5) + _bullish_engulfing_tail()
        for bad in ("", "not-a-date", None):
            with self.subTest(value=bad):
                self.assertEqual(pattern_hit(bars, bad), [])

    def test_timestamped_bar_dates_are_handled(self) -> None:
        bars = _flat(5) + [
            _bar("2026-07-20T00:00:00+05:30", 105.0, 105.5, 99.5, 100.0),
            _bar("2026-07-21T00:00:00+05:30", 99.0, 106.5, 98.5, 106.0),
        ]
        self.assertIn("bullish_engulfing", pattern_hit(bars, "2026-07-20"))

    def test_zero_range_bar_is_not_a_pattern(self) -> None:
        bars = _flat(5) + [_bar("2026-07-21", 100.0, 100.0, 100.0, 100.0)]
        self.assertEqual(pattern_hit(bars, "2026-07-20"), [])


if __name__ == "__main__":
    unittest.main()
