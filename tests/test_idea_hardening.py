"""Reproduce timestamp races, unaffordable targets and look-ahead entry errors."""
import copy
import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from app.db import Database
from app import books
from app.models import Quote
from app.market_data import _upstox_quote_asof, _is_stale_quote, UpstoxMarketDataProvider
from app.screening import confirmation as c, tracking as t, plans
from scripts import v2_quote_feed as feed
from tests.test_idea_tracking import plan, publish, quote, NOW, report
from tests.test_stock_plans import screen, book
from tests.test_ideas_product_ui import run_js


def test_quote_update_time_is_distinct_from_last_trade_and_missing_is_not_live():
    assert _upstox_quote_asof({'timestamp':NOW.isoformat(),'last_trade_time':str(int((NOW-timedelta(minutes=5)).timestamp()*1000))})==NOW.isoformat()
    assert _upstox_quote_asof({'last_trade_time':str(int(NOW.timestamp()*1000))})==NOW.isoformat()
    assert _is_stale_quote(_upstox_quote_asof({}))
    assert _is_stale_quote((datetime.now(timezone.utc)+timedelta(hours=1)).isoformat())


def test_index_identity_and_partial_batch_cannot_assign_another_instruments_price():
    provider=object.__new__(UpstoxMarketDataProvider)
    data={'NSE_INDEX:Nifty 50':{'last_price':23000},'NSE_INDEX:Nifty Bank':{'last_price':55000}}
    assert provider._find_quote_item(data,feed.BENCHMARK_ROWS[0])['last_price']==23000
    assert provider._find_quote_item(data,feed.BENCHMARK_ROWS[1])['last_price']==55000
    equity={'symbol':'TEST','upstox_instrument_key':'NSE_EQ|ISIN'}
    assert provider._find_quote_item({'NSE_EQ:OTHER':{'last_price':100}},equity) is None
    assert provider._find_quote_item({'NSE_EQ:TEST':{'last_price':101,'instrument_token':equity['upstox_instrument_key']}},equity)['last_price']==101


def test_slow_response_cannot_replace_newer_quote_even_with_different_timezone(tmp_path):
    db=Database(tmp_path/'market.db')
    with db.connect() as con:
        con.execute('CREATE TABLE universe(symbol TEXT,exchange TEXT)')
        con.execute("INSERT INTO universe VALUES('TEST','NSE')")
        con.execute('CREATE TABLE latest_quotes(symbol TEXT PRIMARY KEY,ts TEXT,price REAL,open REAL,high REAL,low REAL,close REAL,volume REAL,source TEXT)')
    db.upsert_quotes({'TEST':Quote('TEST',105,'upstox-live','2026-09-30T04:01:00+00:00')})
    db.upsert_quotes({'TEST':Quote('TEST',90,'upstox-live','2026-09-30T09:30:00+05:30')})
    db.upsert_quotes({'TEST':Quote('TEST',999,'upstox-live','2099-01-01T00:00:00+00:00')})
    with db.connect() as con:assert tuple(con.execute('SELECT price,ts FROM latest_quotes').fetchone())==(105,'2026-09-30T04:01:00+00:00')


def test_profitable_t3_does_not_justify_a_loss_making_t1():
    data=screen(1);data['equities'][0]['metrics'].update(price=1900,atr_pct=2)
    result=plans.shortlist(data,book(),now=NOW)
    assert not result['ideas']
    assert 'First target is not profitable' in result['rejected'][0]['reason']


def test_new_plans_freeze_a_precise_forward_policy():
    p=plans.shortlist(screen(1),book(),now=NOW)['ideas'][0]
    assert p['model_version']=='conditional-pullback-v4' and p['confirmation_policy']==c.POLICY
    assert all(value>0 for value in p['estimated_net_at_targets'])
    assert '1.5x' in p['buy_condition'] and not p['actionable']


def test_gap_details_and_stop_price_are_recorded_without_erasing_legacy_state(tmp_path):
    db=tmp_path/'tracking.db';publish(db);quote(db,100);quote(db,94,180)
    row=report(db,now=NOW+timedelta(seconds=180))['rows'][0]
    assert row['events'][-1]['price']==94
    assert row['coverage_gaps'][0]['seconds']==180 and row['gaps']==1
    con=sqlite3.connect(db);con.execute("UPDATE events SET price=NULL WHERE kind='STOPPED'");con.commit();con.close()
    event=report(db)['rows'][0]['events'][-1]
    assert event['price'] is None and event['price_recorded_in_state']==94
    assert event['historical_event_price_missing']


