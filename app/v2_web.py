"""v2 web UI - clean, mobile-first dashboard for the v2 paper engines.

Self-contained FastAPI router mounted at /v2/. Reads the paper book
(var/v2_paper.db) and live quotes (latest_quotes) READ-ONLY and serves a single
responsive page + JSON APIs. Zero coupling to the legacy dashboard.
"""
from __future__ import annotations

import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone

import asyncio
import json as _jsonmod

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from . import v2_engine as eng

MAIN_DB = os.environ.get("OPENSTOCKS_DB", "/opt/opentrade/var/trading_agent.db")
V2_DB = os.environ.get("V2_PAPER_DB", "/opt/opentrade/var/v2_paper.db")
LIVE_SOURCE = {"IN": "upstox-live", "US": "alpaca-iex-live"}

router = APIRouter(prefix="/v2")
_panel_cache: dict = {}


def _ro(path):
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)


def _live_map(market):
    try:
        con = _ro(MAIN_DB)
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
        val = bool(eng.regime_ok(mdf, d[-1], eng.DEFAULTS["regime_lookback"])) if d else False
        _regime_cache[market] = (time.time(), val)
    except Exception:
        _regime_cache[market] = (time.time(), False)
    finally:
        _regime_loading.discard(market)


def _regime(market):
    """Non-blocking: return cached regime instantly; refresh in a background
    thread so the dashboard never waits on the heavy panel load."""
    c = _regime_cache.get(market)
    if c and time.time() - c[0] < 1800:
        return c[1]
    if market not in _regime_loading:
        _regime_loading.add(market)
        import threading
        threading.Thread(target=_regime_bg, args=(market,), daemon=True).start()
    return c[1] if c else None


def _markets(v2):
    try:
        return v2.execute("SELECT market,budget FROM v2_book ORDER BY market").fetchall()
    except Exception:
        return []


def _market_stats(v2, market, budget, live):
    today_s = datetime.now(IST).date().isoformat()
    pos = v2.execute("SELECT symbol,entry_price,shares FROM v2_positions WHERE market=?", (market,)).fetchall()
    realised = v2.execute("SELECT COALESCE(SUM(pnl),0) FROM v2_trades WHERE market=?", (market,)).fetchone()[0] or 0.0
    realised_today = v2.execute("SELECT COALESCE(SUM(pnl),0) FROM v2_trades WHERE market=? AND exit_date=?",
                                (market, today_s)).fetchone()[0] or 0.0
    mtm = unreal = 0.0
    for sym, entry, shares in pos:
        p = live.get(sym, {}).get("price", entry)
        mtm += shares * p
        unreal += (p - entry) * shares
    cash = budget - sum(r[1] * r[2] for r in pos) + realised
    rets = [r[0] for r in v2.execute("SELECT return_pct FROM v2_trades WHERE market=?", (market,))]
    wins = [r for r in rets if r > 0]
    loss = [r for r in rets if r <= 0]
    pf = (sum(wins) / abs(sum(loss))) if loss else (len(wins) and 9.9)
    return dict(market=market, budget=budget, equity=cash + mtm, cash=cash, deployed=mtm,
                deploy_pct=round(mtm / budget * 100) if budget else 0,
                today_pnl=realised_today + unreal, overall_pnl=realised + unreal,
                positions=len(pos), trades=len(rets),
                win=(len(wins) / len(rets) * 100) if rets else 0.0, pf=round(pf or 0, 2))


@router.get("/api/overview")
def api_overview():
    v2 = _ro(V2_DB)
    live = {"IN": _live_map("IN"), "US": _live_map("US")}
    markets = []
    for market, budget in _markets(v2):
        s = _market_stats(v2, market, budget, live[market])
        markets.append({"market": s["market"], "ccy": "₹" if market == "IN" else "$",
                        "budget": round(s["budget"]), "equity": round(s["equity"]),
                        "cash": round(s["cash"]), "deployed": round(s["deployed"]), "deploy_pct": s["deploy_pct"],
                        "today_pnl": round(s["today_pnl"], 2), "overall_pnl": round(s["overall_pnl"], 2),
                        "today_pct": round(s["today_pnl"] / s["budget"] * 100, 2) if s["budget"] else 0,
                        "overall_pct": round(s["overall_pnl"] / s["budget"] * 100, 2) if s["budget"] else 0,
                        "positions": s["positions"], "trades": s["trades"], "win": round(s["win"]), "pf": s["pf"]})
    v2.close()
    return JSONResponse(dict(markets=markets, regime={"IN": _regime("IN"), "US": _regime("US")},
                             as_of=datetime.now(IST).strftime("%H:%M:%S IST")))


IST = timezone(timedelta(hours=5, minutes=30))


