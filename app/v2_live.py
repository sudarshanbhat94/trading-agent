"""v2 live engine - one shared capital pool per market, both strategies.

Budget is a TOTAL per market (US $20,000, India ₹1,00,000), shared across the
swing + gap strategies - NOT per trade, NOT per book. Positions are sized from
that single pool; cash is deducted on buy and returned on exit. Runs as a
background thread inside opentrade.service, only during real market hours.
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone

import pandas as pd

from . import v2_engine as eng
from . import market_regions

MAIN_DB = os.environ.get("OPENSTOCKS_DB", "/opt/opentrade/var/trading_agent.db")
V2_DB = os.environ.get("V2_PAPER_DB", "/opt/opentrade/var/v2_paper.db")
IST = timezone(timedelta(hours=5, minutes=30))
LIVE_SOURCE = {"IN": "upstox-live", "US": "alpaca-iex-live"}
BUDGET = {"IN": 100000.0, "US": 20000.0}     # TOTAL paper capital per market
MAXPOS = {"IN": 10, "US": 10}                # max concurrent positions per market
COST_SIDE = {"IN": 0.30 / 200, "US": 0.12 / 200}
# per-strategy trade plan; both draw from the shared market pool
PLAN = {
    "gap_momentum":  dict(regime_gated=False, threshold=0.20, atr_stop=1.5, atr_target=0.0, trail=0.10, priority=0),
    "swing_meanrev": dict(regime_gated=True,  threshold=0.55, atr_stop=2.0, atr_target=3.5, trail=0.0,  priority=1),
}
SCHEMA = """
CREATE TABLE IF NOT EXISTS v2_book(market TEXT PRIMARY KEY, budget REAL, max_pos INTEGER, started_at TEXT);
CREATE TABLE IF NOT EXISTS v2_positions(id INTEGER PRIMARY KEY AUTOINCREMENT, market TEXT, strategy TEXT, symbol TEXT,
  entry_date TEXT, entry_price REAL, shares REAL, stop REAL, target REAL, trail REAL, peak REAL, conviction REAL, opened_at TEXT);
CREATE TABLE IF NOT EXISTS v2_trades(id INTEGER PRIMARY KEY AUTOINCREMENT, market TEXT, strategy TEXT, symbol TEXT,
  entry_date TEXT, entry_price REAL, exit_date TEXT, exit_price REAL, shares REAL, pnl REAL, return_pct REAL, reason TEXT, conviction REAL);
