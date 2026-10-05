"""Observe issued ideas using the existing feed; no trading/application startup."""
import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.screening import tracking, confirmation, store

ROOT=Path(__file__).resolve().parents[1]


def cycle(main,path):
    if Path(main).resolve()==Path(path).resolve():
        raise ValueError('tracking output cannot be the market database')
    con=tracking.connect(path);con.close()
    wanted=tracking.symbols(path)
    quotes={}
    con=sqlite3.connect(f'file:{Path(main).resolve()}?mode=ro',uri=True,timeout=5)
    try:
        # Bind variables; only the existing quote feed is read.
        if wanted:
            for symbol,price,ts in con.execute("SELECT symbol,price,ts FROM latest_quotes WHERE source='upstox-live' AND symbol IN ("+','.join('?' for _ in wanted)+')',wanted):
                quotes[symbol]=dict(price=price,ts=ts,source='upstox-live')
    finally:
        con.close()
    health=tracking.observe(path,quotes)
    folder=Path(main).parent
    try:
        screen=store.report(os.getenv('SCREENING_DB',str(folder/'screening.db')))
        view_path=Path(os.getenv('SLEEVE_VIEW_FILE',str(folder/'sleeve_view.json')))
        regime=json.loads(view_path.read_text()).get('IN',{}) if view_path.exists() else {}
        health['assessed']=confirmation.refresh(main,path,
            os.getenv('V2_PAPER_DB',str(folder/'v2_paper.db')),screen,regime)
    except (sqlite3.Error,OSError,ValueError):
        health['assessment_error']='Research confirmation evidence unavailable; no eligibility claim'
    con=tracking.connect(path)
    try:
        with con:con.execute('INSERT OR REPLACE INTO health VALUES(1,?)',(tracking._json(health),))
    finally:con.close()
    return health


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--main-db',default=os.getenv('OPENSTOCKS_DB',str(ROOT/'var'/'trading_agent.db')))
    parser.add_argument('--tracking-db',default=os.getenv('IDEA_TRACKING_DB',str(ROOT/'var'/'idea_tracking.db')))
    parser.add_argument('--report-user',type=int,help='read-only JSON report for this user; no poll or publication')
    parser.add_argument('--offset',type=int,default=0)
    parser.add_argument('--limit',type=int,default=100)
    parser.add_argument('--bootstrap',action='store_true',help='import the initial captured plans and quote samples')
    args=parser.parse_args()
    if args.report_user is not None:
        data=tracking.report(args.tracking_db,args.report_user,limit=max(1,args.limit),offset=max(0,args.offset))
    elif args.bootstrap:
        folder=Path(args.main_db).parent
        data=dict(imported_samples=tracking.import_bootstrap(args.tracking_db,folder/'idea_tracking_bootstrap.json',folder/'idea_quote_bootstrap.jsonl'))
    else:
        data=cycle(args.main_db,args.tracking_db)
    print(json.dumps(data,allow_nan=False))


if __name__=='__main__':
    main()