def _rw():
    c = sqlite3.connect(V2_DB, timeout=30)
    c.execute("PRAGMA busy_timeout=8000")
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
    for pid, market, strat, sym, entry, shares, stop, target, trail, peak, edate, oat in v2.execute(
            "SELECT id,market,strategy,symbol,entry_price,shares,stop,target,trail,peak,entry_date,opened_at "
            "FROM v2_positions"):
        p = live.get(market, {}).get(sym, {}).get("price", entry)
        tstop = max(stop, peak * (1 - trail)) if trail else stop
        head = (p - tstop) / (peak - tstop) if peak > tstop else 0
        out.append(dict(id=pid, market=market, ccy="₹" if market == "IN" else "$", strategy=strat, symbol=sym,
                        entry=round(entry, 2), live=round(p, 2), qty=round(shares, 2), value=round(p * shares, 2),
                        pnl=round((p / entry - 1) * 100, 2), pnl_amt=round((p - entry) * shares, 2),
                        stop=round(tstop, 2), trail=bool(trail), today=str(edate) == today_s,
                        since=_ist(oat or edate), headroom=round(max(0, min(1, head)) * 100)))
    v2.close()
    out.sort(key=lambda x: -x["pnl"])
    return JSONResponse(out)


@router.get("/api/engine-status")
def api_engine_status():
    try:
        from . import v2_live
        st = v2_live.status()
    except Exception:
        st = {}
    sessions = {}
    for m in ("IN", "US"):
        try:
            from . import market_regions
            sessions[m] = market_regions.market_session_for_region(m).get("is_open", False)
        except Exception:
            sessions[m] = False
    return JSONResponse(dict(engine=st, market_open=sessions))


def _stream_payload():
    v2 = _ro(V2_DB)
    live = {"IN": _live_map("IN"), "US": _live_map("US")}
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
    return dict(markets=markets, positions=positions, as_of=datetime.now(IST).strftime("%H:%M:%S IST"))


@router.get("/api/stream")
async def api_stream():
    async def gen():
        while True:
            try:
                payload = await asyncio.to_thread(_stream_payload)
                yield "data: " + _jsonmod.dumps(payload) + "\n\n"
            except Exception:
                yield "data: {}\n\n"
            await asyncio.sleep(2.5)
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
                           pnl=round(ret, 2), pnl_amt=round(pnl), reason=reason, when=_ist(xdate), ts=str(xdate)))
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
        for strat in ("gap_momentum", "swing_meanrev"):
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
                badge = f"gap {round(conv*15)}%" if strat == "gap_momentum" else f"dip · {conv:.2f}"
                chg = ((lq.get("price", 0) / lq["prev"] - 1) * 100) if lq.get("prev") else 0
                out.append(dict(symbol=sym, market=market, ccy="₹" if market == "IN" else "$", strategy=strat,
                                badge=badge, live=round(lq.get("price", 0), 2), chg=round(chg, 2)))
    v2.close()
    return JSONResponse(out[:24])


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
        out.append(dict(market=market, ccy="₹" if market == "IN" else "$",
                        overall_pnl=round(s["overall_pnl"]), today_pnl=round(s["today_pnl"]),
                        win=round(s["win"]), pf=s["pf"], trades=s["trades"], deploy_pct=s["deploy_pct"],
                        avg_win=round(sum(wins) / len(wins), 2) if wins else 0,
                        avg_loss=round(sum(loss) / len(loss), 2) if loss else 0, curve=curve))
    v2.close()
    return JSONResponse(out)


