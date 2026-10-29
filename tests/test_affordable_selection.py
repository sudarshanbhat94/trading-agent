"""Synthetic account funding checks; no change to selection or execution policy."""
import copy
from dataclasses import asdict, replace
from datetime import timedelta
import json
import sqlite3
from types import SimpleNamespace

import pytest

from app import books, paper_exchange
from app.costs import entry_charge
from app.screening import automation, plans, selection, tracking, validation
from app.sleeves.base import Candidate
from app.sleeves.config import SLEEVES
from app.sleeves.regime import RegimeView
from app.sleeves.risk import RiskManager, SLIPPAGE, stop_loss_including_costs
from tests.test_stock_plans import NOW, book, screen


def candidate(entry=250., stop=242., target=300.):
    return Candidate('SYNTHETIC', 'quality_momentum', .9, entry, stop, target=target)


def reservation(con, key, uid, epoch, *, cash, notional, risk, status='pending'):
    payload=dict(plan=dict(symbol=key, sleeve='quality_momentum'),
                 cash_required=cash, notional=notional, risk_amount=risk)
    con.execute('INSERT INTO paper_order_intents VALUES(?,?,?,?,?,?,?)',
                (key, uid, epoch, key, key, json.dumps(payload), NOW.isoformat()))
    con.execute('INSERT INTO paper_order_state VALUES(?,?,?,?)', (key, status, 'synthetic', '{}'))


def test_pending_reservations_are_account_epoch_scoped_and_preview_never_writes():
    with sqlite3.connect(':memory:') as con:
        con.row_factory=sqlite3.Row
        books.ensure_schema(con);paper_exchange.ensure_schema(con)
        epoch=NOW.isoformat()
        for uid in (2,3):
            con.execute('INSERT INTO user_book VALUES(?,?,?,?)', (uid, 'IN', 10000, epoch))
        reservation(con,'OWN',2,epoch,cash=2000,notional=1900,risk=90)
        reservation(con,'OTHER_USER',3,epoch,cash=3000,notional=2900,risk=40)
        reservation(con,'OLD_EPOCH',2,(NOW-timedelta(days=30)).isoformat(),cash=4000,notional=3900,risk=100)
        reservation(con,'CANCELLED',2,epoch,cash=6000,notional=5900,risk=140,status='cancelled')
        con.commit()
        before=list(con.iterdump());changes=con.total_changes
        con.execute('PRAGMA query_only=ON')
        own,error=plans.account_state(con,2,{},NOW)
        other,other_error=plans.account_state(con,3,{},NOW)
        assert error==other_error==''
        assert (own.cash,own.deployed,own.open_risk,own.open_positions)==(8000,1900,90,1)
        assert (other.cash,other.deployed,other.open_risk,other.open_positions)==(7000,2900,40,1)
        assert own.per_sleeve_positions=={'quality_momentum':1}
        assert own.per_sleeve_notional=={'quality_momentum':1900}
        assert own.equity==other.equity==10000
        result=plans.shortlist(screen(1),own,now=NOW)
        assert result['ideas']==[]
        assert 'sleeve full' in result['funding_rejections'][0]['reason']
        assert con.total_changes==changes and list(con.iterdump())==before


@pytest.mark.parametrize('status', ['filling','unknown'])
def test_unresolved_order_integrity_blocks_affordability_preview(status):
    with sqlite3.connect(':memory:') as con:
        con.row_factory=sqlite3.Row
        books.ensure_schema(con);paper_exchange.ensure_schema(con)
        con.execute('INSERT INTO user_book VALUES(?,?,?,?)',(2,'IN',10000,NOW.isoformat()))
        reservation(con,'UNRESOLVED',2,NOW.isoformat(),cash=2000,notional=1900,risk=90,status=status)
        con.commit();changes=con.total_changes
        con.execute('PRAGMA query_only=ON')
        account,error=plans.account_state(con,2,{},NOW)
        assert error
        result=plans.shortlist(screen(1),account,now=NOW,book_error=error)
        assert result['count']==0 and result['book_error']==error
        assert con.total_changes==changes


