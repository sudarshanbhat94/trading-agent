"""v2 LIVE runner - continuous live signals + real-time execution. No cron.

Every cycle (default 45s), during market hours, for each market:
  1. pull live quotes (price + day open/high/low/volume)
  2. SYNTHESISE today's daily bar from live data, append to cached history, and
     RECOMPUTE every signal (conviction + gap) fresh - so signals are always
     current and never depend on the lagging daily-candle feed
  3. refresh the live watchlist (v2_signals)
  4. enter new signals at the live price (one per name per day), regime-gated for
     swing, ungated for gap
  5. monitor open positions on live price/high/low and fire stop/target/trailing
     exits the instant they hit
  6. write a live equity snapshot

Historical lookback is cached and refreshed every few hours; only the live bar
recomputes each cycle, so a poll is cheap. Reads main DB READ-ONLY; writes only
var/v2_paper.db.

  python3 scripts/v2_live_runner.py --loop --interval 45
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import v2_engine as eng  # noqa: E402

LIVE_SOURCE = {"IN": "upstox-live", "US": "alpaca-iex-live"}
STRATEGIES = {
    "swing_meanrev": dict(kind="conviction", regime_gated=True, threshold=0.55,
                          atr_stop=2.0, atr_target=3.5, trail=0.0, max_pos=50),
    "gap_momentum":  dict(kind="gap", regime_gated=False, threshold=0.0,
                          atr_stop=1.5, atr_target=0.0, trail=0.10, max_pos=50),
}
_HIST: dict = {}      # market -> (loaded_ts, {sym: tail_df}, market_df)
_HIST_TTL = 6 * 3600


def _ro(p):
    return sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=30)


def _hist(main_db, market):
    h = _HIST.get(market)
    if h and time.time() - h[0] < _HIST_TTL:
        return h[1], h[2]
    con = _ro(main_db)
    syms, mdf = eng.load_panel(con, market, topn=eng.DEFAULTS["topn"])
    con.close()
    tails = {s: g.tail(90).copy() for s, g in syms.items() if len(g) >= 70}
    _HIST[market] = (time.time(), tails, mdf)
    print(f"  [{market}] history cached: {len(tails)} symbols")
    return tails, mdf


def _live(main_db, market):
    con = _ro(main_db)
    rows = con.execute("SELECT symbol,price,open,high,low,close,volume,ts FROM latest_quotes WHERE source=?",
                       (LIVE_SOURCE[market],)).fetchall()
    con.close()
    out, newest = {}, None
    for sym, p, o, h, l, c, v, ts in rows:
        try:
            price = float(p)
        except (TypeError, ValueError):
            continue
        if price <= 0:
            continue
        out[sym] = dict(price=price, open=_n(o, price), high=_n(h, price), low=_n(l, price),
                        prev=_n(c, price), vol=_n(v, 0))
        if ts and (newest is None or ts > newest):
            newest = ts
    return out, newest


def _n(v, d):
    try:
        x = float(v)
        return x if x > 0 else d
    except (TypeError, ValueError):
        return d


def _fresh(newest_ts, max_age=900):
    if not newest_ts:
        return False
    try:
        t = datetime.fromisoformat(newest_ts.replace("Z", "+00:00"))
    except ValueError:
        return False
    return (datetime.now(timezone.utc) - t).total_seconds() <= max_age


def live_signals(tails, mdf, live):
    """Recompute conviction + gap signals using a live-synthesised current bar."""
    today = pd.Timestamp(datetime.now(timezone.utc).date())
    rets = [live[s]["price"] / t["close"].iloc[-1] - 1
            for s, t in tails.items() if s in live and t["close"].iloc[-1] > 0]
    mret = float(pd.Series(rets).median()) if rets else 0.0
    mdf_live = mdf.copy()
    mdf_live.loc[today] = {"mkt_ret1": mret, "mkt_cum": mdf["mkt_cum"].iloc[-1] * (1 + mret)}
    conv, gap = [], []
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
            conv.append(dict(symbol=s, score=round(c, 4), atr=atr, price=lq["price"]))
        prevc = t["close"].iloc[-1]
        g = lq["open"] / prevc - 1 if prevc > 0 else 0
        rv = float(row["rvol"]) if not pd.isna(row["rvol"]) else 0
        if 0.03 <= g <= 0.15 and rv >= 1.5:
            gap.append(dict(symbol=s, score=round(min(g / 0.15, 1.0), 4), atr=atr,
                            price=lq["price"], gap=round(g * 100, 2)))
    conv.sort(key=lambda x: -x["score"]); gap.sort(key=lambda x: -x["score"])
    return conv, gap, mdf_live, today


def poll(main_db, v2, markets, cost_side, dry=False):
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for market in markets:
        try:
            live, newest = _live(main_db, market)
            if not _fresh(newest):
                continue  # market closed / feed stale -> skip (no live trading)
            tails, mdf = _hist(main_db, market)
            conv, gap, mdf_live, today = live_signals(tails, mdf, live)
            regime = eng.regime_ok(mdf_live, today, eng.DEFAULTS["regime_lookback"])
            sig_by = {"conviction": conv, "gap": gap}
            for strat, cfg in STRATEGIES.items():
                st = v2.execute("SELECT capital,max_pos FROM v2_state WHERE market=? AND strategy=?",
                                (market, strat)).fetchone()
                if not st:
                    continue
                capital, max_pos = st[0], int(st[1])
                sigs = sig_by[cfg["kind"]]
                # refresh live watchlist
                v2.execute("DELETE FROM v2_signals WHERE market=? AND strategy=?", (market, strat))
                for rank, s in enumerate(sigs[:max_pos], 1):
                    v2.execute("INSERT INTO v2_signals VALUES(?,?,?,?,?,?,?)",
                               (market, strat, today.date().isoformat(), s["symbol"], s["score"],
                                round(s["price"], 2), rank))
                # current book
                positions = {r[0]: dict(id=r[1], entry=r[2], shares=r[3], stop=r[4], target=r[5],
                                        trail=r[6], peak=r[7])
                             for r in v2.execute("SELECT symbol,id,entry_price,shares,stop,target,trail,peak "
                                                 "FROM v2_positions WHERE market=? AND strategy=?", (market, strat))}
                traded_today = {r[0] for r in v2.execute(
                    "SELECT symbol FROM v2_trades WHERE market=? AND strategy=? AND entry_date=?",
                    (market, strat, today.date().isoformat()))}
                realised = v2.execute("SELECT COALESCE(SUM(pnl),0) FROM v2_trades WHERE market=? AND strategy=?",
                                      (market, strat)).fetchone()[0] or 0.0
                cash = capital - sum(p["shares"] * p["entry"] for p in positions.values()) + realised
                alloc = capital / max_pos
                # ENTRIES at live price (regime gate for swing; one per name/day)
                gate = (not cfg["regime_gated"]) or regime
                filled = 0
                if gate:
                    for s in sigs:
                        if len(positions) >= max_pos:
                            break
                        sym = s["symbol"]
                        if sym in positions or sym in traded_today or s["score"] < cfg["threshold"] or alloc > cash:
                            continue
                        entry, atr = s["price"], s["atr"]
                        shares = alloc / entry
                        cash -= shares * entry * (1 + cost_side[market])
                        tgt = entry + cfg["atr_target"] * atr if cfg["atr_target"] else 0.0
                        if not dry:
                            v2.execute("INSERT INTO v2_positions(market,strategy,symbol,entry_date,entry_price,"
                                       "shares,stop,target,trail,peak,conviction,opened_at) "
                                       "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                                       (market, strat, sym, today.date().isoformat(), entry, shares,
                                        entry - cfg["atr_stop"] * atr, tgt, cfg["trail"], entry,
                                        s["score"], stamp))
                        positions[sym] = dict(id=None, entry=entry, shares=shares,
                                              stop=entry - cfg["atr_stop"] * atr, target=tgt,
                                              trail=cfg["trail"], peak=entry)
                        traded_today.add(sym); filled += 1
                # EXITS on live price/high/low
                exited = 0
                for sym, p in list(positions.items()):
                    lq = live.get(sym)
                    if not lq:
                        continue
                    peak = max(p["peak"], lq["high"], lq["price"])
                    eff = p["stop"]
                    if p["trail"]:
                        eff = max(eff, peak * (1 - p["trail"]))
                    exit_px = reason = None
                    if lq["low"] <= eff or lq["price"] <= eff:
                        exit_px, reason = min(eff, lq["price"]), ("trail" if p["trail"] and eff > p["stop"] else "stop")
                    elif p["target"] and (lq["high"] >= p["target"] or lq["price"] >= p["target"]):
                        exit_px, reason = max(p["target"], lq["price"]), "target"
                    if exit_px is not None and not dry:
                        cash += p["shares"] * exit_px * (1 - cost_side[market])
                        if p["id"]:
                            v2.execute("INSERT INTO v2_trades(market,strategy,symbol,entry_date,entry_price,"
                                       "exit_date,exit_price,shares,pnl,return_pct,reason,conviction) "
                                       "SELECT market,strategy,symbol,entry_date,entry_price,?,?,?,?,?,?,conviction "
                                       "FROM v2_positions WHERE id=?",
                                       (today.date().isoformat(), exit_px, p["shares"],
                                        p["shares"] * (exit_px - p["entry"]), (exit_px / p["entry"] - 1) * 100,
                                        reason, p["id"]))
                            v2.execute("DELETE FROM v2_positions WHERE id=?", (p["id"],))
                        del positions[sym]; exited += 1
                    elif not dry and p["id"]:
                        v2.execute("UPDATE v2_positions SET peak=? WHERE id=?", (peak, p["id"]))
                pv = sum(p["shares"] * (live[s]["price"] if s in live else p["entry"]) for s, p in positions.items())
                if not dry:
                    v2.execute("INSERT OR REPLACE INTO v2_equity(market,strategy,date,equity,cash,positions_value,n_positions)"
                               " VALUES(?,?,?,?,?,?,?)",
                               (market, strat, "LIVE_" + stamp[:19], cash + pv, cash, pv, len(positions)))
                    v2.execute("UPDATE v2_state SET last_date=? WHERE market=? AND strategy=?",
                               (today.date().isoformat(), market, strat))
                v2.commit()
                if filled or exited:
                    print(f"  [{market}/{strat}] fill={filled} exit={exited} open={len(positions)} "
                          f"sigs={len(sigs)} regime={regime}")
        except Exception as exc:
            import traceback
            print(f"  [{market}] poll error: {exc}"); traceback.print_exc()
    return stamp


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--main-db", default="/opt/opentrade/var/trading_agent.db")
    ap.add_argument("--v2-db", default="/opt/opentrade/var/v2_paper.db")
    ap.add_argument("--markets", default="IN,US")
    ap.add_argument("--interval", type=int, default=45)
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()
    markets = [m.strip().upper() for m in a.markets.split(",") if m.strip()]
    cost_side = {"IN": 0.30 / 200, "US": 0.12 / 200}
    v2 = sqlite3.connect(a.v2_db, timeout=60)
    print(f"v2 LIVE runner @ {datetime.now(timezone.utc).isoformat(timespec='seconds')} "
          f"markets={markets} loop={a.loop} interval={a.interval}s")
    if a.loop:
        while True:
            t0 = time.time()
            stamp = poll(a.main_db, v2, markets, cost_side, a.dry)
            print(f"poll {stamp} ({time.time()-t0:.1f}s)")
            time.sleep(max(5, a.interval - (time.time() - t0)))
    else:
        poll(a.main_db, v2, markets, cost_side, a.dry)
    v2.close()


if __name__ == "__main__":
    main()