@router.get("/api/stock/{symbol}")
def api_stock(symbol: str, market: str = "IN"):
    symbol = symbol.upper()
    live = _live_map(market).get(symbol, {})
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
        entry = round(close * 1.005, 2); stop = round(entry - 2 * atr, 2); target = round(entry + 3.5 * atr, 2)
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
        return JSONResponse(dict(symbol=symbol, market=market, live=round(live.get("price", close), 2),
                                 verdict=verdict, score=round(conv, 2), entry=entry, stop=stop,
                                 target=target, rr=rr, regime=_regime(market), factors=factors,
                                 news=_news(symbol)))
    except Exception as exc:
        return JSONResponse(dict(symbol=symbol, error=str(exc)[:120], news=_news(symbol)))


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
function loadStats(){fetch('/v2/api/stats').then(r=>r.json()).then(d=>{document.getElementById('statlist').innerHTML=d.map(s=>`<div class=pos><div class=row><b>${s.market} · ${s.strategy.indexOf('gap')>=0?'gap':'swing'}</b><span class="${col(s.ret)}">${sgn(s.ret)}%</span></div><div class=grid style="margin-top:10px"><div class=card><div class=mut style="font-size:11px">win rate</div><div class=tile><div class=v>${s.win}%</div></div></div><div class=card><div class=mut style="font-size:11px">profit factor</div><div class=tile><div class=v>${s.pf}</div></div></div><div class=card><div class=mut style="font-size:11px">avg win</div><div class="v up" style="font-size:17px">${sgn(s.avg_win)}%</div></div><div class=card><div class=mut style="font-size:11px">avg loss</div><div class="v dn" style="font-size:17px">${s.avg_loss}%</div></div></div><div class=mut style="font-size:11px;margin-top:8px">${s.trades} closed trades</div></div>`).join('');});}
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
    <div class=seg id=mkt><b data-m=BOTH class=on onclick="setMkt('BOTH')">Both</b><b data-m=IN onclick="setMkt('IN')">India</b><b data-m=US onclick="setMkt('US')">US</b></div>
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
function loadStats(){api('/v2/api/stats').then(r=>{document.getElementById('statlist').innerHTML=r.j.filter(s=>inMkt(s.market)).map(s=>`<div class=raise><div class=row><b>${s.market} · ${s.strategy.indexOf('gap')>=0?'gap':'swing'}</b><span class="${col(s.ret)}">${sgn(s.ret)}%</span></div><div class=grid style="margin-top:10px"><div class=card><div class=mut style="font-size:11px">win rate</div><div style="font-size:18px;font-weight:600">${s.win}%</div></div><div class=card><div class=mut style="font-size:11px">profit factor</div><div style="font-size:18px;font-weight:600">${s.pf}</div></div><div class=card><div class=mut style="font-size:11px">avg win</div><div class="up" style="font-size:17px;font-weight:600">${sgn(s.avg_win)}%</div></div><div class=card><div class=mut style="font-size:11px">avg loss</div><div class="dn" style="font-size:17px;font-weight:600">${s.avg_loss}%</div></div></div><div class=mut style="font-size:11px;margin-top:8px">${s.trades} closed trades</div></div>`).join('')||'<div class=skel>no data</div>';});}
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
</style></head><body>

<div id=login class=hide><h1>OpenStocks</h1><p class=mut style="margin:0 0 22px">AI trading desk · sign in</p>
 <div class=field><label>username</label><input id=u autocomplete=username></div>
 <div class=field><label>password</label><input id=pw type=password autocomplete=current-password></div>
 <button class=pri style="width:100%;margin-top:8px" onclick=doLogin()>Sign in</button>
 <div id=lerr class=dn style="font-size:13px;margin-top:10px"></div>
 <p class=mut style="font-size:13px;margin-top:18px">No account? <b style="cursor:pointer;color:var(--inf)" onclick="alert('Ask an admin to create your account — sign-up with approval is coming next.')">Request access</b></p></div>

<div id=app class="app hide">
 <nav class=side><div class=b>OpenStocks</div>
  <a data-t=home onclick="go('home')"><svg viewBox="0 0 24 24"><path d="M3 11l9-8 9 8M5 10v10h14V10"/></svg>Home</a>
  <a data-t=positions onclick="go('positions')"><svg viewBox="0 0 24 24"><rect x=3 y=6 width=18 height=13 rx=2/><path d="M3 10h18"/></svg>Portfolio</a>
  <a data-t=orders onclick="go('orders')"><svg viewBox="0 0 24 24"><path d="M4 6h16M4 12h16M4 18h10"/></svg>Orders</a>
  <a data-t=analyze onclick="go('analyze')"><svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><path d="M21 21l-4-4"/></svg>Analyze</a>
  <a data-t=account onclick="go('account')"><svg viewBox="0 0 24 24"><circle cx="12" cy="8" r="4"/><path d="M4 21c0-4 4-6 8-6s8 2 8 6"/></svg>Account</a>
 </nav>
 <div class=main>
  <div class=top><span class=live><span class=dot></span><span id=clock>live</span></span>
   <div style="display:flex;align-items:center;gap:10px"><div class=seg id=mkt><b data-m=BOTH class=on onclick="setMkt('BOTH')">Both</b><b data-m=IN onclick="setMkt('IN')">India</b><b data-m=US onclick="setMkt('US')">US</b></div>
    <div class=prof id=avatar onclick="document.getElementById('pm').classList.toggle('hide')">U</div></div></div>
  <div id=pm class="menu hide">
   <a onclick="go('account');document.getElementById('pm').classList.add('hide')">Account &amp; settings</a>
   <a onclick=doLogout()>Log out</a></div>

  <div id=home class="tab on">
   <div class=row><div class=mut style="font-size:12px">your paper balance</div><span id=modeb class="modepill bg-warn">paper</span></div>
   <div class=hero id=pv>—</div><div id=ppnl style="font-size:13px" class=mut>&nbsp;</div>
   <div class=chips id=regime></div>
   <div class=sec><span>engine performance</span><span class=mut style="font-size:12px;font-weight:400">house strategies</span></div>
   <div class=grid id=engines></div>
   <div class=sec><span>open positions</span><span class=mut style="font-size:12px;font-weight:400" id=posn></span></div>
   <div id=homepos></div></div>

  <div id=positions class=tab>
   <div class=sec><span>portfolio</span><span id=postot style="font-size:13px"></span></div>
   <div class=seg style="margin-bottom:10px"><b id=sbpos class=on onclick="subPos('pos')">Positions · today</b><b id=sbhold onclick="subPos('hold')">Holdings</b></div>
   <div id=poslist class=skel>loading…</div></div>

  <div id=orders class=tab>
   <div class=sec><span>orders &amp; activity</span><span id=ordtot class=mut style="font-size:12px;font-weight:400"></span></div>
   <div id=ordlist class=skel>loading…</div></div>

  <div id=analyze class=tab>
   <div class=sec>analyse a stock</div>
   <div style="display:flex;gap:8px;margin-bottom:8px"><input id=qsym placeholder="symbol e.g. RELIANCE / AAPL" style="flex:1" onkeydown="if(event.key=='Enter')doAnalyze()">
    <select id=qmkt style="width:88px"><option value=IN>India</option><option value=US>US</option></select>
    <button class=pri onclick=doAnalyze()>Go</button></div>
   <div id=ares></div></div>

  <div id=account class=tab></div>
  <div id=detail class=tab></div>
 </div>