def test_expensive_quality_leader_falls_through_without_promoting_a_quality_failure():
    data=screen(3)
    data['equities'][0]['metrics']['price']=4000
    data['equities'][1]['sector']=data['equities'][0]['sector']
    data['equities'][2].update(score=99)
    data['equities'][2]['metrics']['price']=250
    data['equities'][2]['fundamentals']['roe_pct']=1
    original=copy.deepcopy(data)
    result=plans.shortlist(data,book(),now=NOW)
    assert data==original
    assert [r['symbol'] for r in result['ideas']]==['STOCK1']
    idea=result['ideas'][0]
    assert (idea['quality_rank'],idea['funding_rank'],idea['rank'])==(2,1,1)
    assert idea['selection_policy']==selection.POLICY
    refusal=result['funding_rejections'][0]
    assert (refusal['symbol'],refusal['quality_rank'],refusal['qualification'])==('STOCK0',1,'entry')
    assert refusal['rejection_stage']=='funding' and not refusal['execution_ready']
    assert any(r['symbol']=='STOCK2' and 'Quality gate failed' in r['reason'] for r in result['rejected'])
    assert (result['funding_summary']['qualified'],result['funding_summary']['fundable'],
            result['funding_summary']['unfunded'])==(2,1,1)


def test_watch_funding_refusal_stays_distinct_from_entry_qualification():
    data=screen(1)
    data['equities'][0]['metrics'].update(price=4000,setup='no controlled entry setup')
    data['equities'][0]['flags']=['no controlled entry setup']
    entry=plans.shortlist(data,book(),now=NOW)
    watch=plans.shortlist(data,book(),now=NOW,_stage='watch')
    assert entry['ideas']==[] and entry['funding_rejections']==[]
    assert watch['ideas']==[]
    assert watch['funding_rejections'][0]['qualification']=='watch'
    assert watch['funding_summary']['qualification']=='watch'
    assert data['equities'][0]['flags']==['no controlled entry setup']


def test_effective_budget_stays_150_within_user_1500_ceiling_including_costs():
    settings_before=asdict(SLEEVES)
    result=plans.shortlist(screen(1),book(),now=NOW)
    idea=result['ideas'][0];facts=idea['funding']
    assert result['risk_cap']==facts['risk_budget']==150
    assert idea['estimated_stop_loss']==round(stop_loss_including_costs(
        idea['entry_high'],idea['stop'],idea['qty']),2)
    assert 0<idea['estimated_stop_loss']<=150<1500
    assert facts['minimum_ticket']==result['funding_summary']['minimum_ticket']==1500
    assert 1500<=idea['notional']<=facts['sleeve_notional_remaining']==3000
    assert idea['cash_required']>idea['notional']
    assert idea['cash_required']==facts['cash_required']<=result['cash']
    assert facts['estimated_stop_loss']==idea['estimated_stop_loss']
    assert facts['max_quantity']==idea['qty'] and facts['max_notional']==idea['notional']
    assert asdict(SLEEVES)==settings_before


@pytest.mark.parametrize('entry,stop,cash,code,required_key,available', [
    (2000,1990,2000,'cash','one_share_cash_required',2000),
    (2000,1800,10000,'stop_risk','one_share_stop_loss',150),
    (3100,3090,10000,'sleeve_allocation',None,3000),
])
def test_one_share_failure_identifies_cash_risk_or_sleeve_constraint(entry,stop,cash,code,required_key,available):
    account=book();account.cash=cash
    allocation=RiskManager().size(candidate(entry,stop,entry*1.2),account)
    assert not allocation.ok
    facts=allocation.funding
    assert {b['code'] for b in facts['blockers']}=={code}
    blocker=facts['blockers'][0]
    assert blocker['available']==available
    assert blocker['required']==(facts[required_key] if required_key else entry)
    assert blocker['required']>blocker['available'] and blocker['reason']
    assert facts['risk_budget']==150 and facts['cash_available']==cash
    assert facts['sleeve_notional_remaining']==3000
    assert facts['max_quantity']==0 and facts['max_notional']==0


