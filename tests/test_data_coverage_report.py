"""Read-only archive aggregation, including missing evidence and outside-universe holds."""
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from tempfile import TemporaryDirectory
from unittest.mock import patch

from scripts import data_coverage_report as report


DAY = '2026-10-27'
OPEN = datetime(2026, 10, 27, 3, 45, tzinfo=timezone.utc)
SCRIPT = Path(report.__file__).resolve()


def receipt(at=OPEN, lane='full', **kwargs):
    row = dict(market='IN', lane=lane, checked_at=at.isoformat(), status='polled',
               coverage_source='quote_provider_response', requested=100, returned=100, fresh=100,
               missing=0, stale=0, source_counts={'upstox-live': 100}, missing_sample=[], stale_sample=[])
    row.update(kwargs)
    return row


class DataCoverageReportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.archive = self.root / 'quote_feed_coverage'
        self.db = self.root / 'market #1?.db'

    def write_archive(self, rows, lane='full'):
        self.archive.mkdir(exist_ok=True)
        path = self.archive / f'coverage_IN_{lane}_{DAY}.jsonl'
        path.write_text(''.join(json.dumps(r) + '\n' for r in rows))
        return path

    def write_daily(self, **changes):
        market = dict(market='IN', status='complete', target=DAY, requested=100, fresh=100,
                      missing=0, unresolved=0, missing_held=0)
        market.update(changes)
        state = dict(checked_at='2026-10-28T02:00:00+00:00', markets=[market])
        with sqlite3.connect(self.db) as con:
            con.execute('CREATE TABLE agent_state(key TEXT PRIMARY KEY,value TEXT)')
            con.execute('INSERT INTO agent_state VALUES(?,?)', ('daily_candle_ingestion', json.dumps(state)))
        return state

    def run_report(self, *extra):
        output = io.StringIO()
        with redirect_stdout(output):
            code = report.main(['--date', DAY, '--archive-dir', str(self.archive), *extra])
        return code, json.loads(output.getvalue())

    def test_missing_or_empty_archive_is_pending_and_creates_nothing(self):
        code, data = self.run_report()
        self.assertEqual(code, 1)
        self.assertFalse(self.archive.exists())
        for row in data['lanes'].values():
            self.assertEqual(row['status'], 'pending_verification')
            self.assertEqual(row['observations'], 0)
            self.assertIsNone(row['min_fresh_pct'])
            self.assertIsNone(row['first'])
        path = self.write_archive([])
        self.assertEqual(report.lane_report(self.archive, DAY, 'IN', 'full')['status'], 'pending_verification')
        self.assertEqual(path.read_bytes(), b'')

    def test_complete_minute_samples_are_sampled_only_and_lanes_stay_separate(self):
        for lane in ('hot', 'full'):
            self.write_archive([receipt(OPEN + timedelta(minutes=i), lane) for i in range(376)], lane)
        code, data = self.run_report()
        self.assertEqual(code, 0)
        for row in data['lanes'].values():
            self.assertEqual(row['status'], 'sampled_only')
            self.assertEqual(row['observations'], 376)
            self.assertEqual(row['gaps_over_90s'], 0)
            self.assertEqual((row['min_fresh_pct'], row['average_fresh_pct']), (100, 100))
            self.assertEqual(row['first'], OPEN.isoformat())
            self.assertEqual(row['last'], (OPEN + timedelta(minutes=375)).isoformat())

    def test_internal_gaps_and_unobserved_session_edges_are_partial(self):
        rows = [receipt(OPEN + timedelta(minutes=i)) for i in range(376) if i not in (10, 11)]
        self.write_archive(rows)
        summary = report.lane_report(self.archive, DAY, 'IN', 'full')
        self.assertEqual(summary['gaps_over_90s'], 1)
        self.assertEqual(summary['status'], 'partial')
        self.write_archive([receipt(OPEN + timedelta(hours=1))])
        summary = report.lane_report(self.archive, DAY, 'IN', 'full')
        self.assertEqual(summary['gaps_over_90s'], 2)
        self.assertEqual(summary['status'], 'partial')

    def test_percentages_are_per_receipt_and_samples_bounded_across_archive(self):
        rows = [receipt()]
        rows += [receipt(OPEN + timedelta(minutes=i + 1), requested=20, returned=15, fresh=10,
                         missing=5, stale=5, source_counts={'upstox-live': 10, 'upstox-last-traded': 5},
                         missing_sample=[f'MISS{i}'], stale_sample=[f'STALE{i}']) for i in range(12)]
        self.write_archive(rows)
        summary = report.lane_report(self.archive, DAY, 'IN', 'full')
        self.assertEqual(summary['min_fresh_pct'], 50)
        self.assertEqual(summary['average_fresh_pct'], round(700 / 13, 2))
        self.assertEqual(summary['partial_receipts'], 12)
        self.assertEqual((summary['missing_max'], summary['stale_max']), (5, 5))
        self.assertEqual(len(summary['missing_sample']), 10)
        self.assertEqual(len(summary['stale_sample']), 10)
        self.assertNotIn('fresh_symbols', summary)

    def test_malformed_receipts_and_stale_sources_cannot_claim_healthy_samples(self):
        bad = [dict(fresh=True), dict(requested=0), dict(returned=99), dict(checked_at=OPEN.replace(tzinfo=None).isoformat()),
               dict(checked_at=(OPEN - timedelta(minutes=1)).isoformat()), dict(lane='hot'), dict(market='US'),
               dict(source_counts={'upstox-last-traded': 100}), dict(source_counts={'upstox-live': float('nan')}),
               dict(missing_sample=['TOKEN_SECRET']), dict(status='made-up'), dict(coverage_source='local_clock')]
        path = self.write_archive([receipt(**change) for change in bad] + [None, [], {}, receipt(), receipt()])
        with path.open('a') as con:
            con.write('BROKER_TOKEN_SECRET\n')
        _, data = self.run_report()
        row = data['lanes']['IN_full']
        self.assertEqual(row['observations'], 1)
        self.assertEqual(row['invalid_receipts'], len(bad) + 5)
        self.assertEqual(row['status'], 'partial')
        self.assertNotIn('TOKEN_SECRET', json.dumps(data))
        self.write_archive([{}])
        self.assertEqual(report.lane_report(self.archive, DAY, 'IN', 'full')['status'], 'pending_verification')

    def test_fetch_error_and_last_trade_are_partial_without_fresh_relabeling(self):
        rows = [receipt(fresh=0, stale=100, source_counts={'upstox-last-traded': 100}, stale_sample=['COFORGE']),
                receipt(OPEN + timedelta(minutes=1), status='fetch_error', coverage_source='quote_provider_error',
                        returned=0, fresh=0, missing=100, source_counts={}, missing_sample=['COFORGE'])]
        path = self.write_archive(rows)
        before = path.read_bytes()
        summary = report.lane_report(self.archive, DAY, 'IN', 'full')
        self.assertEqual((summary['min_fresh_pct'], summary['average_fresh_pct']), (0, 0))
        self.assertEqual(summary['partial_receipts'], 2)
        self.assertEqual(summary['missing_sample'], ['COFORGE'])
        self.assertEqual(summary['stale_sample'], ['COFORGE'])
        self.assertEqual(path.read_bytes(), before)

    def test_daily_partial_held_outside_requested_universe_is_valid(self):
        self.write_daily(status='partial', missing_held=3)
        before = self.db.read_bytes()
        summary = report.daily_report(self.db, DAY)
        self.assertEqual(summary['status'], 'partial')
        self.assertEqual(summary['markets'][0]['missing'], 0)
        self.assertEqual(summary['markets'][0]['missing_held'], 3)
        self.assertEqual(summary['checked_at'], '2026-10-28T02:00:00+00:00')
        self.assertEqual(self.db.read_bytes(), before)

    def test_daily_missing_file_state_or_malformed_state_never_looks_recorded(self):
        self.assertEqual(report.daily_report(self.db, DAY)['status'], 'unavailable_or_malformed')
        self.assertFalse(self.db.exists())
        self.write_daily()
        self.assertEqual(report.daily_report(self.db, DAY)['status'], 'recorded')
        self.assertEqual(report.daily_report(self.db, '2026-10-28')['markets'][0]['status'], 'date_mismatch')
        with sqlite3.connect(self.db) as con:
            con.execute('DELETE FROM agent_state')
        self.assertEqual(report.daily_report(self.db, DAY)['status'], 'pending_verification')
        with sqlite3.connect(self.db) as con:
            con.execute('INSERT INTO agent_state VALUES(?,?)', ('daily_candle_ingestion', 'TOKEN_SECRET'))
        summary = report.daily_report(self.db, DAY)
        self.assertEqual(summary['status'], 'unavailable_or_malformed')
        self.assertNotIn('TOKEN_SECRET', json.dumps(summary))

    def test_cli_default_tracks_feed_env_is_read_only_and_imports_no_strategy(self):
        self.write_archive([receipt()])
        self.write_daily(missing_held=2, status='partial')
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.rglob('*') if p.is_file()}
        env = dict(os.environ, OPENSTOCKS_DB=str(self.root / 'main.db'))
        env.pop('QUOTE_FEED_COVERAGE_DIR', None)
        result = subprocess.run([sys.executable, '-B', str(SCRIPT), '--date', DAY, '--db', str(self.db)],
                                cwd=self.root, env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 1, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(data['lanes']['IN_full']['observations'], 1)
        self.assertEqual(data['daily_candle_ingestion']['status'], 'partial')
        self.assertEqual(before, {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.rglob('*') if p.is_file()})
        check = "import sys; from scripts import data_coverage_report; assert not any(m in sys.modules for m in ('app.config','app.db','app.v2_live','scripts.v2_quote_feed','app.market_data'))"
        subprocess.run([sys.executable, '-B', '-c', check], cwd=SCRIPT.parents[1], check=True, capture_output=True)

    def test_date_is_strict_and_script_stays_within_requested_limit(self):
        for day in ('20261027', '2026-10-99', '2026-1-1'):
            with self.subTest(day=day), redirect_stdout(io.StringIO()), patch('sys.stderr', new=io.StringIO()):
                with self.assertRaises(SystemExit) as result:
                    report.main(['--date', day])
                self.assertEqual(result.exception.code, 2)
        self.assertLessEqual(len(SCRIPT.read_text().splitlines()), 150)


if __name__ == '__main__':
    unittest.main()
