"""Forward observations of issued research plans. Never a broker or paper book.

Entry-zone touches are hypothetical scenarios, NOT fulfilled entry confirmation.
Sampled quotes cannot establish every intervening price crossing or a trade edge.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .store import timestamp
from .. import costs
from ..market_regions import INDIA_TRADING_HOLIDAYS, market_session_for_region

IST = ZoneInfo('Asia/Kolkata')
VERSION = 'conditional-pullback-v1'
TABLES = {'publications', 'states', 'events', 'samples', 'health', 'benchmarks', 'coverage', 'assessments', 'assessment_events'}
TERMINAL = {'INVALIDATED', 'EXPIRED_UNTOUCHED', 'STOPPED', 'TARGET_3', 'TIME_EXIT'}
SCHEMA = '''
CREATE TABLE IF NOT EXISTS publications(
 id INTEGER PRIMARY KEY,user_id INTEGER NOT NULL,fingerprint TEXT NOT NULL,
 symbol TEXT NOT NULL,issued_at TEXT NOT NULL,payload TEXT NOT NULL,
 UNIQUE(user_id,fingerprint));
CREATE INDEX IF NOT EXISTS publication_owner ON publications(user_id,id);
CREATE TABLE IF NOT EXISTS states(publication_id INTEGER PRIMARY KEY,payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events(
 publication_id INTEGER NOT NULL,kind TEXT NOT NULL,observed_at TEXT NOT NULL,
 price REAL,payload TEXT NOT NULL,PRIMARY KEY(publication_id,kind));
CREATE TABLE IF NOT EXISTS samples(
 symbol TEXT NOT NULL,quote_at TEXT NOT NULL,source TEXT NOT NULL,
 price REAL NOT NULL,captured_at TEXT NOT NULL,PRIMARY KEY(symbol,quote_at,source));
CREATE TABLE IF NOT EXISTS health(id INTEGER PRIMARY KEY CHECK(id=1),payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS benchmarks(
 symbol TEXT NOT NULL,quote_at TEXT NOT NULL,source TEXT NOT NULL,
 price REAL NOT NULL,captured_at TEXT NOT NULL,PRIMARY KEY(symbol,quote_at,source));
CREATE TABLE IF NOT EXISTS coverage(
 publication_id INTEGER NOT NULL,start_at TEXT NOT NULL,end_at TEXT NOT NULL,
 seconds REAL NOT NULL,captured_at TEXT NOT NULL,
 PRIMARY KEY(publication_id,start_at,end_at));
CREATE TABLE IF NOT EXISTS assessments(publication_id INTEGER PRIMARY KEY,payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS assessment_events(
 publication_id INTEGER NOT NULL,kind TEXT NOT NULL,observed_at TEXT NOT NULL,
 payload TEXT NOT NULL,PRIMARY KEY(publication_id,kind,observed_at));
CREATE TRIGGER IF NOT EXISTS immutable_assessment_update BEFORE UPDATE ON assessment_events
 BEGIN SELECT RAISE(ABORT,'immutable research assessments'); END;
CREATE TRIGGER IF NOT EXISTS immutable_assessment_delete BEFORE DELETE ON assessment_events
 BEGIN SELECT RAISE(ABORT,'immutable research assessments'); END;
CREATE TRIGGER IF NOT EXISTS immutable_publication_update BEFORE UPDATE ON publications
 BEGIN SELECT RAISE(ABORT,'immutable idea publication'); END;
CREATE TRIGGER IF NOT EXISTS immutable_publication_delete BEFORE DELETE ON publications
 BEGIN SELECT RAISE(ABORT,'immutable idea publication'); END;
'''


def default_path(main_db):
    return os.getenv('IDEA_TRACKING_DB', str(Path(main_db).parent / 'idea_tracking.db'))


def _json(data):
    return json.dumps(data, allow_nan=False, sort_keys=True, separators=(',', ':'))


def connect(path):
    """Refuse renamed books before any DDL; readers use mode=ro separately."""
    path = Path(path).resolve()
    if path.name in {'v2_paper.db', 'trading_agent.db', 'screening.db'}:
        raise ValueError('a dedicated idea tracking database is required')
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=5)
    found = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
    if found - TABLES:
        con.close()
        raise ValueError('refusing a database containing non-tracking tables')
    con.execute('PRAGMA journal_mode=WAL')
    con.executescript(SCHEMA)
    return con


def cost_profile():
    # Freeze all charges and slippage so later configuration cannot restate history.
    return dict(flat=costs.BROKERAGE_FLAT,stt=costs.STT_DELIVERY,
                exchange=costs.EXCHANGE_TXN,ipft=costs.IPFT_TURNOVER,
                sebi=costs.SEBI_TURNOVER,stamp=costs.STAMP_DELIVERY,
                dp=costs.DP_CHARGE,gst=costs.GST,slippage=0.002)


def net(plan, entry, exit_price):
    c = plan['cost_profile']; qty = plan['qty']
    buy = entry * qty * (1+c['slippage'])
    sell = exit_price * qty * (1-c['slippage'])
    brokerage = 2*c['flat']; exchange = (buy+sell)*c['exchange']
    ipft = (buy+sell)*c['ipft']; sebi = (buy+sell)*c['sebi']
    charge = brokerage+(buy+sell)*c['stt']+exchange+ipft+sebi+buy*c['stamp']+c['dp']
    charge += (brokerage+exchange+ipft+c['dp'])*c['gst']
    return sell-buy-charge


def fingerprint(plan):
    fields = {k:plan[k] for k in ('symbol','price_asof','entry_low','entry_high','stop','t1','t2','t3','qty')}
    fields['version'] = plan.get('model_version', VERSION)
    return hashlib.sha256(_json(fields).encode()).hexdigest()


def _validate(plan):
    values = [float(plan[k]) for k in ('stop','entry_low','entry_high','t1','t2','t3')]
    if not all(math.isfinite(v) and v>0 for v in values):
        raise ValueError('finite positive plan levels required')
    if not values[0]<values[1]<=values[2]<values[3]<values[4]<values[5]:
        raise ValueError('ordered long plan levels required')
    qty = float(plan['qty'])
    if not math.isfinite(qty) or qty<=0 or qty!=int(qty) or not plan.get('symbol'):
        raise ValueError('identified plan with positive whole-share size required')


def publish(path, user_id, plans, issued_at=None, now=None):
    """First API delivery creates a version; unchanged refresh never rewrites it."""
    now = now or datetime.now(timezone.utc)
    issued = timestamp(issued_at) if issued_at else now
    if issued>now or int(user_id)<=0:
        raise ValueError('non-future publication and identified user required')
    con = connect(path); ids = {}
    try:
        with con:
            for original in plans:
                _validate(original)
                plan = dict(original,model_version=original.get('model_version',VERSION),
                            cost_profile=cost_profile(),expiry_sessions=40)
                key = fingerprint(plan)
                con.execute('INSERT OR IGNORE INTO publications(user_id,fingerprint,symbol,issued_at,payload) VALUES(?,?,?,?,?)',
                            (int(user_id),key,plan['symbol'],issued.isoformat(),_json(plan)))
                pid = con.execute('SELECT id FROM publications WHERE user_id=? AND fingerprint=?', (int(user_id),key)).fetchone()[0]
                con.execute('INSERT OR IGNORE INTO states VALUES(?,?)', (pid,_json(dict(status='WAITING',samples=0,gaps=0))))
                ids[key] = pid
    finally:
        con.close()
    return ids


def publication(path, user_id, publication_id):
    """Owned immutable plan, read without changing its observations/history."""
    con = sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True)
    try:
        row = con.execute('SELECT id,fingerprint,issued_at,payload FROM publications WHERE id=? AND user_id=?',
                          (int(publication_id),int(user_id))).fetchone()
        if not row:
            return None
        return dict(id=row[0],fingerprint=row[1],issued_at=row[2],plan=json.loads(row[3]),
                    execution_approved=False,reason='Research plan requires independent model promotion before execution')
    finally:
        con.close()


def sessions_between(start, end):
    a, b = timestamp(start).astimezone(IST).date(), timestamp(end).astimezone(IST).date()
    count = 0
    while a<b:
        a += timedelta(days=1)
        if a.weekday()<5 and a.isoformat() not in INDIA_TRADING_HOLIDAYS:
            count += 1
    return count


def _event(con, pid, kind, at, price=None, **data):
    con.execute('INSERT OR IGNORE INTO events VALUES(?,?,?,?,?)', (pid,kind,at,price,_json(data)))


def _advance(con, pid, issued_at, plan, state, q, captured):
    if state['status'] in TERMINAL:
        return False
    if not q:
        # An untouched plan may expire without a quote; no price or P&L invented.
        if state['status']=='WAITING' and sessions_between(issued_at,captured)>=plan['expiry_sessions']:
            state['status']='EXPIRED_UNTOUCHED'
            _event(con,pid,'EXPIRED_UNTOUCHED',captured)
            return True
        return False
    at, price = q['ts'], q['price']
    if timestamp(at)<timestamp(issued_at) or (state.get('last_at') and timestamp(at)<=timestamp(state['last_at'])):
        return False
    previous = timestamp(state.get('last_at') or issued_at)
    current = timestamp(at)
    # Count only regular-session time, including wholly missed intervening days.
    day = previous.astimezone(IST).date(); end = current.astimezone(IST).date()
    covered_seconds = 0
    while day<=end:
        if day.weekday()<5 and day.isoformat() not in INDIA_TRADING_HOLIDAYS:
            opened=datetime.combine(day,datetime.min.time(),IST).replace(hour=9,minute=15)
            closed=opened.replace(hour=15,minute=30)
            covered_seconds += max(0,(min(current,closed)-max(previous,opened)).total_seconds())
        day += timedelta(days=1)
    if covered_seconds>120:
        state['gaps'] += 1
        con.execute('INSERT OR IGNORE INTO coverage VALUES(?,?,?,?,?)',
                    (pid,previous.isoformat(),current.isoformat(),covered_seconds,captured))
    if not state.get('last_at'):
        state.update(first_at=at,first_price=price)
    state.update(last_at=at,last_price=price,samples=state['samples']+1,
                 low=min(price,state.get('low',price)),high=max(price,state.get('high',price)))
    if state['status']=='WAITING':
        if sessions_between(issued_at,at)>=plan['expiry_sessions']:
            state['status']='EXPIRED_UNTOUCHED';_event(con,pid,state['status'],at)
        elif price<=plan['stop']:
            state['status']='INVALIDATED';_event(con,pid,'INVALIDATED',at,price)
        elif plan['entry_low']<=price<=plan['entry_high']:
            state.update(status='ZONE_TOUCHED',entry_at=at,entry_price=price,
                         risk=-net(plan,price,plan['stop']))
            _event(con,pid,'ZONE_TOUCHED',at,price,scenario='hypothetical; entry confirmation not fulfilled')
    if state.get('entry_at'):
        for i in (1,2,3):
            if price>=plan['t'+str(i)] and not state.get('t'+str(i)+'_at'):
                state['t'+str(i)+'_at']=at
                _event(con,pid,'TARGET_'+str(i)+'_TOUCH',at,price)
        reason = ('STOPPED' if price<=plan['stop'] else 'TARGET_3' if price>=plan['t3'] else
                  'TIME_EXIT' if sessions_between(state['entry_at'],at)>=plan['expiry_sessions'] else None)
        if reason:
            state.update(status=reason,exit_at=at,exit_price=price,net=round(net(plan,state['entry_price'],price),2))
            state['r']=round(state['net']/state['risk'],4)
            _event(con,pid,reason,at,price,net=state['net'],r=state['r'])
    return True


def observe(path, quotes, captured_at=None):
    """Only identified, fresh, post-publication regular-session quote samples."""
    captured = timestamp(captured_at) if captured_at else datetime.now(timezone.utc)
    con = connect(path); fresh = {}
    try:
        with con:
            for symbol,q in quotes.items():
                try:
                    at=timestamp(q['ts']); price=float(q['price'])
                    if q.get('source','upstox-live')!='upstox-live' or not math.isfinite(price) or price<=0:
                        continue
                    if not 0<=(captured-at).total_seconds()<=120 or not market_session_for_region('IN',at)['is_open']:
                        continue
                except (KeyError,TypeError,ValueError):
                    continue
                fresh[symbol]=dict(price=price,ts=at.isoformat())
                con.execute('INSERT OR IGNORE INTO samples VALUES(?,?,?,?,?)', (symbol,at.isoformat(),'upstox-live',price,captured.isoformat()))
            rows=con.execute('SELECT p.id,p.issued_at,p.payload,s.payload FROM publications p JOIN states s ON s.publication_id=p.id').fetchall()
            active=set(); changed=0
            for pid,issued,encoded,stored in rows:
                plan=json.loads(encoded); state=json.loads(stored)
                if state['status'] not in TERMINAL:
                    active.add(plan['symbol'])
                if _advance(con,pid,issued,plan,state,fresh.get(plan['symbol']),captured.isoformat()):
                    con.execute('UPDATE states SET payload=? WHERE publication_id=?', (_json(state),pid));changed+=1
            health=dict(polled_at=captured.isoformat(),market_open=market_session_for_region('IN',captured)['is_open'],
                        active_symbols=len(active),fresh_symbols=len(active & fresh.keys()),missing_symbols=sorted(active-fresh.keys()),changed=changed)
            con.execute('INSERT OR REPLACE INTO health VALUES(1,?)',(_json(health),))
    finally:
        con.close()
    return health


def symbols(path):
    con=sqlite3.connect(f'file:{Path(path).resolve()}?mode=ro',uri=True)
    try:
        return [r[0] for r in con.execute('SELECT DISTINCT p.symbol FROM publications p JOIN states s ON s.publication_id=p.id WHERE json_extract(s.payload,\'$.status\') NOT IN (?,?,?,?,?)',tuple(sorted(TERMINAL)))]
    finally:
        con.close()


def observe_benchmarks(path, quotes, captured_at=None):
    """Direct index quotes stay in the research DB, never the trading universe."""
    captured=timestamp(captured_at) if captured_at else datetime.now(timezone.utc)
    accepted=[]
    for symbol,q in quotes.items():
        try:
            at=timestamp(q['ts']);price=float(q['price'])
            if (symbol not in ('NIFTY','BANKNIFTY') or q.get('source')!='upstox-live'
                or not math.isfinite(price) or price<=0
                or not 0<=(captured-at).total_seconds()<=120
                or not market_session_for_region('IN',at)['is_open']):
                continue
            accepted.append((symbol,at.isoformat(),'upstox-nse-index',price,captured.isoformat()))
        except (KeyError,TypeError,ValueError):
            continue
    if not accepted:return 0
    con=connect(path)
    try:
        with con:con.executemany('INSERT OR IGNORE INTO benchmarks VALUES(?,?,?,?,?)',accepted)
    finally:con.close()
    return len(accepted)


def matched_benchmarks(con, start, end):
    """Never substitute parity bars or unmatched dates for index observations.

    Endpoint skew is disclosed and capped at 30 seconds; this is sampled
    relative strength, not an exact-time official-close comparison.
    """
    result={}
    for symbol in ('NIFTY','BANKNIFTY'):
        points=[]
        for at in (start,end):
            try:
                center=timestamp(at)
                row=con.execute('SELECT quote_at,price FROM benchmarks WHERE symbol=? '
                    'AND quote_at>=? AND quote_at<=? '
                    'ORDER BY ABS(julianday(quote_at)-julianday(?)) LIMIT 1',
                    (symbol,(center-timedelta(seconds=30)).isoformat(),
                     (center+timedelta(seconds=30)).isoformat(),at)).fetchone()
            except sqlite3.OperationalError:row=None
            points.append(row)
        if not all(points) or timestamp(points[1][0])<=timestamp(points[0][0]):
            result[symbol]=dict(available=False,reason='Matching timestamped index observations unavailable')
        else:
            result[symbol]=dict(available=True,start_at=points[0][0],end_at=points[1][0],
                move_pct=round((points[1][1]/points[0][1]-1)*100,4),
                start_skew_seconds=round(abs((timestamp(points[0][0])-timestamp(start)).total_seconds()),3),
                end_skew_seconds=round(abs((timestamp(points[1][0])-timestamp(end)).total_seconds()),3),
                source='upstox-nse-index',max_endpoint_skew_seconds=30)
    return result


def report(path,user_id,now=None,limit=100,offset=0):
    """Private cohort report; summary includes ALL versions, not just latest wins."""
    now=now or datetime.now(timezone.utc)
    try:
        con=sqlite3.connect(f'file:{Path(path).resolve()}?mode=ro',uri=True,timeout=5)
        try:
            rows=con.execute('SELECT p.id,p.fingerprint,p.symbol,p.issued_at,p.payload,s.payload FROM publications p JOIN states s ON s.publication_id=p.id WHERE p.user_id=? ORDER BY p.id DESC',(int(user_id),)).fetchall()
            health=con.execute('SELECT payload FROM health WHERE id=1').fetchone()
            events=con.execute('SELECT e.publication_id,e.kind,e.observed_at,e.price,e.payload FROM events e JOIN publications p ON p.id=e.publication_id WHERE p.user_id=? ORDER BY e.observed_at',(int(user_id),)).fetchall()
            try:
                gaps=con.execute('SELECT c.publication_id,c.start_at,c.end_at,c.seconds,c.captured_at '
                    'FROM coverage c JOIN publications p ON p.id=c.publication_id '
                    'WHERE p.user_id=? ORDER BY c.end_at',(int(user_id),)).fetchall()
            except sqlite3.OperationalError:gaps=[] # pre-migration read remains valid
            try:
                assessments=dict(con.execute('SELECT a.publication_id,a.payload FROM assessments a '
                    'JOIN publications p ON p.id=a.publication_id WHERE p.user_id=?',(int(user_id),)).fetchall())
            except sqlite3.OperationalError:assessments={}
            matches={}
            for pid,key,symbol,issued,encoded,stored in rows:
                state=json.loads(stored)
                if state.get('first_at') and state.get('last_at'):
                    matches[pid]=matched_benchmarks(con,state['first_at'],state['last_at'])
        finally:
            con.close()
    except (sqlite3.Error,OSError):
        return dict(status='unavailable',rows=[],summary={},note='Forward tracker unavailable; no performance claim')
    result=[]; summary=dict(published=len(rows),symbols=len({r[2] for r in rows}),waiting=0,zone_touched=0,resolved=0,wins=0,invalidated=0,expired=0,t1=0,t2=0,t3=0,stopped=0)
    timeline={}
    for pid,kind,at,price,payload in events:
        timeline.setdefault(pid,[]).append(dict(kind=kind,at=at,price=price,**json.loads(payload)))
    for pid,key,symbol,issued,encoded,stored in rows:
        plan=json.loads(encoded); state=json.loads(stored)
        row=dict(id=pid,fingerprint=key,symbol=symbol,issued_at=issued,plan=plan,**state,events=timeline.get(pid,[]))
        row['coverage_gaps']=[dict(start_at=a,end_at=b,seconds=seconds,captured_at=capture)
            for gap_pid,a,b,seconds,capture in gaps if gap_pid==pid][-10:]
        row['benchmarks']=matches.get(pid,{})
        row['confirmation']=json.loads(assessments[pid]) if pid in assessments else None
        if row['confirmation']:
            assessment=row['confirmation']
            assessment['fresh']=0<=(now-timestamp(assessment['checked_at'])).total_seconds()<=120
            assessment['assessed_eligible']=assessment['eligible']
            if not assessment['fresh']:assessment['eligible']=False
        for event in row['events']:
            if event['price'] is None and event['kind']==state['status'] and state.get('exit_price'):
                event['price_recorded_in_state']=state['exit_price']
                event['historical_event_price_missing']=True
        if state.get('first_price'):
            row['observed_move_pct']=round((state['last_price']/state['first_price']-1)*100,2)
            for match in row['benchmarks'].values():
                if match['available']:
                    match['stock_minus_index_pp']=round(row['observed_move_pct']-match['move_pct'],4)
            age=(now-timestamp(state['last_at'])).total_seconds()
            row['quote_fresh']=0<=age<=120
        if state.get('entry_at'):
            summary['zone_touched']+=1
            row['scenario_net']=state.get('net',round(net(plan,state['entry_price'],state['last_price']),2))
            row['scenario_r']=round(row['scenario_net']/state['risk'],4)
        for i in (1,2,3): summary['t'+str(i)]+=int(bool(state.get('t'+str(i)+'_at')))
        summary['waiting']+=int(state['status']=='WAITING')
        summary['invalidated']+=int(state['status']=='INVALIDATED')
        summary['expired']+=int(state['status']=='EXPIRED_UNTOUCHED')
        summary['stopped']+=int(state['status']=='STOPPED')
        if state.get('exit_at'):
            summary['resolved']+=1;summary['wins']+=int(state['net']>0)
        result.append(row)
    closed=[r for r in result if r.get('exit_at')]
    summary['scenario_win_pct']=round(100*summary['wins']/len(closed),2) if closed else None
    summary['scenario_avg_r']=round(sum(r['r'] for r in closed)/len(closed),4) if closed else None
    h=json.loads(health[0]) if health else {}
    # A subscriber never receives another subscriber's monitoring coverage.
    active=[r for r in result if r['status'] not in TERMINAL]
    own_symbols={r['symbol'] for r in active}
    missing={r['symbol'] for r in active if not r.get('quote_fresh')}
    h.update(active_symbols=len(own_symbols),fresh_symbols=len(own_symbols-missing),missing_symbols=sorted(missing))
    h.pop('changed',None)
    h['stale']=not h.get('polled_at') or not 0<=(now-timestamp(h['polled_at'])).total_seconds()<=120
    return dict(status='ok',summary=summary,health=h,rows=result[offset:offset+limit],offset=offset,limit=limit,
                note='Forward quote observations. Zone-touch scenarios are hypothetical, after frozen delivery fees and 0.2% slippage each way. No entry confirmation, portfolio allocation or actual trade is implied. Quotes can miss crossings between samples; revisions remain separate. No earlier price history is backfilled.')


def assessment_history(path, user_id, publication_id, limit=50, offset=0):
    """Owned immutable predicates; read-only and separate from fill evidence."""
    if not 1 <= limit <= 200 or offset < 0:
        raise ValueError('Invalid assessment page')
    con=sqlite3.connect(f'file:{Path(path).resolve()}?mode=ro',uri=True,timeout=5)
    try:
        owner=con.execute('SELECT symbol FROM publications WHERE id=? AND user_id=?',
                          (publication_id,int(user_id))).fetchone()
        if not owner:return None
        count=con.execute('SELECT COUNT(*) FROM assessment_events WHERE publication_id=?',
                          (publication_id,)).fetchone()[0]
        rows=con.execute('SELECT kind,observed_at,payload FROM assessment_events '
                         'WHERE publication_id=? ORDER BY observed_at DESC,kind LIMIT ? OFFSET ?',
                         (publication_id,limit,offset)).fetchall()
        return dict(publication_id=publication_id,symbol=owner[0],total=count,limit=limit,offset=offset,
                    events=[dict(kind=k,observed_at=at,assessment=json.loads(payload)) for k,at,payload in rows],
                    execution_approved=False,note='Dated research checks; neither approval nor an actual fill')
    finally:con.close()


def import_bootstrap(path,seed_path,quotes_path):
    """Import the live capture started while building this feature, idempotently."""
    seed=json.loads(Path(seed_path).read_text())
    publish(path,seed['user_id'],seed['plans']['ideas'],issued_at=seed['issued_at'])
    count=0
    for line in Path(quotes_path).read_text().splitlines():
        try:
            sample=json.loads(line)
        except json.JSONDecodeError:
            continue # A currently-being-written final line is retried next run.
        if timestamp(sample['captured_at'])>datetime.now(timezone.utc):
            raise ValueError('future bootstrap capture')
        observe(path,sample['quotes'],sample['captured_at']);count+=1
    return count
