"""Actual owned trade IDs and balanced cash/settlement postings.

Order acknowledgements and averaged order snapshots are never accounting
events. Cash below is a trade-date receivable/payable, not available margin.
Fees and settlement remain unknown until separately sourced evidence arrives.
Corrections append reversals; they cannot silently replace a broker trade.
"""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json

from .account_safety import atomic
from .paper_ledger import minor


def ensure_schema(con):
    con.execute('''CREATE TABLE IF NOT EXISTS broker_ledger_events(
      id INTEGER PRIMARY KEY,user_id INTEGER NOT NULL,broker TEXT NOT NULL,
      event_key TEXT NOT NULL,kind TEXT NOT NULL,order_id TEXT,trade_id TEXT,
      instrument_key TEXT,product TEXT,side TEXT,quantity INTEGER,price TEXT,
      currency TEXT NOT NULL,occurred_at TEXT NOT NULL,observed_at TEXT NOT NULL,
      source TEXT NOT NULL,payload TEXT NOT NULL,fingerprint TEXT NOT NULL,
      UNIQUE(user_id,broker,event_key))''')
    con.execute('''CREATE TABLE IF NOT EXISTS broker_ledger_postings(
      event_id INTEGER NOT NULL,account TEXT NOT NULL,amount_minor INTEGER NOT NULL,
      PRIMARY KEY(event_id,account),FOREIGN KEY(event_id) REFERENCES broker_ledger_events(id))''')
    for table in ('broker_ledger_events','broker_ledger_postings'):
        for action in ('UPDATE','DELETE'):
            con.execute(f"CREATE TRIGGER IF NOT EXISTS immutable_{table}_{action.lower()} BEFORE {action} ON {table} "
                        "BEGIN SELECT RAISE(ABORT,'immutable broker accounting'); END")


def _time(value):
    if not isinstance(value,str):raise ValueError('Sourced aware trade timestamp required')
    at=datetime.fromisoformat(value.replace('Z','+00:00'))
    if at.tzinfo is None:raise ValueError('Sourced aware trade timestamp required')
    return at


def _amount(value):
    try:
        if isinstance(value,bool):raise ValueError('Invalid price')
        value=Decimal(str(value))
        if not value.is_finite() or value<=0:raise ValueError('Invalid price')
        return value
    except InvalidOperation as exc:raise ValueError('Invalid price') from exc