CREATE TABLE IF NOT EXISTS v2_equity(market TEXT, date TEXT, equity REAL, cash REAL, positions_value REAL, n_positions INTEGER, PRIMARY KEY(market,date));
CREATE TABLE IF NOT EXISTS v2_signals(market TEXT, strategy TEXT, date TEXT, symbol TEXT, conviction REAL, ref_close REAL, rank INTEGER);
"""
_HIST: dict = {}
_started = False
_status: dict = {"IN": "init", "US": "init"}


def ensure_schema(v2):
    v2.executescript(SCHEMA)
    for m, b in BUDGET.items():
        if not v2.execute("SELECT 1 FROM v2_book WHERE market=?", (m,)).fetchone():
            v2.execute("INSERT INTO v2_book(market,budget,max_pos,started_at) VALUES(?,?,?,?)",
                       (m, b, MAXPOS[m], datetime.now(timezone.utc).isoformat()))
    v2.commit()


def market_open(market):
    try:
        return bool(market_regions.market_session_for_region(market).get("is_open"))
    except Exception:
        return False


def _ro(p):
    return sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=30)


def _rw():
    c = sqlite3.connect(V2_DB, timeout=30)
    c.execute("PRAGMA busy_timeout=8000")
    return c


def _hist(market):
    h = _HIST.get(market)
    if h and time.time() - h[0] < 6 * 3600:
        return h[1], h[2]
    con = _ro(MAIN_DB)
    syms, mdf = eng.load_panel(con, market, topn=eng.DEFAULTS["topn"])
    con.close()
    tails = {s: g.tail(90).copy() for s, g in syms.items() if len(g) >= 70}
    _HIST[market] = (time.time(), tails, mdf)
    return tails, mdf


def _f(v, d):
    try:
        x = float(v)
        return x if x > 0 else d
    except (TypeError, ValueError):
        return d


# severe-negative catalysts a pro would NOT buy into
NEG_EVENTS = {"fraud_governance", "legal_regulatory", "debt_liquidity", "analyst_downgrade"}


def _news_state(mcon, symbol):
    """Return (net_score, severe_negative) from the last 3 days of news."""
    try:
        rows = mcon.execute("SELECT score,events_json FROM sentiment_events WHERE symbol=? "
                            "AND ts>=datetime('now','-3 days') ORDER BY ts DESC LIMIT 3", (symbol,)).fetchall()
    except Exception:
        return 0.0, False
    if not rows:
        return 0.0, False
    import json
    score = 0.0
    for sc, _ in rows:
        try:
            score = float(sc); break
        except (TypeError, ValueError):
            continue
    severe = False
    for _, ej in rows:
        try:
            for e in json.loads(ej or "[]"):
                if e.get("event_type") in NEG_EVENTS and float(e.get("score") or 0) < -0.15:
                    severe = True
        except Exception:
            continue
    return score, (severe or score <= -0.35)


def _live(market):
    con = _ro(MAIN_DB)
    rows = con.execute("SELECT symbol,price,open,high,low,close,volume FROM latest_quotes WHERE source=?",
                       (LIVE_SOURCE[market],)).fetchall()
    con.close()
    out = {}
    for sym, p, o, h, l, c, v in rows:
        try:
            price = float(p)
        except (TypeError, ValueError):
            continue
        if price > 0:
            out[sym] = dict(price=price, open=_f(o, price), high=_f(h, price), low=_f(l, price), vol=_f(v, 0))
    return out


def _signals(tails, mdf, live):
    today = pd.Timestamp(datetime.now(IST).date())
    rets = [live[s]["price"] / t["close"].iloc[-1] - 1 for s, t in tails.items()
            if s in live and t["close"].iloc[-1] > 0]
    mret = float(pd.Series(rets).median()) if rets else 0.0
    mdf_live = mdf.copy()
    mdf_live.loc[today] = {"mkt_ret1": mret, "mkt_cum": mdf["mkt_cum"].iloc[-1] * (1 + mret)}
    out = []
    for s, t in tails.items():
        lq = live.get(s)
        if not lq:
            continue
        tl = t.copy()
        tl.loc[today] = {"open": lq["open"], "high": lq["high"], "low": lq["low"],
                         "close": lq["price"], "volume": lq["vol"] or t["volume"].iloc[-1]}
        try:
            row = eng.compute_features(tl, mdf_live).iloc[-1]
        except Exception:
            continue
        atr = float(row["atr14"]) if not pd.isna(row["atr14"]) else 0.0
        if atr <= 0:
            continue
        c = eng.conviction(row)
        if c > 0:
            out.append(dict(symbol=s, strategy="swing_meanrev", score=round(c, 4), atr=atr, price=lq["price"]))
        prevc = t["close"].iloc[-1]
        g = lq["open"] / prevc - 1 if prevc > 0 else 0
        rv = float(row["rvol"]) if not pd.isna(row["rvol"]) else 0
        if 0.03 <= g <= 0.15 and rv >= 1.5:
            out.append(dict(symbol=s, strategy="gap_momentum", score=round(min(g / 0.15, 1.0), 4), atr=atr, price=lq["price"]))
    return out, mdf_live, today


def poll_market(market):
    live = _live(market)
    if not live:
        _status[market] = "no quotes"
        return
    tails, mdf = _hist(market)
    sigs, mdf_live, today = _signals(tails, mdf, live)
    regime = eng.regime_ok(mdf_live, today, eng.DEFAULTS["regime_lookback"])
    v2 = _rw()
    book = v2.execute("SELECT budget,max_pos FROM v2_book WHERE market=?", (market,)).fetchone()
    if not book:
        v2.close(); return
    budget, max_pos = book[0], int(book[1])
    cside = COST_SIDE[market]
    today_s = today.date().isoformat()
    alloc = budget / max_pos
    # refresh live signal lists (for the watchlist UI), per strategy
    for strat in PLAN:
        v2.execute("DELETE FROM v2_signals WHERE market=? AND strategy=?", (market, strat))
    ranked = {}
    for s in sigs:
        ranked.setdefault(s["strategy"], []).append(s)
    for strat, lst in ranked.items():
        lst.sort(key=lambda x: -x["score"])
        for rank, s in enumerate(lst[:max_pos], 1):
            v2.execute("INSERT INTO v2_signals VALUES(?,?,?,?,?,?,?)",
                       (market, strat, today_s, s["symbol"], s["score"], round(s["price"], 2), rank))
    # current shared book
    positions = {r[0]: dict(id=r[1], strategy=r[2], entry=r[3], shares=r[4], stop=r[5], target=r[6], trail=r[7], peak=r[8])
                 for r in v2.execute("SELECT symbol,id,strategy,entry_price,shares,stop,target,trail,peak "
                                     "FROM v2_positions WHERE market=?", (market,))}
    traded = {r[0] for r in v2.execute("SELECT symbol FROM v2_trades WHERE market=? AND entry_date=?", (market, today_s))}
    realised = v2.execute("SELECT COALESCE(SUM(pnl),0) FROM v2_trades WHERE market=?", (market,)).fetchone()[0] or 0.0
    cash = budget - sum(p["shares"] * p["entry"] for p in positions.values()) + realised
    # candidate ordering: catalysts (gap) first, then swing; each must clear its own gate
    cand = []
    for s in sigs:
        pl = PLAN[s["strategy"]]
        if s["score"] < pl["threshold"]:
            continue
        if pl["regime_gated"] and not regime:
            continue
        cand.append((pl["priority"], -s["score"], s, pl))
    cand.sort(key=lambda x: (x[0], x[1]))
    mcon = _ro(MAIN_DB)
    fills = exits = vetoed = 0
    for _, _, s, pl in cand:
        if len(positions) >= max_pos or alloc > cash:
            break
        sym = s["symbol"]
        if sym in positions or sym in traded:
            continue
        nscore, severe = _news_state(mcon, sym)
        if severe:                       # pro check: never buy into bad news
            vetoed += 1
            continue
        entry, atr = s["price"], s["atr"]
        shares = alloc / entry
        cash -= shares * entry * (1 + cside)
        tgt = entry + pl["atr_target"] * atr if pl["atr_target"] else 0.0
        v2.execute("INSERT INTO v2_positions(market,strategy,symbol,entry_date,entry_price,shares,stop,target,trail,peak,conviction,opened_at)"
                   " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                   (market, s["strategy"], sym, today_s, entry, shares, entry - pl["atr_stop"] * atr, tgt, pl["trail"], entry,
                    s["score"], datetime.now(timezone.utc).isoformat()))
        positions[sym] = dict(id=None, strategy=s["strategy"], entry=entry, shares=shares,
                              stop=entry - pl["atr_stop"] * atr, target=tgt, trail=pl["trail"], peak=entry)
        traded.add(sym); fills += 1
    mcon.close()
    # exits on live price/high/low
    for sym, p in list(positions.items()):
        if p["id"] is None:
            continue
        lq = live.get(sym)
        if not lq:
            continue
        peak = max(p["peak"], lq["high"], lq["price"])
        eff = p["stop"]
        if p["trail"]:
            eff = max(eff, peak * (1 - p["trail"]))
        ex = reason = None
        if lq["low"] <= eff or lq["price"] <= eff:
            ex, reason = min(eff, lq["price"]), ("trail" if p["trail"] and eff > p["stop"] else "stop")
        elif p["target"] and (lq["high"] >= p["target"] or lq["price"] >= p["target"]):
            ex, reason = max(p["target"], lq["price"]), "target"
        if ex is not None:
            cash += p["shares"] * ex * (1 - cside)
            v2.execute("INSERT INTO v2_trades(market,strategy,symbol,entry_date,entry_price,exit_date,exit_price,shares,pnl,return_pct,reason,conviction)"
                       " SELECT market,strategy,symbol,entry_date,entry_price,?,?,?,?,?,?,conviction FROM v2_positions WHERE id=?",
                       (today_s, ex, p["shares"], p["shares"] * (ex - p["entry"]), (ex / p["entry"] - 1) * 100, reason, p["id"]))
            v2.execute("DELETE FROM v2_positions WHERE id=?", (p["id"],))
            del positions[sym]; exits += 1
        else:
            v2.execute("UPDATE v2_positions SET peak=? WHERE id=?", (peak, p["id"]))
    pv = sum(p["shares"] * (live[s]["price"] if s in live else p["entry"]) for s, p in positions.items())
    v2.execute("INSERT OR REPLACE INTO v2_equity(market,date,equity,cash,positions_value,n_positions) VALUES(?,?,?,?,?,?)",
               (market, "LIVE_" + datetime.now(timezone.utc).isoformat()[:19], cash + pv, cash, pv, len(positions)))
    v2.commit(); v2.close()
    _status[market] = (f"open · {datetime.now(IST).strftime('%H:%M IST')} · +{fills}/-{exits}"
                       f" · {len(positions)} pos · {vetoed} news-vetoed")


def loop(interval):
    try:
        v2 = _rw(); ensure_schema(v2); v2.close()
    except Exception:
        pass
    while True:
        for m in ("IN", "US"):
            try:
                if market_open(m):
                    poll_market(m)
                else:
                    _status[m] = "closed"
            except Exception as exc:
                _status[m] = f"err {str(exc)[:40]}"
        time.sleep(interval)


def start_background(interval=45):
    global _started
    if _started:
        return
    _started = True
    threading.Thread(target=loop, args=(interval,), daemon=True, name="v2-live-engine").start()


def status():
    return dict(_status)
