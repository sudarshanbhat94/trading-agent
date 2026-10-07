"""Synthetic forward lifecycle evidence; never evidence of strategy profits."""
import copy
import json
import os
import sqlite3
import unittest
from contextlib import closing, ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

from app import approved_execution, books, paper_exchange, paper_ledger, v2_live
from app.screening import automation, confirmation, tracking
from app.sleeves.config import SLEEVES
from app.sleeves.engine import SleeveEngine
from app.sleeves.regime import RegimeView
from app.sleeves.risk import RiskManager
from tests import test_approved_execution as fixture_module
from tests.test_stock_plans import book, screen

FIRST = datetime(2026, 10, 5, 5, tzinfo=timezone.utc)
ENTRY = datetime(2026, 10, 6, 5, tzinfo=timezone.utc)


class _Clock(datetime):
    @classmethod
    def now(cls, tz=None):
        return ENTRY.astimezone(tz) if tz else ENTRY.replace(tzinfo=None)


class SelectivePaperAutomationTest(unittest.TestCase):
    def setUp(self):
        self.f = fixture_module.ApprovedPaperPipelineTest()
        with patch('tests.test_approved_execution.datetime', _Clock):
            self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.con = self.f.con
        self.con.execute("UPDATE v2_book SET started_at=? WHERE market='IN'", (FIRST.isoformat(),))
        self.con.execute("UPDATE user_book SET started_at=? WHERE user_id=2 AND market='IN'", (FIRST.isoformat(),))
        self.con.commit()
        self.journal = Path(self.f.tmp.name)/'equity_automation.db'
        self.env = patch.dict(os.environ, EQUITY_AUTOMATION_DB=str(self.journal))
        self.env.start(); self.addCleanup(self.env.stop)
        self.clock = patch('app.v2_live.datetime', _Clock)
        self.clock.start(); self.addCleanup(self.clock.stop)
        self.sleeve = automation.SelectivePaperSleeve()
        self.regime = RegimeView('ON', True, .7, 'ON', 'Synthetic supportive market')

    def evidence(self, now, day, close=100):
        data = screen(1); row = data['equities'][0]
        data.update(generated_at=now.isoformat(), price_asof=day)
        row.update(symbol='TEST', news=dict(checked_at=now.isoformat()))
        row['metrics']['price'] = close
        row['participation']['session'] = day
        return data

    def context(self, now=FIRST, price=100, day='2026-10-01', bars=None):
        return SimpleNamespace(observed_at=now, paper_epoch=FIRST.isoformat(), book=book(),
            equity_screen=self.evidence(now, day, 100 if day=='2026-10-01' else 98),
            live={'TEST':dict(price=price, ts=now.isoformat(), source='upstox-live')},
            tails={'TEST':bars} if bars is not None else {}, asof=day,
            eligible_symbols={'TEST'}, regime=self.regime, settings=SLEEVES)

    def rebound(self):
        dates = [d for d in pd.bdate_range(end='2026-10-05', periods=23)
                 if d.date().isoformat() != '2026-10-02'][-21:]
        frame = pd.DataFrame(dict(open=94., high=99., low=90., close=95., volume=100.), index=dates)
        frame.loc[dates[-1]] = [92., 100., 90., 98., 200.]
        return frame

    def confirmed(self):
        initial = self.sleeve.propose(self.context())
        self.assertEqual(initial.candidates, [])
        touch = FIRST+timedelta(minutes=1)
        automation.observe({'TEST':dict(price=95, ts=touch.isoformat(), source='upstox-live')}, touch)
        ctx = self.context(ENTRY, 95, '2026-10-05', self.rebound())
        self.assertEqual(self.sleeve.propose(ctx).candidates, [])  # Evidence first known now.
        ctx.observed_at += timedelta(seconds=5)
        ctx.live['TEST']['ts'] = ctx.observed_at.isoformat()
        dec = self.sleeve.propose(ctx)
        self.assertEqual(len(dec.candidates), 1)
        return ctx, dec.candidates[0]

    def submit(self):
        ctx, candidate = self.confirmed()
        self.screen_read = patch.object(automation, 'current_screen', return_value=ctx.equity_screen)
        self.screen_read.start(); self.addCleanup(self.screen_read.stop)
        allocation = RiskManager().size(candidate, ctx.book)
        self.assertTrue(allocation.ok)
        result = paper_exchange.enqueue_house(self.con, self.f.catalogue, allocation, ctx.live, regime='ON', now=ctx.observed_at)
        return ctx, candidate, allocation, result

    def quotes(self, at, price=95):
        return {'TEST':dict(price=price, ts=at.isoformat(), source='upstox-live', execution=self.f.snapshot(at, price))}

    def test_automatic_publication_does_not_require_ui_and_zone_touch_is_not_a_fill(self):
        self.sleeve.propose(self.context())
        self.sleeve.propose(self.context())
        with closing(sqlite3.connect(self.journal)) as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM publications').fetchone()[0], 1)
        ctx, candidate = self.confirmed()
        metadata = candidate.why['selective_paper']
        self.assertEqual(metadata['model_version'], automation.MODEL_VERSION)
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM v2_positions').fetchone()[0], 0)
        self.assertEqual(paper_exchange.pending(self.con), [])
        self.assertEqual(ctx.book.cash, 10000)

    def test_forward_stock_house_subscriber_protection_exit_and_costed_accounting(self):
        ctx, candidate, allocation, order = self.submit()
        self.assertEqual(order['status'], 'pending')
        at = ctx.observed_at+timedelta(seconds=1)
        house = paper_exchange.service_house(self.con, self.f.catalogue, self.quotes(at), regime='ON', now=at)[0]
        self.assertEqual(house['status'], 'filled')
        self.assertEqual(house['model_version'], automation.MODEL_VERSION)
        self.assertEqual(house['source_publication']['fingerprint'], candidate.why['selective_paper']['fingerprint'])
        frozen = json.loads(self.con.execute('SELECT payload FROM paper_order_intents WHERE id=?', (order['order_id'],)).fetchone()[0])['plan']
        self.assertEqual((frozen['entry_low'], frozen['entry_high']), (95, 95.15))
        self.assertLessEqual(house['qty'], 30)
        payload = json.loads(self.con.execute("SELECT payload FROM execution_outbox WHERE topic='house_entry'").fetchone()[0])
        at += timedelta(seconds=1)
        mirror = approved_execution.submit_house_mirror(self.con, self.f.catalogue, 2, payload, self.quotes(at), regime='ON', now=at)
        self.assertEqual(mirror['status'], 'pending'); self.assertEqual(books.positions(self.con, 2), [])
        at += timedelta(seconds=1)
        filled = paper_exchange.service(self.con, self.f.catalogue, self.quotes(at), regime='ON', now=at)[0]
        self.assertEqual(filled['model_version'], automation.MODEL_VERSION)
        self.assertEqual(len(books.positions(self.con, 2)), 1)
        self.assertEqual(books.positions(self.con, 2)[0]['stop'], 92)
        self.assertEqual(books.positions(self.con, 2)[0]['target'], 112)
        paper_exchange.queue_exit(self.con, 2, filled['position_id'], 'target', now=at)
        at += timedelta(seconds=1)
        paper_exchange.service_exits(self.con, self.quotes(at, 112.05), now=at)
        self.assertEqual(books.positions(self.con, 2), [])
        ledger = paper_ledger.report(self.con, 2, 'IN', books.current_epoch(self.con, 2), books.cash(self.con, 2))
        self.assertEqual(ledger['cash_difference_minor'], 0)
        self.assertEqual(self.con.execute('SELECT model_version,sleeve,regime FROM user_trades WHERE user_id=2').fetchone(),
                         (automation.MODEL_VERSION, 'quality_momentum', 'ON'))

    def test_off_stale_missing_news_bad_quality_weak_volume_and_duplicate_identity_do_not_execute(self):
        for mode in ('OFF', 'stale', 'news', 'quality', 'volume', 'duplicate', 'outside', 'cash'):
            with self.subTest(mode=mode):
                ctx = self.context()
                if mode=='OFF':ctx.regime = RegimeView('OFF', False, .2, 'OFF', 'Fixture')
                elif mode=='stale':ctx.equity_screen['stale'] = True
                elif mode=='news':ctx.equity_screen['equities'][0]['flags'] = ['adverse official filing']
                elif mode=='quality':ctx.equity_screen['equities'][0]['fundamentals']['roe_pct'] = 1
                elif mode=='volume':ctx.equity_screen['equities'][0]['metrics']['relative_volume'] = .5
                elif mode=='duplicate':ctx.equity_screen['equities'].append(copy.deepcopy(ctx.equity_screen['equities'][0]))
                elif mode=='outside':ctx.eligible_symbols = {'OTHER'}
                elif mode=='cash':ctx.book.cash = 100
                self.assertEqual(self.sleeve.propose(ctx).candidates, [])
                with closing(sqlite3.connect(self.journal)) as c:
                    self.assertEqual(c.execute('SELECT COUNT(*) FROM publications').fetchone()[0], 0)
        self.assertEqual(paper_exchange.pending(self.con), [])

    def test_legacy_and_forged_candidate_and_changed_epoch_or_levels_are_refused(self):
        ctx, candidate = self.confirmed()
        with patch.object(automation, 'current_screen', return_value=ctx.equity_screen):
            for field, change in (('model_version', 'legacy'), ('fingerprint', 'bad'), ('epoch', 'old')):
                metadata = dict(candidate.why['selective_paper'], **{field:change})
                with self.assertRaises(ValueError):
                    automation.binding(metadata, 'TEST', 92, 112, FIRST.isoformat(), self.f.catalogue, ctx.observed_at)
            with self.assertRaises(ValueError):
                automation.binding(candidate.why['selective_paper'], 'TEST', 91, 112, FIRST.isoformat(), self.f.catalogue, ctx.observed_at)

    def test_pending_news_change_refuses_fill_and_releases_reservation(self):
        ctx, _, _, order = self.submit()
        ctx.equity_screen['equities'][0]['flags'] = ['adverse official filing']
        at = ctx.observed_at+timedelta(seconds=1)
        result = paper_exchange.service_house(self.con, self.f.catalogue, self.quotes(at), regime='ON', now=at)[0]
        self.assertEqual(result['status'], 'rejected')
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM v2_positions').fetchone()[0], 0)
        self.assertEqual(paper_exchange.pending(self.con), [])

    def test_pending_plan_invalidation_blocks_fill_even_with_old_positive_assessment(self):
        ctx, _, _, order = self.submit()
        at = ctx.observed_at+timedelta(seconds=1)
        automation.observe(self.quotes(at, 90), at)
        # A later recovery cannot resurrect the original invalidated plan.
        at += timedelta(seconds=1)
        result = paper_exchange.service_house(self.con, self.f.catalogue, self.quotes(at), regime='ON', now=at)[0]
        self.assertEqual(result['status'], 'rejected')
        self.assertIn('invalidated', result['reason'])
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM v2_positions').fetchone()[0], 0)

    def test_pending_trial_feature_flag_change_blocks_fill(self):
        ctx, _, _, order = self.submit()
        at = ctx.observed_at+timedelta(seconds=1)
        with patch.object(SLEEVES.quality_momentum, 'enabled', False):
            result = paper_exchange.service_house(self.con, self.f.catalogue, self.quotes(at), regime='ON', now=at)[0]
        self.assertEqual(result['status'], 'rejected')
        self.assertEqual(paper_exchange.pending(self.con), [])

    def test_confirmation_expires_and_original_publication_cannot_be_rebound(self):
        ctx, candidate = self.confirmed()
        with patch.object(automation, 'current_screen', return_value=ctx.equity_screen):
            with self.assertRaises(ValueError):
                automation.binding(candidate.why['selective_paper'], 'TEST', 92, 112, FIRST.isoformat(),
                                   self.f.catalogue, ctx.observed_at+timedelta(seconds=121))
        with closing(sqlite3.connect(self.journal)) as c:
            with self.assertRaises(sqlite3.IntegrityError):c.execute('UPDATE publications SET payload=?', ('{}',))
            with self.assertRaises(sqlite3.IntegrityError):c.execute('DELETE FROM assessment_events')

    def test_forming_bar_does_not_replace_completed_confirmation(self):
        self.sleeve.propose(self.context())
        touch = FIRST+timedelta(minutes=1)
        automation.observe(self.quotes(touch), touch)
        frame = self.rebound()
        frame.loc[pd.Timestamp('2026-10-06')] = [105, 115, 95, 110, 100000]
        ctx = self.context(ENTRY, 95, '2026-10-05', frame)
        self.sleeve.propose(ctx)
        ctx.observed_at += timedelta(seconds=5);ctx.live = self.quotes(ctx.observed_at)
        self.assertEqual(len(self.sleeve.propose(ctx).candidates), 1)

    def test_journal_is_separate_and_research_fingerprints_unchanged(self):
        plan = dict(symbol='TEST',price_asof='2026-10-01',entry_low=95,entry_high=96,stop=92,t1=104,t2=108,t3=112,qty=20,
                    model_version='conditional-pullback-v2')
        before = tracking.fingerprint(plan)
        research = Path(self.f.tmp.name)/'research.db'
        tracking.publish(research, 2, [plan], now=FIRST)
        original = research.read_bytes()
        self.confirmed()
        self.assertEqual(tracking.fingerprint(plan), before)
        self.assertNotEqual(tracking.fingerprint(dict(plan,automation_epoch='one',automation_model_version=automation.MODEL_VERSION)),
                            tracking.fingerprint(dict(plan,automation_epoch='two',automation_model_version=automation.MODEL_VERSION)))
        self.assertEqual(research.read_bytes(), original)
        with patch.dict(os.environ, EQUITY_AUTOMATION_DB=tracking.default_path(v2_live.MAIN_DB)):
            with self.assertRaises(ValueError):automation.default_path()

    def test_fast_observation_runs_without_pending_orders(self):
        self.sleeve.propose(self.context())
        touch = FIRST+timedelta(minutes=1)
        with patch.object(v2_live, '_live', return_value=self.quotes(touch)), \
             patch('app.v2_live.datetime', _Clock), patch.object(automation, 'observe') as observe:
            v2_live.service_personal_paper(self.con, 'IN')
        observe.assert_called_once()

    def test_old_factor_model_is_not_installed_and_no_broker_lane_is_enabled(self):
        from app import live_trade
        self.assertIsInstance(SleeveEngine().sleeves['quality_momentum'], automation.SelectivePaperSleeve)
        self.assertNotIn('quality_momentum', live_trade.MIRRORED_LANES)

    def test_actual_broker_fanout_returns_before_tokens_network_or_submission(self):
        from app import broker, live_trade
        with patch.object(broker, 'linked_users') as users, patch.object(broker, 'verify') as verify, \
             patch.object(live_trade, 'mirror_entry') as submit, patch.object(v2_live, '_ro') as read:
            v2_live._live_mirror_entry(self.con, 'IN', 'quality_momentum', 'TEST', 95, strict=True)
        users.assert_not_called();verify.assert_not_called();submit.assert_not_called();read.assert_not_called()

    def test_actual_production_pass_reserves_confirmed_stock_only_and_later_service_fills(self):
        ctx, candidate = self.confirmed()
        ctx.observed_at += timedelta(seconds=1)
        engine = SleeveEngine()
        engine.gate.view = lambda *args: self.regime
        with ExitStack() as stack:
            stack.enter_context(patch.object(automation, 'current_screen', return_value=ctx.equity_screen))
            stack.enter_context(patch.object(v2_live, '_SLEEVE_ENGINE', engine))
            stack.enter_context(patch.object(v2_live, 'market_open', return_value=True))
            stack.enter_context(patch.object(v2_live, '_live', return_value=self.quotes(ctx.observed_at)))
            stack.enter_context(patch.object(v2_live, '_hist', return_value=({'TEST': self.rebound()}, pd.DataFrame())))
            stack.enter_context(patch.object(v2_live, '_rw', side_effect=lambda: sqlite3.connect(self.f.path)))
            stack.enter_context(patch.object(v2_live, '_ro', side_effect=lambda _: sqlite3.connect(':memory:')))
            stack.enter_context(patch.object(v2_live.eng, 'complete_trading_dates', return_value=[pd.Timestamp('2026-10-05')]))
            stack.enter_context(patch('app.sleeves.reference.refresh_membership'))
            stack.enter_context(patch('app.sleeves.reference.snapshot', return_value=({'TEST'}, {})))
            stack.enter_context(patch.object(v2_live, '_SLEEVE_VIEW_FILE', str(Path(self.f.tmp.name)/'view.json')))
            stack.enter_context(patch.object(v2_live, '_forward_updated_day', {'IN':'2026-10-06'}))
            # Clock after the first confirmation, with a strictly later quote.
            with patch.object(_Clock, 'now', return_value=ctx.observed_at):
                from app import entry_contracts
                with entry_contracts.using(self.f.catalogue, ctx.observed_at):
                    v2_live.sleeve_pass('IN')
            self.assertEqual(self.con.execute('SELECT COUNT(*) FROM v2_positions').fetchone()[0], 0)
            intents = paper_exchange.pending(self.con, 0)
            self.assertEqual(len(intents), 1)
            plan = json.loads(intents[0][5])['plan']
            self.assertEqual((plan['symbol'], plan['sleeve'], plan['model_version']), ('TEST', 'quality_momentum', automation.MODEL_VERSION))
            at = ctx.observed_at+timedelta(seconds=1)
            filled = paper_exchange.service_house(self.con, self.f.catalogue, self.quotes(at), regime='ON', now=at)[0]
            self.assertEqual(filled['status'], 'filled')
            self.assertLessEqual(filled['qty']*filled['entry'], 3000)

    def test_duplicate_restart_and_feature_flag_cannot_open_a_second_position(self):
        ctx, candidate, allocation, result = self.submit()
        again = paper_exchange.enqueue_house(self.con, self.f.catalogue, allocation, ctx.live, regime='ON', now=ctx.observed_at)
        self.assertEqual(again, result)
        at = ctx.observed_at+timedelta(seconds=1)
        paper_exchange.service_house(self.con, self.f.catalogue, self.quotes(at), regime='ON', now=at)
        self.con.commit()
        self.con.close(); self.con = sqlite3.connect(self.f.path)
        self.addCleanup(self.con.close)
        self.assertEqual(paper_exchange.service_house(self.con, self.f.catalogue, self.quotes(at), regime='ON', now=at), [])
        with patch.object(SLEEVES.quality_momentum, 'enabled', False):
            with self.assertRaises(ValueError):
                paper_exchange.enqueue_house(self.con, self.f.catalogue, allocation, ctx.live, regime='ON', now=at)
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM v2_positions').fetchone()[0], 1)


if __name__ == '__main__':
    unittest.main()
