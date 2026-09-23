"""Point-in-time reference snapshots supplied by an identified data provider.

The importer requires publication/availability timestamps. Absence never
becomes an invented fundamental score or a claim of index membership.
"""
import csv
import io
import logging
import math
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

PATH = os.getenv("SLEEVE_REFERENCE_DB", str(Path(__file__).resolve().parents[2] / "var" / "sleeve_reference.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS fundamentals(
 symbol TEXT,known_at TEXT,source TEXT,roe_pct REAL,debt_equity REAL,eps_growth_std_pct REAL,
 PRIMARY KEY(symbol,known_at,source));
CREATE TABLE IF NOT EXISTS membership(
 symbol TEXT,index_name TEXT,known_at TEXT,source TEXT,
 PRIMARY KEY(symbol,index_name,known_at,source));
"""

_LAST_REFRESH = 0.0
_FACTOR_LAST_ATTEMPT = 0.0
FACTOR_SOURCE = "NSE Nifty100 and Nifty500 Multicap Momentum Quality 50 constituent files"
FACTOR_URLS = {
    "NIFTY100": "https://www.niftyindices.com/IndexConstituent/ind_nifty100list.csv",
    "NIFTY500_MQ50": "https://www.niftyindices.com/IndexConstituent/ind_nifty500MulticapMomentumQuality50_list.csv",
}


def _parse_constituents(body: str, expected: int) -> list[str]:
    """Reject an incomplete or changed NSE file instead of trading a partial list."""
    reader = csv.DictReader(io.StringIO(body.lstrip("\ufeff")))
    if not reader.fieldnames or not {"Symbol", "Series", "ISIN Code"} <= set(reader.fieldnames):
        raise ValueError("NSE constituent columns changed")
    symbols, isins = set(), set()
    for row in reader:
        symbol = (row.get("Symbol") or "").strip().upper()
        isin = (row.get("ISIN Code") or "").strip().upper()
        if (row.get("Series") or "").strip().upper() != "EQ" or not symbol or not isin.startswith("INE") or len(isin) != 12:
            raise ValueError("invalid NSE equity constituent")
        if symbol in symbols or isin in isins:
            raise ValueError("duplicate NSE constituent")
        symbols.add(symbol)
        isins.add(isin)
    if len(symbols) != expected:
        raise ValueError(f"expected {expected} constituents, got {len(symbols)}")
    return sorted(symbols)


def refresh_factor_membership(path=PATH):
    """Fetch both official files as one dated batch; fail closed on errors."""
    global _FACTOR_LAST_ATTEMPT
    now = datetime.now(timezone.utc)
    if factor_members(now, path) is not None and time.time() - _FACTOR_LAST_ATTEMPT < 86400:
        return
    if time.time() - _FACTOR_LAST_ATTEMPT < 3600:
        return
    _FACTOR_LAST_ATTEMPT = time.time()
    import httpx
    try:
        membership = {}
        with httpx.Client(timeout=15, follow_redirects=True,
                          headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.niftyindices.com/"}) as client:
            for name, url in FACTOR_URLS.items():
                response = client.get(url)
                response.raise_for_status()
                membership[name] = _parse_constituents(response.text, 100 if name == "NIFTY100" else 50)
        overlap = set(membership["NIFTY100"]) & set(membership["NIFTY500_MQ50"])
        if not 5 <= len(overlap) <= 15:
            raise ValueError("unexpected large-cap factor intersection")
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(path, timeout=10)
        try:
            import_snapshot(con, dict(source=FACTOR_SOURCE, known_at=now.isoformat(),
                                      membership=membership), now=now)
        finally:
            con.close()
    except (httpx.HTTPError, OSError, ValueError, sqlite3.Error) as exc:
        # A failed refresh never replaces the last valid batch. The reader
        # independently checks freshness before allowing any new entry.
        logging.getLogger("openstocks.sleeves.reference").warning(
            "factor constituents unavailable; stock entries blocked if cache is stale: %s", exc)
        return


def factor_members(now, path=PATH):
    """Large-cap names in the NSE quality+momentum index, known within 7 days."""
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            stamp = now.astimezone(timezone.utc).isoformat()
            rows = con.execute(
                "SELECT symbol,index_name FROM membership WHERE source=? AND known_at=("
                "SELECT MAX(known_at) FROM membership WHERE source=? AND index_name='NIFTY100' "
                "AND julianday(known_at)<=julianday(?) AND julianday(known_at)>=julianday(?)-7)",
                (FACTOR_SOURCE, FACTOR_SOURCE, stamp, stamp)).fetchall()
        finally:
            con.close()
    except sqlite3.OperationalError:
        return None
    large = {s for s, index in rows if index == "NIFTY100"}
    factor = {s for s, index in rows if index == "NIFTY500_MQ50"}
    if len(large) != 100 or len(factor) != 50 or not 5 <= len(large & factor) <= 15:
        return None
    return large & factor


def refresh_membership(path=PATH):
    """Refresh the public NSE constituent snapshot at most once a day."""
    global _LAST_REFRESH
    now = datetime.now(timezone.utc)
    eligible, _ = snapshot(now, path)
    if eligible is not None and time.time() - _LAST_REFRESH < 86400:
        return
    if time.time() - _LAST_REFRESH < 3600:
        return
    _LAST_REFRESH = time.time()
    from ..bars5m import fetch_members
    symbols = fetch_members()
    if not 400 <= len(symbols) <= 600:
        return
    Path(path).parent.mkdir(parents=True,exist_ok=True)
    con = sqlite3.connect(path,timeout=10)
    try:
        import_snapshot(con,dict(source="NSE Nifty 500 constituent file",known_at=now.isoformat(),
                                  membership={"NIFTY500":sorted(symbols)}),now=now)
    finally:
        con.close()


def import_snapshot(con, data, now=None):
    """Validate the complete batch before any write. Timestamped JSON input."""
    now = now or datetime.now(timezone.utc)
    source = str(data.get("source") or "").strip()
    known = datetime.fromisoformat(data["known_at"].replace("Z", "+00:00"))
    if not source or known.tzinfo is None or known > now:
        raise ValueError("source and a non-future, timezone-aware known_at are required")
    stamp = known.astimezone(timezone.utc).isoformat()
    fundamentals = []
    for row in data.get("fundamentals", []):
        symbol = str(row["symbol"]).strip().upper()
        values = [float(row[k]) for k in ("roe_pct", "debt_equity", "eps_growth_std_pct")]
        if not symbol or not all(math.isfinite(v) for v in values) or min(values[1:]) < 0:
            raise ValueError("invalid fundamental values")
        fundamentals.append((symbol,stamp,source,*values))
    members = []
    for index_name, symbols in data.get("membership", {}).items():
        bounds = {"NIFTY50": (45, 60), "NIFTY100": (90, 115),
                  "NIFTY500": (400, 600), "NIFTY500_MQ50": (50, 50)}
        if index_name not in bounds or not isinstance(symbols, list):
            raise ValueError("membership must be a complete named NSE index snapshot")
        symbols = {str(s).strip().upper() for s in symbols}
        low, high = bounds[index_name]
        if "" in symbols or not low <= len(symbols) <= high:
            raise ValueError("incomplete or invalid constituent snapshot")
        members.extend((s,index_name,stamp,source) for s in symbols)
    if not fundamentals and not members:
        raise ValueError("empty reference snapshot")
    con.executescript(SCHEMA)
    with con:
        con.executemany("INSERT OR REPLACE INTO fundamentals VALUES(?,?,?,?,?,?)",fundamentals)
        con.executemany("INSERT OR REPLACE INTO membership VALUES(?,?,?,?)",members)
    return dict(fundamentals=len(fundamentals),members=len(members))


def snapshot(now, path=PATH):
    """Return eligible members and cross-sectional quality scores as known now."""
    import pandas as pd
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro",uri=True)
        try:
            stamp = now.astimezone(timezone.utc).isoformat()
            rows = con.execute("SELECT symbol,roe_pct,debt_equity,eps_growth_std_pct FROM ("
                "SELECT *,ROW_NUMBER() OVER(PARTITION BY symbol ORDER BY julianday(known_at) DESC) rn "
                "FROM fundamentals WHERE julianday(known_at)<=julianday(?) "
                "AND julianday(known_at)>=julianday(?)-180) WHERE rn=1",(stamp,stamp)).fetchall()
            members = con.execute("SELECT symbol FROM membership WHERE index_name='NIFTY500' "
                "AND known_at=(SELECT MAX(known_at) FROM membership WHERE index_name='NIFTY500' "
                "AND julianday(known_at)<=julianday(?) AND julianday(known_at)>=julianday(?)-31)",
                (stamp,stamp)).fetchall()
        finally:
            con.close()
    except sqlite3.OperationalError:
        return None, {}
    eligible = {r[0] for r in members} or None
    rows = [r for r in rows if eligible is not None and r[0] in eligible]
    if len(rows) < 20:
        return eligible, {}
    df = pd.DataFrame(rows,columns=["symbol","roe","leverage","variability"]).set_index("symbol")
    # Transparent equal-weight ranks, high ROE and low leverage/earnings
    # variability. This is a research hypothesis, not a validated profit model.
    quality = (df.roe.rank(pct=True) + (-df.leverage).rank(pct=True)
               + (-df.variability).rank(pct=True)) / 3
    return eligible, quality.to_dict()
