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
from . import factor_investigation as fi

MAIN_DB = os.environ.get("OPENSTOCKS_DB", "/opt/opentrade/var/trading_agent.db")
V2_DB = os.environ.get("V2_PAPER_DB", "/opt/opentrade/var/v2_paper.db")
IST = timezone(timedelta(hours=5, minutes=30))
LIVE_SOURCE = {"IN": "upstox-live", "US": "alpaca-iex-live"}
BUDGET = {"IN": 100000.0, "US": 20000.0}     # TOTAL paper capital per market
MAXPOS = {"IN": 10, "US": 10}                # max concurrent positions per market
COST_SIDE = {"IN": 0.30 / 200, "US": 0.12 / 200}
# per-strategy trade plan; both draw from the shared market pool
PLAN = {
    "gap_momentum":  dict(regime_gated=False, threshold=0.38, atr_stop=1.5, atr_target=0.0, trail=0.10, priority=0),
    "swing_meanrev": dict(regime_gated=True,  threshold=0.55, atr_stop=2.0, atr_target=3.5, trail=0.0,  priority=1),
}
# Index/sector/leveraged ETFs - never traded by the single-stock strategies. They
# don't behave like gap/mean-reversion setups and create correlated, duplicate
# exposure (e.g. holding QQQ + QQQM, both Nasdaq-100, at the same time).
ETF_EXCLUDE = {
    "QQQ", "QQQM", "ONEQ", "SPY", "VOO", "IVV", "SPLG", "DIA", "IWM", "IWB", "VTI", "VEA", "VWO",
    "SMH", "SOXX", "SOXL", "SOXS", "XLK", "XLF", "XLE", "XLV", "XLI", "XLY", "XLP", "XLU", "XLB", "XLC", "XLRE",
    "TQQQ", "SQQQ", "UPRO", "SPXL", "SPXU", "ARKK", "ARKG", "VUG", "VTV", "SCHD", "JEPI", "JEPQ",
    "GLD", "SLV", "USO", "TLT", "HYG", "LQD", "VXX", "UVXY", "XBI", "KRE", "KWEB", "FXI", "EEM", "EFA", "VIG",
}
MIN_PRICE = {"IN": 50.0, "US": 5.0}     # quality/liquidity floor - skip the cheapest, most manipulable names
SLOT_MIN_UTIL = 0.55                    # IN whole-share fill must use >= this fraction of its slot (else capital waste)
GAP_SLOT_CAP = 7                        # cap gap_momentum slots so it can't monopolize the book (reserve up to MAXPOS-cap for swing)
GAP_TARGET = {"IN": 0.10, "US": 0.0}    # gap_momentum profit target by market: IN momentum mean-reverts,
                                        # so take profit at +10% (backtested: less give-back, edge intact,
                                        # win rate 37%->46%); US trends, a target chops the big runners -> trail only.
BE_TRIGGER_ATR = 3.0                    # once a trade is up >= this many ATR, lock the stop at breakeven.
                                        # Backtested NEUTRAL (only arms on rare big winners, so it never cuts
                                        # normal trades that would recover) - protects against a monster winner
                                        # round-tripping below entry without harming the edge.
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
    # keep ~1y of bars so the pre-trade factor investigation has enough history
    # (drawdown-from-252d-high, RSI, 50d-slope, etc.); signals only need the tail.
    tails = {s: g.tail(300).copy() for s, g in syms.items() if len(g) >= 70}
    _HIST[market] = (time.time(), tails, mdf)
    return tails, mdf


def _f(v, d):
    try:
        x = float(v)
        return x if x > 0 else d
    except (TypeError, ValueError):
        return d


_SECTOR_CACHE: dict = {}


def _sector_map(market):
    """symbol -> sector, cached 6h. Used for the concentration cap. Best-effort:
    if the universe table has no sector data the cap simply doesn't bind."""
    c = _SECTOR_CACHE.get(market)
    if c and time.time() - c[0] < 6 * 3600:
        return c[1]
    out = {}
    try:
        con = _ro(MAIN_DB)
        for sym, sec in con.execute("SELECT symbol, sector FROM universe"):
            if sym:
                out[str(sym).upper()] = str(sec).strip() if sec else "unknown"
        con.close()
    except Exception:
        pass
    _SECTOR_CACHE[market] = (time.time(), out)
    return out


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


