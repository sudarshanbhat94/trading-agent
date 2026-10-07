"""Durable approved NSE cash-paper intents, filled on a later exchange event.

This adapter deliberately supports whole-order fills only. It uses at most
10% of displayed best-ask size, shared across accounts per snapshot. It never
inventories a partial fill or predicts an exchange queue position. Reservation
and fill are serialized with the same book/risk/ledger transaction.
"""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json

from .account_safety import atomic
from . import books, executable_quotes

SIMULATION_VERSION = 'nse-top-depth-next-event-aon-v1'


def ensure_schema(con):
    from . import execution_contracts
    execution_contracts.ensure_schema(con)
    con.execute('CREATE TABLE IF NOT EXISTS paper_order_intents('
                'id TEXT PRIMARY KEY,user_id INTEGER NOT NULL,epoch TEXT NOT NULL,plan_id TEXT NOT NULL,'
                'request_key TEXT NOT NULL,payload TEXT NOT NULL,submitted_at TEXT NOT NULL,'
                'UNIQUE(user_id,epoch,request_key),UNIQUE(user_id,epoch,plan_id))')
    con.execute('CREATE TABLE IF NOT EXISTS paper_order_state('
                'order_id TEXT PRIMARY KEY,status TEXT NOT NULL,reason TEXT NOT NULL,result TEXT NOT NULL)')
    con.execute('CREATE TABLE IF NOT EXISTS paper_order_events('
                'id INTEGER PRIMARY KEY,order_id TEXT NOT NULL,kind TEXT NOT NULL,payload TEXT NOT NULL,observed_at TEXT NOT NULL)')
    con.execute('CREATE TABLE IF NOT EXISTS paper_depth_usage('
                'snapshot_id TEXT NOT NULL,side TEXT NOT NULL,quantity INTEGER NOT NULL,PRIMARY KEY(snapshot_id,side))')
    for table in ('paper_order_intents', 'paper_order_events'):
        for operation in ('UPDATE', 'DELETE'):
            con.execute(f'CREATE TRIGGER IF NOT EXISTS immutable_{table}_{operation.lower()} BEFORE {operation} ON {table} '
                        "BEGIN SELECT RAISE(ABORT,'immutable paper exchange evidence'); END")


def sync_sessions(con, catalogue, *, now):
    """Cache current sourced sessions independently of instrument entry rules.

    Copies retain the original immutable evidence identity. An entry catalogue
    outage can use an unexpired cache, but never invent tomorrow's session.
    """
    from . import execution_contracts
    before,after=now-timedelta(seconds=1),now+timedelta(seconds=1)
    rows=catalogue.execute("SELECT id,kind,identity,observed_at,effective_from,effective_until,source,payload "
        "FROM execution_contract_evidence WHERE kind='session' "
        'AND julianday(observed_at)<=julianday(?) AND julianday(effective_from)<=julianday(?) '
        'AND julianday(effective_until)>julianday(?) LIMIT 1001',
        (after.isoformat(),after.isoformat(),before.isoformat())).fetchall()
    if len(rows)>1000:raise ValueError('Sourced session cache exceeds reviewed bounds')
    count=0
    with atomic(con):
        for identity,kind,calendar,observed,start,end,source,text in rows:
            if executable_quotes.moment(observed)>now or not executable_quotes.moment(start)<=now<executable_quotes.moment(end):continue
            copied=execution_contracts.record(con,kind,calendar,json.loads(text),source=source,
                observed_at=observed,effective_from=start,effective_until=end,now=now)
            if copied!=identity:raise ValueError('Sourced session fingerprint differs')
            count+=1
    return count


def _open_session(con, calendar, snapshot, now):
    from . import execution_contracts
    if not calendar:raise ValueError('Original sourced exchange calendar unavailable')
    _,session=execution_contracts._latest(con,'session',calendar,now)
    if not session['open']:raise ValueError('Sourced exchange session is closed')
    opens,closes=map(executable_quotes.moment,(session['opens_at'],session['closes_at']))
    stamp=executable_quotes.moment(snapshot['snapshot_at'])
    if not opens<=stamp<=now<closes:raise ValueError('Executable quote is outside the sourced exchange session')


def _event(con, order_id, kind, payload, now):
    con.execute('INSERT INTO paper_order_events(order_id,kind,payload,observed_at) VALUES(?,?,?,?)',
                (order_id, kind, json.dumps(payload, sort_keys=True, allow_nan=False), now.isoformat()))


