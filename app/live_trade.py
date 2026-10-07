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
MIRRORED_LANES = ("mean_reversion", "early_momentum", "manual")

# UPSTOX PRODUCT CODE, per lane. Every order went out as "D" (delivery),
# including the lanes that square off the same afternoon — which is not a
# labelling nicety, it is most of the cost on a small account:
#
#   Rs 9,000 round trip    delivery Rs 91 (1.01%)    intraday Rs 24 (0.27%)
#
# Three separate reasons, all in the same direction:
#   * brokerage caps at 0.1% intraday (Rs 9) but is a FLAT Rs 20 on delivery,
#     so the smaller the ticket the worse delivery is;
#   * STT is 0.1% on BOTH legs for delivery, 0.025% on the sell only intraday;
#   * delivery adds a Rs 20 + GST DP charge on every sell.
#
# At delivery pricing a 1% intraday move on Rs 9,000 is a LOSS. At intraday
# pricing it is +Rs 66. The break-even move falls from ~1.0% to ~0.27%.
#
# Read from the engine's own INTRADAY_STRATS rather than restated here, so a
# lane cannot be classified two different ways in two files.
def product_for(strategy):
    from . import v2_live
    same_day = set(v2_live.INTRADAY_STRATS) | {"btst"}
    return "I" if str(strategy) in same_day else "D"

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
        rows = main_db.execute(
            "SELECT upstox_instrument_key FROM universe WHERE symbol=? AND enabled=1",
            (str(symbol),)).fetchall()
        if len(rows)!=1:
            return None
        catalogued = main_db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='instrument_snapshots'").fetchone()
        if catalogued:
            from .instrument_catalog import resolve
            spec, key = resolve(main_db,symbol=str(symbol),venue="NSE",segment="NSE_EQ")
            from .execution_ports import route_for
            route_for("upstox",spec)
            return key if key == rows[0][0] else None
    except Exception:
        return None
    key = rows[0][0]
    # This adapter implements NSE cash equity only. Other segments must not
    # inherit its quantity, product or cost assumptions.
    return key if key and str(key).startswith("NSE_EQ|") and str(key)[7:].strip() else None


def live_qty(v2, user_id, symbol):
    """Shares the SLEEVE holds, from its own filled orders.

    Derived rather than stored: a position table can drift out of step with what
    the broker actually did, and the order ledger is the thing that was really
    sent. Never read from the paper position — its size is 10x this.
    """
    row = v2.execute(
        "SELECT COALESCE(SUM(CASE WHEN side='BUY' THEN filled_qty ELSE -filled_qty END),0)"
        " FROM v2_live_orders WHERE symbol=? AND user_id=?",
        (str(symbol), int(user_id))).fetchone()
    return int(row[0] or 0)


def open_symbols(v2, user_id):
    rows = v2.execute(
        "SELECT symbol, SUM(CASE WHEN side='BUY' THEN filled_qty ELSE -filled_qty END) q"
        " FROM v2_live_orders WHERE user_id=?"
        " GROUP BY symbol HAVING q>0", (int(user_id),))
    return {r[0]: int(r[1]) for r in rows}


def entry_product(v2, user_id, symbol, default="D"):
    """What product this sleeve's OPEN position was bought with.

    Read from the ledger, not re-derived from the lane: the exit may fire from a
    path that no longer knows the strategy, and an exit that disagrees with its
    entry is rejected by Upstox or silently converts the position to delivery.
    """
    row = v2.execute(
        "SELECT product FROM v2_live_orders WHERE user_id=? AND symbol=?"
        " AND side='BUY' AND filled_qty>0 ORDER BY id DESC LIMIT 1",
        (int(user_id), str(symbol))).fetchone()
    return (row[0] if row and row[0] else default)


