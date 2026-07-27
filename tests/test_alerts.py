"""Price alert rules and where they get evaluated.

The rules themselves were never tested, and — more seriously — they were only
evaluated inside the SSE payload builder, so an alert fired only while a
browser had the dashboard open. Set an alert, close the tab, and it never
triggered. Telegram delivery existed but was unreachable without a browser.

These tests pin the trigger conditions and assert that the engine loop, which
runs server-side regardless of any client, is what evaluates them.
"""

from __future__ import annotations

import inspect
import unittest

from app import v2_live
from app.v2_web import alert_hit


class AboveTest(unittest.TestCase):
    def test_fires_at_and_above_the_level(self) -> None:
        self.assertTrue(alert_hit("above", 100.0, 100.0))
        self.assertTrue(alert_hit("above", 100.0, 100.01))

    def test_silent_below(self) -> None:
        self.assertFalse(alert_hit("above", 100.0, 99.99))


class BelowTest(unittest.TestCase):
    def test_fires_at_and_below_the_level(self) -> None:
        self.assertTrue(alert_hit("below", 100.0, 100.0))
        self.assertTrue(alert_hit("below", 100.0, 99.99))

    def test_silent_above(self) -> None:
        self.assertFalse(alert_hit("below", 100.0, 100.01))


class PercentTest(unittest.TestCase):
    def test_fires_on_a_rally(self) -> None:
        self.assertTrue(alert_hit("pct", 5.0, 120.0, day_change_pct=6.2))

    def test_fires_on_a_fall_of_the_same_size(self) -> None:
        """A percent alert is about magnitude — a -6% collapse matters at least
        as much as a +6% rally."""
        self.assertTrue(alert_hit("pct", 5.0, 80.0, day_change_pct=-6.2))

    def test_silent_inside_the_band(self) -> None:
        self.assertFalse(alert_hit("pct", 5.0, 101.0, day_change_pct=4.9))
        self.assertFalse(alert_hit("pct", 5.0, 99.0, day_change_pct=-4.9))

    def test_exact_threshold_fires(self) -> None:
        self.assertTrue(alert_hit("pct", 5.0, 105.0, day_change_pct=5.0))

    def test_missing_day_change_does_not_fire(self) -> None:
        self.assertFalse(alert_hit("pct", 5.0, 105.0, day_change_pct=None))


class RobustnessTest(unittest.TestCase):
    def test_unknown_kind_never_fires(self) -> None:
        self.assertFalse(alert_hit("sideways", 100.0, 100.0))
        self.assertFalse(alert_hit(None, 100.0, 100.0))

    def test_unparseable_inputs_never_fire(self) -> None:
        """Failing closed matters: a spurious fire sends a false signal to the
        user's phone."""
        self.assertFalse(alert_hit("above", "abc", 100.0))
        self.assertFalse(alert_hit("above", 100.0, None))
        self.assertFalse(alert_hit("pct", 5.0, 100.0, day_change_pct="abc"))

    def test_string_numbers_are_accepted(self) -> None:
        self.assertTrue(alert_hit("above", "100", "101"))


class EvaluationSiteTest(unittest.TestCase):
    """Where alerts are checked from is the whole bug."""

    def test_engine_loop_evaluates_alerts(self) -> None:
        source = inspect.getsource(v2_live.loop)
        self.assertIn("_check_alerts()", source,
                      "the engine loop must evaluate alerts server-side")

    def test_evaluation_is_throttled(self) -> None:
        """The loop runs every ~8s; an unthrottled pass would add DB work to
        every cycle. The codebase's own lesson: universe-scanning passes must
        never run at the exit cadence."""
        source = inspect.getsource(v2_live.loop)
        self.assertIn("ALERT_INTERVAL", source)
        self.assertGreaterEqual(v2_live.ALERT_INTERVAL, 10)

    def test_alert_failure_cannot_stop_the_engine(self) -> None:
        source = inspect.getsource(v2_live.loop)
        block = source[source.index("_check_alerts()"):]
        self.assertIn("except Exception", block,
                      "an alert error must not break the trading loop")

    def test_import_is_lazy_to_avoid_a_cycle(self) -> None:
        """v2_web imports v2_live; a module-level import back would be circular."""
        source = inspect.getsource(v2_live.loop)
        self.assertIn("from . import v2_web", source)
        module_source = inspect.getsource(v2_live)
        header = module_source[:module_source.index("def ")]
        self.assertNotIn("import v2_web", header)


if __name__ == "__main__":
    unittest.main()