def test_only_direct_timestamp_matched_benchmarks_can_establish_relative_move(tmp_path):
    db=tmp_path/'tracking.db';publish(db)
    quote(db,100);quote(db,110,60)
    for seconds,prices in [(0,(1000,10000)),(60,(1010,9900))]:
        at=(NOW+timedelta(seconds=seconds)).isoformat()
        t.observe_benchmarks(db,{s:dict(price=p,ts=at,source='upstox-live') for s,p in zip(('NIFTY','BANKNIFTY'),prices)},at)
    row=report(db,now=NOW+timedelta(seconds=60))['rows'][0]
    assert row['benchmarks']['NIFTY']['stock_minus_index_pp']==9
    assert row['benchmarks']['BANKNIFTY']['stock_minus_index_pp']==11
    other=tmp_path/'other.db';publish(other);quote(other,100);quote(other,110,60)
    assert t.observe_benchmarks(other,{'NIFTY':dict(price=1000,ts=NOW.isoformat(),source='upstox-nfo')},NOW.isoformat())==0
    assert not report(other)['rows'][0]['benchmarks']['NIFTY']['available']


def test_benchmark_capture_failure_cannot_discard_equity_quotes(tmp_path):
    class Provider:
        async def get_quotes(self,rows):
            return {'TEST':Quote('TEST',100,'upstox-live',NOW.isoformat()),'NIFTY':Quote('NIFTY',1000,'upstox-live',NOW.isoformat())}
    class DB:
        received=None
        def upsert_quotes(self,q):self.received=q
    db=DB()
    with patch.object(feed.market_regions,'market_session_for_region',return_value={'is_open':True}),patch.object(t,'observe_benchmarks',side_effect=OSError),patch.object(feed,'_cooldown',{}):
        feed._poll(db,{'IN':Provider()},{'IN':[{'symbol':'TEST'}]},'',benchmarks=True)
    assert set(db.received)=={'TEST'}


def confirmation_fixture():
    now=datetime(2026,10,6,4,tzinfo=timezone.utc)
    p=plan();p.update(model_version='conditional-pullback-v2',confirmation_policy=dict(c.POLICY),cost_profile=t.cost_profile())
    prior=[];day=datetime(2026,10,1).date()
    while len(prior)<20:
        if day.weekday()<5 and day.isoformat() not in t.INDIA_TRADING_HOLIDAYS:
            prior.append(dict(session=day.isoformat(),open=100,high=101,low=99,close=100,volume=100))
        day-=timedelta(days=1)
    bars=sorted(prior,key=lambda b:b['session'])+[dict(session='2026-10-05',open=99,high=105,low=98,close=104,volume=200,known_at=now.isoformat())]
    state=dict(entry_at='2026-10-05T04:00:00+00:00',status='ZONE_TOUCHED')
    q=dict(price=99.5,ts=now.isoformat(),source='upstox-live')
    context=dict(regime='ON',regime_fresh=True,evidence_ok=True,news_checked_at=now.isoformat(),risk_ok=True)
    return p,state,bars,q,context,now


def test_confirmation_is_not_backfilled_to_touch_or_already_observed_quote():
    p,s,b,q,ctx,now=confirmation_fixture()
    first=c.assess(p,s,b,q,ctx,now)
    assert first['confirmation_at']==now.isoformat() and not first['eligible']
    assert next(x for x in first['checks'] if x['code']=='later_fill')['passed'] is False
    q.update(ts=(now+timedelta(seconds=30)).isoformat())
    second=c.assess(p,s,b,q,ctx,now+timedelta(seconds=30),first)
    assert second['eligible'] and second['confirmation_at']==first['confirmation_at']
    assert second['production_approved'] is False


@pytest.mark.parametrize('failure',['off','news','future_bar','weak_volume','outside','risk','legacy','late_session','corrected_bar'])
def test_entry_model_fails_closed_on_each_required_condition(failure):
    p,s,b,q,ctx,now=confirmation_fixture()
    previous=c.assess(p,s,b,q,ctx,now)
    later=now+timedelta(seconds=30);q['ts']=later.isoformat()
    if failure=='off':ctx['regime']='OFF'
    if failure=='news':ctx['news_checked_at']=(now-timedelta(hours=3)).isoformat()
    if failure=='future_bar':b[-1]['known_at']=(later+timedelta(hours=1)).isoformat()
    if failure=='weak_volume':b[-1]['volume']=100
    if failure=='outside':q['price']=105
    if failure=='risk':ctx['risk_ok']=False
    if failure=='legacy':p['model_version']='conditional-pullback-v1'
    if failure=='late_session':later+=timedelta(days=1);q['ts']=later.isoformat();ctx['news_checked_at']=later.isoformat()
    if failure=='corrected_bar':b[-1]['close']=104.5
    result=c.assess(p,s,b,q,ctx,later,previous)
    assert not result['eligible']
    if failure=='corrected_bar':assert result['confirmation_at']==later.isoformat()


