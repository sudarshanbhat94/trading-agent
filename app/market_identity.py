"""Reconcile ingestion identities from two official, current masters.

Changing an ISIN must never concatenate two price bases. Prior market history
is archived before rebuilding; owned positions are quarantined for corporate
action review. Paper books, historical fills and user preferences are untouched.
"""
import csv
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import hashlib
import io
import json
from pathlib import Path

import httpx

from .exchange_session_feed import archive_bytes, IST
from .nse_cash_contract_feed import expanded, JSON_URL, HEADERS, unique
from .instrument_policy import is_retired

NSE_URL='https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv'


def read_source(http,url,archive,now,compressed=False):
    with http.stream('GET',url,follow_redirects=False) as response:
        response.raise_for_status();parts=[];size=0
        for part in response.iter_bytes():
            size+=len(part)
            if size>32*1024*1024:raise ValueError('Oversized instrument master')
            parts.append(part)
        headers=dict(response.headers)
    raw=b''.join(parts)
    if compressed:
        published=parsedate_to_datetime(headers['last-modified'])
        if published.tzinfo is None or published>now or published.astimezone(IST).date()!=now.astimezone(IST).date():
            raise ValueError('Current instrument master required')
    digest,filename=archive_bytes(archive,raw,suffix='.gz' if compressed else '.csv')
    return expanded(raw) if compressed else raw.decode('utf-8-sig'),dict(url=url,sha256=digest,file=filename)


def reconcile(db,nse_rows,broker_rows,members,held,now,*,apply=True):
    """Pure source selection precedes a single atomic market-data transaction."""
    if now.tzinfo is None:raise ValueError('Aware identity capture time required')
    nse=unique(nse_rows,lambda r:str(r.get('SYMBOL','')).strip())
    broker=unique([r for r in broker_rows if r.get('segment')=='NSE_EQ'],lambda r:r.get('trading_symbol'))
    existing={r['symbol']:r for r in db.get_universe(enabled_only=False,market_region='IN')}
    other_symbols={r['symbol'] for r in db.get_universe(enabled_only=False,market_region='US')}
    issues=[];updates=[];added=[];verified=[]
    required={s for s,r in existing.items() if r.get('enabled') or s in held}|set(members)
    for symbol in sorted(required):
        if symbol in other_symbols:
            issues.append(dict(symbol=symbol,reason='cross_market_symbol_collision'));continue
        old=existing.get(symbol);b=broker.get(symbol);n=nse.get(symbol)
        if old and str(old.get('exchange','')).upper()!='NSE':continue
        if not b or is_retired((old or {}).get('upstox_instrument_key')):
            if not (old and is_retired(old.get('upstox_instrument_key'))):issues.append(dict(symbol=symbol,reason='absent_or_ambiguous_current_master'))
            continue
        key=b.get('instrument_key');isin=b.get('isin')
        if not key or key!='NSE_EQ|'+str(isin):
            issues.append(dict(symbol=symbol,reason='broker_identity_conflict'));continue
        old_key=(old or {}).get('upstox_instrument_key')
        if old_key==key:
            verified.append(symbol);continue
        if not n or str(n.get('ISIN NUMBER','')).strip()!=isin or str(n.get('SERIES','')).strip()!=b.get('instrument_type'):
            issues.append(dict(symbol=symbol,reason='independent_exchange_identity_unverified'));continue
        if symbol in held:
            issues.append(dict(symbol=symbol,reason='held_identity_change_requires_corporate_action_review'));continue
        if old:
            updates.append((symbol,old_key,key))
        elif symbol in members and b.get('instrument_type')=='EQ':
            added.append(dict(symbol=symbol,name=str(n.get('NAME OF COMPANY') or symbol).strip(),exchange='NSE',
                upstox_instrument_key=key,yahoo_symbol=symbol+'.NS',enabled=1))
        else:issues.append(dict(symbol=symbol,reason='unsupported_new_constituent_series'))
    # New inserts use the repository's normalized schema/defaults. Existing
    # rows keep sectors, enabled flags and all user-selected metadata.
    result=dict(status='partial' if issues else 'complete',observed_at=now.isoformat(),
                verified=len(verified),updated=[s for s,_,_ in updates],added=[r['symbol'] for r in added],issues=issues,
                note='Old price bases are archived, not concatenated. Held identity changes require review.')
    if not apply:return result
    normalized=[db._normalize_universe_row(r) for r in added]
    with db.connect() as con:
        con.execute('CREATE TABLE IF NOT EXISTS market_identity_archive(id TEXT PRIMARY KEY,symbol TEXT NOT NULL,old_key TEXT,new_key TEXT NOT NULL,observed_at TEXT NOT NULL,kind TEXT NOT NULL,payload TEXT NOT NULL)')
        for action in ('UPDATE','DELETE'):
            con.execute(f"CREATE TRIGGER IF NOT EXISTS immutable_market_identity_{action.lower()} BEFORE {action} ON market_identity_archive BEGIN SELECT RAISE(ABORT,'immutable market identity archive'); END")
        con.execute('BEGIN IMMEDIATE')
        for symbol,old_key,new_key in updates:
            current=con.execute('SELECT upstox_instrument_key FROM universe WHERE symbol=?',(symbol,)).fetchone()
            if not current or current[0]!=old_key:raise ValueError('Concurrent instrument identity change')
            for table in ('candles','latest_quotes'):
                cursor=con.execute('SELECT * FROM '+table+" WHERE symbol=? AND source LIKE 'upstox-live%'",(symbol,))
                columns=[d[0] for d in cursor.description]
                for row in cursor.fetchall():
                    payload=json.dumps(dict(zip(columns,row)),sort_keys=True,allow_nan=False)
                    identity=hashlib.sha256(json.dumps([symbol,old_key,new_key,table,payload]).encode()).hexdigest()
                    con.execute('INSERT OR IGNORE INTO market_identity_archive VALUES(?,?,?,?,?,?,?)',
                        (identity,symbol,old_key,new_key,now.isoformat(),table,payload))
                con.execute('DELETE FROM '+table+" WHERE symbol=? AND source LIKE 'upstox-live%'",(symbol,))
            con.execute('UPDATE universe SET upstox_instrument_key=? WHERE symbol=?',(new_key,symbol))
        for row in normalized:
            columns=list(row)
            con.execute('INSERT INTO universe ('+','.join(columns)+') VALUES ('+','.join('?' for _ in columns)+')',
                        [row[k] for k in columns])
    return result


def refresh(db,held,*,client=None,now=None,members=None):
    from .sleeves.reference import snapshot
    now=now or datetime.now(timezone.utc)
    if members is None:members,_=snapshot(now)
    if not members:raise ValueError('Current verified Nifty membership required')
    http=client or httpx.Client(timeout=20,headers=HEADERS,follow_redirects=False)
    try:
        archive=Path(db.path).parent/'market-identity-source'
        n,ns=read_source(http,NSE_URL,archive,now)
        b,bs=read_source(http,JSON_URL,archive,now,compressed=True)
        nse=[{str(k).strip():str(v or '').strip() for k,v in r.items()} for r in csv.DictReader(io.StringIO(n))]
        broker=json.loads(b)
        if len(nse)<1000 or not isinstance(broker,list) or len(broker)<1000:
            raise ValueError('Incomplete instrument masters')
        result=reconcile(db,nse,broker,members,held,datetime.now(timezone.utc) if client is None else now)
        result['sources']=[ns,bs]
        db.set_state('market_identity_refresh',result)
        return result
    finally:
        if client is None:http.close()