def _post(con,uid,broker,key,kind,data,postings,now):
    if not con.in_transaction:raise RuntimeError('Accounting requires serialized transaction')
    if type(uid) is not int or uid<1 or not key or not broker or not data.get('source'):
        raise ValueError('Owned and sourced accounting identity required')
    if len(postings)<2 or any(type(v) is not int for v in postings.values()) or sum(postings.values()):
        raise ValueError('Accounting must balance in minor currency units')
    payload=json.dumps(data,sort_keys=True,separators=(',',':'),allow_nan=False)
    digest=hashlib.sha256(json.dumps([kind,payload,sorted(postings.items())]).encode()).hexdigest()
    prior=con.execute('SELECT id,fingerprint FROM broker_ledger_events WHERE user_id=? AND broker=? AND event_key=?',
                      (uid,broker,key)).fetchone()
    if prior:
        if prior[1]!=digest:raise ValueError('Broker accounting identity conflicts; a sourced correction is required')
        return prior[0],False
    cur=con.execute('''INSERT INTO broker_ledger_events(user_id,broker,event_key,kind,order_id,trade_id,
      instrument_key,product,side,quantity,price,currency,occurred_at,observed_at,source,payload,fingerprint)
      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
      (uid,broker,key,kind,data.get('order_id'),data.get('trade_id'),data.get('instrument_key'),
       data.get('product'),data.get('side'),data.get('quantity'),data.get('price'),data['currency'],
       data['occurred_at'],now.isoformat(),data['source'],payload,digest))
    con.executemany('INSERT INTO broker_ledger_postings VALUES(?,?,?)',
                    [(cur.lastrowid,k,v) for k,v in sorted(postings.items())])
    return cur.lastrowid,True


def ingest_trades(con,uid,trades,*,broker='upstox',now=None):
    """Whole batch is atomic. External or conflicting evidence is refused.

    A trade is bound to one owned intent by order ID and contract/product/side.
    Partial executions accumulate by exchange trade ID, never by average-price
    differences. Unknown timestamps/prices are not invented from observation.
    """
    now=now or datetime.now(timezone.utc)
    if not isinstance(trades,list) or now.tzinfo is None:raise ValueError('Complete dated trade evidence required')
    ensure_schema(con)
    with atomic(con):
        from .worker_fencing import require_current
        require_current(con)
        inserted=0
        for trade in trades:
            if not isinstance(trade,dict):raise ValueError('Malformed trade evidence')
            oid,tid=str(trade.get('order_id') or ''),str(trade.get('trade_id') or '')
            owned=con.execute('SELECT instrument_key,product,side,qty FROM v2_live_orders '
                              'WHERE user_id=? AND broker_order_id=?',(uid,oid)).fetchall()
            if not oid or not tid or len(owned)!=1:raise ValueError('External or ambiguous trade ownership')
            key,product,side,requested=owned[0]
            if (trade.get('instrument_token'),trade.get('product'),trade.get('transaction_type'))!=(key,product,side):
                raise ValueError('Trade identity does not match owned intent')
            qty=trade.get('quantity')
            if type(qty) is not int or qty<=0:raise ValueError('Invalid confirmed trade quantity')
            px=_amount(trade.get('average_price'))
            timestamp=trade.get('executed_at')
            if timestamp is None:
                # Upstox documents this field as exchange-local wall time.
                raw=trade.get('exchange_timestamp')
                try:
                    from zoneinfo import ZoneInfo
                    timestamp=datetime.strptime(raw,'%d-%b-%Y %H:%M:%S').replace(tzinfo=ZoneInfo('Asia/Kolkata')).isoformat()
                except (ValueError,TypeError) as exc:raise ValueError('Trade execution timestamp unavailable') from exc
            executed=_time(timestamp)
            if executed>now:raise ValueError('Future trade execution evidence')
            data=dict(order_id=oid,trade_id=tid,instrument_key=key,product=product,side=side,
                      quantity=qty,price=str(px.normalize()),currency='INR',occurred_at=executed.isoformat(),
                      source=broker+':actual-tradebook')
            notional=minor(px*qty)
            postings={'inventory_cost':notional,'trade_payable':-notional} if side=='BUY' else {
                      'trade_receivable':notional,'disposal_proceeds':-notional}
            _,new=_post(con,uid,broker,'trade:'+oid+':'+tid,'FILL',data,postings,now)
            inserted+=int(new)
            total=con.execute("SELECT COALESCE(SUM(quantity),0) FROM broker_ledger_events e WHERE user_id=? AND broker=? "
                              "AND order_id=? AND kind='FILL' AND NOT EXISTS (SELECT 1 FROM broker_ledger_events r "
                              "WHERE r.user_id=e.user_id AND r.broker=e.broker AND r.kind='REVERSAL' "
                              "AND json_extract(r.payload,'$.reverses')=e.id)",(uid,broker,oid)).fetchone()[0]
            if total>requested:raise ValueError('Confirmed trade quantity exceeds owned intent')
        return inserted


def record_fee(con,uid,order_id,reference,amount,*,source,occurred_at,broker='upstox',now=None):
    """One immutable total fee assessment per sourced reference, never estimated."""
    now=now or datetime.now(timezone.utc)
    if not isinstance(reference,str) or not reference.strip() or _time(occurred_at)>now:
        raise ValueError('Sourced fee identity/date required')
    if isinstance(amount,bool):raise ValueError('Invalid fee amount')
    amount=_amount(amount) if amount!=0 else Decimal(0)
    ensure_schema(con)
    with atomic(con):
        if not con.execute('SELECT 1 FROM v2_live_orders WHERE user_id=? AND broker_order_id=?',(uid,order_id)).fetchone():
            raise ValueError('Fee belongs to an unknown account order')
        data=dict(order_id=order_id,currency='INR',source=source,occurred_at=occurred_at,amount=str(amount))
        return _post(con,uid,broker,'fee:'+reference,'FEE',data,{'fees':minor(amount),'trade_payable':-minor(amount)},now)[0]


def settle(con,uid,reference,amount,*,source,occurred_at,broker='upstox',now=None):
    """Signed cash movement backed by statement evidence; no inferred settlement."""
    now=now or datetime.now(timezone.utc)
    if not reference or _time(occurred_at)>now or isinstance(amount,bool):raise ValueError('Sourced settlement required')
    value=minor(amount)
    if not value:raise ValueError('Nonzero settlement required')
    ensure_schema(con)
    with atomic(con):
        account='trade_receivable' if value>0 else 'trade_payable'
        prior=con.execute('SELECT id FROM broker_ledger_events WHERE user_id=? AND broker=? AND event_key=?',
                          (uid,broker,'settlement:'+reference)).fetchone()
        outstanding=con.execute('SELECT COALESCE(SUM(p.amount_minor),0) FROM broker_ledger_postings p '
                                'JOIN broker_ledger_events e ON e.id=p.event_id WHERE e.user_id=? AND e.broker=? '
                                'AND p.account=?',(uid,broker,account)).fetchone()[0]
        if not prior and (value>0 and value>outstanding or value<0 and value<outstanding):
            raise ValueError('Settlement exceeds owned trade obligations')
        data=dict(currency='INR',source=source,occurred_at=occurred_at,amount_minor=value)
        return _post(con,uid,broker,'settlement:'+reference,'SETTLEMENT',data,{'cash':value,account:-value},now)[0]


def reverse(con,uid,event_id,reference,*,source,occurred_at,broker='upstox',now=None):
    now=now or datetime.now(timezone.utc)
    if not reference or _time(occurred_at)>now:raise ValueError('Sourced correction required')
    with atomic(con):
        row=con.execute('SELECT currency FROM broker_ledger_events WHERE id=? AND user_id=? AND broker=?',
                        (event_id,uid,broker)).fetchone()
        if not row:raise ValueError('Unknown owned correction target')
        if con.execute("SELECT 1 FROM broker_ledger_events WHERE user_id=? AND broker=? AND kind='REVERSAL' "
                       "AND json_extract(payload,'$.reverses')=?",(uid,broker,event_id)).fetchone():
            raise ValueError('Event already reversed')
        postings={k:-v for k,v in con.execute('SELECT account,amount_minor FROM broker_ledger_postings WHERE event_id=?',(event_id,))}
        data=dict(currency=row[0],source=source,occurred_at=occurred_at,reverses=event_id)
        return _post(con,uid,broker,'reversal:'+reference,'REVERSAL',data,postings,now)[0]


def report(con,uid,broker='upstox'):
    balances=dict(con.execute('SELECT p.account,SUM(p.amount_minor) FROM broker_ledger_postings p JOIN broker_ledger_events e '
                             'ON e.id=p.event_id WHERE e.user_id=? AND e.broker=? GROUP BY p.account',(uid,broker)))
    counts=dict(con.execute('SELECT kind,COUNT(*) FROM broker_ledger_events WHERE user_id=? AND broker=? GROUP BY kind',(uid,broker)))
    # FIFO gross outcomes remain separate from unsourced final charges.
    from collections import defaultdict,deque
    lots=defaultdict(deque);gross=Decimal(0);closed=0;oversold=False
    fills=con.execute("SELECT instrument_key,product,side,quantity,price FROM broker_ledger_events e WHERE user_id=? AND broker=? "
                      "AND kind='FILL' AND NOT EXISTS (SELECT 1 FROM broker_ledger_events r WHERE r.user_id=e.user_id "
                      "AND r.broker=e.broker AND r.kind='REVERSAL' AND json_extract(r.payload,'$.reverses')=e.id) "
                      "ORDER BY julianday(occurred_at),id",(uid,broker))
    for key,product,side,qty,px in fills:
        px=Decimal(px);queue=lots[(key,product)]
        if side=='BUY':queue.append([qty,px]);continue
        remaining=qty
        while remaining and queue:
            taken=min(remaining,queue[0][0]);gross+=taken*(px-queue[0][1]);closed+=taken
            remaining-=taken;queue[0][0]-=taken
            if not queue[0][0]:queue.popleft()
        if remaining:oversold=True
    return dict(scope='actual-owned-broker-accounting',broker=broker,currency='INR',balances_minor=balances,
                events_by_kind=counts,balanced=sum(balances.values())==0,available_margin=None,
                realised_gross_minor=None if oversold else minor(gross),closed_units=closed,
                realised_net_minor=None,inventory_consistent=not oversold,
                fee_coverage='sourced-assessments-only',settlement_coverage='sourced-statements-only',
                certified=False,note='Trade-date postings are not spendable broker cash or certified net P&L')
