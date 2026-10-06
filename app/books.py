"""Per-user paper books.

WHY NOT ADD user_id TO v2_positions
That was the obvious move and it is the wrong one. The engine touches those
tables in fifty places, every one of them assuming a single book, and a
half-scoped query does not fail — it silently returns somebody else's rows into
a trading decision. The engine's book is also the EVIDENCE BASE: every measured
claim in this codebase (the 42% exit decay, the per-lane records, the option
target study) is computed from those tables, and reshaping them puts all of it
at risk to ship a product feature.

So the engine keeps its book, untouched, as the house record. Users get their
own tables here, and the two never share a row.

WHAT A USER'S BOOK IS
Each subscribed user has an independent Rs 10,000 paper book. The engine's
decisions are applied to THEIR cash:

  * when the house book opens a position, every subscribed user's book opens
    the same symbol, sized to their own cash and capped by the house-approved
    quantity so a mirror cannot enlarge the engine's risk decision;
  * when the house closes it, their book closes it too;
  * manual buys and sells hit only the book of whoever pressed the button;
  * a reset clears only the caller's rows.

A user whose cash cannot afford a share simply skips that trade, and the skip
is not an error — it is the honest consequence of a smaller book.

THE HOUSE BOOK IS user_id 0 AND IS NEVER STORED HERE. It stays in v2_positions
so that nothing about the engine changes.
"""
from __future__ import annotations

import logging
import json
import math
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
_LOG = logging.getLogger("openstocks.books")

DEFAULT_BUDGET = {"IN": 10000.0, "US": 20000.0}   # matches v2_live.BUDGET
MAX_POSITIONS = 3          # matches v2_live.MAXPOS["IN"]
# Fraction of the book one position may take. Mirrors the house rule
# (budget / max_pos) rather than inventing a second sizing policy.
POSITION_FRACTION = 1.0 / MAX_POSITIONS

SCHEMA = """
CREATE TABLE IF NOT EXISTS user_book(
  user_id INTEGER, market TEXT, budget REAL, started_at TEXT,
  PRIMARY KEY(user_id, market));
CREATE TABLE IF NOT EXISTS user_positions(
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, market TEXT, strategy TEXT,
  symbol TEXT, entry_date TEXT, entry_price REAL, shares REAL, stop REAL,
  target REAL, opened_at TEXT, src_id INTEGER, sleeve TEXT, regime TEXT,
  book_epoch TEXT);
CREATE TABLE IF NOT EXISTS user_trades(
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, market TEXT, strategy TEXT,
  symbol TEXT, entry_date TEXT, entry_price REAL, exit_date TEXT, exit_price REAL,
  shares REAL, pnl REAL, return_pct REAL, reason TEXT, opened_at TEXT, closed_at TEXT,
  sleeve TEXT, regime TEXT, book_epoch TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS ux_user_pos ON user_positions(user_id, market, symbol);
CREATE INDEX IF NOT EXISTS ix_user_trades ON user_trades(user_id, market, exit_date);
-- One equity point per user per day. Without this a personal book can never
-- draw a curve, which is most of what makes a book feel like yours.
CREATE TABLE IF NOT EXISTS user_equity(
  user_id INTEGER, market TEXT, date TEXT, equity REAL, cash REAL,
  positions_value REAL, n_positions INTEGER,
  PRIMARY KEY(user_id, market, date));
"""


