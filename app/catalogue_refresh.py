"""Bounded official BOD discovery refresh; it cannot grant order permission.

Upstox does not publish all exchange/settlement/circuit/action rules in this
file. Keep this provider separate from reviewed execution snapshots so a
daily discovery refresh cannot replace a reviewed identity with UNKNOWN.
"""
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3

from .account_safety import atomic
from . import instrument_catalog as catalogue

SOURCE = 'https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz'
PROVIDER = 'upstox-discovery'
MAX_COMPRESSED = 32 * 1024 * 1024
MAX_EXPANDED = 128 * 1024 * 1024
IST = timezone(timedelta(hours=5, minutes=30))


def ensure_schema(con):
    catalogue.ensure_schema(con)
    con.execute('''CREATE TABLE IF NOT EXISTS catalogue_refresh_events(
      id TEXT PRIMARY KEY, observed_at TEXT NOT NULL, payload TEXT NOT NULL)''')
    for action in ('UPDATE', 'DELETE'):
        con.execute(f'CREATE TRIGGER IF NOT EXISTS immutable_catalogue_refresh_{action.lower()} '
                    f'BEFORE {action} ON catalogue_refresh_events '
                    "BEGIN SELECT RAISE(ABORT,'immutable source refresh'); END")


def decode_master(raw):
    if not isinstance(raw, bytes) or not raw or len(raw) > MAX_COMPRESSED:
        raise ValueError('Official master compressed size is invalid')
    with gzip.GzipFile(fileobj=io.BytesIO(raw)) as zipped:
        expanded = zipped.read(MAX_EXPANDED + 1)
    if len(expanded) > MAX_EXPANDED:
        raise ValueError('Official master exceeds expanded size limit')
    records = json.loads(expanded)
    if not isinstance(records, list) or not records or len(records) > 500_000:
        raise ValueError('Official master layout changed')
    result = []
    for row in records:
        if not isinstance(row, dict):
            raise ValueError('Malformed official master row')
        if row.get('segment') != 'NSE_EQ' or row.get('instrument_type') not in {'EQ', 'ETF'}:
            continue
        if row.get('exchange') != 'NSE' or not isinstance(row.get('isin'), str) or \
                row.get('instrument_key') != 'NSE_EQ|' + row['isin']:
            raise ValueError('Cash alias does not match its sourced ISIN')
        # Preserve provider metadata, including unknown tick units and series.
        # No unit conversion, T+1/calendar default or guessed circuits here.
        result.append(catalogue.upstox_contract(row))
    if not result:
        raise ValueError('No NSE cash contracts in official master')
    return result, len(records)


def _archive(root, digest, raw):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if root.is_symlink():
        raise ValueError('Source archive must not be a symlink')
    path = root / (digest + '.json.gz')
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    except FileExistsError:
        if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError('Existing source archive digest mismatch')
        return path.name
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path.name


def ingest(con, raw, headers, archive_root, *, now=None):
    now = now or datetime.now(timezone.utc)
    try:
        published = parsedate_to_datetime(headers['last-modified'])
    except (KeyError, ValueError, TypeError) as exc:
        raise ValueError('Official source publication timestamp unavailable') from exc
    if now.tzinfo is None or published.tzinfo is None or published > now or now - published > timedelta(hours=25):
        raise ValueError('Official source publication is future-dated or stale')
    rows, total = decode_master(raw)
    digest = hashlib.sha256(raw).hexdigest()
    filename = _archive(archive_root, digest, raw)
    result = dict(source=SOURCE, provider=PROVIDER, source_day=published.astimezone(IST).date().isoformat(),
                  published_at=published.isoformat(), observed_at=now.isoformat(), sha256=digest,
                  archive=filename, source_records=total, cash_contracts=len(rows),
                  order_permission=False, commercial_rights_verified=False,
                  missing_rules=['reviewed series/tick units', 'circuits', 'settlement',
                                 'exchange sessions', 'restrictions/corporate actions'])
    ensure_schema(con)
    with atomic(con):
        result['snapshot_id'] = catalogue.import_snapshot(con, rows, provider=PROVIDER, source=SOURCE + '#sha256=' + digest,
            source_day=result['source_day'], observed_at=result['observed_at'], now=now)
        text = json.dumps(result, sort_keys=True, separators=(',', ':'), allow_nan=False)
        event_id = hashlib.sha256(text.encode()).hexdigest()
        con.execute('INSERT OR IGNORE INTO catalogue_refresh_events VALUES(?,?,?)', (event_id, now.isoformat(), text))
    return result


def refresh(con, archive_root, *, client=None, now=None):
    """One fixed-host request, no credentials, redirects or automatic retries."""
    import httpx
    owned = client is None
    client = client or httpx.Client(timeout=httpx.Timeout(20.0, connect=6.0), follow_redirects=False)
    try:
        with client.stream('GET', SOURCE, follow_redirects=False) as response:
            if response.status_code != 200:
                raise ValueError('Official BOD download did not return HTTP 200')
            length = response.headers.get('content-length')
            if length is not None and (not length.isdigit() or int(length) > MAX_COMPRESSED):
                raise ValueError('Official master declared size is invalid')
            chunks, length = [], 0
            for chunk in response.iter_bytes():
                length += len(chunk)
                if length > MAX_COMPRESSED:
                    raise ValueError('Official master download exceeds size limit')
                chunks.append(chunk)
            return ingest(con, b''.join(chunks), response.headers, archive_root, now=now)
    finally:
        if owned:
            client.close()


def report(con, *, now=None):
    now = now or datetime.now(timezone.utc)
    try:
        row = con.execute('SELECT payload FROM catalogue_refresh_events '
                          'WHERE julianday(observed_at)<=julianday(?) ORDER BY julianday(observed_at) DESC LIMIT 1',
                          (now.isoformat(),)).fetchone()
    except sqlite3.Error:
        row = None
    if not row:
        return dict(status='missing', order_permission=False)
    result = json.loads(row[0])
    published = datetime.fromisoformat(result['published_at'])
    result['status'] = 'fresh' if timedelta() <= now - published <= timedelta(hours=25) else 'stale'
    return result
