"""Real-time NSE corporate-announcements ingester — the same-day catalyst source
the v2 volume_surge lane gates on.

Our Bing-RSS sentiment feed proved too slow/incomplete (validation: it tagged
M&M Financial a day late, missed TVS Motor entirely). NSE's own corporate-
announcements API publishes results / orders / board outcomes in real time,
keyed by the exact NSE `symbol`. This poller pulls it, classifies each filing
into a tradeable category, and writes a compact `nse_announcements` table the
engine reads to answer "does this symbol have a fresh material catalyst?".

  python3 scripts/nse_announcements.py --once     # one poll
  python3 scripts/nse_announcements.py --loop --interval 300

Runs via systemd timer during market hours. Read-only on everything except its
own table. Self-prunes rows older than 7 days.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import time
from datetime import datetime, timezone

import httpx

# Own DEDICATED db (WAL): the 12GB trading_agent.db is rollback-journal mode where
# a writer locks out all readers — adding this poller there stalled the engine's
# catalyst reads (and the ingester's own writes). A separate WAL file lets writers
# and the engine's readers proceed concurrently with zero contention.
DB = os.environ.get("CATALYST_DB", "/opt/opentrade/var/catalysts.db")
URL = "https://www.nseindia.com/api/corporate-announcements?index=equities"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
      "Accept": "application/json,text/plain,*/*", "Accept-Language": "en-US,en;q=0.9",
      "Referer": "https://www.nseindia.com/"}

# Subject/text -> tradeable category. Everything not matched is 'other' (ignored
# by the catalyst gate) so procedural filings (trading-window, AGM notices,
# duplicate-share, newspaper ads, con-call schedules) never count as a catalyst.
RESULTS_KEYS = ("financial result", "outcome of board meeting", "un-audited",
                "unaudited", "audited financial", "quarterly result")
ORDER_KEYS = ("order", "contract", "bagged", "work order", "letter of award", "loa",
              "awarded", "secures", "wins ", "bags ", "purchase order",
              "receipt of order", "emerges as l1", "lowest bidder", "l1 bidder", "letter of intent")
CORP_KEYS = ("buyback", "buy-back", "bonus issue", "acquisition", "amalgamation",
             "merger", "fund rais", "fundrais", "preferential", "qip", "stake",
             "allotment of equity", "allotment of shares", "issue of securities",
             "rights issue", "warrants", "capacity expansion", "commissioning", "capex")
NOISE_KEYS = ("trading window", "loss of", "duplicate", "newspaper", "postal ballot",
              "compliance certificate", "compliance report", "reg. 74", "record date",
              "sub-division", "annual report", "agm", "investor meet", "con. call",
              "conference call", "analysts", "institutional investor", "shareholders meeting",
              "spurt in volume", "spurt in price", "clarification", "monitoring agency",
              "depositories", "authorised capital", "appointment", "investor presentation",
              "esop", "espx", "esos", "esps")
# bearish / value-destroying -> NEVER a buy catalyst, even if the text mentions
# "contract" or "order" (e.g. "Rescission/termination of contract" is a LOSS).
NEGATIVE_KEYS = ("termination", "rescission", "cancellation", "cancelled", "withdrawal",
                 "default", "resignation", "downgrade", "insolvency", "nclt", "fraud",
                 "penalty", "suspension", "delisting")


def classify(subject: str, text: str) -> str:
    s = ((subject or "") + " " + (text or "")).lower()
    # exclusions FIRST so a bearish/procedural filing never masquerades as a catalyst
    if any(k in s for k in NEGATIVE_KEYS):
        return "noise"
    if any(k in s for k in NOISE_KEYS):
        return "noise"
    if any(k in s for k in RESULTS_KEYS):
        return "results"
    if any(k in s for k in ORDER_KEYS):
        return "order"
    if any(k in s for k in CORP_KEYS):
        return "corp_action"
    return "other"


def _epoch(an_dt: str) -> int:
    # "23-Jul-2026 17:00:07" (IST) -> unix epoch
    try:
        dt = datetime.strptime(an_dt.strip(), "%d-%b-%Y %H:%M:%S")
        return int(dt.timestamp()) - int(5.5 * 3600)  # naive parse is IST -> to UTC epoch
    except Exception:
        return 0


def ensure_schema(c):
    c.execute("PRAGMA journal_mode=WAL")   # readers never block on the writer
    c.execute("""CREATE TABLE IF NOT EXISTS nse_announcements(
        symbol TEXT, an_epoch INTEGER, an_dt TEXT, category TEXT, subject TEXT,
        text TEXT, ingested_at TEXT, PRIMARY KEY(symbol, an_epoch, subject))""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_nse_ann_sym ON nse_announcements(symbol, an_epoch)")
    c.commit()


def fetch(client):
    client.get("https://www.nseindia.com")   # bootstrap cookies
    time.sleep(1)
    # today's FULL range (the bare endpoint returns only the latest 20, which
    # drops filings during a results-hour burst; the date range returns them all)
    today = datetime.now().strftime("%d-%m-%Y")
    r = client.get(URL + "&from_date=%s&to_date=%s" % (today, today))
    if r.status_code != 200:
        r = client.get(URL)   # fallback to latest-20 if the range query is rejected
        if r.status_code != 200:
            return []
    d = r.json()
    return d if isinstance(d, list) else (d.get("data") or [])


def poll_once(verbose=True):
    c = sqlite3.connect(DB, timeout=30)
    c.execute("PRAGMA busy_timeout=10000")
    ensure_schema(c)
    try:
        with httpx.Client(headers=UA, timeout=20, follow_redirects=True) as client:
            rows = fetch(client)
    except Exception as exc:
        print("NSE fetch error:", str(exc)[:120], flush=True)
        c.close(); return 0
    now = datetime.now(timezone.utc).isoformat()
    ins = mat = 0
    for x in rows:
        sym = (x.get("symbol") or "").upper().strip()
        if not sym:
            continue
        subject = (x.get("desc") or "")[:200]
        text = (x.get("attchmntText") or "")[:500]
        cat = classify(subject, text)
        an_dt = x.get("an_dt") or ""
        ep = _epoch(an_dt)
        try:
            cur = c.execute("INSERT OR IGNORE INTO nse_announcements"
                            "(symbol,an_epoch,an_dt,category,subject,text,ingested_at) VALUES(?,?,?,?,?,?,?)",
                            (sym, ep, an_dt, cat, subject, text, now))
            if cur.rowcount:
                ins += 1
                if cat in ("results", "order", "corp_action"):
                    mat += 1
        except Exception:
            pass
    # prune > 7 days
    c.execute("DELETE FROM nse_announcements WHERE an_epoch < ?", (int(time.time()) - 7 * 86400,))
    c.commit(); c.close()
    if verbose:
        print("[%s] pulled %d rows, +%d new (%d material) " %
              (datetime.now().strftime("%H:%M:%S"), len(rows), ins, mat), flush=True)
    return ins


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--interval", type=float, default=300)
    a = ap.parse_args()
    if a.loop:
        while True:
            try:
                poll_once()
            except Exception as exc:
                print("poll error:", str(exc)[:120], flush=True)
            time.sleep(a.interval)
    else:
        poll_once()


if __name__ == "__main__":
    main()
