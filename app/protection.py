"""Durable stop obligations for owned, confirmed broker inventory.

Legacy application polling is labelled APP_ONLY, never broker protection.
Native activation needs a separate recorded authorization and complete dated
contract rules. This module does not create that authorization automatically.
An uncertain native stop reserves the right to sell: another exit must wait.
"""
import json
import math
import time
from datetime import datetime,timezone

from .account_safety import atomic
from .live_release import authorized as live_scope_authorized

BLOCKING = {'app_only','required','submitting','unknown','failed','triggered','cancelling'}
SAFE_TO_SELL = {'app_only','cancelled','closed'}


def ensure_schema(con):
    con.execute('''CREATE TABLE IF NOT EXISTS protection_obligations(
      entry_id INTEGER PRIMARY KEY,user_id INTEGER NOT NULL,symbol TEXT NOT NULL,
      instrument_key TEXT NOT NULL,product TEXT NOT NULL,quantity INTEGER NOT NULL,
      stop REAL NOT NULL,state TEXT NOT NULL,native_id TEXT,exit_order_id TEXT,
      authorization_reference TEXT,cancel_requested_at REAL,updated_at REAL NOT NULL)''')
    if 'submission_attempted' not in {r[1] for r in con.execute('PRAGMA table_info(protection_obligations)')}:
        con.execute('ALTER TABLE protection_obligations ADD COLUMN submission_attempted INTEGER NOT NULL DEFAULT 0')
        con.execute("UPDATE protection_obligations SET submission_attempted=1 WHERE native_id IS NOT NULL "
                    "OR (authorization_reference IS NOT NULL AND state IN ('submitting','unknown','armed','triggered','cancelling'))")
    con.execute('''CREATE TABLE IF NOT EXISTS protection_events(
      id INTEGER PRIMARY KEY,entry_id INTEGER NOT NULL,user_id INTEGER NOT NULL,
      state TEXT NOT NULL,detail TEXT NOT NULL,observed_at REAL NOT NULL)''')
    for action in ('UPDATE','DELETE'):
        con.execute(f"CREATE TRIGGER IF NOT EXISTS immutable_protection_{action.lower()} "
                    f"BEFORE {action} ON protection_events BEGIN SELECT RAISE(ABORT,'immutable protection event'); END")


def _row(con,uid,entry_id):
    row=con.execute('SELECT entry_id,user_id,symbol,instrument_key,product,quantity,stop,state,native_id,'
                    'exit_order_id,authorization_reference,cancel_requested_at,submission_attempted FROM protection_obligations '
                    'WHERE entry_id=? AND user_id=?',(entry_id,uid)).fetchone()
    return dict(zip(('entry_id','user_id','symbol','instrument_key','product','quantity','stop','state',
                     'native_id','exit_order_id','authorization_reference','cancel_requested_at','submission_attempted'),row)) if row else None


def _event(con,row,state,detail,now=None):
    from .worker_fencing import require_current
    require_current(con)
    now=time.time() if now is None else now
    if row['state']==state:return
    con.execute('UPDATE protection_obligations SET state=?,updated_at=? WHERE entry_id=? AND user_id=?',
                (state,now,row['entry_id'],row['user_id']))
    con.execute('INSERT INTO protection_events(entry_id,user_id,state,detail,observed_at) VALUES(?,?,?,?,?)',
                (row['entry_id'],row['user_id'],state,detail,now))
    _incident(con,row,state,detail,now)


def _incident(con,row,state,detail,now):
    from .execution_outbox import ensure_schema,incident
    ensure_schema(con)
    if state in {'unknown','failed','triggered'}:
        incident(con,row['user_id'],'NATIVE_PROTECTION',row['entry_id'],state+': '+detail)
    elif state in {'armed','closed','cancelled'}:
        con.execute("UPDATE execution_incidents SET resolved_at=? WHERE user_id=? AND code='NATIVE_PROTECTION' AND reference=?",
                    (now,row['user_id'],str(row['entry_id'])))
        con.execute("UPDATE execution_incidents SET resolved_at=? WHERE user_id=? AND code='PARTIAL_ENTRY_PROTECTION' AND reference=?",
                    (now,row['user_id'],str(row['entry_id'])))


