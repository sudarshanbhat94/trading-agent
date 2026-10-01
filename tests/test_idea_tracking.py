"""Forward tracking preserves original plans without fabricating trades."""
import copy
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from app.screening import tracking as t
from app import v2_web, plans
from scripts.idea_tracker import cycle
from tests.test_ideas_product_ui import run_js

NOW=datetime(2026,10,1,4,tzinfo=timezone.utc) # NSE 09:30


def plan(symbol='TEST',qty=10):
    return dict(symbol=symbol,price_asof='2026-09-30',entry_low=99.,entry_high=100.,stop=95.,t1=110.,t2=115.,t3=120.,qty=qty)


def publish(path,uid=2,p=None):
    return t.publish(path,uid,[p or plan()],issued_at=NOW.isoformat(),now=NOW)


def quote(path,price,seconds=0,symbol='TEST',source='upstox-live',capture=None):
    at=NOW+timedelta(seconds=seconds)
    return t.observe(path,{symbol:dict(price=price,ts=at.isoformat(),source=source)},capture or at.isoformat())


def report(path,uid=2,now=NOW,**kw):
    return t.report(path,uid,now=now,**kw)


def test_first_publication_is_immutable_and_account_scoped(tmp_path):
    db=tmp_path/'tracking.db';ids=publish(db)
    original=report(db)['rows'][0]
    revised=plan();revised.update(quote_price=999,rank=7,score=99)
    assert publish(db,p=revised)==ids
    assert report(db)['rows'][0]==original
    revised['entry_high']=101
    assert publish(db,p=revised)!=ids
    publish(db,uid=3)
    assert report(db)['summary']['published']==2
    assert report(db,uid=3)['summary']['published']==1
    assert report(db,uid=4)['rows']==[]
    assert report(db,limit=1)['summary']['published']==2


@pytest.mark.parametrize('price,seconds,source,capture',[
    (99,-1,'upstox-live',NOW.isoformat()),
    (99,1,'upstox-live',NOW.isoformat()),
    (99,0,'upstox-live',(NOW+timedelta(seconds=121)).isoformat()),
    (99,0,'yahoo',NOW.isoformat()),
    (float('nan'),0,'upstox-live',NOW.isoformat()),
    (0,0,'upstox-live',NOW.isoformat()),
])
def test_bad_or_prepublication_quotes_cannot_create_entry(tmp_path,price,seconds,source,capture):
    db=tmp_path/'tracking.db';publish(db);quote(db,price,seconds,source=source,capture=capture)
    row=report(db)['rows'][0]
    assert row['status']=='WAITING' and row['samples']==0
    assert not row['events']


def test_duplicate_and_out_of_order_ticks_cannot_rewrite_path(tmp_path):
    db=tmp_path/'tracking.db';publish(db)
    quote(db,105,30);quote(db,105,30);quote(db,99,10)
    row=report(db)['rows'][0]
    assert row['samples']==1 and row['status']=='WAITING'
    assert row['last_price']==105


def test_targets_before_entry_not_winners_and_gap_below_stop_is_invalidated(tmp_path):
    db=tmp_path/'tracking.db';publish(db)
    quote(db,121);quote(db,94,30)
    data=report(db);row=data['rows'][0]
    assert row['status']=='INVALIDATED'
    assert data['summary']['t1']==data['summary']['wins']==data['summary']['resolved']==0
    assert data['summary']['scenario_win_pct'] is None
    assert 'scenario_net' not in row


def test_touch_then_targets_and_stop_marks_actual_gap_price_and_frozen_costs(tmp_path):
    db=tmp_path/'tracking.db';publish(db)
    quote(db,99.5);quote(db,111,30);quote(db,116,60);quote(db,93,90)
    data=report(db);row=data['rows'][0]
    assert [e['kind'] for e in row['events']]==['ZONE_TOUCHED','TARGET_1_TOUCH','TARGET_2_TOUCH','STOPPED']
    assert row['net']==round(t.net(row['plan'],99.5,93),2)
    assert row['exit_price']==93 and row['net']<0
    assert row['r']<-1 and data['summary']['scenario_win_pct']==0
    with patch.object(t.costs,'BROKERAGE_FLAT',999):
        publish(db)
        assert report(db)['rows'][0]['scenario_net']==row['net']
    quote(db,125,120)
    assert report(db)['rows'][0]['status']=='STOPPED'