def test_tracking_ui_shows_machine_checks_gap_and_recorded_legacy_exit(tmp_path):
    db=tmp_path/'tracking.db';publish(db);quote(db,100);quote(db,94,180)
    row=report(db,now=NOW+timedelta(seconds=180))
    row['rows'][0]['confirmation']=dict(model_version=c.VERSION,reason='Volume insufficient',checked_at=NOW.isoformat(),fresh=True,checks=[dict(passed=False,reason='Need 1.5x volume')])
    result=run_js(tmp_path,'console.log(JSON.stringify({html:renderIdeaTracking('+__import__('json').dumps(row)+')}));')
    assert 'Need 1.5x volume' in result['html'] and 'Coverage gap:' in result['html']
    assert 'matching index observations unavailable' in result['html']


@pytest.mark.parametrize('model',['conditional-pullback-v2','conditional-pullback-v3','conditional-pullback-v4'])
def test_research_cycle_records_replayable_checks_without_writing_paper_book(tmp_path,model):
    p,s,b,q,ctx,now=confirmation_fixture();p['qty']=16;p['stop']=96
    p['model_version']=model
    if model in ('conditional-pullback-v3','conditional-pullback-v4'):
        from app.screening.selection import POLICY
        p['selection_policy']=dict(POLICY)
    market=tmp_path/'market.db';paper=tmp_path/'v2_paper.db';tracker=tmp_path/'tracker.db'
    con=sqlite3.connect(market)
    con.execute('CREATE TABLE latest_quotes(symbol TEXT,price REAL,ts TEXT,source TEXT)')
    con.execute('CREATE TABLE candles(symbol TEXT,ts TEXT,open REAL,high REAL,low REAL,close REAL,volume REAL,source TEXT)')
    con.execute('INSERT INTO latest_quotes VALUES(?,?,?,?)',('TEST',q['price'],q['ts'],q['source']))
    con.executemany('INSERT INTO candles VALUES(?,?,?,?,?,?,?,?)',
        [('TEST',bar['session'],bar['open'],bar['high'],bar['low'],bar['close'],bar['volume'],'upstox-live:day') for bar in b])
    con.execute('INSERT INTO candles VALUES(?,?,?,?,?,?,?,?)',('TEST','2026-10-06',99,100,99,99.5,1,'upstox-live:day'))
    con.commit();con.close()
    con=sqlite3.connect(paper);books.ensure_schema(con)
    con.execute('INSERT INTO user_book VALUES(?,?,?,?)',(2,'IN',10000,'2026-10-01T00:00:00+00:00'));con.commit();con.close()
    touch=datetime(2026,10,5,4,tzinfo=timezone.utc)
    t.publish(tracker,2,[p],issued_at=touch.isoformat(),now=touch)
    t.observe(tracker,{'TEST':dict(q,ts=touch.isoformat())},touch.isoformat())
    if model in ('conditional-pullback-v3','conditional-pullback-v4'):
        from tests.test_stock_plans import screen as evidence_fixture
        screen=evidence_fixture(1);screen['price_asof']='2026-10-05'
        screen['equities'][0].update(symbol='TEST',news={'checked_at':now.isoformat()})
        screen['equities'][0]['participation']['session']=screen['price_asof']
    else:
        screen=dict(price_asof='2026-10-05',equities=[dict(symbol='TEST',flags=[],news={'checked_at':now.isoformat()})])
    regime=dict(regime='ON',asof='2026-10-05',cycle_date='2026-10-06')
    before=hashlib.sha256(paper.read_bytes()).digest()
    assert c.refresh(market,tracker,paper,screen,regime,now)==1
    later=now+timedelta(seconds=30)
    con=sqlite3.connect(market);con.execute('UPDATE latest_quotes SET ts=?',(later.isoformat(),));con.commit();con.close()
    assert c.refresh(market,tracker,paper,screen,regime,later)==1
    row=t.report(tracker,2,now=later)['rows'][0]
    assert row['confirmation']['eligible'] and row['confirmation']['context']['regime']=='ON'
    con=sqlite3.connect(tracker)
    assert {r[0] for r in con.execute('SELECT kind FROM assessment_events')}=={'ASSESSMENT_OBSERVED','CONFIRMATION_RECORDED','ENTRY_ELIGIBLE_SHADOW','ZONE_ELIGIBLE_BASELINE'}
    con.close()
    assert hashlib.sha256(paper.read_bytes()).digest()==before
