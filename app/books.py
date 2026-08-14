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
Pro is sold as "your own Rs 1,00,000 paper book". That means the engine's
decisions applied to THEIR cash:

  * when the house book opens a position, every subscribed user's book opens
    the same symbol, SIZED TO THEIR OWN CASH — not the house quantity;
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
  target REAL, opened_at TEXT, src_id INTEGER, sleeve TEXT, regime TEXT);
CREATE TABLE IF NOT EXISTS user_trades(
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, market TEXT, strategy TEXT,
  symbol TEXT, entry_date TEXT, entry_price REAL, exit_date TEXT, exit_price REAL,
  shares REAL, pnl REAL, return_pct REAL, reason TEXT, opened_at TEXT, closed_at TEXT,
  sleeve TEXT, regime TEXT);
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
    con.commit()


def ensure_book(con, user_id, market="IN"):
    """Create this user's book on first touch, and RECONCILE it to the current
    default. Returns the budget.

    Reconciliation matters as much as creation. This only ever inserted, so
    every existing book kept the capital it was created with: after the house
    book moved Rs 1,00,000 -> Rs 10,000 the website still showed eight user
    books at Rs 1,00,000 carrying the whole legacy ledger (-Rs 44,140 for
    uid 2), while the box read a clean Rs 10,000. Same defect as v2_book's
    seed-only ensure_schema, one table over.

    A capital change re-anchors `started_at` for the same reason it does on the
    house book: user_equity snapshots are denominated in the book they were
    taken under, so realised P&L and peak equity must be scoped to the current
    epoch or the numbers stay in the old size forever. Nothing is deleted —
    reconcile_capital() closes what the smaller book cannot carry.
    """
    default = DEFAULT_BUDGET.get(market, 10000.0)
    row = con.execute("SELECT budget FROM user_book WHERE user_id=? AND market=?",
                      (int(user_id), market)).fetchone()
    if row:
        if abs(float(row[0]) - default) > 1e-9:
            _LOG.warning("user %s book resized Rs %.0f -> Rs %.0f; epoch re-anchored",
                         user_id, float(row[0]), default)
            con.execute("UPDATE user_book SET budget=?, started_at=?"
                        " WHERE user_id=? AND market=?",
                        (default, datetime.now(timezone.utc).isoformat(),
                         int(user_id), market))
            con.commit()
            return default
        return float(row[0])
    budget = default
    con.execute("INSERT OR IGNORE INTO user_book(user_id,market,budget,started_at)"
                " VALUES(?,?,?,?)",
                (int(user_id), market, budget, datetime.now(timezone.utc).isoformat()))
    con.commit()
    return budget


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
    return float(row[0]) if row else DEFAULT_BUDGET.get(market, 100000.0)


def cash(con, user_id, market="IN"):
    """Budget minus what is deployed plus what has been realised.

    Realised P&L is ADDED rather than tracked as a separate balance so a book
    can never disagree with its own trade history — the same reason the house
    book computes it this way.
    """
    budget = budget_of(con, user_id, market)
    spent = con.execute("SELECT COALESCE(SUM(entry_price*shares),0) FROM user_positions"
                        " WHERE user_id=? AND market=?",
                        (int(user_id), market)).fetchone()[0] or 0.0
    # Scoped to the current epoch, and compared on the TIMESTAMP: the legacy
    # ledger shares the calendar date the resize happened, so a date-only
    # compare drags the whole old book back in.
    realised = con.execute("SELECT COALESCE(SUM(pnl),0) FROM user_trades"
                           " WHERE user_id=? AND market=? AND COALESCE(closed_at,'')>=?",
                           (int(user_id), market,
                            epoch_of(con, user_id, market))).fetchone()[0] or 0.0
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
            "shares", "stop", "target", "opened_at", "sleeve", "regime")
    rows = con.execute(f"SELECT {','.join(cols)} FROM user_positions"
                       " WHERE user_id=? AND market=? ORDER BY id",
                       (int(user_id), market)).fetchall()
    return [dict(zip(cols, r)) for r in rows]


def open_symbols(con, user_id, market="IN"):
    return {r[0] for r in con.execute(
        "SELECT symbol FROM user_positions WHERE user_id=? AND market=?",
        (int(user_id), market))}


