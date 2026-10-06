"""Immutable approved-paper plan → shared account risk → fill → owned position.

Research eligibility is not approval. Approvals are written only by a trusted
operator/review service; this module exposes no automatic research promotion.
The production engine's model allowlist is unchanged. Manual plans must be
explicitly labelled manual and cannot cite a research publication as approval.
"""
from datetime import datetime,timezone
import hashlib
import json
import math

from .account_safety import atomic
from . import books,execution_contracts
from .execution_ports import route_for


def ensure_schema(con):
    con.execute('''CREATE TABLE IF NOT EXISTS approved_execution_plans(
      id TEXT PRIMARY KEY,user_id INTEGER NOT NULL,market TEXT NOT NULL,epoch TEXT NOT NULL,
      payload TEXT NOT NULL,approved_by TEXT NOT NULL,approval_reference TEXT NOT NULL,
      created_at TEXT NOT NULL)''')
    con.execute('''CREATE TABLE IF NOT EXISTS approved_execution_events(
      id INTEGER PRIMARY KEY,plan_id TEXT NOT NULL,user_id INTEGER NOT NULL,request_key TEXT NOT NULL,
      kind TEXT NOT NULL,payload TEXT NOT NULL,observed_at TEXT NOT NULL,
      UNIQUE(user_id,request_key,kind))''')
    for table in ('approved_execution_plans','approved_execution_events'):
        for action in ('UPDATE','DELETE'):
            con.execute(f"CREATE TRIGGER IF NOT EXISTS immutable_{table}_{action.lower()} BEFORE {action} ON {table} "
                        "BEGIN SELECT RAISE(ABORT,'immutable execution approval/event'); END")


def _moment(value):
    at=datetime.fromisoformat(value.replace('Z','+00:00'))
    if at.tzinfo is None:raise ValueError('aware plan timestamp required')
    return at


def approve(con,plan,*,approved_by,approval_reference,now=None):
    """Freeze reviewed content; a reference is attribution, not alpha proof."""
    now=now or datetime.now(timezone.utc)
    plan=dict(plan)
    if type(plan.get('user_id')) is not int or plan['user_id']<1 or plan.get('market')!='IN' or \
            not isinstance(approved_by,str) or not approved_by.strip() or not isinstance(approval_reference,str) or not approval_reference.strip():
        raise ValueError('owned, reviewed NSE paper approval required')
    if plan.get('mode')!='paper' or plan.get('side')!='BUY' or plan.get('product')!='D':
        raise ValueError('This approved route implements delivery paper buys only')
    if type(plan.get('quantity')) is not int or plan['quantity']<1 or \
            not isinstance(plan.get('instrument_id'),str) or not plan['instrument_id'].startswith('ins_') or \
            not plan.get('symbol') or not plan.get('model_version') or not plan.get('evidence_reference'):
        raise ValueError('incomplete approved plan identity')
    levels=[plan.get(k) for k in ('stop','entry_low','entry_high','target')]
    if any(isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) for v in levels) or \
            not 0<levels[0]<levels[1]<=levels[2]<levels[3]:raise ValueError('invalid frozen entry/stop/target')
    if not _moment(plan['evidence_at'])<=now<_moment(plan['expires_at']):raise ValueError('future or expired plan evidence')
    if plan.get('approval_kind')=='manual':
        if plan.get('sleeve')!='manual' or plan.get('publication_id') is not None:
            raise ValueError('Research cannot be reclassified as a manual plan')
    elif plan.get('approval_kind')=='validated-model':
        if plan.get('sleeve') not in {'mean_reversion','quality_momentum','early_momentum','index_directional','options_overlay'} or \
                not plan.get('independent_validation_reference'):
            raise ValueError('Reviewed model validation reference required')
    else:raise ValueError('Research-only plans cannot open positions')
    if not con.execute("SELECT 1 FROM user_book WHERE user_id=? AND market='IN'",(plan['user_id'],)).fetchone() or \
            plan.get('epoch')!=books.current_epoch(con,plan['user_id'],'IN'):
        raise ValueError('Approval belongs to another paper epoch')
    text=json.dumps(plan,sort_keys=True,separators=(',',':'),allow_nan=False)
    plan_id='plan_'+hashlib.sha256(text.encode()).hexdigest()
    ensure_schema(con)
    with atomic(con):
        existing=con.execute('SELECT approved_by,approval_reference FROM approved_execution_plans WHERE id=?',(plan_id,)).fetchone()
        if existing and existing!=(approved_by,approval_reference):raise ValueError('Approval attribution cannot be rebound')
        con.execute('INSERT OR IGNORE INTO approved_execution_plans VALUES(?,?,?,?,?,?,?,?)',
                    (plan_id,plan['user_id'],'IN',plan['epoch'],text,approved_by,approval_reference,now.isoformat()))
    return plan_id


def _event(con,plan_id,uid,request_key,kind,payload,now):
    con.execute('INSERT INTO approved_execution_events(plan_id,user_id,request_key,kind,payload,observed_at) VALUES(?,?,?,?,?,?)',
                (plan_id,uid,request_key,kind,json.dumps(payload,sort_keys=True),now.isoformat()))