def integrity_reason(con, *, allow_filling=True):
    if con.execute('SELECT 1 FROM paper_order_intents i LEFT JOIN paper_order_state s ON s.order_id=i.id '
                   'WHERE s.order_id IS NULL LIMIT 1').fetchone():return 'Paper order state is incomplete; reconcile before new risk'
    if con.execute("SELECT 1 FROM paper_order_state WHERE status NOT IN "
                   "('pending','filling','filled','rejected','cancelled','expired') LIMIT 1").fetchone():
        return 'Unknown paper order outcome; reconcile before new risk'
    if con.execute('SELECT 1 FROM paper_order_state s LEFT JOIN paper_order_intents i ON i.id=s.order_id '
                   'WHERE i.id IS NULL LIMIT 1').fetchone():return 'Orphan paper order outcome; reconcile before new risk'
    if not allow_filling and con.execute("SELECT 1 FROM paper_order_state WHERE status='filling' LIMIT 1").fetchone():
        return 'Interrupted paper fill requires reconciliation before new risk'
    return ''


def _wait(con, row, reason, now, snapshot=None):
    current=status(con,row[1],row[0])
    if current.get('reason')==reason:return
    current['reason']=reason
    con.execute('UPDATE paper_order_state SET reason=?,result=? WHERE order_id=?',(reason,json.dumps(current),row[0]))
    evidence={'reason':reason}
    if isinstance(snapshot,dict):evidence.update(snapshot_id=snapshot.get('snapshot_id'),snapshot_at=snapshot.get('snapshot_at'))
    _event(con,row[0],'WAITING',evidence,now)


def pending(con, user_id=None, epoch=None):
    sql = "SELECT i.id,i.user_id,i.epoch,i.plan_id,i.request_key,i.payload,i.submitted_at FROM paper_order_intents i " \
          "JOIN paper_order_state s ON s.order_id=i.id WHERE s.status='pending' " \
          "AND COALESCE(json_extract(i.payload,'$.plan.side'),'BUY')='BUY'"
    args = []
    if user_id is not None: sql += ' AND i.user_id=?'; args.append(user_id)
    if epoch is not None: sql += ' AND i.epoch=?'; args.append(epoch)
    return con.execute(sql+' ORDER BY i.submitted_at,i.id', args).fetchall()


def reserved_state(con, user_id, epoch, state):
    """Pending commitments affect buying power/risk, never cash or equity P&L."""
    counts, notionals = dict(state.per_sleeve_positions), dict(state.per_sleeve_notional)
    cash = risk = deployed = strategic = 0.0
    rows = pending(con, user_id, epoch)
    for row in rows:
        payload = json.loads(row[5]); sleeve = payload['plan']['sleeve']
        cash += payload['cash_required']; risk += payload['risk_amount']; deployed += payload['notional']
        if sleeve == 'index_directional': strategic += payload['risk_amount']
        counts[sleeve] = counts.get(sleeve, 0)+1
        notionals[sleeve] = notionals.get(sleeve, 0)+payload['notional']
    return replace(state, cash=state.cash-cash, deployed=state.deployed+deployed,
                   open_positions=state.open_positions+len(rows), per_sleeve_positions=counts,
                   per_sleeve_notional=notionals, open_risk=state.open_risk+risk,
                   strategic_open_risk=state.strategic_open_risk+strategic)


def reservation_summary(con, user_id, epoch):
    rows = pending(con, user_id, epoch)
    return dict(pending_orders=len(rows), reserved_cash=round(sum(json.loads(r[5])['cash_required'] for r in rows), 2))


def enqueue(con, user_id, plan_id, request_key, plan, key, quotes, *, regime, now):
    from .sleeves.base import Candidate
    from .sleeves.risk import RiskManager, stop_loss_including_costs, SLIPPAGE
    from .costs import entry_charge
    problem=integrity_reason(con,allow_filling=False)
    if problem:raise ValueError(problem)
    row = con.execute('SELECT id,plan_id FROM paper_order_intents WHERE user_id=? AND epoch=? AND request_key=?',
                      (user_id, plan['epoch'], request_key)).fetchone()
    if row:
        if row[1] != plan_id: raise ValueError('Request identity belongs to another approved plan')
        return status(con, user_id, row[0])
    if con.execute('SELECT 1 FROM paper_order_intents WHERE user_id=? AND epoch=? AND plan_id=?',
                   (user_id, plan['epoch'], plan_id)).fetchone(): raise ValueError('This plan already has an order intent')
    price = plan['entry_high']
    state, reason = books.risk_state(con, user_id, 'IN', quotes)
    candidate = Candidate(plan['symbol'], plan['sleeve'], 1, price, plan['stop'], target=plan['target'],
                          allocation_pct=0.5 if plan['sleeve']=='index_directional' else 0)
    allocation = RiskManager().size(candidate, state) if state and not reason else None
    if not allocation or not allocation.ok or allocation.shares < plan['quantity']:
        raise ValueError(reason or (allocation.reason if allocation and not allocation.ok else 'Approved quantity exceeds account risk allocation'))
    if con.execute("SELECT 1 FROM paper_order_intents i JOIN paper_order_state s ON s.order_id=i.id "
                   "WHERE i.user_id=? AND i.epoch=? AND s.status='pending' AND json_extract(i.payload,'$.plan.symbol')=?",
                   (user_id, plan['epoch'], plan['symbol'])).fetchone() or plan['symbol'] in books.open_symbols(con, user_id, 'IN'):
        raise ValueError('Already held or reserved for this account')
    if plan['quantity']*price < RiskManager().s.min_ticket: raise ValueError('Approved quantity is below the minimum viable ticket')
    notional = plan['quantity']*price
    payload = dict(plan=plan, instrument_key=key, regime_at_submit=regime, notional=notional,
                   risk_amount=stop_loss_including_costs(price, plan['stop'], plan['quantity']),
                   cash_required=notional*(1+SLIPPAGE)+entry_charge(notional*(1+SLIPPAGE)),
                   simulation_version=SIMULATION_VERSION)
    order_id = 'paper_'+hashlib.sha256(json.dumps([user_id, plan['epoch'], request_key], separators=(',', ':')).encode()).hexdigest()
    result = dict(ok=True, mode='paper', order_id=order_id, plan_id=plan_id, status='pending', qty=0,
                  requested_qty=plan['quantity'], paper_recorded=False, reason='Awaiting a later executable exchange snapshot',
                  simulation_version=SIMULATION_VERSION)
    con.execute('INSERT INTO paper_order_intents VALUES(?,?,?,?,?,?,?)',
                (order_id, user_id, plan['epoch'], plan_id, request_key, json.dumps(payload, sort_keys=True), now.isoformat()))
    con.execute('INSERT INTO paper_order_state VALUES(?,?,?,?)', (order_id, 'pending', result['reason'], json.dumps(result)))
    _event(con, order_id, 'RESERVED', payload, now)
    return result