def test_all_losses_and_winners_stay_in_summary_after_new_versions(tmp_path):
    db=tmp_path/'tracking.db';publish(db);publish(db,p=plan('WIN'))
    quote(db,100);quote(db,94,30)
    quote(db,100,symbol='WIN');quote(db,121,30,symbol='WIN')
    publish(db,p=plan('NEW'))
    data=report(db,limit=1)
    assert data['summary']['published']==3 and len(data['rows'])==1
    assert data['summary']['resolved']==2 and data['summary']['scenario_win_pct']==50
    assert data['summary']['t1']==1 and data['summary']['t3']==1
    allrows=report(db)['rows']
    assert data['summary']['scenario_avg_r']==round(sum(r['r'] for r in allrows if r.get('exit_at'))/2,4)


def test_incomplete_and_invalid_levels_never_partially_publish(tmp_path):
    db=tmp_path/'tracking.db';bad=plan('BAD');bad['stop']=110
    with pytest.raises(ValueError):t.publish(db,2,[plan(),bad],now=NOW)
    assert report(db)['summary']['published']==0
    with pytest.raises(ValueError):t.publish(db,2,[plan()],issued_at=(NOW+timedelta(seconds=1)).isoformat(),now=NOW)


def test_session_calendar_expiry_and_active_requires_real_fresh_exit_quote(tmp_path):
    db=tmp_path/'tracking.db';publish(db);publish(db,p=plan('PENDING'))
    quote(db,100)
    # No expiry fee or fabricated liquidation for entered scenario without a quote.
    later=NOW+timedelta(days=90)
    t.observe(db,{},later.isoformat())
    rows={r['symbol']:r for r in report(db,now=later)['rows']}
    assert rows['TEST']['status']=='ZONE_TOUCHED' and not rows['TEST'].get('exit_at')
    assert rows['PENDING']['status']=='EXPIRED_UNTOUCHED' and 'scenario_net' not in rows['PENDING']
    assert t.sessions_between(NOW.isoformat(),'2026-10-05T04:00:00+00:00')==1 # holiday+weekend excluded
    t.observe(db,{'TEST':dict(price=101,ts='2027-01-04T04:00:00+00:00')},'2027-01-04T04:00:00+00:00')
    assert report(db,now=later)['rows'][1]['status']=='TIME_EXIT'


def test_regular_session_only_and_whole_missing_session_is_a_gap(tmp_path):
    db=tmp_path/'tracking.db';publish(db)
    quote(db,99,seconds=7*3600) # after NSE close
    assert report(db)['rows'][0]['samples']==0
    quote(db,106)
    # Oct 5 skips Oct 1's remaining session plus holiday/weekend.
    t.observe(db,{'TEST':dict(price=105,ts='2026-10-05T04:00:00+00:00')},'2026-10-05T04:00:00+00:00')
    assert report(db)['rows'][0]['gaps']==1


def test_rejects_real_or_renamed_books_before_ddl(tmp_path):
    for name in ('v2_paper.db','trading_agent.db','screening.db'):
        path=tmp_path/name
        with pytest.raises(ValueError):t.connect(path)
        assert not path.exists()
    path=tmp_path/'renamed.db';con=sqlite3.connect(path);con.execute('CREATE TABLE user_book(cash)');con.commit();con.close()
    with pytest.raises(ValueError):t.connect(path)
    con=sqlite3.connect(path);assert con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()==[('user_book',)]