def _owned_remaining(con,uid):
    """Allocate confirmed exits to entries within the same contract/product.

    A later position in the same ticker must not revive an earlier stop.
    This is inventory attribution, not an inferred broker fill or tax lot.
    """
    groups={};remaining={}
    for entry_id,key,product,side,qty in con.execute(
            'SELECT id,instrument_key,product,side,filled_qty FROM v2_live_orders '
            'WHERE user_id=? AND filled_qty>0 ORDER BY id',(uid,)):
        group=groups.setdefault((key,product),{'buys':[],'sold':0})
        if side=='BUY':group['buys'].append((entry_id,qty))
        elif side=='SELL':group['sold']+=qty
    for group in groups.values():
        sold=group['sold']
        for entry_id,qty in group['buys']:
            consumed=min(sold,qty);sold-=consumed
            remaining[entry_id]=qty-consumed
    return remaining


def _activation_contract(con,uid,row):
    allowed,why=live_scope_authorized(uid,product=row['product'],model='native-protection')
    if not allowed:raise ValueError(why)
    entry=con.execute('SELECT status,filled_qty FROM v2_live_orders WHERE id=? AND user_id=?',
                      (row['entry_id'],uid)).fetchone()
    if not entry or entry[0] not in {'filled','cancelled','rejected'} or entry[1]!=row['quantity'] or \
            _owned_remaining(con,uid).get(row['entry_id'],0)!=row['quantity']:
        raise ValueError('Entry fills or remaining owned inventory are unresolved')
    from .entry_contracts import protective_contract
    protective_contract(row['symbol'],row['quantity'],row['stop'],row['product'],row['instrument_key'])


def observe_fills(con,uid):
    """Inside fill transaction; import ownership from exact journal entries only."""
    ensure_schema(con)
    remaining=_owned_remaining(con,uid)
    entries=con.execute("SELECT o.id,o.symbol,o.instrument_key,o.product,o.filled_qty,p.stop "
                         "FROM v2_live_orders o LEFT JOIN v2_live_protection p ON p.user_id=o.user_id AND p.symbol=o.symbol "
                         "WHERE o.user_id=? AND o.side='BUY' AND o.filled_qty>0 ORDER BY o.id",(uid,)).fetchall()
    for entry_id,symbol,key,product,qty,stop in entries:
        row=_row(con,uid,entry_id)
        if not row:
            valid=isinstance(stop,(int,float)) and math.isfinite(stop) and stop>0
            state='app_only' if valid else 'failed'
            con.execute('INSERT INTO protection_obligations(entry_id,user_id,symbol,instrument_key,product,quantity,stop,state,updated_at) '
                        'VALUES(?,?,?,?,?,?,?,?,?)',(entry_id,uid,symbol,key,product,qty,stop if valid else 0,state,time.time()))
            con.execute('INSERT INTO protection_events(entry_id,user_id,state,detail,observed_at) VALUES(?,?,?,?,?)',
                        (entry_id,uid,state,'Confirmed entry; native protection not activated',time.time()))
            row=_row(con,uid,entry_id)
            _incident(con,row,state,'Confirmed entry; native protection not activated',time.time())
        if row['quantity']!=qty:
            con.execute('UPDATE protection_obligations SET quantity=? WHERE entry_id=?',(qty,entry_id))
            if row['native_id'] or row['state']!='app_only':
                _event(con,row,'unknown','Further entry fills require protection quantity reconciliation')
            if row['state']=='closed' and not row['native_id'] and not row['authorization_reference']:
                con.execute('INSERT INTO v2_live_protection(user_id,symbol,stop) VALUES(?,?,?) '
                            'ON CONFLICT(user_id,symbol) DO NOTHING',(uid,symbol,row['stop']))
            row=_row(con,uid,entry_id)
        if row['state']=='closed' and row['quantity']==qty:continue
        held=remaining.get(entry_id,0)
        exit_filled=con.execute("SELECT status FROM v2_live_orders WHERE user_id=? AND broker_order_id=?",(uid,row['exit_order_id'])).fetchone() if row['exit_order_id'] else None
        if held==0 and (row['state'] in SAFE_TO_SELL or (exit_filled and exit_filled[0]=='filled')):
            _event(con,row,'closed','Owned journal inventory is flat')


