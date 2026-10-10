"""Immutable approved-paper plan → shared account risk → fill → owned position.

Research eligibility is not approval. Approvals are written only by a trusted
operator/review service; this module exposes no automatic research promotion.
The production engine's model allowlist is unchanged. Manual plans must be
explicitly labelled manual and cannot cite a research publication as approval.
"""
from datetime import datetime,timedelta,timezone
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
    con.execute('''CREATE TABLE IF NOT EXISTS manual_plan_bindings(
      user_id INTEGER NOT NULL,epoch TEXT NOT NULL,request_key TEXT NOT NULL,
      fingerprint TEXT NOT NULL,plan_id TEXT NOT NULL,
      PRIMARY KEY(user_id,epoch,request_key))''')
    for table in ('approved_execution_plans','approved_execution_events','manual_plan_bindings'):
        for action in ('UPDATE','DELETE'):
            con.execute(f"CREATE TRIGGER IF NOT EXISTS immutable_{table}_{action.lower()} BEFORE {action} ON {table} "
                        "BEGIN SELECT RAISE(ABORT,'immutable execution approval/event'); END")
    from .paper_exchange import ensure_schema as exchange_schema
    exchange_schema(con)


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
            not 0<levels[0]<levels[1]<=levels[2] or \
            not (levels[3]>levels[2] or plan.get('approval_kind')=='house-mirror' and levels[3]==0):
        raise ValueError('invalid frozen entry/stop/target')
    if not _moment(plan['evidence_at'])<=now<_moment(plan['expires_at']):raise ValueError('future or expired plan evidence')
    if plan.get('approval_kind')=='manual':
        if plan.get('sleeve')!='manual' or plan.get('publication_id') is not None:
            raise ValueError('Research cannot be reclassified as a manual plan')
    elif plan.get('approval_kind')=='house-mirror':
        from .sleeves.config import PRODUCTION_SLEEVES
        from .exit_policy import ExitPolicy
        source=con.execute("SELECT strategy,symbol,shares,stop,target,exit_policy,opened_at FROM v2_positions "
                           "WHERE id=? AND market='IN'",(plan.get('house_position_id'),)).fetchone()
        contract=con.execute("SELECT payload FROM entry_contract_records WHERE scope='house' AND user_id=0 AND position_id=?",
                             (plan.get('house_position_id'),)).fetchone()
        if not source or not contract or source[0] not in PRODUCTION_SLEEVES or \
                (plan['sleeve'],plan['symbol'],plan['stop'],plan['target'],plan.get('exit_policy'))!=(source[0],source[1],source[3],source[4],source[5]) or \
                plan['quantity']>source[2] or json.loads(contract[0])['instrument_id']!=plan['instrument_id']:
            raise ValueError('Mirror approval requires the current owned allowlisted house fill')
        ExitPolicy.decode(plan['exit_policy'])
        if plan['sleeve'] == 'quality_momentum':
            from .screening import automation
            origin = automation.source_fill(con, plan['house_position_id'], plan['house_epoch'])
            if plan.get('model_version') != automation.MODEL_VERSION or plan.get('selective_paper') != origin['selective_paper'] or \
                    plan['entry_low'] != origin['entry_low'] or plan['entry_high'] > origin['entry_high']:
                raise ValueError('Stock mirror must retain the actual fill model and frozen entry zone')
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