def status(con, user_id, order_id):
    row = con.execute('SELECT s.result,s.status FROM paper_order_state s JOIN paper_order_intents i ON i.id=s.order_id '
                      'WHERE i.id=? AND i.user_id=?', (order_id, user_id)).fetchone()
    if not row: raise ValueError('Paper order not found for this account')
    result=json.loads(row[0])
    if result.get('status')!=row[1]:raise ValueError('Paper order outcome requires reconciliation')
    return result


def _terminal(con, row, kind, reason, now):
    result = dict(ok=False, mode='paper', order_id=row[0], plan_id=row[3], status=kind,
                  qty=0, requested_qty=json.loads(row[5])['plan']['quantity'], paper_recorded=False,
                  reason=reason, simulation_version=SIMULATION_VERSION)
    con.execute('UPDATE paper_order_state SET status=?,reason=?,result=? WHERE order_id=?',
                (kind, reason, json.dumps(result), row[0]))
    _event(con, row[0], kind.upper(), result, now)
    return result


def cancel(con, user_id, order_id, *, now=None):
    from .recovery_guard import assert_database_execution_allowed
    assert_database_execution_allowed(con)
    now = now or datetime.now(timezone.utc)
    with atomic(con):
        current = status(con, user_id, order_id)
        if current['status'] != 'pending': return current  # A filled order cannot be cancelled retroactively.
        row=con.execute('SELECT id,user_id,epoch,plan_id,request_key,payload,submitted_at FROM paper_order_intents '
                        'WHERE id=? AND user_id=?',(order_id,user_id)).fetchone()
        if json.loads(row[5])['plan'].get('side')=='SELL':
            raise ValueError('Exit protection remains active; cancelling an owned exit is unsupported')
        return _terminal(con, row, 'cancelled', 'Cancelled by account owner', now)


