"""Frozen, independent research of generic historical benchmark fixtures.

No account, trade ledger, broker credentials or production database is read.
No orders are placed. This compares hypotheses; it does not promote a strategy.
Signals use completed closes, fills use the next session open plus slippage.
Generic replay helpers are retained for accounting regression tests only.
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

# Generic accounting fixture only. No named fund, live source or executable
# index route is attached to these retained regression helpers.
SPEC={'max_deployed':.35,'cost_model':'generic equity delivery fixture'}



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
    raise SystemExit('Retired fund research runner removed. Use actual index evidence and a separately certified tradable contract; historical generic replay helpers are not index execution proof.')

if __name__=='__main__': main()