def ensure_schema(con):
    con.executescript(SCHEMA)
    con.execute("CREATE TABLE IF NOT EXISTS paper_entry_intents(user_id INTEGER,market TEXT,epoch TEXT,"
                "request_key TEXT,qty INTEGER NOT NULL,price REAL NOT NULL,position_id INTEGER,"
                "PRIMARY KEY(user_id,market,epoch,request_key))")
    intent_columns = {r[1] for r in con.execute("PRAGMA table_info(paper_entry_intents)")}
    if "fingerprint" not in intent_columns:
        con.execute("ALTER TABLE paper_entry_intents ADD COLUMN fingerprint TEXT")
    columns = {r[1] for r in con.execute("PRAGMA table_info(user_positions)")}
    for name, kind in (("exit_policy", "TEXT"), ("peak", "REAL"),
                       ("entry_fee", "REAL DEFAULT 0"), ("product", "TEXT DEFAULT 'D'"),
                       ("risk_amt", "REAL")):
        if name not in columns:
            con.execute(f"ALTER TABLE user_positions ADD COLUMN {name} {kind}")
    trade_columns = {r[1] for r in con.execute("PRAGMA table_info(user_trades)")}
    if "risk_amt" not in trade_columns:
        con.execute("ALTER TABLE user_trades ADD COLUMN risk_amt REAL")
    for table in ('user_positions','user_trades'):
        existing={r[1] for r in con.execute('PRAGMA table_info('+table+')')}
        for field in ('instrument_id','plan_id','model_version'):
            if field not in existing:con.execute('ALTER TABLE '+table+' ADD COLUMN '+field+' TEXT')
    from . import account_safety
    account_safety.ensure_schema(con)
    from . import paper_ledger
    paper_ledger.ensure_schema(con)
    con.execute("CREATE TABLE IF NOT EXISTS user_book_decisions("
                "id INTEGER PRIMARY KEY,user_id INTEGER,market TEXT,epoch TEXT,"
                "symbol TEXT,accepted INTEGER,reason TEXT,created_at TEXT)")
    from .exit_policy import ExitPolicy
    for pid, strategy, stop, target in con.execute(
            "SELECT id,strategy,stop,target FROM user_positions WHERE exit_policy IS NULL").fetchall():
        con.execute("UPDATE user_positions SET exit_policy=?,peak=COALESCE(peak,entry_price) WHERE id=?",
                    (ExitPolicy.create(strategy, stop, target,
                                       max_hold_days=0 if strategy == "manual" else None).encode(), pid))
    con.commit()


def ensure_book(con, user_id, market="IN"):
    """Create a missing book; allocation/epoch changes require an explicit reset."""
    default = DEFAULT_BUDGET.get(market, 10000.0)
    row = con.execute("SELECT budget FROM user_book WHERE user_id=? AND market=?",
                      (int(user_id), market)).fetchone()
    if row:
        return float(row[0])
    budget = default
    nested = con.in_transaction
    con.execute("INSERT OR IGNORE INTO user_book(user_id,market,budget,started_at)"
                " VALUES(?,?,?,?)",
                (int(user_id), market, budget, datetime.now(timezone.utc).isoformat()))
    if not nested:
        con.commit()
    return budget



LEGACY_EPOCH = "legacy"


def current_epoch(con, user_id, market="IN") -> str:
    """The epoch id this book is currently trading in.

    EVERY read of a book's money filters on this. Scoping by timestamp
    comparison was tried and failed five separate times — in books.cash,
    books.stats, v2_web._market_stats, sleeve_pass and the equity series — each
    one a fresh place to forget the filter, and each forgotten one put the old
    Rs 1,00,000 ledger back on the dashboard.

    A stamped column cannot be forgotten in the same way: rows carry the epoch
    they were written in, legacy rows carry `legacy`, and a query that omits
    the filter returns nothing rather than silently returning everything.
    """
    row = con.execute("SELECT started_at FROM user_book WHERE user_id=? AND market=?",
                      (int(user_id), market)).fetchone()
    return (row[0] if row and row[0] else LEGACY_EPOCH) or LEGACY_EPOCH


def reset_book(con, user_id, market="IN", budget=None):
    """TRUE fresh start for one user's book.

    Capital, cash, equity and peak all return to the budget; open positions are
    cleared; realised P&L for the new epoch is zero. Legacy trades are NOT
    deleted — they are stamped `legacy` and every book read filters them out,
    so they stay queryable for history while being unable to touch equity,
    cash, peak, drawdown or the dashboard.
    """
    cap = float(budget if budget is not None else DEFAULT_BUDGET.get(market, 10000.0))
    now = datetime.now(timezone.utc).isoformat()
    uid = int(user_id)
    # anything not already stamped belongs to the book that came before
    for tbl in ("user_trades", "user_positions"):
        con.execute(f"UPDATE {tbl} SET book_epoch=? WHERE user_id=? AND market=?"
                    " AND (book_epoch IS NULL OR book_epoch='')",
                    (LEGACY_EPOCH, uid, market))
    con.execute("DELETE FROM user_positions WHERE user_id=? AND market=?", (uid, market))
    con.execute("DELETE FROM user_equity WHERE user_id=? AND market=?", (uid, market))
    con.execute("INSERT OR REPLACE INTO user_book(user_id,market,budget,started_at)"
                " VALUES(?,?,?,?)", (uid, market, cap, now))
    con.execute("INSERT OR REPLACE INTO user_equity(user_id,market,date,equity,cash,"
                "positions_value,n_positions) VALUES(?,?,?,?,?,?,?)",
                (uid, market, now[:10], cap, cap, 0.0, 0))
    con.commit()
    _LOG.warning("user %s book RESET: capital Rs %.0f, epoch %s, legacy history "
                 "retained but excluded", uid, cap, now)
    return now