def request_native(con,uid,entry_id,*,authorization_reference):
    """Operator-only activation boundary; no UI/API calls this automatically.

    The reference must cite a separately reviewed native execution approval.
    A reference is recorded for audit, not presented as proof of permission.
    """
    if not isinstance(authorization_reference,str) or not authorization_reference.strip():
        raise ValueError('Separate native protection authorization required')
    from .recovery_guard import assert_database_execution_allowed
    assert_database_execution_allowed(con)
    with atomic(con):
        row=_row(con,uid,entry_id)
        if not row or row['state']!='app_only':raise ValueError('Owned protection is not eligible for activation')
        _activation_contract(con,uid,row)
        con.execute('UPDATE protection_obligations SET authorization_reference=? WHERE entry_id=? AND user_id=?',
                    (authorization_reference,entry_id,uid))
        _event(con,row,'required','Native activation explicitly recorded')


def activate_reviewed_fills(con,uid):
    """Activate only new canonical terminal fills under separate reviewed policy.

    No legacy inventory is adopted and no permission record is generated.
    Partial entries wait for cancellation/terminal status. Missing evidence
    leaves an explicit obligation and blocks further account entries.
    """
    from .live_release import native_policy
    activated=0
    rows=con.execute("SELECT p.entry_id,p.product FROM protection_obligations p JOIN v2_live_orders o ON o.id=p.entry_id "
        "WHERE p.user_id=? AND p.state='app_only' AND o.status IN ('filled','cancelled','rejected') "
        "AND EXISTS (SELECT 1 FROM entry_contract_records c WHERE c.scope='broker' AND c.user_id=p.user_id AND c.position_id=p.entry_id)",(uid,)).fetchall()
    for entry_id,product in rows:
        policy=native_policy(uid,product)
        if not policy:continue
        try:
            request_native(con,uid,entry_id,authorization_reference=policy['reference']+' / '+policy['source_commit'])
            activated+=1
        except ValueError:
            with atomic(con):
                row=_row(con,uid,entry_id)
                if row:_event(con,row,'failed','Reviewed native coverage cannot establish owned dated contract; new entries blocked')
    return activated


def settle_partial_entries(con,uid):
    """Cancel a reviewed partial entry's remainder before native stop sizing.

    A cancellation acknowledgement is not final. Pending commitments remain
    until terminal order evidence; no blind cancellation retry is allowed.
    """
    from .live_release import native_policy
    from .order_journal import finish_entry_before_exit
    from .execution_outbox import incident
    from .recovery_guard import assert_database_execution_allowed
    assert_database_execution_allowed(con)
    rows=con.execute("SELECT o.id,o.symbol,o.product FROM v2_live_orders o JOIN protection_obligations p ON p.entry_id=o.id "
                     "WHERE o.user_id=? AND o.side='BUY' AND o.filled_qty>0 AND o.status IN ('partial','submitted','unknown') "
                     "AND p.state='app_only' AND EXISTS (SELECT 1 FROM entry_contract_records c "
                     "WHERE c.scope='broker' AND c.user_id=o.user_id AND c.position_id=o.id)",(uid,)).fetchall()
    handled=0
    for entry_id,symbol,product in rows:
        if not native_policy(uid,product):continue
        with atomic(con):incident(con,uid,'PARTIAL_ENTRY_PROTECTION',entry_id,'Partial entry: cancel remainder and verify terminal fills before native coverage')
        finish_entry_before_exit(con,uid,symbol)
        handled+=1
    return handled


