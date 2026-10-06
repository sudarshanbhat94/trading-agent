import gzip
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from unittest.mock import patch

import httpx
from app import catalogue_refresh as refresh, instrument_catalog


class OfficialDiscoveryRefreshTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.con = sqlite3.connect(':memory:'); self.addCleanup(self.con.close)
        self.now = datetime(2026, 10, 6, 19, 10, tzinfo=timezone.utc)  # Oct 7 in IST
        self.raw = gzip.compress(json.dumps([dict(segment='NSE_EQ', exchange='NSE', instrument_type='EQ',
            isin='TEST', instrument_key='NSE_EQ|TEST', trading_symbol='TEST', lot_size=1, tick_size=5, freeze_quantity=100000)]).encode())
        self.headers = {'last-modified': format_datetime(self.now - timedelta(minutes=1), usegmt=True)}

    def ingest(self, **kwargs):
        return refresh.ingest(self.con, self.raw, self.headers, self.root, now=self.now, **kwargs)

    def test_dated_archive_is_immutable_and_cannot_replace_reviewed_execution_provider(self):
        spec = instrument_catalog.Instrument('NSE', 'NSE_EQ', 'EQUITY', 'TEST', 'INR', 'TEST', 'EQ', tick_size='0.05', freeze_quantity=10000)
        reviewed = instrument_catalog.import_snapshot(self.con, [(spec, 'NSE_EQ|TEST')], provider='upstox',
            source='SYNTHETIC reviewed source', source_day='2026-10-07', observed_at=self.now.isoformat(), now=self.now)
        result = self.ingest()
        self.assertEqual(result['source_day'], '2026-10-07'); self.assertFalse(result['order_permission'])
        self.assertFalse(result['commercial_rights_verified'])
        self.assertEqual(instrument_catalog.resolve(self.con, symbol='TEST', provider='upstox', now=self.now)[0], spec)
        unknown, _ = instrument_catalog.resolve(self.con, symbol='TEST', provider=refresh.PROVIDER, now=self.now)
        self.assertIsNone(unknown.tick_size); self.assertEqual(unknown.series, 'UNKNOWN')
        self.assertEqual(self.ingest(), result)
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM catalogue_refresh_events').fetchone()[0], 1)
        self.assertEqual(hashlib.sha256((self.root/result['archive']).read_bytes()).hexdigest(), result['sha256'])
        with self.assertRaises(sqlite3.IntegrityError):
            self.con.execute('DELETE FROM catalogue_refresh_events')

    def test_missing_future_or_stale_source_date_never_relabels_current_day(self):
        for headers in ({}, {'last-modified':'invalid'}, {'last-modified':format_datetime(self.now+timedelta(seconds=1))},
                        {'last-modified':format_datetime(self.now-timedelta(hours=26))}):
            with self.assertRaises(ValueError):
                refresh.ingest(self.con, self.raw, headers, self.root, now=self.now)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_size_schema_alias_duplicates_and_archive_corruption_refuse(self):
        with patch.object(refresh, 'MAX_EXPANDED', 12):
            with self.assertRaises(ValueError): self.ingest()
        for data in ({'changed':'layout'}, [dict(segment='NSE_EQ', exchange='NSE', instrument_type='EQ', isin='TEST', instrument_key='NSE_EQ|OTHER')]):
            with self.assertRaises(ValueError):
                refresh.ingest(self.con, gzip.compress(json.dumps(data).encode()), self.headers, self.root, now=self.now)
        result = self.ingest(); (self.root/result['archive']).write_bytes(b'corrupted')
        with self.assertRaises(ValueError): self.ingest()
        duplicate = json.loads(gzip.decompress(self.raw))*2
        with self.assertRaises(ValueError):
            refresh.ingest(self.con, gzip.compress(json.dumps(duplicate).encode()), self.headers, self.root, now=self.now)
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM catalogue_refresh_events').fetchone()[0], 1)

    def test_fixed_official_host_redirect_and_oversize_refusal_no_retry(self):
        calls = []
        def response(request):
            calls.append(str(request.url))
            return httpx.Response(302, headers={'location':'https://attacker.invalid/master'})
        with httpx.Client(transport=httpx.MockTransport(response)) as client:
            with self.assertRaises(ValueError): refresh.refresh(self.con, self.root, client=client, now=self.now)
        self.assertEqual(calls, [refresh.SOURCE])
        calls.clear()
        def valid(request):
            calls.append(str(request.url)); return httpx.Response(200, headers=self.headers, content=self.raw)
        with httpx.Client(transport=httpx.MockTransport(valid)) as client:
            result = refresh.refresh(self.con, self.root, client=client, now=self.now)
        self.assertEqual(result['cash_contracts'], 1); self.assertEqual(calls, [refresh.SOURCE])
        self.assertEqual(refresh.report(self.con, now=self.now+timedelta(hours=26))['status'], 'stale')


class DailyReviewedRuleJobTest(unittest.TestCase):
    def test_expired_bundle_is_not_successful_and_rolls_back_even_after_import(self):
        from copy import deepcopy
        from tests.test_catalogue_ingestion import EvidenceBundleTest
        from scripts.refresh_contract_catalogue import apply_reviewed
        fixture = EvidenceBundleTest(); fixture.setUp(); self.addCleanup(fixture.doCleanups)
        bundle = deepcopy(fixture.bundle)
        today = fixture.now.astimezone(refresh.IST).date().isoformat(); bundle['source_day'] = today
        apply_reviewed(fixture.con, bundle, fixture.root, today, now=fixture.now)
        before = fixture.con.execute('SELECT COUNT(*) FROM catalogue_imports').fetchone()[0]
        bundle['instruments'][0]['effective_from'] = (fixture.now-timedelta(days=2)).isoformat()
        bundle['instruments'][0]['effective_until'] = (fixture.now-timedelta(days=1)).isoformat()
        # A different, expired identity cannot borrow an earlier valid rule.
        bundle['instruments'][0]['contract']['security_id'] = 'EXPIRED'
        bundle['instruments'][0]['aliases'] = {'upstox':'NSE_EQ|EXPIRED'}
        with self.assertRaises(ValueError): apply_reviewed(fixture.con, bundle, fixture.root, today, now=fixture.now)
        self.assertEqual(fixture.con.execute('SELECT COUNT(*) FROM catalogue_imports').fetchone()[0], before)
        self.assertEqual(fixture.con.execute("SELECT COUNT(*) FROM instrument_contracts WHERE broker_key='NSE_EQ|EXPIRED'").fetchone()[0], 0)
        with self.assertRaises(ValueError): apply_reviewed(fixture.con, fixture.bundle, fixture.root, '1999-01-01', now=fixture.now)
