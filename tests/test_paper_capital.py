"""An explicit capital allocation cannot erase trades or another user's book."""
from datetime import timedelta
import sqlite3
import unittest

from app import books, paper_capital, paper_ledger
from app.sleeves.accounting import session_pnl
from app.sleeves.risk import RiskManager, BookState
from app.sleeves.base import Candidate
from tests import test_approved_execution as fixtures


class PaperCapitalTest(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.ApprovedPaperPipelineTest();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.con=self.f.con
        self.old=(self.f.now-timedelta(days=2)).isoformat()
        self.con.execute("UPDATE v2_book SET budget=10000,started_at=? WHERE market='IN'",(self.old,))
        self.con.execute("UPDATE user_book SET started_at=? WHERE user_id=2 AND market='IN'",(self.old,))
        books.ensure_book(self.con,3)
        self.con.execute("INSERT OR REPLACE INTO user_equity VALUES(2,'IN',?,10000,10000,0,0)",(self.old[:10],))
        self.con.commit()

    def allocate(self,**kwargs):
        return paper_capital.allocate(self.con,2,100000,request_key='fixture-allocation',reference='explicit synthetic instruction',now=self.f.now,**kwargs)

    def test_capital_risk_cash_pnl_and_ledger_use_new_amount_without_other_owner_change(self):
        receipt=self.allocate()
        self.assertEqual(books.current_epoch(self.con,2),receipt['epoch'])
        st=books.stats(self.con,2,'IN',{})
        for key in ('budget','cash','equity'):self.assertEqual(st[key],100000)
        for key in ('overall_pnl','realised','unrealised','positions','trades'):self.assertEqual(st[key],0)
        self.assertEqual(books.budget_of(self.con,3),10000)
        self.assertEqual(self.con.execute("SELECT budget FROM v2_book WHERE market='IN'").fetchone()[0],100000)
        state,error=books.risk_state(self.con,2,quotes={})
        self.assertFalse(error);self.assertEqual(state.capital,100000)
        self.assertEqual(state.day_pnl,0)
        self.assertEqual(state.peak_equity,100000)
        self.assertFalse(RiskManager().halted(state)[0])
        candidate=Candidate('TEST','quality_momentum',.9,2000,1900,target=2400)
        larger=RiskManager().size(candidate,state)
        smaller=RiskManager().size(candidate,BookState(10000,10000,0,0,{},10000,10000,0))
        self.assertGreater(larger.shares,smaller.shares)
        self.assertLessEqual(larger.risk_amount,1500)
        self.assertLessEqual(larger.notional,30000)
        report=paper_ledger.report(self.con,2,'IN',receipt['epoch'],100000)
        self.assertEqual(report['cash_difference_minor'],0);self.assertTrue(report['balanced'])
        self.assertEqual(session_pnl(self.con,'IN',100000,self.f.now.astimezone(books.IST).date().isoformat(),receipt['epoch'],100000,False),0)
        self.assertEqual(books.equity_series(self.con,2)[-1][1],100000)
        self.assertEqual(len(books.equity_series(self.con,2)),1)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM user_equity WHERE date=?",(self.old[:10],)).fetchone()[0],1)
        self.assertEqual(self.allocate(),receipt)
        with self.assertRaises(sqlite3.IntegrityError):self.con.execute('DELETE FROM paper_capital_allocations')
        self.con.rollback()

    def test_open_position_refuses_atomically(self):
        from app import approved_execution
        self.f.plan_id=approved_execution.approve(self.con,dict(self.f.plan,epoch=self.old),approved_by='fixture',approval_reference='synthetic',now=self.f.now)
        self.f.submit()
        with self.assertRaisesRegex(ValueError,'Open positions'):self.allocate()
        self.assertEqual(books.budget_of(self.con,2),10000)
        self.assertEqual(len(books.positions(self.con,2)),1)

    def test_pending_order_refuses_and_no_epoch_changes(self):
        from app import approved_execution
        # Bind fixture approval to the intentionally older synthetic epoch.
        plan=dict(self.f.plan,epoch=self.old)
        pid=approved_execution.approve(self.con,plan,approved_by='fixture',approval_reference='synthetic',now=self.f.now)
        approved_execution.submit(self.con,self.f.catalogue,2,pid,'pending-capital',{'TEST':dict(price=100,ts=self.f.now.isoformat())},regime='ON',now=self.f.now)
        with self.assertRaisesRegex(ValueError,'Unresolved'):self.allocate()
        self.assertEqual(books.current_epoch(self.con,2),self.old)

    def test_current_epoch_loss_cannot_be_hidden_and_legacy_history_is_preserved(self):
        self.con.execute("INSERT INTO user_trades(user_id,market,symbol,pnl,book_epoch) VALUES(2,'IN','OLD',-50,'legacy')")
        self.con.commit()
        self.allocate()
        self.assertEqual(self.con.execute('SELECT pnl FROM user_trades').fetchone()[0],-50)
        self.con.execute("INSERT INTO user_trades(user_id,market,symbol,pnl,book_epoch) VALUES(2,'IN','NEW',-5,?)",(books.current_epoch(self.con,2),))
        self.con.commit()
        with self.assertRaisesRegex(ValueError,'Current-epoch trades'):
            paper_capital.allocate(self.con,2,200000,request_key='different',reference='fixture',now=self.f.now+timedelta(seconds=1))

    def test_invalid_capital_and_rebound_identity_refuse(self):
        for capital in (True,float('inf'),float('nan'),0,10000):
            with self.assertRaises(ValueError):paper_capital.allocate(self.con,2,capital,request_key='bad',reference='fixture',now=self.f.now)
        self.allocate()
        with self.assertRaises(ValueError):paper_capital.allocate(self.con,3,100000,request_key='fixture-allocation',reference='explicit synthetic instruction',now=self.f.now)

    def test_ledger_failure_rolls_back_both_capital_changes(self):
        from unittest.mock import patch
        with patch.object(paper_ledger,'anchor',side_effect=RuntimeError('fixture disk failure')):
            with self.assertRaises(RuntimeError):self.allocate()
        self.assertEqual(books.budget_of(self.con,2),10000)
        self.assertEqual(self.con.execute("SELECT budget FROM v2_book WHERE market='IN'").fetchone()[0],10000)
        self.assertEqual(books.current_epoch(self.con,2),self.old)
