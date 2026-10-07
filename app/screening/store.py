"""Evidence captured now cannot become evidence available in an earlier replay."""
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

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
    for verb in ('UPDATE', 'DELETE'):
        con.execute(f'CREATE TRIGGER IF NOT EXISTS screening_evidence_no_{verb.lower()} BEFORE {verb} '
                    "ON evidence BEGIN SELECT RAISE(ABORT,'source evidence is immutable'); END")


def _object(encoded):
    def invalid_constant(value):
        raise ValueError('non-finite evidence')
    data = json.loads(encoded, parse_constant=invalid_constant)
    if not isinstance(data, dict):
        raise ValueError('evidence object required')
    return data


def save(con, symbol, kind, source, payload, known_at, now=None):
    known = timestamp(known_at)
    now = now or datetime.now(timezone.utc)
    if known > now or not isinstance(source, str) or not source.strip() or \
            not isinstance(symbol, str) or not symbol.strip() or not isinstance(payload, dict):
        raise ValueError("identified source and non-future availability required")
    # JSON strictness rejects NaN/Infinity instead of silently breaking the API.
    encoded = json.dumps(payload, allow_nan=False)
    con.execute("INSERT OR IGNORE INTO evidence VALUES(?,?,?,?,?)",
                (symbol.upper(), kind, known.isoformat(), source, encoded))
    saved = con.execute('SELECT payload FROM evidence WHERE symbol=? AND kind=? AND known_at=? AND source=?',
                        (symbol.upper(), kind, known.isoformat(), source)).fetchone()
    if not saved or _object(saved[0]) != payload:
        raise ValueError('source identity cannot overwrite original evidence; capture a dated correction')


def latest(con, symbol, kind, now, max_days):
    # SQLite julianday rounds sub-millisecond instants. It is only a bounded
    # candidate lookup; exact aware datetimes decide what was known NOW.
    rows = con.execute("SELECT known_at,source,payload FROM evidence "
                       "WHERE symbol=? AND kind=? AND julianday(known_at)<=julianday(?) "
                       "ORDER BY julianday(known_at) DESC LIMIT 128",
                       (symbol, kind, (now+timedelta(milliseconds=1)).isoformat())).fetchall()
    candidates = []
    try:
        for known, source, payload in rows:
            at = timestamp(known)
            if 0 <= (now-at).total_seconds() <= max_days*86400:
                candidates.append((at, known, source, _object(payload)))
        if not candidates:
            return None
        newest = max(r[0] for r in candidates)
        matching = [r for r in candidates if r[0] == newest]
        first = matching[0][3]
        if any(r[3] != first for r in matching):
            return None  # Equal-time conflicting sources/corrections are unknown.
        sources = sorted({r[2] for r in matching})
        if not sources or not all(isinstance(s, str) and s.strip() for s in sources):
            return None
        return dict(first, known_at=newest.isoformat(), source=sources[0], sources=sources)
    except (ValueError, TypeError):
        return None


def _current_rows(data, now):
    """Expiry is evaluated when Ideas is read, not frozen at screen capture."""
    rows = data.get('equities', [])
    if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
        raise ValueError('invalid screen rows')
    for row in rows:
        flags = row.setdefault('flags', [])
        if not isinstance(flags, list):
            raise ValueError('invalid screen flags')
        for kind, days, reason in (
                ('news', 2/24, 'official news feed not freshly checked'),
                ('earnings', 2, 'earnings calendar not freshly checked'),
                ('fundamentals', 14, 'financial statements unavailable or stale')):
            evidence = row.get(kind)
            try:
                fresh = isinstance(evidence, dict) and 0 <= (now-timestamp(evidence['known_at'])).total_seconds() <= days*86400
                if kind == 'news':
                    fresh = fresh and 0 <= (now-timestamp(evidence['checked_at'])).total_seconds() <= 7200
            except (KeyError, ValueError, TypeError):
                fresh = False
            if not fresh and reason not in flags:
                flags.append(reason)
        if flags:
            row['status'] = 'REVIEW REQUIRED'


def report(path, now=None):
    now = now or datetime.now(timezone.utc)
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        try:
            rows = con.execute("SELECT generated_at,payload FROM screens WHERE julianday(generated_at) "
                               "<=julianday(?) ORDER BY julianday(generated_at) DESC LIMIT 32",
                               ((now+timedelta(milliseconds=1)).isoformat(),)).fetchall()
        finally:
            con.close()
        candidates = [(timestamp(at), encoded) for at, encoded in rows if timestamp(at) <= now]
        row = max(candidates, key=lambda r:r[0]) if candidates else None
        data = _object(row[1]) if row else {}
        if data:
            if timestamp(data['generated_at']) != row[0]:
                raise ValueError('screen availability does not match its record')
            data['stale'] = (now-row[0]).total_seconds() > 7200
            try:
                price_day = datetime.strptime(data['price_asof'], '%Y-%m-%d').date()
                local = now.astimezone(ZoneInfo('Asia/Kolkata'))
                completed = price_day < local.date() or (price_day == local.date() and (local.hour,local.minute) >= (15,30))
                data['price_stale'] = not completed or not 0 <= (local.date()-price_day).days <= 4
            except (KeyError, ValueError, TypeError):
                data['price_stale'] = True
            _current_rows(data, now)
    except (sqlite3.Error, OSError, ValueError, KeyError, TypeError):
        data = {}
    if not data:
        return dict(status="unavailable", equities=[], indices=[],
                    note="Evidence screen has not completed; no quality claims available")
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
        historical = row.get("last_option_snapshot")
        if historical:
            try:
                age = (now - timestamp(historical["published_at"])).total_seconds()
                expiry = datetime.strptime(historical["expiry"], "%d-%b-%Y").date()
                valid = 0 <= age <= 4*86400 and expiry >= now.date()
            except (KeyError, ValueError, TypeError):
                valid = False
            if not valid:
                row["last_option_snapshot"] = None
    return data