def submit_stop(con,uid,entry_id,port):
    """Claim before I/O. A crash after claim is UNKNOWN and cannot be retried."""
    from .recovery_guard import assert_database_execution_allowed
    from .worker_fencing import require_current
    assert_database_execution_allowed(con)
    if con.in_transaction:raise RuntimeError('Protection submission requires a committed claim')
    with atomic(con):
        require_current(con)
        row=_row(con,uid,entry_id)
        if not row or row['state']!='required' or not row['authorization_reference']:
            return 'skipped'
        try:_activation_contract(con,uid,row)
        except ValueError:
            _event(con,row,'failed','Native authorization/contract/ownership no longer valid; no transmission')
            return 'failed'
        con.execute('UPDATE protection_obligations SET submission_attempted=1 WHERE entry_id=? AND user_id=?',
                    (entry_id,uid))
        _event(con,row,'submitting','Stop submission reserved before broker I/O')
    # atomic() uses a savepoint if called inside an existing transaction. Do
    # not transmit from a caller's uncommitted transaction.
    if con.in_transaction:raise RuntimeError('Protection submission requires a committed claim')
    try:
        result=port.place_stop(uid,row['instrument_key'],row['quantity'],product=row['product'],stop=row['stop'])
        ids=(result.get('data') or {}).get('gtt_order_ids',[])
        valid=result.get('ok') and isinstance(ids,list) and len(ids)==1 and isinstance(ids[0],str) and ids[0].startswith('GTT-')
        state='unknown'  # Acceptance still needs a status observation.
        native_id=ids[0] if valid else None
    except Exception:
        state,native_id='unknown',None
    with atomic(con):
        require_current(con)
        current=_row(con,uid,entry_id)
        if current['state']=='submitting':
            con.execute('UPDATE protection_obligations SET native_id=? WHERE entry_id=? AND user_id=?',
                        (native_id,entry_id,uid))
            _event(con,current,state,'Submission needs broker status; never blind retry')
    return state


def refresh(con,uid,port):
    """Missing GTT is unknown: this endpoint omits completed triggers."""
    ensure_schema(con)
    ids=[r[0] for r in con.execute("SELECT entry_id FROM protection_obligations WHERE user_id=? "
                                   "AND state NOT IN ('app_only','closed','cancelled')",(uid,))]
    for entry_id in ids:
        row=_row(con,uid,entry_id)
        if not row['native_id']:
            if row['state']=='submitting':
                with atomic(con):_event(con,row,'unknown','Worker resumed after uncertain submission')
            continue
        try:
            rows=port.protection_status(uid,row['native_id'])
            matches=[r for r in rows if r.get('gtt_order_id')==row['native_id']]
            if len(matches)!=1:raise ValueError('missing/ambiguous native stop')
            evidence=matches[0];rules=evidence.get('rules',[])
            if evidence.get('type')!='SINGLE' or evidence.get('instrument_token')!=row['instrument_key'] or \
                    evidence.get('product')!=row['product'] or type(evidence.get('quantity')) is not int or \
                    evidence['quantity']!=row['quantity'] or len(rules)!=1:
                raise ValueError('native contract changed')
            rule=rules[0]
            if rule.get('transaction_type')!='SELL' or rule.get('strategy')!='ENTRY' or rule.get('trigger_type')!='BELOW' or \
                    rule.get('trigger_price')!=row['stop']:
                raise ValueError('native trigger changed')
            status=rule.get('status');oid=rule.get('order_id')
            expires=evidence.get('expires_at')
            if isinstance(expires,bool) or not isinstance(expires,(int,float)) or not math.isfinite(expires):
                raise ValueError('native expiry unavailable')
            state='armed' if status=='SCHEDULED' and expires>time.time()*1_000_000 else \
                  'cancelled' if status=='CANCELLED' and not oid else \
                  'failed' if status in {'EXPIRED','FAILED'} and not oid else 'unknown'
            if oid:
                state='triggered'
                with atomic(con):
                    existing=con.execute('SELECT id,instrument_key,side,product,qty FROM v2_live_orders '
                                         'WHERE user_id=? AND broker_order_id=?',(uid,str(oid))).fetchall()
                    if existing and (len(existing)!=1 or tuple(existing[0][1:])!=(row['instrument_key'],'SELL',row['product'],row['quantity'])):
                        raise ValueError('native exit ownership conflict')
                    if not existing:
                        con.execute("INSERT INTO v2_live_orders(ts,user_id,market,symbol,instrument_key,side,qty,price,notional,"
                                    "product,status,reason,broker_order_id,semantic_key,origin_position_id,filled_qty) "
                                    "VALUES(?,?,'IN',?,?,'SELL',?,?,?,?, 'submitted','native stop',?,?,?,0)",
                                    (datetime.now(timezone.utc).isoformat(),uid,row['symbol'],row['instrument_key'],row['quantity'],
                                     row['stop'],row['quantity']*row['stop'],row['product'],str(oid),
                                     f"protection:{entry_id}:{oid}",entry_id))
                    con.execute('UPDATE protection_obligations SET exit_order_id=? WHERE entry_id=? AND user_id=?',
                                (str(oid),entry_id,uid))
            if row['cancel_requested_at'] and state=='armed':state='cancelling'
        except Exception:
            state='unknown'
        with atomic(con):_event(con,row,state,'Native status observed; trigger is not an exchange fill')


