"""Sourced sessions gate fills without requiring new-entry instrument rules."""
import sqlite3
import unittest
from datetime import timedelta

from app import approved_execution, books, execution_contracts, paper_exchange, paper_ledger
from tests.test_approved_execution import ApprovedPaperPipelineTest as Fixture


class PaperSessionBoundaryTest(unittest.TestCase):
    def setUp(self):
        self.f=Fixture();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.con=self.f.con;self.origin=self.f.now
        self.calendar='NSE:FIXTURE:ALL_DAY'
        self.start=self.origin.replace(hour=0,minute=0,second=0,microsecond=0)
        self.end=self.start+timedelta(days=1)

    def session(self,*,observed,closes=None,open=True,opens=None):
        payload={'open':open}
        if open:payload.update(opens_at=(opens or self.start).isoformat(),closes_at=(closes or self.end).isoformat())
        return execution_contracts.record(self.f.catalogue,'session',self.calendar,payload,
            source='SYNTHETIC EXCHANGE NOTICE, NOT PRODUCTION',observed_at=observed.isoformat(),
            effective_from=self.start.isoformat(),effective_until=self.end.isoformat(),now=observed)

    def enter(self):
        self.f.now=self.origin+timedelta(microseconds=2)
        approved_execution.submit(self.con,self.f.catalogue,2,self.f.plan_id,'session-fixture',
            {'TEST':dict(price=100,ts=self.f.now.isoformat())},regime='ON',now=self.f.now)
        self.f.fill_pending()
        self.pid=books.positions(self.con,2)[0]['id']

    def request(self):
        return paper_exchange.queue_exit(self.con,2,self.pid,'stop',now=self.origin+timedelta(seconds=1.5))

    def quotes(self,at):
        return {'TEST':dict(price=95,ts=at.isoformat(),execution=self.f.snapshot(at,price=95.05))}

    def test_fresh_bid_after_sourced_early_close_never_fills_or_consumes_depth(self):
        close=self.origin+timedelta(seconds=2)
        self.session(observed=self.origin+timedelta(microseconds=1),closes=close)
        self.enter();order=self.request();cash=books.cash(self.con,2)
        after=close+timedelta(seconds=1)
        self.assertEqual(paper_exchange.service_exits(self.con,self.quotes(after),now=after),[])
        self.assertEqual(len(books.positions(self.con,2)),1);self.assertEqual(books.cash(self.con,2),cash)
        self.assertIn('session',paper_exchange.status(self.con,2,order['order_id'])['reason'])
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM paper_depth_usage WHERE side='SELL'").fetchone()[0],0)

    def test_dated_closed_notice_overrides_open_cache_and_reopening_can_fill_once(self):
        self.enter();order=self.request();at=self.origin+timedelta(seconds=2)
        self.session(observed=at,open=False);paper_exchange.sync_sessions(self.con,self.f.catalogue,now=at)
        self.assertEqual(paper_exchange.service_exits(self.con,self.quotes(at),now=at),[])
        self.assertEqual(len(books.positions(self.con,2)),1)
        reopen=at+timedelta(seconds=1)
        self.session(observed=reopen,opens=reopen)
        paper_exchange.sync_sessions(self.con,self.f.catalogue,now=reopen)
        self.assertEqual(paper_exchange.service_exits(self.con,self.quotes(reopen),now=reopen)[0]['status'],'filled')
        self.assertEqual(paper_exchange.service_exits(self.con,self.quotes(reopen),now=reopen),[])
        self.assertEqual(paper_ledger.report(self.con,2,'IN',books.current_epoch(self.con,2),books.cash(self.con,2))['cash_difference_minor'],0)

    def test_missing_expired_conflicting_or_future_session_does_not_invent_a_fill(self):
        self.enter();order=self.request()
        tomorrow=self.end+timedelta(seconds=1)
        self.assertEqual(paper_exchange.service_exits(self.con,self.quotes(tomorrow),now=tomorrow),[])
        at=self.origin+timedelta(seconds=2)
        self.session(observed=at,open=False);self.session(observed=at,open=True)
        paper_exchange.sync_sessions(self.con,self.f.catalogue,now=at)
        self.assertEqual(paper_exchange.service_exits(self.con,self.quotes(at),now=at),[])
        future=at+timedelta(seconds=1);self.session(observed=future,open=True)
        paper_exchange.sync_sessions(self.con,self.f.catalogue,now=at)
        self.assertFalse(self.con.execute('SELECT 1 FROM execution_contract_evidence WHERE observed_at=?',(future.isoformat(),)).fetchone())
        self.assertEqual(len(books.positions(self.con,2)),1)
        self.assertEqual(paper_exchange.status(self.con,2,order['order_id'])['status'],'pending')

    def test_cached_session_survives_source_outage_and_is_immutable(self):
        self.enter();self.request();self.f.catalogue.close()
        at=self.origin+timedelta(seconds=2)
        self.assertEqual(paper_exchange.service_exits(self.con,self.quotes(at),now=at)[0]['status'],'filled')
        with self.assertRaises(sqlite3.IntegrityError):self.con.execute("DELETE FROM execution_contract_evidence")

    def test_quote_before_sourced_open_cannot_fill_during_later_open_session(self):
        self.enter();self.request();open_at=self.origin+timedelta(seconds=3)
        self.session(observed=self.origin+timedelta(seconds=2),opens=open_at)
        paper_exchange.sync_sessions(self.con,self.f.catalogue,now=open_at)
        self.assertEqual(paper_exchange.service_exits(self.con,self.quotes(open_at-timedelta(seconds=.5)),now=open_at),[])
        self.assertEqual(paper_exchange.service_exits(self.con,self.quotes(open_at),now=open_at)[0]['status'],'filled')

    def test_exact_source_ordering_refuses_submillisecond_future_and_effective_boundaries(self):
        at=self.origin+timedelta(seconds=2)
        closed=self.session(observed=at,open=False)
        self.session(observed=at+timedelta(microseconds=1),open=True)
        selected,payload=execution_contracts._latest(self.f.catalogue,'session',self.calendar,at)
        self.assertEqual(selected,closed);self.assertFalse(payload['open'])
        for calendar,start,end,eligible in [
            ('FUTURE',at+timedelta(microseconds=1),self.end,False),
            ('JUST_VALID',self.start,at+timedelta(microseconds=1),True),
            ('EXPIRED',self.start,at,False)]:
            execution_contracts.record(self.f.catalogue,'session',calendar,{'open':False},
                source='SYNTHETIC EXACT-TIME BOUNDARY',observed_at=at.isoformat(),
                effective_from=start.isoformat(),effective_until=end.isoformat(),now=at)
            if eligible:self.assertFalse(execution_contracts._latest(self.f.catalogue,'session',calendar,at)[1]['open'])
            else:
                with self.assertRaises(ValueError):execution_contracts._latest(self.f.catalogue,'session',calendar,at)