</div>

<nav class=nav>
<a data-t=home class=on onclick="go('home')"><svg viewBox="0 0 24 24"><path d="M3 11l9-8 9 8M5 10v10h14V10"/></svg>home</a>
<a data-t=positions onclick="go('positions')"><svg viewBox="0 0 24 24"><rect x=3 y=6 width=18 height=13 rx=2/><path d="M3 10h18"/></svg>portfolio</a>
<a data-t=orders onclick="go('orders')"><svg viewBox="0 0 24 24"><path d="M4 6h16M4 12h16M4 18h10"/></svg>orders</a>
<a data-t=analyze onclick="go('analyze')"><svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><path d="M21 21l-4-4"/></svg>analyze</a>
<a data-t=account onclick="go('account')"><svg viewBox="0 0 24 24"><circle cx="12" cy="8" r="4"/><path d="M4 21c0-4 4-6 8-6s8 2 8 6"/></svg>account</a>
</nav>

<script>
var INR=new Intl.NumberFormat('en-IN'),USD=new Intl.NumberFormat('en-US'),cur='home',MKT='BOTH',ME=null,MODE='paper',ACC=null;
function sgn(x){return (x>0?'+':'')+x}function col(x){return x>0?'up':(x<0?'dn':'mut')}
function show(i){document.getElementById(i).classList.remove('hide')}function hide(i){document.getElementById(i).classList.add('hide')}
function api(u,o){return fetch(u,Object.assign({headers:{'Content-Type':'application/json'}},o||{})).then(r=>r.json().catch(()=>({})).then(j=>({ok:r.ok,j:j})))}
function setMkt(m){MKT=m;document.querySelectorAll('#mkt b').forEach(b=>b.classList.toggle('on',b.dataset.m==m));refresh()}
function go(t){cur=t;['home','positions','orders','analyze','account','detail'].forEach(x=>document.getElementById(x).classList.toggle('on',x==t));
 document.querySelectorAll('.nav a,.side a').forEach(a=>a.classList.toggle('on',a.dataset.t==t));
 if(t=='positions')loadPos();if(t=='orders')loadOrders();if(t=='account')loadAccount();window.scrollTo(0,0)}
function inMkt(m){return MKT=='BOTH'||m==MKT}
function boot(){api('/api/auth/me').then(r=>{var u=r.j.user||r.j;if(r.ok&&u&&u.username){ME=u;MODE=(u.signal_execution_mode||'paper');hide('login');show('app');document.getElementById('avatar').textContent=(u.username[0]||'U').toUpperCase();loadAccountData();refresh();startStream();}else{show('login');hide('app');}}).catch(()=>{show('login');hide('app')})}
var ES=null;
function startStream(){if(ES)return;try{ES=new EventSource('/v2/api/stream');ES.onmessage=function(e){try{applyStream(JSON.parse(e.data||'{}'))}catch(x){}};ES.onerror=function(){};}catch(e){}}
function applyStream(d){if(!d||!d.markets)return;document.getElementById('clock').textContent=d.as_of||document.getElementById('clock').textContent;
 if(cur=='home'){var ms=d.markets.filter(m=>inMkt(m.market));var pv=document.getElementById('pv');if(pv)pv.innerHTML=ms.map(m=>fmtc(m.ccy,m.equity)).join('  ·  ')||'—';var pp=document.getElementById('ppnl');if(pp)pp.innerHTML=ms.map(m=>'today '+pnlS(m.ccy,m.today_pnl,m.today_pct)).join(' &nbsp;·&nbsp; ');}
 (d.positions||[]).forEach(p=>{var el=document.getElementById('px_'+p.id);if(el){var old=parseFloat(el.getAttribute('data-v'));el.textContent=p.ccy+p.live;if(old&&old!=p.live){el.style.color=(p.live>old?'var(--up)':'var(--dn)');setTimeout(function(){el.style.color=''},450)}el.setAttribute('data-v',p.live)}
  var pe=document.getElementById('pl_'+p.id);if(pe){pe.firstChild&&(pe.textContent=sgn(p.pnl)+'% · '+(p.pnl_amt<0?'-':'+')+p.ccy+(p.ccy=='₹'?INR:USD).format(Math.abs(p.pnl_amt)));pe.className=col(p.pnl);pe.style.fontSize='12px';}});}
