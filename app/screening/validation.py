"""Read-only forward portfolio comparison; never the actual paper book.

The gated zone-touch baseline and confirmed-entry hypothesis both fill on
a SUBSEQUENT fresh sample. Stops/T3/time exits use observed prices, not invented
threshold fills. T1/T2 are markers only. This is a sampled shadow experiment.
"""
import json
import math
import sqlite3
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import tracking as t
from .confirmation import VERSION
from ..sleeves.base import Candidate
from ..sleeves.config import SLEEVES, SleeveSettings, SleeveConfig
from ..sleeves.risk import BookState, RiskManager

PROTOCOL = 'idea-forward-portfolio-v1'


def protocol(start=None):
    """Register BEFORE the cohort begins. Previously inspected data is excluded."""
    return dict(version=PROTOCOL,start_at=(start or datetime.now(timezone.utc)).isoformat(),
        model_version=VERSION,capital=10000.,initial_positions=[],settings=asdict(SLEEVES),
        cost_profile=t.cost_profile(),entry='next fresh sample after decision, within 120 seconds and original zone',
        exit='full position at first observed stop/T3 crossing or 40-session time stop; T1/T2 markers only',
        comparison='same future cohort and allocator; zone-only versus rebound-confirmed entry, both with market/news/risk checks',
        promotion='independent positive after-cost expectancy and portfolio return with uncertainty assessed; accounting and risk must pass',
        limitation='No tick or order-book fills; missing observations can hide crossings. No production promotion.')


def legs(plan, entry, price):
    """Split the frozen round-trip schedule to reconcile cash on each leg."""
    c=plan['cost_profile'];q=plan['qty']
    buy=entry*q*(1+c['slippage']);sell=price*q*(1-c['slippage'])
    eb=buy*c['exchange'];ib=buy*c['ipft'];sb=buy*c['sebi']
    es=sell*c['exchange'];is_=sell*c['ipft'];ss=sell*c['sebi']
    entry_fee=c['flat']+buy*(c['stt']+c['stamp'])+eb+ib+sb+(c['flat']+eb+ib)*c['gst']
    exit_fee=c['flat']+sell*c['stt']+es+is_+ss+c['dp']+(c['flat']+es+is_+c['dp'])*c['gst']
    return buy+entry_fee,sell-exit_fee


def _settings(p):
    data=dict(p['settings'])
    for name in ('mean_reversion','quality_momentum','early_momentum','index_directional','options_overlay'):
        data.pop(name,None)
    settings=SleeveSettings(**data)
    for name in ('mean_reversion','quality_momentum','early_momentum','index_directional','options_overlay'):
        setattr(settings,name,SleeveConfig(**p['settings'][name]))
    return settings


