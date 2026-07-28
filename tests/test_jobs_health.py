"""Background-pipeline health assessment.

This exists because every serious bug found in this codebase recently was a
pipeline failing silently — the candle deadlock exited 0 after writing 700k
rows, alerts only fired with a browser open, the announcements poller lost a
day with no backfill.

The threshold that matters most is the daily-candle one. `/v2/api/health` uses
five days, which is why a feed lagging one or two trading sessions — the bug
that actually happened — passed as healthy. These tests pin a 30-hour limit and
assert the real historical case is caught.

Ages are hand-computed against a fixed `now`.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from app import jobs_health as jh

NOW = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)


def _ago(hours):
    return (NOW - timedelta(hours=hours)).isoformat()


def _obs(**pipelines):
    return {key: {"latest": value, "rows": 10} for key, value in pipelines.items()}


def _by_key(checks):
    return {c["pipeline"]: c for c in checks}


class AgeTest(unittest.TestCase):
    def test_hours_are_computed_from_an_iso_timestamp(self) -> None:
        self.assertAlmostEqual(jh._age_hours(_ago(3), NOW), 3.0, places=2)

    def test_zulu_suffix_is_handled(self) -> None:
        stamp = NOW.replace(tzinfo=None).isoformat() + "Z"
        self.assertAlmostEqual(jh._age_hours(stamp, NOW), 0.0, places=2)

    def test_space_separated_timestamp(self) -> None:
        """SQLite's datetime() emits a space rather than a T."""
        stamp = (NOW - timedelta(hours=2)).isoformat().replace("T", " ")
        self.assertAlmostEqual(jh._age_hours(stamp, NOW), 2.0, places=2)

    def test_bare_date_is_read_as_ist(self) -> None:
        """The shareholding quarter is a plain date, not a timestamp."""
        self.assertIsNotNone(jh._age_hours("2026-06-30", NOW))

    def test_unreadable_values(self) -> None:
        for value in (None, "", "not-a-date"):
            with self.subTest(value=value):
                self.assertIsNone(jh._age_hours(value, NOW))


class CandleThresholdTest(unittest.TestCase):
    """The check that /v2/api/health was too loose to make."""

    def test_one_session_behind_is_caught(self) -> None:
        """The real 2026-07 bug: Friday's bar had not landed by Monday
        afternoon. Roughly 72 hours — well past the 30h limit."""
        checks = _by_key(jh.assess(_obs(daily_candles=_ago(72)), now=NOW))
        self.assertEqual(checks["daily_candles"]["status"], "stale")
        self.assertIn("expected under 30", checks["daily_candles"]["detail"])

    def test_a_normal_overnight_gap_is_fine(self) -> None:
        """A bar from yesterday's close is expected, not a fault."""
        checks = _by_key(jh.assess(_obs(daily_candles=_ago(20)), now=NOW))
        self.assertEqual(checks["daily_candles"]["status"], "ok")

    def test_the_old_five_day_tolerance_would_have_missed_it(self) -> None:
        """Documents why this module exists rather than reusing the old check."""
        age = jh._age_hours(_ago(72), NOW)
        self.assertLess(age / 24, 5)                       # passes a 5-day rule
        self.assertGreater(age, jh.PIPELINES["daily_candles"]["max_age_hours"])


class AssessTest(unittest.TestCase):
    def test_fresh_pipelines_are_ok(self) -> None:
        checks = _by_key(jh.assess(
            _obs(quotes=_ago(0.01), daily_candles=_ago(2), catalysts=_ago(1),
                 engine=_ago(0.05), shareholding=_ago(24 * 30)), now=NOW))
        for key in ("quotes", "daily_candles", "catalysts", "engine", "shareholding"):
            self.assertEqual(checks[key]["status"], "ok", key)

    def test_absent_pipeline_is_unknown_not_broken(self) -> None:
        """Shareholding does not exist until its ingester ships. Calling that
        'broken' would cry wolf."""
        checks = _by_key(jh.assess(_obs(quotes=_ago(0.01)), now=NOW))
        self.assertEqual(checks["shareholding"]["status"], "unknown")
        self.assertIn("no data source", checks["shareholding"]["detail"])

    def test_missing_timestamp_is_unknown(self) -> None:
        checks = _by_key(jh.assess({"catalysts": {"latest": None, "rows": 0}}, now=NOW))
        self.assertEqual(checks["catalysts"]["status"], "unknown")

    def test_quotes_and_engine_are_idle_when_the_market_is_shut(self) -> None:
        """Judging a continuous feed at 2am would report an outage nightly."""
        checks = _by_key(jh.assess(
            _obs(quotes=_ago(10), engine=_ago(10)), now=NOW, market_open=False))
        self.assertEqual(checks["quotes"]["status"], "idle")
        self.assertEqual(checks["engine"]["status"], "idle")

    def test_candles_are_judged_even_when_the_market_is_shut(self) -> None:
        """Candle ingestion runs after the close — being shut is no excuse."""
        checks = _by_key(jh.assess(_obs(daily_candles=_ago(72)), now=NOW, market_open=False))
        self.assertEqual(checks["daily_candles"]["status"], "stale")

    def test_worst_first_ordering(self) -> None:
        checks = jh.assess(_obs(quotes=_ago(0.01), daily_candles=_ago(72)), now=NOW)
        self.assertEqual(checks[0]["status"], "stale")

    def test_every_pipeline_is_always_reported(self) -> None:
        self.assertEqual(len(jh.assess({}, now=NOW)), len(jh.PIPELINES))

    def test_row_counts_pass_through(self) -> None:
        checks = _by_key(jh.assess({"catalysts": {"latest": _ago(1), "rows": 2395}}, now=NOW))
        self.assertEqual(checks["catalysts"]["rows"], 2395)

    def test_garbage_observations_do_not_raise(self) -> None:
        for value in (None, "nonsense", 5, {"quotes": "bad"}):
            with self.subTest(value=value):
                self.assertEqual(len(jh.assess(value, now=NOW)), len(jh.PIPELINES))


class SummaryTest(unittest.TestCase):
    def test_stale_pipeline_fails_the_summary(self) -> None:
        summary = jh.summarise(jh.assess(_obs(daily_candles=_ago(72)), now=NOW))
        self.assertFalse(summary["ok"])
        self.assertIn("daily_candles", summary["stale"])

    def test_unknown_alone_does_not_fail(self) -> None:
        """Not-yet-deployed is not an outage."""
        summary = jh.summarise(jh.assess(
            _obs(quotes=_ago(0.01), daily_candles=_ago(2), catalysts=_ago(1),
                 engine=_ago(0.05)), now=NOW))
        self.assertTrue(summary["ok"])
        self.assertIn("shareholding", summary["unknown"])

    def test_all_current(self) -> None:
        summary = jh.summarise(jh.assess(
            _obs(quotes=_ago(0.01), daily_candles=_ago(2), catalysts=_ago(1),
                 engine=_ago(0.05), shareholding=_ago(24)), now=NOW))
        self.assertTrue(summary["ok"])
        self.assertEqual(summary["headline"], "all pipelines current")

    def test_empty_input(self) -> None:
        self.assertTrue(jh.summarise([])["ok"])


if __name__ == "__main__":
    unittest.main()