function doLogin(){document.getElementById('lerr').textContent='';api('/api/auth/login',{method:'POST',body:JSON.stringify({username:document.getElementById('u').value,password:document.getElementById('pw').value})}).then(r=>{if(r.ok&&!r.j.detail){boot()}else{document.getElementById('lerr').textContent=r.j.detail||'Login failed'}})}
function doLogout(){api('/api/auth/logout',{method:'POST'}).then(()=>{ME=null;show('login');hide('app')})}
function loadAccountData(){api('/api/account').then(r=>{ACC=r.j;renderBalance()})}
function balOf(){try{var p=ACC&&(ACC.paper||{});var cb=p.cash_by_market||ACC.paper_cash_by_market||{};var IN=cb.IN!=null?cb.IN:(p.india_cash||0),US=cb.US!=null?cb.US:(p.us_cash||0);return {IN:+IN||0,US:+US||0}}catch(e){return{IN:0,US:0}}}
function renderBalance(){document.getElementById('modeb').textContent=MODE;document.getElementById('modeb').className='modepill '+(MODE=='live'?'bg-inf':'bg-warn');}
function refresh(){renderBalance();if(cur=='home')loadHome();if(cur=='positions')loadPos();if(cur=='orders')loadOrders()}
function engCard(e){return `<div class=card><div class=row><span class=mut style="font-size:12px">${e.market} · ${e.strategy.indexOf('gap')>=0?'gap':'swing'}</span><span class="${col(e.ret)}" style="font-size:13px">${sgn(e.ret)}%</span></div><div class=mut style="font-size:11px;margin-top:3px">win ${e.win}% · PF ${e.pf} · ${e.positions} pos</div></div>`}
function posCard(p){var c=p.headroom>40?'var(--up)':(p.headroom>15?'var(--warn)':'var(--dn)');var s=p.market=='IN'?'₹':'$';var amt=(p.market=='IN'?'₹':'$')+(p.market=='IN'?INR:USD).format(Math.abs(p.pnl_amt));
 return `<div class=pos><div class=row><div style="display:flex;gap:8px;align-items:center;cursor:pointer" onclick="stock('${p.symbol}','${p.market}')"><b>${p.symbol}</b><span class="badge ${p.strategy.indexOf('gap')>=0?'bg-inf':'bg-mut'}">${p.strategy.indexOf('gap')>=0?'gap':'swing'}</span></div>
 <div style="text-align:right"><div class=num id=px_${p.id} data-v="${p.live}">${s}${p.live}</div><div id=pl_${p.id} style="font-size:12px" class="${col(p.pnl)}">${sgn(p.pnl)}% · ${p.pnl_amt<0?'-':'+'}${amt}</div></div></div>
 <div class=bar><i style="width:${p.headroom}%;background:${c}"></i></div>
 <div class=row style="margin-top:6px"><span class=mut style="font-size:10px">qty ${p.qty} · ${s}${(p.market=='IN'?INR:USD).format(p.value)} in · ${p.trail?'trail':'stop'} ${s}${p.stop}</span><button class="sm" onclick="exitPos(${p.id},'${p.symbol}')">Exit</button></div>
 <div class=mut style="font-size:10px;margin-top:3px">since ${p.since||''}</div></div>`}
function ordRow(o){var s=o.ccy,fmt=(o.ccy=='₹'?INR:USD);
 var right=(o.side=='SELL'&&o.pnl!=null)?('<div class="'+col(o.pnl)+'">'+sgn(o.pnl)+'%</div><div class=mut style="font-size:11px">'+(o.pnl_amt<0?'-':'+')+s+fmt.format(Math.abs(o.pnl_amt))+'</div>'):('<div class=mut style="font-size:12px">'+s+fmt.format(o.value)+'</div>');
 var tag=o.status=='open'?'<span class="badge bg-warn">open</span>':(o.reason?'<span class=mut style="font-size:10px">'+o.reason+'</span>':'');
 return '<div class=lrow style="cursor:default" onclick="stock(\''+o.symbol+'\',\''+o.market+'\')"><div><div style="display:flex;gap:7px;align-items:center"><span class="badge '+(o.side=='BUY'?'bg-inf':'bg-mut')+'">'+o.side+'</span><b>'+o.symbol+'</b>'+tag+'</div><div class=mut style="font-size:11px;margin-top:2px">'+o.qty+' @ '+s+o.price+' · '+o.when+'</div></div><div style="text-align:right">'+right+'</div></div>';}
