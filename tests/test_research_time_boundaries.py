"""Exact decision boundaries, using synthetic observations only."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import sqlite3
import unittest

from app import research_data


class ResearchTimeBoundaryTest(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(':memory:')
        self.addCleanup(self.con.close)
        self.at = datetime(2026, 1, 5, 10, 0, 0, 123100, timezone.utc)
        self.row = dict(instrument_id='ins_synthetic', kind='membership', fact_key='NIFTY100:membership',
                        effective_at=(self.at-timedelta(days=1)).isoformat(),
                        published_at=self.at.isoformat(), observed_at=self.at.isoformat(),
                        source='SYNTHETIC', rights_reference='fixture only',
                        payload=dict(index='NIFTY100', member=True))

    def record(self, row):
        research_data.record(self.con, [row], now=self.at+timedelta(days=1))

    def known(self):
        return research_data.known(self.con, 'ins_synthetic', 'membership', self.at.isoformat())

    def test_submillisecond_future_observation_never_enters_a_decision(self):
        row = deepcopy(self.row)
        row['observed_at'] = (self.at+timedelta(microseconds=100)).isoformat()
        self.record(row)
        self.assertEqual(self.known(), [])

    def test_submillisecond_future_effective_and_publication_times_are_excluded(self):
        for field in ('effective_at', 'published_at'):
            with self.subTest(field=field):
                con = sqlite3.connect(':memory:')
                self.addCleanup(con.close)
                row = deepcopy(self.row)
                row[field] = (self.at+timedelta(microseconds=100)).isoformat()
                if field == 'published_at':
                    row['observed_at'] = (self.at+timedelta(microseconds=200)).isoformat()
                research_data.record(con, [row], now=self.at+timedelta(seconds=1))
                self.assertEqual(research_data.known(con, 'ins_synthetic', 'membership', self.at.isoformat()), [])

    def test_latest_correction_uses_exact_observation_time_not_insertion_order(self):
        newer = deepcopy(self.row)
        newer['observed_at'] = (self.at+timedelta(microseconds=100)).isoformat()
        newer['payload']['member'] = False
        self.record(newer)
        self.record(self.row)
        result = research_data.known(self.con, 'ins_synthetic', 'membership', newer['observed_at'])
        self.assertFalse(result[0]['payload']['member'])

    def test_equivalent_offsets_cannot_hide_contemporaneous_conflicts(self):
        self.record(self.row)
        other = deepcopy(self.row)
        other['observed_at'] = self.at.astimezone(timezone(timedelta(hours=5, minutes=30))).isoformat()
        other['payload']['member'] = False
        self.record(other)
        with self.assertRaisesRegex(ValueError, 'Conflicting'):
            self.known()

    def test_a_later_correction_to_an_earlier_effective_period_stays_latest(self):
        self.record(self.row)
        other = deepcopy(self.row)
        other['observed_at'] = (self.at+timedelta(hours=1)).isoformat()
        other['effective_at'] = (self.at-timedelta(days=2)).isoformat()
        other['payload']['member'] = False
        self.record(other)
        result = research_data.known(self.con, 'ins_synthetic', 'membership', other['observed_at'])
        self.assertFalse(result[0]['payload']['member'])
