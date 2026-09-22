"""Offline diagnostic replay of production proposals and unified allocation.

No database writes or broker calls. Daily bars cannot reproduce ordered live
ticks: stops precede targets on ambiguous bars; trails advance NEXT session.
Universe selection uses only history available at each decision. Delisting,
corporate actions, historical membership and delivery publication timestamps
are not verified, so this is diagnostic evidence, never a promotion certificate.
"""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import replace
import hashlib
import json
import logging
from pathlib import Path
import sqlite3
import sys
from unittest.mock import patch

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import costs, v2_engine
from app.sleeves.engine import SleeveEngine
from app.sleeves.risk import BookState


def daily_exit(position, bar, age):
    """Prior-session stop only; a gap through it executes at the worse open."""
    c = position['candidate']
    stop = position['stop']
    if bar.open <= stop:
        return float(bar.open), 'gap_stop'
    if bar.low <= stop:
        return stop, 'stop'
    if c.target and bar.high >= c.target:
        return c.target, 'target'
    if c.max_hold_days and age >= c.max_hold_days:
        return float(bar.close), 'time'
    return None, None


def load(path):
    with sqlite3.connect(f'file:{Path(path).resolve()}?mode=ro', uri=True) as con:
        raw = pd.read_sql_query('SELECT * FROM prices', con)
        delivery = pd.read_sql_query('SELECT * FROM delivery', con)
    raw['date'] = pd.to_datetime(raw.date)
    raw = raw.sort_values(['symbol', 'date']).drop_duplicates(['symbol', 'date'])
    panels = {}
    for sym, frame in raw.groupby('symbol'):
        g = frame.set_index('date')
        g.attrs['symbol'] = sym
        panels[sym] = g
    closes = raw.pivot(index='date', columns='symbol', values='close')
    # Missing sessions are not filled; suspended symbols supply no zero return.
    returns = closes.pct_change(fill_method=None).median(axis=1)
    market = pd.DataFrame({'mkt_ret1':returns, 'mkt_cum':(1+returns.fillna(0)).cumprod()})
    delivery['date'] = pd.to_datetime(delivery.date)
    deliveries = {s:g.set_index('date').sort_index() for s,g in delivery.groupby('symbol')}
    return panels, market, deliveries


