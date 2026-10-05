"""Deterministic forward research checks. Never opens positions or orders.

This policy is pre-registered for new v2 publications. Old first-touch scenarios
remain unchanged and are not retrospectively relabelled as confirmed entries.
"""
import hashlib
import json
import math
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import tracking as t
from .store import timestamp

VERSION = 'confirmed-pullback-review-v1'
POLICY = dict(version=VERSION, volume_multiple=1.5, close_location=0.6,
              next_session_only=True, production_approved=False)


def next_session(day):
    day += timedelta(days=1)
    while day.weekday()>=5 or day.isoformat() in t.INDIA_TRADING_HOLIDAYS:
        day += timedelta(days=1)
    return day


def assess(plan, state, bars, quote, context, now, previous=None):
    """Use only completed bars known NOW; entry requires a later quote.

    Retain the first observed confirmation time for unchanged bar evidence.
    A corrected bar gets a new evidence digest/time; no retroactive fill.
    """
    checks=[];previous=previous or {};confirmed_at=None;digest=None
    def check(code, passed, reason):
        checks.append(dict(code=code,passed=bool(passed),reason=reason))
    supported=(plan.get('model_version')=='conditional-pullback-v2'
               and plan.get('confirmation_policy')==POLICY)
    check('model',supported,'Forward research v2 policy required; legacy scenarios are preserved')
    touch=state.get('entry_at')
    alive=state.get('status') not in t.TERMINAL
    check('zone_touch',bool(touch) and alive,'A post-publication observed entry touch and a valid plan are required')
    rebound=False;next_day=None
    try:
        ordered=sorted(bars,key=lambda b:b['session'])
        bar=ordered[-1];prior=ordered[-21:-1]
        day=datetime.fromisoformat(bar['session']).date()
        closed=datetime.combine(day,datetime.min.time(),t.IST).replace(hour=15,minute=30)
        known=timestamp(bar['known_at'])
        numbers=[float(bar[k]) for k in ('open','high','low','close','volume')]
        volumes=[float(b['volume']) for b in prior]
        valid=(len(prior)==20 and len({b['session'] for b in ordered})==len(ordered)
               and all(math.isfinite(v) and v>0 for v in numbers+volumes)
               and closed<=known<=now and day.weekday()<5
               and day.isoformat() not in t.INDIA_TRADING_HOLIDAYS
               and all(b['session']<bar['session'] for b in prior)
               and bar['low']<=min(bar['open'],bar['close'])<=max(bar['open'],bar['close'])<=bar['high'])
        if touch:valid=valid and timestamp(touch).astimezone(t.IST).date()<=day
        rebound=(valid and bar['close']>bar['open'] and bar['close']>prior[-1]['close']
                 and bar['high']>bar['low']
                 and (bar['close']-bar['low'])/(bar['high']-bar['low'])>=POLICY['close_location']
                 and bar['volume']>=sum(volumes)/20*POLICY['volume_multiple'])
        digest=hashlib.sha256(json.dumps([bar['session'],numbers,
            [(b['session'],b['close'],b['volume']) for b in prior]],sort_keys=True).encode()).hexdigest()
        if rebound and supported and touch and alive:
            confirmed_at=(previous.get('confirmation_at') if previous.get('bar_digest')==digest else None) or now.isoformat()
        next_day=next_session(day)
    except (KeyError,TypeError,ValueError,IndexError):
        pass
    check('completed_rebound',rebound,'Completed positive rebound; top 40% of range; volume >=1.5x prior 20-session mean')
    check('next_session',next_day==now.astimezone(t.IST).date(),'Entry is restricted to the next NSE session after confirmation')
    fresh=False;in_zone=False;later=False
    try:
        at=timestamp(quote['ts']);price=float(quote['price'])
        fresh=(quote.get('source')=='upstox-live' and math.isfinite(price) and price>0
               and 0<=(now-at).total_seconds()<=120 and t.market_session_for_region('IN',at)['is_open'])
        in_zone=plan['entry_low']<=price<=plan['entry_high']
        later=bool(confirmed_at) and at>timestamp(confirmed_at)
    except (KeyError,TypeError,ValueError):pass
    check('fresh_quote',fresh,'Fresh regular-session quote with identified source required')
    check('entry_zone',in_zone,'Current price must remain inside the original range')
    check('later_fill',later,'A quote strictly after confirmation availability is required; no first-touch backfill')
    try:
        economic=all(t.net(plan,plan['entry_high'],plan[k])>0 for k in ('t1','t2','t3'))
    except (KeyError,TypeError,ValueError):economic=False
    check('targets',economic,'All displayed targets must be profitable at the frozen quantity and costs')
    check('regime',context.get('regime_fresh') and context.get('regime') in ('ON','NEUTRAL'),
          'A fresh supportive production regime is required')
    try:
        news_fresh=0<=(now-timestamp(context['news_checked_at'])).total_seconds()<=7200
    except (KeyError,TypeError,ValueError):news_fresh=False
    check('evidence',news_fresh and context.get('evidence_ok'),
          'Fresh official news/earnings checks and no current evidence flags required')
    check('risk',context.get('risk_ok'),context.get('risk_reason') or 'Existing account and portfolio risk limits must pass')
    eligible=all(c['passed'] for c in checks)
    baseline_codes={'model','zone_touch','fresh_quote','entry_zone','targets','regime','evidence','risk'}
    baseline_eligible=all(c['passed'] for c in checks if c['code'] in baseline_codes)
    return dict(model_version=VERSION,checked_at=now.isoformat(),confirmation_at=confirmed_at,
                bar_digest=digest,checks=checks,eligible=eligible,baseline_eligible=baseline_eligible,production_approved=False,
                status='ENTRY_ELIGIBLE_SHADOW' if eligible else 'RESEARCH_WAITING',
                reason=next((c['reason'] for c in checks if not c['passed']),'All research checks passed; execution remains unpromoted'))


