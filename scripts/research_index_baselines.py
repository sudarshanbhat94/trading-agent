"""Frozen, independent research of two public index ETF benchmarks.

No account, trade ledger, broker credentials or production database is read.
No orders are placed. This compares hypotheses; it does not promote a strategy.
Signals use completed closes, fills use the next session open plus slippage.
The existing equity fee model is a conservative proxy, not verified ETF charges.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import httpx
import pandas as pd
from app.costs import round_trip
from scripts.audit_market_prices import bars_from_response

SYMBOLS=('NIFTYBEES.NS','BANKBEES.NS')
SPEC={'version':2,'symbols':SYMBOLS,'warmup_start':'2018-01-01',
      'development':['2020-01-01','2023-12-31'],'holdout_start':'2024-01-01',
      'rules':['buy_hold','monthly_trend200','monthly_momentum252_trend200'],
      'capital':10000,'max_deployed':.35,'slippage_bps':[5,20],
      'cost_model':'existing equity delivery fee proxy','tuning':False}


def desired(rule, history):
    if len(history)<253:
        return False
    c=history['close']
    if rule=='buy_hold':
        return True
    trend=float(c.iloc[-1])>float(c.tail(200).mean())
    return bool(trend and (rule=='monthly_trend200' or c.iloc[-1]>c.iloc[-253]))


def replay(frame, rule, start, end, slip_bps=5):
    cash=10000.;qty=0;entry=0.;trades=[];curve=[]
    slippage=slip_bps/10000
    for i,(date,row) in enumerate(frame.iterrows()):
        if date<start or date>end or i<253:
            continue
        history=frame.iloc[:i]  # NEVER includes today's high/low/close.
        previous=frame.index[i-1]
        # Exactly one decision per month, at the first session open, from the
        # completed prior session. The earlier harness evaluated again on day
        # two because it tracked the previous bar's month instead of today's.
        pending=desired(rule,history) if date[:7]!=previous[:7] else None
        if pending is False and qty:
            px=float(row.open)*(1-slippage)
            fee=round_trip(qty*entry,qty*px)
            pnl=qty*(px-entry)-fee
            cash+=qty*px-fee
            trades.append({'entry':entry,'exit':px,'qty':qty,'pnl':pnl,'date':date})
            qty=0
        elif pending is True and not qty:
            px=float(row.open)*(1+slippage)
            # Reserve a full flat round trip; open equity includes exit-cost estimate.
            qty=max(0,int(min(cash-100,cash*SPEC['max_deployed'])//px));entry=px
            cash-=qty*entry
        pending=None
        value=qty*float(row.close)
        curve.append((date,cash+value-(round_trip(qty*entry,value) if qty else 0)))
    if qty and curve:
        px=float(frame.loc[curve[-1][0],'close'])*(1-slippage)
        fee=round_trip(qty*entry,qty*px)
        pnl=qty*(px-entry)-fee;cash+=qty*px-fee
        trades.append({'entry':entry,'exit':px,'qty':qty,'pnl':pnl,'date':curve[-1][0],
                       'forced_research_close':True})
        curve[-1]=(curve[-1][0],cash)
    peak=10000.;dd=0.
    for _,value in curve:
        peak=max(peak,value);dd=min(dd,100*(value/peak-1))
    wins=sum(t['pnl']>0 for t in trades)
    return {'return_pct':round((cash/10000-1)*100,3),'max_drawdown_pct':round(dd,3),
            'trades':len(trades),'win_rate_pct':round(100*wins/len(trades),2) if trades else None,
            'net_pnl':round(cash-10000,2),'cost_proxy':SPEC['cost_model'],
            'positive_net':cash>10000,'within_existing_drawdown_limit':dd>=-10,
            'curve':curve,'closed_trades':trades}


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--out',type=Path,required=True)
    p.add_argument('--end',default=datetime.now(timezone.utc).date().isoformat())
    p.add_argument('--cached',action='store_true');a=p.parse_args();a.out.mkdir(parents=True,exist_ok=True)
    manifest=a.out/'frozen-spec.json'
    encoded=json.dumps(SPEC,sort_keys=True,indent=2)
    if manifest.exists() and manifest.read_text()!=encoded:
        raise SystemExit('Existing frozen specification differs; use a distinct research run')
    manifest.write_text(encoded)  # written before fetching or examining outcomes
    results={};sources={}
    for symbol in SYMBOLS:
        path=a.out/(symbol+'.json')
        if not a.cached:
            url='https://query1.finance.yahoo.com/v8/finance/chart/'+symbol
            res=httpx.get(url,params={'period1':1514764800,'period2':int(pd.Timestamp(a.end,tz='UTC').timestamp()),
                           'interval':'1d','events':'splits,div'},headers={'User-Agent':'Mozilla/5.0'},timeout=30)
            res.raise_for_status();path.write_text(json.dumps(res.json()))
        body=json.loads(path.read_text());bars,splits=bars_from_response(body)
        if len(bars)<800: raise SystemExit('Insufficient independent daily history for '+symbol)
        frame=pd.DataFrame.from_dict(bars,orient='index').sort_index()
        sources[symbol]={'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
                         'bars':len(frame),'first':frame.index[0],'last':frame.index[-1],'splits':splits}
        results[symbol]={}
        for label,start,end in [('development','2020-01-01','2023-12-31'),('holdout','2024-01-01',a.end)]:
            results[symbol][label]={rule:{str(slip):replay(frame,rule,start,end,slip)
                for slip in SPEC['slippage_bps']} for rule in SPEC['rules']}
    report={'spec':SPEC,'sources':sources,'results':results,'promotion':'NOT AUTHORIZED BY THIS REPORT',
            'limitations':['Public vendor data require corporate-action and distribution verification.',
                'Single-ETF research, not the five-sleeve production portfolio or a broker fill test.',
                'Equity fees are a conservative ETF proxy; no claim of exact ETF economics.',
                'Existing book drawdown halt is measured, not simulated as a permanent stop.',
                'Published strategy hypotheses and a retrospective holdout do not guarantee an untouched future result.']}
    (a.out/'report.json').write_text(json.dumps(report,indent=2))
    for sym,windows in results.items():
        for window,rules in windows.items():
            for rule,runs in rules.items():
                print(sym,window,rule,{k:{x:v[x] for x in ('return_pct','max_drawdown_pct','trades')} for k,v in runs.items()})

if __name__=='__main__':main()
