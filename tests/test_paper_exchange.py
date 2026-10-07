import json
import sqlite3
import unittest
from datetime import timedelta
from unittest.mock import patch

from app import approved_execution, books, paper_exchange, paper_ledger, v2_web
from tests.test_approved_execution import ApprovedPaperPipelineTest as Fixture


class LaterEventPaperExchangeTest(unittest.TestCase):
    def setUp(self):
        self.f = Fixture(); self.f.setUp(); self.addCleanup(self.f.doCleanups)
        self.con = self.f.con; self.now = self.f.now

    def submit(self):
        return approved_execution.submit(self.con, self.f.catalogue, 2, self.f.plan_id, 'exchange-request',
            {'TEST':dict(price=100, ts=self.now.isoformat())}, regime='ON', now=self.now)

    def test_submit_reserves_risk_without_fill_pnl_or_cash_changes(self):
        result = self.submit(); self.assertEqual(result['status'], 'pending'); self.assertEqual(result['qty'], 0)
        self.assertFalse(result['paper_recorded']); self.assertEqual(books.positions(self.con, 2), [])
        stats = books.stats(self.con, 2, 'IN', {})
        self.assertEqual(stats['cash'], 10000); self.assertEqual(stats['equity'], 10000)
        self.assertEqual(stats['realised'], 0); self.assertEqual(stats['pending_orders'], 1)
        self.assertLess(stats['available_cash'], stats['cash']); self.assertEqual(self.submit(), result)
        state, reason = books.risk_state(self.con, 2, 'IN', {})
        self.assertFalse(reason); self.assertGreater(state.open_risk, 0); self.assertEqual(state.open_positions, 1)

    def test_executable_ask_determines_zone_and_corrupt_outcome_cannot_fake_receipt(self):
        quotes={'TEST':dict(price=101,ts=self.now.isoformat(),execution=self.f.snapshot(self.now))}
        result=approved_execution.submit(self.con,self.f.catalogue,2,self.f.plan_id,'ask-zone-request',quotes,regime='ON',now=self.now)
        self.assertEqual(result['status'],'pending')  # Last trade differs from the current ask.
        self.con.execute("UPDATE paper_order_state SET status='filling' WHERE order_id=?",(result['order_id'],));self.con.commit()
        with self.assertRaises(ValueError):paper_exchange.status(self.con,2,result['order_id'])
        with self.assertRaises(ValueError):self.f.fill_pending()
        self.assertEqual(books.positions(self.con,2),[])

    def test_submission_snapshot_stale_wrong_identity_and_no_liquidity_never_fill(self):
        order = self.submit()
        for snap, at in [(self.f.snapshot(self.now), self.now),
                         (self.f.snapshot(self.now), self.now+timedelta(seconds=40)),
                         (dict(self.f.snapshot(), instrument_key='NSE_EQ|OTHER'), self.now+timedelta(seconds=1)),
                         (self.f.snapshot(quantity=199), self.now+timedelta(seconds=1)),
                         (self.f.snapshot(price=101), self.now+timedelta(seconds=1))]:
            self.f.fill_pending(snapshot=snap, now=at)
            self.assertEqual(paper_exchange.status(self.con, 2, order['order_id'])['status'], 'pending')
        self.assertEqual(books.positions(self.con, 2), []); self.assertEqual(books.cash(self.con, 2), 10000)
        self.f.fill_pending(snapshot=self.f.snapshot(quantity=200))
        self.assertEqual(len(books.positions(self.con, 2)), 1)

    def test_restart_cancel_owner_and_late_fill_race(self):
        order = self.submit(); self.con.close(); self.f.con = self.con = sqlite3.connect(self.f.path)
        with self.assertRaises(ValueError): paper_exchange.cancel(self.con, 3, order['order_id'])
        result = paper_exchange.cancel(self.con, 2, order['order_id'], now=self.now)
        self.assertEqual(result['status'], 'cancelled'); self.assertEqual(self.submit(), result)
        self.f.fill_pending(); self.assertEqual(books.positions(self.con, 2), [])
        self.assertEqual(books.stats(self.con, 2, 'IN', {})['available_cash'], 10000)

    def test_fill_fault_rolls_back_usage_postings_events_and_reservation(self):
        order = self.submit()
        with patch.object(paper_ledger, 'entry', side_effect=OSError('synthetic storage fault')):
            with self.assertRaises(OSError): self.f.fill_pending()
        self.assertEqual(books.positions(self.con, 2), [])
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM paper_depth_usage').fetchone()[0], 0)
        self.assertEqual(paper_exchange.status(self.con, 2, order['order_id'])['status'], 'pending')
        self.f.fill_pending(); filled = paper_exchange.status(self.con, 2, order['order_id'])
        self.assertEqual(filled['status'], 'filled'); self.assertEqual(self.f.fill_pending(), [])
        self.assertEqual(paper_exchange.cancel(self.con, 2, order['order_id']), filled)
        report = paper_ledger.report(self.con, 2, 'IN', books.current_epoch(self.con, 2), books.cash(self.con, 2))
        self.assertEqual(report['cash_difference_minor'], 0)
        event = self.con.execute("SELECT payload FROM paper_order_events WHERE kind='FILLED'").fetchone()[0]
        self.assertEqual(json.loads(event)['snapshot']['instrument_key'], 'NSE_EQ|TEST')

    def test_regime_expiry_and_epoch_cancel_commitments_without_a_trade(self):
        for mode in ('OFF', 'expired', 'reset'):
            with self.subTest(mode=mode):
                order = self.submit()
                if mode == 'reset': books.reset_book(self.con, 2, 'IN')
                self.f.fill_pending(regime='OFF' if mode=='OFF' else 'ON',
                    now=self.now+timedelta(hours=2) if mode=='expired' else self.now+timedelta(seconds=1))
                self.assertNotEqual(paper_exchange.status(self.con, 2, order['order_id'])['status'], 'pending')
                self.assertEqual(books.positions(self.con, 2), []); self.assertEqual(books.cash(self.con, 2), 10000)
                if mode != 'reset':
                    self.f.plan['model_version'] += '-next'
                    self.f.plan_id = approved_execution.approve(self.con, self.f.plan,
                        approved_by='fixture account owner', approval_reference='fixture', now=self.now)
                    # New request identity for the next independent scenario.
                    self.now += timedelta(microseconds=1)
                    self.submit = lambda: approved_execution.submit(self.con, self.f.catalogue, 2, self.f.plan_id,
                        'exchange-request-'+mode, {'TEST':dict(price=100,ts=self.now.isoformat())},regime='ON',now=self.now)

    def test_depth_capacity_is_not_reused_across_accounts(self):
        self.submit(); books.ensure_book(self.con, 3)
        plan = dict(self.f.plan, user_id=3, epoch=books.current_epoch(self.con, 3))
        other = approved_execution.approve(self.con, plan, approved_by='fixture owner', approval_reference='fixture', now=self.now)
        accepted = approved_execution.submit(self.con, self.f.catalogue, 3, other, 'other-owner-request',
            {'TEST':dict(price=100,ts=self.now.isoformat())},regime='ON',now=self.now)
        self.assertEqual(accepted['status'], 'pending')
        outcomes = self.f.fill_pending(snapshot=self.f.snapshot(quantity=200))
        self.assertEqual(len(outcomes), 1); self.assertEqual(len(paper_exchange.pending(self.con)), 1)
        self.assertEqual(self.con.execute('SELECT quantity FROM paper_depth_usage').fetchone()[0], 20)
        self.f.fill_pending(snapshot=self.f.snapshot(self.now+timedelta(seconds=2),quantity=200), now=self.now+timedelta(seconds=2))
        self.assertEqual(len(paper_exchange.pending(self.con)), 0)

    def test_owned_api_exposes_pending_and_cancel_without_faking_a_fill(self):
        order=self.submit()
        with patch.object(v2_web, 'V2_DB', str(self.f.path)):
            response=v2_web.api_paper_orders({'id':2}); data=json.loads(response.body)
            self.assertEqual(data['orders'][0]['status'], 'pending')
            self.assertEqual(json.loads(v2_web.api_paper_orders({'id':3}).body)['orders'], [])
            self.assertEqual(json.loads(v2_web.api_cancel_paper_order(order['order_id'],{'id':2}).body)['status'], 'cancelled')
