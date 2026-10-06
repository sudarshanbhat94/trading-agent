"""Small durable account invariants, independent of chart retention."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from uuid import uuid4


def ensure_schema(con):
    con.execute("CREATE TABLE IF NOT EXISTS account_risk_state("
                "kind TEXT,user_id INTEGER,market TEXT,epoch TEXT,"
                "peak_equity REAL NOT NULL,last_equity REAL NOT NULL,"
                "observed_at TEXT NOT NULL,session_day TEXT,session_open REAL,"
                "PRIMARY KEY(kind,user_id,market,epoch))")


@contextmanager
def atomic(con):
    """Serialize check/write; never commit a caller's existing transaction."""
    nested = con.in_transaction
    name = "account_" + uuid4().hex
    con.execute("SAVEPOINT " + name if nested else "BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        if nested:
            con.execute("ROLLBACK TO " + name)
            con.execute("RELEASE " + name)
        else:
            con.rollback()
        raise
    else:
        con.execute("RELEASE " + name) if nested else con.commit()


def observe(con, kind, uid, market, epoch, equity, capital, day,
            session_open=None, historical_peak=None):
    """Call from a writer, with a complete mark. Preserve every observed peak."""
    peak = max(float(equity), float(capital), float(historical_peak or capital))
    con.execute("INSERT INTO account_risk_state(kind,user_id,market,epoch,"
                "peak_equity,last_equity,observed_at,session_day,session_open) "
                "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(kind,user_id,market,epoch) "
                "DO UPDATE SET peak_equity=MAX(account_risk_state.peak_equity,excluded.peak_equity),"
                "last_equity=excluded.last_equity,observed_at=excluded.observed_at,"
                "session_day=excluded.session_day,session_open=CASE "
                "WHEN account_risk_state.session_day=excluded.session_day THEN "
                "COALESCE(account_risk_state.session_open,excluded.session_open) "
                "ELSE excluded.session_open END",
                (kind, int(uid), market, epoch, peak, float(equity),
                 datetime.now(timezone.utc).isoformat(), day, session_open))


def peak(con, kind, uid, market, epoch, fallback):
    row = con.execute("SELECT peak_equity FROM account_risk_state WHERE "
                      "kind=? AND user_id=? AND market=? AND epoch=?",
                      (kind, int(uid), market, epoch)).fetchone()
    return max(float(fallback), float(row[0])) if row else float(fallback)