def replay(publications, samples, decisions, p, end):
    """Chronological replay, one funded position per stock across its versions."""
    start=t.timestamp(p['start_at']);end=t.timestamp(end)
    if end<start:raise ValueError('Experiment end precedes its registration')
    if p['version']!=PROTOCOL or p['capital']!=10000 or p['initial_positions']:
        raise ValueError('This preregistered experiment requires a clean Rs 10,000 initial shadow book')
    if p['cost_profile']!=t.cost_profile():
        raise ValueError('Cost configuration changed; this protocol requires its registered risk/cost implementation')
    pubs={pid:plan for pid,issued,plan in publications
          if start<=t.timestamp(issued)<=end and plan.get('model_version')=='conditional-pullback-v2'}
    settings=_settings(p);risk=RiskManager(settings)
    cash=peak=equity=10000.;positions={};pending={};consumed=set();marks={}
    trades=[];rejected=[];fills=[];max_dd=0.;day=None;day_open=10000.;gaps=[]
    timeline=[]
    for pid,at,data in decisions:
        if pid in pubs and start<=t.timestamp(at)<=end:
            timeline.append((t.timestamp(at),1,'decision',(pid,data)))
    for symbol,at,price,captured in samples:
        observed=t.timestamp(captured);quote_at=t.timestamp(at)
        if start<=observed<=end and 0<=(observed-quote_at).total_seconds()<=120 and t.market_session_for_region('IN',quote_at)['is_open']:
            timeline.append((observed,0,'quote',(symbol,quote_at,float(price))))
    def valuation():
        return cash+sum(legs(pos['plan'],pos['entry'],marks[symbol][0])[1] for symbol,pos in positions.items())
    for known,_,kind,data in sorted(timeline,key=lambda v:(v[0],v[1])):
        current_day=known.astimezone(t.IST).date()
        if day!=current_day:day=current_day;day_open=valuation()
        if kind=='decision':
            pid,evidence=data
            if pid not in consumed and pubs[pid]['symbol'] not in positions:
                pending[pid]=(known,evidence)
            continue
        symbol,at,price=data
        if not math.isfinite(price) or price<=0:continue
        old=marks.get(symbol)
        if old and at<=old[1]:continue
        if old and at.astimezone(t.IST).date()==old[1].astimezone(t.IST).date() and (at-old[1]).total_seconds()>120:
            gaps.append(dict(symbol=symbol,start_at=old[1].isoformat(),end_at=at.isoformat()))
        marks[symbol]=(price,at)
        pos=positions.get(symbol)
        if pos:
            plan=pos['plan']
            reason=('STOPPED' if price<=plan['stop'] else 'TARGET_3' if price>=plan['t3'] else
                    'TIME_EXIT' if t.sessions_between(pos['entry_at'],at.isoformat())>=40 else None)
            if reason:
                proceeds=legs(plan,pos['entry'],price)[1];cash+=proceeds
                net=proceeds-pos['debit']
                trades.append(dict(symbol=symbol,publication_id=pos['pid'],qty=plan['qty'],
                    entry_at=pos['entry_at'],exit_at=at.isoformat(),entry=pos['entry'],exit=price,
                    pnl=round(net,2),r=round(net/pos['risk'],4),reason=reason,regime=pos['regime']))
                del positions[symbol]
        equity=valuation();peak=max(peak,equity);max_dd=max(max_dd,1-equity/peak)
        for pid,(decision,evidence) in list(pending.items()):
            plan=pubs[pid]
            if known-decision>timedelta(seconds=120):
                rejected.append(dict(publication_id=pid,reason='No subsequent fresh fill within decision validity'));del pending[pid];continue
            if plan['symbol']!=symbol or at<=decision:continue
            del pending[pid];consumed.add(pid)
            reason=None
            if symbol in positions:reason='Stock already held; overlapping versions are not independent'
            elif not plan['entry_low']<=price<=plan['entry_high']:reason='Next observed price outside original entry zone'
            if reason:rejected.append(dict(publication_id=pid,reason=reason));continue
            deployed=sum(pos['entry']*pos['plan']['qty'] for pos in positions.values())
            open_risk=sum(pos['risk'] for pos in positions.values())
            book=BookState(10000.,cash,deployed,len(positions),{'quality_momentum':len(positions)},
                equity,peak,equity-day_open,{'quality_momentum':deployed},open_risk)
            cand=Candidate(symbol,'quality_momentum',plan.get('score',0)/100,price,plan['stop'],target=plan['t3'],max_hold_days=40)
            allocation=risk.size(cand,book)
            if not allocation.ok:rejected.append(dict(publication_id=pid,reason=allocation.reason));continue
            frozen=dict(plan,qty=min(allocation.shares,plan['qty']))
            debit,_=legs(frozen,price,price)
            loss=-t.net(frozen,price,frozen['stop'])
            if debit>cash or loss<=0 or any(t.net(frozen,price,frozen[k])<=0 for k in ('t1','t2','t3')):
                rejected.append(dict(publication_id=pid,reason='Cash or frozen target economics failed'));continue
            cash-=debit
            positions[symbol]=dict(pid=pid,plan=frozen,entry=price,entry_at=at.isoformat(),debit=debit,risk=loss,
                regime=(evidence.get('context') or {}).get('regime','UNKNOWN'))
            fills.append(dict(publication_id=pid,symbol=symbol,qty=frozen['qty'],entry_at=at.isoformat(),price=price))
            equity=valuation();peak=max(peak,equity);max_dd=max(max_dd,1-equity/peak)
            assert cash>=-1e-8 and len(positions)<=settings.max_positions_total
    return dict(capital=10000.,cash=round(cash,2),equity=round(valuation(),2),realised=round(sum(x['pnl'] for x in trades),2),
        open_positions=len(positions),trades=trades,fills=fills,rejected=rejected,waiting_decisions=len(pending),
        publications=len(pubs),unique_stocks=len({v['symbol'] for v in pubs.values()}),
        sampled_drawdown_pct=round(max_dd*100,4),coverage_gaps=gaps,
        win_pct=round(sum(x['pnl']>0 for x in trades)/len(trades)*100,2) if trades else None,
        average_r=round(sum(x['r'] for x in trades)/len(trades),4) if trades else None,
        profitability_established=False,note='Hypothetical funded replay; overlapping versions share one stock position. Sampled gaps remain uncertain.')


def report(path,user_id,p,end=None):
    """All input reads are mode=ro; protocol and original publications stay intact."""
    end=end or datetime.now(timezone.utc).isoformat()
    con=sqlite3.connect(f'file:{Path(path).resolve()}?mode=ro',uri=True,timeout=5)
    try:
        publications=[(pid,at,json.loads(payload)) for pid,at,payload in con.execute(
            'SELECT id,issued_at,payload FROM publications WHERE user_id=? ORDER BY id',(user_id,))]
        wanted={v[2]['symbol'] for v in publications}
        samples=[r for r in con.execute("SELECT symbol,quote_at,price,captured_at FROM samples WHERE source='upstox-live' AND captured_at>=? AND captured_at<=? ORDER BY captured_at",(p['start_at'],end)) if r[0] in wanted]
        try:
            decisions=[(pid,at,json.loads(payload)) for pid,at,payload in con.execute(
                "SELECT a.publication_id,a.observed_at,a.payload FROM assessment_events a JOIN publications p ON p.id=a.publication_id WHERE p.user_id=? AND a.kind='ENTRY_ELIGIBLE_SHADOW' ORDER BY a.observed_at",(user_id,))]
        except sqlite3.OperationalError:decisions=[]
        try:
            baseline=[(pid,at,json.loads(payload)) for pid,at,payload in con.execute(
                "SELECT e.publication_id,e.observed_at,e.payload FROM assessment_events e JOIN publications p ON p.id=e.publication_id WHERE p.user_id=? AND e.kind='ZONE_ELIGIBLE_BASELINE' ORDER BY e.observed_at",(user_id,))]
        except sqlite3.OperationalError:baseline=[]
    finally:con.close()
    return dict(protocol=p,start_at=p['start_at'],end_at=end,
        confirmed=replay(publications,samples,decisions,p,end),
        first_touch_baseline=replay(publications,samples,baseline,p,end),
        verdict='Unvalidated. Compare independent completed forward outcomes; no promotion or orders.')