def _live(market, symbols=None):
    con = _ro(MAIN_DB)
    if symbols:
        syms = list(symbols)
        rows = con.execute(
            "SELECT symbol,price,open,high,low,close,volume FROM latest_quotes WHERE source=? AND symbol IN (%s)"
            % ",".join("?" * len(syms)), (LIVE_SOURCE[market], *syms)).fetchall()
    else:
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
        if s["symbol"] in ETF_EXCLUDE:                  # no index/sector/leveraged ETFs
            continue
        if s["price"] < MIN_PRICE.get(market, 0.0):     # quality/liquidity floor
            continue
        cand.append((pl["priority"], -s["score"], s, pl))
    cand.sort(key=lambda x: (x[0], x[1]))
    # strategy balance: hold back slots for swing ONLY when swing actually has
    # eligible candidates, so we never leave cash idle in a gap-only (risk-off) tape
    strat_count = {}
    for p in positions.values():
        strat_count[p["strategy"]] = strat_count.get(p["strategy"], 0) + 1
    swing_avail = sum(1 for _, _, s, _ in cand
                      if s["strategy"] == "swing_meanrev" and s["symbol"] not in positions and s["symbol"] not in traded)
    gap_cap = max_pos - min(max_pos - GAP_SLOT_CAP, swing_avail)
    # ---- pre-trade factor investigation: score the whole universe once ----
    sector_map = _sector_map(market)
    held_sectors = {}
    for psym in positions:
        sec = sector_map.get(str(psym).upper(), "unknown")
        held_sectors[sec] = held_sectors.get(sec, 0) + 1
    try:
        fasof = eng.complete_trading_dates(tails, 0.5)[-1]
        fpanel = fi.build_factor_panel(tails, mdf, fasof)
        fscores = fi.score_panel(fpanel) if len(fpanel) else None
    except Exception as _fexc:
        fpanel = fscores = None
        _status[market] = f"factor panel err: {str(_fexc)[:30]}"
    mcon = _ro(MAIN_DB)
    fills = exits = vetoed = investig = 0
    for _, _, s, pl in cand:
        if len(positions) >= max_pos or alloc * (1 + cside) > cash:   # leave cost headroom; never overdraw the pool
            break
        sym = s["symbol"]
        if sym in positions or sym in traded:
            continue
        if s["strategy"] == "gap_momentum" and strat_count.get("gap_momentum", 0) >= gap_cap:
            continue                      # don't let gap_momentum monopolize the book
        nscore, severe = _news_state(mcon, sym)
        if severe:                       # pro check: never buy into bad news
            vetoed += 1
            continue
        # ---- THE GATE: full multi-factor investigation must say BUY ----
        size_mult = 1.0
        if fscores is not None:
            rep = fi.investigate(sym, fpanel, fscores, market, s["strategy"], regime, severe, held_sectors, sector_map)
            if rep["verdict"] != "BUY":
                investig += 1
                continue
            size_mult = rep.get("size_mult", 1.0)
        entry, atr = s["price"], s["atr"]
        shares = (alloc * size_mult) / entry   # volatility-scaled position size
        if market == "IN":               # NSE: whole shares only, no fractions
            shares = float(int(shares))
            if shares < 1:               # stock too pricey for the per-position budget
                continue
            if shares * entry < SLOT_MIN_UTIL * alloc:   # 1-share fill wastes the slot (e.g. a 5,400 stock in a 10,000 slot)
                continue
        cash -= shares * entry * (1 + cside)
        tgt = entry + pl["atr_target"] * atr if pl["atr_target"] else 0.0
        if s["strategy"] == "gap_momentum" and GAP_TARGET.get(market):   # per-market gap profit target
            tgt = entry * (1 + GAP_TARGET[market])
        v2.execute("INSERT INTO v2_positions(market,strategy,symbol,entry_date,entry_price,shares,stop,target,trail,peak,conviction,opened_at)"
                   " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                   (market, s["strategy"], sym, today_s, entry, shares, entry - pl["atr_stop"] * atr, tgt, pl["trail"], entry,
                    s["score"], datetime.now(timezone.utc).isoformat()))
        positions[sym] = dict(id=None, strategy=s["strategy"], entry=entry, shares=shares,
                              stop=entry - pl["atr_stop"] * atr, target=tgt, trail=pl["trail"], peak=entry)
        strat_count[s["strategy"]] = strat_count.get(s["strategy"], 0) + 1
        sec = sector_map.get(sym, "unknown")
        held_sectors[sec] = held_sectors.get(sec, 0) + 1
        traded.add(sym); fills += 1
    mcon.close()
    v2.commit(); v2.close()
    _status[market] = (f"signals {datetime.now(IST).strftime('%H:%M IST')} · +{fills} new · "
                       f"{vetoed} news-vetoed · {investig} investigation-rejected")


