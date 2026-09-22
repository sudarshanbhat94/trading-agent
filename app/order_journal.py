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


def ensure_schema(con):
    cols = {r[1] for r in con.execute("PRAGMA table_info(v2_live_orders)")}
    for name, kind in (("intent_key", "TEXT"), ("filled_qty", "INTEGER DEFAULT 0"),
                       ("average_price", "REAL DEFAULT 0"), ("reconciled_at", "TEXT"),
                       ("cancel_requested_at", "TEXT")):
        if name not in cols:
            con.execute(f"ALTER TABLE v2_live_orders ADD COLUMN {name} {kind}")
    con.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_live_intent "
                "ON v2_live_orders(user_id,intent_key) WHERE intent_key IS NOT NULL")
    con.execute("CREATE TABLE IF NOT EXISTS v2_live_protection("
                "user_id INTEGER,symbol TEXT,stop REAL,target REAL,exit_reason TEXT,"
                "PRIMARY KEY(user_id,symbol))")
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
        changed += 1
    con.commit()
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
    rows = list(con.execute(
        "SELECT id,broker_order_id,side,cancel_requested_at FROM v2_live_orders "
        "WHERE user_id=? AND symbol=? AND status IN (?,?,?,?,?)",
        (uid, symbol, *ACTIVE)))
    for rid, oid, side, requested in rows:
        if side != 'BUY' or not oid or requested:
            continue
        cursor = con.execute(
            "UPDATE v2_live_orders SET cancel_requested_at=? WHERE id=? AND user_id=? "
            "AND cancel_requested_at IS NULL AND status IN (?,?,?,?,?)",
            (datetime.now(timezone.utc).isoformat(), rid, uid, *ACTIVE))
        con.commit()
        if cursor.rowcount:
            try:
                broker.cancel_order(uid, oid)
            except Exception:
                pass  # Uncertain cancellation retains the reservation.
    if rows:
        refresh(con, uid)
    return not unresolved(con, uid, symbol)


def submit(con, uid, market, symbol, key, side, qty, reference, product, reason,
           stop=None, target=None):
    """Persist and reserve an intent before transmission; never blind-retry."""
    from . import broker
    if qty < 1 or reference <= 0 or not math.isfinite(reference):
        return "rejected: invalid order"
    st = broker.state(uid)
    if not st.get("live_ready"):
        return "skipped: not armed"
    # Serialize the check/reservation across API requests and the engine.
    con.commit()
    con.execute("BEGIN IMMEDIATE")
    try:
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
            from .sleeves.config import SLEEVES
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
            if stop is not None:
                con.execute("INSERT INTO v2_live_protection(user_id,symbol,stop,target) VALUES(?,?,?,?) "
                            "ON CONFLICT(user_id,symbol) DO UPDATE SET stop=excluded.stop,"
                            "target=excluded.target,exit_reason=NULL", (uid,symbol,stop,target))
        tag = uuid.uuid4().hex[:20]
        con.execute(
            "INSERT INTO v2_live_orders(ts,user_id,market,symbol,instrument_key,side,qty,"
            "price,notional,product,status,reason,intent_key,filled_qty) "
            "VALUES(?,?,?,?,?,?,?,?,?,?, 'pending',?,?,0)",
            (datetime.now(timezone.utc).isoformat(), uid, market, symbol, key, side, qty,
             reference, qty * reference, product, reason, tag))
        con.commit()
    except Exception:
        con.rollback()
        raise
    try:
        result = broker.place_order(uid, key, qty, side, price=0.0, product=product, tag=tag)
        status = "submitted" if result.get("ok") and result.get("order_id") else (
            "rejected" if 400 <= int(result.get("status") or 0) < 500 else "unknown")
    except Exception:
        result, status = {}, "unknown"
    con.execute("UPDATE v2_live_orders SET status=CASE WHEN status='pending' THEN ? ELSE status END,"
                "broker_order_id=COALESCE(broker_order_id,?),response=? "
                "WHERE user_id=? AND intent_key=?",
                (status, result.get("order_id"), json.dumps(result.get("response") or {})[:2000], uid, tag))
    con.commit()
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
