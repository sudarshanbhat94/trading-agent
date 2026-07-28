"""v2 web UI - clean, mobile-first dashboard for the v2 paper engines.

Self-contained FastAPI router mounted at /v2/. Reads the paper book
(var/v2_paper.db) and live quotes (latest_quotes) READ-ONLY and serves a single
responsive page + JSON APIs. Zero coupling to the legacy dashboard.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone

import asyncio
import json as _jsonmod

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from . import indicators as ta
from . import analysts as ana
from . import narrative as narr
from . import portfolio as pf
from . import recommendation as rec
from . import v2_engine as eng

_LOG = logging.getLogger("openstocks.v2_web")

MAIN_DB = os.environ.get("OPENSTOCKS_DB", "/opt/opentrade/var/trading_agent.db")
V2_DB = os.environ.get("V2_PAPER_DB", "/opt/opentrade/var/v2_paper.db")
CATALYST_DB = os.environ.get("CATALYST_DB", "/opt/opentrade/var/catalysts.db")
LIVE_SOURCE = {"IN": "upstox-live", "US": "alpaca-iex-live"}

router = APIRouter(prefix="/v2")
_panel_cache: dict = {}


def _ro(path):
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)


def _live_map(market, symbols=None):
    """Live price snapshot for a market. Pass `symbols` (an iterable) to read
    only those rows — the per-second stream uses this so it never scans the whole
    quote table just to value a handful of held positions."""
    try:
        con = _ro(MAIN_DB)
        if symbols:
            syms = [s for s in symbols]
            if not syms:
                con.close()
                return {}
            q = ("SELECT symbol,price,open,high,low,close FROM latest_quotes WHERE source=? AND symbol IN (%s)"
                 % ",".join("?" * len(syms)))
            rows = con.execute(q, [LIVE_SOURCE[market], *syms]).fetchall()
        else:
            rows = con.execute("SELECT symbol,price,open,high,low,close FROM latest_quotes WHERE source=?",
                               (LIVE_SOURCE[market],)).fetchall()
        con.close()
    except Exception:
        return {}
    out = {}
    for sym, p, o, h, l, c in rows:
        try:
            price = float(p)
        except (TypeError, ValueError):
            continue
        if price > 0:
            out[sym] = dict(price=price, open=_n(o, price), high=_n(h, price),
                            low=_n(l, price), prev=_n(c, price))
    return out


def _n(v, d):
    try:
        x = float(v)
        return x if x > 0 else d
    except (TypeError, ValueError):
        return d


def _panel(market):
    c = _panel_cache.get(market)
    if c and time.time() - c[0] < 900:
        return c[1], c[2]
    con = _ro(MAIN_DB)
    syms, mdf = eng.load_panel(con, market, topn=eng.DEFAULTS["topn"])
    con.close()
    _panel_cache[market] = (time.time(), syms, mdf)
    return syms, mdf


_regime_cache: dict = {}
_regime_loading: set = set()


def _regime_bg(market):
    try:
        syms, mdf = _panel(market)
        d = eng.complete_trading_dates(syms, 0.5)
        if d:
            state = eng.regime_state(mdf, d[-1], eng.DEFAULTS["regime_lookback"])
            if state == "ON" and eng.regime_strong(mdf, d[-1], eng.DEFAULTS["regime_lookback"]):
                state = "STRONG"
        else:
            state = "OFF"
        _regime_cache[market] = (time.time(), state)
    except Exception:
        _regime_cache[market] = (time.time(), "OFF")
    finally:
        _regime_loading.discard(market)


def _regime_state(market):
    """Non-blocking graded regime ("STRONG"/"ON"/"NEUTRAL"/"OFF" or None while
    warming): cached, refreshed in a background thread so the dashboard never
    waits on the heavy panel load."""
    c = _regime_cache.get(market)
    if c and time.time() - c[0] < 1800:
        return c[1]
    if market not in _regime_loading:
        _regime_loading.add(market)
        import threading
        threading.Thread(target=_regime_bg, args=(market,), daemon=True).start()
    return c[1] if c else None


def _regime(market):
    """Back-compat bool view of the graded regime (True = dip-buys allowed)."""
    s = _regime_state(market)
    return None if s is None else s != "OFF"


def _markets(v2):
    try:
        return v2.execute("SELECT market,budget FROM v2_book ORDER BY market").fetchall()
    except Exception:
        return []


def _prev_close_map(market, symbols):
    """yesterday's close per held symbol from the warm panel; {} while cold."""
    if not symbols:
        return {}
    c = _panel_cache.get(market)
    if not c or time.time() - c[0] > 900:
        return {}
    syms = c[1]
    today = datetime.now(IST).date()
    out = {}
    for s in symbols:
        g = syms.get(s)
        if g is None or len(g) < 2 or (today - g.index[-1].date()).days > 5:
            continue
        out[s] = float(g["close"].iloc[-2] if g.index[-1].date() >= today else g["close"].iloc[-1])
    return out


def _market_stats(v2, market, budget, live):
    today_s = datetime.now(IST).date().isoformat()
    pos = v2.execute("SELECT symbol,entry_price,shares,entry_date FROM v2_positions WHERE market=?", (market,)).fetchall()
    realised = v2.execute("SELECT COALESCE(SUM(pnl),0) FROM v2_trades WHERE market=?", (market,)).fetchone()[0] or 0.0
    realised_today = v2.execute("SELECT COALESCE(SUM(pnl),0) FROM v2_trades WHERE market=? AND exit_date=?",
                                (market, today_s)).fetchone()[0] or 0.0
    mtm = unreal = unreal_today = 0.0
    prevs = _prev_close_map(market, [r[0] for r in pos])
    for sym, entry, shares, edate in pos:
        p = live.get(sym, {}).get("price", entry)
        mtm += shares * p
        unreal += (p - entry) * shares
        # today's move only: vs yesterday's close (entry when opened today or
        # no reference) — NOT the position's lifetime P&L
        ref = prevs.get(sym) or entry
        base = entry if str(edate) == today_s else (ref if ref > 0 else entry)
        unreal_today += (p - base) * shares
    cash = budget - sum(r[1] * r[2] for r in pos) + realised
    rets = [r[0] for r in v2.execute("SELECT return_pct FROM v2_trades WHERE market=?", (market,))]
    wins = [r for r in rets if r > 0]
    loss = [r for r in rets if r <= 0]
    loss_sum = abs(sum(loss))
    pf = (sum(wins) / loss_sum) if loss_sum > 0 else (9.9 if wins else 0.0)
    return dict(market=market, budget=budget, equity=cash + mtm, cash=cash, deployed=mtm,
                deploy_pct=round(mtm / budget * 100) if budget else 0,
                today_pnl=realised_today + unreal_today, overall_pnl=realised + unreal,
                positions=len(pos), trades=len(rets),
                win=(len(wins) / len(rets) * 100) if rets else 0.0, pf=round(pf or 0, 2))


LANE_LABELS = {
    "swing_meanrev": "swing mean-reversion",
    "mom_breakout": "momentum breakout",
    "gap_momentum": "gap momentum",
    "volume_surge": "volume surge",
    "intraday_news": "intraday news",
    "btst": "BTST (overnight)",
    "manual": "manual",
}
OVERNIGHT_LANES = ("swing_meanrev", "mom_breakout", "gap_momentum", "btst", "manual")