def service(con, catalogue, quotes, *, regime, now=None):
    """No network; the production worker passes its current canonical evidence."""
    from . import approved_execution, entry_contracts, execution_contracts
    from .recovery_guard import assert_database_execution_allowed
    from .worker_fencing import require_current
    assert_database_execution_allowed(con)
    now = now or datetime.now(timezone.utc); outcomes = []
    with atomic(con):
        require_current(con)
        problem=integrity_reason(con,allow_filling=False)
        if problem:raise ValueError(problem)
        sync_sessions(con,catalogue,now=now)
        for row in pending(con):
            order_id, uid, epoch, plan_id, request_key, text, submitted_at = row
            if uid == 0: continue  # House proposals use the same journal; separate owned posting.
            payload = json.loads(text); plan = payload['plan']
            if epoch != books.current_epoch(con, uid, 'IN'):
                outcomes.append(_terminal(con, row, 'cancelled', 'Paper epoch changed', now)); continue
            if plan.get('approval_kind')=='house-mirror':
                source=con.execute("SELECT 1 FROM v2_positions WHERE id=? AND market='IN'",(plan['house_position_id'],)).fetchone()
                if not source:
                    outcomes.append(_terminal(con,row,'cancelled','Origin house position already closed',now));continue
            if now >= executable_quotes.moment(plan['expires_at']):
                outcomes.append(_terminal(con, row, 'expired', 'Approved entry window expired', now)); continue
            if regime not in {'ON', 'NEUTRAL'} or plan['sleeve']=='index_directional' and regime!='ON':
                outcomes.append(_terminal(con, row, 'rejected', 'Current regime blocks new equity longs', now)); continue
            snapshot = (quotes.get(plan['symbol']) or {}).get('execution')
            try:
                price, displayed = executable_quotes.top(snapshot, 'BUY', key=payload['instrument_key'],
                    symbol=plan['symbol'], now=now, after=executable_quotes.moment(submitted_at))
            except (ValueError, KeyError, TypeError) as exc:
                _wait(con,row,str(exc) if isinstance(exc,ValueError) else 'Executable quote evidence incomplete',now);continue
            if not plan['entry_low'] <= price <= plan['entry_high']:
                _wait(con,row,'Executable ask is outside the frozen entry zone',now,snapshot);continue
            used = con.execute("SELECT quantity FROM paper_depth_usage WHERE snapshot_id=? AND side='BUY'", (snapshot['snapshot_id'],)).fetchone()
            if displayed//10-(used[0] if used else 0) < plan['quantity']:
                _wait(con,row,'Insufficient unconsumed displayed ask liquidity for a whole-order fill',now,snapshot);continue
            try:
                spec, key, evidence = execution_contracts.order_contract(catalogue, instrument_id=plan['instrument_id'],
                                          quantity=plan['quantity'], price=price, now=now)
                if key != payload['instrument_key'] or spec.symbol != plan['symbol']: raise ValueError('Canonical identity changed')
                _open_session(con,evidence['calendar'],snapshot,now)
            except ValueError as exc:
                outcomes.append(_terminal(con, row, 'rejected', str(exc), now)); continue
            # Remove only this reservation inside the same write transaction.
            # Rollback restores it if the fill/ledger/event fails.
            con.execute("UPDATE paper_order_state SET status='filling' WHERE order_id=?", (order_id,))
            with entry_contracts.using(catalogue, now):
                quantity = books.buy(con, uid, 'IN', plan['sleeve'], plan['symbol'], price, plan['quantity'],
                    plan['stop'], plan['target'], src_id=plan.get('house_position_id'),
                    exit_policy=plan.get('exit_policy'), sleeve=plan['sleeve'], regime=regime, quotes=quotes,
                    request_key='approved:'+request_key, exact_quantity=True)
            if not quantity:
                outcomes.append(_terminal(con, row, 'rejected', books.refusal(con, uid, 'IN'), now)); continue
            receipt = books.entry_receipt(con, uid, 'IN', 'approved:'+request_key)
            con.execute('UPDATE user_positions SET instrument_id=?,plan_id=?,model_version=? WHERE id=? AND user_id=?',
                        (spec.id, plan_id, plan['model_version'], receipt['position_id'], uid))
            con.execute("INSERT INTO paper_depth_usage VALUES(?,'BUY',?) ON CONFLICT(snapshot_id,side) "
                        'DO UPDATE SET quantity=quantity+excluded.quantity', (snapshot['snapshot_id'], quantity))
            result = dict(ok=True, mode='paper', order_id=order_id, plan_id=plan_id, status='filled',
                          position_id=receipt['position_id'], instrument_id=spec.id, entry=receipt['entry'], qty=quantity,
                          paper_recorded=True, protection='application-paper', model_version=plan['model_version'],
                          simulation_version=SIMULATION_VERSION)
            con.execute("UPDATE paper_order_state SET status='filled',reason='',result=? WHERE order_id=?", (json.dumps(result), order_id))
            _event(con, order_id, 'FILLED', dict(result=result, snapshot=snapshot), now)
            approved_execution._event(con, plan_id, uid, request_key, 'FILLED', result, now)
            outcomes.append(result)
    return outcomes


