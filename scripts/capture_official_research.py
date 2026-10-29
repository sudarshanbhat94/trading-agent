#!/usr/bin/env python3
"""Capture/replay official evidence in a dedicated database; never submit orders.

Exit 0 is captured source evidence, 2 is partial/missing coverage, 1 is failure.
None is execution, model, commercial or release approval.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import official_research


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, required=True)
    parser.add_argument('--archive', type=Path)
    sub = parser.add_subparsers(dest='action', required=True)
    replay = sub.add_parser('import', help='Import raw bytes with their original capture receipt')
    replay.add_argument('--feed', choices=sorted(official_research.FEEDS), required=True)
    replay.add_argument('--input', type=Path, required=True)
    replay.add_argument('--receipt', type=Path, required=True)
    live = sub.add_parser('refresh', help='One public request per requested source')
    live.add_argument('--delivery-session', required=True)
    live.add_argument('--rights-reference', required=True)
    sub.add_parser('report', help='Read-only current source coverage')
    args = parser.parse_args(argv)
    if args.action != 'report' and args.archive is None:
        parser.error('--archive is required for source capture')
    con = None
    try:
        if args.action == 'report':
            if not args.db.is_file():
                raise ValueError('Existing evidence database required')
            uri = args.db.resolve().as_uri()+'?mode=ro'
            con = sqlite3.connect(uri, uri=True)
        else:
            con = sqlite3.connect(args.db)
        with con:
            if args.action == 'import':
                raw = args.input.open('rb')
                try:
                    content = raw.read(official_research.MAX_RAW+1)
                finally:
                    raw.close()
                result = official_research.ingest(con, args.feed, content, args.archive,
                         json.loads(args.receipt.read_text()), now=datetime.now(timezone.utc))
            elif args.action == 'refresh':
                errors = []
                for feed, day in (('nifty100', None), ('delivery', args.delivery_session)):
                    try:
                        official_research.refresh(con, args.archive, feed, source_day=day,
                                                 rights_reference=args.rights_reference)
                    except Exception as exc:
                        errors.append({'feed': feed, 'error_type': type(exc).__name__})
                result = official_research.report(con)
                result['errors'] = errors
            else:
                result = official_research.report(con)
        # Source/account paths, licences and full metadata stay in the private
        # evidence DB. The CLI emits only bounded operational coverage.
        if 'feeds' in result:
            output = dict(feeds={feed: {key: row[key] for key in (
                'status', 'stale', 'source_records', 'facts', 'identities', 'matched', 'unmatched', 'unavailable') if key in row}
                for feed, row in result['feeds'].items()}, errors=result.get('errors', []),
                missing_connectors=result['missing_connectors'], execution_approved=False)
            healthy = not output['errors'] and all(row['status'] == 'captured' and not row.get('stale') for row in output['feeds'].values())
        else:
            output = {key: result[key] for key in ('feed', 'status', 'source_records', 'facts', 'identities',
                        'matched', 'unmatched', 'unavailable', 'execution_approved') if key in result}
            healthy = result['status'] == 'captured'
        print(json.dumps(output, sort_keys=True))
        return 0 if healthy else 2
    except Exception as exc:
        print(json.dumps({'status': 'failed', 'error_type': type(exc).__name__, 'execution_approved': False}))
        return 1
    finally:
        if con is not None:
            con.close()


if __name__ == '__main__':
    raise SystemExit(main())
