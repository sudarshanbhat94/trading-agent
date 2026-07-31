"""What is actually moving the index right now.

The index direction call was five readings off DAILY futures bars — on expiry
day it was reading yesterday's candle to trade a contract expiring in hours.
Nobody buys an index on yesterday's data. Meanwhile every input that genuinely
explains a Nifty move was already sitting in the database, unused.

Nifty is not an abstraction: it is ~50 stocks, and its top ten are roughly 60%
of it. If HDFC Bank, Reliance and ICICI are green, the index is green almost
regardless of what the other forty do. So the first question is not "what does
the chart say" but "which heavyweights are moving, and is the rest of the
market following".

Five readings, all from LIVE data rather than yesterday's close:

  contribution  turnover-weighted move of the heavyweights, which is what
                actually drags the index
  breadth       advances minus declines across the whole universe — a rally
                carried by three stocks is not the same as one carried by four
                hundred, and only breadth tells them apart
  sector        which sectors are leading, using the real industry labels
  volume        CASH market participation, not futures volume
  vix           India VIX level and direction; a rising VIX with a rising index
                is a warning, not a confirmation

Everything reads latest_quotes, so it is current to the last few seconds.
"""
from __future__ import annotations

import logging
import os
import sqlite3

_LOG = logging.getLogger("openstocks.internals")

MAIN_DB = os.environ.get("OPENSTOCKS_DB", "/opt/opentrade/var/trading_agent.db")
LIVE_SOURCE = "upstox-live"
# Heavyweights must be actual NIFTY 50 CONSTITUENTS, not "whatever traded most
# today". Ranking the full 2,392-name universe by turnover returned SMLMAH and
# MOIL — real movers, but not in the index at all, so their move explains
# nothing about Nifty. Within the constituent list, turnover is a reasonable
# stand-in for index weight (free-float weights are not in the database and the
# largest caps are reliably the most traded), and it is stated as an
# approximation rather than dressed up as a true weighting.
HEAVYWEIGHT_N = 15
NIFTY50_URL = "https://nsearchives.nseindia.com/content/indices/ind_nifty50list.csv"
_MEMBERS: tuple = (0.0, frozenset())
MEMBERS_TTL = 12 * 3600


def _ro(path=MAIN_DB):
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=20)


def _f(v, d=0.0):
    try:
        out = float(v)
        return out if out == out else d
    except (TypeError, ValueError):
        return d


def snapshot(con=None):
    """Live quote + previous close per symbol, with sector. One query."""
    own = con is None
    con = con or _ro()
    try:
        rows = con.execute(
            "SELECT q.symbol, q.price, q.open, q.high, q.low, q.volume, u.sector"
            " FROM latest_quotes q LEFT JOIN universe u ON UPPER(u.symbol)=UPPER(q.symbol)"
            " WHERE q.source=? AND q.price>0", (LIVE_SOURCE,)).fetchall()
    except Exception as exc:
        _LOG.warning("internals snapshot failed: %s", exc)
        rows = []
    finally:
        if own:
            con.close()
    out = []
    for sym, price, open_, high, low, vol, sector in rows:
        price, open_ = _f(price), _f(open_)
        if price <= 0 or open_ <= 0:
            continue
        out.append(dict(symbol=str(sym).upper(), price=price, open=open_,
                        high=_f(high, price), low=_f(low, price), volume=_f(vol),
                        sector=(sector or "").strip(),
                        chg=(price / open_ - 1) * 100,
                        turnover=price * _f(vol)))
    return out


def nifty50(now=None):
    """NIFTY 50 constituents. Falls back to the last good set on failure, so an
    NSE outage narrows the reading rather than silently replacing the index with
    whatever happened to trade."""
    global _MEMBERS
    import time
    import csv as _csv
    import io as _io
    stamp = now or time.time()
    if _MEMBERS[1] and stamp - _MEMBERS[0] < MEMBERS_TTL:
        return _MEMBERS[1]
    try:
        import httpx
        ua = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
              "Accept": "*/*", "Referer": "https://www.nseindia.com/"}
        text = httpx.get(NIFTY50_URL, headers=ua, timeout=30, follow_redirects=True).text
        names = {(r.get("Symbol") or "").strip().upper()
                 for r in _csv.DictReader(_io.StringIO(text))}
        names.discard("")
        if names:
            _MEMBERS = (stamp, frozenset(names))
    except Exception as exc:
        _LOG.warning("nifty50 list failed: %s", exc)
    return _MEMBERS[1]


def contribution(rows, top=HEAVYWEIGHT_N, members=None):
    """Turnover-weighted move of the heavyweights.

    This is the reading that actually explains an index move. A turnover-weighted
    average of the biggest names tracks the index far more closely than an
    equal-weighted one, because the index itself is cap-weighted and the largest
    caps are the most traded.
    """
    if not rows:
        return None
    members = members if members is not None else nifty50()
    pool = [r for r in rows if r["symbol"] in members] if members else []
    if not pool:
        return None                     # no constituent list -> no claim
    heavies = sorted(pool, key=lambda r: -r["turnover"])[:top]
    total = sum(r["turnover"] for r in heavies)
    if total <= 0:
        return None
    weighted = sum(r["chg"] * r["turnover"] for r in heavies) / total
    leaders = sorted(heavies, key=lambda r: -abs(r["chg"]))[:5]
    return dict(weighted_move=weighted, n=len(heavies),
                leaders=[dict(symbol=r["symbol"], chg=round(r["chg"], 2)) for r in leaders])