def house_state(con, quotes, now):
    """Revalue the house under the same allocator, using current-epoch history."""
    from . import v2_live, account_safety
    from .sleeves.feeds import fresh_quotes
    from .sleeves.risk import BookState, stop_loss_including_costs
    from .live_trade import product_for
    from zoneinfo import ZoneInfo
    row = con.execute("SELECT budget,started_at FROM v2_book WHERE market='IN'").fetchone()
    if not row: raise ValueError('House book unavailable')
    capital, epoch = float(row[0]), row[1]
    positions = con.execute("SELECT symbol,shares,entry_price,stop,COALESCE(sleeve,strategy),"
                            "COALESCE(entry_fee,0),entry_date FROM v2_positions WHERE market='IN'").fetchall()
    marks = fresh_quotes(quotes, now); day = now.astimezone(ZoneInfo('Asia/Kolkata')).date().isoformat()
    counts, notionals = {}, {}; risk = strategic = fees = value = 0.0
    for symbol, qty, entry, stop, sleeve, fee, opened in positions:
        if symbol not in marks or not stop or stop <= 0: raise ValueError('House held valuation or stop unavailable')
        mark = marks[symbol]['price']; value += qty*mark; fees += fee
        counts[sleeve] = counts.get(sleeve, 0)+1
        notionals[sleeve] = notionals.get(sleeve, 0)+qty*entry
        loss = stop_loss_including_costs(mark, min(mark, stop), qty, product_for(sleeve)); risk += loss
        if sleeve == 'index_directional': strategic += loss
    deployed = sum(notionals.values()); cash = capital-deployed-fees+v2_live._epoch_pnl(con, 'IN')
    equity = cash+value
    from .sleeves.accounting import session_pnl
    day_pnl = session_pnl(con, 'IN', equity, day, epoch, capital, bool(positions))
    if day_pnl is None: raise ValueError('House daily equity baseline unavailable')
    historical = con.execute("SELECT COALESCE(MAX(equity),?) FROM v2_equity WHERE market='IN' "
                             "AND julianday(substr(date,6))>=julianday(?)", (capital, epoch)).fetchone()[0]
    peak = account_safety.peak(con, 'house', 0, 'IN', epoch, max(capital, equity, historical))
    if con.in_transaction:
        account_safety.observe(con, 'house', 0, 'IN', epoch, equity, capital, day,
                               equity-day_pnl, historical_peak=peak)
    state = BookState(capital, cash, deployed, len(positions), counts, equity, peak, day_pnl,
                      notionals, risk, strategic)
    return reserved_state(con, 0, epoch, state), epoch


def enqueue_house(con, catalogue, allocation, quotes, *, regime, now):
    """Freeze an existing allowlisted sleeve proposal, never promote a stock model."""
    from .sleeves.config import PRODUCTION_SLEEVES
    from .sleeves.risk import RiskManager, stop_loss_including_costs, SLIPPAGE
    from . import execution_contracts
    from .instrument_catalog import resolve
    from .costs import entry_charge
    from decimal import Decimal, ROUND_FLOOR
    candidate = allocation.candidate
    if regime not in {'ON','NEUTRAL'} or candidate.sleeve not in PRODUCTION_SLEEVES or candidate.instrument != 'EQ' or \
            candidate.sleeve=='index_directional' and regime!='ON':
        raise ValueError('Unsupported or unpromoted house proposal')
    if candidate.sleeve=='index_directional':
        from .sleeves.index_directional import SYMBOL
        if candidate.symbol!=SYMBOL:raise ValueError('Instrument is outside the production index model')
    with atomic(con):
        from .worker_fencing import require_current
        from .recovery_guard import assert_database_execution_allowed
        assert_database_execution_allowed(con); require_current(con)
        problem=integrity_reason(con,allow_filling=False)
        if problem:raise ValueError(problem)
        state, epoch = house_state(con, quotes, now)
        spec, key = resolve(catalogue, symbol=candidate.symbol, venue='NSE', segment='NSE_EQ', now=now)
        _, rules = execution_contracts._latest(catalogue, 'rules', spec.id, now)
        tick = Decimal(str(rules['tick_size']))
        ceiling = float((Decimal(str(candidate.entry))*(1+Decimal(str(SLIPPAGE)))/tick).to_integral_value(rounding=ROUND_FLOOR)*tick)
        sized = RiskManager().size(replace(candidate, entry=ceiling), state)
        if not sized.ok: raise ValueError(sized.reason)
        quantity = min(int(allocation.shares), sized.shares)
        if quantity*ceiling < RiskManager().s.min_ticket: raise ValueError('House ticket below account minimum')
        spec, key, evidence = execution_contracts.order_contract(catalogue, instrument_id=spec.id,
                                                quantity=quantity, price=ceiling, now=now)
        spec.validate_order(quantity, candidate.stop, now=now)
        if candidate.target: spec.validate_order(quantity, candidate.target, now=now)
        from zoneinfo import ZoneInfo
        day = now.astimezone(ZoneInfo('Asia/Kolkata')).date().isoformat()
        semantic = ':'.join(('IN', epoch, candidate.sleeve, candidate.symbol, day))
        if con.execute('SELECT 1 FROM house_entry_intents WHERE semantic_key=?', (semantic,)).fetchone():
            raise ValueError('House proposal already filled in this session')
        if con.execute("SELECT 1 FROM paper_order_intents i JOIN paper_order_state s ON s.order_id=i.id "
                       "WHERE i.user_id=0 AND i.epoch=? AND s.status='pending' "
                       "AND json_extract(i.payload,'$.plan.symbol')=?", (epoch, candidate.symbol)).fetchone():
            raise ValueError('House symbol already reserved')
        plan = dict(side='BUY',symbol=candidate.symbol, sleeve=candidate.sleeve, quantity=quantity,
                    stop=candidate.stop, target=candidate.target, trail=candidate.trail_pct,
                    score=candidate.score, why=candidate.why, max_hold_days=candidate.max_hold_days,
                    entry_low=float(Decimal(str(candidate.stop))+tick), entry_high=ceiling,
                    instrument_id=spec.id, epoch=epoch, model_version='existing-production-sleeve-v1',
                    expires_at=(now+timedelta(minutes=5)).isoformat())
        request_key = 'house-proposal:'+hashlib.sha256((semantic+now.isoformat()).encode()).hexdigest()
        order_id = 'paper_'+hashlib.sha256(request_key.encode()).hexdigest()
        notional = quantity*ceiling
        payload = dict(plan=plan, instrument_key=key, regime_at_submit=regime, notional=notional,
            risk_amount=stop_loss_including_costs(ceiling, candidate.stop, quantity),
            cash_required=notional*(1+SLIPPAGE)+entry_charge(notional*(1+SLIPPAGE)),
            simulation_version=SIMULATION_VERSION, contract_evidence=evidence)
        result = dict(ok=True, mode='paper', order_id=order_id, plan_id='houseplan_'+order_id[6:], status='pending',
                      qty=0, requested_qty=quantity, paper_recorded=False, reason='Awaiting a later executable exchange snapshot')
        con.execute('INSERT INTO paper_order_intents VALUES(?,?,?,?,?,?,?)',
                    (order_id, 0, epoch, result['plan_id'], request_key, json.dumps(payload, sort_keys=True), now.isoformat()))
        con.execute('INSERT INTO paper_order_state VALUES(?,?,?,?)', (order_id, 'pending', result['reason'], json.dumps(result)))
        _event(con, order_id, 'RESERVED', payload, now)
        return result