def replay(panels, market, deliveries, start, end, slip_bps=5):
    engine = SleeveEngine()
    capital = cash = peak = previous_equity = 10000.0
    positions, trades, curve, pending = {}, [], [], []
    pending_regime_exit = False
    counts, regimes, rejections = Counter(), Counter(), Counter()
    dates = market.index[(market.index>=start)&(market.index<=end)]
    # Causal rolling features may be cached; no future row is returned to a sleeve.
    features = {s:v2_engine.compute_features(g, market) for s,g in panels.items() if len(g)>=90}
    def cached(g, _market):
        return features[g.attrs['symbol']].loc[g.index]
    slip = slip_bps/10000
    for n, day in enumerate(dates):
        prior_dates = market.index[market.index<day]
        if not len(prior_dates):
            continue
        asof = prior_dates[-1]
        bars = {s:g.loc[day] for s,g in panels.items() if day in g.index}
        def valuation():
            return cash + sum(p['qty']*p['mark'] for p in positions.values())
        def book():
            value = valuation()
            per_count, per_value = Counter(), Counter()
            risk = 0.0
            for p in positions.values():
                per_count[p['candidate'].sleeve]+=1
                per_value[p['candidate'].sleeve]+=p['qty']*p['mark']
                risk+=max(0,p['mark']-p['stop'])*p['qty']
            return BookState(capital,cash,value-cash,len(positions),dict(per_count),
                             value,max(peak,value),value-previous_equity,dict(per_value),risk)
        if pending_regime_exit:
            for sym,p in list(positions.items()):
                if p['candidate'].sleeve != 'index_directional' or sym not in bars:
                    continue
                price=float(bars[sym].open)*(1-slip)
                c=p['candidate'];basis=c.entry*p['qty'];proceeds=price*p['qty']
                fee=costs.round_trip(basis,proceeds,'D');pnl=proceeds-basis-fee
                cash+=proceeds-fee
                trades.append(dict(symbol=sym,sleeve=c.sleeve,regime=p['regime'],
                    entry_date=p['date'],exit_date=str(day.date()),qty=p['qty'],
                    entry=c.entry,exit=price,gross=proceeds-basis,fees=fee,pnl=pnl,
                    r=pnl/p['initial_risk'],reason='regime_off'))
                del positions[sym]
            pending_regime_exit=False
        # Overnight gaps are known before allocating at this open.
        for s,p in positions.items():
            if s in bars:
                p['mark']=float(bars[s].open)
        # Signal generated yesterday, execution today. Re-size with today's
        # gap and adverse fill; a pre-existing absolute stop is not widened.
        for candidate, regime in pending:
            if candidate.symbol not in bars or candidate.symbol in positions:
                continue
            c = replace(candidate,entry=float(bars[candidate.symbol].open)*(1+slip))
            sane, why = c.is_sane()
            if not sane:
                rejections[why]+=1
                continue
            allocation = engine.risk.size(c,book())
            if not allocation.ok:
                rejections[allocation.reason.split('(')[0]]+=1
                continue
            cash-=allocation.notional
            positions[c.symbol] = dict(candidate=c,qty=allocation.shares,mark=c.entry,
                stop=c.stop,peak=c.entry,day=n,date=str(day.date()),regime=regime,
                initial_risk=allocation.risk_amount)
        for sym,p in list(positions.items()):
            if sym not in bars:
                continue
            bar=bars[sym]
            price,reason=daily_exit(p,bar,n-p['day'])
            if price is not None:
                price*=1-slip
                c=p['candidate']; basis=c.entry*p['qty']; proceeds=price*p['qty']
                fee=costs.round_trip(basis,proceeds,'D')
                pnl=proceeds-basis-fee
                cash+=proceeds-fee
                trades.append(dict(symbol=sym,sleeve=c.sleeve,regime=p['regime'],
                    entry_date=p['date'],exit_date=str(day.date()),qty=p['qty'],
                    entry=c.entry,exit=price,gross=proceeds-basis,fees=fee,pnl=pnl,
                    r=pnl/p['initial_risk'],reason=reason))
                del positions[sym]
            else:
                p['mark']=float(bar.close)
                p['peak']=max(p['peak'],float(bar.high))
                if p['candidate'].trail_pct and p['peak']>p['candidate'].entry:
                    p['stop']=max(p['stop'],p['peak']*(1-p['candidate'].trail_pct))
        equity=valuation(); peak=max(peak,equity)
        curve.append(dict(date=str(day.date()),equity=equity,cash=cash,
                          positions=len(positions),drawdown_pct=(equity/peak-1)*100))
        # Membership uses a rolling liquidity screen, never today's Nifty list
        # backdated. The distinction from production is disclosed in the report.
        ranked=[]
        for sym,g in panels.items():
            gi=g.loc[:day]
            if len(gi)<120 or day not in gi.index:
                continue
            ranked.append((float((gi.close*gi.volume).tail(120).median()),sym))
        tails={s:panels[s].loc[:day] for _,s in sorted(ranked,reverse=True)[:700]}
        def delivery(sym):
            g=deliveries.get(sym)
            if g is None or day not in g.index:
                return None,None
            series=g.loc[:day,'delivery_pct']
            return float(series.iloc[-1]),float(series.tail(20).mean())
        with patch.object(v2_engine,'compute_features',side_effect=cached):
            next_day = dates[n+1] if n+1 < len(dates) else day
            result=engine.run(tails,market.loc[:day],day,{},book(),trade_date=next_day,
                eligible_symbols=set(tails),require_reference_data=True,
                quality_scores={},delivery_pct=delivery,routable_instruments=('EQ',))
        regimes[result.regime.state]+=1
        pending=[]
        for decision in result.decisions:
            counts[decision.sleeve]+=len(decision.candidates)
            rejections.update(why for _,why in decision.rejected)
        for allocation in result.allocations:
            pending.append((allocation.candidate,result.regime.state))
        if result.regime.state == 'OFF' and (day.year,day.month)!=(next_day.year,next_day.month):
            pending_regime_exit=True
        previous_equity=equity
        if n%25==0:
            print(f'{day.date()} equity={equity:.2f} trades={len(trades)}',flush=True)
    def summary(rows):
        return dict(trades=len(rows),gross=sum(t['gross'] for t in rows),
            net=sum(t['pnl'] for t in rows),fees=sum(t['fees'] for t in rows),
            win_rate=sum(t['pnl']>0 for t in rows)/len(rows) if rows else None,
            average_r=sum(t['r'] for t in rows)/len(rows) if rows else None)
    return dict(summary=summary(trades),ending_equity=curve[-1]['equity'] if curve else capital,
        open_positions=len(positions),max_drawdown_pct=min((x['drawdown_pct'] for x in curve),default=0),
        regimes=dict(regimes),proposals=dict(counts),rejections=rejections.most_common(25),
        by_sleeve={s:summary([t for t in trades if t['sleeve']==s]) for s in engine.sleeves},
        by_regime={r:summary([t for t in trades if t['regime']==r]) for r in ('ON','NEUTRAL','OFF')},
        trades=trades,curve=curve)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db',required=True);parser.add_argument('--output',required=True)
    args=parser.parse_args();out=Path(args.output);out.mkdir(parents=True,exist_ok=True)
    spec=dict(capital=10000,development=['2025-10-01','2026-03-31'],
        retrospective_holdout=['2026-04-01','2026-09-16'],slippage_bps=5,
        limitations=['18-month unadjusted sample; survivorship and corporate actions unverified',
            'Past-only top-700 liquidity, not verified historical Nifty membership',
            'Daily OHLC exit approximation, not live-tick execution parity',
            'Missing historical fundamentals/options: corresponding sleeves cannot qualify',
            'Delivery assumed available by next open; publication timestamps unverified',
            'Fees charged at exit; open positions marked gross; no forced terminal liquidation',
            'Synthetic median-market series across observed universe; no future membership filtering'],
        input_sha256=hashlib.file_digest(open(args.db,'rb'),'sha256').hexdigest())
    (out/'frozen-spec.json').write_text(json.dumps(spec,indent=2))
    logging.disable(logging.CRITICAL)
    panels,market,delivery=load(args.db)
    results={}
    for label in ('development','retrospective_holdout'):
        lo,hi=map(pd.Timestamp,spec[label])
        results[label]=replay(panels,market,delivery,lo,hi,spec['slippage_bps'])
        (out/f'{label}.json').write_text(json.dumps(results[label],indent=2))
    print(json.dumps({k:{x:v[x] for x in ('summary','ending_equity','max_drawdown_pct','open_positions','proposals')} for k,v in results.items()},indent=2))


if __name__=='__main__':
    main()
