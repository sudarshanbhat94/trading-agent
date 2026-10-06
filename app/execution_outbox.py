"""Durable paper-to-account delivery; no HTTP inside a book transaction."""
import json
import time
import uuid

from .account_safety import atomic
from .worker_fencing import require_current


def ensure_schema(con):
    from .incident_inbox import ensure_schema as ensure_inbox
    ensure_inbox(con)
    con.execute("CREATE TABLE IF NOT EXISTS execution_outbox("
                "id INTEGER PRIMARY KEY,semantic_key TEXT NOT NULL UNIQUE,topic TEXT NOT NULL,"
                "payload TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',attempts INTEGER DEFAULT 0,"
                "lease_token TEXT,lease_until REAL DEFAULT 0,next_attempt REAL DEFAULT 0,last_error TEXT)")
    con.execute("CREATE TABLE IF NOT EXISTS execution_incidents("
                "id INTEGER PRIMARY KEY,user_id INTEGER NOT NULL,code TEXT NOT NULL,reference TEXT NOT NULL,"
                "detail TEXT NOT NULL,opened_at REAL NOT NULL,resolved_at REAL,"
                "UNIQUE(user_id,code,reference))")


def incident(con, user_id, code, reference, detail):
    con.execute("INSERT INTO execution_incidents(user_id,code,reference,detail,opened_at) "
                "VALUES(?,?,?,?,?) ON CONFLICT(user_id,code,reference) DO UPDATE SET "
                "detail=excluded.detail,resolved_at=NULL",
                (int(user_id), code, str(reference), str(detail)[:400], time.time()))


def enqueue(con, key, topic, payload):
    encoded = json.dumps(payload,sort_keys=True)
    existing = con.execute("SELECT topic,payload FROM execution_outbox WHERE semantic_key=?",(key,)).fetchone()
    if existing and tuple(existing)!=(topic,encoded):
        raise ValueError("execution event identity cannot be rebound")
    con.execute("INSERT OR IGNORE INTO execution_outbox(semantic_key,topic,payload) VALUES(?,?,?)",
                (key, topic, encoded))


def drain(con, deliver=None, limit=20, now=None):
    """Expired leases retry the same semantic event, never a fresh signal."""
    if con.in_transaction:
        raise RuntimeError("outbox delivery requires a committed connection")
    now = time.time() if now is None else now
    delivered = 0
    for _ in range(limit):
        token = uuid.uuid4().hex
        with atomic(con):
            require_current(con)
            row = con.execute("SELECT id,topic,payload FROM execution_outbox WHERE status!='done' "
                              "AND lease_until<=? AND next_attempt<=? ORDER BY id LIMIT 1", (now, now)).fetchone()
            if not row:
                break
            con.execute("UPDATE execution_outbox SET lease_token=?,lease_until=?,attempts=attempts+1 "
                        "WHERE id=?", (token, now + 120, row[0]))
        try:
            (deliver or _deliver)(con, row[1], json.loads(row[2]))
        except Exception as exc:
            with atomic(con):
                require_current(con)
                # Logs deliberately omit exception text which may contain a
                # broker URL, response, token or external filing text.
                updated = con.execute("UPDATE execution_outbox SET lease_until=0,next_attempt=?,last_error=? "
                            "WHERE id=? AND lease_token=?", (now + 30, type(exc).__name__, row[0], token))
                if updated.rowcount:
                    incident(con, 0, "OUTBOX_DELIVERY_FAILED", row[0], type(exc).__name__)
        else:
            with atomic(con):
                require_current(con)
                updated = con.execute("UPDATE execution_outbox SET status='done',lease_until=0,last_error=NULL "
                            "WHERE id=? AND lease_token=?", (row[0], token))
                if updated.rowcount:
                    con.execute("UPDATE execution_incidents SET resolved_at=? WHERE user_id=0 "
                                "AND code='OUTBOX_DELIVERY_FAILED' AND reference=?", (now, str(row[0])))
            delivered += bool(updated.rowcount)
    return delivered


def _deliver(con, topic, payload):
    from . import v2_live, books, plans, broker
    from datetime import datetime, timezone
    if topic == "house_entry":
        # A delayed entry must never be resurrected after the source exited or
        # its epoch changed. Exits have separate durable delivery events.
        position = con.execute("SELECT 1 FROM v2_positions WHERE id=?", (payload["src_id"],)).fetchone()
        book = con.execute("SELECT started_at FROM v2_book WHERE market=?", (payload["market"],)).fetchone()
        if not position or not book or book[0] != payload["house_epoch"]:
            return
        from .sleeves.feeds import fresh_quotes
        from .sleeves.risk import SLIPPAGE
        marks = fresh_quotes(v2_live._live(payload["market"], [payload["symbol"]]), datetime.now(timezone.utc))
        quote = marks.get(payload["symbol"])
        if quote is None:
            raise RuntimeError("entry quote unavailable")
        price = float(quote["price"])
        if abs(price / payload["price"] - 1) > SLIPPAGE:
            incident(con, 0, "MIRROR_ENTRY_MISSED", payload["src_id"], "Current price exceeds the approved slippage allowance")
            con.commit()
            return
        errors = []
        for uid in books.subscribers(None, plans):
            try:
                epoch = books.current_epoch(con, uid, payload["market"])
                if epoch != books.LEGACY_EPOCH and epoch > payload["created_at"]:
                    continue  # Do not reintroduce a pre-reset entry.
                books.buy(con, uid, payload["market"], payload["strategy"], payload["symbol"],
                          price, payload["max_shares"], payload["stop"], payload["target"],
                          payload["src_id"], payload["sleeve"], payload["regime"],
                          exit_policy=payload["exit_policy"], request_key="house:" + str(payload["src_id"]))
            except Exception:
                errors.append(uid)
        # Journal submission has its own durable origin-derived semantic key.
        v2_live._live_mirror_entry(con, payload["market"], payload["strategy"], payload["symbol"],
                                   price, src_id=payload["src_id"], stop=payload["stop"],
                                   target=payload["target"], strict=True)
        if errors:
            raise RuntimeError("personal delivery incomplete")
    elif topic == "house_exit":
        books.mirror_exit(con, None, plans, payload["market"], payload["symbol"], payload["price"],
                          payload["reason"], src_id=payload["src_id"], strict=True)
        v2_live._live_mirror_exit(con, payload["market"], payload["symbol"], payload["price"],
                                  payload["reason"], src_id=payload["src_id"], strict=True)
    else:
        raise ValueError("unknown execution event")