def prepare_exit(con,uid,symbol,port):
    """Cancel known native stops first. Acceptance does not free the sell right."""
    if con.in_transaction:raise RuntimeError('Cancellation requires a committed reservation')
    from .worker_fencing import require_current
    from .recovery_guard import assert_database_execution_allowed
    assert_database_execution_allowed(con)
    rows=[_row(con,uid,r[0]) for r in con.execute('SELECT entry_id FROM protection_obligations WHERE user_id=? AND symbol=?',
                                                (uid,symbol))]
    for index,row in enumerate(rows):
        if row['state'] in {'required','failed'} and not row['submission_attempted'] and not row['native_id']:
            with atomic(con):
                require_current(con)
                current=_row(con,uid,row['entry_id'])
                if current['state'] in {'required','failed'} and not current['submission_attempted'] and not current['native_id']:
                    _event(con,current,'cancelled','Untransmitted stop obligation withdrawn for owned exit')
            row=rows[index]=_row(con,uid,row['entry_id'])
        if row['state'] in SAFE_TO_SELL or (row['state'] in {'failed','unknown'} and not row['native_id'] and not row['submission_attempted']):continue
        if row['native_id'] and not row['cancel_requested_at'] and not row['exit_order_id']:
            with atomic(con):
                require_current(con)
                cur=con.execute('UPDATE protection_obligations SET cancel_requested_at=? WHERE entry_id=? AND user_id=? '
                                'AND cancel_requested_at IS NULL',(time.time(),row['entry_id'],uid))
                if cur.rowcount:_event(con,row,'cancelling','Cancellation reserved before I/O')
            if con.in_transaction:raise RuntimeError('Cancellation requires a committed reservation')
            if cur.rowcount:
                try:port.cancel_protection(uid,row['native_id'])
                except Exception:pass  # Wait for actual cancellation/trigger evidence.
    return all(row['state'] in SAFE_TO_SELL or (row['state'] in {'failed','unknown'} and not row['native_id'] and not row['submission_attempted']) for row in rows)


def blocks_entry(con,uid):
    marks=','.join('?' for _ in BLOCKING)
    return bool(con.execute("SELECT 1 FROM protection_obligations WHERE user_id=? AND (state IN ("+marks+") "
                            "OR (state='armed' AND updated_at<?)) LIMIT 1",
                            (uid,*sorted(BLOCKING),time.time()-120)).fetchone())


def report(con,uid):
    rows=con.execute('SELECT entry_id,symbol,quantity,stop,state,native_id,exit_order_id,updated_at FROM protection_obligations '
                     'WHERE user_id=? ORDER BY entry_id DESC LIMIT 100',(uid,)).fetchall()
    remaining=_owned_remaining(con,uid)
    result=[dict(zip(('entry_id','symbol','quantity','stop','state','native_id','exit_order_id','updated_at'),r)) for r in rows]
    for row in result:row['remaining_quantity']=remaining.get(row['entry_id'],0)
    from .live_release import native_policy
    return dict(rows=result,native_guarantees_fill=False,
                activation_automatic=bool(native_policy(uid,'D') or native_policy(uid,'I')),
                activation_requires_separate_review=True,live_certified=False)
