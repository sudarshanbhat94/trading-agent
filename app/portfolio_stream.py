"""Account-bound, durable Upstox portfolio observations.

Only the authenticated outbound collector calls ingest; there is no public
webhook. Unsequenced notifications never arm a stop or release a sell right.
They can retain an exact child identity omitted by the completed-GTT REST API.
Actual journal/order/trade/inventory reconciliation remains authoritative.
"""
import hashlib
import json
import math
import time
from uuid import uuid4

from .account_safety import atomic


def ensure_schema(con):
    con.execute('''CREATE TABLE IF NOT EXISTS broker_stream_accounts(
      user_id INTEGER PRIMARY KEY,account_hash TEXT NOT NULL UNIQUE)''')
    con.execute('''CREATE TABLE IF NOT EXISTS broker_stream_connections(
      id TEXT PRIMARY KEY,user_id INTEGER NOT NULL,account_hash TEXT NOT NULL,
      generation INTEGER NOT NULL,started_at REAL NOT NULL,expires_at REAL NOT NULL,
      state TEXT NOT NULL,UNIQUE(user_id,generation))''')
    con.execute('''CREATE TABLE IF NOT EXISTS broker_stream_events(
      id INTEGER PRIMARY KEY,connection_id TEXT NOT NULL,user_id INTEGER NOT NULL,
      kind TEXT NOT NULL,payload TEXT NOT NULL,received_at REAL NOT NULL,fingerprint TEXT NOT NULL,
      UNIQUE(connection_id,kind,fingerprint))''')
    con.execute('CREATE INDEX IF NOT EXISTS stream_owned_events ON broker_stream_events(user_id,kind,id)')
    for table in ('broker_stream_accounts','broker_stream_events'):
        for action in ('UPDATE','DELETE'):
            con.execute(f"CREATE TRIGGER IF NOT EXISTS {table}_no_{action.lower()} BEFORE {action} ON {table} "
                        "BEGIN SELECT RAISE(ABORT,'immutable owned stream evidence'); END")


def account_hash(account):
    if not isinstance(account,str) or not account.strip():raise ValueError('Verified broker profile identity required')
    return hashlib.sha256(('upstox:'+account.strip()).encode()).hexdigest()


def open_connection(con,uid,account,*,now=None,ttl=90):
    now=time.time() if now is None else now
    if type(uid) is not int or uid<1:raise ValueError('Invalid stream owner')
    digest=account_hash(account)
    with atomic(con):
        owned=con.execute('SELECT account_hash FROM broker_stream_accounts WHERE user_id=?',(uid,)).fetchone()
        other=con.execute('SELECT user_id FROM broker_stream_accounts WHERE account_hash=?',(digest,)).fetchone()
        if owned and owned[0]!=digest or other and other[0]!=uid:
            raise ValueError('Broker identity changed or is already bound to another account; reconcile ownership')
        active=con.execute('SELECT generation,state,expires_at FROM broker_stream_connections WHERE user_id=? '
                           'ORDER BY generation DESC LIMIT 1',(uid,)).fetchone()
        if active and active[1]=='open' and active[2]>now:raise ValueError('Account stream already leased')
        con.execute('INSERT OR IGNORE INTO broker_stream_accounts VALUES(?,?)',(uid,digest))
        identity=uuid4().hex;generation=active[0]+1 if active else 1
        con.execute('INSERT INTO broker_stream_connections VALUES(?,?,?,?,?,?,?)',
                    (identity,uid,digest,generation,now,now+ttl,'open'))
        _append(con,identity,uid,'connection',dict(generation=generation,reconnect=bool(active)),now)
        return identity


def _current(con,connection,now):
    row=con.execute('SELECT user_id,account_hash,generation,expires_at,state,started_at FROM broker_stream_connections WHERE id=?',
                    (connection,)).fetchone()
    latest=con.execute('SELECT MAX(generation) FROM broker_stream_connections WHERE user_id=?',(row[0],)).fetchone() if row else None
    bound=con.execute('SELECT account_hash FROM broker_stream_accounts WHERE user_id=?',(row[0],)).fetchone() if row else None
    if not row or row[4]!='open' or row[3]<=now or row[5]>now or latest[0]!=row[2] or not bound or bound[0]!=row[1]:
        raise ValueError('Expired/replaced account stream or conflicting owner')
    return row[:5]


def heartbeat(con,connection,*,now=None):
    now=time.time() if now is None else now
    with atomic(con):
        _current(con,connection,now)
        con.execute('UPDATE broker_stream_connections SET expires_at=? WHERE id=?',(now+90,connection))


def close_connection(con,connection,*,now=None):
    now=time.time() if now is None else now
    with atomic(con):
        row=con.execute('SELECT user_id,state FROM broker_stream_connections WHERE id=?',(connection,)).fetchone()
        if row and row[1]=='open':
            con.execute("UPDATE broker_stream_connections SET state='closed',expires_at=? WHERE id=?",(now,connection))
            _append(con,connection,row[0],'gap',dict(reason='Connection closed; polling reconciliation required'),now)


