#!/usr/bin/env python3
"""Refresh sourced NSE cash session status, without changing any trading book."""
import argparse
import json
from pathlib import Path
import sqlite3
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.exchange_session_feed import configured_token, refresh


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db',type=Path,required=True)
    parser.add_argument('--market-db',type=Path,required=True)
    parser.add_argument('--archive',type=Path,required=True)
    args=parser.parse_args()
    try:
        with sqlite3.connect(args.db) as con:
            result=refresh(con,args.archive,token=configured_token(args.market_db))
        print(json.dumps(result,sort_keys=True))
        return 0
    except Exception as exc:
        # HTTP exceptions may contain private request details. Never dump them.
        print(json.dumps(dict(status='failed',error_type=type(exc).__name__,order_permission=False)))
        return 1


if __name__=='__main__':
    raise SystemExit(main())
