"""Shared, bounded login reservations; count in-flight hash work across workers.

Store only a digest of the client/account pair. A successful attempt releases
its own reservation, never failures belonging to concurrent attempts.
"""
import hashlib
import math
import secrets
import sqlite3

from .account_safety import atomic


def ensure_schema(con):
    con.execute('CREATE TABLE IF NOT EXISTS auth_login_reservations('
                'id TEXT PRIMARY KEY,key_hash TEXT NOT NULL,started_at REAL NOT NULL,outcome TEXT NOT NULL)')
    con.execute('CREATE INDEX IF NOT EXISTS auth_login_reservations_key '
                'ON auth_login_reservations(key_hash,started_at)')
    con.execute('CREATE TABLE IF NOT EXISTS auth_login_locks('
                'key_hash TEXT PRIMARY KEY,locked_until REAL NOT NULL)')


def _key(key):
    return hashlib.sha256(key.encode()).hexdigest()


def reserve(con, key, now, *, maximum=5, window=300, lockout=900):
    if not math.isfinite(now) or maximum < 1 or window < 1 or lockout < window:
        raise ValueError('Invalid login guard policy')
    hashed = _key(key)
    with atomic(con):
        con.execute('DELETE FROM auth_login_reservations WHERE started_at<=?', (now-window,))
        con.execute('DELETE FROM auth_login_locks WHERE locked_until<=?', (now,))
        row = con.execute('SELECT locked_until FROM auth_login_locks WHERE key_hash=?', (hashed,)).fetchone()
        if row:
            return None, max(1, math.ceil(row[0]-now))
        count = con.execute('SELECT COUNT(*) FROM auth_login_reservations WHERE key_hash=?', (hashed,)).fetchone()[0]
        if count >= maximum:
            con.execute('INSERT INTO auth_login_locks VALUES(?,?)', (hashed, now+lockout))
            return None, lockout
        ticket = secrets.token_urlsafe(24)
        con.execute('INSERT INTO auth_login_reservations VALUES(?,?,?,?)', (ticket, hashed, now, 'pending'))
        return ticket, 0


def finish(con, ticket, successful, now, *, maximum=5, window=300, lockout=900):
    with atomic(con):
        row = con.execute('SELECT key_hash,outcome FROM auth_login_reservations WHERE id=?', (ticket,)).fetchone()
        if not row or row[1] != 'pending':
            return  # Idempotent; an expired attempt cannot clear a lock.
        if successful:
            con.execute('DELETE FROM auth_login_reservations WHERE id=?', (ticket,))
        else:
            con.execute("UPDATE auth_login_reservations SET outcome='failed' WHERE id=?", (ticket,))
            failed = con.execute("SELECT COUNT(*) FROM auth_login_reservations WHERE key_hash=? "
                                 "AND started_at>? AND outcome='failed'", (row[0], now-window)).fetchone()[0]
            if failed >= maximum:
                con.execute('INSERT INTO auth_login_locks VALUES(?,?) ON CONFLICT(key_hash) '
                            'DO UPDATE SET locked_until=MAX(locked_until,excluded.locked_until)', (row[0], now+lockout))