function fmtc(ccy,n){return ccy+(ccy=='₹'?INR:USD).format(Math.round(n))}
function pnlS(ccy,v,p){return '<span class="'+col(v)+'">'+(v<0?'-':'+')+fmtc(ccy,Math.abs(v))+' ('+sgn(p)+'%)</span>'}
function mktCard(m){var nm=m.market=='IN'?'India ₹':'US $';return '<div class=card><div class=row><span class=mut style="font-size:12px">'+nm+'</span><span class=mut style="font-size:11px">'+m.deploy_pct+'% deployed</span></div>'
 +'<div style="font-size:20px;font-weight:600;margin-top:2px">'+fmtc(m.ccy,m.equity)+'</div>'
 +'<div style="font-size:12px;margin-top:4px">today '+pnlS(m.ccy,m.today_pnl,m.today_pct)+'</div>'
 +'<div style="font-size:12px">overall '+pnlS(m.ccy,m.overall_pnl,m.overall_pct)+'</div>'
 +'<div class=mut style="font-size:10px;margin-top:4px">budget '+fmtc(m.ccy,m.budget)+' · '+m.positions+' pos · cash '+fmtc(m.ccy,m.cash)+'</div></div>'}
function loadHome(){api('/v2/api/overview').then(r=>{var d=r.j;document.getElementById('clock').textContent=d.as_of;
 var ms=(d.markets||[]).filter(m=>inMkt(m.market));
 document.getElementById('pv').innerHTML=ms.map(m=>fmtc(m.ccy,m.equity)).join('  ·  ')||'—';
 document.getElementById('ppnl').innerHTML=ms.map(m=>'today '+pnlS(m.ccy,m.today_pnl,m.today_pct)).join(' &nbsp;·&nbsp; ');
 document.getElementById('regime').innerHTML=['IN','US'].filter(inMkt).map(m=>'<span class=chip><span style="color:'+(d.regime[m]?'var(--up)':'var(--warn)')+'">●</span> '+m+(d.regime[m]==null?' …':(d.regime[m]?' risk-on':' risk-off'))+'</span>').join('');
 document.getElementById('engines').innerHTML=ms.map(mktCard).join('');});
 api('/v2/api/positions').then(r=>{var ps=r.j.filter(p=>inMkt(p.market));document.getElementById('posn').textContent=ps.length+' open';document.getElementById('homepos').innerHTML=ps.slice(0,5).map(posCard).join('')||'<div class=skel>no open positions</div>';});}
function load(){loadHome()}
var POS=[],SUBPOS='pos';
function subPos(v){SUBPOS=v;document.getElementById('sbpos').className=(v=='pos'?'on':'');document.getElementById('sbhold').className=(v=='hold'?'on':'');renderPos();}
function renderPos(){var ps=POS.filter(p=>inMkt(p.market)).filter(p=>SUBPOS=='pos'?p.today:!p.today);
 var byc={};ps.forEach(p=>{byc[p.ccy]=(byc[p.ccy]||0)+p.pnl_amt});
 var t=Object.keys(byc).map(cc=>'<span class="'+col(byc[cc])+'">'+(byc[cc]<0?'-':'+')+cc+(cc=='₹'?INR:USD).format(Math.abs(Math.round(byc[cc])))+'</span>').join(' · ');
 document.getElementById('postot').innerHTML=(t||'—')+' P&L';
 document.getElementById('poslist').innerHTML=ps.map(posCard).join('')||'<div class=skel>'+(SUBPOS=='pos'?'nothing bought today':'no overnight holdings')+'</div>';}
function loadPos(){api('/v2/api/positions').then(r=>{POS=r.j;renderPos();})}
function loadOrders(){api('/v2/api/orders?limit=120').then(r=>{var os=r.j.filter(o=>inMkt(o.market));
 var b=os.filter(o=>o.side=='BUY').length,sl=os.filter(o=>o.side=='SELL').length;
 document.getElementById('ordtot').textContent=os.length?(b+' buys · '+sl+' sells'):'';
 document.getElementById('ordlist').innerHTML=os.map(ordRow).join('')||'<div class=skel>no orders yet</div>';});}