HOLD_DAYS = {"swing_meanrev": 8, "gap_momentum": 20}


def exit_monitor(market):
    """Cheap, fast exit pass: checks open positions against LIVE price/high/low
    for stop / target / trailing / time exit. No feature recompute, so it can
    run every few seconds for near-instant exits."""
    from datetime import date
    v2 = _rw()
    row = v2.execute("SELECT budget FROM v2_book WHERE market=?", (market,)).fetchone()
    if not row:
        v2.close(); return
    budget = row[0]
    cside = COST_SIDE[market]
    today = datetime.now(IST).date()
    today_s = today.isoformat()
    positions = {}
    for r in v2.execute("SELECT id,strategy,symbol,entry_price,shares,stop,target,trail,peak,entry_date "
                        "FROM v2_positions WHERE market=?", (market,)):
        positions[r[2]] = dict(id=r[0], strategy=r[1], entry=r[3], shares=r[4], stop=r[5],
                               target=r[6], trail=r[7], peak=r[8], edate=r[9])
    if not positions:                            # nothing held -> nothing to monitor
        v2.close(); return
    live = _live(market, positions.keys())       # only held symbols (cheap, not all 10k)
    realised = v2.execute("SELECT COALESCE(SUM(pnl),0) FROM v2_trades WHERE market=?", (market,)).fetchone()[0] or 0.0
    cash = budget - sum(p["shares"] * p["entry"] for p in positions.values()) + realised
    exits = 0
    for sym, p in list(positions.items()):
        lq = live.get(sym)
        if not lq:
            continue
        peak = max(p["peak"], lq["high"], lq["price"])
        eff = p["stop"]
        if p["trail"]:
            eff = max(eff, peak * (1 - p["trail"]))
        # breakeven lock on a big winner only (recover ATR from the initial stop)
        atr_stop = PLAN.get(p["strategy"], {}).get("atr_stop", 2.0)
        atr_est = (p["entry"] - p["stop"]) / atr_stop if atr_stop else 0.0
        if atr_est > 0 and peak >= p["entry"] + BE_TRIGGER_ATR * atr_est:
            eff = max(eff, p["entry"])
        try:
            held = (today - date.fromisoformat(str(p["edate"]))).days
        except ValueError:
            held = 0
        # IN gap names spike then mean-revert -> take profit at a fixed target
        # (validated: cuts give-back, holds the edge). US gap trends -> trail only.
        # Computed dynamically so it also protects positions opened before this rule.
        eff_tgt = p["target"] or 0.0
        if p["strategy"] == "gap_momentum" and GAP_TARGET.get(market):
            eff_tgt = max(eff_tgt, p["entry"] * (1 + GAP_TARGET[market]))
        ex = reason = None
        if lq["low"] <= eff or lq["price"] <= eff:
            ex, reason = min(eff, lq["price"]), ("trail" if p["trail"] and eff > p["stop"] else "stop")
        elif eff_tgt and (lq["high"] >= eff_tgt or lq["price"] >= eff_tgt):
            ex, reason = max(eff_tgt, lq["price"]), "target"
        elif held >= HOLD_DAYS.get(p["strategy"], 10):
            ex, reason = lq["price"], "time"
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
    if exits:
        _status[market] = (_status.get(market, "") + f" · -{exits} exit").strip(" ·")


_last_signal: dict = {}
SIGNAL_INTERVAL = 300   # heavy signal recompute cadence (s) — daily signals barely
                        # change intraday, so 5min keeps the GIL-heavy panel/feature
                        # compute from starving the web event loop (exits still run every cycle)


def loop(interval):
    try:
        v2 = _rw(); ensure_schema(v2); v2.close()
    except Exception:
        pass
    while True:
        for m in ("IN", "US"):
            try:
                if market_open(m):
                    exit_monitor(m)                              # fast exits every cycle
                    if time.time() - _last_signal.get(m, 0) >= SIGNAL_INTERVAL:
                        poll_market(m)                           # heavy signal gen periodically
                        _last_signal[m] = time.time()
                else:
                    _status[m] = "closed"
            except Exception as exc:
                _status[m] = f"err {str(exc)[:40]}"
        time.sleep(interval)


def start_background(interval=8):
    global _started
    if _started:
        return
    _started = True
    threading.Thread(target=loop, args=(interval,), daemon=True, name="v2-live-engine").start()


def status():
    return dict(_status)
