"""Mirror the paper book's decisions into REAL broker orders.

SHAPE OF THE DESIGN
The live sleeve does not think. It shadows the paper book: when the engine opens
a position, this places the same buy with real money; when the engine closes
one, this sells. Two consequences, both deliberate:

  * there is only ONE set of trading decisions to reason about, review and
    blame. A second, independently-deciding live strategy would double the
    surface area and make the paper record stop being evidence about the live
    one.
  * the paper book remains a complete, uninterrupted record. It is the control
    group. If the live sleeve diverges from it, the difference is execution —
    slippage, rejects, partial fills — and that is exactly the quantity worth
    measuring, because the exit study says execution is where the edge dies.

SIZE IS NOT MIRRORED. Paper runs Rs 1,00,000 across 6 slots; the sleeve runs
Rs 10,000 across 3. Copying paper quantities would put ~Rs 16,000 orders into a
Rs 10,000 account. Every order is re-sized to the sleeve.

WHAT CANNOT HAPPEN HERE
  * an order for a symbol with no Upstox instrument key. 10,377 of the 13,036
    enabled symbols have none, and a guessed key is an order for the wrong
    stock. Unresolvable means SKIPPED, and the skip is recorded.
  * a buy that exceeds the real available margin. The cap is the LOWER of the
    configured sleeve and what the broker says is actually there.
  * selling more than the sleeve actually holds. Live quantity is derived from
    this module's own filled orders, never from the paper position's size.
  * any order at all while disarmed — every path re-checks broker.can_trade.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone, timedelta

IST = timezone(timedelta(hours=5, minutes=30))
_LOG = logging.getLogger("openstocks.live")

# Lanes the sleeve is allowed to mirror. gap_momentum is a measured net loser
# and quarantined from the paper book already; it must not reappear here.
MIRRORED_LANES = ("swing_meanrev", "mom_breakout", "volume_surge", "btst", "manual")

_margin_cache: dict = {}
MARGIN_TTL = 60


def available_margin(user_id, force=False):
    """Real cash at the broker, cached. None when the broker cannot be reached.

    None is NOT treated as "plenty" by the callers below — an unknown balance
    blocks new buys, because the alternative is discovering the balance by
    having an order rejected.
    """
    now = time.time()
    # cache PER USER: one shared slot would hand one person's balance to
    # another's sizing calculation
    slot = _margin_cache.setdefault(int(user_id), {"t": 0.0, "v": None})
    if not force and slot["v"] is not None and now - slot["t"] < MARGIN_TTL:
        return slot["v"]
    try:
        from . import broker
        data = (broker.funds(user_id) or {}).get("data") or {}
        eq = data.get("equity") or {}
        val = float(eq.get("available_margin"))
        slot.update(t=now, v=val)
        return val
    except Exception as exc:
        _LOG.warning("could not read broker margin for %s: %s", user_id, exc)
        slot.update(t=now, v=None)
        return None


def instrument_key(main_db, symbol):
    """Upstox key for an NSE equity, or None.

    None means DO NOT TRADE. Two thirds of the universe has no key, and there is
    no safe way to invent one — an instrument key names a specific listed
    security, so a wrong guess is a real order for the wrong company.
    """
    try:
        row = main_db.execute(
            "SELECT upstox_instrument_key FROM universe WHERE symbol=? AND enabled=1",
            (str(symbol),)).fetchone()
    except Exception:
        return None
    key = (row or [None])[0]
    return key if key and str(key).strip() else None


def live_qty(v2, user_id, symbol):
    """Shares the SLEEVE holds, from its own filled orders.

    Derived rather than stored: a position table can drift out of step with what
    the broker actually did, and the order ledger is the thing that was really
    sent. Never read from the paper position — its size is 10x this.
    """
    row = v2.execute(
        "SELECT COALESCE(SUM(CASE WHEN side='BUY' THEN qty ELSE -qty END),0)"
        " FROM v2_live_orders WHERE symbol=? AND status='sent' AND user_id=?",
        (str(symbol), int(user_id))).fetchone()
    return int(row[0] or 0)


def open_symbols(v2, user_id):
    rows = v2.execute(
        "SELECT symbol, SUM(CASE WHEN side='BUY' THEN qty ELSE -qty END) q"
        " FROM v2_live_orders WHERE status='sent' AND user_id=?"
        " GROUP BY symbol HAVING q>0", (int(user_id),))
    return {r[0]: int(r[1]) for r in rows}


def day_notional(v2, user_id, today_s=None):
    today_s = today_s or datetime.now(IST).date().isoformat()
    row = v2.execute("SELECT COALESCE(SUM(notional),0) FROM v2_live_orders"
                     " WHERE substr(ts,1,10)=? AND status='sent' AND side='BUY'"
                     " AND user_id=?", (today_s, int(user_id))).fetchone()
    return float(row[0] or 0.0)


def size_for_sleeve(price, st, margin=None):
    """Whole shares for ONE live position.

    Target notional is the sleeve split across its maximum position count, so a
    full book is fully invested and no single name can dominate. Then clamped by
    the per-order cap and by real margin.
    """
    from . import broker
    price = float(price or 0)
    if price <= 0:
        return 0
    budget = float(st.get("budget") or 0)
    target = budget / max(1, broker.MAX_OPEN_POSITIONS)
    target = min(target, float(st.get("max_order") or 0))
    if margin is not None:
        target = min(target, float(margin))
    return max(0, int(target // price))


def _record(v2, user_id, market, symbol, key, side, qty, price, status, reason,
            response=None, order_id=None):
    """user_id is REQUIRED and positional on purpose.

    It was a trailing keyword defaulting to None, so every call that forgot it
    wrote a ledger row belonging to nobody — invisible to that user's own
    history and to the per-user caps. A required positional turns a missed call
    into a TypeError at import time instead of a silent orphan row.
    """
    v2.execute(
        "INSERT INTO v2_live_orders(ts,market,symbol,instrument_key,side,qty,price,"
        "notional,status,broker_order_id,reason,response,user_id)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (datetime.now(IST).isoformat(), market, symbol, key or "", side, int(qty or 0),
         float(price or 0), float(qty or 0) * float(price or 0), status, order_id,
         reason, json.dumps(response)[:2000] if response else None, user_id))
    v2.commit()


def mirror_entry(v2, main_db, user_id, market, symbol, price, strategy):
    """Place the sleeve's real BUY for a position the engine just opened.

    Returns a short status string. Every refusal is written to v2_live_orders
    with status='skipped' so the ledger explains what the sleeve did NOT do —
    silence would be indistinguishable from the feature being broken.
    """
    from . import broker
    if market != "IN":
        return "skipped: non-IN market"
    if strategy not in MIRRORED_LANES:
        return f"skipped: {strategy} is not mirrored"
    st = broker.state(user_id)
    if not st.get("live_ready"):
        return "skipped: not armed"
    key = instrument_key(main_db, symbol)
    if not key:
        _record(v2, user_id, market, symbol, None, "BUY", 0, price, "skipped",
                "no upstox instrument key")
        return "skipped: no instrument key"
    if live_qty(v2, user_id, symbol) > 0:
        return "skipped: already held live"
    margin = available_margin(user_id)
    if margin is None:
        _record(v2, user_id, market, symbol, key, "BUY", 0, price, "skipped",
                "broker margin unreadable")
        return "skipped: margin unknown"
    qty = size_for_sleeve(price, st, margin)
    if qty <= 0:
        _record(v2, user_id, market, symbol, key, "BUY", 0, price, "skipped",
                f"unaffordable at Rs {float(price):,.2f}")
        return "skipped: unaffordable"
    ok, why = broker.can_trade(
        dict(symbol=symbol, qty=qty, price=price), user_id, st=st,
        market_is_open=True,
        day_notional=day_notional(v2, user_id), open_positions=len(open_symbols(v2, user_id)))
    if not ok:
        _record(v2, user_id, market, symbol, key, "BUY", qty, price, "skipped", why)
        return f"skipped: {why}"
    res = broker.place_order(user_id, key, qty, "BUY", price=0.0)
    _record(v2, user_id, market, symbol, key, "BUY", qty, price,
            "sent" if res.get("ok") else "failed",
            "mirror " + strategy, res.get("response"), res.get("order_id"))
    _LOG.info("LIVE BUY %s x%d @~%.2f -> %s", symbol, qty, price, res.get("order_id"))
    return "sent" if res.get("ok") else f"failed: {res.get('status')}"


def mirror_exit(v2, main_db, user_id, market, symbol, price, reason):
    """Sell whatever the SLEEVE holds. Never the paper quantity."""
    from . import broker
    if market != "IN":
        return "skipped: non-IN market"
    st = broker.state(user_id)
    qty = live_qty(v2, user_id, symbol)
    if qty <= 0:
        return "skipped: nothing held live"
    if not st.get("live_ready"):
        # An exit blocked by a disarmed sleeve leaves REAL shares held. That is
        # a position the operator must know about, so it is recorded loudly
        # rather than dropped.
        _record(v2, user_id, market, symbol, None, "SELL", qty, price, "skipped",
                f"NOT ARMED — {qty} shares still held live")
        return "skipped: not armed (position still open)"
    key = instrument_key(main_db, symbol)
    if not key:
        _record(v2, user_id, market, symbol, None, "SELL", qty, price, "skipped",
                "no upstox instrument key")
        return "skipped: no instrument key"
    res = broker.place_order(user_id, key, qty, "SELL", price=0.0)
    _record(v2, user_id, market, symbol, key, "SELL", qty, price,
            "sent" if res.get("ok") else "failed",
            f"mirror exit: {reason}", res.get("response"), res.get("order_id"))
    _LOG.info("LIVE SELL %s x%d @~%.2f (%s) -> %s", symbol, qty, price, reason,
              res.get("order_id"))
    return "sent" if res.get("ok") else f"failed: {res.get('status')}"