def submit_manual_paper(con,catalogue,user_id,symbol,request_key,quotes,*,quantity=None,
                        stop=None,target=None,regime,now=None):
    """Compatibility entry point: a human request freezes a manual approval.

    Explicit levels/quantity are never rebound on a retry. This is not model
    approval and cannot cite or promote research publications. Both approval
    and fill use the same serialized account transaction and contract source.
    """
    from .sleeves.feeds import fresh_quotes
    from .sleeves.base import Candidate
    from .sleeves.risk import RiskManager
    from .instrument_catalog import resolve
    now=now or datetime.now(timezone.utc)
    if type(user_id) is not int or user_id<1 or not isinstance(request_key,str) or not 8<=len(request_key)<=128:
        raise ValueError('Owned stable request identity required')
    if quantity is not None and (type(quantity) is not int or quantity<1):raise ValueError('Whole quantity required')
    for value in (stop,target):
        if value is not None and (isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value)):
            raise ValueError('Finite numeric stop/target required')
    fingerprint=hashlib.sha256(json.dumps([symbol,quantity,stop,target],separators=(',',':'),allow_nan=False).encode()).hexdigest()
    with atomic(con):
        books.ensure_book(con,user_id,'IN');epoch=books.current_epoch(con,user_id,'IN')
        prior=con.execute('SELECT fingerprint,plan_id FROM manual_plan_bindings WHERE user_id=? AND epoch=? AND request_key=?',
                          (user_id,epoch,request_key)).fetchone()
        if prior:
            if prior[0]!=fingerprint:raise ValueError('Request identity belongs to a different manual plan')
            return submit(con,catalogue,user_id,prior[1],request_key,quotes,regime=regime,now=now)
        quote=fresh_quotes(quotes,now).get(symbol)
        if quote is None:raise ValueError('Fresh reference quote unavailable')
        spec,key=resolve(catalogue,symbol=symbol,venue='NSE',segment='NSE_EQ',now=now)
        from .executable_quotes import top
        price,_=top(quote.get('execution'),'BUY',key=key,symbol=symbol,now=now)
        frozen_stop=stop if stop is not None else round(price*.94,2)
        frozen_target=target if target is not None else round(price*1.06,2)
        if not 0<frozen_stop<price<frozen_target:raise ValueError('Stop must be below entry and target above it')
        state,reason=books.risk_state(con,user_id,'IN',quotes)
        allocation=RiskManager().size(Candidate(symbol,'manual',1,price,frozen_stop,target=frozen_target),state) if state and not reason else None
        if not allocation or not allocation.ok:
            raise ValueError(reason or (allocation.reason if allocation else 'Account risk unavailable'))
        frozen_qty=quantity if quantity is not None else allocation.shares
        spec,_,_=execution_contracts.order_contract(catalogue,instrument_id=spec.id,quantity=frozen_qty,price=price,now=now)
        from decimal import Decimal
        # Manual buys are best-ask-limited: a later cheaper ask is acceptable
        # only while it remains strictly above the user's frozen stop.
        entry_low=float(Decimal(str(frozen_stop))+Decimal(str(spec.tick_size)))
        plan=dict(user_id=user_id,market='IN',epoch=epoch,mode='paper',side='BUY',product='D',
                  instrument_id=spec.id,symbol=symbol,quantity=frozen_qty,stop=frozen_stop,
                  entry_low=entry_low,entry_high=price,target=frozen_target,sleeve='manual',
                  model_version='human-manual-v1',approval_kind='manual',publication_id=None,
                  evidence_reference='manual-request:'+request_key,evidence_at=now.isoformat(),
                  expires_at=(now+timedelta(minutes=5)).isoformat())
        plan_id=approve(con,plan,approved_by='account:'+str(user_id),approval_reference='manual-request:'+request_key,now=now)
        con.execute('INSERT INTO manual_plan_bindings VALUES(?,?,?,?,?)',(user_id,epoch,request_key,fingerprint,plan_id))
        return submit(con,catalogue,user_id,plan_id,request_key,quotes,regime=regime,now=now)


def _event(con,plan_id,uid,request_key,kind,payload,now):
    con.execute('INSERT INTO approved_execution_events(plan_id,user_id,request_key,kind,payload,observed_at) VALUES(?,?,?,?,?,?)',
                (plan_id,uid,request_key,kind,json.dumps(payload,sort_keys=True),now.isoformat()))