def test_minimum_ticket_gross_cash_fits_but_entry_fees_make_it_unfundable():
    account=book();account.cash=1500
    allocation=RiskManager().size(candidate(750,740,900),account)
    facts=allocation.funding
    assert not allocation.ok and facts['minimum_ticket_qty']==2
    assert 2*750==facts['minimum_ticket']==account.cash
    assert facts['minimum_ticket_cash_required']==round(1500*(1+SLIPPAGE)+entry_charge(1500*(1+SLIPPAGE)),2)
    assert facts['minimum_ticket_cash_required']>account.cash
    assert facts['minimum_ticket_stop_loss']<facts['risk_budget']
    assert facts['max_quantity']==1 and facts['max_notional']==750
    assert any(b['code']=='minimum_ticket' and b['required']==1500 and b['available']==750 for b in facts['blockers'])


def test_minimum_ticket_gross_stop_fits_but_round_trip_costs_exceed_daily_room():
    allocation=RiskManager().size(candidate(750,710,900),book())
    facts=allocation.funding
    assert not allocation.ok
    assert facts['minimum_ticket_qty']==2
    assert 2*(750-710)<facts['risk_budget']==150
    assert facts['minimum_ticket_stop_loss']==round(stop_loss_including_costs(750,710,2),2)>150
    assert facts['one_share_stop_loss']<150 and facts['max_quantity']==1
    assert any(b['code']=='minimum_ticket' for b in facts['blockers'])


@pytest.mark.parametrize('changes,expected', [
    ({'day_pnl':-50},100),({'open_risk':50},100),
    ({'day_pnl':-50,'open_risk':40},60),({'day_pnl':50,'open_risk':40},110),
])
def test_realised_losses_and_open_risk_reduce_daily_room_and_profits_do_not_replenish(changes,expected):
    account=book()
    for key,value in changes.items():setattr(account,key,value)
    allocation=RiskManager().size(candidate(),account)
    assert allocation.funding['risk_budget']==expected
    assert not allocation.ok
    assert plans.shortlist(screen(1),account,now=NOW)['risk_cap']==expected


@pytest.mark.parametrize('overrides,expected_risk,expected_ticket', [
    ({'risk_per_trade':.005},50,1500),
    ({'daily_loss_limit':.005},50,1500),
    ({'min_ticket':2000},150,2000),
])
def test_personal_shortlist_respects_stricter_custom_settings(overrides,expected_risk,expected_ticket):
    settings=replace(SLEEVES,**overrides)
    assert plans.shortlist(screen(1),book(),now=NOW)['count']==1
    result=plans.shortlist(screen(1),book(),now=NOW,settings=settings)
    assert result['ideas']==[] and result['risk_cap']==expected_risk
    assert result['funding_summary']['minimum_ticket']==expected_ticket
    facts=result['funding_rejections'][0]['funding']
    assert facts['risk_budget']==expected_risk and facts['minimum_ticket']==expected_ticket


@pytest.mark.parametrize('overrides,held,reason', [
    ({'enabled':False},0,'sleeve disabled'),
    ({'max_positions':0},0,'sleeve full (0/0)'),
    ({'max_positions':2},2,'sleeve full (2/2)'),
    ({'risk_share':.1},0,'below Rs 1500 minimum'),
])
def test_nested_sleeve_overrides_survive_shortlisting_without_reconstruction_or_mutation(
        monkeypatch,overrides,held,reason):
    # Reconstructing SleeveSettings would silently restore these permissive
    # environment defaults instead of preserving the supplied nested config.
    monkeypatch.setenv('SLEEVE_QUALITY_MOMENTUM','true')
    monkeypatch.setenv('SHARE_QUALITY_MOMENTUM','0.30')
    settings=copy.deepcopy(SLEEVES)
    settings.capital=50000  # Funding still belongs to this account's Rs 10,000.
    settings.quality_momentum=replace(settings.quality_momentum,**overrides)
    original_config=settings.quality_momentum
    before=asdict(settings)
    account=book()
    if held:
        account.open_positions=held
        account.per_sleeve_positions={'quality_momentum':held}
    result=plans.shortlist(screen(1),account,now=NOW,settings=settings)
    assert result['ideas']==[]
    refusal=result['funding_rejections'][0]
    assert reason in refusal['reason']
    assert result['capital']==10000 and result['risk_cap']==150
    if 'risk_share' in overrides:
        facts=refusal['funding']
        assert facts['sleeve_notional_remaining']==1000
        assert facts['max_quantity']==1 and facts['max_notional']==768
        assert any(b['code']=='minimum_ticket' for b in facts['blockers'])
    assert settings.quality_momentum is original_config
    assert asdict(settings)==before


