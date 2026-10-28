"""Dated observations must never masquerade as owned fills or approvals."""
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import unittest
from datetime import datetime, timedelta, timezone

from app import books, execution_funnel, paper_exchange, approved_execution, workspace_ui
from app.screening import automation, tracking
from tests import test_approved_execution as fixtures


class ExecutionFunnelTest(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.ApprovedPaperPipelineTest(); self.f.setUp(); self.addCleanup(self.f.doCleanups)
        self.con = self.f.con
        self.now = self.f.now
        self.path = Path(self.f.tmp.name)/'journal.db'
        journal = tracking.connect(self.path); journal.close()
        self.house_epoch = 'fixture-strategy-epoch'
        self.con.execute("INSERT OR REPLACE INTO v2_book(market,started_at) VALUES('IN',?)", (self.house_epoch,))
        self.con.commit()
        self.screen = dict(status='current',screened_count=497,generated_at=self.now.isoformat(),price_asof='2026-10-27',rejections=[])

    def read(self, uid=2, now=None, quotes=None):
        before = self.con.total_changes
        result = execution_funnel.report(self.con, uid, self.screen, quotes or {}, journal_path=self.path, now=now or self.now)
        self.assertEqual(self.con.total_changes,before)
        return {r['key']:r for r in result['stages']}

    def publication(self, *, symbol='TEST', passes=True, at=None, epoch=None, terminal=False, missing=False, price=100, stop=98):
        at = at or self.now
        plan = dict(symbol=symbol,score=90,stop=stop,t3=price*1.3,
                    automation_epoch=epoch or self.house_epoch,automation_model_version=automation.MODEL_VERSION)
        checks = [dict(code=key,passed=passes or key!='completed_rebound',reason=key+' blocker') for key in (
            'model','zone_touch','completed_rebound','next_session','fresh_quote','entry_zone','later_fill',
            'targets','regime','evidence','selection','risk')]
        assessment = dict(checked_at=at.isoformat(),checks=checks,signal_eligible=passes)
        with sqlite3.connect(self.path) as j:
            pid=j.execute('INSERT INTO publications(user_id,fingerprint,symbol,issued_at,payload) VALUES(?,?,?,?,?)',
                (automation.JOURNAL_OWNER,symbol+str(j.execute('SELECT COUNT(*) FROM publications').fetchone()[0]),symbol,
                 min(at,self.now).isoformat(),json.dumps(plan))).lastrowid
            j.execute('INSERT INTO states VALUES(?,?)',(pid,json.dumps(dict(status='STOPPED' if terminal else 'WAITING'))))
            if not missing:
                j.execute('INSERT INTO assessment_events VALUES(?,?,?,?)',(pid,'ASSESSMENT_OBSERVED',at.isoformat(),json.dumps(assessment)))
                # Convenience latest-assessment cache is deliberately absent.
        return pid

    def test_empty_journal_is_observed_zero_but_missing_journal_is_unknown(self):
        result=self.read()
        self.assertEqual([result[k]['count'] for k in ('screened','confirmed','fundable','ordered','filled','exited')],[497,0,0,0,0,0])
        self.path.unlink()
        result=self.read()
        self.assertIsNone(result['confirmed']['count']);self.assertIsNone(result['fundable']['count'])
        self.assertEqual(result['ordered']['count'],0)
        self.assertFalse(self.path.exists())

    def test_confirmed_is_not_fundable_without_fresh_account_quote(self):
        self.publication()
        result=self.read()
        self.assertEqual(result['confirmed']['count'],1)
        self.assertIsNone(result['fundable']['count'])
        quote=dict(price=100,ts=self.now.isoformat(),source='upstox-live')
        result=self.read(quotes={'TEST':quote})
        self.assertEqual(result['fundable']['count'],1)
        self.assertEqual(result['ordered']['count'],0)
        self.assertEqual(books.positions(self.con,2),[])
        self.assertEqual(books.cash(self.con,2),10000)

    def test_affordability_rejection_does_not_reduce_signal_confirmation(self):
        self.publication(price=5000,stop=4900)
        result=self.read(quotes={'TEST':dict(price=5000,ts=self.now.isoformat(),source='upstox-live')})
        self.assertEqual(result['confirmed']['count'],1)
        self.assertEqual(result['fundable']['count'],0)
        self.assertTrue(result['fundable']['blockers'])

    def test_overlapping_versions_legacy_epochs_and_terminal_plans_are_not_independent(self):
        self.publication();self.publication(passes=False)
        self.publication(symbol='OLD',epoch='old')
        self.publication(symbol='DONE',terminal=True)
        result=self.read()
        self.assertEqual(result['confirmed']['count'],0)
        self.assertIn('completed_rebound',result['confirmed']['blockers'][0]['reason'])

    def test_missing_today_and_stale_positive_are_explicit_not_current_funding(self):
        self.publication(at=self.now-timedelta(days=1))
        result=self.read();self.assertIsNone(result['confirmed']['count'])
        self.publication()
        result=self.read(now=self.now+timedelta(minutes=3),quotes={'TEST':dict(price=100,ts=(self.now+timedelta(minutes=3)).isoformat(),source='upstox-live')})
        self.assertEqual(result['confirmed']['count'],1)
        self.assertEqual(result['confirmed']['status'],'historical')
        self.assertIsNone(result['fundable']['count'])

    def test_paper_submit_later_fill_exit_counts_and_account_isolation(self):
        order=approved_execution.submit(self.con,self.f.catalogue,2,self.f.plan_id,'funnel-fixture',
            {'TEST':dict(price=100,ts=self.now.isoformat())},regime='ON',now=self.now)
        self.assertEqual(order['status'],'pending')
        self.assertEqual(self.read()['ordered']['count'],1)
        self.assertEqual(self.read()['filled']['count'],0)
        self.f.fill_pending()
        at=self.now+timedelta(seconds=2)
        result=self.read(now=at);self.assertEqual(result['filled']['count'],1)
        self.f.fill_pending();self.assertEqual(self.read(now=at)['filled']['count'],1)
        position=books.positions(self.con,2)[0]
        paper_exchange.queue_exit(self.con,2,position['id'],'fixture target',now=at)
        self.assertEqual(self.read(now=at)['exited']['count'],0)
        at+=timedelta(seconds=1)
        paper_exchange.service_exits(self.con,{'TEST':dict(price=110,ts=at.isoformat(),execution=self.f.snapshot(at,price=110.05))},now=at)
        self.assertEqual(self.read(now=at)['exited']['count'],1)
        books.ensure_book(self.con,3);self.con.commit()
        other=self.read(uid=3,now=at)
        self.assertEqual([other[k]['count'] for k in ('ordered','filled','exited')],[0,0,0])
        self.con.execute("UPDATE user_book SET started_at='new-epoch' WHERE user_id=2");self.con.commit()
        self.assertEqual(self.read(now=at)['filled']['count'],0)

    def test_ist_day_and_fill_date_are_independent_of_submission_date(self):
        # A real durable intent with an immutable fill just after IST midnight.
        midnight=datetime(2026,10,28,18,30,tzinfo=timezone.utc)
        epoch=books.current_epoch(self.con,2)
        self.con.execute('INSERT INTO paper_order_intents VALUES(?,?,?,?,?,?,?)',
            ('overnight',2,epoch,'fixture-plan','fixture-key',json.dumps(dict(plan=dict(market='IN',side='BUY'))),(midnight-timedelta(seconds=1)).isoformat()))
        for _ in range(2):
            self.con.execute('INSERT INTO paper_order_events(order_id,kind,payload,observed_at) VALUES(?,?,?,?)',
                ('overnight','FILLED','{}',midnight.isoformat()))
        # No mutable state is needed to count the immutable fill.
        self.con.commit()
        result=self.read(now=midnight+timedelta(seconds=1))
        # Missing state is surfaced by account integrity, not silently labelled fundable.
        self.assertEqual(result['ordered']['count'],0)
        self.assertEqual(result['filled']['count'],1)


@unittest.skipUnless(shutil.which('node'),'Node required for actual renderer')
class FunnelRendererTest(unittest.TestCase):
    def test_six_dated_stages_unknowns_and_escaped_blockers(self):
        js=workspace_ui.JS.split('function wsFunnel(',1)[1].split('function wsPipeline(',1)[0]
        data=dict(user_id=2,session_date='2026-10-29',note='Different populations',stages=[
            dict(key='confirmed',label='Confirmed',count=0,status='observed',as_of='2026-10-29T04:00:00Z',
                 blockers=[dict(count=3,reason='<img onerror=bad>')])])
        script="function esc(v){return String(v??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;');}\nfunction wsFunnel("+js+'\nconsole.log(wsFunnel('+json.dumps(data)+'));'
        result=subprocess.run(['node','-e',script],text=True,capture_output=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)
        for text in ('Screened','Confirmed','Fundable','Ordered','Filled','Exited','2026-10-29','09:30 IST','&lt;img','—','Blockers & source dates'):
            self.assertIn(text,result.stdout)
        self.assertNotIn('<img',result.stdout)
        source=(Path(__file__).resolve().parents[1]/'app/product_assets/workspace.js').read_text()
        self.assertIn("String(DESK_DATA.execution_funnel.user_id)===String(ME.id)",source)
        self.assertIn("<div id=ideasHead></div><div id=ideasList",source)