def epoch_of(con, user_id, market="IN") -> str:
    """When this book's current capital took effect.

    Realised P&L and peak equity are scoped to it. Without that, P&L earned on
    a Rs 1,00,000 book is charged against a Rs 10,000 one and equity goes
    permanently negative — which is exactly what pinned the house book's
    drawdown brake at -105% until it was scoped the same way.
    """
    row = con.execute("SELECT started_at FROM user_book WHERE user_id=? AND market=?",
                      (int(user_id), market)).fetchone()
    return (row[0] if row and row[0] else "") or ""


def budget_of(con, user_id, market="IN"):
    """This book's budget WITHOUT creating it.

    ensure_book inserts, and stats() runs on the per-second stream for every
    connected dashboard — so the read path was opening a write transaction once
    a second per open tab, against a SQLite file the engine also writes. Reads
    must not write.
    """
    row = con.execute("SELECT budget FROM user_book WHERE user_id=? AND market=?",
                      (int(user_id), market)).fetchone()
    return float(row[0]) if row else DEFAULT_BUDGET.get(market, 10000.0)


def cash(con, user_id, market="IN"):
    """Budget minus what is deployed plus what has been realised.

    Realised P&L is ADDED rather than tracked as a separate balance so a book
    can never disagree with its own trade history — the same reason the house
    book computes it this way.
    """
    budget = budget_of(con, user_id, market)
    ep = current_epoch(con, user_id, market)
    spent = con.execute("SELECT COALESCE(SUM(entry_price*shares+COALESCE(entry_fee,0)),0) FROM user_positions"
                        " WHERE user_id=? AND market=? AND COALESCE(book_epoch,?)=?",
                        (int(user_id), market, LEGACY_EPOCH, ep)).fetchone()[0] or 0.0
    # Scoped to the current epoch, and compared on the TIMESTAMP: the legacy
    # ledger shares the calendar date the resize happened, so a date-only
    # compare drags the whole old book back in.
    realised = con.execute("SELECT COALESCE(SUM(pnl),0) FROM user_trades"
                           " WHERE user_id=? AND market=? AND COALESCE(book_epoch,?)=?",
                           (int(user_id), market, LEGACY_EPOCH, ep)).fetchone()[0] or 0.0
    return budget - float(spent) + float(realised)


