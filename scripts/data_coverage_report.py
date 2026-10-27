"""Read-only sampled coverage report. No receipt is proof of continuous coverage."""
import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys
from contextlib import closing
from datetime import date, datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.market_regions import market_session_for_region


def stamp(value):
    at = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if at.tzinfo is None:
        raise ValueError('Aware receipt time required')
    return at.astimezone(timezone.utc)


def integer(value):
    return type(value) is int and value >= 0


def require(condition):
    if not condition:
        raise ValueError('Invalid coverage receipt')


def receipt(row, market, lane, day):
    at = stamp(row['checked_at'])
    counts = [row[k] for k in ('requested', 'returned', 'fresh', 'missing', 'stale')]
    n, returned, fresh, missing, stale = counts
    require(row['market'] == market and row['lane'] == lane and at.date().isoformat() == day)
    require(market_session_for_region(market, at)['is_open'] and all(map(integer, counts)) and n > 0)
    require(n == returned + missing and returned == fresh + stale)
    require(row['status'] in ('polled', 'fetch_error'))
    require(row['coverage_source'] == ('quote_provider_response' if row['status'] == 'polled' else 'quote_provider_error'))
    sources = row['source_counts']
    require(isinstance(sources, dict) and all(isinstance(k, str) and integer(v) for k, v in sources.items()))
    require(sum(sources.values()) == returned and (row['status'] != 'fetch_error' or returned == 0))
    live = sources.get('upstox-live', 0) if market == 'IN' else sum(v for k, v in sources.items() if k.startswith('alpaca-') and k.endswith('-live'))
    require(fresh <= live)
    for key, count in (('missing_sample', missing), ('stale_sample', stale)):
        sample = row[key]
        require(isinstance(sample, list) and len(sample) <= min(10, count))
        require(all(isinstance(s, str) and 0 < len(s) <= 40 and all(c.isalnum() or c in '&_.-' for c in s) for s in sample))
    return at


def lane_report(folder, day, market, lane):
    observations, invalid = {}, 0
    try:
        with (Path(folder) / f'coverage_{market}_{lane}_{day}.jsonl').open() as archive:
            for line in archive:
                try:
                    row = json.loads(line)
                    at = receipt(row, market, lane, day)
                    if at in observations:
                        raise ValueError('Duplicate receipt')
                    observations[at] = row
                except (AssertionError, KeyError, TypeError, ValueError, AttributeError):
                    invalid += 1
    except FileNotFoundError:
        pass
    except (OSError, UnicodeError):
        invalid += 1
    times = sorted(observations)
    rows = [observations[t] for t in times]
    percentages = [100 * r['fresh'] / r['requested'] for r in rows]
    gaps = partial = 0
    if times:
        session = market_session_for_region(market, times[0])
        edges = [stamp(session['open_time']), *times, stamp(session['close_time'])]
        gaps = sum((b - a).total_seconds() > 90 for a, b in zip(edges, edges[1:]))
        partial = sum(bool(r['missing'] or r['stale'] or r['status'] != 'polled') for r in rows)
    return dict(status='pending_verification' if not times else 'partial' if invalid or gaps or partial else 'sampled_only',
                observations=len(times), invalid_receipts=invalid, gaps_over_90s=gaps, partial_receipts=partial,
                first=times[0].isoformat() if times else None, last=times[-1].isoformat() if times else None,
                requested_min=min((r['requested'] for r in rows), default=None),
                requested_max=max((r['requested'] for r in rows), default=None),
                min_fresh_pct=round(min(percentages), 2) if percentages else None,
                average_fresh_pct=round(sum(percentages) / len(percentages), 2) if percentages else None,
                missing_max=max((r['missing'] for r in rows), default=None),
                stale_max=max((r['stale'] for r in rows), default=None),
                missing_sample=sorted({s for r in rows for s in r['missing_sample']})[:10],
                stale_sample=sorted({s for r in rows for s in r['stale_sample']})[:10])


def daily_report(path, day):
    try:
        with closing(sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=1)) as con:
            found = con.execute("SELECT value FROM agent_state WHERE key='daily_candle_ingestion'").fetchone()
        if found is None:
            return dict(status='pending_verification')
        receipt = json.loads(found[0])
        checked_at = stamp(receipt['checked_at']).isoformat()
        require(isinstance(receipt['markets'], list) and receipt['markets'])
        markets = []
        for row in receipt['markets']:
            counts = [row[k] for k in ('requested', 'fresh', 'missing', 'unresolved', 'missing_held')]
            n, fresh, missing, unresolved, held = counts
            require(row['market'] in ('IN', 'US') and all(map(integer, counts)) and n > 0)
            require(fresh + missing == n and unresolved <= missing)
            require(date.fromisoformat(row['target']).isoformat() == row['target'])
            status = 'date_mismatch' if row['target'] != day else row['status']
            require(row['status'] in ('complete', 'partial', 'failed'))
            if status == 'complete' and (unresolved or held):
                status = 'partial'
            markets.append(dict(market=row['market'], status=status, target=row['target'],
                                **dict(zip(('requested', 'fresh', 'missing', 'unresolved', 'missing_held'), counts))))
        return dict(status='recorded' if all(r['status'] == 'complete' for r in markets) else 'partial',
                    checked_at=checked_at, markets=markets)
    except (OSError, sqlite3.Error, AssertionError, KeyError, TypeError, ValueError, AttributeError):
        return dict(status='unavailable_or_malformed')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--date', required=True, help='YYYY-MM-DD; archive UTC date')
    parser.add_argument('--archive-dir', type=Path)
    parser.add_argument('--db', type=Path, help='Optional read-only daily_candle_ingestion state lookup')
    args = parser.parse_args(argv)
    try:
        if date.fromisoformat(args.date).isoformat() != args.date:
            raise ValueError('Noncanonical date')
    except ValueError:
        parser.error('--date must be YYYY-MM-DD')
    main_db = Path(os.getenv('OPENSTOCKS_DB', '/opt/opentrade/var/trading_agent.db'))
    folder = args.archive_dir or Path(os.getenv('QUOTE_FEED_COVERAGE_DIR', str(main_db.parent / 'quote_feed_coverage')))
    markets = ['IN'] + (['US'] if any(folder.glob(f'coverage_US_*_{args.date}.jsonl')) else [])
    lanes = {f'{m}_{lane}': lane_report(folder, args.date, m, lane) for m in markets for lane in ('hot', 'full')}
    report = dict(date=args.date, note='Sampled receipts only; unobserved session edges and gaps >90s are partial.', lanes=lanes)
    if args.db is not None:
        report['daily_candle_ingestion'] = daily_report(args.db, args.date)
    print(json.dumps(report, sort_keys=True, indent=2))
    return 0 if all(r['status'] == 'sampled_only' for r in lanes.values()) and report.get('daily_candle_ingestion', {}).get('status', 'recorded') == 'recorded' else 1


if __name__ == '__main__':
    raise SystemExit(main())