def breadth(rows):
    """Advances vs declines across the whole universe.

    A rally carried by three stocks and one carried by four hundred look
    identical on the index chart. Only breadth separates them, and the
    difference decides whether a move continues.
    """
    if not rows:
        return None
    up = sum(1 for r in rows if r["chg"] > 0.1)
    down = sum(1 for r in rows if r["chg"] < -0.1)
    flat = len(rows) - up - down
    if up + down == 0:
        return None
    return dict(advances=up, declines=down, flat=flat,
                ratio=up / max(down, 1), pct_up=up / len(rows) * 100)


def sectors(rows, min_names=3):
    """Average move per sector, best and worst first.

    Uses the real industry labels restored from the NSE index files — before
    that every NSE name carried one label and this reading was impossible.
    """
    buckets = {}
    for r in rows:
        if not r["sector"] or r["sector"] == "NSE Listed Equity":
            continue
        buckets.setdefault(r["sector"], []).append(r["chg"])
    out = [dict(sector=k, chg=sum(v) / len(v), n=len(v))
           for k, v in buckets.items() if len(v) >= min_names]
    out.sort(key=lambda x: -x["chg"])
    return out


def cash_volume(rows, con=None):
    """Today's cash turnover against its own recent average.

    Futures volume was standing in for this, which says nothing about whether
    the cash market is participating in the move.
    """
    if not rows:
        return None
    today = sum(r["turnover"] for r in rows)
    own = con is None
    con = con or _ro()
    try:
        avg = con.execute(
            "SELECT AVG(t) FROM (SELECT date, SUM(close*total_volume) t"
            " FROM delivery_data GROUP BY date ORDER BY date DESC LIMIT 20)").fetchone()[0]
    except Exception:
        avg = None
    finally:
        if own:
            con.close()
    if not avg or avg <= 0:
        return dict(turnover=today, ratio=None)
    return dict(turnover=today, ratio=today / float(avg))


def vix(con=None):
    """India VIX level and change — rising VIX with a rising index is a warning."""
    own = con is None
    con = con or _ro()
    try:
        row = con.execute("SELECT value, pct_change FROM india_vix"
                          " ORDER BY date DESC LIMIT 1").fetchone()
    except Exception:
        row = None
    finally:
        if own:
            con.close()
    if not row:
        return None
    return dict(value=_f(row[0]), change=_f(row[1]))


def fii_positioning(con=None):
    """FII index-futures long/short ratio — institutional positioning."""
    own = con is None
    con = con or _ro()
    try:
        row = con.execute(
            "SELECT fut_idx_long, fut_idx_short FROM participant_oi"
            " WHERE client_type='FII' ORDER BY date DESC LIMIT 1").fetchone()
    except Exception:
        row = None
    finally:
        if own:
            con.close()
    if not row or not _f(row[1]):
        return None
    long_, short = _f(row[0]), _f(row[1])
    return dict(long=long_, short=short, ratio=long_ / short)


def read(con=None):
    """Every internal, in one call."""
    own = con is None
    con = con or _ro()
    try:
        rows = snapshot(con)
        return dict(universe=len(rows), contribution=contribution(rows),
                    breadth=breadth(rows), sectors=sectors(rows)[:3],
                    sectors_worst=sectors(rows)[-3:], cash_volume=cash_volume(rows, con),
                    vix=vix(con), fii=fii_positioning(con))
    finally:
        if own:
            con.close()


def votes(internals):
    """Turn the internals into directional votes for the index call.

    Each is a live reading of what the market is doing NOW, which is the whole
    point — the previous readings were daily bars, so on expiry day they were
    describing yesterday.
    """
    out = {}
    c = internals.get("contribution")
    if c:
        move = c["weighted_move"]
        out["heavyweights"] = ((1 if move > 0.15 else -1 if move < -0.15 else 0),
                               f"heavyweights {move:+.2f}% (turnover-weighted): "
                               + ", ".join(f"{x['symbol']} {x['chg']:+.1f}%"
                                           for x in c["leaders"][:3]))
    b = internals.get("breadth")
    if b:
        out["breadth"] = ((1 if b["ratio"] >= 1.5 else -1 if b["ratio"] <= 0.67 else 0),
                          f"breadth {b['advances']}up/{b['declines']}down "
                          f"({b['pct_up']:.0f}% advancing)")
    v = internals.get("vix")
    if v:
        # A spiking VIX alongside a rising index is a warning, not a confirmation.
        out["vix"] = ((-1 if v["change"] > 5 else 1 if v["change"] < -5 else 0),
                      f"India VIX {v['value']:.2f} ({v['change']:+.1f}%)")
    f = internals.get("fii")
    if f:
        out["fii"] = ((1 if f["ratio"] >= 1.2 else -1 if f["ratio"] <= 0.5 else 0),
                      f"FII index futures {f['ratio']:.2f} long/short")
    cv = internals.get("cash_volume")
    if cv and cv.get("ratio"):
        out["cash_volume"] = (0, f"cash turnover {cv['ratio']:.2f}x the 20-day average")
    return out
