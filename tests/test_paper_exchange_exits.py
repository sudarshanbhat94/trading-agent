import json
import sqlite3
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from unittest.mock import patch

from app import books, paper_exchange as exchange, paper_ledger, v2_web, v2_live, approved_execution
from tests.test_approved_execution import ApprovedPaperPipelineTest as Fixture


class PaperBidExitTest(unittest.TestCase):
    def setUp(self):
        self.f=Fixture();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.f.submit();self.con=self.f.con;self.pid=books.positions(self.con,2)[0]['id']

    def quote(self,price=100,quantity=1000,at=None):
        at=at or self.f.now+timedelta(seconds=3)
        return {'TEST':dict(price=price-.05,ts=at.isoformat(),execution=self.f.snapshot(at,price=price,quantity=quantity))},at

    def test_production_cycle_checks_and_fills_stop_despite_entry_catalogue_outage(self):
        from datetime import datetime,timezone
        books.ensure_book(self.con,3)
        plan=dict(self.f.plan,user_id=3,epoch=books.current_epoch(self.con,3))
        pid=approved_execution.approve(self.con,plan,approved_by='fixture',approval_reference='fixture',now=self.f.now)
        approved_execution.submit(self.con,self.f.catalogue,3,pid,'pending-outage-fixture',
            {'TEST':dict(price=100,ts=self.f.now.isoformat())},regime='ON',now=self.f.now)
        def quote(*a,**kw):
            at=datetime.now(timezone.utc)
            return {'TEST':dict(price=98,ts=at.isoformat(),execution=self.f.snapshot(at,price=98.05))}
        with patch.object(v2_live,'_live',side_effect=quote),patch.object(v2_live,'sleeve_view',return_value={}), \
                patch('app.entry_contracts.open_catalogue',side_effect=ValueError('synthetic catalogue outage')):
            v2_live.service_personal_paper(self.con,'IN')
        self.assertEqual(books.positions(self.con,2),[])
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM user_trades WHERE user_id=2').fetchone()[0],1)
        self.assertEqual(len(exchange.pending(self.con,3)),1)
        self.assertEqual(paper_ledger.report(self.con,2,'IN',books.current_epoch(self.con,2),books.cash(self.con,2))['cash_difference_minor'],0)

    def test_exit_request_keeps_cash_equity_inventory_until_later_bid(self):
        before=books.cash(self.con,2)
        order=exchange.queue_exit(self.con,2,self.pid,'stop',now=self.f.now+timedelta(seconds=2))
        self.assertEqual(order['status'],'pending');self.assertEqual(books.cash(self.con,2),before)
        self.assertEqual(len(books.positions(self.con,2)),1);self.assertEqual(len(exchange.pending(self.con,2)),0)
        self.assertEqual(len(exchange.exit_pending(self.con)),1)
        with self.assertRaises(ValueError):exchange.cancel(self.con,2,order['order_id'])
        quotes,at=self.quote(price=95,quantity=199)
        self.assertEqual(exchange.service_exits(self.con,quotes,now=at),[])
        quotes,at=self.quote(price=95,quantity=200)
        result=exchange.service_exits(self.con,quotes,now=at)[0]
        self.assertEqual(result['exit'],94.95);self.assertLess(result['pnl'],0)
        self.assertEqual(books.positions(self.con,2),[])
        self.assertEqual(exchange.service_exits(self.con,quotes,now=at),[])
        ledger=paper_ledger.report(self.con,2,'IN',books.current_epoch(self.con,2),books.cash(self.con,2))
        self.assertEqual(ledger['cash_difference_minor'],0)

    def test_stale_same_event_and_wrong_account_do_not_sell(self):
        at=self.f.now+timedelta(seconds=2);exchange.queue_exit(self.con,2,self.pid,'manual',now=at)
        with self.assertRaises(ValueError):exchange.queue_exit(self.con,3,self.pid,'manual',now=at)
        quotes,_=self.quote(at=at);self.assertEqual(exchange.service_exits(self.con,quotes,now=at),[])
        self.assertEqual(exchange.service_exits(self.con,quotes,now=at+timedelta(seconds=31)),[])
        self.assertEqual(len(books.positions(self.con,2)),1)

    def test_exit_fill_fault_restores_owned_position_and_pending_order(self):
        order=exchange.queue_exit(self.con,2,self.pid,'stop',now=self.f.now+timedelta(seconds=2));quotes,at=self.quote()
        with patch.object(paper_ledger,'exit',side_effect=OSError('synthetic storage fault')):
            with self.assertRaises(OSError):exchange.service_exits(self.con,quotes,now=at)
        self.assertEqual(len(books.positions(self.con,2)),1)
        self.assertEqual(exchange.status(self.con,2,order['order_id'])['status'],'pending')
        self.assertEqual(exchange.service_exits(self.con,quotes,now=at)[0]['status'],'filled')

    def test_concurrent_workers_cannot_oversell_or_duplicate_the_exit(self):
        exchange.queue_exit(self.con,2,self.pid,'stop',now=self.f.now+timedelta(seconds=2));quotes,at=self.quote()
        barrier=Barrier(2)
        def worker(_):
            with sqlite3.connect(self.f.path,timeout=10) as con:
                barrier.wait();return exchange.service_exits(con,quotes,now=at)
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(worker,range(2)))
        self.assertEqual(sum(len(r) for r in results),1)
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM user_trades WHERE user_id=2').fetchone()[0],1)
        self.assertGreaterEqual(books.cash(self.con,2),0)

    def test_actual_sell_handler_returns_accepted_without_a_phantom_trade(self):
        with patch.object(v2_web,'V2_DB',str(self.f.path)),patch.object(v2_web,'_market_shut',return_value=None), \
                patch.object(v2_web,'_live_map',return_value={'TEST':dict(price=100,ts=self.f.now.isoformat())}):
            result=v2_web.api_sell(dict(symbol='TEST',market='IN',mode='paper'),dict(id=2))
            data=json.loads(result.body)
            self.assertEqual(result.status_code,202);self.assertEqual(data['side'],'SELL');self.assertFalse(data['paper_recorded'])
            self.assertEqual(len(books.positions(self.con,2)),1)