def size_for(con, user_id, market, price):
    """Whole shares this user's book can take of one position.

    Capped by BOTH the per-position fraction and the cash actually free, so a
    depleted book takes smaller positions instead of going negative — which is
    what the house book's manual-buy path used to do.
    """
    price = float(price or 0)
    if price <= 0:
        return 0
    budget = ensure_book(con, user_id, market)
    free = cash(con, user_id, market)
    allowance = min(budget * POSITION_FRACTION, free * 0.98)
    return max(0, int(allowance // price))


def positions(con, user_id, market="IN"):
    cols = ("id", "market", "strategy", "symbol", "entry_date", "entry_price",
            "shares", "stop", "target", "opened_at", "sleeve", "regime",
            "exit_policy", "peak", "entry_fee", "product", "src_id", "book_epoch", "risk_amt",
            "instrument_id","plan_id","model_version")
    ep = current_epoch(con, user_id, market)
    rows = con.execute(f"SELECT {','.join(cols)} FROM user_positions"
                       " WHERE user_id=? AND market=? AND COALESCE(book_epoch,?)=?"
                       " ORDER BY id",
                       (int(user_id), market, LEGACY_EPOCH, ep)).fetchall()
    return [dict(zip(cols, r)) for r in rows]


def open_symbols(con, user_id, market="IN"):
    return {r[0] for r in con.execute(
        "SELECT symbol FROM user_positions WHERE user_id=? AND market=?",
        (int(user_id), market))}


def buy(con, user_id, market, strategy, symbol, price, shares=None,
        stop=None, target=None, src_id=None, sleeve=None, regime=None,
        exit_policy=None, quotes=None, request_key=None, exact_quantity=False):
    """Open a position in ONE user's book. Returns shares bought, or 0.

    Zero is a normal outcome, not a failure: a book too small for one share of
    a Rs 5,000 stock skips it. Refusing loudly there would turn a smaller
    account into an error message on every expensive name.
    """
    from .account_safety import atomic
    from .exit_policy import ExitPolicy
    from .sleeves.base import Candidate
    from .sleeves.risk import RiskManager
    from .live_trade import product_for
    from .costs import entry_charge
    from .recovery_guard import assert_database_execution_allowed
    assert_database_execution_allowed(con)
    try:
        price, stop = float(price), float(stop or 0)
        if not math.isfinite(price) or not 0 < stop < price:
            return 0
        if shares is not None and (isinstance(shares,bool) or int(shares) != shares):
            return 0
        requested = int(shares) if shares is not None else None
        if requested is not None and requested <= 0:
            return 0
        policy = (ExitPolicy.decode(exit_policy) if exit_policy else
                  ExitPolicy.create(strategy, stop, target,
                                    max_hold_days=0 if strategy == "manual" else None))
        if policy.stop != stop or policy.target != float(target or 0):
            return 0  # Allocated risk and executable protection must agree.
    except (TypeError, ValueError, OverflowError):
        return 0
    import hashlib
    signature = hashlib.sha256(json.dumps([market,strategy,symbol,requested,stop,float(target or 0),src_id,
                                          sleeve,regime,policy.encode(),exact_quantity],sort_keys=True).encode()).hexdigest()
    with atomic(con):
        from .worker_fencing import require_current
        require_current(con)
        ensure_book(con, user_id, market)
        epoch = current_epoch(con, user_id, market)
        if request_key:
            prior = con.execute("SELECT qty,fingerprint FROM paper_entry_intents WHERE user_id=? AND market=? "
                                "AND epoch=? AND request_key=?", (user_id,market,epoch,str(request_key))).fetchone()
            if prior:
                if prior[1] != signature:
                    raise ValueError("idempotency key refers to a different paper intent")
                return int(prior[0])  # The original fill, even if now closed.
        state, reason = risk_state(con, user_id, market, quotes)
        if symbol in open_symbols(con, user_id, market):
            reason = "already held"
        candidate = Candidate(symbol, sleeve or strategy, 1, price, stop,
                              target=float(target or 0),
                              allocation_pct=0.5 if strategy == "index_directional" else 0,
                              product=product_for(strategy))
        allocation = RiskManager().size(candidate, state) if state and not reason else None
        if allocation is not None and not allocation.ok:
            reason = allocation.reason
        if exact_quantity and requested is not None and allocation and requested > allocation.shares:
            reason = "requested quantity exceeds the account risk allocation"
        qty = min(allocation.shares, requested) if allocation and requested is not None else (
              allocation.shares if allocation else 0)
        if qty and qty * price < RiskManager().s.min_ticket:
            reason = "approved quantity is below the minimum viable ticket"
        contract=None
        if qty and not reason:
            from . import entry_contracts
            try:contract=entry_contracts.check(market,symbol,qty,price,stop,target,product=candidate.product,regime=regime)
            except ValueError as exc:reason=str(exc)
        accepted = bool(qty > 0 and not reason)
        now = datetime.now(IST)
        con.execute("INSERT INTO user_book_decisions(user_id,market,epoch,symbol,accepted,reason,created_at) "
                    "VALUES(?,?,?,?,?,?,?)", (int(user_id), market, epoch, symbol,
                                             int(accepted), reason or "approved", now.isoformat()))
        if not accepted:
            return 0
        fee = entry_charge(qty * price, candidate.product) if market == "IN" else 0
        from .sleeves.risk import stop_loss_including_costs
        initial_risk = stop_loss_including_costs(price,stop,qty,candidate.product) if market == "IN" else None
        from . import paper_ledger
        paper_ledger.anchor(con, user_id, market, epoch, cash(con,user_id,market),
                            sum(p["entry_price"]*p["shares"] for p in positions(con,user_id,market)))
        cur = con.execute("INSERT OR IGNORE INTO user_positions(user_id,market,strategy,"
                          "symbol,entry_date,entry_price,shares,stop,target,opened_at,"
                          "src_id,sleeve,regime,book_epoch,exit_policy,peak,entry_fee,product,risk_amt) "
                          "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                          (int(user_id), market, strategy, symbol, now.date().isoformat(),
                           price, qty, stop, target, now.isoformat(), src_id, sleeve, regime,
                           epoch, policy.encode(), price, fee, candidate.product,initial_risk))
        if cur.rowcount:
            paper_ledger.entry(con,user_id,market,epoch,cur.lastrowid,qty*price,fee)
            entry_contracts.record(con,'personal',int(user_id),cur.lastrowid,contract)
            con.execute('UPDATE user_positions SET instrument_id=? WHERE id=? AND user_id=?',
                        (contract['instrument_id'],cur.lastrowid,int(user_id)))
        if cur.rowcount and request_key:
            con.execute("INSERT INTO paper_entry_intents(user_id,market,epoch,request_key,qty,price,position_id,fingerprint) "
                        "VALUES(?,?,?,?,?,?,?,?)",
                        (user_id,market,epoch,str(request_key),qty,price,cur.lastrowid,signature))
        return qty if cur.rowcount else 0


def entry_receipt(con, user_id, market, request_key):
    epoch = current_epoch(con,user_id,market)
    row = con.execute("SELECT qty,price,position_id FROM paper_entry_intents WHERE user_id=? AND market=? "
                      "AND epoch=? AND request_key=?",(user_id,market,epoch,request_key)).fetchone()
    return dict(qty=row[0],entry=row[1],position_id=row[2]) if row else None


def refusal(con, user_id, market):
    row = con.execute("SELECT reason FROM user_book_decisions WHERE user_id=? AND market=? "
                      "ORDER BY id DESC LIMIT 1", (int(user_id), market)).fetchone()
    return row[0] if row else "invalid levels or size"


def _paper_peak(con, user_id, market, epoch, capital):
    """Retain known active-epoch observations before replacing display rows."""
    from . import account_safety
    chart = con.execute("SELECT MAX(equity) FROM user_equity WHERE user_id=? AND market=? "
                        "AND date>=?", (int(user_id), market, epoch[:10])).fetchone()[0]
    return account_safety.peak(con, "paper", user_id, market, epoch,
                               max(float(capital), float(chart or capital)))


def risk_state(con, user_id, market="IN", quotes=None):
    """Account-specific, after-cost book state; incomplete valuation refuses risk."""
    from . import account_safety
    from .sleeves.risk import BookState, stop_loss_including_costs
    from .sleeves.feeds import fresh_quotes
    pos = positions(con, user_id, market)
    if quotes is None and pos:
        from .v2_live import _live
        try:
            quotes = _live(market, [p["symbol"] for p in pos])
        except Exception:
            return None, "account valuation unavailable: quote source could not be read"
    quotes = fresh_quotes(quotes or {}, datetime.now(timezone.utc))
    if any(p["symbol"] not in quotes for p in pos):
        return None, "account valuation unavailable: held quote missing or stale"
    if any(not p["stop"] or not math.isfinite(p["stop"]) or p["stop"] <= 0 for p in pos):
        return None, "account stop risk unavailable"
    st = stats(con, user_id, market, quotes)
    epoch = current_epoch(con, user_id, market)
    day = datetime.now(IST).date().isoformat()
    baseline = con.execute("SELECT equity FROM user_equity WHERE user_id=? AND market=? "
                           "AND date<? AND date>=? ORDER BY date DESC LIMIT 1",
                           (int(user_id), market, day, epoch[:10])).fetchone()
    if baseline:
        opening = float(baseline[0])
    else:
        carried = any(p["entry_date"] < day for p in pos) or con.execute(
            "SELECT 1 FROM user_trades WHERE user_id=? AND market=? AND book_epoch=? "
            "AND entry_date<? AND exit_date>=? LIMIT 1",
            (int(user_id), market, epoch, day, day)).fetchone()
        if carried:
            return None, "daily equity baseline unavailable"
        prior = con.execute("SELECT COALESCE(SUM(pnl),0) FROM user_trades WHERE "
                            "user_id=? AND market=? AND book_epoch=? AND exit_date<?",
                            (int(user_id), market, epoch, day)).fetchone()[0]
        opening = st["budget"] + float(prior or 0)
    peak = _paper_peak(con, user_id, market, epoch, st["budget"])
    if con.in_transaction:
        account_safety.observe(con, "paper", user_id, market, epoch, st["equity"],
                               st["budget"], day, opening, historical_peak=peak)
    counts, notional, risks = {}, {}, []
    for p in pos:
        sleeve = p["sleeve"] or p["strategy"]
        counts[sleeve] = counts.get(sleeve, 0) + 1
        notional[sleeve] = notional.get(sleeve, 0) + p["entry_price"] * p["shares"]
        mark = quotes[p["symbol"]]["price"]
        risk = stop_loss_including_costs(mark, min(mark, p["stop"]), p["shares"], p["product"])
        risks.append((sleeve, risk))
    return BookState(st["budget"], st["cash"], sum(notional.values()), len(pos), counts,
                     st["equity"], max(peak, st["equity"]), st["equity"] - opening,
                     notional, sum(r for _, r in risks),
                     sum(r for s, r in risks if s == "index_directional")), ""


def sell(con, user_id, market, symbol, price, reason="manual", position_id=None):
    """Close a position in ONE user's book. Returns (pnl, return_pct) or None."""
    from .account_safety import atomic
    from .recovery_guard import assert_database_execution_allowed
    assert_database_execution_allowed(con)
    with atomic(con):
        from .worker_fencing import require_current
        require_current(con)
        return _sell_locked(con, user_id, market, symbol, price, reason, position_id)


def _sell_locked(con, user_id, market, symbol, price, reason, position_id):
    from .recovery_guard import assert_database_execution_allowed
    assert_database_execution_allowed(con)
    epoch = current_epoch(con, user_id, market)
    sql = ("SELECT id,strategy,entry_date,entry_price,shares,opened_at,"
                      "sleeve,regime,COALESCE(entry_fee,0),risk_amt,instrument_id,plan_id,model_version"
           " FROM user_positions WHERE user_id=? AND market=? AND symbol=? "
           "AND COALESCE(book_epoch,?)=?")
    args = [int(user_id), market, symbol, LEGACY_EPOCH, epoch]
    if position_id is not None:
        sql += " AND id=?"
        args.append(position_id)
    row = con.execute(sql, args).fetchone()
    if not row:
        return None
    pid, strategy, edate, entry, shares, opened, sleeve, regime, paid_fee, initial_risk, instrument_id, plan_id, model_version = row
    price = float(price or 0)
    if not math.isfinite(price) or price <= 0:
        return None
    # SAME cost model as the house book. A user's book that reported gross P&L
    # while the engine reported net would make the two incomparable, which is
    # the whole reason for running them side by side.
    from .v2_live import net_trade_pnl
    net, pct = net_trade_pnl(market, shares, float(entry), price, strategy=strategy)
    from . import paper_ledger
    paper_ledger.anchor(con,user_id,market,epoch,cash(con,user_id,market),
                        sum(p["entry_price"]*p["shares"] for p in positions(con,user_id,market)))
    # Existing after-cost P&L already includes the entry fee. Only the
    # remaining exit charge belongs to this cash leg.
    exit_charge = shares*(price-entry)-net-paid_fee
    paper_ledger.exit(con,user_id,market,epoch,pid,shares*entry,shares*price,exit_charge)
    now = datetime.now(IST)
    con.execute("INSERT INTO user_trades(user_id,market,strategy,symbol,entry_date,"
                "entry_price,exit_date,exit_price,shares,pnl,return_pct,reason,"
                "opened_at,closed_at,sleeve,regime,book_epoch,risk_amt,instrument_id,plan_id,model_version)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (int(user_id), market, strategy, symbol, edate, entry,
                 now.date().isoformat(), price, shares, net, pct, reason,
                 opened, now.isoformat(), sleeve, regime,
                 current_epoch(con, user_id, market),initial_risk,instrument_id,plan_id,model_version))
    con.execute("DELETE FROM user_positions WHERE id=?", (pid,))
    return net, pct


