#!/usr/bin/env python3
"""Verify candidate startup against private copies. Never starts a worker."""
import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('paper','accounts','protocol','destination'):
        parser.add_argument('--'+name,type=Path,required=True)
    parser.add_argument('--user-id',type=int,required=True)
    args=parser.parse_args()
    # Before settings or database modules import. Only isolated paths are used.
    for name in ('OPENSTOCKS_DISABLE_ENGINE','OPENSTOCKS_DISABLE_V2'):
        os.environ[name]='1'
    for name in ('TELEGRAM_BOT_TOKEN','WHATSAPP_ACCESS_TOKEN'):
        os.environ[name]=''
    for name,filename in (('OPENSTOCKS_DB','accounts.db'),('DATABASE_PATH','accounts.db'),
                         ('V2_PAPER_DB','paper.db'),('OPENSTOCKS_CATALOGUE_DB','catalogue.db'),
                         ('CATALYST_DB','catalysts.db')):
        os.environ[name]=str(args.destination/'candidate'/filename)
    os.environ['BROKER_STATE_DIR']=str(args.destination/'candidate'/'unavailable-brokers')
    from app.upgrade_rehearsal import rehearse
    def migrate_paper(path):
        from app import v2_live
        with closing(sqlite3.connect(path)) as con: v2_live.ensure_schema(con)
    def migrate_accounts(path):
        from app.db import Database
        Database(path).init()
    try:
        result=rehearse(args.paper,args.accounts,args.protocol,args.destination,user_id=args.user_id,
                       migrate_paper=migrate_paper,migrate_accounts=migrate_accounts)
        print(json.dumps(result,sort_keys=True)); return 0
    except Exception as exc:
        # No raw exception paths, private rows or source values in stdout.
        print(json.dumps(dict(status='failed',error_type=type(exc).__name__,execution_disabled=True)))
        return 1


if __name__=='__main__':raise SystemExit(main())