def submit_house_mirror(con,catalogue,user_id,payload,quotes,*,regime,now):
    """Subscriber copy of an existing allowlisted fill, with independent risk."""
    from . import paper_exchange
    from .instrument_catalog import resolve
    from .sleeves.base import Candidate
    from .sleeves.risk import RiskManager, SLIPPAGE
    from decimal import Decimal, ROUND_FLOOR
    from .executable_quotes import top
    with atomic(con):
        books.ensure_book(con,user_id,'IN');epoch=books.current_epoch(con,user_id,'IN')
        request_key='house-fill:'+str(payload['src_id'])
        prior=con.execute('SELECT id FROM paper_order_intents WHERE user_id=? AND epoch=? AND request_key=?',
                          (user_id,epoch,request_key)).fetchone()
        if prior:return paper_exchange.status(con,user_id,prior[0])
        spec,key=resolve(catalogue,symbol=payload['symbol'],venue='NSE',segment='NSE_EQ',now=now)
        price,_=top((quotes.get(payload['symbol']) or {}).get('execution'),'BUY',key=key,symbol=payload['symbol'],now=now)
        _,rules=execution_contracts._latest(catalogue,'rules',spec.id,now)
        tick=Decimal(str(rules['tick_size']))
        ceiling=float((Decimal(str(payload['price']))*(1+Decimal(str(SLIPPAGE)))/tick).to_integral_value(rounding=ROUND_FLOOR)*tick)
        origin = None
        if (payload['sleeve'] or payload['strategy']) == 'quality_momentum':
            from .screening import automation
            origin = automation.source_fill(con, payload['src_id'], payload['house_epoch'])
            # Signal validity is independent of the filled house slot. This
            # subscriber still has its own risk check below and again at fill.
            source = automation.binding(origin['selective_paper'], payload['symbol'], payload['stop'], payload['target'],
                                        payload['house_epoch'], catalogue, now)
            ceiling = min(ceiling, origin['entry_high'])
            if price < origin['entry_low']:
                raise ValueError('Current ask is below the original stock entry zone')
        if price>ceiling:raise ValueError('Current ask exceeds the source fill slippage allowance')
        state,reason=books.risk_state(con,user_id,'IN',quotes)
        candidate=Candidate(payload['symbol'],payload['sleeve'] or payload['strategy'],1,ceiling,payload['stop'],
            target=payload['target'],allocation_pct=.5 if payload['strategy']=='index_directional' else 0)
        allocation=RiskManager().size(candidate,state) if state and not reason else None
        if not allocation or not allocation.ok:raise ValueError(reason or (allocation.reason if allocation else 'Account risk unavailable'))
        quantity=min(allocation.shares,int(payload['max_shares']))
        if origin:
            automation.check_economics(source, quantity)
        plan=dict(user_id=user_id,market='IN',epoch=epoch,mode='paper',side='BUY',product='D',
            instrument_id=spec.id,symbol=payload['symbol'],quantity=quantity,stop=payload['stop'],target=payload['target'],
            entry_low=float(Decimal(str(payload['stop']))+tick),entry_high=ceiling,
            sleeve=payload['sleeve'] or payload['strategy'],model_version='existing-production-house-mirror-v1',
            approval_kind='house-mirror',house_position_id=payload['src_id'],house_epoch=payload['house_epoch'],
            exit_policy=payload['exit_policy'],evidence_reference='house-fill:'+str(payload['src_id']),
            evidence_at=payload['created_at'],expires_at=(now+timedelta(minutes=5)).isoformat())
        if origin:
            plan.update(entry_low=origin['entry_low'], model_version=origin['model_version'],
                        selective_paper=origin['selective_paper'], source_publication=origin['source_publication'],
                        validation='unvalidated paper trial')
        plan_id=approve(con,plan,approved_by='existing-production-house-fill',
                        approval_reference='current-house-position:'+str(payload['src_id']),now=now)
        return submit(con,catalogue,user_id,plan_id,request_key,quotes,regime=regime,now=now)