def buy(con, user_id, market, strategy, symbol, price, shares=None,
        stop=None, target=None, src_id=None):
    """Open a position in ONE user's book. Returns shares bought, or 0.

    Zero is a normal outcome, not a failure: a book too small for one share of
    a Rs 5,000 stock skips it. Refusing loudly there would turn a smaller
    account into an error message on every expensive name.
    """
    price = float(price or 0)
    if price <= 0:
        return 0
    if symbol in open_symbols(con, user_id, market):
        return 0
    n = con.execute("SELECT COUNT(*) FROM user_positions WHERE user_id=? AND market=?",
                    (int(user_id), market)).fetchone()[0]
    if n >= MAX_POSITIONS:
        return 0
    qty = int(shares) if shares else size_for(con, user_id, market, price)
    if qty < 1 or qty * price > cash(con, user_id, market):
        return 0
    now = datetime.now(IST)
    # OR IGNORE + returning qty regardless was a lie waiting to happen: the
    # unique index can block this (two mirrors racing on the same symbol) and
    # the caller would be told shares were bought that do not exist, leaving the
    # book's cash and its positions permanently disagreeing. rowcount is the
    # only honest answer.
    cur = con.execute("INSERT OR IGNORE INTO user_positions(user_id,market,strategy,"
                      "symbol,entry_date,entry_price,shares,stop,target,opened_at,src_id)"
                      " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                      (int(user_id), market, strategy, symbol, now.date().isoformat(),
                       price, qty, stop, target, now.isoformat(), src_id))
    con.commit()
    return qty if cur.rowcount else 0


def sell(con, user_id, market, symbol, price, reason="manual"):
    """Close a position in ONE user's book. Returns (pnl, return_pct) or None."""
    row = con.execute("SELECT id,strategy,entry_date,entry_price,shares,opened_at"
                      " FROM user_positions WHERE user_id=? AND market=? AND symbol=?",
                      (int(user_id), market, symbol)).fetchone()
    if not row:
        return None
    pid, strategy, edate, entry, shares, opened = row
    price = float(price or 0)
    if price <= 0:
        return None
    # SAME cost model as the house book. A user's book that reported gross P&L
    # while the engine reported net would make the two incomparable, which is
    # the whole reason for running them side by side.
    from .v2_live import net_trade_pnl
    net, pct = net_trade_pnl(market, shares, float(entry), price)
    now = datetime.now(IST)
    con.execute("INSERT INTO user_trades(user_id,market,strategy,symbol,entry_date,"
                "entry_price,exit_date,exit_price,shares,pnl,return_pct,reason,"
                "opened_at,closed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (int(user_id), market, strategy, symbol, edate, entry,
                 now.date().isoformat(), price, shares, net, pct, reason,
                 opened, now.isoformat()))
    con.execute("DELETE FROM user_positions WHERE id=?", (pid,))
    con.commit()
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
    realised = con.execute("SELECT COALESCE(SUM(pnl),0) FROM user_trades"
                           " WHERE user_id=? AND market=?",
                           (int(user_id), market)).fetchone()[0] or 0.0
    rets = [r[0] for r in con.execute("SELECT return_pct FROM user_trades"
                                      " WHERE user_id=? AND market=?",
                                      (int(user_id), market))]
    wins = [r for r in rets if r > 0]
    free = budget - sum(p["entry_price"] * p["shares"] for p in pos) + realised
    return dict(market=market, budget=budget, cash=round(free, 2),
                deployed=round(mtm, 2), equity=round(free + mtm, 2),
                overall_pnl=round(realised + unreal, 2), realised=round(realised, 2),
                unrealised=round(unreal, 2), positions=len(pos), trades=len(rets),
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
                 stop=None, target=None, src_id=None):
    """Fan the house book's entry out to every subscriber's own book."""
    done = 0
    for uid in subscribers(db, plans_mod):
        try:
            if buy(con, uid, market, strategy, symbol, price, None, stop, target, src_id):
                done += 1
        except Exception:
            _LOG.exception("book mirror entry failed for user %s", uid)
    return done


def mirror_exit(con, db, plans_mod, market, symbol, price, reason):
    """And the exit. Only books that actually hold it are touched."""
    done = 0
    for (uid,) in con.execute("SELECT DISTINCT user_id FROM user_positions"
                              " WHERE market=? AND symbol=?", (market, symbol)):
        try:
            if sell(con, uid, market, symbol, price, reason):
                done += 1
        except Exception:
            _LOG.exception("book mirror exit failed for user %s", uid)
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