def test_daemon_reads_main_feed_only_and_report_does_not_write(tmp_path):
    main=tmp_path/'main.db';db=tmp_path/'tracking.db';publish(db)
    con=sqlite3.connect(main);con.execute('CREATE TABLE latest_quotes(symbol,price,ts,source)')
    con.execute('INSERT INTO latest_quotes VALUES(?,?,?,?)',('TEST',99,NOW.isoformat(),'upstox-live'));con.commit();con.close()
    original=main.read_bytes()
    with patch.object(t,'datetime',wraps=datetime) as dt:
        dt.now.return_value=NOW
        data=cycle(main,db)
    assert data['fresh_symbols']==1 and report(db)['rows'][0]['status']=='ZONE_TOUCHED'
    assert main.read_bytes()==original
    before=db.stat().st_mtime_ns;report(db);assert db.stat().st_mtime_ns==before
    with pytest.raises(ValueError):cycle(main,main)


def test_initial_capture_import_does_not_backfill_or_duplicate(tmp_path):
    db=tmp_path/'tracking.db';seed=tmp_path/'seed.json';samples=tmp_path/'capture.jsonl'
    seed.write_text(json.dumps(dict(user_id=2,issued_at=NOW.isoformat(),plans=dict(ideas=[plan()]))))
    data=[dict(captured_at=NOW.isoformat(),quotes={'TEST':dict(price=99,ts=(NOW-timedelta(seconds=1)).isoformat())}),
          dict(captured_at=(NOW+timedelta(seconds=30)).isoformat(),quotes={'TEST':dict(price=105,ts=(NOW+timedelta(seconds=30)).isoformat())})]
    samples.write_text('\n'.join(map(json.dumps,data))+'\n{')
    assert t.import_bootstrap(db,seed,samples)==2
    assert t.import_bootstrap(db,seed,samples)==2
    row=report(db)['rows'][0]
    assert row['samples']==1 and row['status']=='WAITING'
    assert row['issued_at']==NOW.isoformat() and row['first_price']==105


def test_private_api_is_personal_paginated_and_does_not_publish(tmp_path):
    db=tmp_path/'tracking.db';publish(db);publish(db,uid=3)
    with patch.dict('os.environ',{'IDEA_TRACKING_DB':str(db)}):
        response=v2_web.api_idea_tracking(user={'id':2},limit=1,offset=0)
        result=json.loads(response.body)
        assert result['summary']['published']==1
        assert result['rows'][0]['id']==list(publish(db).values())[0]
        assert response.headers['cache-control']=='private, no-store'
        assert plans.ROUTE_FEATURES['/v2/api/idea-tracking']=='ideas'
        with pytest.raises(Exception) as e:v2_web.api_idea_tracking(user={'id':2},limit=201,offset=0)
        assert e.value.status_code==400


def test_ui_renders_frozen_original_history_without_claiming_actual_pnl(tmp_path):
    db=tmp_path/'tracking.db';publish(db);quote(db,100);quote(db,94,30)
    data=report(db);data['rows'][0]['symbol']='<img>' # escaped, not interpreted
    result=run_js(tmp_path,'console.log(JSON.stringify({html:renderIdeaTracking('+json.dumps(data)+'),empty:renderIdeaTracking({status:"unavailable",note:"No feed"})}));')
    assert 'Your ideas, from first publication' in result['html']
    assert 'Stop observed' in result['html'] and 'Original plan #1' in result['html']
    assert 'Hypothetical zone-touch scenarios' in result['html'] and 'win rate 0%' in result['html']
    assert 'not actual paper trades' in result['html']
    assert '&lt;img>' in result['html']
    assert 'Tracking unavailable' in result['empty']