def reset(con, user_id, market=None):
    """Clear ONE user's book. Never touches anyone else, never the engine's."""
    if market:
        con.execute("DELETE FROM user_positions WHERE user_id=? AND market=?",
                    (int(user_id), market))
        con.execute("DELETE FROM user_trades WHERE user_id=? AND market=?",
                    (int(user_id), market))
        con.execute("DELETE FROM user_book WHERE user_id=? AND market=?",
                    (int(user_id), market))
    else:
        for t in ("user_positions", "user_trades", "user_book"):
            con.execute(f"DELETE FROM {t} WHERE user_id=?", (int(user_id),))
    con.commit()


def stats(con, user_id, market, live):
    """Everything the dashboard needs for ONE user's book."""
    budget = budget_of(con, user_id, market)
    pos = positions(con, user_id, market)
    mtm = unreal = 0.0
    for p in pos:
        px = float((live or {}).get(p["symbol"], {}).get("price") or p["entry_price"])
        mtm += p["shares"] * px
        unreal += (px - p["entry_price"]) * p["shares"]
    ep = current_epoch(con, user_id, market)
    realised = con.execute("SELECT COALESCE(SUM(pnl),0) FROM user_trades"
                           " WHERE user_id=? AND market=? AND COALESCE(book_epoch,?)=?",
                           (int(user_id), market, LEGACY_EPOCH, ep)).fetchone()[0] or 0.0
    rets = [r[0] for r in con.execute("SELECT return_pct FROM user_trades"
                                      " WHERE user_id=? AND market=? AND COALESCE(book_epoch,?)=?",
                                      (int(user_id), market, LEGACY_EPOCH, ep))]
    wins = [r for r in rets if r > 0]
    fees = sum(float(p["entry_fee"] or 0) for p in pos)
    free = budget - sum(p["entry_price"] * p["shares"] for p in pos) - fees + realised
    return dict(market=market, budget=budget, cash=round(free, 2),
                deployed=round(mtm, 2), equity=round(free + mtm, 2),
                overall_pnl=round(realised + unreal - fees, 2), realised=round(realised, 2),
                unrealised=round(unreal - fees, 2), positions=len(pos), trades=len(rets),
                win=(round(len(wins) / len(rets) * 100) if rets else 0),
                deploy_pct=(round(mtm / budget * 100) if budget else 0))