function exitPos(id,sym){if(!confirm('Exit '+sym+' at live price?'))return;api('/v2/api/positions/'+id+'/exit',{method:'POST'}).then(r=>{if(r.ok){loadPos();loadHome()}else{alert(r.j.error||'Failed')}})}
function doAnalyze(){var s=document.getElementById('qsym').value.trim().toUpperCase();if(!s)return;var m=document.getElementById('qmkt').value;document.getElementById('ares').innerHTML='<div class=skel>analysing '+s+'…</div>';renderStock(s,m,'ares')}
function loadAccount(){var el=document.getElementById('account');var u=ME||{};var b=balOf();
 el.innerHTML=`<div class=sec>account</div>
 <div class=raise><div style="display:flex;gap:12px;align-items:center"><div class=prof style="width:44px;height:44px;font-size:17px">${(u.username||'U')[0].toUpperCase()}</div><div><div style="font-weight:600">${u.username||'—'}</div><div class=mut style="font-size:12px">${u.role||'user'}${u.credits!=null?' · credits '+u.credits:''}</div></div></div></div>
 <div class=sec>trading mode</div>
 <div class=raise><div class=mut style="font-size:13px;margin-bottom:9px">Paper = simulated cash. Live = real orders via your broker.</div>
  <div class=toggle><b id=mp class="${MODE!='live'?'on':''}" onclick="setMode('paper')">Paper</b><b id=ml class="${MODE=='live'?'on':''}" onclick="setMode('live')">Live</b></div>
  <div id=modemsg class=mut style="font-size:12px;margin-top:9px"></div></div>
 <div class=sec>paper allocation</div>
 <div class=raise><div class=field><label>India cash (₹)</label><input id=cin type=number value="${Math.round(b.IN)||''}"></div><div class=field><label>US cash ($)</label><input id=cus type=number value="${Math.round(b.US)||''}"></div>
  <button class=pri onclick=saveCash()>Save allocation</button><div id=cashmsg class=mut style="font-size:12px;margin-top:9px"></div></div>
 <div class=sec>engine performance</div><div id=acctstats class=skel>loading…</div>
 ${(u.role=='admin')?'<div class=sec>admin · allocate paper money</div><div id=adminbox class=raise><div class=skel>loading users…</div></div>':''}
 <div class=sec>broker (for live)</div><div class=raise><div class=row style="padding:6px 0"><span>Upstox · India</span><button class=sm onclick="openBroker('upstox')">connect</button></div><div class=row style="padding:6px 0;border-top:1px solid var(--line)"><span>Alpaca · US</span><button class=sm onclick="openBroker('alpaca')">connect</button></div></div>`;
 api('/v2/api/stats').then(r=>{document.getElementById('acctstats').innerHTML=r.j.map(s=>`<div class=raise><div class=row><b>${s.market=='IN'?'India':'US'}</b><span class="${col(s.overall_pnl)}">overall ${s.overall_pnl<0?'-':'+'}${s.ccy}${(s.ccy=='₹'?INR:USD).format(Math.abs(s.overall_pnl))}</span></div><div class=grid style="margin-top:8px"><div class=card><div class=mut style="font-size:11px">win</div><div style="font-size:17px;font-weight:600">${s.win}%</div></div><div class=card><div class=mut style="font-size:11px">PF</div><div style="font-size:17px;font-weight:600">${s.pf}</div></div><div class=card><div class=mut style="font-size:11px">avg win</div><div class="up" style="font-size:16px;font-weight:600">${sgn(s.avg_win)}%</div></div><div class=card><div class=mut style="font-size:11px">avg loss</div><div class="dn" style="font-size:16px;font-weight:600">${s.avg_loss}%</div></div></div><div class=mut style="font-size:11px;margin-top:7px">${s.trades} closed · ${s.deploy_pct}% deployed</div></div>`).join('')||'<div class=skel>no data</div>';});
 if(u.role=='admin')api('/api/users').then(r=>{var us=(r.j.users||[]);document.getElementById('adminbox').innerHTML=us.map(x=>`<div style="padding:8px 0;border-bottom:1px solid var(--line)"><div class=row><b>${x.username}</b><span class=mut style="font-size:11px">${x.role||'user'}</span></div><div style="display:flex;gap:6px;margin-top:6px"><input id="ai_${x.id}" type=number placeholder="India ₹" style="padding:7px 9px"><input id="au_${x.id}" type=number placeholder="US $" style="padding:7px 9px"><button class=sm onclick="allocUser(${x.id})">set</button></div></div>`).join('')||'<div class=mut>no users</div>';});}
function setMode(m){api('/api/me/signal-execution-mode',{method:'POST',body:JSON.stringify({signal_execution_mode:m})}).then(r=>{if(r.ok){MODE=(r.j.signal_execution_mode||m);document.getElementById('mp').className=(MODE!='live'?'on':'');document.getElementById('ml').className=(MODE=='live'?'on':'');document.getElementById('modemsg').textContent=r.j.message||('Mode: '+MODE);renderBalance();}else{document.getElementById('modemsg').textContent=r.j.detail||'Failed';}})}
function saveCash(){var b={},i=document.getElementById('cin').value,u=document.getElementById('cus').value;if(i)b.india_cash=+i;if(u)b.us_cash=+u;
 var url=(ME&&ME.role=='admin'&&ME.id)?('/api/users/'+ME.id+'/paper-cash'):'/api/me/paper-cash';
 api(url,{method:'POST',body:JSON.stringify(b)}).then(r=>{document.getElementById('cashmsg').textContent=r.ok?'Saved ✓':(r.j.detail||'Failed');if(r.ok)loadAccountData();})}
