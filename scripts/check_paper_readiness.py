#!/usr/bin/env python3
"""Read the actual website preflight without submitting orders or initializing state.

Exit 0: selected instruments are ready for further order checks, not certified.
Exit 2: blocked/waiting. Exit 1: report unavailable. Paths must already exist.
"""
import argparse
import json
import os
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--paper', type=Path, required=True)
    parser.add_argument('--market', type=Path, required=True)
    parser.add_argument('--catalogue', type=Path, required=True)
    parser.add_argument('--user-id', type=int, required=True)
    parser.add_argument('--symbols', default='RELIANCE,ITC')
    parser.add_argument('--decision', type=Path)
    args = parser.parse_args()
    if args.user_id < 1 or any(not p.is_file() for p in (args.paper, args.market, args.catalogue)):
        print(json.dumps(dict(status='unavailable', reason='Existing private book, market and catalogue paths required')))
        return 1
    for key, path in [('V2_PAPER_DB', args.paper), ('OPENSTOCKS_DB', args.market), ('OPENSTOCKS_CATALOGUE_DB', args.catalogue)]:
        os.environ[key] = str(path.resolve())
    if args.decision:
        os.environ['SLEEVE_VIEW_FILE'] = str(args.decision.resolve())
    try:
        from app.v2_web import api_trading_readiness
        response = api_trading_readiness(symbols=args.symbols, user={'id': args.user_id})
        result = json.loads(response.body)
        print(json.dumps(result, sort_keys=True, allow_nan=False))
        return 0 if result['paper']['status'] == 'ready_for_order_checks' else 2
    except Exception as exc:
        print(json.dumps(dict(status='unavailable', error_type=type(exc).__name__)))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