def _auth_db():
    """The auth DB, without importing app.main from the engine thread.

    `from .main import db` inside the engine pulls in the whole FastAPI app on
    first call. If that import is mid-flight or fails, the exception is caught
    by the caller and user books silently stop mirroring — a feature that is
    off with no error anywhere. Building the Database directly has no such
    ordering hazard.
    """
    from .config import Settings
    from .db import Database
    return Database(Settings().database_path)


def snapshot_equity(con, user_id, market, live, day=None):
    """Record today's equity for ONE book. INSERT OR REPLACE keyed on the day,
    so repeated calls update rather than accumulate."""
    day = day or datetime.now(IST).date().isoformat()
    st = stats(con, user_id, market, live)
    from . import account_safety
    # Peak is durable even when today's display row is replaced or pruned.
    epoch = current_epoch(con, user_id, market)
    peak = _paper_peak(con, user_id, market, epoch, st["budget"])
    account_safety.observe(con, "paper", user_id, market, current_epoch(con, user_id, market),
                           st["equity"], st["budget"], day, historical_peak=peak)
    con.execute("INSERT OR REPLACE INTO user_equity(user_id,market,date,equity,cash,"
                "positions_value,n_positions) VALUES(?,?,?,?,?,?,?)",
                (int(user_id), market, day, st["equity"], st["cash"],
                 st["deployed"], st["positions"]))
    con.commit()
    return st["equity"]


