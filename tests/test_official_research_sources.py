"""Synthetic source contracts; these are not licensed or market fixtures."""
from datetime import datetime, timedelta, timezone
import csv
import gzip
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

import httpx
from app import official_research as sources, research_data
from scripts.capture_official_research import main


class OfficialResearchSourceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir='/private/tmp') if Path('/private/tmp').is_dir() else tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.con = sqlite3.connect(':memory:')
        self.addCleanup(self.con.close)
        self.now = datetime(2026, 1, 5, 12, tzinfo=timezone.utc)
        self.day = '2026-01-05'

    def master_row(self, symbol='TEST', isin='INE000000010', token=10):
        row = ['0']*54
        row[0:7] = [str(token), symbol, 'EQ', '0', '100000', '1', '']
        row[7:19] = ['2', '1']+['2', '0']*5
        row[19:23] = ['1', '5', 'SYNTHETIC fixture only', '0']
        row[26], row[37], row[48], row[51], row[52], row[53] = '41', '1', '1', 'N', '10', isin
        return row

    def master(self, rows=None):
        seconds = int((self.now.replace(hour=9)-datetime(1980, 1, 1, tzinfo=timezone.utc)).total_seconds())
        return gzip.compress(('NEATCM|01.00.00|'+str(seconds)+'\n'+
                              '\n'.join('|'.join(row) for row in (rows or [self.master_row()]))+'\n').encode())

    def metadata(self, feed, raw, now=None, day=None):
        now, day = now or self.now, day or self.day
        source = sources.MASTER_SPEC if feed == 'master' else sources.MEMBERSHIP_URL if feed == 'nifty100' else \
                 sources.DELIVERY_PREFIX+datetime.fromisoformat(day).strftime('%d%m%Y')+'.csv'
        result = dict(source=source, source_day=day, observed_at=now.isoformat(),
                      sha256=hashlib.sha256(raw).hexdigest(), rights_reference='SYNTHETIC; no commercial permission')
        if feed == 'master':
            result.update(layout=sources.LAYOUT, review_reference='synthetic-layout-review',
                          delivery_reference='synthetic licensed delivery receipt', epoch_timezone='UTC',
                          published_at=self.now.replace(hour=10).isoformat(), header_version='01.00.00')
        return result

    def ingest(self, feed, raw, now=None, metadata=None):
        now = now or self.now
        return sources.ingest(self.con, feed, raw, self.root/'archive',
                              metadata or self.metadata(feed, raw, now), now=now)

    def csv(self, columns, rows):
        output = io.StringIO()
        writer = csv.writer(output); writer.writerow(columns); writer.writerows(rows)
        return output.getvalue().encode()

    def membership(self, shift=0):
        return self.csv(['Company Name', 'Industry', 'Symbol', 'Series', 'ISIN Code'],
                        [['SYNTHETIC '+str(i), 'Synthetic sector', 'TEST'+str(i), 'EQ', f'INE{i:08d}0']
                         for i in range(shift, 100+shift)])

    def delivery(self, rows=None):
        return self.csv([' SYMBOL', ' SERIES', ' DATE1', ' TTL_TRD_QNTY', ' DELIV_QTY', ' DELIV_PER'],
                        rows or [['TEST', 'EQ', '05-Jan-2026', '1000', '600', '60.00']])

    def test_licensed_identity_units_and_settlement_do_not_enable_orders(self):
        raw = self.master()
        result = self.ingest('master', raw)
        spec = json.loads(self.con.execute('SELECT payload FROM instrument_contracts').fetchone()[0])
        self.assertEqual(spec['tick_size'], '0.05')
        self.assertEqual(spec['lot_size'], 1)
        self.assertEqual(spec['settlement'], 'T+1')
        self.assertIsNone(spec['freeze_quantity'])
        self.assertFalse(spec['tradable'])
        self.assertFalse(result['execution_approved'])
        self.assertFalse(result['commercial_rights_verified'])
        self.assertEqual(self.ingest('master', raw), result)
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM research_source_events').fetchone()[0], 1)

    def test_schema_receipt_and_raw_evidence_are_immutable(self):
        result = self.ingest('master', self.master())
        archive = self.root/'archive'/result['archive']
        self.assertEqual(hashlib.sha256(archive.read_bytes()).hexdigest(), result['sha256'])
        self.assertEqual(archive.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.con.execute('SELECT version FROM schema_migration_receipts').fetchone()[0], 'research-source-schema-v1')
        for sql in ('DELETE FROM research_source_events', "UPDATE research_source_events SET status='captured'"):
            with self.assertRaises(sqlite3.IntegrityError):
                self.con.execute(sql)
            self.con.rollback()

    def test_unknown_layout_timezone_and_mii_csv_are_not_licensed_master(self):
        raw = self.master()
        for changes in ({'epoch_timezone': ''}, {'layout': 'public-mii-csv'}, {'header_version': '02.00.00'}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.ingest('master', raw, metadata=dict(self.metadata('master', raw), **changes))
        with self.assertRaises(ValueError):
            self.ingest('master', b'TOKEN,SYMBOL,SERIES\n10,TEST,EQ\n')
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM instrument_snapshots').fetchone()[0], 0)

    def test_changed_width_duplicate_tokens_and_ambiguous_isins_refuse(self):
        cases = [[self.master_row()[:-1]], [self.master_row(), self.master_row('OTHER')],
                 [self.master_row(), self.master_row('OTHER', token=11)]]
        for index, rows in enumerate(cases):
            with self.subTest(index=index), self.assertRaises(ValueError):
                self.ingest('master', self.master(rows), now=self.now+timedelta(seconds=index))
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM instrument_contracts').fetchone()[0], 0)

    def test_suspensions_deletions_and_bse_contingency_are_excluded(self):
        active = self.master_row()
        blocked = []
        for i, (field, value) in enumerate(((5, '2'), (51, 'Y'), (7, '3'), (8, '0'), (48, '2'))):
            row = self.master_row('BLOCK'+str(i), f'INE{i+100:08d}0', token=20+i)
            row[field] = value; blocked.append(row)
        result = self.ingest('master', self.master([active]+blocked))
        self.assertEqual(result['identities'], 1)
        self.assertEqual(result['excluded'], 5)

    def test_delivery_binds_exact_dated_master_and_stays_unknown_before_capture(self):
        self.ingest('master', self.master())
        later = self.now+timedelta(seconds=1)
        result = self.ingest('delivery', self.delivery(), now=later)
        self.assertEqual(result['matched'], 1)
        identity = self.con.execute('SELECT instrument_id FROM research_facts').fetchone()[0]
        self.assertEqual(research_data.known(self.con, identity, 'delivery', self.now.isoformat()), [])
        fact = research_data.known(self.con, identity, 'delivery', later.isoformat())[0]
        self.assertEqual(fact['payload']['delivery_pct'], '60.00')
        self.assertFalse(fact['payload']['corporate_action_adjusted'])
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM research_facts WHERE kind='bar'").fetchone()[0], 0)

    def test_missing_or_differently_dated_identity_is_explicit_not_ticker_inference(self):
        result = self.ingest('delivery', self.delivery())
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['unmatched'], 1)
        self.assertEqual(result['facts'], 0)
        self.ingest('master', self.master())
        other = self.delivery([['TEST', 'EQ', '02-Jan-2026', '1000', '600', '60']])
        result = self.ingest('delivery', other, metadata=self.metadata('delivery', other, day='2026-01-02'))
        self.assertEqual(result['unmatched'], 1)

    def test_failed_newer_master_does_not_borrow_older_identity_coverage(self):
        self.ingest('master', self.master())
        with self.assertRaises(ValueError):
            self.ingest('master', self.master([self.master_row()[:-1]]), now=self.now+timedelta(seconds=1))
        result = self.ingest('delivery', self.delivery(), now=self.now+timedelta(seconds=2))
        self.assertEqual(result['unmatched'], 1)
        self.assertEqual(sources.report(self.con, now=self.now+timedelta(seconds=2))['feeds']['master']['status'], 'failed')

    def test_contemporaneous_different_masters_refuse_until_newer_capture(self):
        self.ingest('master', self.master())
        self.ingest('master', self.master([self.master_row('OTHER', 'INE000000020', 11)]))
        self.assertEqual(sources.report(self.con, now=self.now)['feeds']['master']['status'], 'conflicting')
        result = self.ingest('delivery', self.delivery(), now=self.now+timedelta(seconds=1))
        self.assertEqual(result['facts'], 0)
        self.ingest('master', self.master(), now=self.now+timedelta(seconds=2))
        result = self.ingest('delivery', self.delivery(), now=self.now+timedelta(seconds=3))
        self.assertEqual(result['matched'], 1)

    def test_bad_delivery_date_duplicate_and_inconsistent_quantities_refuse_atomically(self):
        self.ingest('master', self.master())
        cases = [[['TEST', 'EQ', '06-Jan-2026', '1000', '600', '60']],
                 [['TEST', 'EQ', '05-Jan-2026', '1000', '600', '60']]*2,
                 [['TEST', 'EQ', '05-Jan-2026', '1000', '600', '90']],
                 [['TEST', 'EQ', '05-Jan-2026', '1000', '1200', '120']],
                 [['TEST', 'EQ', '05-Jan-2026', '1000', '600', 'NaN']]]
        for i, rows in enumerate(cases):
            with self.subTest(i=i), self.assertRaises(ValueError):
                self.ingest('delivery', self.delivery(rows), now=self.now+timedelta(seconds=i+1))
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM research_facts').fetchone()[0], 0)

    def test_missing_delivery_values_remain_missing_and_closed_session_required(self):
        result = self.ingest('delivery', self.delivery([['TEST', 'EQ', '05-Jan-2026', '1000', '-', '-']]))
        self.assertEqual(result['unavailable'], 1)
        self.assertEqual(result['status'], 'partial')
        early = self.now.replace(hour=9)
        with self.assertRaises(ValueError):
            self.ingest('delivery', self.delivery(), now=early)

    def test_full_current_membership_removals_and_replay_preserve_original_facts(self):
        raw = self.membership()
        first = self.ingest('nifty100', raw)
        self.assertEqual(first['members'], 100)
        self.assertEqual(self.ingest('nifty100', raw), first)
        identity = self.con.execute('SELECT instrument_id FROM research_facts ORDER BY rowid LIMIT 1').fetchone()[0]
        later = self.now+timedelta(seconds=1)
        raw = self.membership(1)
        result = self.ingest('nifty100', raw, now=later)
        self.assertEqual(result['removals'], 1)
        self.assertEqual(self.ingest('nifty100', raw, now=later), result)
        self.assertTrue(research_data.known(self.con, identity, 'membership', self.now.isoformat())[0]['payload']['member'])
        self.assertFalse(research_data.known(self.con, identity, 'membership', later.isoformat())[0]['payload']['member'])
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM research_facts').fetchone()[0], 201)

    def test_membership_missing_duplicate_columns_and_backdating_refuse(self):
        raw = self.membership()
        for bad in (raw.replace(b'Company Name,Industry', b'Industry,Industry'), b'Company Name,Industry,Symbol,Series,ISIN Code\n'):
            with self.assertRaises(ValueError):
                self.ingest('nifty100', bad)
        with self.assertRaises(ValueError):
            self.ingest('nifty100', raw, metadata=self.metadata('nifty100', raw, day='2026-01-02'))

    def test_raw_digest_mismatch_corrupt_archive_and_source_substitution_refuse(self):
        raw = self.master()
        for changes in ({'sha256': '0'*64}, {'source': 'https://untrusted.invalid/master'}, {'rights_reference': ''}):
            with self.assertRaises(ValueError):
                self.ingest('master', raw, metadata=dict(self.metadata('master', raw), **changes))
        result = self.ingest('master', raw)
        (self.root/'archive'/result['archive']).write_bytes(b'corrupt fixture')
        with self.assertRaisesRegex(ValueError, 'digest'):
            self.ingest('master', raw)

    def test_source_failure_and_redirect_are_recorded_without_response_body(self):
        calls = []
        def transport(request):
            calls.append(str(request.url))
            return httpx.Response(302, headers={'location': 'https://untrusted.invalid'}, text='PRIVATE_RESPONSE_MARKER')
        with httpx.Client(transport=httpx.MockTransport(transport)) as client:
            with self.assertRaises(ValueError):
                sources.refresh(self.con, self.root/'archive', 'nifty100',
                                rights_reference='synthetic', client=client, now=self.now)
        self.assertEqual(calls, [sources.MEMBERSHIP_URL])
        report = sources.report(self.con, now=self.now)
        self.assertEqual(report['feeds']['nifty100']['status'], 'failed')
        self.assertNotIn('PRIVATE_RESPONSE_MARKER', json.dumps(report))

    def test_fixed_source_fetch_archives_actual_bytes_and_exposes_missing_coverage(self):
        with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=self.membership()))) as client:
            result = sources.refresh(self.con, self.root/'archive', 'nifty100',
                                     rights_reference='synthetic', client=client, now=self.now)
        self.assertEqual(result['members'], 100)
        self.assertFalse(sources.report(self.con, now=self.now)['model_promoted'])
        report = sources.report(self.con, now=self.now+timedelta(days=8))
        self.assertTrue(report['feeds']['nifty100']['stale'])
        self.assertEqual(report['feeds']['master']['status'], 'missing')

    def test_parse_failure_has_one_receipt_and_does_not_repeat_or_modify_it(self):
        with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b'INVALID SYNTHETIC CSV'))) as client:
            for _ in range(2):
                with self.assertRaises(ValueError):
                    sources.refresh(self.con, self.root/'archive', 'nifty100',
                                    rights_reference='synthetic', client=client, now=self.now)
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM research_source_events').fetchone()[0], 1)

    def test_production_book_database_is_rejected_before_source_writes(self):
        self.con.execute('CREATE TABLE user_book(user_id INTEGER,cash REAL)')
        self.con.execute('INSERT INTO user_book VALUES(2,10000)'); self.con.commit()
        before = self.con.iterdump()
        original = '\n'.join(before)
        with self.assertRaisesRegex(ValueError, 'dedicated'):
            self.ingest('master', self.master())
        self.assertEqual('\n'.join(self.con.iterdump()), original)
        self.assertFalse((self.root/'archive').exists())

    def test_forward_experiment_database_is_rejected_before_source_writes(self):
        self.con.execute('CREATE TABLE assessment_events(id TEXT,payload TEXT)')
        self.con.execute("INSERT INTO assessment_events VALUES('synthetic','unchanged')"); self.con.commit()
        original = '\n'.join(self.con.iterdump())
        with self.assertRaisesRegex(ValueError, 'dedicated'):
            self.ingest('master', self.master())
        self.assertEqual('\n'.join(self.con.iterdump()), original)

    def test_gzip_bomb_and_archive_symlinks_refuse(self):
        from unittest.mock import patch
        with patch.object(sources, 'MAX_EXPANDED', 10), self.assertRaises(ValueError):
            self.ingest('master', self.master())
        other = self.root/'elsewhere'
        other.mkdir()
        (self.root/'linked').symlink_to(other, target_is_directory=True)
        with self.assertRaises(ValueError):
            sources.ingest(self.con, 'master', self.master(), self.root/'linked',
                           self.metadata('master', self.master()), now=self.now)
        self.assertEqual(list(other.iterdir()), [])

    def test_cli_report_is_read_only_and_does_not_create_missing_database(self):
        missing = self.root/'missing.db'
        from contextlib import redirect_stdout
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(['--db', str(missing), 'report']), 1)
        self.assertFalse(missing.exists())
        path = self.root/'evidence.db'
        from contextlib import closing
        with closing(sqlite3.connect(path)) as con:
            sources.ensure_schema(con)
        before = path.read_bytes()
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(['--db', str(path), 'report']), 2)
        self.assertEqual(path.read_bytes(), before)
        self.assertNotIn(str(self.root), output.getvalue())


if __name__ == '__main__':
    unittest.main()