def submit(con,catalogue,user_id,plan_id,request_key,quotes,*,regime,now=None):
    """One serialized, exact-quantity account decision; no broker substitution."""
    now=now or datetime.now(timezone.utc)
    if type(user_id) is not int or user_id<1 or not isinstance(request_key,str) or not 8<=len(request_key)<=128:
        raise ValueError('owned request identity required')
    from .recovery_guard import assert_database_execution_allowed
    from .sleeves.feeds import fresh_quotes
    from .instrument_catalog import resolve
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
        from . import paper_exchange
        queued=con.execute('SELECT id,plan_id FROM paper_order_intents WHERE user_id=? AND epoch=? AND request_key=?',
                           (user_id,plan['epoch'],request_key)).fetchone()
        if queued:
            if queued[1]!=plan_id:raise ValueError('Request identity belongs to another plan')
            return paper_exchange.status(con,user_id,queued[0])
        already=con.execute("SELECT payload FROM approved_execution_events WHERE user_id=? AND plan_id=? AND kind='FILLED' LIMIT 1",(user_id,plan_id)).fetchone()
        if already:
            raise ValueError('This approved plan already filled; a new reviewed plan is required')
        reason=None;quote=fresh_quotes(quotes,now).get(plan['symbol']) or {};price=quote.get('price')
        # Last-trade price is a valuation mark. When current depth exists,
        # the executable ask determines zone eligibility for a long intent.
        if quote.get('execution') is not None:
            from .executable_quotes import top
            try:
                _,key=resolve(catalogue,instrument_id=plan['instrument_id'],now=now)
                price,_=top(quote['execution'],'BUY',key=key,symbol=plan['symbol'],now=now)
            except ValueError as exc:reason=str(exc)
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
                if plan['target']:spec.validate_order(plan['quantity'],plan['target'],now=now)
            except ValueError as exc:reason=str(exc)
        if reason is not None:
            result=dict(ok=False,mode='paper',plan_id=plan_id,status='rejected',reason=reason,paper_recorded=False)
            _event(con,plan_id,user_id,request_key,'REJECTED',result,now);return result
        try:
            result=paper_exchange.enqueue(con,user_id,plan_id,request_key,plan,key,quotes,regime=regime,now=now)
        except ValueError as exc:
            result=dict(ok=False,mode='paper',plan_id=plan_id,status='rejected',reason=str(exc),paper_recorded=False)
            _event(con,plan_id,user_id,request_key,'REJECTED',result,now);return result
        _event(con,plan_id,user_id,request_key,'RISK_RESERVED',dict(result=result,contract_evidence=evidence,instrument_id=spec.id),now)
        return result


def report(con,user_id):
    rows=con.execute('SELECT id,payload,approved_by,approval_reference,created_at FROM approved_execution_plans '
                     'WHERE user_id=? ORDER BY created_at DESC LIMIT 50',(user_id,)).fetchall()
    plans=[];now=datetime.now(timezone.utc);epoch=books.current_epoch(con,user_id,'IN')
    for row in rows:
        plan=json.loads(row[1])
        filled=con.execute("SELECT payload FROM approved_execution_events WHERE user_id=? AND plan_id=? AND kind='FILLED' LIMIT 1",(user_id,row[0])).fetchone()
        state='retired' if plan['epoch']!=epoch else 'filled' if filled else 'expired' if _moment(plan['expires_at'])<=now else 'approved'
        order=con.execute('SELECT i.id,s.status FROM paper_order_intents i JOIN paper_order_state s ON s.order_id=i.id '
                          'WHERE i.plan_id=? AND i.user_id=?',(row[0],user_id)).fetchone()
        if order and state!='retired':state=order[1]
        plans.append(dict(id=row[0],plan=plan,status=state,order_id=order[0] if order else None,fill=json.loads(filled[0]) if filled else None,approved_by=row[2],approval_reference=row[3],created_at=row[4]))
    return dict(scope='owned-approved-paper',plans=plans,
                approvals_automatic=False,broker_execution=False)