def _hold_days(entry_date, exit_date):
    """Calendar days held. Returns None when either date is unusable."""
    try:
        start = datetime.strptime(str(entry_date)[:10], "%Y-%m-%d").date()
        end = datetime.strptime(str(exit_date)[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None
    return (end - start).days


def strategy_stats(rows):
    """Per-lane performance from closed trades.

    `rows` are (strategy, return_pct, pnl, entry_date, exit_date) tuples. Pure,
    so it can be tested without a database.

    Every lane is reported under its own name. The UI previously collapsed
    everything to "gap" or "swing", which made btst, volume_surge,
    intraday_news and mom_breakout indistinguishable — and unevaluatable.
    """
    buckets: dict[str, dict] = {}
    for strategy, ret, pnl, entry_date, exit_date in rows:
        lane = str(strategy or "unknown")
        b = buckets.setdefault(lane, {"rets": [], "pnl": 0.0, "holds": []})
        try:
            b["rets"].append(float(ret))
        except (TypeError, ValueError):
            continue
        try:
            b["pnl"] += float(pnl or 0.0)
        except (TypeError, ValueError):
            pass
        held = _hold_days(entry_date, exit_date)
        if held is not None:
            b["holds"].append(held)

    out = []
    for lane, b in buckets.items():
        rets = b["rets"]
        if not rets:
            continue
        wins = [r for r in rets if r > 0]
        loss = [r for r in rets if r <= 0]
        loss_sum = abs(sum(loss))
        # Zero-denominator guard. A single break-even trade makes `loss`
        # non-empty while summing to zero; that exact case once 500'd the
        # overview endpoint and blanked the dashboard.
        pf = (sum(wins) / loss_sum) if loss_sum > 0 else (9.9 if wins else 0.0)
        out.append(dict(
            strategy=lane,
            label=LANE_LABELS.get(lane, lane.replace("_", " ")),
            overnight=lane in OVERNIGHT_LANES,
            trades=len(rets),
            win=round(len(wins) / len(rets) * 100, 1),
            pf=round(pf, 2),
            pnl=round(b["pnl"], 2),
            avg=round(sum(rets) / len(rets), 2),
            avg_win=round(sum(wins) / len(wins), 2) if wins else 0.0,
            avg_loss=round(sum(loss) / len(loss), 2) if loss else 0.0,
            best=round(max(rets), 2),
            worst=round(min(rets), 2),
            avg_hold_days=round(sum(b["holds"]) / len(b["holds"]), 1) if b["holds"] else None,
        ))
    # Most-traded first; it is the lane with the most evidence behind it.
    out.sort(key=lambda x: (-x["trades"], x["strategy"]))
    return out


@router.get("/api/overview")
def api_overview():
    v2 = _ro(V2_DB)
    live = {"IN": _live_map("IN"), "US": _live_map("US")}
    markets = []
    for market, budget in _markets(v2):
        s = _market_stats(v2, market, budget, live[market])
        eq = [round(r[0]) for r in v2.execute(
            "SELECT equity FROM v2_equity WHERE market=? ORDER BY date DESC LIMIT 40", (market,))][::-1]
        if not eq:
            eq = [round(budget)]
        # hero chart data — series with a MEANING, not "last 40 rows in whatever
        # order": today's minute equity vs yesterday's close, plus daily closes.
        utc_d = datetime.now(timezone.utc).date().isoformat()
        today_eq = [round(r[0]) for r in v2.execute(
            "SELECT equity FROM v2_equity WHERE market=? AND date LIKE ? ORDER BY date",
            (market, "LIVE_" + utc_d + "%"))]
        prev_row = v2.execute("SELECT equity FROM v2_equity WHERE market=? AND date < ? ORDER BY date DESC LIMIT 1",
                              (market, "LIVE_" + utc_d)).fetchone()
        prev_eq = round(prev_row[0]) if prev_row else round(budget)
        dd = {r[0]: round(r[1]) for r in v2.execute(
            "SELECT date, equity FROM v2_equity WHERE market=? AND date NOT LIKE 'LIVE_%'", (market,))}
        for r in v2.execute("SELECT substr(date,6,10) d, equity, MAX(date) FROM v2_equity "
                            "WHERE market=? AND date LIKE 'LIVE_%' GROUP BY substr(date,6,10)", (market,)):
            dd[r[0]] = round(r[1])   # per-day close from the last LIVE snapshot wins
        days = sorted(dd)[-90:]
        dser = [dd[k] for k in days]
        sharpe, maxdd = _risk_metrics(dser)     # institutional risk metrics (borrowed idea)
        markets.append({"market": s["market"], "ccy": "₹" if market == "IN" else "$",
                        "today_series": today_eq, "prev_equity": prev_eq,
                        "daily_series": dser, "sharpe": sharpe, "maxdd": maxdd,
                        "daily_start": (days[0] if days else None),
                        "budget": round(s["budget"]), "equity": round(s["equity"]), "equity_series": eq,
                        "cash": round(s["cash"]), "deployed": round(s["deployed"]), "deploy_pct": s["deploy_pct"],
                        "today_pnl": round(s["today_pnl"], 2), "overall_pnl": round(s["overall_pnl"], 2),
                        "today_pct": round(s["today_pnl"] / s["budget"] * 100, 2) if s["budget"] else 0,
                        "overall_pct": round(s["overall_pnl"] / s["budget"] * 100, 2) if s["budget"] else 0,
                        "positions": s["positions"], "trades": s["trades"], "win": round(s["win"]), "pf": s["pf"]})
    v2.close()
    return JSONResponse(dict(markets=markets, regime={"IN": _regime("IN"), "US": _regime("US")},
                             regime_state={"IN": _regime_state("IN"), "US": _regime_state("US")},
                             as_of=datetime.now(IST).strftime("%H:%M:%S IST")))


def _risk_metrics(eq):
    """Annualized Sharpe + max drawdown % from a daily equity series (borrowed
    from institutional terminals). None until there's enough history."""
    if not eq or len(eq) < 5:
        return None, None
    rets = [eq[i] / eq[i - 1] - 1 for i in range(1, len(eq)) if eq[i - 1] > 0]
    if len(rets) < 4:
        return None, None
    import statistics
    mu = statistics.mean(rets)
    sd = statistics.pstdev(rets)
    sharpe = round((mu / sd) * (252 ** 0.5), 2) if sd > 0 else None
    peak, mdd = eq[0], 0.0
    for v in eq:
        peak = max(peak, v)
        if peak > 0:
            mdd = min(mdd, (v - peak) / peak)
    return sharpe, round(mdd * 100, 1)


IST = timezone(timedelta(hours=5, minutes=30))


def _rw():
    c = sqlite3.connect(V2_DB, timeout=30)
    c.execute("PRAGMA busy_timeout=8000")
    # WAL so the 1s stream / page reads never queue behind engine writes.
    # (Safe here: 6MB DB with tiny writes — unlike the main quote DB where an
    # unbounded WAL once caused an outage; nightly backup prunes/copies this.)
    c.execute("PRAGMA journal_mode=WAL")
    return c


def _ist(ts):
    if not ts:
        return ""
    try:
        t = datetime.fromisoformat(str(ts).replace("Z", "+00:00").replace("LIVE_", ""))
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return t.astimezone(IST).strftime("%d %b %H:%M IST")
    except ValueError:
        return str(ts)


@router.get("/api/positions")
def api_positions():
    v2 = _ro(V2_DB)
    live = {"IN": _live_map("IN"), "US": _live_map("US")}
    out = []
    today_s = datetime.now(IST).date().isoformat()
    try:
        rows = v2.execute("SELECT id,market,strategy,symbol,entry_price,shares,stop,target,trail,peak,"
                          "entry_date,opened_at,why FROM v2_positions").fetchall()
    except Exception:   # why column not migrated yet
        rows = [(*r, None) for r in v2.execute(
            "SELECT id,market,strategy,symbol,entry_price,shares,stop,target,trail,peak,entry_date,opened_at "
            "FROM v2_positions")]
    for pid, market, strat, sym, entry, shares, stop, target, trail, peak, edate, oat, why in rows:
        p = live.get(market, {}).get(sym, {}).get("price", entry)
        tstop = max(stop, peak * (1 - trail)) if trail else stop
        head = (p - tstop) / (peak - tstop) if peak > tstop else 0
        try:
            why_d = _jsonmod.loads(why) if why else None
        except Exception:
            why_d = None
        out.append(dict(id=pid, market=market, ccy="₹" if market == "IN" else "$", strategy=strat, symbol=sym,
                        entry=round(entry, 2), live=round(p, 2), qty=round(shares, 2), value=round(p * shares, 2),
                        pnl=round((p / entry - 1) * 100, 2), pnl_amt=round((p - entry) * shares, 2),
                        stop=round(tstop, 2), trail=bool(trail), today=str(edate) == today_s,
                        since=_ist(oat or edate), headroom=round(max(0, min(1, head)) * 100), why=why_d))
    v2.close()
    out.sort(key=lambda x: -x["pnl"])
    return JSONResponse(out)


@router.get("/api/health")
def api_health():
    """Pipeline self-check: quote freshness, daily-candle freshness, engine
    heartbeat. The UI shows a red banner when anything is unhealthy — so a silent
    stall (like the 2-week candle starvation) can never go unnoticed again."""
    checks = []
    now = datetime.now(timezone.utc)
    try:
        from . import v2_live
        mkts = list(v2_live.ENABLED_MARKETS)
    except Exception:
        mkts = ["IN"]
    try:
        from . import market_regions
        openm = {m: bool(market_regions.market_session_for_region(m).get("is_open")) for m in mkts}
    except Exception:
        openm = {m: False for m in mkts}
    con = _ro(MAIN_DB)
    for m in mkts:
        src = LIVE_SOURCE[m]
        ts = con.execute("SELECT MAX(ts) FROM latest_quotes WHERE source=?", (src,)).fetchone()[0]
        age = 1e9
        if ts:
            try:
                age = (now - datetime.fromisoformat(str(ts).replace("Z", "+00:00"))).total_seconds()
            except ValueError:
                pass
        ok = age < 180 if openm.get(m) else True
        checks.append(dict(name=f"{m} live feed", ok=ok,
                           detail=(f"{age:.0f}s old" if openm.get(m) else "market closed")))
    _daily = {"IN": "upstox-live:day", "US": "alpaca-iex-live:day"}
    for m in mkts:
        src = _daily[m]
        ts = con.execute("SELECT MAX(ts) FROM candles WHERE source=?", (src,)).fetchone()[0]
        days = 99
        if ts:
            try:
                days = (now - datetime.fromisoformat(str(ts).replace("Z", "+00:00"))).days
            except ValueError:
                pass
        checks.append(dict(name=f"{m} daily candles", ok=days <= 5, detail=f"{days}d old"))
    con.close()
    try:
        v2 = _ro(V2_DB)
        last = v2.execute("SELECT MAX(date) FROM v2_equity WHERE date LIKE 'LIVE_%'").fetchone()[0]
        v2.close()
        hb = 1e9
        if last:
            hb = (now - datetime.fromisoformat(str(last)[5:]).replace(tzinfo=timezone.utc)).total_seconds()
        any_open = any(openm.values())
        checks.append(dict(name="engine heartbeat", ok=(hb < 600 if any_open else True),
                           detail=(f"{hb/60:.0f}min ago" if hb < 1e9 else "no data") if any_open else "markets closed"))
    except Exception:
        checks.append(dict(name="engine heartbeat", ok=False, detail="unreadable"))
    return JSONResponse(dict(ok=all(c["ok"] for c in checks), checks=checks))


# ---------------- watchlist · alerts · search · movers ----------------
def _uwl(v2):
    v2.executescript(
        "CREATE TABLE IF NOT EXISTS v2_watch_user(symbol TEXT, market TEXT, added_at TEXT, PRIMARY KEY(symbol,market));"
        "CREATE TABLE IF NOT EXISTS v2_alerts(id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, market TEXT,"
        " kind TEXT, value REAL, created_at TEXT, triggered_at TEXT, triggered_price REAL, active INTEGER DEFAULT 1);")


def _panel_warm(market):
    """True if the candle panel cache is warm. When cold, kicks the shared
    background loader and returns False — callers must degrade gracefully
    instead of blocking a request for 30-90s (this froze the watchlist and
    movers after every restart)."""
    c = _panel_cache.get(market)
    if c and time.time() - c[0] < 900:
        return True
    if market not in _regime_loading:
        _regime_loading.add(market)
        import threading
        threading.Thread(target=_regime_bg, args=(market,), daemon=True).start()
    return False


def _daychg(market, symbols):
    """live price + day-change vs the last daily close, from the cached engine
    panel (no extra DB scan). NON-BLOCKING: while the panel is cold, returns
    live prices with chg=None and lets the background loader warm it."""
    if not symbols:
        return {}
    live = _live_map(market, symbols)
    if not _panel_warm(market):
        return {s: dict(price=lq["price"], chg=None) for s, lq in live.items()}
    try:
        syms, _ = _panel(market)
    except Exception:
        return {}
    today = datetime.now(IST).date()
    out = {}
    for s in symbols:
        g, lq = syms.get(s), live.get(s)
        if g is None or lq is None or len(g) < 2:
            continue
        if (today - g.index[-1].date()).days > 5:      # stale per-symbol candles
            out[s] = dict(price=lq["price"], chg=None)  # -> ellipsis, not a bogus %
            continue
        prev = float(g["close"].iloc[-2] if g.index[-1].date() >= today else g["close"].iloc[-1])
        out[s] = dict(price=lq["price"], chg=(lq["price"] / prev - 1) * 100 if prev > 0 else 0.0)
    return out


@router.get("/api/watchlist")
def api_watchlist():
    v2 = _rw()
    _uwl(v2)
    rows = v2.execute("SELECT symbol,market FROM v2_watch_user ORDER BY added_at DESC").fetchall()
    alerts = v2.execute("SELECT id,symbol,market,kind,value,active,triggered_at,triggered_price "
                        "FROM v2_alerts ORDER BY id DESC LIMIT 60").fetchall()
    v2.close()
    chg = {m: _daychg(m, [r[0] for r in rows if r[1] == m]) for m in ("IN", "US")}
    watch = []
    for sym, m in rows:
        d = chg.get(m, {}).get(sym) or {}
        c = d.get("chg")
        watch.append(dict(symbol=sym, market=m, ccy="₹" if m == "IN" else "$",
                          price=round(d.get("price", 0), 2), chg=(round(c, 2) if c is not None else None)))
    al = [dict(id=a[0], symbol=a[1], market=a[2], ccy="₹" if a[2] == "IN" else "$", kind=a[3],
               value=a[4], active=bool(a[5]), triggered_at=_ist(a[6]) if a[6] else None,
               triggered_price=a[7]) for a in alerts]
    return JSONResponse(dict(watch=watch, alerts=al))


@router.post("/api/watchlist")
def api_watchlist_add(payload: dict):
    sym = str(payload.get("symbol", "")).upper().strip()
    market = payload.get("market", "IN")
    if not sym:
        return JSONResponse({"error": "symbol required"}, status_code=400)
    v2 = _rw()
    _uwl(v2)
    v2.execute("INSERT OR IGNORE INTO v2_watch_user VALUES(?,?,?)",
               (sym, market, datetime.now(timezone.utc).isoformat()))
    v2.commit(); v2.close()
    return JSONResponse({"ok": True})


@router.delete("/api/watchlist/{symbol}")
def api_watchlist_del(symbol: str, market: str = "IN"):
    v2 = _rw()
    _uwl(v2)
    v2.execute("DELETE FROM v2_watch_user WHERE symbol=? AND market=?", (symbol.upper(), market))
    v2.commit(); v2.close()
    return JSONResponse({"ok": True})


@router.post("/api/alerts")
def api_alerts_add(payload: dict):
    sym = str(payload.get("symbol", "")).upper().strip()
    kind = payload.get("kind")
    try:
        value = float(payload.get("value"))
    except (TypeError, ValueError):
        # A catalyst alert has no threshold — it fires on the next material
        # filing — so only the price kinds require a number.
        if payload.get("kind") in ("catalyst", "pattern"):
            value = 0.0
        else:
            return JSONResponse({"error": "bad value"}, status_code=400)
    if not sym or kind not in ("above", "below", "pct", "catalyst",
                              "cross_up", "cross_down", "pattern"):
        return JSONResponse({"error": "bad alert"}, status_code=400)
    if kind in ("cross_up", "cross_down") and int(value) not in ALERT_SMA_PERIODS:
        return JSONResponse({"error": "bad alert"}, status_code=400)
    v2 = _rw()
    _uwl(v2)
    v2.execute("INSERT INTO v2_alerts(symbol,market,kind,value,created_at,active) VALUES(?,?,?,?,?,1)",
               (sym, payload.get("market", "IN"), kind, value, datetime.now(timezone.utc).isoformat()))
    v2.commit(); v2.close()
    return JSONResponse({"ok": True})


@router.post("/api/buy")
def api_buy(payload: dict):
    """Manual paper buy into the v2 book — user-initiated, bypasses the engine's
    entry gates so the user can act on their own read."""
    sym = str(payload.get("symbol", "")).upper().strip()
    market = payload.get("market", "IN")
    if not sym:
        return JSONResponse({"error": "no symbol"}, status_code=400)
    px = float((_live_map(market).get(sym) or {}).get("price") or 0)
    if px <= 0:
        return JSONResponse({"error": "no live price for " + sym}, status_code=400)
    v2 = _rw()
    try:
        book = v2.execute("SELECT budget,max_pos FROM v2_book WHERE market=?", (market,)).fetchone()
        if not book:
            return JSONResponse({"error": "no book"}, status_code=400)
        budget, max_pos = book[0], int(book[1])
        if v2.execute("SELECT 1 FROM v2_positions WHERE market=? AND symbol=?", (market, sym)).fetchone():
            return JSONResponse({"error": "already holding " + sym}, status_code=400)
        n = v2.execute("SELECT COUNT(*) FROM v2_positions WHERE market=?", (market,)).fetchone()[0]
        if n >= max_pos:
            return JSONResponse({"error": "book full (%d/%d slots) — sell something first" % (n, max_pos)}, status_code=400)
        realized = v2.execute("SELECT COALESCE(SUM(pnl),0) FROM v2_trades WHERE market=?", (market,)).fetchone()[0] or 0
        invested = v2.execute("SELECT COALESCE(SUM(shares*entry_price),0) FROM v2_positions WHERE market=?", (market,)).fetchone()[0] or 0
        cash = budget - invested + realized
        qty = int(min(budget / max_pos, cash * 0.98) / px)
        if qty < 1:
            return JSONResponse({"error": "not enough cash for 1 share (₹%.0f free, ₹%.0f/share)" % (cash, px)}, status_code=400)
        stop, target = round(px * 0.94, 2), round(px * 1.06, 2)
        v2.execute("INSERT INTO v2_positions(market,strategy,symbol,entry_date,entry_price,shares,stop,target,trail,peak,conviction,opened_at,why)"
                   " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (market, "manual", sym, datetime.now(IST).date().isoformat(), px, qty, stop, target, 0.0, px, 1.0,
                    datetime.now(timezone.utc).isoformat(), '{"setup":"manual buy"}'))
        v2.commit()
    finally:
        v2.close()
    try:
        from . import telegram_bot
        telegram_bot.notify_trade("BUY", sym, qty, round(px, 2), market, strategy="manual", stop=stop, target=target)
    except Exception:
        pass
    return JSONResponse({"ok": True, "symbol": sym, "qty": qty, "entry": round(px, 2)})


@router.post("/api/reset")
def api_reset():
    """Self-serve paper-book reset — clean ₹1,00,000, clears positions/trades/
    equity/signals. Keeps telegram_accounts. Paper only."""
    v2 = _rw()
    try:
        for t in ("v2_positions", "v2_trades", "v2_equity", "v2_signals"):
            try:
                v2.execute("DELETE FROM %s" % t)
            except Exception:
                pass
        now_utc = datetime.now(timezone.utc).isoformat()
        for market, budget, mp in (("IN", 100000.0, 6), ("US", 20000.0, 6)):
            if v2.execute("SELECT 1 FROM v2_book WHERE market=?", (market,)).fetchone():
                v2.execute("UPDATE v2_book SET budget=?, max_pos=?, started_at=? WHERE market=?",
                           (budget, mp, now_utc, market))
        v2.commit()
    finally:
        v2.close()
    # reset in-engine throttles/session so it re-arms cleanly next open
    try:
        from . import v2_live
        v2_live._EQ_SNAP.clear()
    except Exception:
        pass
    return JSONResponse({"ok": True, "budget": 100000})


@router.post("/api/sell")
def api_sell(payload: dict):
    """Manual paper sell — close a held position at the live price."""
    sym = str(payload.get("symbol", "")).upper().strip()
    market = payload.get("market", "IN")
    v2 = _rw()
    try:
        r = v2.execute("SELECT id,entry_price,shares,strategy FROM v2_positions WHERE market=? AND symbol=?",
                       (market, sym)).fetchone()
        if not r:
            return JSONResponse({"error": "not holding " + sym}, status_code=400)
        pid, entry, shares, strat = r
        px = float((_live_map(market).get(sym) or {}).get("price") or entry)
        pnl = shares * (px - entry); ret = (px / entry - 1) * 100
        v2.execute("INSERT INTO v2_trades(market,strategy,symbol,entry_date,entry_price,exit_date,exit_price,shares,pnl,return_pct,reason,conviction)"
                   " SELECT market,strategy,symbol,entry_date,entry_price,?,?,?,?,?,'manual',conviction FROM v2_positions WHERE id=?",
                   (datetime.now(IST).date().isoformat(), round(px, 2), shares, round(pnl, 2), round(ret, 2), pid))
        v2.execute("DELETE FROM v2_positions WHERE id=?", (pid,))
        v2.commit()
    finally:
        v2.close()
    try:
        from . import telegram_bot
        telegram_bot.notify_trade("SELL", sym, shares, round(px, 2), market, pnl_pct=round(ret, 2), strategy="manual")
    except Exception:
        pass
    return JSONResponse({"ok": True, "symbol": sym, "pnl_pct": round(ret, 2)})


@router.delete("/api/alerts/{aid}")
def api_alerts_del(aid: int):
    v2 = _rw()
    _uwl(v2)
    v2.execute("DELETE FROM v2_alerts WHERE id=?", (aid,))
    v2.commit(); v2.close()
    return JSONResponse({"ok": True})


_alert_last_check = [0.0]


# Filing categories the engine treats as tradeable. Kept identical to
# v2_live's catalyst gate so an alert cannot fire on something the engine
# considers noise.
MATERIAL_CATEGORIES = ("results", "order", "corp_action")


def catalyst_since(symbol, since_iso):
    """Newest material NSE filing for `symbol` after `since_iso`, or None.

    Returns (category, subject, an_dt). Read-only against the catalyst feed's
    own database; a missing table or file simply means no catalyst, never an
    exception into the alert loop.
    """
    try:
        epoch = int(datetime.fromisoformat(str(since_iso).replace("Z", "+00:00")).timestamp())
    except (TypeError, ValueError):
        return None
    try:
        con = _ro(CATALYST_DB)
        row = con.execute(
            "SELECT category,subject,an_dt FROM nse_announcements WHERE symbol=? "
            "AND an_epoch > ? AND category IN (?,?,?) ORDER BY an_epoch DESC LIMIT 1",
            (str(symbol).upper(), epoch, *MATERIAL_CATEGORIES)).fetchone()
        con.close()
        return tuple(row) if row else None
    except Exception:
        _LOG.debug("catalyst lookup unavailable for %s", symbol, exc_info=True)
        return None


_ALERT_CANDLE_CACHE: dict = {}
ALERT_CANDLE_TTL = 300          # daily bars change once a session; 5 min is generous
ALERT_SMA_PERIODS = (20, 50, 200)


def _alert_bars(symbol, market="IN", limit=210):
    """Recent daily OHLC bars for an alerted symbol, cached.

    The alert loop runs every 20s and this reads the candle table, so results
    are cached for ALERT_CANDLE_TTL. Daily bars only change once a session, so
    a stale-by-minutes cache costs nothing and keeps the loop off the disk.
    """
    key = (str(symbol).upper(), market)
    hit = _ALERT_CANDLE_CACHE.get(key)
    if hit and time.time() - hit[0] < ALERT_CANDLE_TTL:
        return hit[1]
    source = "upstox-live:day" if market == "IN" else "alpaca-iex-live:day"
    bars = []
    try:
        con = _ro(MAIN_DB)
        rows = con.execute(
            "SELECT ts,open,high,low,close FROM candles WHERE symbol=? AND source=? "
            "ORDER BY ts DESC LIMIT ?", (key[0], source, int(limit))).fetchall()
        con.close()
        for ts, o, h, low, c in reversed(rows):
            if None in (o, h, low, c):
                continue
            bars.append((str(ts), float(o), float(h), float(low), float(c)))
    except Exception:
        _LOG.debug("alert candle load failed for %s", symbol, exc_info=True)
    _ALERT_CANDLE_CACHE[key] = (time.time(), bars)
    return bars


def _alert_candles(symbol, market="IN", limit=210):
    """Closing prices only, for the moving-average cross rules."""
    return [bar[4] for bar in _alert_bars(symbol, market, limit)]


def pattern_hit(bars, since_iso):
    """Candlestick patterns on the newest bar, if that bar is NEW.

    Returns the list of patterns found, or [] for no fire.

    The freshness check is what makes this usable. Daily patterns persist for
    the whole session, so firing on "a pattern exists" would re-trigger every
    20s until midnight. The bar must be dated after the alert was created, so
    each alert fires at most once per new bar.
    """
    if not bars:
        return []
    try:
        cutoff = str(since_iso)[:10]
        datetime.fromisoformat(cutoff)
    except (TypeError, ValueError):
        return []
    if str(bars[-1][0])[:10] <= cutoff:
        return []
    opens = [b[1] for b in bars]
    highs = [b[2] for b in bars]
    lows = [b[3] for b in bars]
    closes = [b[4] for b in bars]
    try:
        return list(ta.candlestick_patterns(opens, highs, lows, closes))
    except Exception:
        _LOG.debug("pattern detection failed", exc_info=True)
        return []


def sma_cross_hit(closes, price, period, direction):
    """Whether `price` has just crossed the `period`-bar SMA.

    A cross needs a BEFORE and an AFTER: the previous close on one side of the
    average and the live price on the other. Testing the live price alone would
    fire every cycle for as long as it stayed across, which is a level alert,
    not a cross.

    The SMA is computed on closes only, so it does not move intraday — the
    crossing is entirely price's doing, which is what makes it detectable.
    """
    try:
        period = int(period)
        price = float(price)
    except (TypeError, ValueError):
        return False
    if direction not in ("up", "down") or period <= 0:
        return False
    if not closes or len(closes) < period + 1:
        return False        # not enough history to know where price came from
    sma = sum(closes[-period:]) / period
    previous = closes[-1]
    if sma <= 0:
        return False
    if direction == "up":
        return previous <= sma < price
    return previous >= sma > price


def alert_hit(kind, value, price, day_change_pct=None):
    """Whether one alert rule is satisfied. Pure, so it can be tested directly.

    `above`/`below` compare the live price; `pct` compares the ABSOLUTE day
    move, so a -6% collapse fires a 5% alert just as a +6% rally does.
    """
    try:
        value = float(value)
        price = float(price)
    except (TypeError, ValueError):
        return False
    if kind == "above":
        return price >= value
    if kind == "below":
        return price <= value
    if kind == "pct":
        try:
            return abs(float(day_change_pct or 0)) >= value
        except (TypeError, ValueError):
            return False
    return False


def _check_alerts():
    """Fire due alerts against live prices; returns newly triggered (for toasts).
    Called from the SSE loop, throttled to every ~5s."""
    now = time.time()
    if now - _alert_last_check[0] < 5:
        return []
    _alert_last_check[0] = now
    fired = []
    try:
        try:
            ro = _ro(V2_DB)
            rows = ro.execute("SELECT id,symbol,market,kind,value,created_at "
                              "FROM v2_alerts WHERE active=1").fetchall()
            ro.close()
        except Exception:
            return []                     # alerts table not created yet
        if not rows:
            return []
        v2 = None
        by_m: dict = {}
        for _, sym, m, _, _, _ in rows:
            by_m.setdefault(m, set()).add(sym)
        live = {m: _live_map(m, s) for m, s in by_m.items()}
        chg = {m: (_daychg(m, list(s)) if any(r[3] == "pct" and r[2] == m for r in rows) else {})
               for m, s in by_m.items()}
        for aid, sym, m, kind, value, created_at in rows:
            lq = live.get(m, {}).get(sym)
            if not lq:
                continue
            p = lq["price"]
            filing = None
            patterns = None
            if kind == "pattern":
                patterns = pattern_hit(_alert_bars(sym, m), created_at)
                hit = bool(patterns)
            elif kind in ("cross_up", "cross_down"):
                hit = sma_cross_hit(_alert_candles(sym, m), p, value,
                                    "up" if kind == "cross_up" else "down")
            elif kind == "catalyst":
                # Fires on the first material filing published after the alert
                # was set — not on price at all.
                filing = catalyst_since(sym, created_at)
                hit = filing is not None
            else:
                hit = alert_hit(kind, value, p, chg.get(m, {}).get(sym, {}).get("chg"))
            if hit:
                if v2 is None:
                    v2 = _rw()
                v2.execute("UPDATE v2_alerts SET active=0, triggered_at=?, triggered_price=? WHERE id=?",
                           (datetime.now(timezone.utc).isoformat(), round(p, 2), aid))
                entry = dict(id=aid, symbol=sym, market=m, kind=kind, value=value, price=round(p, 2))
                if patterns:
                    entry["patterns"] = patterns
                if filing:
                    entry["catalyst"] = {"category": filing[0], "subject": filing[1],
                                         "when": filing[2]}
                fired.append(entry)
        if v2 is not None:
            v2.commit(); v2.close()
        if fired:
            try:
                from . import telegram_bot
                for f in fired:
                    telegram_bot.notify_alert(f["symbol"], f["market"], f["kind"], f["value"], f["price"])
            except Exception:
                pass
    except Exception:
        return []
    return fired


@router.get("/api/search")
def api_search(q: str = ""):
    q = q.strip().upper()
    if len(q) < 2:
        return JSONResponse([])
    con = _ro(MAIN_DB)
    rows = con.execute(
        "SELECT symbol,name,exchange FROM universe WHERE enabled=1 AND (symbol LIKE ? OR upper(name) LIKE ?) "
        "ORDER BY CASE WHEN symbol LIKE ? THEN 0 ELSE 1 END, length(symbol) LIMIT 8",
        (q + "%", "%" + q + "%", q + "%")).fetchall()
    con.close()
    return JSONResponse([dict(symbol=r[0], name=(r[1] or "")[:40],
                              market="IN" if str(r[2]).upper() in ("NSE", "BSE") else "US") for r in rows])


_movers_cache: dict = {}
_movers_loading: set = set()


def _movers_bg():
    """Compute movers per market in the background — cold panel loads take
    10-60s and must never block a request."""
    try:
        out = {}
        for m in ("IN", "US"):
            try:
                syms, _ = _panel(m)
                d = _daychg(m, list(syms.keys()))
                rows = sorted(((s, v["price"], v["chg"]) for s, v in d.items() if v["chg"] is not None and abs(v["chg"]) > 0.01),
                              key=lambda r: -r[2])
                fmt = lambda r: dict(symbol=r[0], price=round(r[1], 2), chg=round(r[2], 2),
                                     ccy="₹" if m == "IN" else "$")
                out[m] = dict(up=[fmt(r) for r in rows[:6]], down=[fmt(r) for r in rows[-6:]][::-1])
            except Exception:
                out[m] = dict(up=[], down=[])
        _movers_cache.update(t=time.time(), v=out)
    finally:
        _movers_loading.discard("x")


@router.get("/api/movers")
def api_movers():
    """Top gainers/losers per market (live price vs panel prev-close). Instant:
    serves the cache; refreshes it in a background thread (60s TTL)."""
    c = _movers_cache.get("v")
    if (c is None or time.time() - _movers_cache.get("t", 0) > 60) and "x" not in _movers_loading:
        _movers_loading.add("x")
        import threading
        threading.Thread(target=_movers_bg, daemon=True).start()
    return JSONResponse(c or {"IN": dict(up=[], down=[]), "US": dict(up=[], down=[]), "warming": True})


_sector_cache = {}
_sector_loading = set()


def _sectors_bg():
    """Average intraday % change per sector across the liquid panel — a terminal-
    style sector heatmap. Cached; refreshed in the background (cold panel = slow)."""
    try:
        con = _ro(MAIN_DB)
        secmap = {s: (sec or "Other") for s, sec in con.execute("SELECT symbol, sector FROM universe")}
        con.close()
        out = {}
        for m in ("IN",):
            try:
                syms, _ = _panel(m)
                d = _daychg(m, list(syms.keys()))
                agg = {}
                for s, v in d.items():
                    if v.get("chg") is None:
                        continue
                    sec = secmap.get(s, "Other")
                    if sec in ("NSE Listed Equity", "", None):
                        sec = "Other"
                    agg.setdefault(sec, []).append((v["chg"], s))
                rows = []
                for sec, lst in agg.items():
                    if len(lst) < 3 or sec == "Other":
                        continue
                    lst.sort(reverse=True)
                    rows.append(dict(sector=sec, chg=round(sum(x[0] for x in lst) / len(lst), 2),
                                     n=len(lst), top=[x[1] for x in lst[:3]]))
                rows.sort(key=lambda r: -r["chg"])
                out[m] = rows
            except Exception:
                out[m] = []
        _sector_cache.update(t=time.time(), v=out)
    finally:
        _sector_loading.discard("x")


@router.get("/api/sectors")
def api_sectors():
    """Sector heatmap data (avg day change per sector). Serves cache, refreshes 90s."""
    c = _sector_cache.get("v")
    if (c is None or time.time() - _sector_cache.get("t", 0) > 90) and "x" not in _sector_loading:
        _sector_loading.add("x")
        import threading
        threading.Thread(target=_sectors_bg, daemon=True).start()
    return JSONResponse(c or {"IN": [], "warming": True})


_index_cache = {}
_index_loading = set()
_INDEX_SYMS = [("^NSEI", "Nifty 50"), ("^NSEBANK", "Bank Nifty"), ("^BSESN", "Sensex")]


def _indices_bg():
    """Fetch Nifty 50 / Bank Nifty / Sensex from Yahoo (has all three incl BSE)."""
    try:
        import httpx
        H = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/124 Safari/537.36"}
        out = []
        with httpx.Client(headers=H, timeout=8, follow_redirects=True) as cl:
            for sym, nm in _INDEX_SYMS:
                try:
                    m = cl.get("https://query1.finance.yahoo.com/v8/finance/chart/" + sym).json()["chart"]["result"][0]["meta"]
                    px = m.get("regularMarketPrice")
                    pc = m.get("chartPreviousClose") or m.get("previousClose")
                    if px and pc:
                        out.append(dict(name=nm, last=round(px, 2), chg=round((px / pc - 1) * 100, 2)))
                except Exception:
                    pass
        if out:
            _index_cache.update(t=time.time(), v=out)
    finally:
        _index_loading.discard("x")


@router.get("/api/indices")
def api_indices():
    """Nifty 50 / Bank Nifty / Sensex for the top index bar. Cache + 30s bg refresh."""
    c = _index_cache.get("v")
    if (c is None or time.time() - _index_cache.get("t", 0) > 30) and "x" not in _index_loading:
        _index_loading.add("x")
        import threading
        threading.Thread(target=_indices_bg, daemon=True).start()
    return JSONResponse(c or [])


CATALYST_DB = os.environ.get("CATALYST_DB", "/opt/opentrade/var/catalysts.db")
_CAT_LABEL = {"results": "Q results", "order": "New order", "corp_action": "Corp action"}


@router.get("/api/catalysts")
def api_catalysts():
    """Today's material NSE corporate filings (results / orders / corp actions) —
    the real-time news that drives price+volume buying. Powers the dashboard
    Catalysts panel (borrowed 'news feed' component idea)."""
    out = []
    try:
        con = _ro(CATALYST_DB)
        cutoff = int(time.time()) - 30 * 3600
        for sym, cat, subj, an_dt in con.execute(
                "SELECT symbol, category, subject, an_dt FROM nse_announcements "
                "WHERE an_epoch >= ? AND category IN ('results','order','corp_action') "
                "ORDER BY an_epoch DESC LIMIT 40", (cutoff,)):
            out.append(dict(symbol=sym, kind=_CAT_LABEL.get(cat, cat),
                            cat=cat, subject=subj, when=(an_dt or "")[:17]))
        con.close()
    except Exception:
        pass
    return JSONResponse(out)


@router.get("/api/attribution")
def api_attribution():
    """P&L attribution by market x strategy (closed + open) and a daily equity
    curve with max drawdown per market — the 'is the engine actually working'
    view."""
    v2 = _ro(V2_DB)
    live = {"IN": _live_map("IN"), "US": _live_map("US")}
    agg = {}
    for m, strat, n, w, pnl, avg in v2.execute(
            "SELECT market,strategy,COUNT(*),SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END),"
            "COALESCE(SUM(pnl),0),COALESCE(AVG(return_pct),0) FROM v2_trades GROUP BY market,strategy"):
        agg[(m, strat)] = dict(market=m, ccy="₹" if m == "IN" else "$", strategy=strat, closed=n,
                               win=round(w / n * 100) if n else 0, realized=round(pnl, 2),
                               avg_ret=round(avg, 2), open=0, unrealized=0.0)
    for m, strat, sym, entry, shares in v2.execute(
            "SELECT market,strategy,symbol,entry_price,shares FROM v2_positions"):
        p = live.get(m, {}).get(sym, {}).get("price", entry)
        a = agg.setdefault((m, strat), dict(market=m, ccy="₹" if m == "IN" else "$", strategy=strat,
                                            closed=0, win=0, realized=0.0, avg_ret=0.0, open=0, unrealized=0.0))
        a["open"] += 1
        a["unrealized"] = round(a["unrealized"] + (p - entry) * shares, 2)
    equity = {}
    for m in ("IN", "US"):
        daily = {}
        for d, e in v2.execute("SELECT substr(date,6,10), equity FROM v2_equity "
                               "WHERE market=? AND date LIKE 'LIVE_%' ORDER BY date", (m,)):
            daily[d] = e                       # last snapshot of each day wins
        ser = sorted(daily.items())
        peak = mdd = 0.0
        for _, e in ser:
            peak = max(peak, e)
            if peak:
                mdd = max(mdd, (peak - e) / peak * 100)
        equity[m] = dict(days=[d for d, _ in ser], equity=[round(e) for _, e in ser], maxdd=round(mdd, 2))
    v2.close()
    return JSONResponse(dict(strategies=sorted(agg.values(), key=lambda x: (x["market"], x["strategy"])),
                             equity=equity))


_ticker_cache: dict = {}
# liquid large-caps shown on the tape alongside held names (the live feed only
# sends `price`, no prev close, so day-change is computed against the candle DB)
WATCH = {
    "IN": ["RELIANCE", "TCS", "HDFCBANK", "ICICIBANK", "INFY", "SBIN", "BHARTIARTL",
           "ITC", "LT", "HINDUNILVR", "AXISBANK", "KOTAKBANK"],
    "US": ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AMD", "AVGO", "JPM"],
}


@router.get("/api/ticker")
def api_ticker():
    """Ticker-tape feed, OPEN markets first. Held names carry a live P&L since
    entry (the feed has no prev-close and the candle history is stale, so a true
    day-change isn't available); watchlist large-caps show live price only.
    Cached 5s."""
    now = time.time()
    c = _ticker_cache.get("v")
    if c is not None and now - _ticker_cache.get("t", 0) < 5:
        return JSONResponse(c)
    try:
        from . import v2_live
        mkts = list(v2_live.ENABLED_MARKETS)
    except Exception:
        mkts = ["IN"]
    held = {"IN": {}, "US": {}}
    try:
        v2 = _ro(V2_DB)
        for market, sym, entry in v2.execute("SELECT market,symbol,entry_price FROM v2_positions"):
            held.setdefault(market, {})[sym] = entry
        v2.close()
    except Exception:
        pass
    try:
        from . import market_regions
        openm = {m: bool(market_regions.market_session_for_region(m).get("is_open")) for m in mkts}
    except Exception:
        openm = {m: False for m in mkts}
    order = sorted(mkts, key=lambda m: not openm.get(m))   # open markets first
    out = []
    for market in order:
        ccy = "₹" if market == "IN" else "$"
        hsyms = list(held.get(market, {}).keys())
        wsyms = [s for s in WATCH.get(market, []) if s not in held.get(market, {})]
        live = _live_map(market, hsyms + wsyms)
        dc = _daychg(market, hsyms + wsyms)
        for s in hsyms + wsyms:
            if s not in live:
                continue
            chg = (dc.get(s) or {}).get("chg")
            out.append(dict(symbol=s, market=market, ccy=ccy, price=round(live[s]["price"], 2),
                            pnl=(round(chg, 2) if chg is not None else None),
                            held=(s in held.get(market, {})), open=openm.get(market, False)))
    _ticker_cache.update(t=now, v=out)
    return JSONResponse(out)


@router.get("/api/engine-status")
def api_engine_status():
    try:
        from . import v2_live
        st = v2_live.status()
        markets = list(v2_live.ENABLED_MARKETS)
    except Exception:
        st = {}
        markets = ["IN"]
    sessions = {}
    for m in markets:
        try:
            from . import market_regions
            sessions[m] = market_regions.market_session_for_region(m).get("is_open", False)
        except Exception:
            sessions[m] = False
    return JSONResponse(dict(engine=st, market_open=sessions))


def _stream_payload():
    v2 = _ro(V2_DB)
    held = {"IN": set(), "US": set()}
    for m, sym in v2.execute("SELECT market,symbol FROM v2_positions"):
        held.setdefault(m, set()).add(sym)
    # only read the prices we actually need (held names) — keeps the 1s stream cheap
    live = {"IN": _live_map("IN", held["IN"]), "US": _live_map("US", held["US"])}
    markets = []
    for market, budget in _markets(v2):
        s = _market_stats(v2, market, budget, live[market])
        markets.append(dict(market=market, ccy="₹" if market == "IN" else "$", equity=round(s["equity"]),
                            today_pnl=round(s["today_pnl"], 2), overall_pnl=round(s["overall_pnl"], 2),
                            today_pct=round(s["today_pnl"] / budget * 100, 2) if budget else 0,
                            overall_pct=round(s["overall_pnl"] / budget * 100, 2) if budget else 0))
    positions = []
    for pid, market, sym, entry, shares in v2.execute("SELECT id,market,symbol,entry_price,shares FROM v2_positions"):
        p = live.get(market, {}).get(sym, {}).get("price", entry)
        positions.append(dict(id=pid, ccy="₹" if market == "IN" else "$", live=round(p, 2),
                              pnl=round((p / entry - 1) * 100, 2), pnl_amt=round((p - entry) * shares, 2)))
    v2.close()
    return dict(markets=markets, positions=positions, alerts_fired=_check_alerts(),
                as_of=datetime.now(IST).strftime("%H:%M:%S IST"))


@router.get("/api/stream")
async def api_stream():
    async def gen():
        while True:
            try:
                payload = await asyncio.to_thread(_stream_payload)
                yield "data: " + _jsonmod.dumps(payload) + "\n\n"
            except Exception:
                yield "data: {}\n\n"
            await asyncio.sleep(1.0)   # per-second live updates
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/api/trades")
def api_trades(limit: int = 60):
    v2 = _ro(V2_DB)
    rows = v2.execute(
        "SELECT market,strategy,symbol,entry_date,entry_price,exit_date,exit_price,shares,pnl,"
        "return_pct,reason FROM v2_trades ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    v2.close()
    out = [dict(market=r[0], strategy=r[1], symbol=r[2], bought=_ist(r[3]), entry=round(r[4], 2),
               sold=_ist(r[5]), exit=round(r[6], 2), pnl_amt=round(r[8]), pnl=round(r[9], 2),
               reason=r[10], win=r[9] > 0) for r in rows]
    return JSONResponse(out)


@router.get("/api/orders")
def api_orders(limit: int = 120):
    """Full order log: BUY fills (open positions + entries of closed trades) and
    SELL fills (exits of closed trades), newest first."""
    v2 = _ro(V2_DB)
    orders = []
    for market, sym, edate, oat, entry, shares, strat in v2.execute(
            "SELECT market,symbol,entry_date,opened_at,entry_price,shares,strategy FROM v2_positions"):
        ccy = "₹" if market == "IN" else "$"
        orders.append(dict(side="BUY", status="open", symbol=sym, market=market, ccy=ccy, strategy=strat,
                           qty=round(shares, 2), price=round(entry, 2), value=round(entry * shares),
                           today=str(edate) == datetime.now(IST).date().isoformat(),
                           when=_ist(oat or edate), ts=str(oat or edate)))
    for market, sym, edate, entry, xdate, exitp, shares, pnl, ret, reason, strat in v2.execute(
            "SELECT market,symbol,entry_date,entry_price,exit_date,exit_price,shares,pnl,return_pct,reason,strategy "
            "FROM v2_trades ORDER BY id DESC LIMIT ?", (limit,)):
        ccy = "₹" if market == "IN" else "$"
        orders.append(dict(side="BUY", status="closed", symbol=sym, market=market, ccy=ccy, strategy=strat,
                           qty=round(shares, 2), price=round(entry, 2), value=round(entry * shares),
                           when=_ist(edate), ts=str(edate)))
        orders.append(dict(side="SELL", status="closed", symbol=sym, market=market, ccy=ccy, strategy=strat,
                           qty=round(shares, 2), price=round(exitp, 2), value=round(exitp * shares),
                           entry=round(entry, 2), pnl=round(ret, 2), pnl_amt=round(pnl), reason=reason,
                           today=str(xdate) == datetime.now(IST).date().isoformat(),
                           when=_ist(xdate), ts=str(xdate)))
    v2.close()
    orders.sort(key=lambda o: o["ts"], reverse=True)
    return JSONResponse(orders[:limit])


@router.post("/api/positions/{pid}/exit")
def api_exit(pid: int):
    rw = _rw()
    row = rw.execute("SELECT market,strategy,symbol,entry_date,entry_price,shares,conviction "
                     "FROM v2_positions WHERE id=?", (pid,)).fetchone()
    if not row:
        rw.close()
        return JSONResponse(dict(error="position not found"), status_code=404)
    market, strat, sym, edate, entry, shares, conv = row
    px = _live_map(market).get(sym, {}).get("price", entry)
    rw.execute("INSERT INTO v2_trades(market,strategy,symbol,entry_date,entry_price,exit_date,exit_price,"
               "shares,pnl,return_pct,reason,conviction) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
               (market, strat, sym, edate, entry, datetime.now(IST).date().isoformat(), px, shares,
                shares * (px - entry), (px / entry - 1) * 100, "manual", conv))
    rw.execute("DELETE FROM v2_positions WHERE id=?", (pid,))
    rw.commit(); rw.close()
    return JSONResponse(dict(ok=True, symbol=sym, exit=round(px, 2), pnl=round((px / entry - 1) * 100, 2)))


@router.get("/api/watch")
def api_watch():
    v2 = _ro(V2_DB)
    live = {"IN": _live_map("IN"), "US": _live_map("US")}
    held = {r[0] for r in v2.execute("SELECT symbol FROM v2_positions")}
    out, seen = [], set()
    for market, _ in _markets(v2):
        for strat in ("gap_momentum", "mom_breakout", "swing_meanrev"):
            d = v2.execute("SELECT MAX(date) FROM v2_signals WHERE market=? AND strategy=?",
                           (market, strat)).fetchone()[0]
            if not d:
                continue
            for sym, conv in v2.execute("SELECT symbol,conviction FROM v2_signals WHERE market=? AND strategy=? "
                                        "AND date=? ORDER BY rank LIMIT 8", (market, strat, d)):
                if sym in held or sym in seen:
                    continue
                seen.add(sym)
                lq = live[market].get(sym, {})
                badge = (f"gap {round(conv*15)}%" if strat == "gap_momentum"
                         else (f"breakout +{round(conv*100)}%" if strat == "mom_breakout" else f"dip · {conv:.2f}"))
                chg = ((lq.get("price", 0) / lq["prev"] - 1) * 100) if lq.get("prev") else 0
                out.append(dict(symbol=sym, market=market, ccy="₹" if market == "IN" else "$", strategy=strat,
                                badge=badge, live=round(lq.get("price", 0), 2), chg=round(chg, 2)))
    v2.close()
    return JSONResponse(out[:24])



@router.get("/api/portfolio")
def api_portfolio(market: str = "IN"):
    """Allocation, concentration, drawdown and per-lane realised-P&L curves."""
    v2 = _ro(V2_DB)
    try:
        row = v2.execute("SELECT budget FROM v2_book WHERE market=?", (market,)).fetchone()
        budget = row[0] if row else 0.0
        live = _live_map(market)
        positions = [
            (sym, strat, shares, entry, (live.get(sym) or {}).get("price"))
            for sym, strat, shares, entry in v2.execute(
                "SELECT symbol,strategy,shares,entry_price FROM v2_positions WHERE market=?",
                (market,))
        ]
        curve = list(v2.execute(
            "SELECT date,equity FROM v2_equity WHERE market=? AND date NOT LIKE 'LIVE_%' "
            "ORDER BY date", (market,)))
        trades = list(v2.execute(
            "SELECT strategy,exit_date,pnl FROM v2_trades WHERE market=?", (market,)))
    except Exception:
        _LOG.exception("portfolio query failed for %s", market)
        v2.close()
        return JSONResponse({"error": "portfolio data unavailable"}, status_code=503)
    v2.close()
    payload = pf.build(positions, curve, trades, budget)
    payload["market"] = market
    payload["ccy"] = "\u20b9" if market == "IN" else "$"
    return JSONResponse(payload)

@router.get("/api/stats")
def api_stats():
    v2 = _ro(V2_DB)
    live = {"IN": _live_map("IN"), "US": _live_map("US")}
    out = []
    for market, budget in _markets(v2):
        s = _market_stats(v2, market, budget, live[market])
        curve = [dict(d=r[0], e=round(r[1])) for r in v2.execute(
            "SELECT date,equity FROM v2_equity WHERE market=? AND date NOT LIKE 'LIVE_%' ORDER BY date", (market,))]
        rets = [r[0] for r in v2.execute("SELECT return_pct FROM v2_trades WHERE market=?", (market,))]
        wins = [r for r in rets if r > 0]; loss = [r for r in rets if r <= 0]
        lanes = strategy_stats(v2.execute(
            "SELECT strategy,return_pct,pnl,entry_date,exit_date FROM v2_trades WHERE market=?",
            (market,)).fetchall())
        out.append(dict(market=market, ccy="₹" if market == "IN" else "$",
                        overall_pnl=round(s["overall_pnl"]), today_pnl=round(s["today_pnl"]),
                        win=round(s["win"]), pf=s["pf"], trades=s["trades"], deploy_pct=s["deploy_pct"],
                        avg_win=round(sum(wins) / len(wins), 2) if wins else 0,
                        avg_loss=round(sum(loss) / len(loss), 2) if loss else 0,
                        by_strategy=lanes, curve=curve))
    v2.close()
    return JSONResponse(out)


@router.get("/api/stock/{symbol}")
def api_stock(symbol: str, market: str = "IN"):
    symbol = symbol.upper()
    live = _live_map(market).get(symbol, {})
    if not _panel_warm(market):     # cold 12GB panel -> never block the request; kick the loader and degrade
        return JSONResponse(dict(symbol=symbol, warming=True,
                                 live=round(live.get("price", 0), 2),
                                 error="analysis warming up — try again in a few seconds"))
    try:
        syms, mdf = _panel(market)
        g = syms.get(symbol)
        if g is None:
            return JSONResponse(dict(symbol=symbol, error="not in liquid universe",
                                     live=round(live.get("price", 0), 2)))
        gf = eng.compute_features(g, mdf)
        row = gf.iloc[-1]
        conv = eng.conviction(row)
        atr = float(row["atr14"]); close = float(row["close"])
        px = float(live.get("price") or close)             # plan off the LIVE price, not a stale close
        # realistic swing plan: stop = 1.5x ATR (capped -6%), target = 2x ATR but
        # CAPPED at +6% — a mean-reversion swing rarely runs 3.5x ATR in an ~8-day
        # hold, so the old +10%+ targets were misleading.
        entry = round(px, 2)
        atr_pct = (atr / entry) if entry > 0 else 0.02
        stop = round(entry * (1 - min(1.5 * atr_pct, 0.06)), 2)
        target = round(entry * (1 + min(2.0 * atr_pct, 0.06)), 2)
        rr = round((target - entry) / (entry - stop), 1) if entry > stop else 0
        verdict = "BUY" if conv >= 0.6 else ("WATCH" if conv >= 0.4 else "AVOID")
        def sc(x): return int(max(4, min(96, round(x))))   # graded, never a flat 100
        a20, a50 = close > row["sma20"], row["sma20"] > row["sma50"]
        factors = dict(
            trend=sc(18 + 34 * a20 + 24 * a50 + 18 * (close > row["sma50"])),
            rel_strength=sc(50 + float(row["rs20"]) * 320),
            volume=sc(28 + (float(row["rvol"]) - 1.0) * 52),
            pullback=sc(92 + float(row["dist_hi20"]) * 230),     # near a base high scores high, not 100
            volatility=sc(float(row["atr_pct"]) / 0.05 * 78))
        held = None
        try:
            v2 = _ro(V2_DB)
            r = v2.execute("SELECT strategy,entry_price,shares,stop,target,trail,peak FROM v2_positions "
                           "WHERE symbol=? AND market=?", (symbol, market)).fetchone()
            v2.close()
            if r:
                hstrat, hentry, hsh, hstop, htgt, htrail, hpeak = r
                tstop = max(hstop, hpeak * (1 - htrail)) if htrail else hstop
                held = dict(strategy="gap" if "gap" in hstrat else "swing", entry=round(hentry, 2),
                            qty=round(hsh, 2), pnl=round((px / hentry - 1) * 100, 2),
                            rule=(f"trailing stop {round(htrail*100)}% (now {round(tstop,2)})" if htrail
                                  else f"target {round(htgt,2)} / stop {round(hstop,2)}"))
        except Exception:
            held = None
        try:
            closes = [round(float(x), 2) for x in g["close"].tail(60).tolist() if x == x]
            closes.append(round(px, 2))     # end the line at the live price
        except Exception:
            closes = []
        try:
            tail = g.tail(90)
            candles, cdates = [], []
            for ts, o, h, lo, cl, vv in zip(tail.index, tail["open"], tail["high"], tail["low"],
                                            tail["close"], tail["volume"]):
                if cl != cl:
                    continue
                candles.append([round(float(o), 2), round(float(h), 2), round(float(lo), 2),
                                round(float(cl), 2), int(float(vv)) if vv == vv else 0])
                cdates.append(str(ts)[:10])
        except Exception:
            candles, cdates = [], []
        ccy = "₹" if market == "IN" else "$"
        # ---- extra technicals (VWAP/SuperTrend/Ichimoku/pivots/patterns) ----
        # Isolated: these are informational, so a failure here must degrade to an
        # empty block rather than turning the whole stock page into an error.
        technicals, tech_summary = {}, ""
        try:
            hist = g.tail(160)
            o_, h_, l_, c_, v_ = [], [], [], [], []
            for o, h, lo, cl, vv in zip(hist["open"], hist["high"], hist["low"],
                                        hist["close"], hist["volume"]):
                if cl != cl or o != o or h != h or lo != lo:   # skip NaN bars
                    continue
                o_.append(float(o)); h_.append(float(h)); l_.append(float(lo))
                c_.append(float(cl)); v_.append(float(vv) if vv == vv else 0.0)
            last_bar = str(hist.index[-1])[:10] if len(hist.index) else None
            technicals, tech_summary = _technical_block(o_, h_, l_, c_, v_, px, ccy, last_bar)
        except Exception:
            _LOG.exception("technical block failed for %s", symbol)
            technicals, tech_summary = {}, ""
        # ---- plain-English insight so a user can actually decide ----
        rs = float(row["rs20"]); rvol = float(row["rvol"]); dist = float(row["dist_hi20"])
        trend_txt = ("in a clear uptrend (above its 20- and 50-day averages)" if (a20 and a50)
                     else "holding above its 20-day average" if a20
                     else "below its key moving averages (downtrend)")
        rs_txt = ("outperforming the market" if rs > 0.03 else
                  "moving in line with the market" if rs > -0.03 else "lagging the market")
        vol_txt = ("on heavy volume" if rvol > 1.5 else "on light volume" if rvol < 0.8 else "on average volume")
        loc_txt = ("right at its recent highs" if dist > -0.02 else
                   "pulled back from its highs (a possible entry zone)" if dist > -0.12 else "well off its highs")
        reg_txt = "risk-on (healthy)" if _regime(market) else "risk-off (weak)"
        vmap = {"BUY": "rates this a BUY setup", "WATCH": "is watching this, not buying yet",
                "AVOID": "would avoid this for now"}
        insight = ("%s is %s and %s, trading %s %s. The engine %s (conviction %.2f) with the broader market %s. "
                   "If you buy: enter around %s%s, stop %s%s (%.1f%%), target %s%s (+%.1f%%) — about %s:1 reward-to-risk."
                   % (symbol, trend_txt, rs_txt, loc_txt, vol_txt, vmap.get(verdict, ""), conv, reg_txt,
                      ccy, entry, ccy, stop, (stop / entry - 1) * 100, ccy, target, (target / entry - 1) * 100, rr))
        if tech_summary:
            insight = f"{insight} {tech_summary}"
        # Structured, evidence-grounded recommendation. Additive and
        # display-only: `verdict` above and the engine's lane logic are
        # unchanged, so this does not affect what gets traded.
        news_items = _news(symbol)
        recommendation = rec.build_recommendation(dict(
            symbol=symbol, price=px, close=close, conviction=conv,
            sma20=float(row["sma20"]), sma50=float(row["sma50"]),
            rs20=rs, rvol=rvol, atr_pct=float(row["atr_pct"]),
            regime_on=_regime(market), technicals=technicals,
            entry=entry, stop=stop, target=target,
            news=news_items,
            news_score=(news_items[0].get("score") if news_items else None),
            held=bool(held),
        ))
        # Prose over the structured call. No model is wired here yet:
        # llm_provider is offline on this deployment, so this serves the
        # deterministic narrative. When a writer is supplied it is still
        # gated by verify_narrative(), which discards any figure not
        # traceable to the evidence.
        recommendation["narrative"] = narr.narrate(recommendation)
        # Multi-agent panel. Additive and display-only: each analyst reports its
        # own domain and the CIO surfaces disagreement, which the single blended
        # recommendation score cannot express.
        recommendation["panel"] = ana.analyse(dict(
            symbol=symbol, price=px, close=close,
            sma20=float(row["sma20"]), sma50=float(row["sma50"]),
            rvol=rvol, atr_pct=float(row["atr_pct"]), regime_on=_regime(market),
            technicals=technicals, news=news_items,
            news_score=(news_items[0].get("score") if news_items else None),
            held=held, macro=_macro_flags(),
        ))
        return JSONResponse(dict(symbol=symbol, market=market, live=round(px, 2),
                                 verdict=verdict, score=round(conv, 2), entry=entry, stop=stop,
                                 target=target, rr=rr, regime=_regime(market), factors=factors,
                                 insight=insight, held=held, chart=closes, candles=candles,
                                 dates=cdates, technicals=technicals,
                                 recommendation=recommendation, news=news_items))
    except Exception as exc:
        return JSONResponse(dict(symbol=symbol, error=str(exc)[:120], news=_news(symbol)))


_PATTERN_LABELS = {
    "doji": ("indecision", "neutral"),
    "hammer": ("a hammer (possible bottom)", "bullish"),
    "hanging_man": ("a hanging man (possible top)", "bearish"),
    "inverted_hammer": ("an inverted hammer", "bullish"),
    "shooting_star": ("a shooting star (rejection of higher prices)", "bearish"),
    "bullish_marubozu": ("a strong full-bodied up day", "bullish"),
    "bearish_marubozu": ("a strong full-bodied down day", "bearish"),
    "bullish_engulfing": ("a bullish engulfing pattern", "bullish"),
    "bearish_engulfing": ("a bearish engulfing pattern", "bearish"),
    "morning_star": ("a morning star (reversal up)", "bullish"),
    "evening_star": ("an evening star (reversal down)", "bearish"),
}


VWAP_WINDOW = 20


def _macro_flags(today=None):
    """Calendar flags for the macro analyst, from date arithmetic only.

    Deliberately does NOT construct MacroCalendarService: that needs settings
    and a Database handle, and this runs in a request path where the rule is to
    never block. These flags are pure functions of the date, reusing
    macro_calendar's own expiry helper so the definition of "expiry" cannot
    drift between the two.
    """
    from . import macro_calendar as mc
    try:
        day = today or datetime.now(IST).date()
        weekly = mc._nearest_weekly_expiry(day)
        monthly = mc._last_thursday(day.year, day.month)
        return {
            "is_expiry_day": day == weekly or day == monthly,
            "is_expiry_week": (weekly - day).days <= 4,
            "is_monthly_expiry_day": day == monthly,
            # RBI MPC placeholders sit early in alternate months; treat the
            # first full week of those months as policy-sensitive.
            "is_rbi_week": day.month in (2, 4, 6, 8, 10, 12) and day.day <= 8,
            "is_budget_week": day.month == 2 and day.day <= 7,
        }
    except Exception:
        _LOG.exception("macro flags failed")
        return {}


def _technical_block(opens, highs, lows, closes, volumes, price, ccy="₹", as_of=None):
    """Compute the extra indicators plus a plain-English summary.

    Pure and list-based so it can be tested without a DataFrame or a database.
    Returns (payload, summary). Every field is independently None/empty when its
    own data requirement is not met, so a short history degrades gracefully
    rather than dropping the whole block.
    """
    payload: dict = {}
    if not closes:
        return payload, ""

    snap = ta.advanced_snapshot(opens, highs, lows, closes, volumes)

    def _r(value, digits=2):
        return round(float(value), digits) if isinstance(value, (int, float)) else None

    st = snap.get("supertrend") or {}
    ichi = snap.get("ichimoku") or {}
    # Pin the VWAP lookback. advanced_snapshot() averages every bar it is given,
    # which would make the number depend on how much history the panel happened
    # to return. A fixed window is a stable, comparable figure.
    windowed_vwap = ta.vwap(highs, lows, closes, volumes, window=VWAP_WINDOW)
    if windowed_vwap is None:
        windowed_vwap = snap.get("vwap")
    payload = {
        "atr": _r(snap.get("atr")),
        "vwap": _r(windowed_vwap),
        "vwap_window": VWAP_WINDOW if windowed_vwap is not None else None,
        "supertrend": {"value": _r(st.get("value")), "direction": st.get("direction")},
        "ichimoku": {k: _r(v) for k, v in ichi.items()},
        "pivot_points": {k: _r(v) for k, v in (snap.get("pivot_points") or {}).items()},
        "fibonacci": {k: _r(v) for k, v in (snap.get("fibonacci") or {}).items()},
        "patterns": list(snap.get("candlestick_patterns") or []),
        # Date of the last candle these were computed from. The daily candle
        # feed can lag the live quote by a session, in which case every value
        # here is as-of that date and NOT the live price — say so rather than
        # letting the UI imply otherwise.
        "as_of": as_of,
        "stale": bool(as_of and closes and price and abs(price - closes[-1]) / closes[-1] > 0.02),
    }

    # ---- plain English, in the voice the rest of this endpoint already uses ----
    parts: list[str] = []
    vwap = payload["vwap"]
    if vwap and price:
        side = "above" if price >= vwap else "below"
        label = f"{payload['vwap_window']}-day" if payload.get("vwap_window") else "average"
        parts.append(f"trading {side} its {label} volume-weighted price of {ccy}{vwap}")
    direction = payload["supertrend"]["direction"]
    st_value = payload["supertrend"]["value"]
    if direction and st_value:
        parts.append(
            f"the SuperTrend is {direction} with the line at {ccy}{st_value}"
            + (" (support)" if direction == "up" else " (resistance)")
        )
    kijun = payload["ichimoku"].get("kijun")
    if kijun and price:
        parts.append(f"{'above' if price >= kijun else 'below'} the Ichimoku baseline {ccy}{kijun}")
    pivots = payload["pivot_points"]
    if pivots.get("s1") and pivots.get("r1"):
        parts.append(f"today's pivot support is {ccy}{pivots['s1']} and resistance {ccy}{pivots['r1']}")

    named = [_PATTERN_LABELS[p][0] for p in payload["patterns"] if p in _PATTERN_LABELS]
    if named:
        parts.append("the last candle shows " + " and ".join(named[:2]))

    if not parts:
        return payload, ""
    summary = "Technicals: " + "; ".join(parts) + "."
    if payload["stale"]:
        drift = (price / closes[-1] - 1) * 100
        summary += (f" (Computed from candles up to {as_of}; the live price is "
                    f"{drift:+.1f}% away from that close, so treat these as stale.)")
    elif as_of:
        summary += f" (as of {as_of})"
    return payload, summary


def _news(symbol):
    try:
        con = _ro(MAIN_DB)
        rows = con.execute("SELECT events_json,score,ts FROM sentiment_events WHERE symbol=? "
                           "ORDER BY ts DESC LIMIT 4", (symbol,)).fetchall()
        con.close()
    except Exception:
        return []
    import json as _json
    out, seen = [], set()
    for ej, score, ts in rows:
        try:
            for e in _json.loads(ej or "[]"):
                t = (e.get("title") or "").strip()
                if not t or t in seen:
                    continue
                seen.add(t)
                out.append(dict(title=t[:120], label=e.get("event_type", "neutral"),
                                score=round(float(e.get("score") or 0), 2), when=_ist(ts)))
                if len(out) >= 5:
                    return out
        except Exception:
            continue
    return out


@router.get("/", response_class=HTMLResponse)
@router.get("", response_class=HTMLResponse)
def spa():
    return HTMLResponse(SPA_HTML)


SPA_HTML = r"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1,maximum-scale=1">
<title>OpenStocks</title>
<style>
:root{--bg:#fff;--surf:#f6f6f4;--line:#e7e7e3;--tx:#16160f;--mut:#76766e;--up:#0f8a5f;--upb:#e4f5ee;--dn:#c4362f;--dnb:#fbeceb;--inf:#185fa5;--infb:#e6f1fb;--warn:#9a6308;--warnb:#fbf0d8;--brand:#16160f}
@media(prefers-color-scheme:dark){:root{--bg:#16160f;--surf:#21211b;--line:#33332c;--tx:#f3f1ea;--mut:#a3a399;--upb:#0e2a20;--dnb:#2c1413;--infb:#0c2438;--warnb:#2a2008}}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
body{margin:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:var(--bg);color:var(--tx);font-size:15px}
.wrap{max-width:760px;margin:0 auto;padding:0 14px 88px}
.row{display:flex;align-items:center;justify-content:space-between}
.mut{color:var(--mut)} .up{color:var(--up)} .dn{color:var(--dn)}
.num{font-variant-numeric:tabular-nums}
.top{display:flex;align-items:center;justify-content:space-between;padding:14px 2px 8px}
.brand{font-size:17px;font-weight:600}
.live{display:flex;align-items:center;gap:5px;font-size:12px;color:var(--up);background:var(--upb);padding:2px 8px;border-radius:20px}
.dot{width:6px;height:6px;border-radius:50%;background:var(--up);animation:pulse 1.6s infinite}
@keyframes pulse{50%{opacity:.35}}
.hero{font-size:32px;font-weight:600;letter-spacing:-.5px;margin:2px 0}
.chips{display:flex;gap:8px;flex-wrap:wrap;margin:6px 0 14px}
.chip{font-size:12px;padding:3px 9px;border-radius:20px;background:var(--surf)}
.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:9px}
@media(min-width:560px){.grid{grid-template-columns:repeat(4,minmax(0,1fr))}}
.card{background:var(--surf);border-radius:12px;padding:11px 13px}
.tile .v{font-size:19px;font-weight:600;margin-top:1px}
.sec{font-size:15px;font-weight:600;margin:18px 2px 9px}
.pos{border:1px solid var(--line);border-radius:12px;padding:11px 13px;margin-bottom:9px}
.bar{height:5px;border-radius:3px;background:var(--surf);overflow:hidden;margin-top:8px}
.bar>i{display:block;height:100%;border-radius:3px}
.badge{font-size:10px;padding:1px 7px;border-radius:10px}
.bg-inf{background:var(--infb);color:var(--inf)} .bg-warn{background:var(--warnb);color:var(--warn)}
.bg-mut{background:var(--surf);color:var(--mut)}
.nav{position:fixed;left:0;right:0;bottom:0;background:var(--bg);border-top:1px solid var(--line);display:flex;max-width:760px;margin:0 auto}
.nav a{flex:1;text-align:center;padding:9px 0 8px;color:var(--mut);text-decoration:none;font-size:11px;display:flex;flex-direction:column;align-items:center;gap:3px;cursor:pointer}
.nav a.on{color:var(--tx)} .nav svg{width:22px;height:22px;stroke:currentColor;fill:none;stroke-width:1.7}
.tab{display:none} .tab.on{display:block}
.skel{color:var(--mut);padding:30px 0;text-align:center}
.lrow{display:flex;align-items:center;justify-content:space-between;padding:10px 2px;border-bottom:1px solid var(--line);cursor:pointer}
.scorebar{height:5px;border-radius:3px;background:var(--surf);overflow:hidden;margin-top:3px}
.scorebar>i{display:block;height:100%;background:var(--up)}
button.act{font-size:14px;padding:11px;border-radius:10px;border:1px solid var(--line);background:var(--bg);color:var(--tx);flex:1}
button.act.pri{background:var(--inf);color:#fff;border-color:var(--inf)}
.back{font-size:14px;color:var(--inf);cursor:pointer;display:inline-flex;gap:5px;align-items:center;padding:10px 0}
.vbox{border-radius:10px;padding:11px 13px;margin:10px 0}
</style><style>
:root{--surf:#f6f7f9;--line:#eaecf0;--tx:#0c0d10;--mut:#697586;--up:#06a35a;--upb:#e7f7ef;--dn:#df2f29;--inf:#2563eb;--infb:#eaf0fe;--sh:0 1px 2px rgba(16,24,40,.06)}
@media(prefers-color-scheme:dark){:root{--bg:#0b0c0e;--surf:#15171b;--card:#15171b;--line:#24262d;--tx:#f0f2f5;--mut:#8b919e;--up:#26c281;--dn:#ff5a52;--inf:#5b8def;--sh:0 1px 2px rgba(0,0,0,.4)}}
body{line-height:1.45;-webkit-font-smoothing:antialiased}
.hero{font-size:36px;font-weight:680;letter-spacing:-.03em;margin:4px 0 2px}
.card{background:var(--card);border:1px solid var(--line);box-shadow:var(--sh);border-radius:14px;padding:13px 15px}
.raise{border-radius:14px;box-shadow:var(--sh);padding:15px 16px}
.pos{background:var(--card);border-radius:14px;box-shadow:var(--sh);padding:13px 15px;transition:border-color .15s}.pos:hover{border-color:var(--mut)}
.sec{font-size:12.5px;color:var(--mut);text-transform:uppercase;letter-spacing:.05em;font-weight:600;margin:22px 2px 10px}
.seg{border-radius:11px;padding:3px}.seg b{border-radius:8px;padding:5px 12px}.seg b.on{background:var(--card);box-shadow:var(--sh)}
.badge{border-radius:7px;font-weight:600;padding:2px 8px}
.num{letter-spacing:-.01em}
button{font-weight:500;border-radius:11px;transition:background .15s,transform .05s}button:hover{background:var(--surf)}button:active{transform:scale(.985)}
button.pri:hover{opacity:.9;background:var(--acc)}
input,select{border-radius:11px;padding:12px 13px;transition:border-color .15s,box-shadow .15s}
input:focus,select:focus{outline:none;border-color:var(--inf);box-shadow:0 0 0 3px var(--infb)}
.tab.on{animation:fade .22s ease}@keyframes fade{from{opacity:0;transform:translateY(5px)}to{opacity:1;transform:none}}
.lrow{border-radius:9px;padding:13px 8px;transition:background .12s}.lrow:hover{background:var(--surf)}
.chip{border:1px solid var(--line);border-radius:9px;font-weight:500}
.nav{padding-bottom:env(safe-area-inset-bottom)}.nav a.on{color:var(--inf)}.nav svg,.side svg{stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}
.modepill{text-transform:uppercase;font-weight:600;font-size:10px;letter-spacing:.05em;border-radius:7px;padding:3px 9px}
.bar,.scorebar{border-radius:4px}.bar>i,.scorebar>i{border-radius:4px;transition:width .4s}
@media(min-width:860px){.side{width:230px;padding:20px 14px;gap:2px}.side .b{font-size:19px;font-weight:680;letter-spacing:-.02em;padding:6px 12px 22px}.side a{padding:11px 13px;border-radius:11px;font-weight:500;transition:all .15s}.side a:hover{background:var(--surf);color:var(--tx)}.side a.on{background:var(--infb);color:var(--inf)}.main{padding:6px 32px 36px}}
#login{max-width:380px;margin:11vh auto;padding:28px 26px;border:1px solid var(--line);border-radius:18px;box-shadow:0 4px 20px rgba(16,24,40,.06)}#login h1{font-size:25px;font-weight:680;letter-spacing:-.02em}
</style></head><body><div class=wrap>

<div class=top><div class=brand>OpenStocks</div>
<div class=live><span class=dot></span><span id=clock>live</span></div></div>

<div id=home class="tab on">
  <div class=mut style="font-size:12px">paper portfolio</div>
  <div class=hero id=pv>—</div>
  <div id=ppnl style="font-size:14px">&nbsp;</div>
  <div class=chips id=regime></div>
  <div class=grid id=engines></div>
  <div class=sec>open positions</div>
  <div id=homepos></div>
</div>

<div id=watch class=tab><div class=sec>watchlist · engine candidates</div><div id=watchlist class=skel>loading…</div></div>
<div id=positions class=tab><div class=sec>positions</div><div id=poslist class=skel>loading…</div></div>
<div id=stats class=tab><div class=sec>engine performance</div><div id=statlist class=skel>loading…</div></div>
<div id=detail class=tab></div>

</div>
<nav class=nav>
<a data-t=home class=on onclick="go('home')"><svg viewBox="0 0 24 24"><path d="M3 11l9-8 9 8M5 10v10h14V10"/></svg>home</a>
<a data-t=watch onclick="go('watch')"><svg viewBox="0 0 24 24"><path d="M1 12s4-7 11-7 11 7 11 7-4 7-11 7-11-7-11-7z"/><circle cx=12 cy=12 r=3/></svg>watch</a>
<a data-t=positions onclick="go('positions')"><svg viewBox="0 0 24 24"><rect x=3 y=6 width=18 height=13 rx=2/><path d="M3 10h18M16 6V4"/></svg>positions</a>
<a data-t=stats onclick="go('stats')"><svg viewBox="0 0 24 24"><path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/></svg>stats</a>
</nav>
<script>
var INR=new Intl.NumberFormat('en-IN'),cur='home';
function sgn(x){return (x>0?'+':'')+x}
function col(x){return x>0?'up':(x<0?'dn':'mut')}
function go(t){cur=t;document.querySelectorAll('.tab').forEach(e=>e.classList.toggle('on',e.id==t));
 document.querySelectorAll('.nav a').forEach(a=>a.classList.toggle('on',a.dataset.t==t));
 if(t=='watch')loadWatch();if(t=='positions')loadPos();if(t=='stats')loadStats();window.scrollTo(0,0)}
function eng4(e){return `<div class="card tile"><div class=row><span class=mut style="font-size:12px">${e.market} · ${e.strategy.indexOf('gap')>=0?'gap':'swing'}</span><span class="${col(e.ret)}" style="font-size:13px">${sgn(e.ret)}%</span></div><div class="mut" style="font-size:11px;margin-top:3px">win ${e.win}% · PF ${e.pf} · ${e.positions} pos</div></div>`}
function posCard(p){var c=p.headroom>40?'var(--up)':(p.headroom>15?'var(--warn)':'var(--dn)');var sym=p.market=='IN'?'₹':'$';
 return `<div class=pos onclick="stock('${p.symbol}','${p.market}')"><div class=row><div style="display:flex;gap:8px;align-items:center"><b>${p.symbol}</b><span class="badge ${p.strategy.indexOf('gap')>=0?'bg-inf':'bg-mut'}">${p.strategy.indexOf('gap')>=0?'gap':'swing'}</span></div><div style="text-align:right"><span class=num>${sym}${p.live}</span> <span class="${col(p.pnl)}" style="font-size:13px">${sgn(p.pnl)}%</span></div></div><div class=bar><i style="width:${p.headroom}%;background:${c}"></i></div><div class=mut style="font-size:10px;margin-top:4px">${p.trail?'trail':'stop'} ${sym}${p.stop}</div></div>`}
function load(){fetch('/v2/api/overview').then(r=>r.json()).then(d=>{
 document.getElementById('pv').textContent='₹'+INR.format(d.portfolio.equity);
 var u=d.portfolio.unreal;document.getElementById('ppnl').innerHTML='<span class="'+col(u)+'">'+sgn(INR.format(u))+' open · '+sgn(d.portfolio.ret)+'% all-time</span>';
 document.getElementById('clock').textContent=d.as_of;
 document.getElementById('regime').innerHTML=['IN','US'].map(m=>'<span class=chip><span style="color:'+(d.regime[m]?'var(--up)':'var(--warn)')+'">●</span> '+m+(d.regime[m]?' risk-on':' risk-off')+'</span>').join('');
 document.getElementById('engines').innerHTML=d.engines.map(eng4).join('');
});
 fetch('/v2/api/positions').then(r=>r.json()).then(d=>{document.getElementById('homepos').innerHTML=d.slice(0,4).map(posCard).join('')||'<div class=skel>no open positions</div>';});}
function loadPos(){fetch('/v2/api/positions').then(r=>r.json()).then(d=>{document.getElementById('poslist').innerHTML=d.map(posCard).join('')||'<div class=skel>no open positions</div>';});}
function loadWatch(){fetch('/v2/api/watch').then(r=>r.json()).then(d=>{document.getElementById('watchlist').innerHTML=d.map(w=>{var sym=w.market=='IN'?'₹':'$';return `<div class=lrow onclick="stock('${w.symbol}','${w.market}')"><div style="display:flex;gap:8px;align-items:center"><b>${w.symbol}</b><span class="badge ${w.strategy.indexOf('gap')>=0?'bg-inf':'bg-warn'}">${w.badge}</span></div><div><span class=num>${sym}${w.live}</span> <span class="${col(w.chg)}" style="font-size:13px">${sgn(w.chg)}%</span></div></div>`}).join('')||'<div class=skel>no candidates right now</div>';});}
function loadStats(){fetch('/v2/api/stats').then(r=>r.json()).then(d=>{document.getElementById('statlist').innerHTML=d.map(s=>`<div class=pos><div class=row><b>${s.market}</b><span class="${col(s.ret)}">${sgn(s.ret)}%</span></div><div class=grid style="margin-top:10px"><div class=card><div class=mut style="font-size:11px">win rate</div><div class=tile><div class=v>${s.win}%</div></div></div><div class=card><div class=mut style="font-size:11px">profit factor</div><div class=tile><div class=v>${s.pf}</div></div></div><div class=card><div class=mut style="font-size:11px">avg win</div><div class="v up" style="font-size:17px">${sgn(s.avg_win)}%</div></div><div class=card><div class=mut style="font-size:11px">avg loss</div><div class="v dn" style="font-size:17px">${s.avg_loss}%</div></div></div><div class=mut style="font-size:11px;margin-top:8px">${s.trades} closed trades</div></div>`).join('');});}
function stock(sym,mkt){go('detail');var el=document.getElementById('detail');el.innerHTML='<div class=skel>analysing '+sym+'…</div>';
 fetch('/v2/api/stock/'+sym+'?market='+mkt).then(r=>r.json()).then(d=>{var s=mkt=='IN'?'₹':'$';
 if(d.error){el.innerHTML='<div class=back onclick="go(\'home\')">‹ back</div><div class=sec>'+sym+'</div><div class=skel>'+d.error+(d.live?' · live '+s+d.live:'')+'</div>';return;}
 var vc=d.verdict=='BUY'?'up':(d.verdict=='WATCH'?'warn':'dn');var vb=d.verdict=='BUY'?'var(--upb)':(d.verdict=='WATCH'?'var(--warnb)':'var(--dnb)');var vt=d.verdict=='BUY'?'var(--up)':(d.verdict=='WATCH'?'var(--warn)':'var(--dn)');
 var f=d.factors,fb=Object.keys(f).map(k=>'<div style="margin:8px 0"><div class=row style="font-size:12px"><span class=mut>'+k.replace('_',' ')+'</span><span>'+f[k]+'</span></div><div class=scorebar><i style="width:'+f[k]+'%"></i></div></div>').join('');
 el.innerHTML='<div class=back onclick="go(\'home\')">‹ back</div><div class=row><div><div class=sec style="margin:0">'+sym+'</div><div class=mut style="font-size:12px">'+mkt+' · live</div></div><div class=hero style="font-size:26px">'+s+d.live+'</div></div>'
 +'<div class=vbox style="background:'+vb+'"><div class=row><b style="color:'+vt+'">'+d.verdict+'</b><span style="color:'+vt+';font-size:12px">score '+d.score+(d.regime?'':' · regime risk-off')+'</span></div></div>'
 +'<div class=grid><div class=card><div class=mut style="font-size:11px">entry</div><div class=v style="font-size:17px">'+s+d.entry+'</div></div><div class=card><div class=mut style="font-size:11px">reward:risk</div><div class=v style="font-size:17px">'+d.rr+':1</div></div><div class=card><div class=mut style="font-size:11px">stop</div><div class="v dn" style="font-size:17px">'+s+d.stop+'</div></div><div class=card><div class=mut style="font-size:11px">target</div><div class="v up" style="font-size:17px">'+s+d.target+'</div></div></div>'
 +'<div class=sec>why this score</div>'+fb
 +'<div style="display:flex;gap:9px;margin:16px 0"><button class=act>Set alert</button><button class="act pri">Paper buy</button></div>';});}
load();setInterval(()=>{if(cur=='home')load();},20000);
</script></body></html>"""


SPA_HTML = r"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>OpenStocks — AI trading</title>
<style>
:root{--bg:#fff;--surf:#f7f7f5;--card:#fff;--line:#e8e8e3;--tx:#16160f;--mut:#76766e;--up:#0f8a5f;--upb:#e4f5ee;--dn:#c4362f;--dnb:#fbeceb;--inf:#185fa5;--infb:#e6f1fb;--warn:#9a6308;--warnb:#fbf0d8;--acc:#16160f}
@media(prefers-color-scheme:dark){:root{--bg:#121210;--surf:#1d1d18;--card:#1a1a16;--line:#33332c;--tx:#f3f1ea;--mut:#a3a399;--upb:#0e2a20;--dnb:#2c1413;--infb:#0c2438;--warnb:#2a2008;--acc:#f3f1ea}}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
body{margin:0;background:var(--bg);color:var(--tx);font-size:15px}
.num{font-variant-numeric:tabular-nums}.mut{color:var(--mut)}.up{color:var(--up)}.dn{color:var(--dn)}
.hide{display:none!important}
input,select,button{font-size:15px}
input,select{width:100%;padding:11px 12px;border:1px solid var(--line);border-radius:10px;background:var(--bg);color:var(--tx)}
button{cursor:pointer;border:1px solid var(--line);border-radius:10px;background:var(--bg);color:var(--tx);padding:11px 14px}
button.pri{background:var(--acc);color:var(--bg);border-color:var(--acc)}
/* login */
#login{max-width:360px;margin:9vh auto;padding:0 18px}
#login h1{font-size:24px;font-weight:600;margin:0 0 4px}
/* shell */
.app{display:flex;min-height:100vh}
.side{display:none}
.main{flex:1;max-width:980px;margin:0 auto;width:100%;padding:0 16px 90px}
.top{display:flex;align-items:center;justify-content:space-between;padding:13px 2px 8px;position:sticky;top:0;background:var(--bg);z-index:5}
.brand{font-size:17px;font-weight:600;display:flex;align-items:center;gap:8px}
.live{display:flex;align-items:center;gap:5px;font-size:11px;color:var(--up);background:var(--upb);padding:2px 8px;border-radius:20px}
.dot{width:6px;height:6px;border-radius:50%;background:var(--up);animation:p 1.6s infinite}@keyframes p{50%{opacity:.35}}
.seg{display:inline-flex;background:var(--surf);border-radius:20px;padding:2px}
.seg b{font-size:12px;font-weight:500;padding:4px 11px;border-radius:18px;cursor:pointer;color:var(--mut)}
.seg b.on{background:var(--bg);color:var(--tx)}
.prof{width:30px;height:30px;border-radius:50%;background:var(--infb);color:var(--inf);display:flex;align-items:center;justify-content:center;font-size:13px;font-weight:600;cursor:pointer}
.menu{position:absolute;right:16px;top:52px;background:var(--card);border:1px solid var(--line);border-radius:12px;padding:6px;min-width:170px;z-index:20}
.menu a{display:flex;gap:9px;align-items:center;padding:10px 12px;border-radius:8px;cursor:pointer;font-size:14px}.menu a:hover{background:var(--surf)}
.modepill{font-size:11px;padding:2px 9px;border-radius:20px}
.hero{font-size:31px;font-weight:600;letter-spacing:-.5px;margin:2px 0}
.chips{display:flex;gap:8px;flex-wrap:wrap;margin:7px 0 14px}.chip{font-size:12px;padding:3px 9px;border-radius:20px;background:var(--surf)}
.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:9px}
@media(min-width:620px){.grid{grid-template-columns:repeat(4,minmax(0,1fr))}}
.card{background:var(--surf);border-radius:12px;padding:11px 13px}
.raise{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 15px;margin-bottom:12px}
.sec{font-size:15px;font-weight:600;margin:18px 2px 9px}
.pos{border:1px solid var(--line);border-radius:12px;padding:11px 13px;margin-bottom:9px;cursor:pointer}
.bar{height:5px;border-radius:3px;background:var(--surf);overflow:hidden;margin-top:8px}.bar>i{display:block;height:100%}
.badge{font-size:10px;padding:1px 7px;border-radius:10px}.bg-inf{background:var(--infb);color:var(--inf)}.bg-mut{background:var(--surf);color:var(--mut)}.bg-warn{background:var(--warnb);color:var(--warn)}
.lrow{display:flex;align-items:center;justify-content:space-between;padding:11px 2px;border-bottom:1px solid var(--line);cursor:pointer}
.row{display:flex;align-items:center;justify-content:space-between}
.tab{display:none}.tab.on{display:block}
.skel{color:var(--mut);padding:26px 0;text-align:center}
.scorebar{height:5px;border-radius:3px;background:var(--surf);overflow:hidden;margin-top:3px}.scorebar>i{display:block;height:100%;background:var(--up)}
.field{margin:10px 0}.field label{font-size:12px;color:var(--mut);display:block;margin-bottom:5px}
.toggle{display:inline-flex;border:1px solid var(--line);border-radius:20px;overflow:hidden}
.toggle b{font-size:13px;padding:7px 16px;cursor:pointer;color:var(--mut)}.toggle b.on{background:var(--acc);color:var(--bg)}
.nav{position:fixed;left:0;right:0;bottom:0;background:var(--bg);border-top:1px solid var(--line);display:flex;z-index:8}
.nav a{flex:1;text-align:center;padding:9px 0 7px;color:var(--mut);font-size:11px;display:flex;flex-direction:column;align-items:center;gap:3px;cursor:pointer}
.nav a.on{color:var(--tx)}.nav svg,.side svg{width:22px;height:22px;stroke:currentColor;fill:none;stroke-width:1.7}
@media(min-width:860px){
 .nav{display:none}
 .side{display:flex;flex-direction:column;width:210px;border-right:1px solid var(--line);padding:16px 12px;position:sticky;top:0;height:100vh}
 .side .b{font-size:18px;font-weight:600;padding:6px 10px 18px}
 .side a{display:flex;gap:11px;align-items:center;padding:10px 12px;border-radius:10px;color:var(--mut);cursor:pointer;font-size:14px}
 .side a.on{background:var(--surf);color:var(--tx)}
 .main{padding:0 26px 30px}
}
</style><style>
:root{--surf:#f6f7f9;--line:#eaecf0;--tx:#0c0d10;--mut:#697586;--up:#06a35a;--upb:#e7f7ef;--dn:#df2f29;--inf:#2563eb;--infb:#eaf0fe;--sh:0 1px 2px rgba(16,24,40,.06)}
@media(prefers-color-scheme:dark){:root{--bg:#0b0c0e;--surf:#15171b;--card:#15171b;--line:#24262d;--tx:#f0f2f5;--mut:#8b919e;--up:#26c281;--dn:#ff5a52;--inf:#5b8def;--sh:0 1px 2px rgba(0,0,0,.4)}}
body{line-height:1.45;-webkit-font-smoothing:antialiased}
.hero{font-size:36px;font-weight:680;letter-spacing:-.03em;margin:4px 0 2px}
.card{background:var(--card);border:1px solid var(--line);box-shadow:var(--sh);border-radius:14px;padding:13px 15px}
.raise{border-radius:14px;box-shadow:var(--sh);padding:15px 16px}
.pos{background:var(--card);border-radius:14px;box-shadow:var(--sh);padding:13px 15px;transition:border-color .15s}.pos:hover{border-color:var(--mut)}
.sec{font-size:12.5px;color:var(--mut);text-transform:uppercase;letter-spacing:.05em;font-weight:600;margin:22px 2px 10px}
.seg{border-radius:11px;padding:3px}.seg b{border-radius:8px;padding:5px 12px}.seg b.on{background:var(--card);box-shadow:var(--sh)}
.badge{border-radius:7px;font-weight:600;padding:2px 8px}
.num{letter-spacing:-.01em}
button{font-weight:500;border-radius:11px;transition:background .15s,transform .05s}button:hover{background:var(--surf)}button:active{transform:scale(.985)}
button.pri:hover{opacity:.9;background:var(--acc)}
input,select{border-radius:11px;padding:12px 13px;transition:border-color .15s,box-shadow .15s}
input:focus,select:focus{outline:none;border-color:var(--inf);box-shadow:0 0 0 3px var(--infb)}
.tab.on{animation:fade .22s ease}@keyframes fade{from{opacity:0;transform:translateY(5px)}to{opacity:1;transform:none}}
.lrow{border-radius:9px;padding:13px 8px;transition:background .12s}.lrow:hover{background:var(--surf)}
.chip{border:1px solid var(--line);border-radius:9px;font-weight:500}
.nav{padding-bottom:env(safe-area-inset-bottom)}.nav a.on{color:var(--inf)}.nav svg,.side svg{stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}
.modepill{text-transform:uppercase;font-weight:600;font-size:10px;letter-spacing:.05em;border-radius:7px;padding:3px 9px}
.bar,.scorebar{border-radius:4px}.bar>i,.scorebar>i{border-radius:4px;transition:width .4s}
@media(min-width:860px){.side{width:230px;padding:20px 14px;gap:2px}.side .b{font-size:19px;font-weight:680;letter-spacing:-.02em;padding:6px 12px 22px}.side a{padding:11px 13px;border-radius:11px;font-weight:500;transition:all .15s}.side a:hover{background:var(--surf);color:var(--tx)}.side a.on{background:var(--infb);color:var(--inf)}.main{padding:6px 32px 36px}}
#login{max-width:380px;margin:11vh auto;padding:28px 26px;border:1px solid var(--line);border-radius:18px;box-shadow:0 4px 20px rgba(16,24,40,.06)}#login h1{font-size:25px;font-weight:680;letter-spacing:-.02em}
</style></head><body>

<div id=login class=hide>
 <h1>OpenStocks</h1><p class=mut style="margin:0 0 22px">AI trading platform · sign in</p>
 <div class=field><label>username</label><input id=u autocomplete=username></div>
 <div class=field><label>password</label><input id=pw type=password autocomplete=current-password></div>
 <button class=pri style="width:100%;margin-top:8px" onclick=doLogin()>Sign in</button>
 <div id=lerr class=dn style="font-size:13px;margin-top:10px"></div>
</div>

<div id=app class="app hide">
 <nav class=side>
  <div class=b>OpenStocks</div>
  <a data-t=home onclick="go('home')"><svg viewBox="0 0 24 24"><path d="M3 11l9-8 9 8M5 10v10h14V10"/></svg>Home</a>
  <a data-t=watch onclick="go('watch')"><svg viewBox="0 0 24 24"><path d="M1 12s4-7 11-7 11 7 11 7-4 7-11 7-11-7-11-7z"/><circle cx=12 cy=12 r=3/></svg>Watchlist</a>
  <a data-t=positions onclick="go('positions')"><svg viewBox="0 0 24 24"><rect x=3 y=6 width=18 height=13 rx=2/><path d="M3 10h18"/></svg>Portfolio</a>
  <a data-t=stats onclick="go('stats')"><svg viewBox="0 0 24 24"><path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/></svg>Performance</a>
  <a data-t=account onclick="go('account')"><svg viewBox="0 0 24 24"><circle cx="12" cy="8" r="4"/><path d="M4 21c0-4 4-6 8-6s8 2 8 6"/></svg>Account</a>
 </nav>
 <div class=main>
  <div class=top>
   <div class=brand><span class=live><span class=dot></span><span id=clock>live</span></span></div>
   <div style="display:flex;align-items:center;gap:10px">
    <div class=seg id=mkt><b data-m=IN class=on>India</b></div>
    <div class=prof id=avatar onclick="document.getElementById('pm').classList.toggle('hide')">U</div>
   </div>
  </div>
  <div id=pm class="menu hide">
   <a onclick="go('account');document.getElementById('pm').classList.add('hide')"><svg width=16 height=16 viewBox="0 0 24 24" fill=none stroke=currentColor stroke-width=1.7><circle cx="12" cy="8" r="4"/><path d="M4 21c0-4 4-6 8-6s8 2 8 6"/></svg> Account & settings</a>
   <a onclick=doLogout()><svg width=16 height=16 viewBox="0 0 24 24" fill=none stroke=currentColor stroke-width=1.7><path d="M16 17l5-5-5-5M21 12H9M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/></svg> Log out</a>
  </div>

  <div id=home class="tab on">
   <div class=row><div class=mut style="font-size:12px">paper portfolio</div><span id=modeb class="modepill bg-warn">paper</span></div>
   <div class=hero id=pv>—</div><div id=ppnl style="font-size:14px">&nbsp;</div>
   <div class=chips id=regime></div>
   <div class=grid id=engines></div>
   <div class=sec>open positions</div><div id=homepos></div>
  </div>
  <div id=watch class=tab><div class=sec>watchlist · live engine candidates</div><div id=watchlist class=skel>loading…</div></div>
  <div id=positions class=tab><div class=sec>positions</div><div id=poslist class=skel>loading…</div></div>
  <div id=stats class=tab><div class=sec>engine performance</div><div id=statlist class=skel>loading…</div></div>
  <div id=account class=tab></div>
  <div id=detail class=tab></div>
 </div>
</div>

<nav class=nav>
<a data-t=home class=on onclick="go('home')"><svg viewBox="0 0 24 24"><path d="M3 11l9-8 9 8M5 10v10h14V10"/></svg>home</a>
<a data-t=watch onclick="go('watch')"><svg viewBox="0 0 24 24"><path d="M1 12s4-7 11-7 11 7 11 7-4 7-11 7-11-7-11-7z"/><circle cx=12 cy=12 r=3/></svg>watch</a>
<a data-t=positions onclick="go('positions')"><svg viewBox="0 0 24 24"><rect x=3 y=6 width=18 height=13 rx=2/><path d="M3 10h18"/></svg>portfolio</a>
<a data-t=stats onclick="go('stats')"><svg viewBox="0 0 24 24"><path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/></svg>stats</a>
<a data-t=account onclick="go('account')"><svg viewBox="0 0 24 24"><circle cx="12" cy="8" r="4"/><path d="M4 21c0-4 4-6 8-6s8 2 8 6"/></svg>account</a>
</nav>

<script>
var INR=new Intl.NumberFormat('en-IN'),cur='home',MKT='BOTH',ME=null,MODE='paper';
function sgn(x){return (x>0?'+':'')+x}function col(x){return x>0?'up':(x<0?'dn':'mut')}
function show(id){document.getElementById(id).classList.remove('hide')}
function hide(id){document.getElementById(id).classList.add('hide')}
function api(u,opt){return fetch(u,Object.assign({headers:{'Content-Type':'application/json'}},opt||{})).then(r=>r.json().catch(()=>({})).then(j=>({ok:r.ok,j:j})))}
function setMkt(m){MKT=m;document.querySelectorAll('#mkt b').forEach(b=>b.classList.toggle('on',b.dataset.m==m));refresh()}
function go(t){cur=t;['home','watch','positions','stats','account','detail'].forEach(x=>document.getElementById(x).classList.toggle('on',x==t));
 document.querySelectorAll('.nav a,.side a').forEach(a=>a.classList.toggle('on',a.dataset.t==t));
 if(t=='watch')loadWatch();if(t=='positions')loadPos();if(t=='stats')loadStats();if(t=='account')loadAccount();window.scrollTo(0,0)}
function inMkt(m){return MKT=='BOTH'||m==MKT}
/* auth */
function boot(){api('/api/auth/me').then(r=>{var u=r.j.user||r.j;
 if(r.ok&&(u&&u.username)){ME=u;MODE=(u.signal_execution_mode||'paper');hide('login');show('app');
  document.getElementById('avatar').textContent=(u.username[0]||'U').toUpperCase();refresh();
 }else{show('login');hide('app');}}).catch(()=>{show('login');hide('app')})}
function doLogin(){document.getElementById('lerr').textContent='';
 api('/api/auth/login',{method:'POST',body:JSON.stringify({username:document.getElementById('u').value,password:document.getElementById('pw').value})})
 .then(r=>{if(r.ok&&!r.j.detail){boot()}else{document.getElementById('lerr').textContent=r.j.detail||'Login failed'}})}
function doLogout(){api('/api/auth/logout',{method:'POST'}).then(()=>{ME=null;show('login');hide('app')})}
/* render helpers */
function eng4(e){return `<div class=card><div class=row><span class=mut style="font-size:12px">${e.market} · ${e.strategy.indexOf('gap')>=0?'gap':'swing'}</span><span class="${col(e.ret)}" style="font-size:13px">${sgn(e.ret)}%</span></div><div class=mut style="font-size:11px;margin-top:3px">win ${e.win}% · PF ${e.pf} · ${e.positions} pos</div></div>`}
function posCard(p){var c=p.headroom>40?'var(--up)':(p.headroom>15?'var(--warn)':'var(--dn)');var s=p.market=='IN'?'₹':'$';
 return `<div class=pos onclick="stock('${p.symbol}','${p.market}')"><div class=row><div style="display:flex;gap:8px;align-items:center"><b>${p.symbol}</b><span class="badge ${p.strategy.indexOf('gap')>=0?'bg-inf':'bg-mut'}">${p.strategy.indexOf('gap')>=0?'gap':'swing'}</span></div><div style="text-align:right"><span class=num>${s}${p.live}</span> <span class="${col(p.pnl)}" style="font-size:13px">${sgn(p.pnl)}%</span></div></div><div class=bar><i style="width:${p.headroom}%;background:${c}"></i></div><div class=mut style="font-size:10px;margin-top:4px">${p.trail?'trail':'stop'} ${s}${p.stop}</div></div>`}
function refresh(){if(cur=='home')load();if(cur=='watch')loadWatch();if(cur=='positions')loadPos();if(cur=='stats')loadStats()}
function load(){api('/v2/api/overview').then(r=>{var d=r.j;
 var es=d.engines.filter(e=>inMkt(e.market));
 var eq=es.reduce((a,e)=>a+e.equity,0),un=es.reduce((a,e)=>a+e.unreal,0),st=es.length*1000000;
 document.getElementById('pv').textContent='₹'+INR.format(eq);
 document.getElementById('ppnl').innerHTML='<span class="'+col(un)+'">'+sgn(INR.format(un))+' open · '+sgn((st?((eq/st-1)*100):0).toFixed(2))+'% all-time</span>';
 document.getElementById('clock').textContent=d.as_of;
 document.getElementById('modeb').textContent=MODE;document.getElementById('modeb').className='modepill '+(MODE=='live'?'bg-inf':'bg-warn');
 document.getElementById('regime').innerHTML=['IN','US'].filter(inMkt).map(m=>'<span class=chip><span style="color:'+(d.regime[m]?'var(--up)':'var(--warn)')+'">●</span> '+m+(d.regime[m]?' risk-on':' risk-off')+'</span>').join('');
 document.getElementById('engines').innerHTML=es.map(eng4).join('');});
 api('/v2/api/positions').then(r=>{document.getElementById('homepos').innerHTML=r.j.filter(p=>inMkt(p.market)).slice(0,5).map(posCard).join('')||'<div class=skel>no open positions</div>';});}
function loadPos(){api('/v2/api/positions').then(r=>{document.getElementById('poslist').innerHTML=r.j.filter(p=>inMkt(p.market)).map(posCard).join('')||'<div class=skel>no open positions</div>';});}
function loadWatch(){api('/v2/api/watch').then(r=>{document.getElementById('watchlist').innerHTML=r.j.filter(w=>inMkt(w.market)).map(w=>{var s=w.market=='IN'?'₹':'$';return `<div class=lrow onclick="stock('${w.symbol}','${w.market}')"><div style="display:flex;gap:8px;align-items:center"><b>${w.symbol}</b><span class="badge ${w.strategy.indexOf('gap')>=0?'bg-inf':'bg-warn'}">${w.badge}</span></div><div><span class=num>${s}${w.live}</span> <span class="${col(w.chg)}" style="font-size:13px">${sgn(w.chg)}%</span></div></div>`}).join('')||'<div class=skel>no candidates</div>';});}
function loadStats(){api('/v2/api/stats').then(r=>{document.getElementById('statlist').innerHTML=r.j.filter(s=>inMkt(s.market)).map(s=>`<div class=raise><div class=row><b>${s.market}</b><span class="${col(s.ret)}">${sgn(s.ret)}%</span></div><div class=grid style="margin-top:10px"><div class=card><div class=mut style="font-size:11px">win rate</div><div style="font-size:18px;font-weight:600">${s.win}%</div></div><div class=card><div class=mut style="font-size:11px">profit factor</div><div style="font-size:18px;font-weight:600">${s.pf}</div></div><div class=card><div class=mut style="font-size:11px">avg win</div><div class="up" style="font-size:17px;font-weight:600">${sgn(s.avg_win)}%</div></div><div class=card><div class=mut style="font-size:11px">avg loss</div><div class="dn" style="font-size:17px;font-weight:600">${s.avg_loss}%</div></div></div><div class=mut style="font-size:11px;margin-top:8px">${s.trades} closed trades</div>${laneRows(s.by_strategy)}</div>`).join('')||'<div class=card style="padding:14px 16px"><span class=mut style="font-size:12px">no closed trades yet — stats appear after the first exits</span></div>';});}
function laneRows(lanes){if(!lanes||!lanes.length)return '';return '<div class=mut style="font-size:11px;margin:12px 0 6px">by lane</div>'+lanes.map(l=>`<div class=row style="padding:6px 0;border-top:1px solid var(--line)"><div><div style="font-size:13px;font-weight:600">${l.label}${l.overnight?' <span class=mut style="font-size:10px">overnight</span>':''}</div><div class=mut style="font-size:11px">${l.trades} trades · win ${l.win}% · PF ${l.pf}${l.avg_hold_days!=null?' · held '+l.avg_hold_days+'d':''}</div></div><div style="text-align:right"><div class="${col(l.avg)}" style="font-size:14px;font-weight:600">${sgn(l.avg)}%</div><div class=mut style="font-size:11px">avg/trade</div></div></div>`).join('');}
/* account + settings */
function loadAccount(){var el=document.getElementById('account');var u=ME||{};
 el.innerHTML=`<div class=sec>account</div>
 <div class=raise><div style="display:flex;gap:12px;align-items:center"><div class=prof style="width:44px;height:44px;font-size:17px">${(u.username||'U')[0].toUpperCase()}</div><div><div style="font-weight:600">${u.username||'—'}</div><div class=mut style="font-size:12px">${u.role||'user'} · credits ${u.credits!=null?u.credits:'—'}</div></div></div></div>
 <div class=sec>trading mode</div>
 <div class=raise><div class=mut style="font-size:13px;margin-bottom:9px">Paper trades on simulated cash. Live routes real orders through your connected broker.</div>
  <div class=toggle><b id=mp class="${MODE!='live'?'on':''}" onclick="setMode('paper')">Paper</b><b id=ml class="${MODE=='live'?'on':''}" onclick="setMode('live')">Live</b></div>
  <div id=modemsg class=mut style="font-size:12px;margin-top:9px"></div></div>
 <div class=sec>paper allocation</div>
 <div class=raise><div class=field><label>India cash (₹)</label><input id=cin type=number placeholder="e.g. 1000000"></div>
  <div class=field><label>US cash ($)</label><input id=cus type=number placeholder="e.g. 25000"></div>
  <button class=pri onclick=saveCash()>Save allocation</button><div id=cashmsg class=mut style="font-size:12px;margin-top:9px"></div></div>
 <div class=sec>broker connections (for live)</div>
 <div class=raise><div class=row style="padding:6px 0"><span>Upstox · India</span><a class=badge style="background:var(--infb);color:var(--inf);cursor:pointer" onclick="openBroker('upstox')">connect</a></div>
  <div class=row style="padding:6px 0;border-top:1px solid var(--line)"><span>Alpaca · US</span><a class=badge style="background:var(--infb);color:var(--inf);cursor:pointer" onclick="openBroker('alpaca')">connect</a></div></div>`;
 api('/api/account').then(r=>{try{var pc=r.j.paper&&r.j.paper.cash_by_market||r.j.paper_cash_by_market||{};if(pc.IN!=null)document.getElementById('cin').value=Math.round(pc.IN);if(pc.US!=null)document.getElementById('cus').value=Math.round(pc.US);}catch(e){}});}
function setMode(m){api('/api/me/signal-execution-mode',{method:'POST',body:JSON.stringify({signal_execution_mode:m})}).then(r=>{if(r.ok){MODE=(r.j.signal_execution_mode||m);document.getElementById('mp').className=(MODE!='live'?'on':'');document.getElementById('ml').className=(MODE=='live'?'on':'');document.getElementById('modemsg').textContent=r.j.message||('Mode: '+MODE);}else{document.getElementById('modemsg').textContent=r.j.detail||'Failed';}});}
function saveCash(){var b={};var i=document.getElementById('cin').value,u=document.getElementById('cus').value;if(i)b.india_cash=+i;if(u)b.us_cash=+u;
 api('/api/me/paper-cash',{method:'POST',body:JSON.stringify(b)}).then(r=>{document.getElementById('cashmsg').textContent=r.ok?'Saved.':(r.j.detail||'Failed');});}
function openBroker(which){var u=which=='upstox'?'/api/me/upstox/auth-url':'/api/alpaca/connect';api(u).then(r=>{if(r.j.auth_url){location.href=r.j.auth_url}else{alert('Open Account on the broker to connect '+which)}});}
function stock(sym,mkt){go('detail');var el=document.getElementById('detail');el.innerHTML='<div class=skel>analysing '+sym+'…</div>';
 api('/v2/api/stock/'+sym+'?market='+mkt).then(r=>{var d=r.j,s=mkt=='IN'?'₹':'$';
 if(d.error){el.innerHTML='<div class=mut style="padding:14px 0;cursor:pointer" onclick="go(\'home\')">‹ back</div><div class=sec>'+sym+'</div><div class=skel>'+d.error+(d.live?' · '+s+d.live:'')+'</div>';return;}
 var vt=d.verdict=='BUY'?'var(--up)':(d.verdict=='WATCH'?'var(--warn)':'var(--dn)'),vb=d.verdict=='BUY'?'var(--upb)':(d.verdict=='WATCH'?'var(--warnb)':'var(--dnb)');
 var fb=Object.keys(d.factors||{}).map(k=>'<div style="margin:8px 0"><div class=row style="font-size:12px"><span class=mut>'+k.replace('_',' ')+'</span><span>'+d.factors[k]+'</span></div><div class=scorebar><i style="width:'+d.factors[k]+'%"></i></div></div>').join('');
 el.innerHTML='<div class=mut style="padding:12px 0;cursor:pointer" onclick="go(\'home\')">‹ back</div><div class=row><div><div class=sec style="margin:0">'+sym+'</div><div class=mut style="font-size:12px">'+mkt+' · live</div></div><div class=hero style="font-size:26px">'+s+d.live+'</div></div>'
 +'<div class=raise style="background:'+vb+';border:none"><div class=row><b style="color:'+vt+'">'+d.verdict+'</b><span style="color:'+vt+';font-size:12px">score '+d.score+(d.regime?'':' · regime risk-off')+'</span></div></div>'
 +'<div class=grid><div class=card><div class=mut style="font-size:11px">entry</div><div style="font-size:17px;font-weight:600">'+s+d.entry+'</div></div><div class=card><div class=mut style="font-size:11px">reward:risk</div><div style="font-size:17px;font-weight:600">'+d.rr+':1</div></div><div class=card><div class=mut style="font-size:11px">stop</div><div class="dn" style="font-size:17px;font-weight:600">'+s+d.stop+'</div></div><div class=card><div class=mut style="font-size:11px">target</div><div class="up" style="font-size:17px;font-weight:600">'+s+d.target+'</div></div></div>'
 +'<div class=sec>why this score</div>'+fb+'<div style="display:flex;gap:9px;margin:16px 0"><button style="flex:1">Set alert</button><button class="pri" style="flex:1">'+(MODE=='live'?'Buy':'Paper buy')+'</button></div>';});}
boot();setInterval(()=>{if(cur=='home'&&ME)load();},20000);
</script></body></html>"""


SPA_HTML = r"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>OpenStocks — AI trading desk</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Crect width='24' height='24' rx='5' fill='%2315150f'/%3E%3Cpath d='M5 16l4-4 3 2 7-8' fill='none' stroke='%231D9E75' stroke-width='2.4' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E">
<style>
:root{--bg:#fff;--surf:#f6f6f3;--card:#fff;--line:#e8e8e3;--tx:#15150f;--mut:#75756d;--up:#0f8a5f;--upb:#e4f5ee;--dn:#c4362f;--dnb:#fbeceb;--inf:#185fa5;--infb:#e6f1fb;--warn:#946008;--warnb:#fbf0d8;--acc:#15150f}
@media(prefers-color-scheme:dark){:root{--bg:#111110;--surf:#1c1c17;--card:#191915;--line:#33332c;--tx:#f2f0e9;--mut:#a2a298;--upb:#0e2a20;--dnb:#2c1413;--infb:#0c2438;--warnb:#2a2008;--acc:#f2f0e9}}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
body{margin:0;background:var(--bg);color:var(--tx);font-size:15px}
.num{font-variant-numeric:tabular-nums}.mut{color:var(--mut)}.up{color:var(--up)}.dn{color:var(--dn)}.hide{display:none!important}
input,select,button{font-size:15px}
input,select{width:100%;padding:11px 12px;border:1px solid var(--line);border-radius:10px;background:var(--bg);color:var(--tx)}
button{cursor:pointer;border:1px solid var(--line);border-radius:10px;background:var(--bg);color:var(--tx);padding:10px 13px}
button.pri{background:var(--acc);color:var(--bg);border-color:var(--acc)}
button.sm{padding:6px 12px;font-size:13px;border-radius:8px}
#login{max-width:360px;margin:9vh auto;padding:0 18px}#login h1{font-size:24px;font-weight:600;margin:0 0 4px}
.app{display:flex;min-height:100vh}.side{display:none}
.main{flex:1;max-width:1000px;margin:0 auto;width:100%;padding:0 16px 92px}
.top{display:flex;align-items:center;justify-content:space-between;padding:13px 2px 8px;position:sticky;top:0;background:var(--bg);z-index:5}
.live{display:flex;align-items:center;gap:5px;font-size:11px;color:var(--up);background:var(--upb);padding:2px 8px;border-radius:20px}
.dot{width:6px;height:6px;border-radius:50%;background:var(--up);animation:p 1.6s infinite}@keyframes p{50%{opacity:.35}}
.seg{display:inline-flex;background:var(--surf);border-radius:20px;padding:2px}.seg b{font-size:12px;font-weight:500;padding:4px 11px;border-radius:18px;cursor:pointer;color:var(--mut)}.seg b.on{background:var(--bg);color:var(--tx)}
.prof{width:30px;height:30px;border-radius:50%;background:var(--infb);color:var(--inf);display:flex;align-items:center;justify-content:center;font-size:13px;font-weight:600;cursor:pointer}
.menu{position:absolute;right:16px;top:52px;background:var(--card);border:1px solid var(--line);border-radius:12px;padding:6px;min-width:180px;z-index:20}
.menu a{display:flex;gap:9px;align-items:center;padding:10px 12px;border-radius:8px;cursor:pointer;font-size:14px}.menu a:hover{background:var(--surf)}
.modepill{font-size:11px;padding:2px 9px;border-radius:20px}
.hero{font-size:30px;font-weight:600;letter-spacing:-.4px;margin:2px 0}
.chips{display:flex;gap:8px;flex-wrap:wrap;margin:8px 0 14px}.chip{font-size:12px;padding:3px 9px;border-radius:20px;background:var(--surf)}
.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:9px}@media(min-width:620px){.grid{grid-template-columns:repeat(4,minmax(0,1fr))}}
.card{background:var(--surf);border-radius:12px;padding:11px 13px}
.raise{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 15px;margin-bottom:12px}
.sec{font-size:15px;font-weight:600;margin:18px 2px 9px;display:flex;justify-content:space-between;align-items:baseline}
.pos{border:1px solid var(--line);border-radius:12px;padding:11px 13px;margin-bottom:9px}
.bar{height:5px;border-radius:3px;background:var(--surf);overflow:hidden;margin-top:8px}.bar>i{display:block;height:100%}
.badge{font-size:10px;padding:1px 7px;border-radius:10px}.bg-inf{background:var(--infb);color:var(--inf)}.bg-mut{background:var(--surf);color:var(--mut)}.bg-warn{background:var(--warnb);color:var(--warn)}.bg-up{background:var(--upb);color:var(--up)}.bg-dn{background:var(--dnb);color:var(--dn)}
.lrow{display:flex;align-items:center;justify-content:space-between;padding:11px 2px;border-bottom:1px solid var(--line);cursor:pointer}
.row{display:flex;align-items:center;justify-content:space-between}
.tab{display:none}.tab.on{display:block}.skel{color:var(--mut);padding:26px 0;text-align:center}
.scorebar{height:6px;border-radius:3px;background:var(--surf);overflow:hidden;margin-top:3px}.scorebar>i{display:block;height:100%}
.field{margin:10px 0}.field label{font-size:12px;color:var(--mut);display:block;margin-bottom:5px}
.toggle{display:inline-flex;border:1px solid var(--line);border-radius:20px;overflow:hidden}.toggle b{font-size:13px;padding:7px 16px;cursor:pointer;color:var(--mut)}.toggle b.on{background:var(--acc);color:var(--bg)}
.nav{position:fixed;left:0;right:0;bottom:0;background:var(--bg);border-top:1px solid var(--line);display:flex;z-index:8}
.nav a{flex:1;text-align:center;padding:8px 0 6px;color:var(--mut);font-size:10px;display:flex;flex-direction:column;align-items:center;gap:3px;cursor:pointer}.nav a.on{color:var(--tx)}.nav svg,.side svg{width:22px;height:22px;stroke:currentColor;fill:none;stroke-width:1.7}
@media(min-width:860px){.nav{display:none}.side{display:flex;flex-direction:column;width:212px;border-right:1px solid var(--line);padding:16px 12px;position:sticky;top:0;height:100vh}.side .b{font-size:18px;font-weight:600;padding:6px 10px 18px}.side a{display:flex;gap:11px;align-items:center;padding:10px 12px;border-radius:10px;color:var(--mut);cursor:pointer;font-size:14px}.side a.on{background:var(--surf);color:var(--tx)}.main{padding:0 28px 30px}}
</style><style>
:root{--surf:#f6f7f9;--line:#eaecf0;--tx:#0c0d10;--mut:#697586;--up:#06a35a;--upb:#e7f7ef;--dn:#df2f29;--inf:#2563eb;--infb:#eaf0fe;--sh:0 1px 2px rgba(16,24,40,.06)}
@media(prefers-color-scheme:dark){:root{--bg:#0b0c0e;--surf:#15171b;--card:#15171b;--line:#24262d;--tx:#f0f2f5;--mut:#8b919e;--up:#26c281;--dn:#ff5a52;--inf:#5b8def;--sh:0 1px 2px rgba(0,0,0,.4)}}
body{line-height:1.45;-webkit-font-smoothing:antialiased}
.hero{font-size:36px;font-weight:680;letter-spacing:-.03em;margin:4px 0 2px}
.card{background:var(--card);border:1px solid var(--line);box-shadow:var(--sh);border-radius:14px;padding:13px 15px}
.raise{border-radius:14px;box-shadow:var(--sh);padding:15px 16px}
.pos{background:var(--card);border-radius:14px;box-shadow:var(--sh);padding:13px 15px;transition:border-color .15s}.pos:hover{border-color:var(--mut)}
.sec{font-size:12.5px;color:var(--mut);text-transform:uppercase;letter-spacing:.05em;font-weight:600;margin:22px 2px 10px}
.seg{border-radius:11px;padding:3px}.seg b{border-radius:8px;padding:5px 12px}.seg b.on{background:var(--card);box-shadow:var(--sh)}
.badge{border-radius:7px;font-weight:600;padding:2px 8px}
.num{letter-spacing:-.01em}
button{font-weight:500;border-radius:11px;transition:background .15s,transform .05s}button:hover{background:var(--surf)}button:active{transform:scale(.985)}
button.pri:hover{opacity:.9;background:var(--acc)}
input,select{border-radius:11px;padding:12px 13px;transition:border-color .15s,box-shadow .15s}
input:focus,select:focus{outline:none;border-color:var(--inf);box-shadow:0 0 0 3px var(--infb)}
.tab.on{animation:fade .22s ease}@keyframes fade{from{opacity:0;transform:translateY(5px)}to{opacity:1;transform:none}}
.lrow{border-radius:9px;padding:13px 8px;transition:background .12s}.lrow:hover{background:var(--surf)}
.chip{border:1px solid var(--line);border-radius:9px;font-weight:500}
.nav{padding-bottom:env(safe-area-inset-bottom)}.nav a.on{color:var(--inf)}.nav svg,.side svg{stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}
.modepill{text-transform:uppercase;font-weight:600;font-size:10px;letter-spacing:.05em;border-radius:7px;padding:3px 9px}
.bar,.scorebar{border-radius:4px}.bar>i,.scorebar>i{border-radius:4px;transition:width .4s}
@media(min-width:860px){.side{width:230px;padding:20px 14px;gap:2px}.side .b{font-size:19px;font-weight:680;letter-spacing:-.02em;padding:6px 12px 22px}.side a{padding:11px 13px;border-radius:11px;font-weight:500;transition:all .15s}.side a:hover{background:var(--surf);color:var(--tx)}.side a.on{background:var(--infb);color:var(--inf)}.main{padding:6px 32px 36px}}
#login{max-width:380px;margin:16vh auto;padding:30px 28px;border:1px solid var(--line);border-radius:18px;box-shadow:0 0 0 1px rgba(0,224,138,.06),0 18px 50px rgba(0,0,0,.5)}#login h1{font-size:26px;font-weight:680;letter-spacing:-.02em}
/* ============ OpenStocks — Kite-style light ============ */
:root{
 --bg:#f5f6f8;--surf:#ffffff;--card:#ffffff;--line:#e6e8ec;--line2:#d6dae0;--tx:#3c4043;--hd:#1a1d21;--mut:#949aa4;
 --up:#3fa45b;--upb:#e9f6ed;--dn:#e34d3f;--dnb:#fdeceb;
 --inf:#4184f3;--infb:#ecf2fe;--warn:#c77d1a;--warnb:#fbf1e0;
 --acc:#4184f3;--sh:0 1px 3px rgba(16,24,40,.05)
}
html{background:#f5f6f8;color-scheme:light}
body{color:var(--tx);overflow-x:hidden;min-height:100vh;display:flow-root;background:#f5f6f8}
.app{min-width:0}.main{min-width:0}
.num,.hero,#pv{font-variant-numeric:tabular-nums;letter-spacing:-.01em}
.hero{font-weight:600;text-shadow:none;color:var(--hd)}
.sec{color:var(--mut)}
.card,.raise,.pos{background:var(--card);border:1px solid var(--line);box-shadow:var(--sh)}
.pos{transition:border-color .15s,box-shadow .15s}
.pos:hover{border-color:var(--line2);box-shadow:0 2px 8px rgba(16,24,40,.08)}
.side{background:#ffffff;border-right:1px solid var(--line)}
.side .b{color:var(--hd);letter-spacing:-.01em}
.side a{transition:background .15s,color .15s;border:none;position:relative;color:var(--mut)}
.side a:hover{background:#f2f4f7;color:var(--tx)}
.side a.on{background:var(--infb);color:var(--inf)}
.side a.on::before{content:"";position:absolute;left:-2px;top:9px;bottom:9px;width:3px;border-radius:3px;background:var(--inf)}
.nav{background:#ffffff;border-top:1px solid var(--line)}
.nav a.on{color:var(--inf)}
.top{background:rgba(245,246,248,.9);backdrop-filter:blur(10px)}
.live{background:var(--upb);color:var(--up);border:1px solid transparent}
.dot{box-shadow:none}
.seg{background:#eceef1;border:1px solid transparent}
.seg b.on{background:#ffffff;color:var(--tx);box-shadow:0 1px 2px rgba(16,24,40,.1)}
.prof{background:var(--infb);color:var(--inf);border:1px solid transparent}
.menu{background:#ffffff;border:1px solid var(--line);box-shadow:0 12px 32px rgba(16,24,40,.14)}
.badge{border:1px solid transparent;font-weight:600}
.bg-inf{background:var(--infb);color:var(--inf)}
.bg-up{background:var(--upb);color:var(--up)}
.bg-dn{background:var(--dnb);color:var(--dn)}
.bg-warn{background:var(--warnb);color:var(--warn)}
.bg-mut{background:#eceef1;color:var(--mut)}
.chip{background:#ffffff;border:1px solid var(--line);white-space:nowrap;display:inline-block;margin:2px 0}
.bar,.scorebar{background:#eceef1}
.lrow{border-bottom:1px solid var(--line)}.lrow:hover{background:#f7f8fa}
.modepill{box-shadow:none}
input,select{background:#ffffff}
button.pri{color:#fff}
#engines svg{filter:none}
/* ---- dark theme (manual toggle via html[data-theme]) ---- */
html[data-theme=dark]{color-scheme:dark;background:#0f1114}
[data-theme=dark]{
 --bg:#0f1114;--surf:#181b1f;--card:#181b1f;--line:#23262b;--line2:#2c3037;--tx:#c7ccd4;--hd:#f1f3f6;--mut:#8890a0;
 --up:#3fb26a;--upb:#12271a;--dn:#f0584a;--dnb:#2a1614;--inf:#5b8def;--infb:#14243d;--warn:#d99a3a;--warnb:#2a2008;
 --acc:#5b8def;--sh:0 1px 2px rgba(0,0,0,.45)
}
[data-theme=dark] body{background:var(--bg)}
/* dark: subtle borders (were too bright/white), readable ticker text */
[data-theme=dark] .card,[data-theme=dark] .raise,[data-theme=dark] .pos,
[data-theme=dark] input,[data-theme=dark] select,[data-theme=dark] .chip{border-color:var(--line)}
[data-theme=dark] .ticker{background:transparent;border-bottom-color:var(--line)}
[data-theme=dark] .tk b{color:var(--hd)}
[data-theme=dark] .tk .num{color:var(--tx)}
[data-theme=dark] .tk .mk{color:var(--mut);border-color:var(--line)}
[data-theme=dark] .top{background:rgba(15,17,20,.9)}
[data-theme=dark] .side,[data-theme=dark] .nav{background:var(--card)}
[data-theme=dark] .side a:hover{background:#20242b;color:var(--tx)}
[data-theme=dark] .lrow:hover{background:#1d2127}
[data-theme=dark] .seg{background:#22262c}
[data-theme=dark] .seg b.on{background:var(--line);color:var(--tx)}
[data-theme=dark] .chip,[data-theme=dark] input,[data-theme=dark] select{background:var(--card)}
[data-theme=dark] .bar,[data-theme=dark] .scorebar,[data-theme=dark] .bg-mut{background:#22262c}
[data-theme=dark] button.pri{color:#0f1114}
[data-theme=dark] .menu{background:var(--card);box-shadow:0 12px 32px rgba(0,0,0,.5)}
/* ---- theme + collapse icon buttons; collapsible sidebar ---- */
.iconbtn{width:32px;height:32px;display:inline-flex;align-items:center;justify-content:center;border-radius:9px;cursor:pointer;color:var(--mut);border:1px solid var(--line);background:var(--card)}
.iconbtn:hover{color:var(--tx);background:var(--surf)}
.iconbtn svg{width:17px;height:17px;stroke:currentColor;fill:none;stroke-width:1.9;stroke-linecap:round;stroke-linejoin:round}
.side .lbl{white-space:nowrap;overflow:hidden}
.side .b .mini{display:none}
.sidefoot{margin-top:auto;display:flex;gap:8px;padding:10px 6px 2px}
@media(min-width:860px){
 .side.collapsed{width:70px}
 .side.collapsed .lbl,.side.collapsed .b .full{display:none}
 .side.collapsed .b .mini{display:inline}
 .side.collapsed a{justify-content:center}
 .side.collapsed .sidefoot{flex-direction:column;align-items:center}
}
/* ---- design polish: calmer type weight, precise cards, real hierarchy ---- */
b,strong,.pos b,.card b,.raise b{font-weight:600}
.hero{color:var(--hd);font-weight:600;letter-spacing:-.022em;font-size:33px}
.num{color:#2b3139}
.mut{color:#8b929c}
.card,.raise,.pos{border-radius:10px;border-color:#e8eaee}
.pos{padding:14px 16px}
.bar{height:3px;background:#eef0f3;margin-top:10px}
.bar>i{border-radius:3px}
.badge{font-weight:500;font-size:10.5px;padding:2px 8px;border-radius:6px;letter-spacing:.01em}
.sec{font-size:11.5px;letter-spacing:.07em;color:#9aa1ac;font-weight:600}
.side a{font-weight:500}
.live{font-weight:500;font-size:11px}
.chip{border-radius:8px}
.seg{border-radius:9px}.seg b{border-radius:7px}
.prof{font-weight:600}
.modepill{font-weight:600}
#healthbar{background:#fef4f3!important;border:1px solid #f8d3ce!important;color:#c23b30!important;border-radius:8px!important;font-weight:500}
.ticker{background:#fff;border-bottom:1px solid var(--line)}
input,select{border-radius:9px}
button{border-radius:9px}
/* ---- Kite-style dense layout ---- */
.k-hero{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:18px 20px}
.k-herotop{display:flex;justify-content:space-between;align-items:flex-start;gap:12px}
.k-hero .hero{font-size:31px;margin:3px 0 0}
.k-lbl{font-size:10.5px;color:var(--mut);text-transform:uppercase;letter-spacing:.06em;font-weight:600}
.k-sub{font-size:13px;margin-top:4px}
.k-metrics{display:flex;gap:14px;margin-top:16px;padding-top:15px;border-top:1px solid var(--line);flex-wrap:wrap}
.k-metric{display:flex;flex-direction:column;gap:4px;flex:1;min-width:78px}
.k-metric .num{font-size:15px;font-weight:600;color:#242a31}
.k-listhead{display:flex;justify-content:space-between;align-items:baseline;font-size:11px;letter-spacing:.07em;text-transform:uppercase;color:#9aa1ac;font-weight:600;margin:22px 3px 8px}
.k-list{background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden}
.prow{display:flex;justify-content:space-between;align-items:center;gap:12px;padding:13px 16px;border-bottom:1px solid var(--line);cursor:pointer;transition:background .12s}
.prow:last-child{border-bottom:none}
.prow:hover{background:#f8f9fb}
.prow-l{min-width:0}
.prow-sym{font-size:14.5px;font-weight:600;color:#242a31;display:flex;align-items:center}
.prow-sub{font-size:12px;color:var(--mut);margin-top:4px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
@media(max-width:859px){.prow-sub{white-space:normal;overflow:visible;text-overflow:clip;line-height:1.45}}   /* mobile: wrap the qty·avg·exit line instead of truncating it mid-word */
.prow-r{text-align:right;white-space:nowrap;flex-shrink:0}
.prow-ltp{font-size:14px;font-weight:600;color:#242a31}
.prow-pnl{font-size:12.5px;margin-top:4px;font-weight:600}
.prow .exitbtn{font-size:11px;padding:5px 12px;margin-left:12px}
.home-main #homepos,.home-main #activity{display:block!important}
/* ---- retail hero: bold, colorful, AI-forward ---- */
.hp-hero{position:relative;background:var(--card);border:1px solid var(--line);border-radius:16px;padding:20px 22px 14px}
.hp-glow{display:none}
.hp-herotop{position:relative;display:flex;justify-content:space-between;align-items:flex-start;gap:12px}
.hp-hlbl{font-size:12px;color:var(--mut);font-weight:500}
.hp-tag{font-size:9.5px;text-transform:uppercase;letter-spacing:.06em;background:var(--warnb);color:var(--warn);padding:2px 7px;border-radius:6px;margin-left:6px;vertical-align:middle;font-weight:600}
.hp-hero .hero{color:var(--hd)!important;font-size:38px;font-weight:700;margin:6px 0 4px;text-shadow:none}
.hp-hchg{font-size:14px;font-weight:600}
.hp-ailive{display:inline-flex;align-items:center;gap:7px;background:var(--upb);color:var(--up);font-size:11px;font-weight:600;padding:5px 11px;border-radius:20px;white-space:nowrap}
.hp-pulse{width:7px;height:7px;border-radius:50%;background:var(--up);box-shadow:0 0 0 0 rgba(63,164,91,.5);animation:hppulse 2s infinite}
@keyframes hppulse{0%{box-shadow:0 0 0 0 rgba(63,164,91,.45)}70%{box-shadow:0 0 0 7px rgba(63,164,91,0)}100%{box-shadow:0 0 0 0 rgba(63,164,91,0)}}
.hp-chartwrap{position:relative;margin:16px -6px 0}
.hp-chart svg{width:100%;height:176px;display:block}
.hp-ranges{position:relative;display:flex;gap:4px;margin-top:10px}
.hp-ranges b{font-size:12px;font-weight:600;color:var(--mut);padding:5px 13px;border-radius:8px;cursor:pointer;transition:all .15s}
.hp-ranges b.on{background:var(--infb);color:var(--inf)}
.hp-stats{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-top:14px}
.hp-stat{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:12px 13px}
.hp-slbl{font-size:11px;color:var(--mut);font-weight:500}
.hp-sval{font-size:16px;font-weight:700;color:#242a31;margin-top:6px}
.hp-sval .up,.hp-sval .dn{font-weight:700}
.hp-sechead{display:flex;justify-content:space-between;align-items:baseline;font-size:15px;font-weight:700;color:#1c2128;margin:22px 3px 10px}
.hp-sechead .mut{font-size:12px;font-weight:500}
@media(max-width:560px){.hp-stats{grid-template-columns:1fr 1fr}.hp-hero .hero{font-size:33px}}
/* ---- AI report feed ---- */
.fd-greet{padding:8px 2px 2px}
.fd-hi{font-size:23px;font-weight:700;color:var(--hd);letter-spacing:-.02em}
.fd-sub{font-size:14px;color:var(--mut);margin-top:4px}
#homefeed .fd-card{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:16px 18px;margin-top:12px}
.fd-hd{display:flex;gap:12px;align-items:center}
.fd-dot{width:34px;height:34px;border-radius:11px;display:flex;align-items:center;justify-content:center;font-size:14px;flex-shrink:0;font-weight:700}
.fd-title{font-size:15px;font-weight:600;color:var(--hd)}
.fd-meta{font-size:12px;color:var(--mut);margin-top:2px}
.indexbar{display:flex;gap:20px;flex-wrap:wrap;align-items:center;padding:9px 4px;border-bottom:1px solid var(--line);margin:2px 0 8px}
.idx{display:flex;align-items:baseline;gap:7px;font-size:13px;white-space:nowrap}
.idx b{font-weight:600;color:var(--hd)}
.idx .iv{font-variant-numeric:tabular-nums;color:var(--tx)}
.idx .ic{font-size:12px;font-weight:600}
/* Kite-style clean watchlist row */
.wlrow{display:flex;align-items:center;gap:10px;padding:12px 2px;border-bottom:1px solid var(--line);cursor:pointer;transition:background .12s}
.wlrow:last-child{border-bottom:none}
.wlrow:hover{background:var(--surf)}
.wlrow .wlL{flex:1;min-width:0}
.wlrow .wlsym{font-size:15px;font-weight:600;color:var(--hd);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.wlrow .wlex{font-size:10px;color:var(--mut);margin-top:2px;letter-spacing:.04em}
.wlrow .wlR{text-align:right;white-space:nowrap}
.wlrow .wlltp{font-size:14.5px;font-variant-numeric:tabular-nums;color:var(--tx)}
.wlrow .wlch{font-size:12px;font-weight:600;margin-top:2px}
.wlrow .wldel{color:var(--mut);opacity:.3;font-size:19px;line-height:1;padding:4px 4px 4px 10px;transition:opacity .12s}
.wlrow:hover .wldel{opacity:.75}
@media(hover:none),(max-width:859px){.wlrow .wldel{opacity:.55}}   /* touch / mobile has no hover — keep the remove × tappable & visible */
.fd-tabs{display:flex;gap:4px;margin-left:auto}
.fd-tab{font-size:10.5px;font-weight:700;letter-spacing:.4px;color:var(--mut);padding:4px 9px;border-radius:7px;cursor:pointer;background:var(--surf)}
.fd-tab.on{background:var(--infb);color:var(--inf)}
.fd-big{font-size:33px;font-weight:700;color:var(--hd);margin:14px 0 3px;letter-spacing:-.02em;font-variant-numeric:tabular-nums}
.fd-chg{font-size:14px;font-weight:600}
.fd-chart{margin:14px -6px 0}.fd-chart svg{width:100%;height:150px;display:block}
.fd-text{font-size:13.5px;color:var(--tx);line-height:1.6;margin-top:12px}
.fd-text b{font-weight:600}
.fd-scored{display:grid;grid-template-columns:repeat(3,1fr);gap:14px 10px;margin-top:15px}
.fd-sn{font-size:19px;font-weight:700;color:var(--hd);font-variant-numeric:tabular-nums}
.fd-sn.up{color:var(--up)}.fd-sn.dn{color:var(--dn)}
.fd-sl{font-size:11px;color:var(--mut);margin-top:3px}
.fd-trades{margin-top:12px}
.fd-movegrid{margin-top:14px}
.fd-movecol+.fd-movecol{margin-top:20px}
.fd-movelbl{font-size:10.5px;color:var(--mut);text-transform:uppercase;letter-spacing:.06em;font-weight:600;margin-bottom:2px;padding-bottom:8px;border-bottom:1px solid var(--line)}
.fd-movecol .fd-trade:last-child{border-bottom:none}
@media(min-width:1080px){.fd-movegrid{display:grid;grid-template-columns:1fr 1fr;gap:0 30px;align-items:start}.fd-movecol+.fd-movecol{margin-top:0}}
.fd-trade{display:flex;gap:9px;align-items:center;padding:9px 0;border-bottom:1px solid var(--line)}
.fd-trade:last-child{border-bottom:none}
.fd-holds{margin-top:12px;border:1px solid var(--line);border-radius:10px;overflow:hidden}
@media(max-width:560px){.fd-scored{grid-template-columns:repeat(2,1fr);gap:15px 10px}}
@media(min-width:1080px){
 #homefeed{display:grid;grid-template-columns:1fr 1fr;gap:14px 16px;align-items:start}
 #homefeed .fd-card{margin-top:0;min-width:0}   /* min-width:0 => grid tracks stay exactly equal regardless of content */
 .heatmap{display:grid;grid-template-columns:1fr 1fr;gap:6px;margin-bottom:4px}
 .htile{border-radius:9px;padding:8px 10px;min-width:0;cursor:default;border:1px solid var(--line)}
 .htile .hs{font-size:11.5px;font-weight:600;color:var(--hd);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
 .htile .hc{font-size:13px;font-weight:700;font-variant-numeric:tabular-nums;margin-top:1px}
 .htile .hn{font-size:9.5px;color:var(--mut)}
 #homefeed>#fdPerf,#homefeed>#fdTrades,#homefeed>#fdHold{grid-column:1 / -1}
 #homefeed>div:empty{display:none}
 /* make the list CONTAINERS full-width (override old per-card grid/width rules) */
 #poslist{display:block!important}
 #ordlist{max-width:none!important;column-count:auto!important}
 /* then flow the ROWS into 2 columns (grid is robust for flex rows) */
 #fdHold .fd-holds,#poslist>.k-list{display:grid!important;grid-template-columns:1fr 1fr;align-items:start;gap:0}
 #fdHold .fd-holds>.prow,#poslist>.k-list>.prow{min-width:0}
 #ordlist .ordgrid{grid-template-columns:1fr 1fr}
 /* rail: stack movers full-width (readable names) + keep it in view while scrolling */
 #movers{grid-template-columns:1fr}
 .home-rail{position:sticky;top:14px;align-self:start}
 /* portfolio: strategy cards + equity curve stretch to fill (were half-width) */
 #attrib{grid-template-columns:repeat(auto-fit,minmax(320px,1fr))!important}
 #eqcurves{grid-template-columns:1fr!important}
}
.mvrow>b{white-space:nowrap}
.tgopt{display:flex;align-items:center;gap:10px;font-size:13.5px;padding:7px 0;cursor:pointer}
.tgopt input{width:auto;margin:0;accent-color:var(--inf)}
.ordbar{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin:0 2px 14px}
#ordcustom{display:inline-flex;gap:8px;align-items:center;flex-wrap:wrap}
input[type=date]{width:auto;padding:8px 11px}
.ordgrid{display:grid;gap:16px}
.mobonly{display:none}
@media(max-width:859px){.mobonly{display:inline-flex}
 #ordlist.os-buy .oc-sell{display:none}#ordlist.os-sell .oc-buy{display:none}}
.ordlbl{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:#9aa1ac;font-weight:600;margin:0 2px 8px}
.ordcol+.ordcol{margin-top:20px}
@media(min-width:1080px){.ordcol+.ordcol{margin-top:0}}
.fd-trade{gap:10px}
.fd-trade .fd-tsym{flex:1;min-width:0;display:flex;gap:8px;align-items:baseline;overflow:hidden}
.fd-trade .fd-tsym .mut{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.rgb{font-size:11px;padding:3px 10px;border:1px solid var(--line);border-radius:7px;cursor:pointer;color:var(--mut);font-weight:500}
.rgb.on{background:var(--infb);color:var(--inf);border-color:rgba(56,189,248,.3)}
.detail-grid{display:flex;flex-direction:column;gap:0}
.detail-main>*+*,.detail-side>*+*{margin-top:0}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:9px}
button{background:rgba(255,255,255,.045);border:1px solid var(--line);color:var(--tx)}
button:hover{background:rgba(255,255,255,.09)}
button.pri{background:var(--acc);color:#fff;border-color:var(--acc);font-weight:600;box-shadow:none}
button.pri:hover{background:#356fd6;border-color:#356fd6}
input,select{background:#ffffff;border:1px solid var(--line);color:var(--tx)}
input::placeholder{color:#aab0b9}
input:focus,select:focus{border-color:var(--inf);box-shadow:0 0 0 3px var(--infb)}
.sec{color:var(--mut)}
#login{background:#fff;border:1px solid var(--line);box-shadow:0 2px 14px rgba(16,24,40,.06)}
.skel{color:var(--mut)}
.toastmsg{position:fixed;bottom:80px;left:50%;transform:translateX(-50%);background:var(--card);border:1px solid var(--line);box-shadow:0 8px 28px rgba(16,24,40,.18);padding:11px 20px;border-radius:12px;z-index:99;font-size:13px;animation:fade .3s}
.ticker{overflow:hidden;white-space:nowrap;align-items:center;height:34px;margin:0 -16px 4px;padding:0;border-bottom:1px solid var(--line);background:linear-gradient(180deg,rgba(255,255,255,.03),transparent)}
.ticker .track{display:inline-flex;gap:24px;padding-left:16px;animation:tick 70s linear infinite;will-change:transform}
.ticker:hover .track{animation-play-state:paused}
.tk{display:inline-flex;gap:6px;align-items:center;font-size:12px;font-variant-numeric:tabular-nums;cursor:pointer}
.tk b{font-weight:600;letter-spacing:.01em}.tk .mk{font-size:9px;color:var(--mut);border:1px solid var(--line);border-radius:4px;padding:0 3px}
@keyframes tick{from{transform:translateX(0)}to{transform:translateX(-50%)}}
/* ============ responsive layout: real desktop dashboard ============ */
#engines.grid{grid-template-columns:1fr}
.engwrap{display:grid;grid-template-columns:minmax(0,190px) minmax(0,1fr);gap:24px;align-items:center;margin-top:12px}
.engchart svg{height:60px!important;margin-top:0!important}
.engstats{display:flex;gap:28px;flex-wrap:wrap;margin-top:12px;border-top:1px solid var(--line);padding-top:11px}
@media(max-width:640px){.engwrap{grid-template-columns:1fr;gap:12px}}
@media(min-width:860px){
 .main{max-width:1280px;padding:12px 40px 52px}
 .ticker{margin:0 -40px 8px;height:36px}
 .top{padding:18px 2px 12px}
 .hero{font-size:40px}
 #engines{gap:16px}
 #engines .card{padding:17px 19px}
 #homepos,#poslist{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px;align-items:start}
 #homepos>.pos,#poslist>.pos{margin-bottom:0}
 #ordlist{column-count:1;max-width:860px}
 #ordlist .lrow:last-child{border-bottom:none}
 #account{max-width:860px}
 #analyze,#detail{max-width:1280px}
 .detail-grid{display:grid;grid-template-columns:minmax(0,1.7fr) minmax(0,1fr);gap:22px;align-items:start}
 .detail-main,.detail-side{min-width:0}
 .detail-side{position:sticky;top:70px}
 .sec{margin-top:28px}
}
@media(min-width:1400px){
 .main{max-width:1520px}
 #poslist{grid-template-columns:repeat(3,minmax(0,1fr))}
}
@media(min-width:1080px){
 .home-grid{display:grid;grid-template-columns:minmax(0,1.6fr) minmax(0,1fr);gap:34px;align-items:start}
 .home-main{min-width:0}.home-rail{min-width:0}
 .home-rail>.sec:first-child{margin-top:6px}
 .home-main #homepos{grid-template-columns:repeat(2,minmax(0,1fr))}
}
#wl .lrow{padding:11px 2px}#wl .lrow:last-child{border-bottom:none}
.mvrow:last-child{border-bottom:none!important}
.pill{display:inline-block;min-width:66px;text-align:center;font-size:11px;font-weight:600;padding:3px 7px;border-radius:7px;font-variant-numeric:tabular-nums}
.pup{background:var(--upb);color:var(--up)}.pdn{background:var(--dnb);color:var(--dn)}.pmut{background:#eceef1;color:var(--mut)}
.icb{padding:5px 8px;font-size:11px;line-height:1;border-radius:7px;color:var(--mut)}
.icb:hover{color:var(--tx)}
.icb svg{width:13px;height:13px;stroke:currentColor;fill:none;stroke-width:2;stroke-linecap:round;display:block}
@media(max-width:859px){
 .hero{font-size:28px;line-height:1.18}
 .main{padding:0 14px 94px}
 #engines{gap:9px}
}
</style></head><body>

<div id=login class=hide><h1>OpenStocks<span style="color:var(--up)">.</span></h1><p class=mut style="margin:0 0 22px">AI trading desk · sign in</p>
 <div class=field><label>username</label><input id=u autocomplete=username></div>
 <div class=field><label>password</label><input id=pw type=password autocomplete=current-password></div>
 <button class=pri style="width:100%;margin-top:8px" onclick=doLogin()>Sign in</button>
 <div id=lerr class=dn style="font-size:13px;margin-top:10px"></div>
 <p class=mut style="font-size:13px;margin-top:18px">No account? <b style="cursor:pointer;color:var(--inf)" onclick="alert('Ask an admin to create your account — sign-up with approval is coming next.')">Request access</b></p></div>

<div id=app class="app hide">
 <nav class=side id=side><div class=b><span class=full>OpenStocks<span style="color:var(--inf)">.</span></span><span class=mini>O<span style="color:var(--inf)">.</span></span></div>
  <a data-t=home onclick="go('home')"><svg viewBox="0 0 24 24"><path d="M3 11l9-8 9 8M5 10v10h14V10"/></svg><span class=lbl>Home</span></a>
  <a data-t=positions onclick="go('positions')"><svg viewBox="0 0 24 24"><rect x=3 y=6 width=18 height=13 rx=2/><path d="M3 10h18"/></svg><span class=lbl>Portfolio</span></a>
  <a data-t=orders onclick="go('orders')"><svg viewBox="0 0 24 24"><path d="M4 6h16M4 12h16M4 18h10"/></svg><span class=lbl>Orders</span></a>
  <a data-t=analyze onclick="go('analyze')"><svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><path d="M21 21l-4-4"/></svg><span class=lbl>Analyze</span></a>
  <a data-t=account onclick="go('account')"><svg viewBox="0 0 24 24"><circle cx="12" cy="8" r="4"/><path d="M4 21c0-4 4-6 8-6s8 2 8 6"/></svg><span class=lbl>Account</span></a>
  <div class=sidefoot>
   <div class=iconbtn onclick=toggleTheme() title="Light / dark"><svg id=themeicon viewBox="0 0 24 24"></svg></div>
   <div class=iconbtn onclick=toggleSide() title="Collapse sidebar"><svg viewBox="0 0 24 24"><path d="M15 6l-6 6 6 6"/></svg></div>
  </div>
 </nav>
 <div class=main>
  <div class=ticker id=ticker style="display:none"></div>
  <div id=healthbar class=hide style="background:var(--dnb);border:1px solid rgba(255,93,108,.45);color:var(--dn);padding:8px 14px;border-radius:10px;margin:8px 0;font-size:12px"></div>
  <div class=top><span style="display:flex;align-items:center;gap:9px"><span id=backbtn class=iconbtn style="display:none" onclick="goBack()" title="Back"><svg viewBox="0 0 24 24"><path d="M15 6l-6 6 6 6"/></svg></span><span class=live><span class=dot></span><span id=clock>live</span></span></span>
   <div style="display:flex;align-items:center;gap:10px"><div class=seg id=mkt><b data-m=IN class=on>India</b></div>
    <div class=iconbtn onclick=toggleTheme() title="Light / dark"><svg id=themeicon2 viewBox="0 0 24 24"></svg></div>
    <div class=prof id=avatar onclick="document.getElementById('pm').classList.toggle('hide')">U</div></div></div>
  <div id=pm class="menu hide">
   <a onclick="go('account');document.getElementById('pm').classList.add('hide')">Account &amp; settings</a>
   <a onclick=doLogout()>Log out</a></div>
  <div id=indexbar class=indexbar style="display:none"></div>

  <div id=home class="tab on"><div class=home-grid>
   <div class=home-main>
    <div class=fd-greet>
     <div style="display:flex;justify-content:space-between;align-items:center"><div class=fd-hi id=fd-hi>OpenStocks desk</div><span class=hp-ailive><span class=hp-pulse></span> live</span></div>
     <div class=fd-sub id=fd-sub>&nbsp;</div>
    </div>
    <div id=homefeed></div>
   </div>
   <div class=home-rail>
    <div class=sec><span>watchlist</span><span style="position:relative"><input id=wlq placeholder="+ add symbol" style="width:150px;padding:6px 11px;font-size:12px" oninput="wlSearch()" autocomplete=off><div id=wlsug class="menu hide" style="position:absolute;right:0;top:36px;min-width:250px;z-index:30"></div></span></div>
    <div class=card id=wl style="padding:4px 14px"></div>
    <div style="margin-top:8px"><span class=mut style="font-size:11px">alerts&nbsp;</span><span id=alerts></span></div>
    <div class=sec><span>catalysts today</span><span class=mut style="font-size:12px;font-weight:400">live NSE filings</span></div>
    <div class=card id=catalysts style="padding:6px 14px;font-size:12px">—</div>
    <div class=sec><span>movers</span><span class=mut style="font-size:12px;font-weight:400">vs prev close</span></div>
    <div class=grid2 id=movers></div>
    <div class=sec><span>engine radar</span><span class=mut style="font-size:12px;font-weight:400">may buy next</span></div>
    <div id=radar class=mut style="font-size:12px">quiet</div>
   </div>
  </div></div>

  <div id=watch class=tab>
   <div class=sec><span>my watchlist</span></div>
   <div style="position:relative;margin-bottom:12px"><input id=wlq2 placeholder="+ add a symbol (e.g. RELIANCE)" oninput="wlSearch('2')" autocomplete=off><div id=wlsug2 class="menu hide" style="position:absolute;left:0;right:0;top:46px;z-index:30"></div></div>
   <div class=card id=wl2 style="padding:4px 14px"></div>
   <div style="margin-top:10px"><span class=mut style="font-size:11px">alerts&nbsp;</span><span id=alerts2></span></div>
   <div class=sec style="margin-top:22px"><span>engine radar</span><span class=mut style="font-size:12px;font-weight:400">may buy next</span></div>
   <div id=watchlist class=skel>loading…</div></div>

  <div id=positions class=tab>
   <div class=sec><span>portfolio</span><span id=postot style="font-size:13px"></span></div>
   <div class=seg style="margin-bottom:10px"><b id=sbpos class=on onclick="subPos('pos')">Positions · today</b><b id=sbhold onclick="subPos('hold')">Holdings</b></div>
   <div id=poslist class=skel>loading…</div>
   <div class=sec><span>allocation &amp; risk</span><span id=pfnote class=mut style="font-size:12px;font-weight:400"></span></div>
   <div id=pfrisk class=skel>loading…</div>
   <div class=sec><span>strategy performance</span><span class=mut style="font-size:12px;font-weight:400">realized + open</span></div>
   <div class=grid id=attrib></div>
   <div class=sec><span>equity curve</span><span id=eqdd class=mut style="font-size:12px;font-weight:400"></span></div>
   <div class=grid2 id=eqcurves></div></div>

  <div id=orders class=tab>
   <div class=sec><span>orders &amp; activity</span><span id=ordtot class=mut style="font-size:12px;font-weight:400"></span></div>
   <div class=ordbar>
    <div class=seg id=ordrange><b data-d=1 class=on onclick="setOrdRange(this)">Today</b><b data-d=3 onclick="setOrdRange(this)">3 days</b><b data-d=7 onclick="setOrdRange(this)">7 days</b><b data-d=custom onclick="setOrdRange(this)">Custom</b></div>
    <div id=ordcustom class=hide><input type=date id=ordfrom> <span class=mut style="font-size:12px">to</span> <input type=date id=ordto> <button class=sm onclick=applyOrdCustom()>Apply</button></div>
   </div>
   <div class="seg mobonly" id=ordside style="margin-bottom:10px"><b class=on onclick="setOrdSide('buy')">Bought</b><b onclick="setOrdSide('sell')">Sold</b></div>
   <div id=ordlist class=os-buy><div class=skel>loading…</div></div></div>

  <div id=analyze class=tab>
   <div class=sec>analyse a stock</div>
   <div style="display:flex;gap:8px;margin-bottom:8px"><input id=qsym placeholder="symbol e.g. RELIANCE / AAPL" style="flex:1" onkeydown="if(event.key=='Enter')doAnalyze()">
    <select id=qmkt style="width:88px"><option value=IN>India</option></select>
    <button class=pri onclick=doAnalyze()>Go</button></div>
   <div id=ares></div></div>

  <div id=account class=tab></div>
  <div id=detail class=tab></div>
 </div>
</div>

<nav class=nav>
<a data-t=home class=on onclick="go('home')"><svg viewBox="0 0 24 24"><path d="M3 11l9-8 9 8"/><path d="M5 10v10h5v-6h4v6h5V10"/></svg>home</a>
<a data-t=watch onclick="go('watch')"><svg viewBox="0 0 24 24"><path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7-10-7-10-7z"/><circle cx=12 cy=12 r=3/></svg>watchlist</a>
<a data-t=orders onclick="go('orders')"><svg viewBox="0 0 24 24"><path d="M4 6h16M4 12h16M4 18h10"/></svg>orders</a>
<a data-t=positions onclick="go('positions')"><svg viewBox="0 0 24 24"><rect x=3 y=6 width=18 height=13 rx=2/><path d="M3 10h18"/></svg>portfolio</a>
<a data-t=analyze onclick="go('analyze')"><svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><path d="M21 21l-4-4"/></svg>analyse</a>
<a data-t=account onclick="go('account')"><svg viewBox="0 0 24 24"><circle cx="12" cy="8" r="4"/><path d="M4 21c0-4 4-6 8-6s8 2 8 6"/></svg>account</a>
</nav>

<script>
var INR=new Intl.NumberFormat('en-IN'),USD=new Intl.NumberFormat('en-US'),cur='home',MKT='IN',ME=null,MODE='paper',ACC=null;
var _SUN='<circle cx="12" cy="12" r="4.3"/><path d="M12 2.5v2.2M12 19.3v2.2M4.2 12H2M22 12h-2.2M5.2 5.2l1.5 1.5M17.3 17.3l1.5 1.5M18.8 5.2l-1.5 1.5M6.7 17.3l-1.5 1.5"/>';
var _MOON='<path d="M20 14.8A8 8 0 1 1 9.2 4a6.4 6.4 0 0 0 10.8 10.8z"/>';
function applyTheme(t){document.documentElement.dataset.theme=t;var ic=(t=='dark')?_SUN:_MOON;['themeicon','themeicon2'].forEach(function(id){var e=document.getElementById(id);if(e)e.innerHTML=ic;});}
function toggleTheme(){var t=(document.documentElement.dataset.theme=='dark')?'light':'dark';try{localStorage.setItem('os_theme',t)}catch(e){}applyTheme(t);}
function toggleSide(){var s=document.getElementById('side');if(!s)return;s.classList.toggle('collapsed');try{localStorage.setItem('os_side',s.classList.contains('collapsed')?'1':'0')}catch(e){}}
(function(){var t;try{t=localStorage.getItem('os_theme')}catch(e){}if(!t)t=(window.matchMedia&&matchMedia('(prefers-color-scheme:dark)').matches)?'dark':'light';applyTheme(t);try{if(localStorage.getItem('os_side')=='1'){var s=document.getElementById('side');if(s)s.classList.add('collapsed');}}catch(e){}})();
function sgn(x){return (x>0?'+':'')+x}function col(x){return x>0?'up':(x<0?'dn':'mut')}
function show(i){document.getElementById(i).classList.remove('hide')}function hide(i){document.getElementById(i).classList.add('hide')}
function api(u,o){return fetch(u,Object.assign({headers:{'Content-Type':'application/json'}},o||{})).then(r=>r.json().catch(()=>({})).then(j=>({ok:r.ok,j:j})))}
function setMkt(m){MKT=m;document.querySelectorAll('#mkt b').forEach(b=>b.classList.toggle('on',b.dataset.m==m));refresh()}
var NAVHIST=[];
function go(t,noPush){if(!noPush&&cur&&cur!=t)NAVHIST.push(cur);cur=t;
 ['home','watch','positions','orders','analyze','account','detail'].forEach(x=>{var e=document.getElementById(x);if(e)e.classList.toggle('on',x==t)});
 document.querySelectorAll('.nav a,.side a').forEach(a=>a.classList.toggle('on',a.dataset.t==t));
 var bk=document.getElementById('backbtn');if(bk)bk.style.display=NAVHIST.length?'inline-flex':'none';
 if(t=='home'){loadHome();loadWL();loadMovers();loadRadar();loadActivity();loadCatalysts();}if(t=='watch'){loadWL();loadWatch();}if(t=='positions')loadPos();if(t=='orders')loadOrders();if(t=='account')loadAccount();window.scrollTo(0,0)}
function goBack(){var p=NAVHIST.pop();go(p||'home',true)}
function inMkt(m){return MKT=='BOTH'||m==MKT}
function boot(){api('/api/auth/me').then(r=>{var u=r.j.user||r.j;if(r.ok&&u&&u.username){ME=u;MODE=(u.signal_execution_mode||'paper');hide('login');show('app');document.getElementById('avatar').textContent=(u.username[0]||'U').toUpperCase();loadAccountData();refresh();startStream();loadTicker();NAVHIST=[];go('home',true);}else{show('login');hide('app');}}).catch(()=>{show('login');hide('app')})}
function loadTicker(){api('/v2/api/ticker').then(r=>{var it=(r.j||[]);var el=document.getElementById('ticker');if(!el)return;if(!it.length){el.style.display='none';return;}
 el.style.display='flex';var h=it.map(t=>{var pnl='';if(t.pnl!=null){var a=t.pnl>0?'▲':(t.pnl<0?'▼':''),cl=t.pnl>0?'up':(t.pnl<0?'dn':'mut');pnl='<span class="'+cl+'">'+a+Math.abs(t.pnl).toFixed(2)+'%</span>';}return '<span class=tk onclick="stock(\''+t.symbol+'\',\''+t.market+'\')"><b>'+t.symbol+'</b><span class=mk>'+t.market+'</span><span class=num>'+t.ccy+(t.ccy=='₹'?INR:USD).format(t.price)+'</span>'+pnl+'</span>'}).join('');
 el.innerHTML='<div class=track>'+h+h+'</div>';}).catch(()=>{});}
var ES=null;
function startStream(){if(ES)return;try{ES=new EventSource('/v2/api/stream');ES.onmessage=function(e){try{applyStream(JSON.parse(e.data||'{}'))}catch(x){}};ES.onerror=function(){};}catch(e){}}
function applyStream(d){if(!d||!d.markets)return;document.getElementById('clock').textContent=d.as_of||document.getElementById('clock').textContent;
 (d.alerts_fired||[]).forEach(a=>{toast('\ud83d\udd14 '+a.symbol+' hit '+a.price+' ('+a.kind+' '+a.value+')');});
 if(cur=='home'){var ms=d.markets.filter(m=>inMkt(m.market));var pv=document.getElementById('pv');if(pv)pv.innerHTML=ms.map(m=>fmtc(m.ccy,m.equity)).join('  ·  ')||'—';var pp=document.getElementById('ppnl');if(pp)pp.innerHTML=ms.map(m=>'today '+pnlS(m.ccy,m.today_pnl,m.today_pct)).join(' &nbsp;·&nbsp; ');}
 (d.positions||[]).forEach(p=>{var el=document.getElementById('px_'+p.id);if(el){var old=parseFloat(el.getAttribute('data-v'));el.textContent=p.ccy+p.live;if(old&&old!=p.live){el.style.color=(p.live>old?'var(--up)':'var(--dn)');setTimeout(function(){el.style.color=''},450)}el.setAttribute('data-v',p.live)}
  var pe=document.getElementById('pl_'+p.id);if(pe){pe.firstChild&&(pe.textContent=sgn(p.pnl)+'% · '+(p.pnl_amt<0?'-':'+')+p.ccy+(p.ccy=='₹'?INR:USD).format(Math.abs(p.pnl_amt)));pe.className=col(p.pnl);pe.style.fontSize='12px';}});}
function doLogin(){document.getElementById('lerr').textContent='';api('/api/auth/login',{method:'POST',body:JSON.stringify({username:document.getElementById('u').value,password:document.getElementById('pw').value})}).then(r=>{if(r.ok&&!r.j.detail){boot()}else{document.getElementById('lerr').textContent=r.j.detail||'Login failed'}})}
function doLogout(){api('/api/auth/logout',{method:'POST'}).then(()=>{ME=null;show('login');hide('app')})}
function loadAccountData(){api('/api/account').then(r=>{ACC=r.j;renderBalance()})}
function balOf(){try{var p=ACC&&(ACC.paper||{});var cb=p.cash_by_market||ACC.paper_cash_by_market||{};var IN=cb.IN!=null?cb.IN:(p.india_cash||0),US=cb.US!=null?cb.US:(p.us_cash||0);return {IN:+IN||0,US:+US||0}}catch(e){return{IN:0,US:0}}}
function renderBalance(){var mb=document.getElementById('modeb');if(mb){mb.textContent=MODE;mb.className='modepill '+(MODE=='live'?'bg-inf':'bg-warn');}}
function refresh(){renderBalance();loadHealth();loadIndices();if(cur=='home'){loadHome();loadWL();loadMovers();loadRadar();loadActivity();loadCatalysts()}if(cur=='positions')loadPos();if(cur=='orders')loadOrders()}
function engCard(e){return `<div class=card><div class=row><span class=mut style="font-size:12px">${e.market} · ${e.strategy.indexOf('gap')>=0?'gap':'swing'}</span><span class="${col(e.ret)}" style="font-size:13px">${sgn(e.ret)}%</span></div><div class=mut style="font-size:11px;margin-top:3px">win ${e.win}% · PF ${e.pf} · ${e.positions} pos</div></div>`}
function stratTag(st){return st.indexOf('gap')>=0?['gap','bg-inf']:(st.indexOf('breakout')>=0?['breakout','bg-up']:['swing','bg-mut'])}
function whyLine(p){if(!p.why)return '';var w=p.why,f=w.factors||{};var bits=(w.reasons||[]).slice(0,2);
 if(!bits.length)bits=['RS '+(f.rel_strength||'-')+' · vol '+(f.volume||'-')];
 return '<div class=mut style="font-size:10px;margin-top:3px" title="entry investigation">why: comp '+(w.composite||'-')+' · '+bits.join(' · ')+'</div>'}
function posCard(p){var c=p.headroom>40?'var(--up)':(p.headroom>15?'var(--warn)':'var(--dn)');var s=p.market=='IN'?'₹':'$';var amt=(p.market=='IN'?'₹':'$')+(p.market=='IN'?INR:USD).format(Math.abs(p.pnl_amt));var st=stratTag(p.strategy);
 return `<div class=pos><div class=row><div style="display:flex;gap:8px;align-items:center;cursor:pointer" onclick="stock('${p.symbol}','${p.market}')"><b>${p.symbol}</b><span class="badge ${st[1]}">${st[0]}</span></div>
 <div style="text-align:right"><div class=num id=px_${p.id} data-v="${p.live}">${s}${p.live}</div><div id=pl_${p.id} style="font-size:12px" class="${col(p.pnl)}">${sgn(p.pnl)}% · ${p.pnl_amt<0?'-':'+'}${amt}</div></div></div>
 <div class=bar><i style="width:${p.headroom}%;background:${c}"></i></div>
 <div style="display:flex;gap:18px;margin-top:9px;align-items:flex-end"><span style="flex:1;display:flex;gap:18px">
  <span><span class=mut style="font-size:9px;text-transform:uppercase;letter-spacing:.05em">entry</span><br><b class=num style="font-size:12.5px">${s}${p.entry}</b></span>
  <span><span class=mut style="font-size:9px;text-transform:uppercase;letter-spacing:.05em">qty</span><br><b class=num style="font-size:12.5px">${p.qty}</b></span>
  <span><span class=mut style="font-size:9px;text-transform:uppercase;letter-spacing:.05em">value</span><br><b class=num style="font-size:12.5px">${s}${(p.market=='IN'?INR:USD).format(p.value)}</b></span>
  <span><span class=mut style="font-size:9px;text-transform:uppercase;letter-spacing:.05em">exit ${p.trail?'(trail)':'(stop)'}</span><br><b class=num style="font-size:12.5px;color:var(--dn)">${s}${p.stop}</b></span></span>
  <button class="sm" onclick="exitPos(${p.id},'${p.symbol}')">Exit</button></div>
 <div class=mut style="font-size:10px;margin-top:5px">since ${p.since||''}</div>${whyLine(p)}</div>`}
function ordRow(o){var s=o.ccy,fmt=(o.ccy=='₹'?INR:USD);
 var right=(o.side=='SELL'&&o.pnl!=null)?('<span style="display:inline-flex;gap:10px;align-items:center"><span class=mut style="font-size:11px">'+(o.pnl_amt<0?'-':'+')+s+fmt.format(Math.abs(o.pnl_amt))+'</span>'+pill(o.pnl)+'</span>'):('<span class="num mut" style="font-size:12.5px">'+s+fmt.format(o.value)+'</span>');
 var tag=o.status=='open'?'<span class="badge bg-warn">open</span>':(o.reason?'<span class=mut style="font-size:10px;border:1px solid var(--line);border-radius:5px;padding:1px 6px">'+o.reason+'</span>':'');
 return '<div class=lrow style="display:grid;grid-template-columns:auto minmax(0,1fr) auto;gap:11px;align-items:center;cursor:pointer" onclick="stock(\''+o.symbol+'\',\''+o.market+'\')"><span class="badge '+(o.side=='BUY'?'bg-inf':'bg-mut')+'">'+o.side+'</span><span style="min-width:0"><b>'+o.symbol+'</b> '+tag+'<span class="mut num" style="display:block;font-size:11px;margin-top:2px">'+o.qty+' @ '+s+o.price+' · '+o.when+'</span></span>'+right+'</div>';}
function fmtc(ccy,n){return ccy+(ccy=='₹'?INR:USD).format(Math.round(n))}
function pnlS(ccy,v,p){return '<span class="'+col(v)+'">'+(v<0?'-':'+')+fmtc(ccy,Math.abs(v))+' ('+sgn(p)+'%)</span>'}
function mktCard(m){var nm=m.market=='IN'?'India · NSE':'US · equities';
 var stat=function(lab,val){return '<div><div class=mut style="font-size:10px;text-transform:uppercase;letter-spacing:.04em">'+lab+'</div><div class=num style="font-size:14px;font-weight:600;margin-top:2px">'+val+'</div></div>'};
 var extra=(m.win!=null?stat('win rate',m.win+'%')+stat('profit factor',m.pf):'');
 return '<div class=card><div class=row><span class=mut style="font-size:12px">'+nm+' · paper book</span><span class=mut style="font-size:11px">'+m.deploy_pct+'% deployed · '+fmtc(m.ccy,m.cash)+' cash</span></div>'
 +'<div class=engwrap>'
   +'<div class=engpnl><div><div class=mut style="font-size:11px">today</div><div style="font-size:19px;font-weight:650">'+pnlS(m.ccy,m.today_pnl,m.today_pct)+'</div></div>'
     +'<div style="margin-top:12px"><div class=mut style="font-size:11px">overall</div><div style="font-size:19px;font-weight:650">'+pnlS(m.ccy,m.overall_pnl,m.overall_pct)+'</div></div></div>'
   +'<div class=engchart>'+spark(m.equity_series,m.ccy)+'</div>'
 +'</div>'
 +'<div class=engstats>'+stat('positions',m.positions)+stat('budget',fmtc(m.ccy,m.budget))+extra+'</div></div>'}
function posRow(p){var s=p.market=='IN'?'₹':'$',fmt=(p.market=='IN'?INR:USD);
 var amt=(p.pnl_amt<0?'-':'+')+s+fmt.format(Math.abs(p.pnl_amt));var st=stratTag(p.strategy);
 return `<div class=prow onclick="stock('${p.symbol}','${p.market}')"><div class=prow-l><div class=prow-sym>${p.symbol}<span class="badge ${st[1]}" style="margin-left:8px;font-weight:500">${st[0]}</span></div><div class=prow-sub>${p.qty} qty · avg ${s}${p.entry} · exit at ${s}${p.stop}</div></div><div class=prow-r><div class="prow-ltp num">${s}${p.live} <span class="${col(p.pnl)}" style="font-weight:600;font-size:11.5px">${sgn(p.pnl)}%</span></div><div class="prow-pnl num ${col(p.pnl)}">${amt}</div></div></div>`;}
function heroChart(series,baseline){
 if(!series||series.length<2)return '';
 var w=340,h=176,n=series.length,lo=Math.min.apply(null,series),hi=Math.max.apply(null,series);
 var ref=(baseline!=null?baseline:series[0]);
 lo=Math.min(lo,ref);hi=Math.max(hi,ref);
 var rng=(hi-lo)||1,pad=16;
 var up=series[n-1]>=ref,c=up?'#3fa45b':'#e34d3f',id='hg'+(SPARKN++);
 var Y=function(v){return (h-pad-(v-lo)/rng*(h-2*pad)).toFixed(1)};
 var pts=series.map(function(v,i){return (i/(n-1)*w).toFixed(1)+','+Y(v)}).join(' ');
 var base=Y(ref);
 return '<svg viewBox="0 0 '+w+' '+h+'" preserveAspectRatio="none"><defs><linearGradient id="'+id+'" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="'+c+'" stop-opacity=".15"/><stop offset="1" stop-color="'+c+'" stop-opacity="0"/></linearGradient></defs>'
  +'<polygon points="0,'+h+' '+pts+' '+w+','+h+'" fill="url(#'+id+')"/>'
  +'<line x1="0" y1="'+base+'" x2="'+w+'" y2="'+base+'" stroke="#c8cdd6" stroke-width="1" stroke-dasharray="2 4" vector-effect="non-scaling-stroke"/>'
  +'<polyline points="'+pts+'" fill="none" stroke="'+c+'" stroke-width="2" vector-effect="non-scaling-stroke" stroke-linejoin="round" stroke-linecap="round"/></svg>';}
function fdSet(id,cls,html){var el=document.getElementById(id);if(!el)return;el.className=cls;el.innerHTML=html;}
var HERO=null,HEROTAB='1d';
function setHeroTab(t){HEROTAB=t;renderHero();}
function renderHero(){
 // one question per view, and the words ALWAYS match the line:
 //  1D  = today's equity minute-by-minute vs yesterday's close (dotted)
 //  1M  = last ~22 daily closes vs the first of the window
 //  All = every daily close since the book started
 if(!HERO)return;var m=HERO,f=(m.ccy=='₹'?INR:USD);
 var series,baseline,noun,note,chg,pct,tab=HEROTAB;
 if(tab=='1d'&&(m.today_series||[]).length>=3){
  series=m.today_series;baseline=m.prev_equity;noun='today';
  chg=m.today_pnl;pct=m.today_pct;note="dotted line = yesterday's close · updates live";
 }else if(tab=='1m'&&(m.daily_series||[]).length>=2){
  series=m.daily_series.slice(-22);baseline=series[0];noun='this month';
  chg=series[series.length-1]-baseline;pct=baseline?Math.round(chg/baseline*10000)/100:0;
  note='one point per trading day · last '+series.length+' sessions';
 }else{
  series=(m.daily_series||[]).length>=2?m.daily_series:(m.equity_series||[]);
  baseline=series[0];noun='since start';
  chg=series[series.length-1]-baseline;pct=baseline?Math.round(chg/baseline*10000)/100:0;
  note='one point per trading day'+(m.daily_start?' · since '+m.daily_start:'');
  if(tab=='1d')note='today’s live chart appears a few minutes after the 9:15 open — showing since start';
 }
 var up=(chg||0)>=0;
 var tabs=['1d','1m','all'].map(function(t){return '<span class="fd-tab'+(t==HEROTAB?' on':'')+'" onclick="setHeroTab(\''+t+'\')">'+t.toUpperCase()+'</span>'}).join('');
 fdSet('fdPerf','fd-card',
  '<div class=fd-hd><span class=fd-dot style="background:'+(up?'var(--upb)':'var(--dnb)')+';color:'+(up?'var(--up)':'var(--dn)')+'">'+(up?'▲':'▼')+'</span>'
  +'<div><div class=fd-title>'+(up?"You're up ":"Down ")+noun+'</div><div class=fd-meta>live paper book · '+(m.market||'IN')+'</div></div>'
  +'<div class=fd-tabs>'+tabs+'</div></div>'
  +'<div class=fd-big>'+fmtc(m.ccy,m.equity)+'</div>'
  +'<div class="fd-chg '+(up?'up':'dn')+'">'+(up?'▲ +':'▼ ')+m.ccy+f.format(Math.abs(Math.round(chg)))+' ('+(up?'+':'')+pct+'%) '+noun+'</div>'
  +'<div class=fd-chart>'+heroChart(series,baseline)+'</div>'
  +'<div class=fd-meta style="margin-top:6px">'+note+'</div>');
}
function loadHome(){
 if(!document.getElementById('fdPerf'))document.getElementById('homefeed').innerHTML='<div id=fdPerf></div><div id=fdBrain></div><div id=fdScore></div><div id=fdTrades></div><div id=fdHold></div>';
 api('/v2/api/overview').then(function(r){var d=r.j;document.getElementById('clock').textContent=d.as_of;
  var ms=(d.markets||[]).filter(function(m){return inMkt(m.market)});var m=ms[0]||{},f=(m.ccy=='₹'?INR:USD);
  var hr=new Date().getHours(),greet=hr<12?'Good morning':(hr<17?'Good afternoon':'Good evening'),up=(m.today_pnl||0)>=0;
  var nm=(ME&&ME.username)?(ME.username.charAt(0).toUpperCase()+ME.username.slice(1)):'';
  document.getElementById('fd-hi').textContent=greet+(nm?', '+nm:'');
  document.getElementById('fd-sub').innerHTML='OpenStocks is managing <b>'+fmtc(m.ccy,m.equity)+'</b> across '+(m.positions||0)+' stocks';
  HERO=m;renderHero();
  var RS={STRONG:['is deploying into strength','the market is trending up hard, so OpenStocks is adding momentum names — and buying stocks moving on fresh news + heavy volume'],
          ON:['is in risk-on mode','conditions look healthy, so OpenStocks is buying dips, breakouts, and news-driven volume surges'],
          NEUTRAL:['is being selective','the market is choppy, so OpenStocks only takes its highest-conviction setups and stocks with a real news catalyst on strong volume'],
          OFF:['is playing defense','the market is weak, so OpenStocks paused routine dip-buys — but still catches individual stocks breaking out on fresh news + volume']};
  var rg=(d.regime_state||{})[m.market]||'NEUTRAL',rv=RS[rg]||RS.NEUTRAL;
  fdSet('fdBrain','fd-card','<div class=fd-hd><span class=fd-dot style="background:var(--infb);color:var(--inf)">◆</span><div><div class=fd-title>OpenStocks '+rv[0]+'</div><div class=fd-meta>market read</div></div></div>'
   +'<div class=fd-text>'+rv[1].charAt(0).toUpperCase()+rv[1].slice(1)+'. Right now <b>'+(m.deploy_pct||0)+'%</b> of your capital is working across <b>'+(m.positions||0)+' stocks</b>, with <b>'+fmtc(m.ccy,m.cash)+'</b> kept in reserve.</div>');
  fdSet('fdScore','fd-card','<div class=fd-hd><span class=fd-dot style="background:#efe9ff;color:#7a4bff">★</span><div><div class=fd-title>Track record</div><div class=fd-meta>this book, all-time</div></div></div>'
   +'<div class=fd-scored>'
   +'<div><div class=fd-sn>'+(m.win!=null?m.win+'%':'—')+'</div><div class=fd-sl>win rate</div></div>'
   +'<div><div class=fd-sn>'+(m.pf!=null?m.pf:'—')+'</div><div class=fd-sl>profit factor</div></div>'
   +'<div><div class=fd-sn>'+(m.sharpe!=null?m.sharpe:'—')+'</div><div class=fd-sl>Sharpe</div></div>'
   +'<div><div class="fd-sn '+((m.maxdd||0)<0?'dn':'')+'">'+(m.maxdd!=null?m.maxdd+'%':'—')+'</div><div class=fd-sl>max drawdown</div></div>'
   +'<div><div class=fd-sn>'+(m.trades||0)+'</div><div class=fd-sl>trades</div></div>'
   +'<div><div class="fd-sn '+(m.overall_pnl>=0?'up':'dn')+'">'+(m.overall_pnl>=0?'+':'')+(m.overall_pct||0)+'%</div><div class=fd-sl>overall</div></div>'
   +'</div>');
 });
 api('/v2/api/orders?limit=60').then(function(r){var os=(r.j||[]).filter(function(o){return o.today&&inMkt(o.market)});
  if(!os.length){fdSet('fdTrades','','');return;}
  var buys=os.filter(function(o){return o.side=='BUY'}),sells=os.filter(function(o){return o.side=='SELL'});
  var trow=function(o){var s=o.ccy,f=(o.ccy=='₹'?INR:USD);
   var right=(o.side=='SELL'&&o.pnl!=null)?('<span class="'+col(o.pnl)+'" style="font-weight:600;font-size:12.5px">'+sgn(o.pnl)+'%</span>'):('<span class="num mut" style="font-size:12px">'+s+f.format(o.value||Math.round(o.qty*o.price))+'</span>');
   return '<div class=fd-trade><span class="badge '+(o.side=='BUY'?'bg-inf':'bg-dn')+'">'+o.side+'</span><span class=fd-tsym><b style="font-size:13.5px">'+o.symbol+'</b><span class="mut num" style="font-size:11.5px">'+o.qty+' @ '+s+o.price+'</span></span>'+right+'</div>';};
  var mk=function(lbl,arr){return arr.length?('<div class=fd-movecol><div class=fd-movelbl>'+lbl+' · '+arr.length+'</div>'+arr.map(trow).join('')+'</div>'):'';};
  fdSet('fdTrades','fd-card','<div class=fd-hd><span class=fd-dot style="background:var(--warnb);color:var(--warn)">⇄</span><div><div class=fd-title>Today’s moves</div><div class=fd-meta>'+buys.length+' bought · '+sells.length+' sold</div></div></div><div class=fd-movegrid>'+mk('Bought',buys)+mk('Sold',sells)+'</div>');
 });
 api('/v2/api/positions').then(function(r){var ps=r.j.filter(function(p){return inMkt(p.market)});
  var pr=ps.map(posRow).join('');
  fdSet('fdHold','fd-card','<div class=fd-hd><span class=fd-dot style="background:var(--upb);color:var(--up)">▤</span><div><div class=fd-title>What your AI is holding</div><div class=fd-meta>'+ps.length+' stocks</div></div></div>'+(pr?'<div class=fd-holds>'+pr+'</div>':'<div class=fd-text>No open positions right now.</div>'));
 });}
function load(){loadHome()}
function load(){loadHome()}
var POS=[],SUBPOS='pos';
function subPos(v){SUBPOS=v;document.getElementById('sbpos').className=(v=='pos'?'on':'');document.getElementById('sbhold').className=(v=='hold'?'on':'');renderPos();}
function renderPos(){var ps=POS.filter(p=>inMkt(p.market)).filter(p=>SUBPOS=='pos'?p.today:!p.today);
 var byc={};ps.forEach(p=>{byc[p.ccy]=(byc[p.ccy]||0)+p.pnl_amt});
 var t=Object.keys(byc).map(cc=>'<span class="'+col(byc[cc])+'">'+(byc[cc]<0?'-':'+')+cc+(cc=='₹'?INR:USD).format(Math.abs(Math.round(byc[cc])))+'</span>').join(' · ');
 document.getElementById('postot').innerHTML=(t||'—')+' P&L';
 var rows=ps.map(posRow).join('');
 document.getElementById('poslist').innerHTML=rows?('<div class=k-list>'+rows+'</div>'):('<div class=mut style="font-size:12px;padding:14px 16px">'+(SUBPOS=='pos'?'nothing bought today':'no overnight holdings')+'</div>');}
function loadPos(){api('/v2/api/positions').then(r=>{POS=r.j;renderPos();});loadAttrib();loadPortfolio();}
function loadAttrib(){api('/v2/api/attribution').then(r=>{var d=r.j||{};
 var rows=(d.strategies||[]).filter(s=>inMkt(s.market));
 document.getElementById('attrib').innerHTML=rows.map(s=>{var st=stratTag(s.strategy);var tot=s.realized+s.unrealized;
  return '<div class=card><div class=row><span class="badge '+st[1]+'">'+s.market+' '+st[0]+'</span><span class="'+col(tot)+'" style="font-size:13px;font-weight:600">'+(tot<0?'-':'+')+s.ccy+(s.ccy=='₹'?INR:USD).format(Math.abs(Math.round(tot)))+'</span></div>'
  +'<div class=mut style="font-size:11px;margin-top:6px">'+s.closed+' closed · win '+s.win+'% · avg '+sgn(s.avg_ret)+'%</div>'
  +'<div class=mut style="font-size:11px">'+s.open+' open · unrl '+(s.unrealized<0?'-':'+')+s.ccy+(s.ccy=='₹'?INR:USD).format(Math.abs(Math.round(s.unrealized)))+'</div></div>'}).join('')||'<div class=skel>no trades yet</div>';
 var eq=d.equity||{};var dd=[];
 document.getElementById('eqcurves').innerHTML=['IN','US'].filter(inMkt).map(m=>{var e=eq[m]||{};if((e.equity||[]).length<3)return '';dd.push(m+' maxDD '+(e.maxdd||0)+'%');
  return '<div class=card><div class=mut style="font-size:11px">'+(m=='IN'?'India ₹':'US $')+' · '+(e.days||[]).length+'d</div>'+spark(e.equity,m=='IN'?'₹':'$')+'</div>'}).join('');
 document.getElementById('eqdd').textContent=dd.join(' · ');});}
var ORD_RANGE={days:1,from:null,to:null};
function setOrdRange(el){var d=el.dataset.d;document.querySelectorAll('#ordrange b').forEach(function(b){b.classList.toggle('on',b===el)});
 if(d=='custom'){document.getElementById('ordcustom').classList.remove('hide');return;}
 document.getElementById('ordcustom').classList.add('hide');ORD_RANGE={days:+d,from:null,to:null};loadOrders();}
function applyOrdCustom(){var f=document.getElementById('ordfrom').value,t=document.getElementById('ordto').value;
 if(!f&&!t){document.getElementById('ordtot').textContent='pick a date';return;}ORD_RANGE={days:null,from:f||null,to:t||null};loadOrders();}
function ordInRange(o){var d=(o.ts||'').slice(0,10);if(!d)return false;
 if(ORD_RANGE.days!=null){var c=new Date(Date.now()-(ORD_RANGE.days-1)*86400000).toISOString().slice(0,10);return d>=c;}
 if(ORD_RANGE.from&&d<ORD_RANGE.from)return false;if(ORD_RANGE.to&&d>ORD_RANGE.to)return false;return true;}
function loadOrders(){api('/v2/api/orders?limit=500').then(r=>{var os=r.j.filter(o=>inMkt(o.market)).filter(ordInRange);
 var buys=os.filter(o=>o.side=='BUY'),sells=os.filter(o=>o.side=='SELL');
 document.getElementById('ordtot').textContent=(buys.length+sells.length)?(buys.length+' bought · '+sells.length+' sold'):'';
 var col=function(lbl,arr,cls){return '<div class="ordcol '+cls+'"><div class=ordlbl>'+lbl+' · '+arr.length+'</div>'+(arr.length?('<div class=card style="padding:2px 15px">'+arr.map(ordRow).join('')+'</div>'):'<div class=card style="padding:15px 16px"><span class=mut style="font-size:12px">nothing in this range</span></div>')+'</div>';};
 document.getElementById('ordlist').innerHTML='<div class=ordgrid>'+col('Bought',buys,'oc-buy')+col('Sold',sells,'oc-sell')+'</div>';});}
function setOrdSide(s){var l=document.getElementById('ordlist');if(l){l.classList.remove('os-buy','os-sell');l.classList.add('os-'+s)}document.querySelectorAll('#ordside b').forEach(function(b,i){b.classList.toggle('on',(i===0)===(s==='buy'))})}
function exitPos(id,sym){if(!confirm('Exit '+sym+' at live price?'))return;api('/v2/api/positions/'+id+'/exit',{method:'POST'}).then(r=>{if(r.ok){loadPos();loadHome()}else{alert(r.j.error||'Failed')}})}
function doAnalyze(){var s=document.getElementById('qsym').value.trim().toUpperCase();if(!s)return;var m=document.getElementById('qmkt').value;document.getElementById('ares').innerHTML='<div class=skel>analysing '+s+'…</div>';renderStock(s,m,'ares')}
function loadAccount(){var el=document.getElementById('account');var u=ME||{};var b=balOf();
 el.innerHTML=`<div class=sec>account</div>
 <div class=raise><div style="display:flex;gap:12px;align-items:center"><div class=prof style="width:44px;height:44px;font-size:17px">${(u.username||'U')[0].toUpperCase()}</div><div><div style="font-weight:600">${u.username||'—'}</div><div class=mut style="font-size:12px">${u.role||'user'}${u.credits!=null?' · credits '+u.credits:''}</div></div></div></div>
 <div class=sec>trading mode</div>
 <div class=raise><div class=mut style="font-size:13px;margin-bottom:9px">Paper = simulated cash. Live = real orders via your broker.</div>
  <div class=toggle><b id=mp class="${MODE!='live'?'on':''}" onclick="setMode('paper')">Paper</b><b id=ml class="${MODE=='live'?'on':''}" onclick="setMode('live')">Live</b></div>
  <div id=modemsg class=mut style="font-size:12px;margin-top:9px"></div></div>
 <div class=sec>telegram alerts</div>
 <div id=tgbox class=raise><div class=skel>loading…</div></div>
 <div class=sec>paper allocation</div>
 <div class=raise><div style="display:flex;gap:16px;flex-wrap:wrap"><div class=field style="flex:0 1 260px"><label>India cash (₹)</label><input id=cin type=number value="${Math.round(b.IN)||''}"></div></div>
  <button class=pri onclick=saveCash()>Save allocation</button><div id=cashmsg class=mut style="font-size:12px;margin-top:9px"></div></div>
 <div class=sec>engine performance</div><div id=acctstats class=skel>loading…</div>
 ${(u.role=='admin')?'<div class=sec>admin · allocate paper money</div><div id=adminbox class=raise><div class=skel>loading users…</div></div>':''}
 <div class=sec>broker (for live)</div><div class=raise><div class=row style="padding:6px 0"><span>Upstox · India</span><button class=sm onclick="openBroker('upstox')">connect</button></div><div class=row style="padding:6px 0;border-top:1px solid var(--line)"><span>Alpaca · US</span><button class=sm onclick="openBroker('alpaca')">connect</button></div></div>
 <div class=sec style="color:var(--dn)">danger zone</div>
 <div class=raise><div class=mut style="font-size:13px;margin-bottom:10px">Reset the paper book to a clean ₹1,00,000 — clears all positions, trades and equity history. Paper money only; your Telegram link is kept.</div><button class=pri style="background:var(--dn);border-color:var(--dn)" onclick=doReset()>Reset paper book</button><div id=resetmsg class=mut style="font-size:12px;margin-top:9px"></div></div>`;
 api('/v2/api/stats').then(r=>{document.getElementById('acctstats').innerHTML=r.j.map(s=>`<div class=raise><div class=row><b>${s.market=='IN'?'India':'US'}</b><span class="${col(s.overall_pnl)}">overall ${s.overall_pnl<0?'-':'+'}${s.ccy}${(s.ccy=='₹'?INR:USD).format(Math.abs(s.overall_pnl))}</span></div><div class=grid style="margin-top:8px"><div class=card><div class=mut style="font-size:11px">win</div><div style="font-size:17px;font-weight:600">${s.win}%</div></div><div class=card><div class=mut style="font-size:11px">PF</div><div style="font-size:17px;font-weight:600">${s.pf}</div></div><div class=card><div class=mut style="font-size:11px">avg win</div><div class="up" style="font-size:16px;font-weight:600">${sgn(s.avg_win)}%</div></div><div class=card><div class=mut style="font-size:11px">avg loss</div><div class="dn" style="font-size:16px;font-weight:600">${s.avg_loss}%</div></div></div><div class=mut style="font-size:11px;margin-top:7px">${s.trades} closed · ${s.deploy_pct}% deployed</div></div>`).join('')||'<div class=card style="padding:14px 16px"><span class=mut style="font-size:12px">no closed trades yet — stats appear after the first exits</span></div>';});
 if(u.role=='admin')api('/api/users').then(r=>{var us=(r.j.users||[]);document.getElementById('adminbox').innerHTML=us.map(x=>`<div style="padding:8px 0;border-bottom:1px solid var(--line)"><div class=row><b>${x.username}</b><span class=mut style="font-size:11px">${x.role||'user'}</span></div><div style="display:flex;gap:6px;margin-top:6px"><input id="ai_${x.id}" type=number placeholder="India ₹" style="padding:7px 9px"><button class=sm onclick="allocUser(${x.id})">set</button></div></div>`).join('')||'<div class=mut>no users</div>';});
 loadTelegram();}
function loadTelegram(){var el=document.getElementById('tgbox');if(!el)return;
 api('/api/me/telegram').then(function(r){var t=r.j||{};
  if(t.linked){
   el.innerHTML='<div class=row style="padding:2px 0 12px"><span style="font-size:13px"><b style="color:var(--up)">● Connected</b> · @'+(t.bot||'your bot')+'</span><button class=sm onclick="tgUnlink()">Disconnect</button></div>'
    +'<div style="border-top:1px solid var(--line);padding-top:12px"><div class=mut style="font-size:12px;margin-bottom:9px">Alert me on Telegram when:</div>'
    +'<label class=tgopt><input type=checkbox id=tgbuy '+(t.alerts_buy?'checked':'')+' onchange="tgSavePrefs()"> The AI buys a stock</label>'
    +'<label class=tgopt><input type=checkbox id=tgsell '+(t.alerts_sell?'checked':'')+' onchange="tgSavePrefs()"> The AI sells a stock</label>'
    +'<label class=tgopt><input type=checkbox id=tgradar '+(t.alerts_radar?'checked':'')+' onchange="tgSavePrefs()"> Stocks the AI is watching to buy</label>'
    +'<label class=tgopt><input type=checkbox id=tgprice '+(t.alerts_price?'checked':'')+' onchange="tgSavePrefs()"> My watchlist price alerts trigger</label>'
    +'<label class=tgopt><input type=checkbox id=tgsummary '+(t.alerts_summary?'checked':'')+' onchange="tgSavePrefs()"> Daily progress summary</label>'
    +'<div style="margin-top:12px"><button class=sm onclick="tgTest(this)">Send a test alert</button></div>'
    +'<div id=tgmsg class=mut style="font-size:12px;margin-top:9px"></div></div>';
  } else if(t.has_token){
   el.innerHTML='<div class=mut style="font-size:13px;margin-bottom:11px">Last step — open your bot <b>@'+t.bot+'</b>, press <b>Start</b>, then tap Verify.</div>'
    +'<a href="'+t.deep_link+'" target=_blank class=pri style="display:inline-block;text-decoration:none;padding:10px 15px">Open @'+t.bot+' in Telegram →</a>'
    +' <button class=sm style="margin-left:6px" onclick="tgVerify(this)">I’ve pressed Start</button>'
    +'<div id=tgmsg class=mut style="font-size:12px;margin-top:10px"></div>'
    +'<div style="margin-top:10px"><span class=mut style="font-size:12px;cursor:pointer;text-decoration:underline" onclick="tgReset()">use a different bot</span></div>';
  } else {
   el.innerHTML='<div class=mut style="font-size:13px;line-height:1.6;margin-bottom:13px">Get buy/sell alerts on your own Telegram. Make your personal bot — takes a minute:<br>'
    +'1. In Telegram, open <b><a href="https://t.me/BotFather" target=_blank style="color:var(--inf)">@BotFather</a></b> and send <b>/newbot</b><br>'
    +'2. Pick a name and a username, then copy the <b>token</b> it gives you<br>'
    +'3. Paste the token below</div>'
    +'<div class=field><input id=tgtok type=text placeholder="paste bot token — e.g. 8123456789:AAH…" autocomplete=off spellcheck=false></div>'
    +'<button class=pri onclick="tgSaveToken(this)">Save token</button>'
    +'<div id=tgmsg class=mut style="font-size:12px;margin-top:10px"></div>';
  }});}
function tgSaveToken(btn){var tok=(document.getElementById('tgtok').value||'').trim();if(!tok){document.getElementById('tgmsg').innerHTML='<span class=dn>Paste your bot token first.</span>';return;}
 btn.disabled=true;btn.textContent='Checking…';
 api('/api/me/telegram/token',{method:'POST',body:JSON.stringify({token:tok})}).then(function(r){
  if(r.ok){loadTelegram();}else{btn.disabled=false;btn.textContent='Save token';document.getElementById('tgmsg').innerHTML='<span class=dn>'+((r.j&&r.j.detail)||'That token didn’t work.')+'</span>';}});}
function tgVerify(btn){btn.disabled=true;btn.textContent='Checking…';
 api('/api/me/telegram/verify',{method:'POST'}).then(function(r){
  if(r.ok){loadTelegram();}else{btn.disabled=false;btn.textContent='I’ve pressed Start';document.getElementById('tgmsg').innerHTML='<span class=dn>'+((r.j&&r.j.detail)||'Open your bot and press Start, then try again.')+'</span>';}});}
function tgReset(){api('/api/me/telegram/unlink',{method:'POST'}).then(loadTelegram);}
function tgTest(btn){btn.disabled=true;btn.textContent='Sending…';var m=document.getElementById('tgmsg');
 api('/api/me/telegram/test',{method:'POST'}).then(function(r){btn.disabled=false;btn.textContent='Send a test alert';
  m.innerHTML=r.ok?'<span class=up>Sent ✓ — check your Telegram</span>':'<span class=dn>'+((r.j&&r.j.detail)||'Failed to send')+'</span>';});}
function tgSavePrefs(){var b=document.getElementById('tgbuy').checked,s=document.getElementById('tgsell').checked,
 rd=document.getElementById('tgradar').checked,su=document.getElementById('tgsummary').checked,pr=document.getElementById('tgprice').checked;
 api('/api/me/telegram/prefs',{method:'POST',body:JSON.stringify({alerts_buy:b,alerts_sell:s,alerts_radar:rd,alerts_summary:su,alerts_price:pr})}).then(function(r){document.getElementById('tgmsg').textContent=r.ok?'Saved ✓':'Failed';});}
function tgUnlink(){if(!confirm('Disconnect Telegram alerts?'))return;api('/api/me/telegram/unlink',{method:'POST'}).then(loadTelegram);}
function setMode(m){api('/api/me/signal-execution-mode',{method:'POST',body:JSON.stringify({signal_execution_mode:m})}).then(r=>{if(r.ok){MODE=(r.j.signal_execution_mode||m);document.getElementById('mp').className=(MODE!='live'?'on':'');document.getElementById('ml').className=(MODE=='live'?'on':'');document.getElementById('modemsg').textContent=r.j.message||('Mode: '+MODE);renderBalance();}else{document.getElementById('modemsg').textContent=r.j.detail||'Failed';}})}
function saveCash(){var b={},i=document.getElementById('cin').value;if(i)b.india_cash=+i;
 var url=(ME&&ME.role=='admin'&&ME.id)?('/api/users/'+ME.id+'/paper-cash'):'/api/me/paper-cash';
 api(url,{method:'POST',body:JSON.stringify(b)}).then(r=>{document.getElementById('cashmsg').textContent=r.ok?'Saved ✓':(r.j.detail||'Failed');if(r.ok)loadAccountData();})}
function allocUser(id){var b={},i=document.getElementById('ai_'+id).value;if(i)b.india_cash=+i;api('/api/users/'+id+'/paper-cash',{method:'POST',body:JSON.stringify(b)}).then(r=>{alert(r.ok?'Allocated to user.':(r.j.detail||'Failed'))})}
function openBroker(w){api(w=='upstox'?'/api/me/upstox/auth-url':'/api/alpaca/connect').then(r=>{if(r.j.auth_url)location.href=r.j.auth_url;else alert('Connect '+w+' from settings.')})}
function stock(sym,mkt){go('detail');renderStock(sym,mkt,'detail')}
function renderStock(sym,mkt,target){var el=document.getElementById(target);if(target=='detail')el.innerHTML='<div class=skel>analysing '+sym+'…</div>';
 api('/v2/api/stock/'+sym+'?market='+mkt).then(r=>{var d=r.j,s=mkt=='IN'?'₹':'$';
 if(d.error){el.innerHTML=(target=='detail'?'<div class=mut style="padding:12px 0;cursor:pointer" onclick="go(\'home\')">‹ back</div>':'')+'<div class=skel>'+(d.error)+(d.live?' · '+s+d.live:'')+'</div>'+newsHtml(d.news,s);return;}
 var vt=d.verdict=='BUY'?'var(--up)':(d.verdict=='WATCH'?'var(--warn)':'var(--dn)'),vb=d.verdict=='BUY'?'var(--upb)':(d.verdict=='WATCH'?'var(--warnb)':'var(--dnb)');
 var fb=Object.keys(d.factors||{}).map(k=>{var v=d.factors[k],c=v>=60?'var(--up)':(v>=40?'var(--warn)':'var(--dn)');return '<div style="margin:8px 0"><div class=row style="font-size:12px"><span class=mut>'+k.replace('_',' ')+'</span><span>'+v+'</span></div><div class=scorebar><i style="width:'+v+'%;background:'+c+'"></i></div></div>'}).join('');
 el.innerHTML=(target=='detail'?'<div class=mut style="padding:10px 0;cursor:pointer" onclick="go(\'home\')">‹ back</div>':'')
 +'<div class=detail-grid><div class=detail-main>'
  +'<div class=row><div><div style="font-size:19px;font-weight:600">'+sym+'</div><div class=mut style="font-size:12px">'+mkt+' · live</div></div><div class=hero style="font-size:27px">'+s+d.live+'</div></div>'
  +'<div id=candleWrap class=raise style="padding:10px 12px;margin-top:10px"></div>'
  +'<div class=raise style="background:'+vb+';border:none;margin-top:10px"><div class=row><b style="color:'+vt+'">'+d.verdict+'</b><span style="color:'+vt+';font-size:12px">score '+d.score+(d.regime===false?' · regime risk-off':'')+'</span></div>'+(d.insight?'<div style="font-size:13.5px;line-height:1.55;margin-top:8px;color:var(--tx)">'+d.insight+'</div>':'')+'</div>'
  +(d.held?'<div class=raise style="margin-top:10px"><div class=row><b>you hold this · '+d.held.strategy+'</b><span class="'+col(d.held.pnl)+'">'+sgn(d.held.pnl)+'%</span></div><div class=mut style="font-size:12px;margin-top:4px">entry '+s+d.held.entry+' · qty '+d.held.qty+' · exits on '+d.held.rule+'</div></div>':'')
 +'</div><div class=detail-side>'
  +'<div class=sec style="margin-top:0;font-size:13px">'+(d.held?'if you buy more — plan':'trade plan')+'</div>'
  +'<div class=grid2><div class=card><div class=mut style="font-size:11px">entry</div><div style="font-size:17px;font-weight:600">'+s+d.entry+'</div></div><div class=card><div class=mut style="font-size:11px">reward:risk</div><div style="font-size:17px;font-weight:600">'+d.rr+':1</div></div><div class=card><div class=mut style="font-size:11px">stop</div><div class="dn" style="font-size:17px;font-weight:600">'+s+d.stop+'</div></div><div class=card><div class=mut style="font-size:11px">target</div><div class="up" style="font-size:17px;font-weight:600">'+s+d.target+'</div></div></div>'
  +'<div style="display:flex;gap:9px;margin:14px 0 4px"><button style="flex:1" onclick="setAlert(\''+sym+'\',\''+mkt+'\','+d.live+')">Set alert</button>'
   +(d.held?'<button class=pri style="flex:1;background:var(--dn);border-color:var(--dn)" onclick="doSell(\''+sym+'\',\''+mkt+'\')">Sell</button>'
           :'<button class=pri style="flex:1" onclick="doBuy(\''+sym+'\',\''+mkt+'\')">Paper buy</button>')+'</div>'
  +recHtml(d.recommendation,s)
  +panelHtml(d.recommendation&&d.recommendation.panel,s)
  +'<div class=sec>why this score</div>'+fb+newsHtml(d.news,s)
 +'</div></div>';
 var cd=d.candles||[];CHART={candles:cd,dates:d.dates||[],ccy:s,levels:[{v:d.entry,c:'#4184f3',t:'entry'},{v:d.stop,c:'#e34d3f',t:'stop'},{v:d.target,c:'#3fa45b',t:'target'}],range:Math.min(66,cd.length)};drawCandles();});}
var CHART=null;
function setRange(n){if(CHART){CHART.range=n;drawCandles()}}
function drawCandles(){var wrap=document.getElementById('candleWrap');if(!wrap||!CHART)return;
 var all=CHART.candles;if(!all||all.length<2){wrap.innerHTML='<div class=skel style="padding:18px 0">no chart data yet</div>';return;}
 var st=Math.max(0,all.length-(CHART.range||all.length));
 var c=all.slice(st),ds=(CHART.dates||[]).slice(st);
 wrap.innerHTML=candleSVG(c,CHART.levels,CHART.ccy,ds)+rangeBar(all.length);}
function rangeBar(total){var opts=[['1M',22],['3M',66],['6M',132]].filter(o=>o[1]<total);opts.push(['All',total]);
 return '<div style="display:flex;gap:6px;margin:8px 0 0">'+opts.map(o=>'<b class="rgb'+(CHART.range==o[1]?' on':'')+'" onclick="setRange('+o[1]+')">'+o[0]+'</b>').join('')+'</div>';}
function candleSVG(c,levels,ccy,dates){
 var W=600,PH=210,GAP=13,VH=54,XA=16,H=PH+GAP+VH+XA,n=c.length;
 var lo=1e18,hi=-1e18,vmax=0;
 c.forEach(function(k){if(k[2]<lo)lo=k[2];if(k[1]>hi)hi=k[1];if(k[4]>vmax)vmax=k[4]});
 var rng=(hi-lo)||1,pad=rng*0.06;lo-=pad;hi+=pad;rng=hi-lo;
 var step=W/n,bw=Math.max(1.4,step*0.62);
 function X(i){return i*step+step/2}
 function Y(p){return (PH-(p-lo)/rng*PH)}
 var body='',i,k,up,col;
 for(i=0;i<n;i++){k=c[i];up=k[3]>=k[0];col=up?'#3fa45b':'#e34d3f';var cx=X(i),yo=Y(k[0]),yc=Y(k[3]),yt=Math.min(yo,yc),hb=Math.max(1,Math.abs(yc-yo));
  body+='<line x1="'+cx.toFixed(1)+'" y1="'+Y(k[1]).toFixed(1)+'" x2="'+cx.toFixed(1)+'" y2="'+Y(k[2]).toFixed(1)+'" stroke="'+col+'" stroke-width="1" vector-effect="non-scaling-stroke"/>';
  body+='<rect x="'+(cx-bw/2).toFixed(1)+'" y="'+yt.toFixed(1)+'" width="'+bw.toFixed(1)+'" height="'+hb.toFixed(1)+'" fill="'+col+'" rx="0.4"/>';
  var vh=vmax?k[4]/vmax*VH:0;body+='<rect x="'+(cx-bw/2).toFixed(1)+'" y="'+(H-vh).toFixed(1)+'" width="'+bw.toFixed(1)+'" height="'+vh.toFixed(1)+'" fill="'+col+'" opacity="0.32"/>';}
 var ma='',pts=[];for(i=0;i<n;i++){if(i<19)continue;var sm=0;for(var j=i-19;j<=i;j++)sm+=c[j][3];pts.push(X(i).toFixed(1)+','+Y(sm/20).toFixed(1))}
 if(pts.length>1)ma='<polyline points="'+pts.join(' ')+'" fill="none" stroke="#38bdf8" stroke-width="1.4" opacity="0.85" vector-effect="non-scaling-stroke"/>';
 var grid='',lab='';[0.5,0.0,1.0].forEach(function(f){var p=lo+rng*f,y=Y(p);grid+='<line x1="0" y1="'+y.toFixed(1)+'" x2="'+W+'" y2="'+y.toFixed(1)+'" stroke="rgba(255,255,255,.06)" stroke-width="1" vector-effect="non-scaling-stroke"/>';lab+='<text x="'+(W-2)+'" y="'+(y-2).toFixed(1)+'" text-anchor="end" font-size="9" fill="#7e8ca1">'+(ccy||'')+(p>=1000?Math.round(p):p.toFixed(1))+'</text>'});
 var lv=(levels||[]).filter(function(l){return l.v&&l.v>=lo&&l.v<=hi}).map(function(l){var y=Y(l.v);return '<line x1="0" y1="'+y.toFixed(1)+'" x2="'+W+'" y2="'+y.toFixed(1)+'" stroke="'+l.c+'" stroke-width="1" stroke-dasharray="3 3" opacity="0.7" vector-effect="non-scaling-stroke"/><text x="3" y="'+(y-2).toFixed(1)+'" font-size="9" fill="'+l.c+'">'+l.t+' '+(ccy||'')+l.v+'</text>'}).join('');
 // x-axis date labels (first / middle / last)
 var xl='';if(dates&&dates.length){var fmt=function(s){var p=(s||'').split('-');return p.length==3?(p[2]+' '+['','Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'][+p[1]]):s};
  [0,Math.floor((n-1)/2),n-1].forEach(function(i){var anc=i===0?'start':(i===n-1?'end':'middle');xl+='<text x="'+X(i).toFixed(1)+'" y="'+(H-4)+'" text-anchor="'+anc+'" font-size="9" fill="#7e8ca1">'+fmt(dates[i])+'</text>';});}
 return '<svg viewBox="0 0 '+W+' '+H+'" style="width:100%;height:auto;display:block;overflow:visible">'+grid+lv+body+ma+lab+xl+'</svg>';
}
function chartHtml(closes,levels){
 if(!closes||closes.length<3)return '';
 var w=300,h=92,n=closes.length,lo=Math.min.apply(null,closes),hi=Math.max.apply(null,closes);
 (levels||[]).forEach(function(l){if(l.v){lo=Math.min(lo,l.v);hi=Math.max(hi,l.v)}});
 var rng=(hi-lo)||1,pad=rng*0.08;lo-=pad;hi+=pad;rng=hi-lo;
 function X(i){return (i/(n-1)*w).toFixed(1)}
 function Y(v){return (h-(v-lo)/rng*h).toFixed(1)}
 var pts=closes.map(function(v,i){return X(i)+','+Y(v)}).join(' ');
 var up=closes[n-1]>=closes[0],col=up?'#06a35a':'#df2f29';
 var lv=(levels||[]).filter(function(l){return l.v}).map(function(l){return '<line x1=0 y1='+Y(l.v)+' x2='+w+' y2='+Y(l.v)+' stroke="'+l.c+'" stroke-width=1 stroke-dasharray="2 3" opacity=".5" vector-effect="non-scaling-stroke"/>'}).join('');
 return '<svg viewBox="0 0 '+w+' '+h+'" preserveAspectRatio="none" style="width:100%;height:94px;display:block;margin:10px 0 4px"><polyline points="0,'+h+' '+pts+' '+w+','+h+'" fill="'+col+'" fill-opacity=".09" stroke="none"/><polyline points="'+pts+'" fill="none" stroke="'+col+'" stroke-width="2" vector-effect="non-scaling-stroke"/>'+lv+'</svg>';
}
function spark(series,ccy){
 if(!series||series.length<3)return '';
 var w=120,h=34,n=series.length,lo=Math.min.apply(null,series),hi=Math.max.apply(null,series),rng=(hi-lo)||1;
 var pts=series.map(function(v,i){return (i/(n-1)*w).toFixed(1)+','+(h-3-(v-lo)/rng*(h-6)).toFixed(1)}).join(' ');
 var up=series[n-1]>=series[0],col=up?'#3fa45b':'#e34d3f',id='sg'+(SPARKN++);
 return '<svg viewBox="0 0 '+w+' '+h+'" preserveAspectRatio="none" style="width:100%;height:38px;display:block;margin-top:8px">'
 +'<defs><linearGradient id="'+id+'" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="'+col+'" stop-opacity=".30"/><stop offset="1" stop-color="'+col+'" stop-opacity="0"/></linearGradient></defs>'
 +'<polygon points="0,'+h+' '+pts+' '+w+','+h+'" fill="url(#'+id+')"/>'
 +'<polyline points="'+pts+'" fill="none" stroke="'+col+'" stroke-width="1.8" vector-effect="non-scaling-stroke"/></svg>';
}
var SPARKN=0;
function pill(v){if(v==null)return '<span class="pill pmut">\u2026</span>';var fl=Math.abs(v)<0.005,up=v>0;return '<span class="pill '+(fl?'pmut':(up?'pup':'pdn'))+'">'+(fl?'0.00%':((up?'\u25b2 ':'\u25bc ')+Math.abs(v).toFixed(2)+'%'))+'</span>'}
function esc(x){if(x==null)return '';return String(x).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');}
function pfTile(label,value,tone){return '<div class=card><div class=mut style="font-size:11px">'+esc(label)+'</div><div style="font-size:17px;font-weight:600'+(tone?';color:'+tone:'')+'">'+esc(value)+'</div></div>';}
function pfHtml(d){
 if(!d||d.error)return '<div class=mut style="font-size:13px">allocation data unavailable</div>';
 var s=d.ccy||'₹',c=d.concentration||{},dd=d.drawdown||{};
 var conc=c.largest_pct>=30?'var(--dn)':(c.largest_pct>=20?'var(--warn)':'var(--up)');
 var h='<div class=grid>';
 h+=pfTile('deployed',(c.deployed_pct!=null?c.deployed_pct:0)+'%');
 h+=pfTile('largest position',(c.largest_pct!=null?c.largest_pct:0)+'%',conc);
 h+=pfTile('top 3',(c.top3_pct!=null?c.top3_pct:0)+'%');
 h+=pfTile('positions',c.n_positions!=null?c.n_positions:0);
 h+=pfTile('max drawdown',(dd.max_drawdown_pct!=null?dd.max_drawdown_pct:0)+'%',dd.max_drawdown_pct<0?'var(--dn)':'');
 h+=pfTile('below high-water',(dd.current_drawdown_pct!=null?dd.current_drawdown_pct:0)+'%',dd.current_drawdown_pct<0?'var(--dn)':'');
 h+='</div>';
 var a=d.allocations||[];
 if(!a.length)return h+'<div class=mut style="font-size:13px;margin-top:10px">no open positions</div>';
 h+='<div style="margin-top:12px">';
 for(var i=0;i<a.length;i++){var p=a[i],w=Math.max(1,Math.min(100,p.pct_of_equity||0));
  var uc=p.unrealised_pct>0?'var(--up)':(p.unrealised_pct<0?'var(--dn)':'var(--mut)');
  h+='<div style="margin:9px 0"><div class=row style="font-size:12.5px"><span><b>'+esc(p.symbol)+'</b> <span class=mut>'+esc(p.strategy)+'</span></span>';
  h+='<span>'+s+(p.value!=null?p.value.toLocaleString():'0')+' <span class=mut>'+(p.pct_of_equity!=null?p.pct_of_equity:0)+'%</span> <span style="color:'+uc+'">'+(p.unrealised_pct>0?'+':'')+(p.unrealised_pct!=null?p.unrealised_pct:0)+'%</span></span></div>';
  h+='<div style="height:5px;border-radius:3px;background:var(--line);margin-top:4px"><div style="height:5px;border-radius:3px;width:'+w+'%;background:var(--ac)"></div></div></div>';}
 return h+'</div>';}
function loadPortfolio(){api('/v2/api/portfolio?market='+(typeof MKT!=='undefined'?MKT:'IN')).then(r=>{
 var el=document.getElementById('pfrisk');if(!el)return;
 el.innerHTML=pfHtml(r.j);
 var n=document.getElementById('pfnote');
 if(n&&r.j&&r.j.sector_exposure===null)n.textContent='sector breakdown unavailable';});}
function stanceBar(v){var pct=Math.round(Math.abs(v)*50),c=v>0.05?'var(--up)':(v<-0.05?'var(--dn)':'var(--mut)');
 var left=v<0?(50-pct):50;
 return '<div style="position:relative;height:5px;border-radius:3px;background:var(--line);margin-top:4px">'
  +'<div style="position:absolute;left:'+left+'%;width:'+Math.max(1,pct)+'%;height:5px;border-radius:3px;background:'+c+'"></div>'
  +'<div style="position:absolute;left:50%;top:-2px;width:1px;height:9px;background:var(--mut);opacity:.5"></div></div>';}
function panelHtml(p,s){
 if(!p||!p.cio||!p.opinions)return '';
 var cio=p.cio,c=cio.stance>0.1?'var(--up)':(cio.stance<-0.1?'var(--dn)':'var(--mut)');
 var h='<div class=sec>analyst panel</div><div class=raise style="margin-top:6px">';
 h+='<div class=row><b style="color:'+c+';font-size:15px">'+esc(cio.consensus)+'</b>';
 h+='<span class=mut style="font-size:12px">'+esc(cio.participating)+'/'+esc((p.opinions||[]).length)+' reporting · confidence '+Math.round((cio.confidence||0)*100)+'%</span></div>';
 h+=stanceBar(cio.stance||0);
 if(cio.dissent&&cio.dissent.length){
  h+='<div style="margin-top:11px;padding:8px 10px;border-radius:8px;background:var(--warnb);border-left:3px solid var(--warn)">';
  h+='<div style="font-size:11px;font-weight:600;color:var(--warn);text-transform:uppercase;letter-spacing:.4px">analysts disagree</div>';
  for(var i=0;i<cio.dissent.length;i++)h+='<div style="font-size:12.5px;margin-top:4px">'+esc(cio.dissent[i])+'</div>';
  h+='</div>';}
 h+='<div style="margin-top:12px">';
 for(var j=0;j<p.opinions.length;j++){var o=p.opinions[j];
  if(o.abstained){h+='<div class=row style="padding:6px 0;border-top:1px solid var(--line);font-size:12.5px"><span class=mut>'+esc(o.agent)+'</span><span class=mut style="font-size:11.5px">abstained — '+esc(o.rationale)+'</span></div>';continue;}
  h+='<div style="padding:7px 0;border-top:1px solid var(--line)">';
  h+='<div class=row style="font-size:12.5px"><b>'+esc(o.agent)+'</b><span class=mut style="font-size:11.5px">conf '+Math.round((o.confidence||0)*100)+'%</span></div>';
  h+=stanceBar(o.stance||0);
  h+='<div class=mut style="font-size:11.5px;margin-top:4px">'+esc(o.rationale)+'</div>';
  if(o.evidence&&o.evidence.length){h+='<details style="margin-top:4px"><summary class=mut style="font-size:11px;cursor:pointer">evidence ('+o.evidence.length+')</summary>';
   for(var k=0;k<o.evidence.length;k++){var e=o.evidence[k];
    h+='<div style="font-size:11px;margin-top:3px"><span class=mut>'+esc(e.metric)+'</span> = '+esc(JSON.stringify(e.value))+' <span class=mut>via '+esc(e.source)+'</span></div>';}
   h+='</details>';}
  h+='</div>';}
 return h+'</div></div>';}
function recBadge(score){var m=[['Strong Sell','var(--dn)'],['Sell','var(--dn)'],['Reduce','var(--warn)'],['Hold','var(--mut)'],['Accumulate','var(--warn)'],['Buy','var(--up)'],['Strong Buy','var(--up)']];return m[score]||m[3];}
function recHtml(r,s){
 if(!r)return '';
 if(r.insufficient_data)return '<div class=sec>recommendation</div><div class=mut style="font-size:13px">Not enough stored data on this name to form a view.</div>';
 var b=recBadge(r.rating_score),col=b[1],pct=Math.round((r.confidence||0)*100);
 var h='<div class=sec>recommendation</div>';
 h+='<div class=raise style="margin-top:6px"><div class=row><b style="color:'+col+';font-size:16px">'+esc(r.rating)+'</b>';
 h+='<span class=mut style="font-size:12px">confidence '+pct+'%</span></div>';
 h+='<div style="height:5px;border-radius:3px;background:var(--line);margin-top:8px"><div style="height:5px;border-radius:3px;width:'+pct+'%;background:'+col+'"></div></div>';
 if(r.narrative&&r.narrative.text)h+='<div style="font-size:13.5px;line-height:1.55;margin-top:10px;color:var(--tx)">'+esc(r.narrative.text)+'</div>';
 var lists=[['bull case',r.bull_case,'var(--up)'],['bear case',r.bear_case,'var(--dn)'],['risks',r.risks,'var(--warn)']];
 for(var i=0;i<lists.length;i++){var t=lists[i][0],items=lists[i][1],c=lists[i][2];
  if(!items||!items.length)continue;
  h+='<div style="margin-top:11px"><div class=mut style="font-size:11px;text-transform:uppercase;letter-spacing:.4px">'+t+'</div>';
  for(var j=0;j<items.length;j++)h+='<div style="font-size:12.5px;margin-top:4px;padding-left:9px;border-left:2px solid '+c+'">'+esc(items[j])+'</div>';
  h+='</div>';}
 var lv=function(title,arr){if(!arr||!arr.length)return '';var out='<div class=mut style="font-size:11px;margin-top:10px">'+title+'</div><div style="display:flex;gap:6px;flex-wrap:wrap;margin-top:4px">';
  for(var k=0;k<arr.length;k++)out+='<span class=card style="padding:3px 8px;font-size:11.5px">'+s+arr[k].price+' <span class=mut>'+esc(arr[k].label)+'</span></span>';
  return out+'</div>';};
 h+=lv('support',r.support)+lv('resistance',r.resistance);
 if(r.targets&&r.targets.length){h+='<div class=mut style="font-size:11px;margin-top:10px">targets</div><div style="display:flex;gap:6px;flex-wrap:wrap;margin-top:4px">';
  for(var t2=0;t2<r.targets.length;t2++){var tg=r.targets[t2];h+='<span class=card style="padding:3px 8px;font-size:11.5px">'+s+tg.price+' <span class="up">'+(tg.upside_pct!=null?'+'+tg.upside_pct+'%':'')+'</span></span>';}
  h+='</div>';}
 if(r.catalysts&&r.catalysts.length){h+='<div class=mut style="font-size:11px;margin-top:10px">catalysts</div>';
  for(var c2=0;c2<r.catalysts.length;c2++)h+='<div style="font-size:12.5px;margin-top:3px">'+esc(r.catalysts[c2].headline)+'</div>';}
 if(r.time_horizon)h+='<div class=mut style="font-size:12px;margin-top:10px">'+esc(r.time_horizon)+'</div>';
 if(r.evidence&&r.evidence.length){h+='<details style="margin-top:10px"><summary class=mut style="font-size:11.5px;cursor:pointer">evidence ('+r.evidence.length+')</summary>';
  for(var e=0;e<r.evidence.length;e++){var ev=r.evidence[e];
   h+='<div style="font-size:11.5px;margin-top:5px"><span class=mut>'+esc(ev.metric)+'</span> = '+esc(JSON.stringify(ev.value))+' <span class=mut>via '+esc(ev.source)+'</span></div>';}
  h+='</details>';}
 return h+'</div>';}
function newsHtml(n,s){if(!n||!n.length)return '<div class=sec>news</div><div class=mut style="font-size:13px">no recent headlines</div>';
 return '<div class=sec>news &amp; sentiment</div>'+n.map(x=>{var c=x.score>0.1?'bg-up':(x.score<-0.1?'bg-dn':'bg-mut');return '<div style="display:flex;gap:9px;align-items:flex-start;margin:8px 0"><span class="badge '+c+'" style="white-space:nowrap;margin-top:1px">'+x.label.replace('_',' ')+'</span><div><div style="font-size:13px;line-height:1.4">'+esc(x.title)+'</div><div class=mut style="font-size:10px">'+(x.when||'')+'</div></div></div>'}).join('');}
var WLT=null;
function wlSearch(sfx){sfx=sfx||'';clearTimeout(WLT);var qi=document.getElementById('wlq'+sfx);if(!qi)return;var q=qi.value.trim();if(q.length<2){hide('wlsug'+sfx);return}
 WLT=setTimeout(function(){api('/v2/api/search?q='+encodeURIComponent(q)).then(r=>{var h=(r.j||[]).map(x=>'<a onclick="addWL(\''+x.symbol+'\',\''+x.market+'\')"><b>'+x.symbol+'</b>&nbsp;<span class=mut style="font-size:11px">'+x.market+' · '+x.name+'</span></a>').join('');
 document.getElementById('wlsug'+sfx).innerHTML=h||'<a class=mut>no match</a>';show('wlsug'+sfx);})},250)}
function addWL(sym,mkt){['','2'].forEach(function(s){var q=document.getElementById('wlq'+s);if(q)q.value='';var sg=document.getElementById('wlsug'+s);if(sg)sg.classList.add('hide')});api('/v2/api/watchlist',{method:'POST',body:JSON.stringify({symbol:sym,market:mkt})}).then(function(){loadWL();toast('✅ added '+sym+' to watchlist')})}
function delWL(sym,mkt){api('/v2/api/watchlist/'+sym+'?market='+mkt,{method:'DELETE'}).then(loadWL)}
function setAlert(sym,mkt,px){var v=prompt('Alert when '+sym+' crosses price (now '+px+'):',(px*1.05).toFixed(2));if(!v)return;var val=parseFloat(v);if(!(val>0))return;
 api('/v2/api/alerts',{method:'POST',body:JSON.stringify({symbol:sym,market:mkt,kind:(val>=px?'above':'below'),value:val})}).then(function(){loadWL();toast('⏰ alert set: '+sym+' '+(val>=px?'above':'below')+' '+val)})}
function delAlert(id){api('/v2/api/alerts/'+id,{method:'DELETE'}).then(loadWL)}
function doReset(){if(!confirm('Reset the paper book to a clean ₹1,00,000? This clears ALL positions, trades and history. (Paper money — cannot be undone.)'))return;
 var m=document.getElementById('resetmsg');if(m)m.textContent='resetting…';
 api('/v2/api/reset',{method:'POST'}).then(function(r){if(r.ok){if(m)m.textContent='✅ book reset to ₹'+INR.format(r.j.budget||100000);toast('✅ paper book reset — clean slate');refresh();}else{if(m)m.textContent='⚠ '+(r.j.error||'failed');}});}
function doBuy(sym,mkt){if(!confirm('Paper buy '+sym+' at the live price?'))return;
 api('/v2/api/buy',{method:'POST',body:JSON.stringify({symbol:sym,market:mkt})}).then(function(r){
  if(r.ok){toast('✅ Bought '+r.j.symbol+' ×'+r.j.qty+' @ '+(mkt=='IN'?'₹':'$')+r.j.entry);renderStock(sym,mkt,'detail');refresh();}
  else toast('⚠ '+(r.j.error||'buy failed'));});}
function doSell(sym,mkt){if(!confirm('Sell your '+sym+' position at the live price?'))return;
 api('/v2/api/sell',{method:'POST',body:JSON.stringify({symbol:sym,market:mkt})}).then(function(r){
  if(r.ok){toast('✅ Sold '+r.j.symbol+' ('+(r.j.pnl_pct>=0?'+':'')+r.j.pnl_pct+'%)');renderStock(sym,mkt,'detail');refresh();}
  else toast('⚠ '+(r.j.error||'sell failed'));});}
function loadWL(){api('/v2/api/watchlist').then(r=>{var d=r.j||{};
 var h=(d.watch||[]).filter(w=>inMkt(w.market)).map(function(w){
  var up=w.chg>0,dn=w.chg<0,cl=up?'up':(dn?'dn':'mut'),a=up?'▲ ':(dn?'▼ ':'');
  var chg=(w.chg==null)?'—':(a+(up?'+':'')+w.chg+'%');
  return '<div class=wlrow onclick="stock(\''+w.symbol+'\',\''+w.market+'\')">'
   +'<div class=wlL><div class=wlsym>'+w.symbol+'</div><div class=wlex>'+(w.market=='IN'?'NSE':w.market)+'</div></div>'
   +'<div class=wlR><div class=wlltp>'+w.ccy+(w.ccy=='₹'?INR:USD).format(w.price)+'</div><div class="wlch '+cl+'">'+chg+'</div></div>'
   +'<span class=wldel onclick="event.stopPropagation();delWL(\''+w.symbol+'\',\''+w.market+'\')" title="remove">×</span>'
   +'</div>';}).join('');
 var wlEmpty='<div class=mut style="font-size:12px;padding:8px 0">nothing watched yet — search a symbol above</div>';
 ['wl','wl2'].forEach(function(id){var e=document.getElementById(id);if(e)e.innerHTML=h||wlEmpty});
 var al=(d.alerts||[]).filter(a=>inMkt(a.market)).slice(0,10);
 var alh=al.length?al.map(a=>'<span class=chip style="font-size:11px;'+(a.active?'':'opacity:.55')+'">'+(a.active?'●':'✓')+' '+a.symbol+' '+(a.kind=='pct'?('±'+a.value+'%'):(a.kind+' '+a.ccy+a.value))+(a.triggered_price?(' → hit '+a.triggered_price):'')+' <b style="cursor:pointer;padding-left:3px" onclick="delAlert('+a.id+')">✕</b></span>').join(' '):'<span class=mut style="font-size:11px">none — use ⏰ on any stock</span>';
 ['alerts','alerts2'].forEach(function(id){var e=document.getElementById(id);if(e)e.innerHTML=alh});});}
function loadMovers(){api('/v2/api/movers').then(r=>{var d=r.j||{};
 var row=(x,m)=>'<div class=mvrow style="display:grid;grid-template-columns:minmax(0,1fr) auto auto;gap:9px;align-items:center;padding:6px 0;border-bottom:1px solid var(--line)"><b style="cursor:pointer;font-size:12px;overflow:hidden;text-overflow:ellipsis" onclick="stock(\''+x.symbol+'\',\''+m+'\')">'+x.symbol+'</b><span class=num style="font-size:12px;color:var(--mut)">'+x.ccy+(x.ccy=='₹'?INR:USD).format(x.price)+'</span>'+pill(x.chg)+'</div>';
 document.getElementById('movers').innerHTML=['IN','US'].filter(inMkt).map(m=>{var v=d[m]||{};if(!(v.up||[]).length&&!(v.down||[]).length)return '';
  return '<div class=card><div class=mut style="font-size:11px;margin-bottom:3px">'+(m=='IN'?'India':'US')+' · top gainers</div>'+(v.up||[]).slice(0,5).map(x=>row(x,m)).join('')+'</div>'
       +'<div class=card><div class=mut style="font-size:11px;margin-bottom:3px">'+(m=='IN'?'India':'US')+' · top losers</div>'+(v.down||[]).slice(0,5).map(x=>row(x,m)).join('')+'</div>'}).join('');});}
function loadHealth(){api('/v2/api/health').then(r=>{var d=r.j||{};var el=document.getElementById('healthbar');
 if(d.ok){el.classList.add('hide');return}
 var bad=(d.checks||[]).filter(c=>!c.ok).map(c=>c.name+' ('+c.detail+')');
 el.textContent='⚠ system check: '+bad.join(' · ');el.classList.remove('hide');});}
function loadActivity(){api('/v2/api/orders?limit=80').then(r=>{var os=(r.j||[]).filter(o=>o.today&&inMkt(o.market));
 var h=os.map(o=>{var right=o.side=='SELL'?('<span style="display:inline-flex;gap:10px;align-items:center"><span class=mut style="font-size:11px">'+(o.entry?o.ccy+o.entry+' → ':'')+o.ccy+o.price+'</span>'+pill(o.pnl!=null?o.pnl:0)+'</span>'):('<span class=mut style="font-size:11px">@ '+o.ccy+o.price+'</span>');
  return '<div class=lrow style="display:grid;grid-template-columns:auto minmax(0,1fr) auto;gap:10px;align-items:center;cursor:pointer" onclick="stock(\''+o.symbol+'\',\''+o.market+'\')"><span class="badge '+(o.side=='BUY'?'bg-inf':(o.pnl>0?'bg-up':'bg-dn'))+'">'+o.side+'</span><b style="overflow:hidden;text-overflow:ellipsis">'+o.symbol+'</b>'+right+'</div>'}).join('');
 var _a=document.getElementById('activity');if(_a)_a.innerHTML=h||'<div class=mut style="font-size:12px;padding:8px 0">nothing bought or sold yet today</div>';});}
function loadRadar(){api('/v2/api/watch').then(r=>{var it=(r.j||[]).filter(x=>inMkt(x.market)).slice(0,10);
 document.getElementById('radar').innerHTML=it.length?it.map(x=>'<span class=chip style="cursor:pointer;margin:2px" onclick="stock(\''+x.symbol+'\',\''+x.market+'\')"><b>'+x.symbol+'</b> <span class=mut style="font-size:10px">'+x.market+' · '+x.badge+'</span></span>').join(' '):'<span class=mut style="font-size:12px">no candidates on the radar right now</span>';});}
function loadWatch(){var el=document.getElementById('watchlist');if(!el)return;api('/v2/api/watch').then(function(r){var it=(r.j||[]).filter(function(w){return inMkt(w.market)});
 el.innerHTML=it.length?it.map(function(w){var s=w.market=='IN'?'₹':'$';return '<div class=lrow style="cursor:pointer" onclick="stock(\''+w.symbol+'\',\''+w.market+'\')"><div style="display:flex;gap:8px;align-items:center"><b>'+w.symbol+'</b><span class="badge '+((w.strategy||'').indexOf('gap')>=0?'bg-inf':'bg-warn')+'">'+w.badge+'</span></div><div><span class=num>'+s+w.live+'</span> <span class="'+col(w.chg)+'" style="font-size:13px">'+sgn(w.chg)+'%</span></div></div>';}).join(''):'<div class=mut style="font-size:12px;padding:8px 0">no candidates on the radar right now</div>';});}
function loadHeatmap(){var el=document.getElementById('heatmap');if(!el)return;api('/v2/api/sectors').then(r=>{var rows=((r.j||{})['IN']||[]);
 if(!rows.length){el.innerHTML='<span class=mut style="font-size:12px">warming…</span>';return;}
 var mx=Math.max(1,Math.max.apply(null,rows.map(x=>Math.abs(x.chg))));
 el.innerHTML=rows.slice(0,8).map(function(x){
   var up=x.chg>=0,a=Math.min(0.9,0.15+Math.abs(x.chg)/mx*0.75);
   var bg=up?'rgba(63,164,91,'+a+')':'rgba(227,77,63,'+a+')';
   return '<div class=htile style="background:'+bg+'" title="'+(x.top||[]).join(', ')+'"><div class=hs>'+x.sector+'</div><div class="hc '+(up?'up':'dn')+'">'+(up?'+':'')+x.chg+'%</div><div class=hn>'+x.n+' stocks</div></div>';
 }).join('');});}
function loadIndices(){var el=document.getElementById('indexbar');if(!el)return;api('/v2/api/indices').then(r=>{var xs=(r.j||[]);
 if(!xs.length){el.style.display='none';return;}
 el.style.display='flex';
 el.innerHTML=xs.map(function(x){var up=x.chg>=0;return '<div class=idx><b>'+x.name+'</b> <span class=iv>'+INR.format(x.last)+'</span> <span class="ic '+(up?'up':'dn')+'">'+(up?'▲ +':'▼ ')+x.chg+'%</span></div>';}).join('');});}
function loadCatalysts(){var el=document.getElementById('catalysts');if(!el)return;api('/v2/api/catalysts').then(r=>{var cs=(r.j||[]).slice(0,8);
 if(!cs.length){el.innerHTML='<span class=mut>no fresh filings yet</span>';return;}
 var bc={results:'bg-inf',order:'bg-up',corp_action:'bg-warn'};
 el.innerHTML=cs.map(c=>'<div class=lrow style="cursor:pointer;padding:8px 2px" onclick="stock(\''+c.symbol+'\',\'IN\')"><div style="min-width:0"><b>'+c.symbol+'</b> <span class="badge '+(bc[c.cat]||'bg-mut')+'" style="font-size:9px">'+c.kind+'</span><div class=mut style="font-size:11px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:210px">'+(c.subject||'')+'</div></div></div>').join('');});}
function toast(t){var e=document.createElement('div');e.className='toastmsg';e.textContent=t;document.body.appendChild(e);setTimeout(function(){e.remove()},6500)}
boot();setInterval(()=>{if(ME){loadHealth();loadIndices();if(cur=='home'){loadHome();loadWL();loadMovers();loadRadar();loadActivity();loadCatalysts()}if(cur=='positions')loadPos()}},20000);
setInterval(()=>{if(ME)loadTicker()},6000);
</script></body></html>"""
