#!/usr/bin/env python3
"""Refresh paper-only stock contracts. Never opens orders or edits a book."""
import argparse
import json
from pathlib import Path
import sqlite3
import sys
from datetime import datetime, timezone
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.nse_cash_contract_feed import refresh
from app.sleeves.reference import snapshot


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db',type=Path,required=True)
    parser.add_argument('--reference-db',type=Path,required=True)
    parser.add_argument('--archive',type=Path,required=True)
    args=parser.parse_args()
    try:
        members,_=snapshot(datetime.now(timezone.utc),str(args.reference_db))
        if not members: raise ValueError('Verified Nifty 500 membership unavailable')
        with sqlite3.connect(args.db, timeout=30) as con:
            result=refresh(con,args.archive,members)
        print(json.dumps(result,sort_keys=True))
        return 0
    except Exception as exc:
        print(json.dumps(dict(status='failed',error_type=type(exc).__name__,broker_execution=False)))
        return 1


if __name__=='__main__': raise SystemExit(main())
