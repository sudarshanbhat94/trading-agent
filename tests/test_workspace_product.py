import copy
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import unittest

from app import books, product_status, workspace_ui
from app.screening.status import research_watch
from tests.test_stock_plans import screen, NOW
from tests.test_desk_status_copy import payload
from tests import test_approved_execution as fixture_module


class WorkspaceDataTest(unittest.TestCase):
    def test_receipts_are_read_only_and_partial_sources_do_not_claim_complete(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'market.db'
            with sqlite3.connect(path) as con:
                con.execute('CREATE TABLE agent_state(key TEXT PRIMARY KEY,value TEXT)')
                con.executemany('INSERT INTO agent_state VALUES(?,?)',[
                    ('daily_candle_ingestion',json.dumps(dict(markets=[dict(status='complete')]))),
                    ('market_identity_refresh',json.dumps(dict(status='partial',issues=[dict(symbol='MISSING',reason='absent')])))])
                con.commit()
                before=con.total_changes
                self.assertEqual(product_status.market_data_status(path)['status'],'partial')
                self.assertEqual(con.total_changes,before)
                con.execute("UPDATE agent_state SET value=? WHERE key='market_identity_refresh'",(json.dumps(dict(status='complete',issues=[])),));con.commit()
                self.assertEqual(product_status.market_data_status(path)['status'],'observed')
                con.execute("UPDATE agent_state SET value=? WHERE key='daily_candle_ingestion'",(json.dumps(dict(markets=[])),));con.commit()
                self.assertEqual(product_status.market_data_status(path)['status'],'partial')

    def test_portfolio_uses_own_positions_cash_and_epoch_instead_of_house(self):
        from unittest.mock import patch
        from app import v2_web
        fixture=fixture_module.ApprovedPaperPipelineTest();fixture.setUp();self.addCleanup(fixture.doCleanups)
        fixture.submit();books.ensure_book(fixture.con,3);fixture.con.commit()
        with patch.object(v2_web,'V2_DB',str(fixture.path)),patch.object(v2_web,'_live_map',return_value={'TEST':dict(price=100)}):
            own=json.loads(v2_web.api_portfolio('IN','mine',dict(id=2,account_plan='auto')).body)
            other=json.loads(v2_web.api_portfolio('IN','mine',dict(id=3,account_plan='auto')).body)
        self.assertEqual(own['concentration']['n_positions'],1)
        self.assertEqual(own['cash'],round(books.cash(fixture.con,2),2))
        self.assertEqual(other['concentration']['n_positions'],0)
        self.assertEqual(other['cash'],10000)
        self.assertEqual(own['epoch'],books.current_epoch(fixture.con,2))

    def test_active_order_and_attribution_reads_exclude_legacy_trades_without_deleting_them(self):
        from unittest.mock import patch
        from app import v2_web
        fixture=fixture_module.ApprovedPaperPipelineTest();fixture.setUp();self.addCleanup(fixture.doCleanups)
        fixture.con.execute("INSERT INTO user_trades(user_id,market,symbol,pnl,book_epoch) VALUES(2,'IN','LEGACY',-44000,'legacy')")
        fixture.con.commit()
        with patch.object(v2_web,'V2_DB',str(fixture.path)),patch.object(v2_web,'_live_map',return_value={}):
            self.assertEqual(v2_web._my_trades(2),[])
            self.assertEqual(v2_web._my_orders(2),[])
            self.assertEqual(v2_web._my_attribution(2)['strategies'],[])
        self.assertEqual(fixture.con.execute('SELECT COUNT(*) FROM user_trades').fetchone()[0],1)
    def test_personal_api_marks_today_and_retains_quote_age(self):
        from unittest.mock import patch
        from app import v2_web
        fixture=fixture_module.ApprovedPaperPipelineTest();fixture.setUp();self.addCleanup(fixture.doCleanups)
        fixture.submit()
        at=fixture.now.isoformat()
        with patch.object(v2_web,'V2_DB',str(fixture.path)), \
             patch.object(v2_web,'_live_map',return_value={'TEST':dict(price=100,ts=at)}):
            rows=v2_web._my_positions(2)
        self.assertTrue(rows[0]['today'])
        self.assertEqual(rows[0]['quote_at'],at)
        self.assertEqual(rows[0]['quote_status'],'fresh')
    def test_ranked_research_is_dated_and_retains_failures_without_buy_permission(self):
        data=screen(12);data['equities'][0]['fundamentals']['roe_pct']=1
        before=copy.deepcopy(data)
        result=research_watch(data,NOW)
        self.assertEqual(len(result['rows']),10)
        self.assertFalse(result['executable'])
        self.assertFalse(result['rows'][0]['evidence_pass'])
        self.assertIn('Quality gate',result['rows'][0]['reason'])
        self.assertEqual(result['coverage']['earnings'],12)
        self.assertEqual(result['price_asof'],'2026-09-30')
        self.assertEqual(data,before)

    def test_stale_duplicate_and_nonfinite_scores_never_become_current_recommendations(self):
        data=screen(3);data['stale']=True
        data['equities'].append(copy.deepcopy(data['equities'][0]))
        data['equities'][1]['score']=float('nan')
        result=research_watch(data,NOW)
        self.assertEqual(result['status'],'stale')
        self.assertEqual([r['symbol'] for r in result['rows']],['STOCK2'])
        self.assertFalse(result['rows'][0]['evidence_pass'])

    def test_owned_orders_and_current_epoch_are_isolated(self):
        fixture=fixture_module.ApprovedPaperPipelineTest();fixture.setUp();self.addCleanup(fixture.doCleanups)
        fixture.submit()
        books.ensure_book(fixture.con,3);fixture.con.commit()
        own=product_status.paper_status(fixture.con,2)
        other=product_status.paper_status(fixture.con,3)
        self.assertEqual(own['orders'],dict(filled=1))
        self.assertEqual(other['orders'],{})
        self.assertIsNone(other['last_order'])
        fixture.con.execute("UPDATE user_book SET started_at='2099-01-01T00:00:00+00:00' WHERE user_id=2")
        self.assertEqual(product_status.paper_status(fixture.con,2)['orders'],{})

    def test_unreadable_or_missing_schema_is_unknown_not_zero_trades(self):
        with sqlite3.connect(':memory:') as con:
            result=product_status.paper_status(con,2)
        self.assertEqual(result['status'],'unavailable')
        self.assertIsNone(result['orders'])
        self.assertIsNone(result['completed'])


@unittest.skipUnless(shutil.which('node'),'node required')
class WorkspaceRendererTest(unittest.TestCase):
    def test_actual_receipts_and_safe_failure_reasons_are_visible(self):
        data=payload();data['market_data']=dict(candles=dict(checked_at='2026-10-11',markets=[
            dict(target='2026-10-09',fresh=491,requested=500,unresolved=4,
                 no_reported_trade=['NOACTIVITY'],quarantined=[dict(symbol='REVIEW')],
                 missing_symbols=[dict(symbol='MISSING',reason='<img src=x>')])]),
            identities=dict(issues=[dict(symbol='OLDKEY',reason='independent_exchange_identity_unverified')]))
        html=self.render(data)
        for text in ('491 / 500','1 have no reported exchange trading','4 unresolved','1 source identities require review','OLDKEY','&lt;img'):
            self.assertIn(text,html)
        self.assertNotIn('<img',html)

    def render(self,data):
        source=(Path(__file__).resolve().parents[1]/'app/v2_web.py').read_text()
        esc=next(line for line in source.splitlines() if line.startswith('function esc(x)'))
        script=esc+'''\nvar DESK_DATA,DESK_ACCOUNT='mine',REAL,MINE,HERO;
function renderIdeas(){};function posRow(){return 'position';}
function deskMoney(v){return v==null?'—':'₹'+Number(v).toLocaleString('en-IN');}
var elements={};var document={getElementById:id=>elements[id]||(elements[id]={})};
'''+workspace_ui.JS+'\nrenderDesk('+json.dumps(data)+');process.stdout.write(elements.homefeed.innerHTML);'
        r=subprocess.run(['node','-e',script],capture_output=True,text=True,timeout=10)
        self.assertEqual(r.returncode,0,r.stderr)
        return r.stdout

    def test_real_counts_rejections_cash_and_ownership_are_visible(self):
        data=payload();data['research_watch']=research_watch(screen(1),NOW)
        data['paper_pipeline']=dict(status='ok',orders={},completed=0,exits=0,worker='running')
        html=self.render(data)
        for text in ('Your portfolio','₹10,000','Stocks checked','Pending buys','Completed trades',
                     'Defensive market','STOCK0','Research only','Data coverage &amp; reasons'):
            self.assertIn(text.replace('&amp;','&') if text=='Data coverage &amp; reasons' else text,html)
        self.assertNotIn('Review buy',html)
        self.assertNotIn('Historical options record',html)
        self.assertNotIn('undefined',html)

    def test_unavailable_counts_are_not_reported_as_zero_or_running(self):
        html=self.render(payload())
        self.assertIn('Paper worker: unknown',html)
        self.assertIn('<span>Pending buys</span><b>—</b>',html)

    def test_market_and_provider_text_cannot_inject_markup(self):
        data=payload();data['research_watch']=research_watch(screen(1),NOW)
        data['research_watch']['rows'][0].update(symbol='<img src=x onerror=alert(1)>',reason='<script>bad()</script>')
        html=self.render(data)
        self.assertNotIn('<img',html);self.assertNotIn('<script>',html)
        self.assertIn('&lt;script&gt;',html)

    def test_all_positions_are_default_and_exit_levels_are_visible(self):
        self.assertIn("SUBPOS='all'",workspace_ui.JS)
        self.assertIn('Stop-loss',workspace_ui.JS)
        self.assertIn('p.quote_at',workspace_ui.JS)
        self.assertIn('Positions could not load',workspace_ui.JS)
