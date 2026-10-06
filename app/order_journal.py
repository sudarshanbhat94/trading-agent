"""Durable broker intents and fill reconciliation. Acceptance is never a fill.

Unknown submission outcomes reserve exposure until broker evidence resolves
them. Reconciliation is scoped to the user and matches our order ID/tag only;
external account trades are never silently adopted into the managed book.
"""
import json
import math
import sqlite3
import uuid
from datetime import datetime, timezone

ACTIVE = ("pending", "submitted", "partial", "unknown", "sent")
TERMINAL = ("filled", "cancelled", "rejected")
from .live_release import authorized as live_scope_authorized


def ensure_schema(con):
    cols = {r[1] for r in con.execute("PRAGMA table_info(v2_live_orders)")}
    for name, kind in (("intent_key", "TEXT"), ("filled_qty", "INTEGER DEFAULT 0"),
                       ("average_price", "REAL DEFAULT 0"), ("reconciled_at", "TEXT"),
                       ("cancel_requested_at", "TEXT"), ("origin_position_id", "INTEGER"),
                       ("semantic_key", "TEXT"), ("request_fingerprint", "TEXT")):
        if name not in cols:
            con.execute(f"ALTER TABLE v2_live_orders ADD COLUMN {name} {kind}")
    con.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_live_intent "
                "ON v2_live_orders(user_id,intent_key) WHERE intent_key IS NOT NULL")
    con.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_live_semantic ON v2_live_orders(user_id,semantic_key) "
                "WHERE semantic_key IS NOT NULL")
    con.execute("CREATE TABLE IF NOT EXISTS v2_live_protection("
                "user_id INTEGER,symbol TEXT,stop REAL,target REAL,exit_reason TEXT,"
                "PRIMARY KEY(user_id,symbol))")
    con.execute("CREATE TABLE IF NOT EXISTS live_book_epoch("
                "user_id INTEGER,market TEXT,capital REAL NOT NULL,started_at TEXT NOT NULL,"
                "PRIMARY KEY(user_id,market))")
    from . import broker_reconciliation, execution_outbox, protection, execution_events, broker_ledger,entry_contracts
    broker_reconciliation.ensure_schema(con)
    execution_outbox.ensure_schema(con)
    protection.ensure_schema(con)
    execution_events.ensure_schema(con)
    broker_ledger.ensure_schema(con)
    entry_contracts.ensure_schema(con)
    con.commit()


def unresolved(con, uid, symbol=None):
    sql = "SELECT COUNT(*) FROM v2_live_orders WHERE user_id=? AND status IN (?,?,?,?,?)"
    args = [uid, *ACTIVE]
    if symbol:
        sql += " AND symbol=?"
        args.append(symbol)
    return bool(con.execute(sql, args).fetchone()[0])


def reconcile(con, uid, updates):
    """Apply broker snapshots monotonically; repeated snapshots are harmless."""
    from .account_safety import atomic
    from .worker_fencing import require_current
    with atomic(con):
        require_current(con)
        return _reconcile_locked(con,uid,updates)


