"""Read-only external price audit of recorded paper equity trades.

Daily ranges can detect suspect prices; they cannot verify intraday fill order.
This is trade attribution, not a backtest or evidence of future profitability.
Raw public responses are cached alongside the report for reproducibility.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
from urllib.parse import quote

import httpx

IST = timezone(timedelta(hours=5,minutes=30))


def bars_from_response(body):
    result = (body.get("chart",{}).get("result") or [None])[0]
    if not result:
        return {}, []
    q = result["indicators"]["quote"][0]
    bars = {}
    for i, stamp in enumerate(result.get("timestamp",[])):
        row = {k:(q.get(k,[])[i] if i < len(q.get(k,[])) else None)
               for k in ("open","high","low","close","volume")}
        if any(not isinstance(row[k], (float, int)) or not math.isfinite(row[k]) or row[k] <= 0
               for k in ("open","high","low","close")):
            continue
        if not row['low'] <= min(row['open'], row['close']) <= max(row['open'], row['close']) <= row['high']:
            continue
        day = datetime.fromtimestamp(stamp,IST).date().isoformat()
        bars[day] = row
    splits = sorted(datetime.fromtimestamp(v["date"],IST).date().isoformat()
                    for v in result.get("events",{}).get("splits",{}).values())
    return bars, splits


def range_check(price, day, bars, splits):
    if any(s > day for s in splits):
        return "corporate_action_review"
    b = bars.get(day)
    if not b:
        return "unavailable"
    return "within_daily_range" if b["low"]*.995 <= price <= b["high"]*1.005 else "outside_daily_range"


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--book",required=True,type=Path)
    p.add_argument("--since",required=True)
    p.add_argument("--until",default=datetime.now(IST).date().isoformat())
    p.add_argument("--out",required=True,type=Path)
    a=p.parse_args()
    a.out.mkdir(parents=True,exist_ok=True)
    c=sqlite3.connect(f"file:{a.book}?mode=ro",uri=True);c.row_factory=sqlite3.Row
    rows=[dict(r) for r in c.execute("SELECT id,symbol,strategy,sleeve,regime,entry_date,exit_date,"
          "entry_price,exit_price,shares,pnl,reason,closed_at FROM v2_trades "
          "WHERE market='IN' AND exit_date>=? AND exit_date<=? ORDER BY id",(a.since,a.until))]
    epoch=c.execute("SELECT started_at FROM v2_book WHERE market='IN'").fetchone()[0]
    c.close()
    eq=[r for r in rows if not re.search(r"\d(CE|PE)$",r["symbol"]) and r["strategy"]!='index_options']
    symbols=sorted({r["symbol"]+'.NS' for r in eq}|{'^NSEI'})
    start=min([a.since]+[r["entry_date"] for r in eq])
    p1=int(datetime.fromisoformat(start).replace(tzinfo=IST).timestamp())-86400*10
    p2=int((datetime.fromisoformat(a.until).replace(tzinfo=IST)+timedelta(days=1)).timestamp())
    def fetch(symbol):
        path=a.out/(symbol.replace('^','_')+'.json')
        url='https://query1.finance.yahoo.com/v8/finance/chart/'+quote(symbol,safe='')
        try:
            r=httpx.get(url,params=dict(period1=p1,period2=p2,interval='1d',events='splits'),
                        headers={'User-Agent':'Mozilla/5.0'},timeout=25)
            r.raise_for_status();body=r.json();path.write_text(json.dumps(body))
            bars,splits=bars_from_response(body)
            return symbol,dict(bars=bars,splits=splits,sha256=hashlib.sha256(path.read_bytes()).hexdigest(),source=url)
        except Exception as exc:
            return symbol,dict(bars={},splits=[],error=type(exc).__name__)
    with ThreadPoolExecutor(max_workers=4) as pool:
        data=dict(pool.map(fetch,symbols))
    benchmark=data['^NSEI']['bars']
    for r in eq:
        source=data[r['symbol']+'.NS']
        r['entry_check']=range_check(r['entry_price'],r['entry_date'],source['bars'],source['splits'])
        r['exit_check']=range_check(r['exit_price'],r['exit_date'],source['bars'],source['splits'])
        r['gross_pnl']=round(r['shares']*(r['exit_price']-r['entry_price']),2)
        r['gross_return_pct']=round(100*(r['exit_price']/r['entry_price']-1),3)
        r['current_epoch']=bool(r['closed_at'] and datetime.fromisoformat(r['closed_at']).replace(
            tzinfo=datetime.fromisoformat(r['closed_at']).tzinfo or timezone.utc)>=datetime.fromisoformat(epoch))
        b0,b1=benchmark.get(r['entry_date']),benchmark.get(r['exit_date'])
        r['benchmark_close_return_pct']=round(100*(b1['close']/b0['close']-1),3) if b0 and b1 else None
    summary={}
    for r in eq:
        k=(r['sleeve'] or r['strategy'])
        s=summary.setdefault(k,dict(trades=0,gross_pnl=0.,net_pnl=0.,gross_winners=0,net_winners=0))
        s['trades']+=1;s['gross_pnl']+=r['gross_pnl'];s['net_pnl']+=r['pnl'] or 0
        s['gross_winners']+=r['gross_pnl']>0;s['net_winners']+=(r['pnl'] or 0)>0
    for s in summary.values():
        s['gross_pnl']=round(s['gross_pnl'],2);s['net_pnl']=round(s['net_pnl'],2)
    report=dict(retrieved_at=datetime.now(timezone.utc).isoformat(),since=a.since,until=a.until,
        equity_trades=len(eq),excluded_derivative_trades=len(rows)-len(eq),by_sleeve=summary,
        coverage={s:dict(bars=len(v['bars']),first=min(v['bars'],default=None),last=max(v['bars'],default=None),
                        sha256=v.get('sha256'),error=v.get('error')) for s,v in data.items()},
        caveats=['Daily range checks allow 0.5% tolerance; agreement does not validate intraday execution or trailing-stop ordering.',
                 'Prices may be split-adjusted; corporate-action cases require review.',
                 'Nifty comparison uses daily closes, not the trade timestamps.',
                 'Selected recorded trades cannot establish out-of-sample strategy profitability.'],trades=eq)
    (a.out/'report.json').write_text(json.dumps(report,indent=2))
    print(json.dumps({k:report[k] for k in ('equity_trades','excluded_derivative_trades','by_sleeve')},indent=2))


if __name__=='__main__':
    main()
