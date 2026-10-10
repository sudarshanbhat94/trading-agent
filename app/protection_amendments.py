"""Claim-once reductions of a known scheduled native stop.

Only confirmed owned inventory can reduce coverage. Acceptance and timeouts
both require a later exact broker observation; neither permits a retry.
"""
import time

from .account_safety import atomic
from .worker_fencing import require_current


def ensure_schema(con):
    con.execute('''CREATE TABLE IF NOT EXISTS native_amendment_intents(
      id INTEGER PRIMARY KEY,entry_id INTEGER NOT NULL,user_id INTEGER NOT NULL,
      native_id TEXT NOT NULL,from_quantity INTEGER NOT NULL,to_quantity INTEGER NOT NULL,
      stop REAL NOT NULL,created_at REAL NOT NULL)''')
    con.execute('''CREATE TABLE IF NOT EXISTS native_amendment_events(
      id INTEGER PRIMARY KEY,amendment_id INTEGER NOT NULL,state TEXT NOT NULL,
      detail TEXT NOT NULL,observed_at REAL NOT NULL)''')
    for table in ('native_amendment_intents','native_amendment_events'):
        for action in ('UPDATE','DELETE'):
            con.execute(f"CREATE TRIGGER IF NOT EXISTS {table}_no_{action.lower()} BEFORE {action} ON {table} "
                        "BEGIN SELECT RAISE(ABORT,'immutable native amendment'); END")


def latest(con,uid,entry_id):
    row=con.execute('SELECT id,native_id,from_quantity,to_quantity,stop FROM native_amendment_intents '
                    'WHERE user_id=? AND entry_id=? ORDER BY id DESC LIMIT 1',(uid,entry_id)).fetchone()
    if not row:return None
    state=con.execute('SELECT state FROM native_amendment_events WHERE amendment_id=? ORDER BY id DESC LIMIT 1',
                      (row[0],)).fetchone()
    return dict(zip(('id','native_id','from_quantity','to_quantity','stop'),row),state=state[0] if state else 'unknown')


def _event(con,intent,state,detail):
    require_current(con)
    con.execute('INSERT INTO native_amendment_events(amendment_id,state,detail,observed_at) VALUES(?,?,?,?)',
                (intent,state,detail,time.time()))


def resolve(con,row,rows):
    """Called inside the fenced status transaction, never on an API ack."""
    from . import protection
    prior=latest(con,row['user_id'],row['entry_id'])
    if not prior or prior['state']=='confirmed':return row
    if prior['native_id']!=row['native_id'] or prior['stop']!=row['stop']:return row
    try:rule,expires=protection._native_evidence(row,rows,quantity=prior['to_quantity'])
    except (ValueError,TypeError,KeyError):return row
    if rule['status']=='SCHEDULED' and not rule.get('order_id') and expires>time.time()*1_000_000 or \
            rule['status'] in {'CANCELLED','EXPIRED','FAILED'} and not rule.get('order_id') or \
            rule['status'] in {'TRIGGERED','OPEN','COMPLETED'} and rule.get('order_id'):
        require_current(con)
        con.execute('UPDATE protection_obligations SET coverage_quantity=? WHERE entry_id=? AND user_id=?',
                    (prior['to_quantity'],row['entry_id'],row['user_id']))
        _event(con,prior['id'],'confirmed','Exact broker quantity observed; acceptance alone was not confirmation')
        return protection._row(con,row['user_id'],row['entry_id'])
    return row


def reduce(con,uid,entry_id,port):
    """Maintain separately authorized coverage; never increase exposure."""
    from . import protection,live_release,broker_reconciliation
    from .entry_contracts import protective_contract
    from .recovery_guard import assert_database_execution_allowed
    assert_database_execution_allowed(con)
    if con.in_transaction:raise RuntimeError('Stop amendment requires a committed claim')
    row=protection._row(con,uid,entry_id)
    if not row or not row['native_id']:return 'skipped'
    prior=latest(con,uid,entry_id)
    if prior and prior['state']!='confirmed':return 'unknown'
    target=protection._owned_remaining(con,uid).get(entry_id,0)
    if not row['coverage_quantity'] or not 0<target<row['coverage_quantity'] or row['exit_order_id'] or row['cancel_requested_at']:
        return 'skipped'
    try:rows=port.protection_status(uid,row['native_id'])
    except Exception:return 'unknown'
    with atomic(con):
        require_current(con)
        current=protection._row(con,uid,entry_id)
        if not current or current['native_id']!=row['native_id']:return 'skipped'
        row=current
        prior=latest(con,uid,entry_id)
        if prior and prior['state']!='confirmed':return 'unknown'
        if not row['authorization_reference'] or row['cancel_requested_at'] or row['exit_order_id'] or \
                row['state'] not in {'armed','unknown'}:return 'skipped'
        target=protection._owned_remaining(con,uid).get(entry_id,0)
        if not row['coverage_quantity'] or not 0<target<row['coverage_quantity'] or protection._pending_exit(con,uid,row):
            return 'skipped'
        entry=con.execute('SELECT status FROM v2_live_orders WHERE id=? AND user_id=?',(entry_id,uid)).fetchone()
        if not entry or entry[0] not in {'filled','cancelled','rejected'}:return 'skipped'
        allowed,_=protection.live_scope_authorized(uid,product=row['product'],model='native-protection')
        if not allowed or not live_release.native_policy(uid,row['product']) or not broker_reconciliation.ready(con,uid):
            return 'skipped'
        try:
            rule,expires=protection._native_evidence(row,rows)
            if rule['status']!='SCHEDULED' or rule.get('order_id') or expires<=time.time()*1_000_000:
                return 'skipped'  # OPEN/triggered quantities cannot be modified.
            protective_contract(row['symbol'],target,row['stop'],row['product'],row['instrument_key'])
        except (ValueError,TypeError,KeyError):return 'unknown'
        cursor=con.execute('INSERT INTO native_amendment_intents(entry_id,user_id,native_id,from_quantity,to_quantity,stop,created_at) '
                           'VALUES(?,?,?,?,?,?,?)',(entry_id,uid,row['native_id'],row['coverage_quantity'],target,row['stop'],time.time()))
        intent=cursor.lastrowid
        _event(con,intent,'claimed','Reduction reserved before broker I/O')
        protection._event(con,row,'amending','Native quantity reduction reserved; coverage unresolved')
    try:port.modify_protection(uid,row['native_id'],quantity=target,stop=row['stop'])
    except Exception:pass  # Even a successful acknowledgement needs exact later status.
    with atomic(con):
        require_current(con)
        latest_state=con.execute('SELECT state FROM native_amendment_events WHERE amendment_id=? ORDER BY id DESC LIMIT 1',
                                 (intent,)).fetchone()
        if latest_state and latest_state[0]=='confirmed':return 'confirmed'
        _event(con,intent,'unknown','Submission outcome awaits exact broker evidence; never blind retry')
        current=protection._row(con,uid,entry_id)
        if current and current['state']=='amending':
            protection._event(con,current,'unknown','Quantity reduction awaits exact native status')
    return 'unknown'
