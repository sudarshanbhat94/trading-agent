#!/usr/bin/env python3
"""Refresh official discovery and import a separately reviewed daily rule bundle.

Exit 2 means discovery alone is insufficient: it is never order permission.
No paper book, strategy, broker credential or release authorization is edited.
"""
import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.catalogue_refresh import refresh, IST
from app.catalogue_ingestion import import_bundle
from app import execution_contracts
from app.account_safety import atomic


def apply_reviewed(con, bundle, root, discovery_day, *, now=None):
    now = now or datetime.now(timezone.utc)
    today = now.astimezone(IST).date().isoformat()
    if bundle.get('source_day') != today or discovery_day != today:
        raise ValueError('Current NSE source-day evidence required for the daily rule job')
    from app.instrument_catalog import Instrument
    with atomic(con):
        result = import_bundle(con, bundle, root, now=now)
        # Import counts alone do not establish current rule coverage. Keep
        # the validation in the same transaction: an expired import rolls back.
        for row in bundle['instruments']:
            if row.get('rules') is None:
                continue
            # A new daily snapshot cannot borrow an older valid rule for the
            # same identity while its own review is expired or future-dated.
            if not execution_contracts._moment(row['effective_from']) <= now < execution_contracts._moment(row['effective_until']):
                raise ValueError('Daily contract row is not currently effective')
            if now-execution_contracts._moment(row['observed_at']) > timedelta(hours=25):
                raise ValueError('Daily contract row review is stale')
            identity = Instrument(**row['contract']).id
            _, rules = execution_contracts._latest(con, 'rules', identity, now)
            if rules != row['rules']:
                raise ValueError('Daily contract row conflicts with the current reviewed rule')
            if (now-execution_contracts._moment(rules['actions_reviewed_at'])).total_seconds() > 25*3600:
                raise ValueError('Current corporate-action review required')
            execution_contracts._latest(con, 'session', rules['calendar'], now)
        for row in bundle['sessions']:
            if not execution_contracts._moment(row['effective_from']) <= now < execution_contracts._moment(row['effective_until']):
                raise ValueError('Daily exchange session is not currently effective')
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, required=True)
    parser.add_argument('--archive', type=Path, required=True)
    parser.add_argument('--bundle', type=Path, default=os.environ.get('OPENSTOCKS_CONTRACT_BUNDLE') or None)
    args = parser.parse_args()
    try:
        with sqlite3.connect(args.db) as con:
            result = dict(discovery=refresh(con, args.archive), reviewed_rules=None, order_permission=False)
            if args.bundle:
                bundle = json.loads(args.bundle.read_text())
                result['reviewed_rules'] = apply_reviewed(con, bundle, args.bundle.parent, result['discovery']['source_day'])
        print(json.dumps(result, sort_keys=True))
        return 0 if result['reviewed_rules'] and result['reviewed_rules']['rules'] else 2
    except Exception as exc:
        # Exceptions can carry private paths/response bodies. Output only type.
        print(json.dumps(dict(status='failed', error_type=type(exc).__name__, order_permission=False)))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
