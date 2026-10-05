"""No look-ahead, overlapping capital or cash drift in forward comparisons."""
import hashlib
import json
import sqlite3
from datetime import timedelta

import pytest

from app.screening import validation as v, tracking as t
from tests.test_idea_tracking import plan, NOW


def fixture():
    p=v.protocol(NOW-timedelta(seconds=1))
    stock=plan(qty=20);stock.update(stop=96.,model_version='conditional-pullback-v2',cost_profile=t.cost_profile())
    publication=(1,NOW.isoformat(),stock)
    decisions=[(1,NOW.isoformat(),dict(context={'regime':'ON'}))]
    def q(seconds,price,symbol='TEST'):
        at=(NOW+timedelta(seconds=seconds)).isoformat()
        return symbol,at,price,at
    return p,publication,decisions,q


def test_cash_legs_exactly_match_frozen_round_trip():
    p,publication,d,q=fixture();stock=publication[2]
    debit,proceeds=v.legs(stock,99.5,94)
    assert proceeds-debit==pytest.approx(t.net(stock,99.5,94))


def test_decision_quote_is_never_retroactively_used_as_fill():
    p,pub,d,q=fixture()
    same=v.replay([pub],[q(0,99.5)],d,p,(NOW+timedelta(seconds=1)).isoformat())
    assert same['fills']==[] and same['equity']==10000
    next_tick=v.replay([pub],[q(0,99.5),q(30,99.5)],d,p,(NOW+timedelta(seconds=30)).isoformat())
    assert len(next_tick['fills'])==1 and next_tick['fills'][0]['entry_at']==q(30,99.5)[1]
    assert next_tick['equity']<10000 and next_tick['cash']>=0


def test_observed_gap_stop_uses_actual_quote_and_reconciles():
    p,pub,d,q=fixture()
    result=v.replay([pub],[q(30,99.5),q(240,93)],d,p,q(240,93)[1])
    assert result['trades'][0]['exit']==93 and result['trades'][0]['reason']=='STOPPED'
    assert result['cash']==pytest.approx(10000+result['realised'],abs=.01)
    assert result['equity']==result['cash'] and result['open_positions']==0
    assert result['coverage_gaps'] and not result['profitability_established']


def test_overlapping_versions_and_other_stocks_share_existing_risk_and_cash():
    p,pub,d,q=fixture()
    other=dict(pub[2],symbol='OTHER')
    pubs=[pub,(2,pub[1],dict(pub[2])),(3,pub[1],other)]
    decisions=d+[(pid,NOW.isoformat(),{}) for pid in (2,3)]
    result=v.replay(pubs,[q(30,99.5),q(30,99.5,'OTHER')],decisions,p,q(30,99.5)[1])
    assert len(result['fills'])==1 and result['unique_stocks']==2
    assert any('already held' in row['reason'] for row in result['rejected'])
    assert any('sleeve full' in row['reason'] for row in result['rejected'])
    assert result['cash']>=0 and result['open_positions']==1


def test_stale_decision_and_unreachable_zone_cannot_fill():
    p,pub,d,q=fixture()
    for seconds,price in [(121,99.5),(30,101)]:
        result=v.replay([pub],[q(seconds,price)],d,p,q(seconds,price)[1])
        assert result['fills']==[] and result['rejected']


def test_preregistered_cohort_excludes_legacy_or_previously_seen_plans():
    p,pub,d,q=fixture()
    for stale in [(1,(NOW-timedelta(days=1)).isoformat(),pub[2]),(1,pub[1],dict(pub[2],model_version='conditional-pullback-v1'))]:
        result=v.replay([stale],[q(30,99.5)],d,p,q(30,99.5)[1])
        assert result['publications']==0 and not result['fills']


def test_validation_report_is_read_only_and_isolates_user(tmp_path):
    path=tmp_path/'tracker.db';p,pub,d,q=fixture()
    t.publish(path,2,[pub[2]],issued_at=pub[1],now=NOW)
    t.publish(path,3,[dict(pub[2],symbol='PRIVATE')],issued_at=pub[1],now=NOW)
    con=sqlite3.connect(path)
    con.execute('INSERT INTO assessment_events VALUES(?,?,?,?)',(1,'ENTRY_ELIGIBLE_SHADOW',pub[1],json.dumps(d[0][2])))
    con.execute('INSERT INTO samples VALUES(?,?,?,?,?)',('TEST',q(30,99.5)[1],'upstox-live',99.5,q(30,99.5)[1]))
    con.commit();con.close()
    before=hashlib.sha256(path.read_bytes()).digest()
    result=v.report(path,2,p,end=q(30,99.5)[1])
    assert result['confirmed']['unique_stocks']==1 and result['confirmed']['open_positions']==1
    assert hashlib.sha256(path.read_bytes()).digest()==before
    assert result['first_touch_baseline']['fills']==[]


def test_configuration_drift_is_explicitly_rejected():
    p,pub,d,q=fixture();p['cost_profile']['flat']+=1
    with pytest.raises(ValueError,match='Cost configuration changed'):
        v.replay([pub],[],d,p,NOW.isoformat())