def day_notional(v2, user_id, today_s=None):
    today_s = today_s or datetime.now(IST).date().isoformat()
    row = v2.execute("SELECT COALESCE(SUM(notional),0) FROM v2_live_orders"
                     " WHERE date(ts,'+5 hours','+30 minutes')=? AND status NOT IN ('skipped','failed','rejected') AND side='BUY'"
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
            response=None, order_id=None, product=None):
    """user_id is REQUIRED and positional on purpose.

    It was a trailing keyword defaulting to None, so every call that forgot it
    wrote a ledger row belonging to nobody — invisible to that user's own
    history and to the per-user caps. A required positional turns a missed call
    into a TypeError at import time instead of a silent orphan row.
    """
    v2.execute(
        "INSERT INTO v2_live_orders(ts,market,symbol,instrument_key,side,qty,price,"
        "notional,status,broker_order_id,reason,response,user_id,product)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (datetime.now(IST).isoformat(), market, symbol, key or "", side, int(qty or 0),
         float(price or 0), float(qty or 0) * float(price or 0), status, order_id,
         reason, json.dumps(response)[:2000] if response else None, user_id, product))
    v2.commit()


def mirror_entry(v2, main_db, user_id, market, symbol, price, strategy, stop=None, target=None,
                 origin_position_id=None, request_key=None, requested_qty=None):
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
    from . import order_journal as journal
    stable = request_key or (f"house:{origin_position_id}:BUY" if origin_position_id is not None else None)
    import hashlib
    signature = hashlib.sha256(json.dumps([market,symbol,strategy,stop,target,origin_position_id,requested_qty],
                                          sort_keys=True).encode()).hexdigest()
    if stable:
        previous = v2.execute("SELECT status,request_fingerprint FROM v2_live_orders WHERE user_id=? AND semantic_key=?",
                              (user_id,stable)).fetchone()
        if previous:
            if previous[1] != signature:
                return "rejected: idempotency key refers to a different intent"
            return previous[0]
    if not journal.refresh(v2, user_id):
        return "skipped: broker reconciliation unavailable"
    if journal.unresolved(v2, user_id):
        return "pending: broker reconciliation required"
    key = instrument_key(main_db, symbol)
    if not key:
        _record(v2, user_id, market, symbol, None, "BUY", 0, price, "skipped",
                "no upstox instrument key")
        return "skipped: no instrument key"
    if live_qty(v2, user_id, symbol) > 0:
        return "skipped: already held live"
    from . import broker_reconciliation
    if broker_reconciliation.refresh(v2,user_id)["status"] != "ok":
        return "skipped: actual broker reconciliation unavailable or mismatched"
    margin = available_margin(user_id)
    if margin is None:
        _record(v2, user_id, market, symbol, key, "BUY", 0, price, "skipped",
                "broker margin unreadable")
        return "skipped: margin unknown"
    qty = size_for_sleeve(price, st, margin)
    # Respect the SAME stop-risk and sleeve exposure limits as paper. No
    # strategy can enlarge its risk by falling back to equal-notional sizing.
    from .sleeves.base import Candidate
    from .sleeves.risk import RiskManager
    held_quotes = managed_quotes(v2, main_db, user_id)
    state, why = account_risk_state(v2, user_id, st, held_quotes)
    if state is None:
        return "skipped: " + why
    allocation = RiskManager().size(Candidate(symbol, strategy, 1, price,
                                              float(stop or 0), target=float(target or 0),
                                              product=product_for(strategy)), state)
    if not allocation.ok:
        v2.commit()
        return "skipped: " + allocation.reason
    qty = min(qty, allocation.shares)
    if requested_qty is not None:
        if isinstance(requested_qty,bool) or not isinstance(requested_qty,int) or not 0<requested_qty<=qty:
            return "rejected: requested quantity exceeds the account risk allocation"
        qty = requested_qty
    from .costs import entry_charge
    from .sleeves.risk import SLIPPAGE
    while qty and qty * price * (1 + SLIPPAGE) + entry_charge(
            qty * price * (1 + SLIPPAGE), product_for(strategy)) > margin:
        qty -= 1
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
    prod = product_for(strategy)
    return journal.submit(v2, user_id, market, symbol, key, "BUY", qty, price, prod,
                          "mirror " + strategy,
                          stop=stop if stop is not None else price*.94,
                          target=target if target is not None else (price*1.06 if strategy == "manual" else 0),
                          strategy=strategy, quotes=held_quotes, origin_position_id=origin_position_id,
                          available_cash=margin, semantic_key=stable,request_fingerprint=signature)


