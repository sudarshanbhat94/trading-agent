"""Actual account evidence, distinct from application order acknowledgements.

External holdings are never adopted. Unknown/ambiguous broker data is a
blocked reconciliation, not a zero balance. Evidence is retained per account.
"""
import json
import math
import time
from datetime import datetime, timezone, timedelta
from collections import defaultdict

from .account_safety import atomic
from .execution_outbox import incident


def ensure_schema(con):
    con.execute("CREATE TABLE IF NOT EXISTS broker_reconciliation(user_id INTEGER PRIMARY KEY,"
                "checked_at REAL NOT NULL,status TEXT NOT NULL,available_cash REAL,payload TEXT NOT NULL)")


def _quantity(value):
    if isinstance(value, bool) or not isinstance(value, (int,float)) or not math.isfinite(value) or int(value) != value:
        raise ValueError("invalid inventory quantity")
    return int(value)


def inventory(positions, holdings):
    if not isinstance(positions,list) or not isinstance(holdings,list):
        raise ValueError("inventory evidence unavailable")
    actual = defaultdict(int)
    seen = set()
    for holding in holdings:
        key = holding["instrument_token"]
        if key in seen:
            raise ValueError("ambiguous duplicate holding")
        seen.add(key)
        actual[(key,"D")] += _quantity(holding["quantity"]) + _quantity(holding.get("t1_quantity",0))
    seen.clear()
    for position in positions:
        key, product = position["instrument_token"], position["product"]
        if product not in {"I","D"} or (key,product) in seen:
            raise ValueError("unsupported or duplicate position")
        seen.add((key,product))
        quantity = _quantity(position["quantity"])
        # Delivery holdings are a separate snapshot. Count only today's net
        # delivery movement, not the carried position twice.
        if product == "D":
            quantity -= _quantity(position["overnight_quantity"])
        actual[(key,product)] += quantity
    return {key:q for key,q in actual.items() if q}


def reconcile(con, uid, *, positions, holdings, funds, trades, checked_at=None):
    checked_at = time.time() if checked_at is None else checked_at
    reasons, differences = [], []
    available = None
    try:
        actual = inventory(positions,holdings)
        available = float(funds["data"]["equity"]["available_margin"])
        if not math.isfinite(available) or available < 0 or not isinstance(trades,list):
            raise ValueError("funds/tradebook unavailable")
        managed = {}
        for key,product,q in con.execute("SELECT instrument_key,product,SUM(CASE WHEN side='BUY' "
                                         "THEN filled_qty ELSE -filled_qty END) FROM v2_live_orders "
                                         "WHERE user_id=? GROUP BY instrument_key,product",(uid,)):
            if q:
                managed[(key,product)] = _quantity(q)
        for key in sorted(set(actual) | set(managed)):
            if managed.get(key,0) != actual.get(key,0):
                differences.append(dict(instrument=key[0],product=key[1],managed=managed.get(key,0),
                                        reported=actual.get(key,0)))
        if differences:
            reasons.append("broker inventory differs; external ownership is not adopted")
        # Match actual tradebook rows to our known IDs, deduplicate trade IDs.
        totals, seen = defaultdict(int), set()
        for trade in trades:
            tid = trade["trade_id"]
            oid = str(trade["order_id"])
            if (oid,tid) in seen:
                raise ValueError("duplicate tradebook evidence")
            seen.add((oid,tid))
            owned = con.execute("SELECT instrument_key,side,product,filled_qty FROM v2_live_orders "
                                "WHERE user_id=? AND broker_order_id=?",(uid,oid)).fetchone()
            if not owned or (trade["instrument_token"],trade["transaction_type"],trade["product"]) != tuple(owned[:3]):
                reasons.append("external or mismatched tradebook activity")
                continue
            totals[oid] += _quantity(trade["quantity"])
        today = datetime.fromtimestamp(checked_at,timezone(timedelta(hours=5,minutes=30))).date().isoformat()
        expected_today = con.execute("SELECT broker_order_id,filled_qty FROM v2_live_orders WHERE user_id=? "
                                     "AND filled_qty>0 AND date(ts,'+5 hours','+30 minutes')=?",(uid,today))
        for oid,q in expected_today:
            if not oid or totals.get(str(oid),0) != q:
                reasons.append("today's managed fills are missing from the actual tradebook")
        for oid,q in totals.items():
            filled = con.execute("SELECT filled_qty FROM v2_live_orders WHERE user_id=? AND broker_order_id=?",
                                 (uid,oid)).fetchone()[0]
            if filled != q:
                reasons.append("tradebook and order fills disagree")
        status = "mismatch" if reasons else "ok"
    except (KeyError,TypeError,ValueError,OverflowError):
        status, reasons = "unknown", ["complete, unambiguous broker evidence unavailable"]
    ensure_schema(con)
    with atomic(con):
        payload = dict(reasons=sorted(set(reasons)),differences=differences,
                       managed_only=True,external_adopted=False,fee_certified=False)
        con.execute("INSERT INTO broker_reconciliation VALUES(?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET "
                    "checked_at=excluded.checked_at,status=excluded.status,available_cash=excluded.available_cash,"
                    "payload=excluded.payload",(uid,checked_at,status,available,json.dumps(payload)))
        if status != "ok":
            incident(con,uid,"BROKER_RECONCILIATION",uid,"; ".join(payload["reasons"]))
        else:
            con.execute("UPDATE execution_incidents SET resolved_at=? WHERE user_id=? AND code='BROKER_RECONCILIATION'",
                        (checked_at,uid))
    return dict(status=status,checked_at=checked_at,available_cash=available,**payload)


def refresh(con, uid, port=None):
    from .execution_ports import UpstoxPort
    from . import broker, order_journal
    port = port or UpstoxPort()
    try:
        updates = port.orders(uid)
        if not isinstance(updates,list):
            raise ValueError("order book unavailable")
        order_journal.reconcile(con,uid,updates)
        return reconcile(con,uid,positions=port.positions(uid),holdings=port.holdings(uid),
                         funds=port.funds(uid),trades=port.trades(uid))
    except Exception:
        return reconcile(con,uid,positions=None,holdings=None,funds=None,trades=None)


def ready(con, uid, now=None):
    now = time.time() if now is None else now
    row = con.execute("SELECT checked_at,status FROM broker_reconciliation WHERE user_id=?",(uid,)).fetchone()
    return bool(row and row[1]=='ok' and 0 <= now-row[0] <= 120)
