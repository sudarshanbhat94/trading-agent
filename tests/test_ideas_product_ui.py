"""Exercise shipped idea controls, including failed saves and blocked order review."""
import json
import shutil
import sqlite3
import subprocess
from unittest.mock import patch

import pytest

from app import books, v2_web
from app.screening.plans import shortlist
from tests.test_stock_plans import screen, book, NOW


def run_js(tmp_path, body, preview=None):
    spa=v2_web.SPA_HTML
    start=spa.index('function renderStockPlans(');end=spa.index('function renderEvidenceScreen(',start)
    data=preview or shortlist(screen(3),book(),now=NOW)
    nodes="const nodes={};const document={getElementById:id=>nodes[id]||(nodes[id]={style:{},open:false,showModal(){this.open=true;}}),querySelectorAll:()=>[]};"
    js=nodes+"const INR=new Intl.NumberFormat('en-IN');function esc(x){return String(x??'').replace(/</g,'&lt;')}function loadWL(){}const notices=[];function toast(s){notices.push(s);}\n"
    js+=spa[start:end]+f"\nconst IDEAS={{stock_plans:{json.dumps(data)},decision:{{regime:'OFF',decision_stale:false}}}};\n"
    js+='(async()=>{'+body+'})().catch(e=>{console.error(e);process.exit(1)});'
    path=tmp_path/'ideas-product.js';path.write_text(js)
    result=subprocess.run(['node',str(path)],capture_output=True,text=True,timeout=20)
    assert result.returncode==0,result.stderr
    return json.loads(result.stdout)


pytestmark=pytest.mark.skipif(not shutil.which('node'),reason='node required')


def test_decision_history_retains_failure_reason_and_renders_untrusted_text_safely(tmp_path):
    result=run_js(tmp_path,"""
    IDEA_ASSESSMENT_CACHE[1]={total:1,events:[{kind:'ASSESSMENT_OBSERVED',observed_at:'2026-10-06T04:00:00Z',assessment:{checks:[{code:'risk',passed:false,reason:'<img src=x onerror=alert(1)> refused'}]}}]};
    console.log(JSON.stringify({html:ideaAssessmentHtml(1),empty:ideaAssessmentHtml(2)}));""")
    assert '&lt;img' in result['html'] and '<img' not in result['html']
    assert 'risk:' in result['html'] and 'not orders or fills' in result['html']
    assert 'why this idea was waiting' in result['empty']


def test_search_sector_saved_views_and_empty_state(tmp_path):
    data=shortlist(screen(3),book(),now=NOW);data['watchlisted']=['STOCK1'];data['ideas'][2]['sector']='Healthcare'
    result=run_js(tmp_path,"""
    const all=ideaFiltered(IDEAS.stock_plans).map(x=>x.symbol);
    ideaSetView('saved');const saved=ideaFiltered(IDEAS.stock_plans).map(x=>x.symbol);
    ideaSetView('discover');ideaFilter('stock2');const searched=ideaFiltered(IDEAS.stock_plans).map(x=>x.symbol);
    ideaFilter('');ideaSector('Healthcare');const sector=ideaFiltered(IDEAS.stock_plans).map(x=>x.symbol);
    ideaFilter('no_match');const empty=ideaCards(IDEAS.stock_plans);
    console.log(JSON.stringify({all,saved,searched,sector,empty}));""",data)
    assert result['all']==['STOCK0','STOCK1','STOCK2']
    assert result['saved']==['STOCK1']
    assert result['searched']==result['sector']==['STOCK2']
    assert 'No matching ideas' in result['empty']


def test_no_qualifying_setup_explains_rejection_without_a_daily_quota(tmp_path):
    data=screen(1);data['equities'][0]['metrics']['relative_volume']=1
    preview=shortlist(data,book(),now=NOW)
    result=run_js(tmp_path,"console.log(JSON.stringify({html:renderStockPlans(IDEAS.stock_plans)}));",preview)
    assert 'No qualifying setups' in result['html']
    assert 'No daily quota; some sessions have none' in result['html']
    assert 'Participation gate failed' in result['html']
    assert 'Try another search' not in result['html'] and 'Show all ideas' not in result['html']
    assert '<article' not in result['html']


def test_card_exposes_after_cost_scenario_and_v3_tracks_confirmation(tmp_path):
    result=run_js(tmp_path,"console.log(JSON.stringify({html:renderStockPlans(IDEAS.stock_plans),confirmation:ideaConfirmationHtml({plan:IDEAS.stock_plans.ideas[0]})}));")
    assert result['html'].count('<article')==3
    assert 'Final-target net scenario' in result['html'] and 'after costs; not a forecast' in result['html']
    assert 'waiting for the next evidence check' in result['confirmation']
    assert 'legacy first-touch' not in result['confirmation']