def mirror_exit(v2, main_db, user_id, market, symbol, price, reason, origin_position_id=None):
    """Sell whatever the SLEEVE holds. Never the paper quantity."""
    from . import broker
    if market != "IN":
        return "skipped: non-IN market"
    from . import order_journal as journal
    if origin_position_id is not None:
        row = v2.execute("SELECT origin_position_id FROM v2_live_orders WHERE user_id=? "
                         "AND symbol=? AND side='BUY' AND status NOT IN ('failed','rejected','skipped') "
                         "ORDER BY id DESC LIMIT 1",
                         (user_id, symbol)).fetchone()
        if not row or row[0] != origin_position_id:
            return "skipped: origin position mismatch"
    journal.request_exit(v2, user_id, symbol, reason)
    st = broker.state(user_id)
    if not st.get("exit_ready"):
        return "skipped: exit credentials unavailable (position still open)"
    if not journal.refresh(v2, user_id):
        return "pending: broker reconciliation unavailable"
    if journal.unresolved(v2, user_id, symbol) and not (
            journal.finish_entry_before_exit(v2, user_id, symbol)):
        return "pending: broker reconciliation required"
    qty = live_qty(v2, user_id, symbol)
    if qty <= 0:
        return "skipped: nothing held live"
    entry = v2.execute("SELECT instrument_key,product FROM v2_live_orders WHERE user_id=? "
                       "AND market=? AND symbol=? AND side='BUY' AND filled_qty>0 "
                       "ORDER BY id DESC LIMIT 1", (user_id, market, symbol)).fetchone()
    key, prod = entry if entry else (None, None)
    if not key or prod not in ("D", "I"):
        _record(v2, user_id, market, symbol, None, "SELL", qty, price, "skipped",
                "owned entry instrument/product unavailable")
        return "skipped: owned entry instrument/product unavailable"
    # SAME product as the entry. Upstox will not let a delivery sell close an
    # intraday buy, and guessing here would either reject the order or convert
    # the position to delivery and charge for it.
    return journal.submit(v2, user_id, market, symbol, key, "SELL", qty, price, prod,
                          f"mirror exit: {reason}", origin_position_id=origin_position_id,
                          semantic_key=f"house:{origin_position_id}:SELL" if origin_position_id is not None else None)


def _alert_failed(user_id, side, symbol, qty, res):
    """Tell the owner their REAL order did not go through.

    Three orders failed today (static-IP block, then market-protection) and the
    only trace was a row in v2_live_orders. Nothing said so unless somebody
    opened the panel and looked — an order that silently does not happen is
    indistinguishable from one that was never wanted.
    """
    try:
        err = ""
        try:
            err = ((res.get("response") or {}).get("errors") or [{}])[0].get("message") or ""
        except Exception:
            err = ""
        _LOG.error("LIVE ORDER FAILED u%s %s %s x%s: %s", user_id, side, symbol, qty,
                   err or res.get("status"))
        from . import telegram_bot
        telegram_bot.notify_users([int(user_id)],
                                 f"⚠️ REAL {side} order FAILED\n{symbol} x{qty}\n"
                                 f"{err or ('HTTP ' + str(res.get('status')))}\n\n"
                                 "Your paper book has moved; the broker has not.")
    except Exception:
        _LOG.exception("could not alert user %s about a failed order", user_id)


