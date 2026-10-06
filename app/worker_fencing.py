"""One paper-engine writer across web workers, with checked fencing tokens."""
import time
from contextvars import ContextVar

from .account_safety import atomic

ACTIVE = ContextVar("paper_engine_fence", default=None)


def ensure_schema(con):
    con.execute("CREATE TABLE IF NOT EXISTS worker_leases("
                "name TEXT PRIMARY KEY,owner TEXT NOT NULL,generation INTEGER NOT NULL,expires REAL NOT NULL)")


def acquire(con, owner, ttl=120, now=None):
    now = time.time() if now is None else now
    with atomic(con):
        row = con.execute("SELECT owner,generation,expires FROM worker_leases WHERE name='paper' ").fetchone()
        if row and row[0] != owner and row[2] > now:
            return None
        generation = (row[1] if row and row[0] == owner and row[2] > now else (row[1] + 1 if row else 1))
        con.execute("INSERT INTO worker_leases VALUES('paper',?,?,?) ON CONFLICT(name) DO UPDATE SET "
                    "owner=excluded.owner,generation=excluded.generation,expires=excluded.expires",
                    (owner, generation, now + ttl))
    return owner, generation


def require_current(con, token=None, now=None):
    token = ACTIVE.get() if token is None else token
    if token is None:
        return  # Explicit synchronous diagnostics and isolated test accounts.
    now = time.time() if now is None else now
    row = con.execute("SELECT owner,generation,expires FROM worker_leases WHERE name='paper'").fetchone()
    if not row or tuple(row[:2]) != tuple(token) or row[2] <= now:
        raise RuntimeError("stale paper-engine fence; write refused")