def equity_series(con, user_id, market="IN", limit=90):
    rows = con.execute("SELECT date,equity FROM user_equity WHERE user_id=? AND market=?"
                       " ORDER BY date DESC LIMIT ?",
                       (int(user_id), market, int(limit))).fetchall()
    return [(r[0], float(r[1])) for r in rows][::-1]


def snapshot_all(con, plans_mod, market, live, db=None):
    """Every subscribed book, once per engine cycle."""
    n = 0
    for uid in subscribers(db, plans_mod):
        try:
            snapshot_equity(con, uid, market, live)
            n += 1
        except Exception:
            _LOG.exception("equity snapshot failed for user %s", uid)
    return n


def subscribers(db, plans_mod):
    """User ids whose plan includes a paper book.

    Read from the auth DB rather than kept in a second list, so a lapsed
    subscription stops the mirror without anything else having to notice.
    """
    out = []
    try:
        db = db or _auth_db()
        for u in (db.list_users() or []):
            if not u.get("active"):
                continue
            plan = plans_mod.effective(u.get("account_plan"), u.get("trial_ends_at"),
                                       plan_expires_at=u.get("plan_expires_at"))
            if plans_mod.allows(plan, "paper_book"):
                out.append(int(u["id"]))
    except Exception:
        _LOG.exception("could not list book subscribers")
    return out


def mirror_entry(con, db, plans_mod, market, strategy, symbol, price,
                 stop=None, target=None, src_id=None, sleeve=None, regime=None,
                 max_shares=None, exit_policy=None):
    """Fan out the decision without exceeding its approved share count."""
    done = 0
    if src_id is None:
        _LOG.error("mirror entry %s refused: origin position is missing", symbol)
        return 0
    for uid in subscribers(db, plans_mod):
        try:
            if buy(con, uid, market, strategy, symbol, price, max_shares, stop, target,
                   src_id, sleeve, regime, exit_policy=exit_policy):
                done += 1
        except Exception:
            _LOG.exception("book mirror entry failed for user %s", uid)
    return done


def mirror_exit(con, db, plans_mod, market, symbol, price, reason, src_id=None, strict=False):
    """Close the exact origin's mirrors, including lapsed subscribers."""
    if src_id is None:
        _LOG.error("mirror exit %s refused: origin position is missing", symbol)
        return 0
    done = 0
    failed = False
    for pid, uid in con.execute("SELECT id,user_id FROM user_positions"
                               " WHERE market=? AND symbol=? AND src_id=?",
                               (market, symbol, src_id)).fetchall():
        try:
            if sell(con, uid, market, symbol, price, reason, position_id=pid):
                done += 1
        except Exception:
            _LOG.exception("book mirror exit failed for user %s", uid)
            failed = True
    if strict and failed:
        raise RuntimeError("personal exit delivery incomplete")
    return done