_SERVICED = {}


def managed_quotes(con, main_db, uid):
    symbols = list(open_symbols(con, uid))
    if not symbols:
        return {}
    try:
        rows = main_db.execute("SELECT symbol,price,ts FROM latest_quotes "
                               "WHERE source='upstox-live' AND symbol IN (" +
                               ",".join("?" for _ in symbols) + ")", symbols).fetchall()
        return {s: dict(price=p, ts=t) for s, p, t in rows}
    except Exception:
        return {}  # An incomplete valuation is refused by account_risk_state.


def account_risk_state(con, uid, st, quotes=None):
    """After-cost managed-ledger state, shared with paper risk authorization.

    This does not certify external inventory reconciliation or actual fees.
    Those remain independent live-release requirements.
    """
    from . import account_safety
    from .costs import entry_charge, exit_charge
    from .sleeves.feeds import fresh_quotes
    from .sleeves.risk import BookState, stop_loss_including_costs
    row = con.execute("SELECT capital,started_at FROM live_book_epoch WHERE user_id=? AND market='IN'",
                      (uid,)).fetchone()
    capital = float(st.get("budget") or 0)
    if row and float(row[0]) != capital:
        return None, "live allocation change requires an explicit approved epoch"
    if not row:
        epoch = "managed-ledger-v1"
        con.execute("INSERT INTO live_book_epoch(user_id,market,capital,started_at) VALUES(?,'IN',?,?)",
                    (uid, capital, epoch))
    else:
        epoch = row[1]
    day = datetime.now(IST).date().isoformat()
    rows = con.execute("SELECT symbol,side,filled_qty,average_price,product,ts,reason "
                       "FROM v2_live_orders WHERE user_id=? AND market='IN' AND filled_qty>0 "
                       "ORDER BY ts,id", (uid,)).fetchall()
    cash, prior_cash = capital, capital
    positions, prior_qty, counts, notionals = {}, {}, {}, {}
    for sym, side, qty, price, product, ts, reason in rows:
        value = qty * price
        if not price or price <= 0:
            return None, "confirmed fill price unavailable"
        delta = -value - entry_charge(value, product) if side == "BUY" else value - exit_charge(value, product)
        cash += delta
        old = positions.setdefault(sym, dict(qty=0, price=price, product=product,
                                             sleeve=(reason or "").removeprefix("mirror ")))
        old["qty"] += qty if side == "BUY" else -qty
        if side == "BUY":
            old.update(price=price, product=product, sleeve=(reason or "").removeprefix("mirror "))
        try:
            moment = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(IST)
        except (TypeError, ValueError):
            return None, "fill session time unavailable"
        if moment.date().isoformat() < day:
            prior_cash += delta
            prior_qty[sym] = prior_qty.get(sym, 0) + (qty if side == "BUY" else -qty)
    positions = {s: p for s, p in positions.items() if p["qty"] != 0}
    live = fresh_quotes(quotes or {}, datetime.now(timezone.utc))
    if any(p["qty"] < 0 or s not in live for s, p in positions.items()):
        return None, "managed position valuation unavailable or inventory is negative"
    saved = con.execute("SELECT session_day,session_open FROM account_risk_state WHERE "
                        "kind='live' AND user_id=? AND market='IN' AND epoch=?", (uid, epoch)).fetchone()
    opening = saved[1] if saved and saved[0] == day else (
              None if any(q != 0 for q in prior_qty.values()) else prior_cash)
    if opening is None:
        return None, "daily live equity baseline unavailable"
    deployed = equity_value = risk = 0.0
    for sym, p in positions.items():
        protection = con.execute("SELECT stop FROM v2_live_protection WHERE user_id=? AND symbol=?",
                                 (uid, sym)).fetchone()
        if not protection or not protection[0]:
            return None, "managed stop risk unavailable"
        mark = live[sym]["price"]
        deployed += p["qty"] * p["price"]
        equity_value += p["qty"] * mark
        sleeve = p["sleeve"]
        counts[sleeve] = counts.get(sleeve, 0) + 1
        notionals[sleeve] = notionals.get(sleeve, 0) + p["qty"] * p["price"]
        risk += stop_loss_including_costs(mark, min(mark, protection[0]), p["qty"], p["product"])
    equity = cash + equity_value
    peak = account_safety.peak(con, "live", uid, "IN", epoch, capital)
    account_safety.observe(con, "live", uid, "IN", epoch, equity, capital, day, opening)
    return BookState(capital, cash, deployed, len(positions), counts, equity,
                     max(peak, equity), equity - opening, notionals, risk), ""