def submit(con,catalogue,user_id,plan_id,request_key,quotes,*,regime,now=None):
    """One serialized, exact-quantity account decision; no broker substitution."""
    now=now or datetime.now(timezone.utc)
    if type(user_id) is not int or user_id<1 or not isinstance(request_key,str) or not 8<=len(request_key)<=128:
        raise ValueError('owned request identity required')
    from .recovery_guard import assert_database_execution_allowed
    from .sleeves.feeds import fresh_quotes
    assert_database_execution_allowed(con)
    with atomic(con):
        row=con.execute('SELECT payload FROM approved_execution_plans WHERE id=? AND user_id=?',(plan_id,user_id)).fetchone()
        if not row:raise ValueError('Approved plan not found for this account')
        plan=json.loads(row[0])
        prior=con.execute("SELECT plan_id,payload FROM approved_execution_events WHERE user_id=? AND request_key=? AND kind IN ('FILLED','REJECTED')",
                          (user_id,request_key)).fetchone()
        if prior:
            if prior[0]!=plan_id:raise ValueError('Request identity belongs to another plan')
            return json.loads(prior[1])
        already=con.execute("SELECT payload FROM approved_execution_events WHERE user_id=? AND plan_id=? AND kind='FILLED' LIMIT 1",(user_id,plan_id)).fetchone()
        if already:
            raise ValueError('This approved plan already filled; a new reviewed plan is required')
        reason=None;price=(fresh_quotes(quotes,now).get(plan['symbol']) or {}).get('price')
        if plan['epoch']!=books.current_epoch(con,user_id,'IN'):reason='Paper epoch changed; approval is not transferable'
        elif not _moment(plan['evidence_at'])<=now<_moment(plan['expires_at']):reason='Plan evidence is future or expired'
        elif regime not in {'ON','NEUTRAL'}:reason='Current regime blocks new equity longs'
        elif price is None or not plan['entry_low']<=price<=plan['entry_high']:reason='Fresh price is outside the frozen entry zone'
        if reason is None:
            try:
                spec,key,evidence=execution_contracts.order_contract(catalogue,instrument_id=plan['instrument_id'],quantity=plan['quantity'],price=price,now=now)
                route_for('paper',spec,plan['product'])
                if spec.symbol!=plan['symbol']:raise ValueError('Symbol/identity changed; reviewed replacement required')
                spec.validate_order(plan['quantity'],plan['stop'],now=now)
                spec.validate_order(plan['quantity'],plan['target'],now=now)
            except ValueError as exc:reason=str(exc)
        if reason is not None:
            result=dict(ok=False,mode='paper',plan_id=plan_id,status='rejected',reason=reason,paper_recorded=False)
            _event(con,plan_id,user_id,request_key,'REJECTED',result,now);return result
        _event(con,plan_id,user_id,request_key,'RISK_REQUESTED',dict(contract_evidence=evidence,instrument_id=spec.id),now)
        quantity=books.buy(con,user_id,'IN',plan['sleeve'],spec.symbol,price,plan['quantity'],plan['stop'],plan['target'],
                           sleeve=plan['sleeve'],regime=regime,quotes=quotes,request_key='approved:'+request_key,exact_quantity=True)
        if not quantity:
            result=dict(ok=False,mode='paper',plan_id=plan_id,status='rejected',reason=books.refusal(con,user_id,'IN'),paper_recorded=False)
            _event(con,plan_id,user_id,request_key,'REJECTED',result,now);return result
        receipt=books.entry_receipt(con,user_id,'IN','approved:'+request_key)
        con.execute('UPDATE user_positions SET instrument_id=?,plan_id=?,model_version=? WHERE id=? AND user_id=?',
                    (spec.id,plan_id,plan['model_version'],receipt['position_id'],user_id))
        result=dict(ok=True,mode='paper',plan_id=plan_id,status='filled',position_id=receipt['position_id'],
                    instrument_id=spec.id,entry=receipt['entry'],qty=quantity,paper_recorded=True,
                    protection='application-paper',model_version=plan['model_version'])
        _event(con,plan_id,user_id,request_key,'FILLED',result,now)
        return result


def report(con,user_id):
    rows=con.execute('SELECT id,payload,approved_by,approval_reference,created_at FROM approved_execution_plans '
                     'WHERE user_id=? ORDER BY created_at DESC LIMIT 50',(user_id,)).fetchall()
    plans=[];now=datetime.now(timezone.utc);epoch=books.current_epoch(con,user_id,'IN')
    for row in rows:
        plan=json.loads(row[1])
        filled=con.execute("SELECT payload FROM approved_execution_events WHERE user_id=? AND plan_id=? AND kind='FILLED' LIMIT 1",(user_id,row[0])).fetchone()
        state='retired' if plan['epoch']!=epoch else 'filled' if filled else 'expired' if _moment(plan['expires_at'])<=now else 'approved'
        plans.append(dict(id=row[0],plan=plan,status=state,fill=json.loads(filled[0]) if filled else None,approved_by=row[2],approval_reference=row[3],created_at=row[4]))
    return dict(scope='owned-approved-paper',plans=plans,
                approvals_automatic=False,broker_execution=False)