def service_house(con, catalogue, quotes, *, regime, now):
    from . import v2_live, execution_contracts, entry_contracts
    from .sleeves.base import Candidate
    from .sleeves.risk import RiskManager, stop_loss_including_costs
    from .sleeves.config import PRODUCTION_SLEEVES
    from .worker_fencing import require_current
    from .recovery_guard import assert_database_execution_allowed
    assert_database_execution_allowed(con); outcomes=[]
    with atomic(con):
        require_current(con)
        problem=integrity_reason(con,allow_filling=False)
        if problem:raise ValueError(problem)
        sync_sessions(con,catalogue,now=now)
        for row in pending(con, 0):
            payload=json.loads(row[5]); plan=payload['plan']; uid=0; order_id=row[0]
            epoch_row=con.execute("SELECT started_at FROM v2_book WHERE market='IN'").fetchone()
            if not epoch_row or epoch_row[0]!=row[2]:
                outcomes.append(_terminal(con,row,'cancelled','House epoch changed',now));continue
            if now>=executable_quotes.moment(plan['expires_at']):
                outcomes.append(_terminal(con,row,'expired','House proposal expired',now));continue
            if regime not in {'ON','NEUTRAL'} or plan['sleeve'] not in PRODUCTION_SLEEVES or \
                    plan['sleeve']=='index_directional' and regime!='ON':
                outcomes.append(_terminal(con,row,'rejected','Regime or production sleeve gate blocks entry',now));continue
            snapshot=(quotes.get(plan['symbol']) or {}).get('execution')
            try:
                price,displayed=executable_quotes.top(snapshot,'BUY',key=payload['instrument_key'],symbol=plan['symbol'],
                                                     now=now,after=executable_quotes.moment(row[6]))
            except (ValueError,KeyError,TypeError) as exc:
                _wait(con,row,str(exc) if isinstance(exc,ValueError) else 'Executable quote evidence incomplete',now);continue
            if not plan['entry_low']<=price<=plan['entry_high']:
                _wait(con,row,'Executable ask is outside the frozen entry zone',now,snapshot);continue
            used=con.execute("SELECT quantity FROM paper_depth_usage WHERE snapshot_id=? AND side='BUY'",(snapshot['snapshot_id'],)).fetchone()
            if displayed//10-(used[0] if used else 0)<plan['quantity']:
                _wait(con,row,'Insufficient unconsumed displayed ask liquidity for a whole-order fill',now,snapshot);continue
            try:
                spec,key,evidence=execution_contracts.order_contract(catalogue,instrument_id=plan['instrument_id'],quantity=plan['quantity'],price=price,now=now)
                if key!=payload['instrument_key'] or spec.symbol!=plan['symbol']:raise ValueError('House canonical identity changed')
                _open_session(con,evidence['calendar'],snapshot,now)
                con.execute("UPDATE paper_order_state SET status='filling' WHERE order_id=?",(order_id,))
                state,_=house_state(con,quotes,now)
                candidate=Candidate(plan['symbol'],plan['sleeve'],plan['score'],price,plan['stop'],target=plan['target'],
                    allocation_pct=.5 if plan['sleeve']=='index_directional' else 0)
                allocation=RiskManager().size(candidate,state)
                if not allocation.ok or allocation.shares<plan['quantity']:raise ValueError('House risk changed before fill')
            except ValueError as exc:
                outcomes.append(_terminal(con,row,'rejected',str(exc),now));continue
            from zoneinfo import ZoneInfo
            day=now.astimezone(ZoneInfo('Asia/Kolkata')).date().isoformat()
            with entry_contracts.using(catalogue,now):
                filled=v2_live.record_entry(con,'IN',plan['sleeve'],plan['symbol'],day,price,plan['quantity'],plan['stop'],
                    plan['target'],plan['trail'],plan['score'],json.dumps(plan['why']),sleeve=plan['sleeve'],regime=regime,
                    risk_amount=stop_loss_including_costs(price,plan['stop'],plan['quantity']),max_hold_days=plan['max_hold_days'])
            if not filled:
                outcomes.append(_terminal(con,row,'rejected','House entry writer refused',now));continue
            pid=con.execute("SELECT id FROM v2_positions WHERE market='IN' AND symbol=? ORDER BY id DESC LIMIT 1",(plan['symbol'],)).fetchone()[0]
            con.execute("INSERT INTO paper_depth_usage VALUES(?,'BUY',?) ON CONFLICT(snapshot_id,side) DO UPDATE SET quantity=quantity+excluded.quantity",
                        (snapshot['snapshot_id'],plan['quantity']))
            result=dict(ok=True,mode='paper',order_id=order_id,plan_id=row[3],status='filled',qty=plan['quantity'],
                        entry=price,position_id=pid,paper_recorded=True,protection='application-paper',simulation_version=SIMULATION_VERSION)
            con.execute("UPDATE paper_order_state SET status='filled',reason='',result=? WHERE order_id=?",(json.dumps(result),order_id))
            _event(con,order_id,'FILLED',dict(result=result,snapshot=snapshot),now);outcomes.append(result)
    return outcomes