def _reconcile_locked(con, uid, updates):
    changed = 0
    for update in updates:
        oid, tag = update.get("order_id"), update.get("tag")
        row = con.execute(
            "SELECT id,qty,filled_qty,status,instrument_key,side,product FROM v2_live_orders "
            "WHERE user_id=? AND ((broker_order_id=? AND broker_order_id<>'') "
            "OR (intent_key=? AND intent_key IS NOT NULL AND broker_order_id IS NULL)) ORDER BY id DESC LIMIT 1",
            (uid, oid, tag)).fetchone()
        if not row:
            continue
        rid, requested, previous, old_status, key, side, product = row
        if (update.get("instrument_token") or update.get("instrument_key")) != key or \
                update.get("transaction_type") != side or update.get("product") != product:
            continue
        try:
            raw_quantity = update["filled_quantity"]
            if isinstance(raw_quantity,bool) or not isinstance(raw_quantity,(int,float)) or int(raw_quantity)!=raw_quantity:
                continue
            filled = int(update["filled_quantity"])
            avg = float(update.get("average_price") or 0)
        except (KeyError, TypeError, ValueError):
            continue
        if not 0 <= filled <= requested or filled < (previous or 0) or \
                not math.isfinite(avg) or (filled and avg <= 0):
            continue
        raw = str(update.get("status") or "").lower()
        if filled == requested:
            status = "filled"
        elif raw in ("cancelled", "rejected"):
            status = raw
        else:
            status = "partial" if filled else "submitted"
        # An older acknowledged snapshot must not reopen a terminal order.
        if old_status in TERMINAL and status not in TERMINAL:
            continue
        con.execute("UPDATE v2_live_orders SET filled_qty=?,average_price=?,status=?,"
                    "broker_order_id=COALESCE(?,broker_order_id),reconciled_at=? WHERE id=?",
                    (filled, avg, status, oid, datetime.now(timezone.utc).isoformat(), rid))
        from .execution_events import record
        record(con,uid,rid,'broker-status-observation')
        changed += 1
    if changed:
        from .protection import observe_fills
        observe_fills(con,uid)
    return changed


def refresh(con, uid):
    from . import broker
    try:
        reconcile(con, uid, broker.orders(uid))
        return True
    except Exception:
        # Outage is not proof of zero fills or of rejection.
        return False


def finish_entry_before_exit(con, uid, symbol):
    """Cancel an outstanding entry before selling its confirmed filled part.

    Reserve the cancellation before sending, so concurrent exit passes do not
    repeat it. A timeout or accepted cancellation is never proof of zero
    remaining shares: the sell waits for terminal broker evidence.
    """
    from . import broker
    from .worker_fencing import require_current
    from .account_safety import atomic
    from .recovery_guard import assert_database_execution_allowed
    assert_database_execution_allowed(con)
    if con.in_transaction:
        raise RuntimeError('Entry cancellation requires a committed reservation')
    rows = list(con.execute(
        "SELECT id,broker_order_id,side,cancel_requested_at FROM v2_live_orders "
        "WHERE user_id=? AND symbol=? AND status IN (?,?,?,?,?)",
        (uid, symbol, *ACTIVE)))
    for rid, oid, side, requested in rows:
        if side != 'BUY' or not oid or requested:
            continue
        with atomic(con):
            require_current(con)
            cursor = con.execute(
                "UPDATE v2_live_orders SET cancel_requested_at=? WHERE id=? AND user_id=? "
                "AND cancel_requested_at IS NULL AND status IN (?,?,?,?,?)",
                (datetime.now(timezone.utc).isoformat(), rid, uid, *ACTIVE))
        if cursor.rowcount:
            try:
                broker.cancel_order(uid, oid)
            except Exception:
                pass  # Uncertain cancellation retains the reservation.
    if rows:
        refresh(con, uid)
    return not unresolved(con, uid, symbol)


