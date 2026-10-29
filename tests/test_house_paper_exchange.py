import json
import unittest
from datetime import timedelta
from unittest.mock import patch

from app import books, paper_exchange as exchange, approved_execution, execution_outbox, paper_ledger, v2_live
from app.sleeves.base import Candidate
from app.sleeves.risk import Allocation
from tests.test_approved_execution import ApprovedPaperPipelineTest as Fixture


class HousePaperExchangeTest(unittest.TestCase):
    def setUp(self):
        self.f=Fixture();self.f.setUp();self.addCleanup(self.f.doCleanups)
        # Synthetic ticker stands in for the current allowed ETF; this is a
        # lifecycle fixture, never an approval of this stock or strategy alpha.
        self.patch=patch('app.sleeves.index_directional.SYMBOL','TEST');self.patch.start();self.addCleanup(self.patch.stop)
        self.con=self.f.con;self.now=self.f.now
        self.candidate=Candidate('TEST','index_directional',.9,100,99,allocation_pct=.5)
        self.alloc=Allocation(self.candidate,20,2000,100)
        self.quote={'TEST':dict(price=100,ts=self.now.isoformat())}

    def enqueue(self,regime='ON'):
        return exchange.enqueue_house(self.con,self.f.catalogue,self.alloc,self.quote,regime=regime,now=self.now)

    def service(self,at=None,regime='ON'):
        at=at or self.now+timedelta(seconds=1)
        quotes={'TEST':dict(price=100,ts=at.isoformat(),execution=self.f.snapshot(at))}
        return exchange.service_house(self.con,self.f.catalogue,quotes,regime=regime,now=at)

    def test_house_proposal_cannot_fill_on_submission_and_uses_unified_risk(self):
        result=self.enqueue();self.assertEqual(result['status'],'pending')
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM v2_positions').fetchone()[0],0)
        state,epoch=exchange.house_state(self.con,self.quote,self.now)
        self.assertEqual(state.open_positions,1);self.assertLess(state.cash,10000)
        self.assertEqual(state.equity,10000);self.assertGreater(state.strategic_open_risk,0)
        self.assertEqual(self.service(self.now),[])
        filled=self.service()[0];self.assertEqual(filled['status'],'filled');self.assertEqual(filled['qty'],20)
        self.assertEqual(self.service(),[])
        with self.assertRaises(ValueError):self.enqueue()
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM v2_positions').fetchone()[0],1)
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM execution_outbox').fetchone()[0],1)

    def test_neutral_off_unpromoted_wrong_index_and_unknown_identity_refuse(self):
        for regime in ('NEUTRAL','OFF',None):
            with self.assertRaises(ValueError):self.enqueue(regime)
        self.alloc.candidate.sleeve='early_momentum'
        with self.assertRaises(ValueError):self.enqueue()
        self.alloc.candidate.sleeve='index_directional';self.alloc.candidate.symbol='OTHER'
        with self.assertRaises(ValueError):self.enqueue()
        self.assertEqual(exchange.pending(self.con),[])

    def test_pending_house_regime_change_cancels_without_resurrecting_a_fill(self):
        self.enqueue();self.assertEqual(self.service(regime='NEUTRAL')[0]['status'],'rejected')
        self.assertEqual(self.service(),[]);self.assertEqual(self.con.execute('SELECT COUNT(*) FROM v2_positions').fetchone()[0],0)

    def test_house_write_failure_restores_pending_risk_and_depth_allowance(self):
        order=self.enqueue()
        with patch.object(execution_outbox,'enqueue',side_effect=OSError('synthetic disk fault')):
            with self.assertRaises(OSError):self.service()
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM v2_positions').fetchone()[0],0)
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM paper_depth_usage').fetchone()[0],0)
        self.assertEqual(exchange.status(self.con,0,order['order_id'])['status'],'pending')
        self.assertEqual(self.service()[0]['status'],'filled')

    def test_house_to_subscriber_is_approved_pending_then_a_later_owned_fill(self):
        self.enqueue();house=self.service()[0]
        payload=json.loads(self.con.execute("SELECT payload FROM execution_outbox WHERE topic='house_entry'").fetchone()[0])
        at=self.now+timedelta(seconds=2)
        quotes={'TEST':dict(price=100,ts=at.isoformat(),execution=self.f.snapshot(at))}
        result=approved_execution.submit_house_mirror(self.con,self.f.catalogue,2,payload,quotes,regime='ON',now=at)
        self.assertEqual(result['status'],'pending');self.assertEqual(books.positions(self.con,2),[])
        self.assertEqual(approved_execution.submit_house_mirror(self.con,self.f.catalogue,2,payload,quotes,regime='ON',now=at),result)
        self.assertEqual(exchange.service(self.con,self.f.catalogue,quotes,regime='ON',now=at),[])
        at+=timedelta(seconds=1);quotes['TEST'].update(ts=at.isoformat(),execution=self.f.snapshot(at))
        self.assertEqual(exchange.service(self.con,self.f.catalogue,quotes,regime='ON',now=at)[0]['status'],'filled')
        position=books.positions(self.con,2)[0];self.assertEqual(position['src_id'],house['position_id'])
        self.assertEqual(position['target'],0);self.assertEqual(position['sleeve'],'index_directional')
        report=paper_ledger.report(self.con,2,'IN',books.current_epoch(self.con,2),books.cash(self.con,2))
        self.assertEqual(report['cash_difference_minor'],0)

    def test_closed_house_origin_cancels_subscriber_pending_entry(self):
        self.enqueue();house=self.service()[0]
        payload=json.loads(self.con.execute("SELECT payload FROM execution_outbox WHERE topic='house_entry'").fetchone()[0])
        at=self.now+timedelta(seconds=2);quotes={'TEST':dict(price=100,ts=at.isoformat(),execution=self.f.snapshot(at))}
        approved_execution.submit_house_mirror(self.con,self.f.catalogue,2,payload,quotes,regime='ON',now=at)
        v2_live.record_exit(self.con,'IN',house['position_id'],at.date().isoformat(),100,20,'fixture exit')
        at+=timedelta(seconds=1);quotes['TEST'].update(ts=at.isoformat(),execution=self.f.snapshot(at))
        result=exchange.service(self.con,self.f.catalogue,quotes,regime='ON',now=at)[0]
        self.assertEqual(result['status'],'cancelled');self.assertEqual(books.positions(self.con,2),[])