def automation_context(settings=SLEEVES,quotes=None):
    return SimpleNamespace(observed_at=NOW,paper_epoch=NOW.isoformat(),book=book(),
        equity_screen=screen(1),live=quotes or {},tails={},asof='2026-09-30',
        eligible_symbols={'STOCK0'},regime=RegimeView('ON',True,.7,'ON','Synthetic'),settings=settings)


def test_automation_uses_its_custom_settings_before_publication(tmp_path,monkeypatch):
    path=tmp_path/'automation.db';monkeypatch.setenv('EQUITY_AUTOMATION_DB',str(path))
    ctx=automation_context(replace(SLEEVES,risk_per_trade=.005))
    result=automation.SelectivePaperSleeve().propose(ctx)
    assert result.candidates==[] and result.diagnostics['new_publications']==0
    with sqlite3.connect(path) as con:
        assert con.execute('SELECT COUNT(*) FROM publications').fetchone()[0]==0


@pytest.mark.parametrize('fresh_quote', [False,True])
def test_affordability_without_quote_or_confirmation_never_authorises_automation(tmp_path,monkeypatch,fresh_quote):
    path=tmp_path/'automation.db';monkeypatch.setenv('EQUITY_AUTOMATION_DB',str(path))
    quotes={'STOCK0':dict(price=765,ts=NOW.isoformat(),source='upstox-live')} if fresh_quote else {}
    preview=plans.shortlist(screen(1),book(),quotes,NOW)
    assert preview['count']==1
    idea=preview['ideas'][0]
    assert not idea['actionable'] and not idea['execution_ready']
    assert not preview['funding_summary']['execution_ready']
    assert idea['state']==('IN ZONE · CONFIRMATION NEEDED' if fresh_quote else 'LIVE QUOTE UNAVAILABLE')
    result=automation.SelectivePaperSleeve().propose(automation_context(quotes=quotes))
    assert result.candidates==[] and result.diagnostics['new_publications']==1
    with sqlite3.connect(path) as con:
        assessment=json.loads(con.execute('SELECT payload FROM assessments').fetchone()[0])
        assert not assessment['eligible']
        checks={c['code']:c['passed'] for c in assessment['checks']}
        assert not checks['zone_touch'] and not checks['completed_rebound']


def test_funding_metadata_does_not_change_fingerprint_or_rewrite_frozen_publication(tmp_path):
    idea=plans.shortlist(screen(1),book(),now=NOW)['ideas'][0]
    stripped={k:v for k,v in idea.items() if k not in (
        'funding','cash_required','quality_rank','funding_rank','funding_status','execution_ready')}
    assert tracking.fingerprint(idea)==tracking.fingerprint(stripped)
    path=tmp_path/'research.db'
    ids=tracking.publish(path,2,[stripped],now=NOW)
    pid=next(iter(ids.values()))
    before=tracking.publication(path,2,pid)
    changed=copy.deepcopy(idea);changed['funding']['cash_available']=9000
    assert tracking.publish(path,2,[changed],now=NOW+timedelta(seconds=30))==ids
    after=tracking.publication(path,2,pid)
    assert after==before
    assert after['issued_at']==NOW.isoformat()


def test_funding_metadata_and_current_model_do_not_restate_registered_forward_protocol():
    from tests.test_idea_validation import fixture
    protocol,publication,decisions,quote=fixture()
    original=copy.deepcopy(protocol)
    current=plans.shortlist(screen(1),book(),now=NOW)['ideas'][0]
    extra=(2,publication[1],dict(publication[2],model_version=current['model_version'],
        funding=current['funding'],quality_rank=2,funding_rank=1))
    end=quote(30,99.5)[1]
    before=validation.replay([publication],[quote(30,99.5)],decisions,protocol,end)
    after=validation.replay([publication,extra],[quote(30,99.5)],
        decisions+[(2,publication[1],copy.deepcopy(decisions[0][2]))],protocol,end)
    assert before==after and protocol==original