def submit(con, uid, market, symbol, key, side, qty, reference, product, reason,
           stop=None, target=None, strategy="manual", quotes=None, origin_position_id=None,
           available_cash=None, semantic_key=None, request_fingerprint=None):
    """Persist and reserve an intent before transmission; never blind-retry."""
    from . import broker
    if type(uid) is not int or uid<1:return 'rejected: invalid execution account'
    if isinstance(qty,bool) or not isinstance(qty,int) or qty < 1 or \
            isinstance(reference,bool) or not isinstance(reference,(int,float)) or reference <= 0 or not math.isfinite(reference):
        return "rejected: invalid order"
    if market!='IN' or side not in {'BUY','SELL'} or product not in {'D','I'} or \
            not isinstance(key,str) or not key.startswith('NSE_EQ|') or not key.split('|',1)[1]:
        return "rejected: unsupported execution capability"
    from .recovery_guard import assert_database_execution_allowed
    assert_database_execution_allowed(con)
    from . import protection
    protection.ensure_schema(con)
    if side=='SELL':
        from .execution_ports import UpstoxPort
        if not protection.prepare_exit(con,uid,symbol,UpstoxPort()):
            return 'pending: native protection cancellation or fill reconciliation required'
    st = broker.state(uid)
    if not st.get("live_ready" if side == "BUY" else "exit_ready"):
        return "skipped: not armed" if side == "BUY" else "skipped: exit credentials unavailable"
    if side == "BUY" and (available_cash is None or not math.isfinite(available_cash)):
        return "rejected: broker funds unavailable"
    if side=='BUY':
        allowed,why=live_scope_authorized(uid,product=product,model=strategy)
        if not allowed:return 'rejected: '+why
        from .live_release import native_policy
        if not native_policy(uid,product):return 'rejected: separately reviewed native coverage policy required'
    # Serialize the check/reservation across API requests and the engine.
    con.commit()
    con.execute("BEGIN IMMEDIATE")
    try:
        from .worker_fencing import require_current
        require_current(con)
        semantic_key = semantic_key or (f"house:{origin_position_id}:{side}" if origin_position_id is not None else None)
        if semantic_key:
            previous = con.execute("SELECT status,instrument_key,side,product,qty,request_fingerprint FROM v2_live_orders "
                                   "WHERE user_id=? AND semantic_key=?", (uid,semantic_key)).fetchone()
            if previous:
                con.rollback()
                if tuple(previous[1:5]) != (key,side,product,qty) or previous[5] != request_fingerprint:
                    return "rejected: idempotency key refers to a different intent"
                return previous[0]
        if unresolved(con, uid, symbol if side == "SELL" else None):
            con.rollback()
            return "pending: broker reconciliation required"
        held = con.execute("SELECT COALESCE(SUM(CASE WHEN side='BUY' THEN filled_qty "
                           "ELSE -filled_qty END),0) FROM v2_live_orders WHERE user_id=? AND symbol=?",
                           (uid, symbol)).fetchone()[0]
        if (side == "BUY" and held > 0) or (side == "SELL" and qty > held):
            con.rollback()
            return "rejected: position changed before submission"
        if side == "BUY":
            if protection.blocks_entry(con,uid):
                con.rollback()
                return 'rejected: protection obligations require reconciliation'
            from .broker_reconciliation import ready
            if not ready(con,uid):
                con.rollback()
                return "rejected: actual broker reconciliation is missing, stale or mismatched"
            reconciled_cash = con.execute("SELECT available_cash FROM broker_reconciliation WHERE user_id=?",(uid,)).fetchone()[0]
            available_cash = min(available_cash,float(reconciled_cash))
            from .sleeves.config import SLEEVES
            from .live_trade import account_risk_state
            from .sleeves.base import Candidate
            from .sleeves.risk import RiskManager
            state, why = account_risk_state(con, uid, st, quotes=quotes)
            if state is None:
                con.rollback()
                return "rejected: " + why
            candidate = Candidate(symbol, strategy, 1, reference, float(stop or 0),
                                  target=float(target or 0), product=product)
            allocation = RiskManager().size(candidate, state)
            from .costs import entry_charge
            from .sleeves.risk import SLIPPAGE
            cash_required = qty * reference * (1 + SLIPPAGE)
            cash_required += entry_charge(cash_required, product)
            if not allocation.ok or qty > allocation.shares or qty * reference < SLEEVES.min_ticket:
                con.rollback()
                return "rejected: " + (allocation.reason if not allocation.ok else "account allocation changed")
            if cash_required > available_cash:
                con.rollback()
                return "rejected: broker cash including fees is insufficient"
            positions = con.execute("SELECT symbol,SUM(CASE WHEN side='BUY' THEN filled_qty ELSE -filled_qty END) q "
                                    "FROM v2_live_orders WHERE user_id=? GROUP BY symbol HAVING q>0", (uid,)).fetchall()
            deployed = 0
            for sym, q in positions:
                avg = con.execute("SELECT average_price FROM v2_live_orders WHERE user_id=? AND symbol=? "
                                  "AND side='BUY' AND filled_qty>0 ORDER BY id DESC LIMIT 1", (uid,sym)).fetchone()[0]
                deployed += q * avg
            # Check again under the writer lock; two independently sized
            # requests must not both consume the same remaining cash/cap.
            if deployed + qty * reference > float(st.get("budget") or 0) * SLEEVES.max_deployed or \
                    len(positions) >= broker.MAX_OPEN_POSITIONS:
                con.rollback()
                return "rejected: aggregate live capital cap"
            from . import entry_contracts
            try:contract=entry_contracts.check(market,symbol,qty,reference,stop,target,broker='upstox',product=product,key=key)
            except ValueError as exc:
                con.rollback()
                return 'rejected: '+str(exc)
            if stop is not None:
                con.execute("INSERT INTO v2_live_protection(user_id,symbol,stop,target) VALUES(?,?,?,?) "
                            "ON CONFLICT(user_id,symbol) DO UPDATE SET stop=excluded.stop,"
                            "target=excluded.target,exit_reason=NULL", (uid,symbol,stop,target))
        tag = uuid.uuid4().hex[:20]
        con.execute(
            "INSERT INTO v2_live_orders(ts,user_id,market,symbol,instrument_key,side,qty,"
            "price,notional,product,status,reason,intent_key,filled_qty,origin_position_id,semantic_key,request_fingerprint) "
            "VALUES(?,?,?,?,?,?,?,?,?,?, 'pending',?,?,0,?,?,?)",
            (datetime.now(timezone.utc).isoformat(), uid, market, symbol, key, side, qty,
             reference, qty * reference, product, reason, tag, origin_position_id, semantic_key,request_fingerprint))
        from .execution_events import record
        rid=con.execute('SELECT last_insert_rowid()').fetchone()[0]
        if side=='BUY':entry_contracts.record(con,'broker',uid,rid,contract)
        record(con,uid,rid,'intent-reserved')
        con.commit()
    except Exception:
        con.rollback()
        raise
    try:
        from .execution_ports import UpstoxPort
        result = UpstoxPort().submit(uid,key,qty,side,product=product,tag=tag)
        status = "submitted" if result.get("ok") and result.get("order_id") else (
            "rejected" if 400 <= int(result.get("status") or 0) < 500 else "unknown")
    except Exception:
        result, status = {}, "unknown"
    from .account_safety import atomic
    with atomic(con):
        require_current(con)
        con.execute("UPDATE v2_live_orders SET status=CASE WHEN status='pending' THEN ? ELSE status END,"
                    "broker_order_id=COALESCE(broker_order_id,?),response=? "
                    "WHERE user_id=? AND intent_key=?",
                    (status, result.get("order_id"), json.dumps(result.get("response") or {})[:2000], uid, tag))
        rid=con.execute('SELECT id FROM v2_live_orders WHERE user_id=? AND intent_key=?',(uid,tag)).fetchone()[0]
        record(con,uid,rid,'transmission-outcome')
    return status


def protect(con, uid, symbol, stop, target):
    con.execute("INSERT INTO v2_live_protection(user_id,symbol,stop,target) VALUES(?,?,?,?) "
                "ON CONFLICT(user_id,symbol) DO UPDATE SET stop=excluded.stop,"
                "target=excluded.target,exit_reason=NULL", (uid, symbol, stop, target))
    con.commit()


def request_exit(con, uid, symbol, reason):
    con.execute("INSERT INTO v2_live_protection(user_id,symbol,exit_reason) VALUES(?,?,?) "
                "ON CONFLICT(user_id,symbol) DO UPDATE SET exit_reason=excluded.exit_reason",
                (uid, symbol, reason))
    con.commit()
