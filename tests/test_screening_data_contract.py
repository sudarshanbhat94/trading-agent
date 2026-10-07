"""Data validity at the real Ideas screen boundary; no strategy promotion."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from app.screening import store
from app.screening.screen import _features, build
from tests import test_evidence_screen as fixtures


class ScreeningDataContractTest(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(':memory:'); self.addCleanup(self.con.close)
        store.initialise(self.con)
        self.now = datetime(2026, 9, 30, 12, tzinfo=timezone.utc)

    def save(self, value, source='NSE', known=None):
        at = known or self.now
        store.save(self.con, 'TEST', 'news', source, value, at.isoformat(), max(self.now, at))

    def report(self, payload):
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        path = Path(directory.name) / 'screening.db'
        with sqlite3.connect(path) as con:
            store.initialise(con)
            con.execute('INSERT INTO screens VALUES(?,?)', (self.now.isoformat(), json.dumps(payload)))
        return store.report(path, self.now)

    def test_same_identity_cannot_rewrite_original_evidence(self):
        self.save({'events': []})
        self.save({'events': []})  # Exact replay remains harmless.
        with self.assertRaises(ValueError):
            self.save({'events': [{'classification': 'risk_review'}]})
        self.assertEqual(store.latest(self.con, 'TEST', 'news', self.now, 1)['events'], [])

    def test_equal_time_conflicting_sources_are_unavailable(self):
        self.save({'events': []})
        self.save({'events': [{'classification': 'risk_review'}]}, 'other official source')
        self.assertIsNone(store.latest(self.con, 'TEST', 'news', self.now, 1))

    def test_sql_timestamp_rounding_cannot_expose_future_evidence(self):
        self.save({'events': []}, known=self.now + timedelta(microseconds=250))
        self.assertIsNone(store.latest(self.con, 'TEST', 'news', self.now, 1))

    def test_timezone_equivalent_conflict_does_not_choose_by_feed_order(self):
        self.save({'events': []})
        self.con.execute('INSERT INTO evidence VALUES(?,?,?,?,?)',
                         ('TEST', 'news', '2026-09-30T17:30:00+05:30', 'NSE', '{"events":[1]}'))
        self.assertIsNone(store.latest(self.con, 'TEST', 'news', self.now, 1))

    def test_malformed_stored_payload_is_unavailable(self):
        self.con.execute('INSERT INTO evidence VALUES(?,?,?,?,?)',
                         ('TEST', 'news', self.now.isoformat(), 'NSE', '[]'))
        self.assertIsNone(store.latest(self.con, 'TEST', 'news', self.now, 1))

    def test_duplicate_session_or_open_outside_range_is_not_price_evidence(self):
        import pandas as pd
        frame = fixtures.prices()
        self.assertIsNone(_features(pd.concat([frame, frame.tail(1)]), frame.index[-1]))
        frame.loc[frame.index[-1], 'open'] = frame.loc[frame.index[-1], 'high'] + 1
        self.assertIsNone(_features(frame, frame.index[-1]))

    def test_relative_strength_requires_matching_return_dates(self):
        frame = fixtures.prices()
        stock = frame.drop(frame.index[-10])
        result = build({'TEST': stock, 'NIFTYBEES': frame}, {'TEST'}, {}, self.con,
                       self.now, frame.index[-1])
        self.assertIsNone(result['equities'][0]['metrics']['rs_vs_nifty20_pct'])
        self.assertIn('Nifty comparison dates do not match', result['equities'][0]['flags'])

    def test_identical_return_dates_preserve_computed_strength(self):
        frame = fixtures.prices()
        result = build({'TEST': frame, 'NIFTYBEES': frame}, {'TEST'}, {}, self.con,
                       self.now, frame.index[-1])
        self.assertEqual(result['equities'][0]['metrics']['rs_vs_nifty20_pct'], 0)
        self.assertEqual(result['equities'][0]['metrics']['rs_benchmark'], 'NIFTYBEES')
        self.assertIn('ETF proxy', result['equities'][0]['metrics']['rs_benchmark_kind'])

    def test_unreviewed_jump_anywhere_in_momentum_window_is_not_evidence(self):
        frame = fixtures.prices()
        frame.loc[frame.index[:-50], ['open','high','low','close']] *= .5
        self.assertIsNone(_features(frame, frame.index[-1]))

    def test_engine_quotes_require_aware_nonfuture_time_and_real_price(self):
        from app.sleeves.feeds import fresh_quotes
        good = dict(price=100, ts=self.now.isoformat())
        bad = (dict(good, ts=(self.now+timedelta(seconds=1)).isoformat()),
               dict(good, ts=self.now.replace(tzinfo=None).isoformat()), dict(good, price=True))
        for quote in bad:
            with self.subTest(quote=quote):
                self.assertEqual(fresh_quotes({'TEST': quote}, self.now), {})
        self.assertEqual(fresh_quotes({'TEST':good}, self.now), {'TEST':good})

    def test_database_cannot_mutate_or_delete_archived_evidence(self):
        self.save({'events':[]})
        for sql in ("UPDATE evidence SET payload='{}'", 'DELETE FROM evidence'):
            with self.assertRaises(sqlite3.IntegrityError):
                self.con.execute(sql)

    def test_cached_screen_cannot_keep_expired_news_as_passed(self):
        old = (self.now - timedelta(hours=3)).isoformat()
        data = self.report(dict(status='ok', generated_at=self.now.isoformat(), price_asof='2026-09-30',
                                equities=[dict(symbol='TEST', flags=[], status='RESEARCH',
                                               news=dict(known_at=old, checked_at=old, events=[]))]))
        self.assertIn('official news feed not freshly checked', data['equities'][0]['flags'])
        self.assertEqual(data['equities'][0]['status'], 'REVIEW REQUIRED')

    def test_cached_price_freshness_is_recomputed(self):
        data = self.report(dict(status='ok', generated_at=self.now.isoformat(), price_asof='2026-09-20',
                                price_stale=False, equities=[]))
        self.assertTrue(data['price_stale'])

    def test_screen_payload_cannot_claim_different_availability(self):
        data = self.report(dict(status='ok', generated_at=(self.now + timedelta(hours=1)).isoformat(), equities=[]))
        self.assertEqual(data['status'], 'unavailable')

    def test_corrupt_screen_returns_complete_empty_state(self):
        data = self.report(dict(status='ok', generated_at='invalid', equities=[]))
        self.assertEqual(data['status'], 'unavailable')
        self.assertEqual(data['equities'], [])
        self.assertEqual(data['indices'], [])