def test_failed_save_keeps_previous_state_and_success_can_add_and_remove(tmp_path):
    result=run_js(tmp_path,"""
    const calls=[];let success=false;
    api=async(u,o)=>{calls.push({u,o});return {ok:success,j:success?{ok:true}:{error:'Subscription required'}};};
    await ideaToggleWatch('STOCK0');const failed=(IDEAS.stock_plans.watchlisted||[]).slice();
    success=true;await ideaToggleWatch('STOCK0');const added=IDEAS.stock_plans.watchlisted.slice();
    const card=ideaCards(IDEAS.stock_plans);await ideaToggleWatch('STOCK0');
    console.log(JSON.stringify({failed,added,removed:IDEAS.stock_plans.watchlisted,notices,calls,card,pending:IDEA_UI.pending}));""")
    assert result['failed']==result['removed']==[]
    assert result['added']==['STOCK0']
    assert result['notices'][0]=='Subscription required'
    assert result['calls'][1]['o']['method']=='POST'
    assert json.loads(result['calls'][1]['o']['body'])==dict(symbol='STOCK0',market='IN')
    assert result['calls'][2]['o']['method']=='DELETE'
    assert result['calls'][2]['u']=='/v2/api/watchlist/STOCK0?market=IN'
    assert 'Remove STOCK0 from watchlist' in result['card']
    assert result['pending']=={}


def test_pending_save_cannot_be_double_submitted(tmp_path):
    result=run_js(tmp_path,"""
    let calls=0,resolve;api=()=>{calls++;return new Promise(r=>{resolve=r});};
    const first=ideaToggleWatch('STOCK0');await ideaToggleWatch('STOCK0');resolve({ok:true,j:{ok:true}});await first;
    console.log(JSON.stringify({calls,saved:IDEAS.stock_plans.watchlisted}));""")
    assert result==dict(calls=1,saved=['STOCK0'])


def test_order_review_is_explicit_and_cannot_submit_unapproved_stock(tmp_path):
    result=run_js(tmp_path,"""
    let calls=0;api=()=>{calls++;throw new Error('Unexpected request');};
    ideaOpenPlan('STOCK0',true);const review=nodes.ideaDialogBody.innerHTML;
    ideaOpenPlan('STOCK0',false);const detail=nodes.ideaDialogBody.innerHTML;
    console.log(JSON.stringify({review,detail,calls,open:nodes.ideaPlanDialog.open}));""")
    assert result['open'] is True and result['calls']==0
    assert 'ORDER REVIEW · PAPER' in result['review']
    assert 'Buy unavailable · research plan' in result['review']
    assert 'disabled>' in result['review']
    assert 'Stock execution has not been approved' in result['review']
    assert 'Execution regime: OFF' in result['review']
    assert 'Estimated net at T1 / T2 / T3' in result['detail']
    assert 'Net reward / estimated stop loss' in result['detail']
    assert 'Stop-loss' in result['detail']


def test_actual_saved_state_is_personal_and_survives_read(tmp_path):
    path=tmp_path/'paper.db'
    con=sqlite3.connect(path);con.row_factory=sqlite3.Row;books.ensure_schema(con);v2_web._uwl(con)
    for uid in (2,3):con.execute('INSERT INTO user_book VALUES(?,?,?,?)',(uid,'IN',10000,NOW.isoformat()))
    con.commit();con.close()
    with patch.object(v2_web,'V2_DB',str(path)),patch.object(v2_web,'_live_map',return_value={}),patch('app.v2_live.market_open',return_value=False):
        response=v2_web.api_watchlist_add({'symbol':'STOCK0','market':'IN'},user={'id':2})
        assert json.loads(response.body)['ok']
        assert v2_web._stock_plans(screen(),'IN',{'id':2})['watchlisted']==['STOCK0']
        assert v2_web._stock_plans(screen(),'IN',{'id':3})['watchlisted']==[]
        v2_web.api_watchlist_del('STOCK0',user={'id':2})
        assert v2_web._stock_plans(screen(),'IN',{'id':2})['watchlisted']==[]
    con=sqlite3.connect(path)
    assert con.execute('SELECT COUNT(*) FROM user_positions').fetchone()[0]==0
    assert con.execute('SELECT COUNT(*) FROM user_trades').fetchone()[0]==0
    con.close()
