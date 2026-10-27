"""Feed coverage regressions; disposable journals and synthetic broker responses."""
import io
import json
import sqlite3
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, Mock, patch

import httpx

from app.config import Settings
from app.db import Database
from app.market_data import UpstoxMarketDataProvider
from app.models import Quote
from app.screening import automation, tracking
from scripts import v2_quote_feed as feed


NOW = datetime(2026, 10, 27, 5, tzinfo=timezone.utc)


def rows(*symbols):
    return [dict(symbol=s, upstox_instrument_key='NSE_EQ|' + s) for s in symbols]


def quote(symbol, age=0, source='upstox-live'):
    return Quote(symbol, 100, source, (NOW - timedelta(seconds=age)).isoformat())


def publish(path, symbol):
    plan = dict(symbol=symbol, price_asof='2026-10-26', qty=1,
                entry_low=99, entry_high=100, stop=95, t1=110, t2=115, t3=120)
    tracking.publish(path, 1, [plan], now=NOW)


class QuoteFeedCoverageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.research = Path(self.tmp.name) / 'research.db'
        self.auto = Path(self.tmp.name) / 'automation.db'
        self.archive = Path(self.tmp.name) / 'quote_feed_coverage'
        for patcher in (
            patch.object(tracking, 'default_path', return_value=str(self.research)),
            patch.object(automation, 'default_path', return_value=str(self.auto)),
            patch.object(feed, '_cooldown', {}),
            patch.object(feed, '_coverage_gaps', {}),
            patch.object(feed, '_coverage_archive_last', {}),
            patch.object(feed, '_coverage_warnings', set()),
            patch.dict('os.environ', {'QUOTE_FEED_COVERAGE_DIR': str(self.archive)}),
            patch.object(feed, 'MARKETS', {'IN': 'upstox'}),
            patch.object(feed.market_regions, 'market_session_for_region', return_value={'is_open': True}),
            patch.object(feed, 'datetime', wraps=datetime),
        ):
            mocked = patcher.start()
            self.addCleanup(patcher.stop)
        feed.datetime.now.return_value = NOW

    def test_automation_only_publication_is_hot_without_ui_visit_or_position(self):
        publish(self.auto, 'COFORGE')
        before = self.auto.read_bytes()
        with patch.object(feed, 'WATCH_HOT', {}):
            hot = feed._hot_rows({'IN': {r['symbol']: r for r in rows('COFORGE')}}, {}, feed._tracked())
        self.assertEqual(hot['IN'], rows('COFORGE'))
        self.assertFalse(self.research.exists())
        self.assertEqual(self.auto.read_bytes(), before)

    def test_journals_union_deduplicates_and_terminal_plans_leave_hot_watch(self):
        publish(self.research, 'BOTH')
        publish(self.auto, 'BOTH')
        publish(self.auto, 'COFORGE')
        with sqlite3.connect(self.auto) as con:
            con.execute("UPDATE states SET payload=? WHERE publication_id IN "
                        "(SELECT id FROM publications WHERE symbol='COFORGE')", ('{"status":"STOPPED"}',))
        self.assertEqual(feed._tracked(), {'BOTH'})

    def test_bad_research_journal_cannot_hide_automation_and_vice_versa(self):
        for bad, good in ((self.research, self.auto), (self.auto, self.research)):
            with self.subTest(bad=bad.name):
                for path in (self.research, self.auto):
                    if path.exists():
                        path.unlink()
                with sqlite3.connect(bad) as con:
                    con.execute('CREATE TABLE unrelated(x)')
                publish(good, 'COFORGE')
                before = bad.read_bytes()
                self.assertEqual(feed._tracked(), {'COFORGE'})
                self.assertEqual(bad.read_bytes(), before)

    def test_full_worker_refreshes_startup_singleton_and_keeps_previous_on_read_failure(self):
        db = Mock()
        initial = {'IN': rows('RELIANCE')}
        expanded = rows('RELIANCE', 'COFORGE', 'TECHM')
        db.get_universe.return_value = expanded
        tick = [0]
        def sleep(_):
            if tick[0]:
                raise RuntimeError('end fixture')
            tick[0] = 61
        with patch.object(feed, '_build', return_value=(db, {'IN': Mock()}, initial, {})), \
             patch.object(feed, '_poll') as poll, \
             patch.object(feed.threading, 'Thread'), \
             patch.object(feed.sys, 'argv', ['quote-feed', '--loop', '--no-nfo']), \
             patch.object(feed.time, 'time', side_effect=lambda: tick[0]), \
             patch.object(feed.time, 'monotonic', side_effect=lambda: tick[0]), \
             patch.object(feed.time, 'sleep', side_effect=sleep):
            with self.assertRaisesRegex(RuntimeError, 'end fixture'):
                feed.main()
        self.assertEqual(poll.call_args_list[0].args[2], initial)
        self.assertEqual(poll.call_args_list[1].args[2], {'IN': expanded})
        db.get_universe.assert_called_once_with(enabled_only=True, market_region='IN')
        db.get_universe.side_effect = sqlite3.OperationalError('unavailable')
        self.assertEqual(feed._refresh_full_rows(db, {'IN': expanded}), {'IN': expanded})

    def poll(self, responses, requested=None, **kwargs):
        provider = Mock(execution_quotes={}, last_quote_diagnostics={})
        provider.get_quotes = AsyncMock(side_effect=responses)
        db = Mock()
        report = feed._poll(db, {'IN': provider}, {'IN': requested or rows('RELIANCE', 'COFORGE')}, '', **kwargs)
        return db, provider, report['IN']

    def test_partial_response_retries_only_missing_and_delivers_first_before_retry(self):
        first, second = quote('RELIANCE'), quote('COFORGE')
        db = Mock()
        async def fetch(requested):
            if requested == rows('COFORGE'):
                db.upsert_quotes.assert_called_once_with({'RELIANCE': first})
                return {'COFORGE': second}
            return {'RELIANCE': first}
        provider = Mock(execution_quotes={}, last_quote_diagnostics={})
        provider.get_quotes = AsyncMock(side_effect=fetch)
        report = feed._poll(db, {'IN': provider}, {'IN': rows('RELIANCE', 'COFORGE')}, '')
        self.assertEqual(provider.get_quotes.call_count, 2)
        self.assertEqual(provider.get_quotes.call_args_list[1].args[0], rows('COFORGE'))
        got = report['IN']
        self.assertEqual(got['fresh_symbols'], ['COFORGE', 'RELIANCE'])
        self.assertEqual(got['missing_symbols'], [])
        self.assertIs(db.upsert_quotes.call_args_list[1].args[0]['COFORGE'], second)

    def test_retry_failure_keeps_delivered_quotes_and_reports_actual_gap(self):
        first = quote('RELIANCE')
        db, provider, got = self.poll([{'RELIANCE': first}, RuntimeError('timeout')])
        self.assertEqual(provider.get_quotes.call_count, 2)
        db.upsert_quotes.assert_called_once_with({'RELIANCE': first})
        self.assertEqual(got['missing_symbols'], ['COFORGE'])
        self.assertEqual(got['fresh_symbols'], ['RELIANCE'])

    def test_total_fetch_failure_reports_every_requested_symbol_missing(self):
        db, provider, got = self.poll([RuntimeError('no quotes returned')])
        self.assertEqual(got['missing_symbols'], ['COFORGE', 'RELIANCE'])
        self.assertEqual(got['fresh_symbols'], [])
        db.upsert_quotes.assert_not_called()
        self.assertEqual(provider.get_quotes.call_count, 1)

    def test_broad_stdout_and_receipt_are_bounded_but_return_remains_detailed(self):
        fresh = [f'FRESH{i:04d}' for i in range(2650)]
        missing = [f'MISSING{i:02d}' for i in range(12)]
        stale = [f'STALE{i:02d}' for i in range(12)]
        quotes = {s: quote(s) for s in fresh}
        quotes.update({s: quote(s, 121, 'upstox-last-traded') for s in stale})
        provider = Mock(execution_quotes={}, last_quote_diagnostics={})
        provider.get_quotes = AsyncMock(side_effect=[quotes, {}])
        db = Mock()
        output = io.StringIO()
        with redirect_stdout(output):
            report = feed._poll(db, {'IN': provider}, {'IN': rows(*(fresh + missing + stale))}, 'full')['IN']
        printed = output.getvalue()
        self.assertNotIn('fresh_symbols', printed)
        self.assertNotIn('FRESH0000', printed)
        self.assertLess(len(printed), 1200)
        summary = json.loads(printed.split('coverage=', 1)[1])
        self.assertEqual((summary['requested'], summary['returned'], summary['fresh'],
                          summary['missing'], summary['stale']), (2674, 2662, 2650, 12, 12))
        self.assertEqual(summary['missing_sample'], missing[:10])
        self.assertEqual(summary['stale_sample'], stale[:10])
        self.assertEqual(summary['source_counts'], {'upstox-live': 2650, 'upstox-last-traded': 12})
        self.assertEqual(len(report['fresh_symbols']), 2650)
        self.assertEqual(report['missing_symbols'], missing)
        self.assertEqual(report['stale_symbols'], stale)
        db.set_state.assert_called_once_with('quote_feed_coverage_IN_full', summary)

    def test_receipts_persist_with_lane_keys_and_checked_at_without_rewriting_quote_times(self):
        path = Path(self.tmp.name) / 'market.db'
        with sqlite3.connect(path) as con:
            con.execute('CREATE TABLE agent_state(key TEXT PRIMARY KEY,value TEXT)')
        db = Database(path)
        original = quote('COFORGE', 121, 'upstox-last-traded')
        provider = Mock(execution_quotes={}, last_quote_diagnostics={})
        provider.get_quotes = AsyncMock(return_value={'COFORGE': original})
        with patch.object(db, 'upsert_quotes') as deliver:
            feed._poll(db, {'IN': provider}, {'IN': rows('COFORGE')}, 'full')
            feed._poll(db, {'IN': provider}, {'IN': rows('COFORGE')}, '', benchmarks=True)
        for lane in ('hot', 'full'):
            receipt = Database(path).get_state('quote_feed_coverage_IN_' + lane)
            self.assertEqual(receipt['checked_at'], NOW.isoformat())
            self.assertEqual(receipt['coverage_source'], 'quote_provider_response')
            self.assertEqual(receipt['status'], 'polled')
            self.assertEqual(receipt['source_counts'], {'upstox-last-traded': 1})
            self.assertEqual((receipt['fresh'], receipt['stale'], receipt['missing']), (0, 1, 0))
        for call in deliver.call_args_list:
            self.assertIs(call.args[0]['COFORGE'], original)
            self.assertEqual(call.args[0]['COFORGE'].asof, (NOW - timedelta(seconds=121)).isoformat())

    def test_telemetry_failures_cannot_erase_delivery_or_detailed_coverage(self):
        original = quote('COFORGE', 120)
        for failing_sink in ('state', 'archive'):
            with self.subTest(sink=failing_sink):
                db = Mock()
                if failing_sink == 'state':
                    db.set_state.side_effect = sqlite3.OperationalError('locked')
                provider = Mock(execution_quotes={}, last_quote_diagnostics={})
                provider.get_quotes = AsyncMock(return_value={'COFORGE': original})
                with patch.object(feed, '_archive_coverage',
                                  side_effect=OSError('disk full') if failing_sink == 'archive' else None) as archive:
                    report = feed._poll(db, {'IN': provider}, {'IN': rows('COFORGE')}, '')['IN']
                self.assertEqual(report['fresh_symbols'], ['COFORGE'])
                self.assertEqual(report['missing_symbols'], [])
                self.assertIs(db.upsert_quotes.call_args.args[0]['COFORGE'], original)
                self.assertEqual(original.asof, (NOW - timedelta(seconds=120)).isoformat())
                db.set_state.assert_called_once()
                archive.assert_called_once()
                self.assertEqual(db.set_state.call_args.args[1]['fresh'], 1)

    def test_total_failure_receipt_has_zero_sources_and_bounded_missing_sample(self):
        requested = rows(*(f'MISSING{i:02d}' for i in range(25)))
        output = io.StringIO()
        with redirect_stdout(output):
            db, _, report = self.poll([RuntimeError('network unavailable')], requested)
        receipt = db.set_state.call_args.args[1]
        self.assertEqual(receipt['status'], 'fetch_error')
        self.assertEqual(receipt['checked_at'], NOW.isoformat())
        self.assertEqual(receipt['source_counts'], {})
        self.assertEqual((receipt['requested'], receipt['returned'], receipt['missing']), (25, 0, 25))
        self.assertEqual(len(receipt['missing_sample']), 10)
        self.assertEqual(len(report['missing_symbols']), 25)
        self.assertNotIn('MISSING24', output.getvalue())
        self.assertNotIn('fresh_symbols', output.getvalue())

    def test_archive_is_periodic_per_lane_and_closed_hours_are_not_samples(self):
        summary = feed._coverage_summary(feed._quote_coverage('IN', rows('X'), {'X': quote('X')}, NOW), NOW, 'polled')
        with patch.object(feed.time, 'monotonic', side_effect=[0, 59, 59, 60]):
            feed._archive_coverage('IN', 'full', summary, NOW)
            feed._archive_coverage('IN', 'full', summary, NOW + timedelta(seconds=59))
            feed._archive_coverage('IN', 'hot', summary, NOW + timedelta(seconds=59))
            feed._archive_coverage('IN', 'full', summary, NOW + timedelta(seconds=60))
        full = self.archive / 'coverage_IN_full_2026-10-27.jsonl'
        hot = self.archive / 'coverage_IN_hot_2026-10-27.jsonl'
        self.assertEqual(len(full.read_text().splitlines()), 2)
        self.assertEqual(len(hot.read_text().splitlines()), 1)
        record = json.loads(hot.read_text())
        self.assertEqual(record, dict(summary, market='IN', lane='hot'))
        with patch.object(feed.market_regions, 'market_session_for_region', return_value={'is_open': False}):
            feed._archive_coverage('IN', 'full', summary, NOW + timedelta(hours=12))
        self.assertEqual(len(full.read_text().splitlines()), 2)

    def test_archive_retention_and_size_caps(self):
        self.archive.mkdir()
        for i in range(1, 27):
            (self.archive / f'coverage_IN_full_2026-10-{i:02d}.jsonl').write_text('{}\n')
        unrelated = self.archive / 'unrelated.jsonl'
        unrelated.write_text('keep')
        summary = feed._coverage_summary(feed._quote_coverage('IN', rows('X'), {'X': quote('X')}, NOW), NOW, 'polled')
        with patch.object(feed.time, 'monotonic', side_effect=[0, 60]), \
             patch.object(feed, 'COVERAGE_ARCHIVE_MAX_BYTES', 1024):
            feed._archive_coverage('IN', 'full', summary, NOW)
            current = self.archive / 'coverage_IN_full_2026-10-27.jsonl'
            self.assertEqual(len(list(self.archive.glob('coverage_IN_full_*.jsonl'))), 14)
            current.write_bytes(b'x' * 1024)
            feed._archive_coverage('IN', 'full', summary, NOW + timedelta(seconds=60))
            self.assertEqual(current.stat().st_size, 1024)
        self.assertEqual(unrelated.read_text(), 'keep')

    def test_market_close_during_first_fetch_prevents_retry(self):
        with patch.object(feed.market_regions, 'market_session_for_region',
                          side_effect=[{'is_open': True}, {'is_open': False}, {'is_open': True}, {'is_open': False}]):
            _, provider, got = self.poll([{'RELIANCE': quote('RELIANCE')}])
        self.assertEqual(provider.get_quotes.call_count, 1)
        self.assertEqual(got['missing_symbols'], ['COFORGE'])

    def test_retry_is_one_batch_maximum_even_if_still_partial(self):
        requested = rows('RELIANCE', *(f'S{i}' for i in range(210)))
        _, provider, got = self.poll([{'RELIANCE': quote('RELIANCE')}, {}], requested)
        self.assertEqual(provider.get_quotes.call_count, 2)
        self.assertEqual(len(provider.get_quotes.call_args_list[1].args[0]), 100)
        self.assertEqual(len(got['missing_symbols']), 210)

    def test_stale_last_trade_and_stale_live_label_are_not_fresh_or_retried(self):
        stale = quote('COFORGE', 121)
        last_trade = quote('RELIANCE', 3600, 'upstox-last-traded')
        db, provider, got = self.poll([{'COFORGE': stale, 'RELIANCE': last_trade}])
        self.assertEqual(provider.get_quotes.call_count, 1)
        self.assertEqual(got['fresh_symbols'], [])
        self.assertEqual(got['stale_symbols'], ['COFORGE', 'RELIANCE'])
        self.assertEqual(db.upsert_quotes.call_args.args[0], {'COFORGE': stale, 'RELIANCE': last_trade})

    def test_future_invalid_and_nonregular_source_quotes_cannot_claim_coverage(self):
        qs = {'FUTURE': quote('FUTURE', -1), 'NAN': replace(quote('NAN'), price=float('nan')),
              'BAD': replace(quote('BAD'), asof='bad'), 'WRONG': quote('WRONG', source='yahoo')}
        self.assertEqual(feed._quote_coverage('IN', rows(*qs), qs, NOW)['fresh_symbols'], [])
        with patch.object(feed.market_regions, 'market_session_for_region', return_value={'is_open': False}):
            self.assertEqual(feed._quote_coverage('IN', rows('X'), {'X': quote('X')}, NOW)['fresh_symbols'], [])

    def test_rate_limit_in_partial_provider_diagnostics_skips_retry_and_cools_both_lanes(self):
        provider = Mock(execution_quotes={}, last_quote_diagnostics={'errors': ['HTTP 429']})
        provider.get_quotes = AsyncMock(return_value={'RELIANCE': quote('RELIANCE')})
        db = Mock()
        with patch.object(feed.time, 'time', return_value=100):
            got = feed._poll(db, {'IN': provider}, {'IN': rows('RELIANCE', 'COFORGE')}, '')
            feed._poll(db, {'IN': provider}, {'IN': rows('RELIANCE', 'COFORGE')}, '')
        self.assertEqual(provider.get_quotes.call_count, 1)
        self.assertEqual(feed._cooldown['IN'], 145)
        self.assertEqual(got['IN']['missing_symbols'], ['COFORGE'])
        db.set_state.assert_called_once()

    def test_retry_rate_limit_preserves_first_delivery_and_sets_cooldown(self):
        db, provider, got = self.poll([{'RELIANCE': quote('RELIANCE')}, RuntimeError('UDAPI10005')])
        self.assertIn('IN', feed._cooldown)
        self.assertEqual(got['missing_symbols'], ['COFORGE'])
        self.assertEqual(db.upsert_quotes.call_count, 1)

    def test_closed_market_makes_no_requests_or_coverage_claim(self):
        provider = Mock()
        db = Mock()
        with patch.object(feed.market_regions, 'market_session_for_region', return_value={'is_open': False}):
            got = feed._poll(db, {'IN': provider}, {'IN': rows('COFORGE')}, '')
        provider.get_quotes.assert_not_called()
        db.set_state.assert_not_called()
        self.assertFalse(self.archive.exists())
        self.assertEqual(got, {})

    def test_real_provider_retains_old_trade_time_or_missing_epoch_through_feed(self):
        provider = UpstoxMarketDataProvider(replace(Settings(), upstox_access_token='synthetic'))
        old = NOW - timedelta(hours=1)
        payload = {'data': {
            'NSE_EQ:COFORGE': dict(instrument_token='NSE_EQ|COFORGE', last_price=100,
                                  last_trade_time=int(old.timestamp() * 1000)),
            'NSE_EQ:RELIANCE': dict(instrument_token='NSE_EQ|RELIANCE', last_price=100),
        }}
        def respond(request):
            return httpx.Response(200, json=payload)
        client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        db = Mock()
        with patch('app.market_data.httpx.AsyncClient', return_value=client):
            got = feed._poll(db, {'IN': provider}, {'IN': rows('COFORGE', 'RELIANCE')}, '')['IN']
        written = db.upsert_quotes.call_args.args[0]
        self.assertEqual(written['COFORGE'].asof, old.isoformat())
        self.assertEqual(written['RELIANCE'].asof, '1970-01-01T00:00:00+00:00')
        self.assertTrue(all(q.source == 'upstox-last-traded' for q in written.values()))
        self.assertEqual(got['fresh_symbols'], [])


if __name__ == '__main__':
    unittest.main()