def managed_position(con, user_id, position_id):
    """Only the new simulated exchange's fills change execution semantics."""
    return bool(con.execute("SELECT 1 FROM paper_order_intents i JOIN paper_order_state s ON s.order_id=i.id "
        "WHERE i.user_id=? AND s.status='filled' AND json_extract(s.result,'$.position_id')=? "
        "AND COALESCE(json_extract(i.payload,'$.plan.side'),'BUY')='BUY' LIMIT 1",(user_id,position_id)).fetchone())


def queue_exit(con, user_id, position_id, reason, *, now=None):
    """Freeze an owned full-position sell. No entry regime or subscription gate."""
    from .recovery_guard import assert_database_execution_allowed
    from .worker_fencing import require_current
    assert_database_execution_allowed(con);now=now or datetime.now(timezone.utc)
    with atomic(con):
        require_current(con)
        scope='house' if user_id==0 else 'personal'
        if user_id==0:
            row=con.execute("SELECT symbol,shares,stop,target,COALESCE(sleeve,strategy),regime FROM v2_positions "
                            "WHERE id=? AND market='IN'",(position_id,)).fetchone()
            epoch=(con.execute("SELECT started_at FROM v2_book WHERE market='IN'").fetchone() or [None])[0]
        else:
            epoch=books.current_epoch(con,user_id,'IN')
            row=con.execute("SELECT symbol,shares,stop,target,COALESCE(sleeve,strategy),regime FROM user_positions "
                            "WHERE id=? AND user_id=? AND market='IN' AND book_epoch=?",(position_id,user_id,epoch)).fetchone()
        if not row or not managed_position(con,user_id,position_id):raise ValueError('Owned exchange-simulated position unavailable')
        prior=con.execute("SELECT i.id FROM paper_order_intents i JOIN paper_order_state s ON s.order_id=i.id "
            "WHERE i.user_id=? AND i.epoch=? AND json_extract(i.payload,'$.plan.side')='SELL' "
            "AND json_extract(i.payload,'$.plan.position_id')=? AND s.status='pending'",(user_id,epoch,position_id)).fetchone()
        if prior:return status(con,user_id,prior[0])
        contract=con.execute('SELECT payload FROM entry_contract_records WHERE scope=? AND user_id=? AND position_id=?',
                             (scope,user_id,position_id)).fetchone()
        if not contract:raise ValueError('Original canonical position contract unavailable')
        contract=json.loads(contract[0]);quantity=int(row[1])
        if quantity!=row[1] or quantity<=0:raise ValueError('Whole owned position quantity required')
        plan=dict(side='SELL',symbol=row[0],quantity=quantity,stop=row[2],target=row[3],sleeve=row[4],
                  calendar=contract.get('contract_evidence',{}).get('calendar'),
                  instrument_id=contract['instrument_id'],position_id=position_id,epoch=epoch,exit_reason=reason)
        request_key=f'owned-exit:{user_id}:{epoch}:{position_id}:{now.isoformat()}'
        order_id='paper_'+hashlib.sha256(request_key.encode()).hexdigest()
        payload=dict(plan=plan,instrument_key=contract['broker_key'],regime_at_submit=row[5],simulation_version=SIMULATION_VERSION)
        result=dict(ok=True,mode='paper',order_id=order_id,plan_id='exitplan_'+order_id[6:],status='pending',side='SELL',
                    qty=0,requested_qty=quantity,paper_recorded=False,reason='Exit requested; awaiting a later executable bid')
        con.execute('INSERT INTO paper_order_intents VALUES(?,?,?,?,?,?,?)',
                    (order_id,user_id,epoch,result['plan_id'],request_key,json.dumps(payload,sort_keys=True),now.isoformat()))
        con.execute('INSERT INTO paper_order_state VALUES(?,?,?,?)',(order_id,'pending',result['reason'],json.dumps(result)))
        _event(con,order_id,'EXIT_REQUESTED',payload,now);return result


