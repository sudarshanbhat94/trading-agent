"""Conditional previews cannot spend money or evade the existing allocator."""
import copy
import json
import shutil
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from app import books, v2_web
from app.screening import plans
from app.sleeves.risk import BookState, stop_loss_including_costs

NOW=datetime(2026,9,30,12,tzinfo=timezone.utc)


def book():
    return BookState(10000,10000,0,0,{},10000,10000,0)


def screen(count=12):
    return dict(status='ok', generated_at=NOW.isoformat(),price_asof='2026-09-30',equities=[
        dict(symbol=f'STOCK{i}',sector='Industrials',score=90-i,flags=[],
             metrics=dict(price=800.,atr_pct=4.,above50=True,rs_vs_nifty20_pct=5.,
                          sector_rs20_pct=2.,return126_pct=10.),
             fundamentals=dict(roe_pct=20),participation=dict(delivery_pct=60)) for i in range(count)])


def test_ten_ranked_alternatives_with_explicit_levels_and_cost_aware_size():
    result=plans.shortlist(screen(),book(),now=NOW)
    assert result['count']==10
    for p in result['ideas']:
        assert p['stop'] < p['entry_low'] <= p['entry_high'] < p['t1'] < p['t2'] < p['t3']
        assert p['entry_high']==768 and p['stop']==736
        assert p['t1']==832 and p['t2']==864 and p['t3']==896
        assert p['estimated_stop_loss']==round(stop_loss_including_costs(768,736,p['qty']),2)
        assert p['estimated_stop_loss']<=150
        assert 1500<=p['notional']<=3000
        assert p['notional']<=result['cash']
        assert p['estimated_net_at_targets'][0] < (p['t1']-p['entry_high'])*p['qty']
        assert p['actionable'] is False
        assert p['state']=='LIVE QUOTE UNAVAILABLE'


def test_never_pad_ten_with_missing_evidence_negative_rs_or_duplicate_symbols():
    data=screen(4)
    data['equities'][1]['flags']=['adverse filing headline']
    data['equities'][2]['metrics']['rs_vs_nifty20_pct']=-2
    data['equities'].append(copy.deepcopy(data['equities'][0]))
    result=plans.shortlist(data,book(),now=NOW)
    assert [p['symbol'] for p in result['ideas']]==['STOCK0','STOCK3']
    assert len(result['rejected'])==2


@pytest.mark.parametrize('changes', [dict(cash=100),dict(day_pnl=-150),
    dict(open_risk=120),dict(open_positions=3),dict(peak_equity=12000)])
def test_book_limits_block_preview_quantities(changes):
    b=book()
    for k,v in changes.items():setattr(b,k,v)
    assert plans.shortlist(screen(),b,now=NOW)['count']==0


def test_missing_held_quote_blocks_sizing_and_never_defaults_to_another_account():
    con=sqlite3.connect(':memory:');con.row_factory=sqlite3.Row;books.ensure_schema(con)
    con.execute('INSERT INTO user_book VALUES(?,?,?,?)',(2,'IN',10000,NOW.isoformat()))
    con.execute('INSERT INTO user_book VALUES(?,?,?,?)',(3,'IN',50000,NOW.isoformat()))
    con.execute('INSERT INTO user_positions(user_id,market,strategy,symbol,entry_price,shares,stop,book_epoch) VALUES(?,?,?,?,?,?,?,?)',
                (3,'IN','quality_momentum','OTHER',100,10,90,NOW.isoformat()))
    con.execute('INSERT INTO user_trades(user_id,market,pnl,exit_date,book_epoch) VALUES(?,?,?,?,?)',
                (2,'IN',-40000,'2026-09-30','legacy'))
    con.commit();before=con.total_changes
    b,error=plans.account_state(con,2,{},NOW)
    assert (b.capital,b.cash,b.equity,b.open_positions,b.day_pnl,error)==(10000,10000,10000,0,0,'')
    other,error=plans.account_state(con,3,{},NOW)
    assert error and other.open_positions==1
    assert plans.shortlist(screen(),other,book_error=error,now=NOW)['count']==0
    assert con.total_changes==before


@pytest.mark.parametrize('price,state', [(780,'WAIT FOR PULLBACK'),(765,'IN ZONE · CONFIRMATION NEEDED'),
    (750,'BELOW ENTRY ZONE'),(730,'INVALIDATED')])
def test_zone_never_becomes_buy_now(price,state):
    result=plans.shortlist(screen(1),book(),dict(STOCK0=dict(price=price,ts=NOW.isoformat())),NOW)
    assert result['ideas'][0]['state']==state
    assert result['ideas'][0]['actionable'] is False


def test_stale_or_future_quote_cannot_supply_entry_confirmation():
    for ts in (NOW-timedelta(hours=1),NOW+timedelta(hours=1)):
        result=plans.shortlist(screen(1),book(),dict(STOCK0=dict(price=765,ts=ts.isoformat())),NOW)
        assert result['ideas'][0]['quote_price'] is None
    data=screen(1);data['stale']=True
    assert plans.shortlist(data,book(),now=NOW)['ideas'][0]['state']=='STALE PLAN'


def test_screen_and_ideas_share_personal_preview_without_publishing():
    data=screen(1)
    with patch.object(v2_web,'_evidence_screen',return_value=data), patch.object(v2_web,'_stock_plans',return_value={'count':1}) as preview:
        response=v2_web.api_screen(user={'id':2})
    assert json.loads(response.body)['stock_plans']=={'count':1}
    assert response.headers['cache-control']=='private, no-store'
    preview.assert_called_once_with(data,'IN',{'id':2})


@pytest.mark.skipif(not shutil.which('node'),reason='node required')
def test_actual_card_renderer_shows_levels_size_watchlist_and_review_actions(tmp_path):
    spa=v2_web.SPA_HTML
    start=spa.index('function renderStockPlans(');end=spa.index('function renderEvidenceScreen(',start)
    js="const INR=new Intl.NumberFormat('en-IN'); function esc(x){return String(x??'').replace(/</g,'&lt;')}\n"
    js+=spa[start:end]+f"\nconsole.log(renderStockPlans({json.dumps(plans.shortlist(screen(1),book(),now=NOW))}));"
    path=tmp_path/'renderer.js';path.write_text(js)
    result=subprocess.run(['node',str(path)],capture_output=True,text=True,timeout=20)
    assert result.returncode==0,result.stderr
    for text in ('STOCK0','Entry range','Stop-loss','Target 1','Target 2','Target 3','2 shares','Estimated stop loss','View plan','Review buy','Add STOCK0 to watchlist','Last close'):
        assert text in result.stdout
    assert 'ideaToggleWatch' in result.stdout
    assert 'ideaBuy(' not in result.stdout