def service(v2, main_db, quotes):
    """Reconcile fills and retain exit obligations independently of paper rows."""
    from . import broker, order_journal as journal
    from .sleeves.feeds import fresh_quotes
    quotes = fresh_quotes(quotes, datetime.now(timezone.utc))
    for uid in broker.linked_users():
        now = time.time()
        if now - _SERVICED.get(uid, 0) < 60:
            continue
        _SERVICED[uid] = now
        if not broker.state(uid).get("exit_ready") or not journal.refresh(v2, uid):
            continue
        from . import protection
        protection.settle_partial_entries(v2,uid)
        protection.activate_reviewed_fills(v2,uid)
        from .execution_ports import UpstoxPort
        # Only obligations explicitly activated under current account-scoped
        # native authorization are transmitted. Recovery never blind-retries.
        v2.commit()
        for entry_id, in v2.execute("SELECT entry_id FROM protection_obligations WHERE user_id=? AND state='required'",(uid,)).fetchall():
            protection.submit_stop(v2,uid,entry_id,UpstoxPort())
        protection.refresh(v2,uid,UpstoxPort())
        from .portfolio_stream import order_updates
        journal.reconcile(v2,uid,order_updates(v2,uid))
        # Triggered native stops create an owned exit intent before ordinary
        # order reconciliation; application exits cannot race that SELL.
        if v2.execute("SELECT 1 FROM protection_obligations WHERE user_id=? AND state='triggered' LIMIT 1",(uid,)).fetchone():
            journal.refresh(v2,uid)
        protection.cancel_overreserved_children(v2,uid,UpstoxPort())
        from . import broker_reconciliation
        broker_reconciliation.refresh(v2,uid)
        from .protection_amendments import reduce
        # Only exact owned inventory, a fresh matching broker account and the
        # existing separately reviewed policy can reduce a scheduled stop.
        for entry_id, in v2.execute("SELECT entry_id FROM protection_obligations WHERE user_id=? "
                                    "AND state IN ('armed','unknown') AND native_id IS NOT NULL",(uid,)).fetchall():
            reduce(v2,uid,entry_id,UpstoxPort())
        state, why = account_risk_state(v2, uid, broker.state(uid), quotes)
        if state is None:
            _LOG.warning("broker account risk valuation u%s unavailable: %s", uid, why)
        v2.commit()
        rows = list(v2.execute("SELECT symbol,stop,target,exit_reason FROM v2_live_protection "
                               "WHERE user_id=?", (uid,)))
        for symbol, stop, target, reason in rows:
            if live_qty(v2, uid, symbol) <= 0 and not journal.unresolved(v2, uid, symbol):
                v2.execute("DELETE FROM v2_live_protection WHERE user_id=? AND symbol=?", (uid, symbol))
                v2.commit()
                continue
            px = (quotes.get(symbol) or {}).get("price")
            if px is None:
                continue
            if not reason and live_qty(v2, uid, symbol) > 0:
                reason = "stop" if stop and px <= stop else ("target" if target and px >= target else None)
            if reason:
                result = mirror_exit(v2, main_db, uid, "IN", symbol, px, reason)
                _LOG.info("broker protection u%s %s: %s", uid, symbol, result)