def _append(con,connection,uid,kind,data,now):
    payload=json.dumps(data,sort_keys=True,separators=(',',':'),allow_nan=False)
    digest=hashlib.sha256(payload.encode()).hexdigest()
    con.execute('INSERT OR IGNORE INTO broker_stream_events(connection_id,user_id,kind,payload,received_at,fingerprint) '
                'VALUES(?,?,?,?,?,?)',(connection,uid,kind,payload,now,digest))


def ingest(con,connection,data,*,now=None):
    """Receive from the verified owner socket. Retain only necessary fields."""
    now=time.time() if now is None else now
    if not isinstance(data,dict):raise ValueError('Invalid portfolio event')
    kind=data.get('update_type')
    if kind not in {'order','gtt_order'}:return False
    with atomic(con):
        uid,digest,*_=_current(con,connection,now)
        for key in ('user_id','userId','placed_by'):
            if data.get(key) is not None and account_hash(data[key])!=digest:
                raise ValueError('Portfolio event broker identity conflicts with authenticated owner')
        if kind=='order':
            fields=('order_id','tag','instrument_token','instrument_key','transaction_type','product',
                    'quantity','filled_quantity','average_price','status')
            payload={key:data[key] for key in fields if key in data}
            if not isinstance(payload.get('order_id'),str) or not payload['order_id']:raise ValueError('Missing order identity')
            if type(payload.get('quantity')) is not int or payload['quantity']<1 or \
                    type(payload.get('filled_quantity')) is not int or not 0<=payload['filled_quantity']<=payload['quantity'] or \
                    payload.get('transaction_type') not in {'BUY','SELL'} or payload.get('product') not in {'D','I'}:
                raise ValueError('Invalid owned order quantities/side/product')
            average=payload.get('average_price')
            if isinstance(average,bool) or not isinstance(average,(int,float)) or not math.isfinite(average) or \
                    average<0 or payload['filled_quantity'] and average<=0:raise ValueError('Invalid order price evidence')
        else:
            fields=('type','instrument_token','product','quantity','gtt_order_id','expires_at','created_at')
            payload={key:data[key] for key in fields if key in data}
            if not isinstance(payload.get('gtt_order_id'),str) or not payload['gtt_order_id'].startswith('GTT-'):
                raise ValueError('Missing native identity')
            if type(payload.get('quantity')) is not int or payload['quantity']<1:raise ValueError('Invalid native quantity')
            rules=data.get('rules')
            if not isinstance(rules,list) or not 0<len(rules)<=3 or any(not isinstance(r,dict) for r in rules):
                raise ValueError('Invalid native rules')
            keep=('strategy','status','trigger_type','trigger_price','transaction_type','order_id')
            payload['rules']=[{key:r[key] for key in keep if key in r} for r in rules]
        _append(con,connection,uid,kind,payload,now)
    return True


def child_evidence(con,uid,row,*,now=None):
    """Archived identity is useful across gaps; archived status is not current.

    Multiple child identities are a conflict, not a choice of the latest
    message. CANCELLED/SCHEDULED notifications never free or arm inventory.
    """
    from .protection import _native_evidence
    from .protection_amendments import latest
    now=time.time() if now is None else now
    amendment=latest(con,uid,row['entry_id'])
    quantities={row['coverage_quantity']}
    if amendment and amendment['native_id']==row['native_id']:quantities.add(amendment['to_quantity'])
    found={}
    records=con.execute("SELECT payload FROM broker_stream_events WHERE user_id=? AND kind='gtt_order' "
                        "AND json_extract(payload,'$.gtt_order_id')=? AND received_at<=? ORDER BY id",
                        (uid,row['native_id'],now))
    for payload, in records:
        data=json.loads(payload)
        if data.get('quantity') not in quantities:continue
        try:rule,_=_native_evidence(row,[data],quantity=data['quantity'])
        except (ValueError,TypeError,KeyError):continue
        if rule['status'] not in {'TRIGGERED','OPEN','COMPLETED'} or not rule.get('order_id'):continue
        identity=(rule['order_id'],data['quantity'])
        found[identity]=data
    if len(found)>1:raise ValueError('Conflicting owned native child observations')
    return list(found.values())


def order_updates(con,uid,*,now=None):
    now=time.time() if now is None else now
    records=con.execute("SELECT payload FROM broker_stream_events WHERE user_id=? AND kind='order' "
                        "AND received_at<=? ORDER BY id DESC LIMIT 1000",(uid,now)).fetchall()
    return [json.loads(r[0]) for r in reversed(records)]


def report(con,uid,*,now=None):
    now=time.time() if now is None else now
    row=con.execute('SELECT generation,expires_at,state,id FROM broker_stream_connections WHERE user_id=? '
                    'ORDER BY generation DESC LIMIT 1',(uid,)).fetchone()
    counts=dict(con.execute('SELECT kind,COUNT(*) FROM broker_stream_events WHERE user_id=? GROUP BY kind',(uid,)))
    connected=False
    if row:
        try:_current(con,row[3],now);connected=True
        except ValueError:pass
    return dict(connected=connected,generation=row[0] if row else None,
                event_counts=counts,notification_status_authoritative=False,authenticated_outbound_only=True)
