"""Refresh dated NSE continuous-session evidence from the configured Upstox feed.

The timing response describes the day's exchange hours; the separate status
response describes the current phase. Both must agree before an open session
can be recorded. This job never creates approvals, books or broker orders.
"""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3

from . import execution_contracts

IST = timezone(timedelta(hours=5, minutes=30))
CALENDAR = 'NSE:CASH:CONTINUOUS'
VERSION = 'upstox-nse-session-v1'
STATUS_TTL = timedelta(minutes=3)
BASE = 'https://api.upstox.com/v2/market/'


def _millis(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError('Invalid exchange timestamp')
    try:
        return datetime.fromtimestamp(value / 1000, timezone.utc)
    except (ValueError, OverflowError, OSError) as exc:
        raise ValueError('Invalid exchange timestamp') from exc


def normalise(timings, status, session_day, observed_at):
    if observed_at.tzinfo is None or observed_at.astimezone(IST).date().isoformat() != session_day:
        raise ValueError('Current aware source day required')
    if not isinstance(timings, dict) or timings.get('status') != 'success' or not isinstance(timings.get('data'), list):
        raise ValueError('Dated exchange timings unavailable')
    if not isinstance(status, dict) or status.get('status') != 'success' or not isinstance(status.get('data'), dict):
        raise ValueError('Current exchange phase unavailable')
    state = status['data']
    if state.get('exchange') != 'NSE' or not isinstance(state.get('status'), str) or not state['status']:
        raise ValueError('Exchange phase identity unavailable')
    changed = _millis(state.get('last_updated'))
    if changed > observed_at:
        raise ValueError('Future exchange status')
    rows = [r for r in timings['data'] if isinstance(r, dict) and r.get('exchange') == 'NSE']
    if len(rows) > 1:
        raise ValueError('Ambiguous exchange sessions')
    start = observed_at.astimezone(IST).replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    payload = dict(open=False, phase=state['status'], phase_changed_at=changed.isoformat(),
                   session_day=session_day, normalizer_version=VERSION,
                   fresh_until=min(observed_at + STATUS_TTL, end).isoformat())
    if rows:
        opens, closes = _millis(rows[0].get('start_time')), _millis(rows[0].get('end_time'))
        if not start <= opens < closes <= end:
            raise ValueError('Exchange times disagree with requested day')
        payload.update(opens_at=opens.isoformat(), closes_at=closes.isoformat())
        if state['status'] == 'NORMAL_OPEN':
            if not opens <= observed_at < closes or changed.astimezone(IST).date().isoformat() != session_day:
                raise ValueError('Open exchange phase contradicts the dated schedule')
            payload['open'] = True
    elif state['status'] == 'NORMAL_OPEN':
        raise ValueError('Open exchange phase has no dated session')
    return payload, start, end


def configured_token(database_path):
    """Read the existing market-data connection without editing broker secrets."""
    from .config import Settings, settings_from_overrides
    from .market_data import normalize_upstox_access_token
    with sqlite3.connect(Path(database_path).resolve().as_uri()+'?mode=ro', uri=True) as con:
        overrides = {}
        for key, value in con.execute('SELECT key,value FROM runtime_settings'):
            try: overrides[key] = json.loads(value)
            except (ValueError, TypeError): overrides[key] = value
    token = normalize_upstox_access_token(settings_from_overrides(Settings(), overrides).upstox_access_token)
    if not token:
        raise ValueError('Configured Upstox market-data token unavailable')
    return token


def archive_bytes(root, raw, *, suffix='.json'):
    root = Path(root); root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.is_symlink(): raise ValueError('Source archive cannot be a symlink')
    digest = hashlib.sha256(raw).hexdigest(); path = root / (digest + suffix)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    except FileExistsError:
        if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError('Archived source differs from its digest')
    else:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(raw); handle.flush(); os.fsync(handle.fileno())
    return digest, path.name


def fetch_json(http, url, archive_root, *, params=None):
    """Bound response size; never serialize request headers or credentials."""
    with http.stream('GET', url, params=params, follow_redirects=False) as response:
        response.raise_for_status()
        chunks=[]; size=0
        for part in response.iter_bytes():
            size += len(part)
            if size > 16 * 1024 * 1024: raise ValueError('Source response exceeds size bound')
            chunks.append(part)
        raw=b''.join(chunks)
    observed=datetime.now(timezone.utc)
    digest, filename=archive_bytes(archive_root,raw)
    return json.loads(raw), dict(source=url,sha256=digest,path=filename,observed_at=observed.isoformat())


def refresh(con, archive_root, *, token, client=None):
    import httpx
    http=client or httpx.Client(timeout=15, follow_redirects=False,
                              headers={'Authorization':'Bearer '+token,'Accept':'application/json'})
    try:
        day=datetime.now(IST).date().isoformat()
        timings,ta=fetch_json(http,BASE+'timings/'+day,archive_root)
        status,sa=fetch_json(http,BASE+'status/NSE',archive_root)
        observed=datetime.now(timezone.utc)
        payload,start,end=normalise(timings,status,day,observed)
        evidence=execution_contracts.record(con,'session',CALENDAR,payload,
            source=json.dumps([ta,sa],sort_keys=True),observed_at=observed.isoformat(),
            effective_from=start.isoformat(),effective_until=end.isoformat(),now=observed)
        return dict(status='ok',calendar=CALENDAR,evidence_id=evidence,**payload)
    finally:
        if client is None: http.close()
