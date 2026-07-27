"""NSE corporate-announcement ingestion.

The volume_surge and btst lanes gate on this table, so two properties matter:
timestamps must be correct regardless of where the process runs, and a poller
that misses a day must be able to recover it. Neither held before.
"""

from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import nse_announcements as na  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))


class EpochTest(unittest.TestCase):
    def test_parses_ist_regardless_of_host_timezone(self) -> None:
        """The old implementation used a naive .timestamp(), so the result
        depended on the host's TZ. 17:00:07 IST is 11:30:07 UTC."""
        expected = int(datetime(2026, 7, 23, 17, 0, 7, tzinfo=IST).timestamp())
        self.assertEqual(na._epoch("23-Jul-2026 17:00:07"), expected)
        self.assertEqual(
            datetime.fromtimestamp(na._epoch("23-Jul-2026 17:00:07"), timezone.utc).strftime("%H:%M"),
            "11:30",
        )

    def test_matches_the_old_utc_host_result(self) -> None:
        """The box runs UTC, where the old formula was correct. New values must
        agree, so stored rows stay consistent with newly ingested ones."""
        naive = datetime.strptime("23-Jul-2026 17:00:07", "%d-%b-%Y %H:%M:%S")
        old_on_utc_host = int(naive.replace(tzinfo=timezone.utc).timestamp()) - int(5.5 * 3600)
        self.assertEqual(na._epoch("23-Jul-2026 17:00:07"), old_on_utc_host)

    def test_handles_whitespace(self) -> None:
        self.assertEqual(na._epoch("  23-Jul-2026 17:00:07  "), na._epoch("23-Jul-2026 17:00:07"))

    def test_unparseable_returns_zero(self) -> None:
        for bad in ("", "not a date", "2026-07-23", None):
            with self.subTest(value=bad):
                self.assertEqual(na._epoch(bad if bad is not None else ""), 0)

    def test_ordering_is_preserved(self) -> None:
        earlier = na._epoch("23-Jul-2026 09:15:00")
        later = na._epoch("23-Jul-2026 15:30:00")
        self.assertLess(earlier, later)
        self.assertEqual(later - earlier, 6 * 3600 + 15 * 60)


class _FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else []

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _FakeClient:
    """Records every URL requested so the date range can be asserted."""

    def __init__(self, responses=None):
        self.urls: list[str] = []
        self.responses = responses or {}

    def get(self, url):
        self.urls.append(url)
        if "nseindia.com/api" not in url:
            return _FakeResponse(200, [])
        for pattern, response in self.responses.items():
            if pattern in url:
                return response
        return _FakeResponse(200, [{"symbol": "TEST"}])

    @property
    def api_urls(self):
        return [u for u in self.urls if "/api/" in u]


def _patch_sleep(test):
    original = na.time.sleep
    na.time.sleep = lambda *_: None
    test.addCleanup(lambda: setattr(na.time, "sleep", original))


class FetchRangeTest(unittest.TestCase):
    def setUp(self) -> None:
        _patch_sleep(self)

    def test_queries_a_backfill_window_not_just_today(self) -> None:
        """A poller that only asks for today loses a day permanently if it is
        down when those filings publish."""
        client = _FakeClient()
        na.fetch(client, backfill_days=3)

        url = client.api_urls[0]
        now_ist = datetime.now(IST)
        expected_from = (now_ist - timedelta(days=3)).strftime("%d-%m-%Y")
        expected_to = now_ist.strftime("%d-%m-%Y")
        self.assertIn(f"from_date={expected_from}", url)
        self.assertIn(f"to_date={expected_to}", url)
        self.assertNotEqual(expected_from, expected_to)

    def test_zero_backfill_is_a_single_day(self) -> None:
        client = _FakeClient()
        na.fetch(client, backfill_days=0)
        today = datetime.now(IST).strftime("%d-%m-%Y")
        self.assertIn(f"from_date={today}&to_date={today}", client.api_urls[0])

    def test_uses_ist_dates_not_host_dates(self) -> None:
        client = _FakeClient()
        na.fetch(client, backfill_days=0)
        self.assertIn(datetime.now(IST).strftime("%d-%m-%Y"), client.api_urls[0])

    def test_falls_back_to_single_day_then_bare_endpoint(self) -> None:
        client = _FakeClient(responses={
            "from_date": _FakeResponse(401),          # both ranged attempts fail
        })
        result = na.fetch(client, backfill_days=3)
        self.assertEqual(result, [{"symbol": "TEST"}])   # bare endpoint served it
        self.assertEqual(len(client.api_urls), 3)        # range, single day, bare

    def test_all_attempts_failing_returns_empty(self) -> None:
        client = _FakeClient(responses={"corporate-announcements": _FakeResponse(503)})
        self.assertEqual(na.fetch(client, backfill_days=1), [])

    def test_non_json_response_returns_empty(self) -> None:
        client = _FakeClient(responses={
            "corporate-announcements": _FakeResponse(200, ValueError("not json")),
        })
        self.assertEqual(na.fetch(client, backfill_days=1), [])

    def test_dict_payload_is_unwrapped(self) -> None:
        client = _FakeClient(responses={
            "corporate-announcements": _FakeResponse(200, {"data": [{"symbol": "ABC"}]}),
        })
        self.assertEqual(na.fetch(client, backfill_days=1), [{"symbol": "ABC"}])

    def test_bootstraps_cookies_before_the_api_call(self) -> None:
        client = _FakeClient()
        na.fetch(client, backfill_days=1)
        self.assertNotIn("/api/", client.urls[0])


class ClassifyTest(unittest.TestCase):
    """Guard the catalyst gate against both false negatives and false positives."""

    def test_results_filing(self) -> None:
        self.assertEqual(na.classify("Financial Results", "Un-audited results for Q1"), "results")

    def test_order_win(self) -> None:
        self.assertEqual(na.classify("Award of contract", "Company bags order worth 500cr"), "order")

    def test_corporate_action(self) -> None:
        self.assertEqual(na.classify("Buyback", "Board approved buyback of equity shares"), "corp_action")

    def test_esop_allotment_is_noise(self) -> None:
        """The real CarTrade filing: a generic 'Updates' subject whose body is
        an ESOP allotment. Must not count as a catalyst."""
        subject = "Updates"
        text = ("Cartrade Tech Limited has informed the Exchange regarding 'Allotment of "
                "3,03,500 equity shares under ESOP 2011, ESOP 2014, ESOP 2015 and ESOP "
                "2021(I) of CarTrade Tech Limited'.")
        self.assertEqual(na.classify(subject, text), "noise")

    def test_negative_keys_beat_order_keys(self) -> None:
        """'Termination of contract' is a loss, not a buy catalyst."""
        self.assertEqual(na.classify("Rescission/termination of contract", ""), "noise")

    def test_procedural_filings_are_noise(self) -> None:
        for subject in ("Trading Window", "Newspaper Publication", "Analysts/Institutional Investor Meet"):
            with self.subTest(subject=subject):
                self.assertEqual(na.classify(subject, ""), "noise")

    def test_unmatched_is_other_not_a_catalyst(self) -> None:
        self.assertEqual(na.classify("Some unrelated disclosure", ""), "other")

    def test_empty_input(self) -> None:
        self.assertEqual(na.classify("", ""), "other")


if __name__ == "__main__":
    unittest.main()