def test_delivery_archives_first_plan_and_keeps_paper_book_bytes_unchanged(tmp_path):
    from app import books
    from tests.test_stock_plans import screen
    paper=tmp_path/'paper.db';db=tmp_path/'tracking.db'
    con=sqlite3.connect(paper);books.ensure_schema(con)
    con.execute('INSERT INTO user_book VALUES(?,?,?,?)',(2,'IN',10000,NOW.isoformat()))
    con.commit();con.close();before=paper.read_bytes()
    with patch.dict('os.environ',{'IDEA_TRACKING_DB':str(db)}),patch.object(v2_web,'V2_DB',str(paper)),patch.object(v2_web,'_live_map',return_value={}),patch('app.v2_live.market_open',return_value=True):
        one=v2_web._stock_plans(screen(2),'IN',{'id':2},publish=True)
        two=v2_web._stock_plans(screen(2),'IN',{'id':2},publish=True)
    assert one['count']==two['count']==2
    assert [r['tracking_id'] for r in one['ideas']]==[r['tracking_id'] for r in two['ideas']]
    assert one['tracking']['summary']['published']==two['tracking']['summary']['published']==2
    assert all(r['tracking']['issued_at'] for r in one['ideas'])
    assert paper.read_bytes()==before


def test_ui_tracking_tab_and_poll_read_only_with_explicit_failure(tmp_path):
    from tests.test_stock_plans import screen,book,NOW as PREVIEW_NOW
    from app.screening.plans import shortlist
    db=tmp_path/'tracking.db';publish(db);quote(db,105)
    data=shortlist(screen(1),book(),now=PREVIEW_NOW);data['tracking']=report(db)
    data['ideas'][0]['tracking_id']=1
    result=run_js(tmp_path,"""
    IDEA_UI.view='tracking';const initial=renderStockPlans(IDEAS.stock_plans);
    const calls=[];api=async(u,o)=>{calls.push({u,o});return {ok:true,j:IDEAS.stock_plans.tracking};};
    await ideaRefreshTracking();const updated=nodes.ideaTrackingPanel.innerHTML;
    api=async()=>({ok:false,j:{detail:'Tracking service unavailable'}});
    await ideaRefreshTracking();const failed=nodes.ideaTrackingPanel.innerHTML;
    console.log(JSON.stringify({initial,updated,failed,calls}));""",data)
    assert 'id=ideaCards hidden' in result['initial']
    assert 'id=ideaFilters hidden' in result['initial']
    assert 'Your ideas, from first publication' in result['updated']
    assert 'Tracking service unavailable' in result['failed']
    assert result['calls']==[{'u':'/v2/api/idea-tracking?offset=0'}]


def test_health_does_not_disclose_other_subscribers_coverage(tmp_path):
    db=tmp_path/'tracking.db';publish(db);publish(db,uid=3,p=plan('OTHER'))
    quote(db,105)
    own=report(db)
    assert own['health']['active_symbols']==own['health']['fresh_symbols']==1
    assert own['health']['missing_symbols']==[]
    assert report(db,uid=3)['health']['missing_symbols']==['OTHER']


def test_quote_refresh_updates_card_but_preserves_original_plan_and_marks_stale(tmp_path):
    result=run_js(tmp_path,"""
    const p=IDEAS.stock_plans.ideas[0],frozen=[p.entry_low,p.entry_high,p.stop,p.t1,p.t2,p.t3,p.qty];
    const row={symbol:p.symbol,last_price:p.entry_high,last_at:new Date().toISOString(),quote_fresh:true};
    ideaSyncObservedQuote(p,row);const fresh={price:p.quote_price,state:p.state};
    row.last_at=new Date(Date.now()-121000).toISOString();ideaSyncObservedQuote(p,row);
    const stale={price:p.quote_price,state:p.state};
    console.log(JSON.stringify({fresh,stale,frozen,after:[p.entry_low,p.entry_high,p.stop,p.t1,p.t2,p.t3,p.qty]}));""")
    assert result['fresh']['state']=='IN ZONE · CONFIRMATION NEEDED'
    assert result['stale']=={'price':None,'state':'LIVE QUOTE UNAVAILABLE'}
    assert result['frozen']==result['after']
