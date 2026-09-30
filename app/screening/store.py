"""Evidence captured now cannot become evidence available in an earlier replay."""
import json
import sqlite3
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS evidence(
 symbol TEXT,kind TEXT,known_at TEXT,source TEXT,payload TEXT,
 PRIMARY KEY(symbol,kind,known_at,source));
CREATE INDEX IF NOT EXISTS evidence_lookup ON evidence(symbol,kind,known_at);
CREATE TABLE IF NOT EXISTS screens(
 generated_at TEXT PRIMARY KEY,payload TEXT NOT NULL);
"""


def timestamp(value):
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("timezone-aware availability timestamp required")
    return dt.astimezone(timezone.utc)


def initialise(con):
    con.executescript(SCHEMA)


def save(con, symbol, kind, source, payload, known_at, now=None):
    known = timestamp(known_at)
    now = now or datetime.now(timezone.utc)
    if known > now or not source or not symbol:
        raise ValueError("identified source and non-future availability required")
    # JSON strictness rejects NaN/Infinity instead of silently breaking the API.
    encoded = json.dumps(payload, allow_nan=False)
    con.execute("INSERT OR REPLACE INTO evidence VALUES(?,?,?,?,?)",
                (symbol.upper(), kind, known.isoformat(), source, encoded))


def latest(con, symbol, kind, now, max_days):
    rows = con.execute("SELECT known_at,source,payload FROM evidence "
                       "WHERE symbol=? AND kind=? AND julianday(known_at)<=julianday(?) "
                       "ORDER BY julianday(known_at) DESC LIMIT 1",
                       (symbol, kind, now.isoformat())).fetchall()
    if not rows or (now - timestamp(rows[0][0])).total_seconds() > max_days * 86400:
        return None
    known, source, payload = rows[0]
    data = json.loads(payload)
    return dict(data, known_at=known, source=source)


def report(path, now=None):
    now = now or datetime.now(timezone.utc)
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        try:
            row = con.execute("SELECT payload FROM screens WHERE julianday(generated_at) "
                              "<=julianday(?) ORDER BY julianday(generated_at) DESC LIMIT 1",
                              (now.isoformat(),)).fetchone()
        finally:
            con.close()
        data = json.loads(row[0]) if row else {}
    except (sqlite3.Error, OSError, ValueError):
        data = {}
    if not data:
        return dict(status="unavailable", equities=[], indices=[],
                    note="Evidence screen has not completed; no quality claims available")
    data["stale"] = (now - timestamp(data["generated_at"])).total_seconds() > 7200
    # The web response may be served between scheduled refreshes. A chain
    # fresh at capture must not remain labelled fresh for the next half hour.
    for row in data.get("indices", []):
        options = row.get("options")
        if options:
            try:
                age = (now - timestamp(options["published_at"])).total_seconds()
            except (KeyError, ValueError, TypeError):
                age = -1
            if not 0 <= age <= 300:
                row["options"] = None
                row.setdefault("flags", []).append("option chain expired since screen capture")
    return data