def refresh(main, path, paper, screen, regime, now=None):
    """Read live evidence and account risk; write ONLY research assessments."""
    from .plans import account_state
    from ..sleeves.base import Candidate
    from ..sleeves.risk import RiskManager
    now=now or datetime.now(timezone.utc)
    con=t.connect(path)
    try:
        rows=con.execute('SELECT p.id,p.user_id,p.payload,s.payload,a.payload '
            'FROM publications p JOIN states s ON s.publication_id=p.id '
            'LEFT JOIN assessments a ON a.publication_id=p.id WHERE json_extract(p.payload,\'$.model_version\')=?',
            ('conditional-pullback-v2',)).fetchall()
        if not rows:return 0
        market=sqlite3.connect(f'file:{Path(main).resolve()}?mode=ro',uri=True,timeout=5)
        account=sqlite3.connect(f'file:{Path(paper).resolve()}?mode=ro',uri=True,timeout=5)
        account.row_factory=sqlite3.Row
        try:
            quotes={s:dict(price=p,ts=at,source=src) for s,p,at,src in market.execute(
                "SELECT symbol,price,ts,source FROM latest_quotes WHERE source='upstox-live'")}
            evidence={r['symbol']:r for r in screen.get('equities',[])}
            books={};bars_by_symbol={};changed=0
            for pid,uid,encoded,stored,prev in rows:
                plan=json.loads(encoded);state=json.loads(stored)
                symbol=plan['symbol']
                if symbol not in bars_by_symbol:
                    local=now.astimezone(t.IST)
                    # A current-day partial daily candle must not hide the
                    # previous completed confirmation throughout the session.
                    cutoff=local.date().isoformat()
                    before_close=(local.hour,local.minute)<(15,30)
                    daily=market.execute("SELECT substr(ts,1,10),open,high,low,close,volume FROM candles "
                        "WHERE symbol=? AND source='upstox-live:day' AND substr(ts,1,10)"+
                        ('<' if before_close else '<=')+"? ORDER BY ts DESC LIMIT 21",(symbol,cutoff)).fetchall()
                    bars_by_symbol[symbol]=[dict(zip(('session','open','high','low','close','volume'),b),known_at=now.isoformat()) for b in daily]
                context=dict(regime=regime.get('regime'),
                    regime_fresh=(regime.get('asof')==screen.get('price_asof')
                        and bars_by_symbol[symbol] and bars_by_symbol[symbol][0]['session']==screen.get('price_asof')
                        and regime.get('cycle_date')==now.astimezone(t.IST).date().isoformat()
                        and not regime.get('execution_halted')),
                    evidence_ok=False,risk_ok=False)
                current=evidence.get(symbol,{})
                context.update(evidence_ok=bool(current) and not current.get('flags') and not screen.get('stale') and not screen.get('price_stale'),
                    news_checked_at=(current.get('news') or {}).get('checked_at'))
                if uid not in books:books[uid]=account_state(account,uid,quotes,now)
                book,error=books[uid];q=quotes.get(symbol,{})
                if not error and q:
                    candidate=Candidate(symbol,'quality_momentum',plan.get('score',0)/100,q['price'],plan['stop'],target=plan['t3'],max_hold_days=40)
                    allocation=RiskManager().size(candidate,book)
                    context.update(risk_ok=allocation.ok and allocation.shares>=plan['qty'],risk_reason=allocation.reason)
                else:context['risk_reason']=error or 'Fresh account quote unavailable'
                previous=json.loads(prev) if prev else {}
                result=assess(plan,state,bars_by_symbol[symbol],q,context,now,previous)
                result['quote']=q
                # Immutable decision evidence for subsequent forward replay.
                # Assessment events never represent an order or a fill.
                result['context']=context
                if result['confirmation_at'] and result['confirmation_at']!=previous.get('confirmation_at'):
                    con.execute('INSERT OR IGNORE INTO assessment_events VALUES(?,?,?,?)',
                        (pid,'CONFIRMATION_RECORDED',result['confirmation_at'],t._json(result)))
                if result['eligible'] and not previous.get('eligible'):
                    con.execute('INSERT OR IGNORE INTO assessment_events VALUES(?,?,?,?)',
                        (pid,'ENTRY_ELIGIBLE_SHADOW',now.isoformat(),t._json(result)))
                if result['baseline_eligible'] and not previous.get('baseline_eligible'):
                    con.execute('INSERT OR IGNORE INTO assessment_events VALUES(?,?,?,?)',
                        (pid,'ZONE_ELIGIBLE_BASELINE',now.isoformat(),t._json(result)))
                con.execute('INSERT OR REPLACE INTO assessments VALUES(?,?)',(pid,t._json(result)));changed+=1
            con.commit()
            return changed
        finally:market.close();account.close()
    finally:con.close()