def monitor_positions(con, market, quotes, today=None, regime_view=None):
    """Personal exits do not depend on subscription, house membership or entry gates."""
    from .account_safety import atomic
    from .exit_policy import ExitPolicy
    from .sleeves.feeds import fresh_quotes
    from .v2_live import evaluate_exit
    now = datetime.now(IST)
    today = today or now.date()
    live = fresh_quotes(quotes, now)
    done = 0
    users = [r[0] for r in con.execute(
        "SELECT DISTINCT user_id FROM user_positions WHERE market=?", (market,))]
    for uid in users:
        with atomic(con):
            for row in positions(con, uid, market):
                quote = live.get(row["symbol"])
                if not quote:
                    continue
                policy = ExitPolicy.decode(row["exit_policy"])
                p = dict(strategy=row["strategy"], entry=row["entry_price"],
                         shares=row["shares"], stop=row["stop"], target=row["target"] or 0,
                         peak=row["peak"] or row["entry_price"], trail=policy.trail,
                         edate=row["entry_date"], exit_policy=row["exit_policy"])
                q = dict(quote, high=quote["price"], low=quote["price"])
                peak, _, price, reason = evaluate_exit(
                    p, q, None, today, today.isoformat(), market, now.strftime("%H:%M"))
                if (price is None and policy.regime_exit == "monthly_off" and regime_view
                        and regime_view.get("regime") == "OFF"
                        and regime_view.get("cycle_date") == today.isoformat()):
                    from .sleeves.index_directional import monthly_rebalance
                    from datetime import date
                    if monthly_rebalance(date.fromisoformat(regime_view["asof"][:10]), today):
                        price, reason = quote["price"], "regime_off"
                if price is not None:
                    if _sell_locked(con, uid, market, row["symbol"], price, reason, row["id"]):
                        done += 1
                else:
                    con.execute("UPDATE user_positions SET peak=? WHERE id=? AND user_id=?",
                                (peak, row["id"], uid))
            risk_state(con, uid, market, live)
    return done


def reconcile_capital(con, user_id, market="IN", prices=None):
    """Close positions this book can no longer carry after a resize.

    A user book that dropped Rs 1,00,000 -> Rs 10,000 still held positions sized
    for the old capital — one of them alone was several times the whole new
    book. CLOSED, not deleted: each is written to user_trades at the last known
    price with reason `book_resize`, so the P&L stays in the ledger and the
    per-sleeve split still accounts for it.

    Idempotent. A symbol already carrying a book_resize row for today has its
    stale position row removed without booking a second close.
    """
    prices = prices or {}
    budget = ensure_book(con, user_id, market)
    slot = budget * POSITION_FRACTION
    today = datetime.now(timezone.utc).date().isoformat()
    closed = orphans = 0
    rows = list(con.execute(
        "SELECT id,symbol,strategy,shares,entry_price,entry_date,opened_at"
        " FROM user_positions WHERE user_id=? AND market=?", (int(user_id), market)))
    for pid, sym, strat, sh, ep, edate, oat in rows:
        notional = float(sh) * float(ep)
        if notional <= slot:
            continue
        already = con.execute(
            "SELECT 1 FROM user_trades WHERE user_id=? AND market=? AND symbol=?"
            " AND reason='book_resize' AND exit_date=?",
            (int(user_id), market, sym, today)).fetchone()
        if already:
            con.execute("DELETE FROM user_positions WHERE id=?", (pid,))
            orphans += 1
            continue
        px = float((prices.get(sym) or {}).get("price") or ep)
        pnl = float(sh) * (px - float(ep))
        basis = float(sh) * float(ep)
        con.execute(
            "INSERT INTO user_trades(user_id,market,strategy,symbol,entry_date,"
            "entry_price,exit_date,exit_price,shares,pnl,return_pct,reason,"
            "opened_at,closed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (int(user_id), market, strat, sym, edate, float(ep), today, px,
             float(sh), pnl, (pnl / basis * 100) if basis else 0.0, "book_resize",
             oat, datetime.now(timezone.utc).isoformat()))
        con.execute("DELETE FROM user_positions WHERE id=?", (pid,))
        closed += 1
    if closed or orphans:
        con.commit()
        _LOG.warning("user %s: book_resize closed %d and cleared %d orphan(s) "
                     "the Rs %.0f book could not carry",
                     user_id, closed, orphans, budget)
    return closed