function allocUser(id){var b={},i=document.getElementById('ai_'+id).value,u=document.getElementById('au_'+id).value;if(i)b.india_cash=+i;if(u)b.us_cash=+u;api('/api/users/'+id+'/paper-cash',{method:'POST',body:JSON.stringify(b)}).then(r=>{alert(r.ok?'Allocated to user.':(r.j.detail||'Failed'))})}
function openBroker(w){api(w=='upstox'?'/api/me/upstox/auth-url':'/api/alpaca/connect').then(r=>{if(r.j.auth_url)location.href=r.j.auth_url;else alert('Connect '+w+' from settings.')})}
function stock(sym,mkt){go('detail');renderStock(sym,mkt,'detail')}
function renderStock(sym,mkt,target){var el=document.getElementById(target);if(target=='detail')el.innerHTML='<div class=skel>analysing '+sym+'…</div>';
 api('/v2/api/stock/'+sym+'?market='+mkt).then(r=>{var d=r.j,s=mkt=='IN'?'₹':'$';
 if(d.error){el.innerHTML=(target=='detail'?'<div class=mut style="padding:12px 0;cursor:pointer" onclick="go(\'home\')">‹ back</div>':'')+'<div class=skel>'+(d.error)+(d.live?' · '+s+d.live:'')+'</div>'+newsHtml(d.news,s);return;}
 var vt=d.verdict=='BUY'?'var(--up)':(d.verdict=='WATCH'?'var(--warn)':'var(--dn)'),vb=d.verdict=='BUY'?'var(--upb)':(d.verdict=='WATCH'?'var(--warnb)':'var(--dnb)');
 var fb=Object.keys(d.factors||{}).map(k=>{var v=d.factors[k],c=v>=60?'var(--up)':(v>=40?'var(--warn)':'var(--dn)');return '<div style="margin:8px 0"><div class=row style="font-size:12px"><span class=mut>'+k.replace('_',' ')+'</span><span>'+v+'</span></div><div class=scorebar><i style="width:'+v+'%;background:'+c+'"></i></div></div>'}).join('');
 el.innerHTML=(target=='detail'?'<div class=mut style="padding:12px 0;cursor:pointer" onclick="go(\'home\')">‹ back</div>':'')
 +'<div class=row><div><div class=sec style="margin:0">'+sym+'</div><div class=mut style="font-size:12px">'+mkt+' · live</div></div><div class=hero style="font-size:25px">'+s+d.live+'</div></div>'
 +'<div class=raise style="background:'+vb+';border:none;margin-top:8px"><div class=row><b style="color:'+vt+'">'+d.verdict+'</b><span style="color:'+vt+';font-size:12px">score '+d.score+(d.regime===false?' · regime risk-off':'')+'</span></div></div>'
 +'<div class=grid><div class=card><div class=mut style="font-size:11px">entry</div><div style="font-size:17px;font-weight:600">'+s+d.entry+'</div></div><div class=card><div class=mut style="font-size:11px">reward:risk</div><div style="font-size:17px;font-weight:600">'+d.rr+':1</div></div><div class=card><div class=mut style="font-size:11px">stop</div><div class="dn" style="font-size:17px;font-weight:600">'+s+d.stop+'</div></div><div class=card><div class=mut style="font-size:11px">target</div><div class="up" style="font-size:17px;font-weight:600">'+s+d.target+'</div></div></div>'
 +'<div class=sec>why this score</div>'+fb+newsHtml(d.news,s)
 +'<div style="display:flex;gap:9px;margin:14px 0"><button style="flex:1" onclick="alert(\'Alerts coming next\')">Set alert</button><button class=pri style="flex:1">'+(MODE=='live'?'Buy':'Paper buy')+'</button></div>';});}
function newsHtml(n,s){if(!n||!n.length)return '<div class=sec>news</div><div class=mut style="font-size:13px">no recent headlines</div>';
 return '<div class=sec>news &amp; sentiment</div>'+n.map(x=>{var c=x.score>0.1?'bg-up':(x.score<-0.1?'bg-dn':'bg-mut');return '<div style="display:flex;gap:9px;align-items:flex-start;margin:8px 0"><span class="badge '+c+'" style="white-space:nowrap;margin-top:1px">'+x.label.replace('_',' ')+'</span><div><div style="font-size:13px;line-height:1.4">'+x.title+'</div><div class=mut style="font-size:10px">'+(x.when||'')+'</div></div></div>'}).join('');}
boot();setInterval(()=>{if(ME){if(cur=='home')loadHome();if(cur=='positions')loadPos()}},20000);
</script></body></html>"""