def exit_pending(con):
    return con.execute("SELECT i.id,i.user_id,i.epoch,i.plan_id,i.request_key,i.payload,i.submitted_at FROM paper_order_intents i "
                       "JOIN paper_order_state s ON s.order_id=i.id WHERE s.status='pending' "
                       "AND json_extract(i.payload,'$.plan.side')='SELL' ORDER BY i.submitted_at,i.id").fetchall()


def service_exits(con, quotes, *, now=None):
    """Exits continue through OFF/disarm; a gap fills at the available bid."""
    from . import v2_live
    from .recovery_guard import assert_database_execution_allowed
    from .worker_fencing import require_current
    from zoneinfo import ZoneInfo
    assert_database_execution_allowed(con);now=now or datetime.now(timezone.utc);outcomes=[]
    with atomic(con):
        require_current(con)
        for row in exit_pending(con):
            order_id,uid,epoch,plan_id,request_key,text,submitted_at=row;payload=json.loads(text);plan=payload['plan']
            if uid==0:
                current=(con.execute("SELECT started_at FROM v2_book WHERE market='IN'").fetchone() or [None])[0]
                position=con.execute("SELECT shares FROM v2_positions WHERE id=? AND market='IN'",(plan['position_id'],)).fetchone()
            else:
                current=books.current_epoch(con,uid,'IN')
                position=con.execute("SELECT shares FROM user_positions WHERE id=? AND user_id=? AND book_epoch=?",
                                     (plan['position_id'],uid,epoch)).fetchone()
            if current!=epoch or not position:
                outcomes.append(_terminal(con,row,'cancelled','Owned position or epoch no longer exists',now));continue
            if position[0]!=plan['quantity']:
                outcomes.append(_terminal(con,row,'rejected','Owned inventory changed; exit requires reconciliation',now));continue
            snapshot=(quotes.get(plan['symbol']) or {}).get('execution')
            try:price,displayed=executable_quotes.top(snapshot,'SELL',key=payload['instrument_key'],symbol=plan['symbol'],
                                                    now=now,after=executable_quotes.moment(submitted_at))
            except (ValueError,KeyError,TypeError) as exc:
                _wait(con,row,str(exc) if isinstance(exc,ValueError) else 'Executable quote evidence incomplete',now);continue
            try:_open_session(con,plan.get('calendar'),snapshot,now)
            except (ValueError,KeyError,TypeError) as exc:
                _wait(con,row,str(exc) if isinstance(exc,ValueError) else 'Sourced exchange session unavailable',now);continue
            used=con.execute("SELECT quantity FROM paper_depth_usage WHERE snapshot_id=? AND side='SELL'",(snapshot['snapshot_id'],)).fetchone()
            if displayed//10-(used[0] if used else 0)<plan['quantity']:
                _wait(con,row,'Insufficient unconsumed displayed bid liquidity; owned position remains open',now,snapshot);continue
            if uid==0:
                pnl,pct=v2_live.record_exit(con,'IN',plan['position_id'],now.astimezone(ZoneInfo('Asia/Kolkata')).date().isoformat(),
                                            price,plan['quantity'],plan['exit_reason'])
            else:
                settled=books._sell_locked(con,uid,'IN',plan['symbol'],price,plan['exit_reason'],plan['position_id'])
                if settled is None:raise ValueError('Owned exit changed inside the serialized fill')
                pnl,pct=settled
            con.execute("INSERT INTO paper_depth_usage VALUES(?,'SELL',?) ON CONFLICT(snapshot_id,side) "
                        'DO UPDATE SET quantity=quantity+excluded.quantity',(snapshot['snapshot_id'],plan['quantity']))
            result=dict(ok=True,mode='paper',order_id=order_id,plan_id=plan_id,status='filled',side='SELL',
                        qty=plan['quantity'],entry=price,exit=price,pnl=pnl,pnl_pct=pct,paper_recorded=True,
                        simulation_version=SIMULATION_VERSION)
            con.execute("UPDATE paper_order_state SET status='filled',reason='',result=? WHERE order_id=?",(json.dumps(result),order_id))
            _event(con,order_id,'FILLED',dict(result=result,snapshot=snapshot),now);outcomes.append(result)
    return outcomes
