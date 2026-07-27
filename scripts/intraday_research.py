"""Intraday momentum research — tests the user-specified design HONESTLY on
real 5-minute NSE bars before anything goes live.

The design under test (user's spec, not the old daily mean-reversion):
  ENTRY  - intraday: stock is UP meaningfully from TODAY's open (not yesterday's
           close), on a VOLUME SURGE, making new intraday highs, before a cutoff
           time. Optionally requires a FRESH NEWS catalyst (sentiment_events).
  EXIT   - take the money: +3..4% target, hard stop, and a "never let a green
           trade go red" breakeven lock once up >= lock_trigger.
           Square off at end of day. NO overnight risk.

Honesty rules:
  - signal on bar t  ->  enter at bar t+1 OPEN (no lookahead)
  - volume surge measured vs the SAME symbol's same-time-of-day median volume
    over the prior 5 sessions (point-in-time)
  - news join uses only events ingested BEFORE the entry bar
  - realistic intraday cost+slippage charged per round trip
  - compares vs hold-to-EOD baseline and reports per-config trade counts

Run on OCI:
  python3 scripts/intraday_research.py            # full grid
  python3 scripts/intraday_research.py --best     # detail on the best config
"""
from __future__ import annotations

import argparse
import sqlite3
from collections import defaultdict

import numpy as np
import pandas as pd

INTRA_DB = "/opt/opentrade/var/intraday_yahoo.db"
MAIN_DB = "/opt/opentrade/var/trading_agent.db"

# realistic IN intraday (MIS) round-trip: brokerage+STT+exchange+GST+stamp
# ~0.10-0.12% plus momentum-chasing slippage on both sides. Deliberately harsh.
COST_RT_PCT = 0.25

IST_OFF = 5.5 * 3600  # bars are epoch-UTC; NSE session 09:15-15:30 IST


def load_bars():
    con = sqlite3.connect(f"file:{INTRA_DB}?mode=ro", uri=True)
    df = pd.read_sql_query(
        "SELECT symbol, ts, open, high, low, close, volume FROM bars WHERE market='IN'", con)
    con.close()
    df["dt"] = pd.to_datetime(df["ts"] + int(IST_OFF), unit="s")  # IST clock
    df["date"] = df["dt"].dt.date
    df["tod"] = df["dt"].dt.hour * 100 + df["dt"].dt.minute      # e.g. 915, 920
    df = df[(df.tod >= 915) & (df.tod <= 1530)]
    df = df.sort_values(["symbol", "ts"]).reset_index(drop=True)
    return df


import json

# genuinely EXOGENOUS catalysts (a real new fact), vs "price_momentum" which is
# derived from the move itself — the circular signal we want to exclude.
EXOGENOUS = {"earnings", "guidance", "order_win", "mna", "merger", "acquisition",
             "corporate_action", "dividend", "upgrade", "rating_change", "buyback"}


def load_news():
    """symbol -> sorted list of (ingest_ts_epoch, score, is_exogenous).
    is_exogenous = the event carries a real catalyst (earnings etc.), not just a
    price_momentum echo of the move we're already trading."""
    con = sqlite3.connect(f"file:{MAIN_DB}?mode=ro", uri=True)
    rows = con.execute("SELECT ts, symbol, score, events_json FROM sentiment_events").fetchall()
    con.close()
    out = defaultdict(list)
    for ts, sym, score, ej in rows:
        try:
            t = pd.Timestamp(ts).timestamp()
        except Exception:
            continue
        exo = False
        try:
            for e in json.loads(ej or "[]"):
                if e.get("event_type") in EXOGENOUS and float(e.get("score") or 0) > 0:
                    exo = True
                    break
        except Exception:
            pass
        out[str(sym).upper()].append((t, float(score or 0), exo))
    for sym in out:
        out[sym].sort()
    return out


def fresh_news_score(news, sym, entry_epoch, lookback_h=24, require_exo=False):
    """Best positive news score in the lookback window BEFORE entry. If
    require_exo, only exogenous-catalyst events count (excludes price_momentum)."""
    lst = news.get(sym)
    if not lst:
        return 0.0
    lo = entry_epoch - lookback_h * 3600
    best = 0.0
    for t, score, exo in reversed(lst):
        if t > entry_epoch:
            continue
        if t < lo:
            break
        if require_exo and not exo:
            continue
        if score > best:
            best = score
    return best


def _decay_tp(mins):
    """Freqtrade-style ROI table: take less profit the longer it's held (results-day
    momentum fades through the session)."""
    if mins < 30:
        return 0.05
    if mins < 60:
        return 0.03
    if mins < 120:
        return 0.015
    return 0.008


def simulate(df, news, move_min, rvol_min, cutoff_tod, tp, sl, lock_trig,
             news_min, hold_to_eod=False, news_lb=24,
             entry_mode="highbreak", rvol_mode="bucket", news_mode="any",
             or_bars=6, roi_decay=False):
    """Walk each symbol-day; first qualifying signal -> one trade.
      entry_mode : 'highbreak' (up vs open + pressing day high) | 'vwap' (up vs
                   open + holding above intraday VWAP — institutional benchmark)
      rvol_mode  : 'bucket' (this 5-min bar vs same-tod median, backtest-native)
                 | 'cum' (cumulative day volume vs typical cumulative-by-now —
                   what the LIVE engine actually computes; fixes the mismatch)
      news_mode  : 'any' (any positive score) | 'earnings' (exogenous catalyst
                   only, excludes the circular price_momentum echo)
    """
    trades = []
    req_exo = (news_mode == "earnings")
    for sym, g in df.groupby("symbol", sort=False):
        g = g.reset_index(drop=True)
        # per-5min-bucket volume baseline (bucket mode)
        piv = g.pivot_table(index="date", columns="tod", values="volume", aggfunc="sum")
        base = piv.rolling(5, min_periods=3).median().shift(1)
        # cumulative-by-tod baseline (cum mode, live-consistent)
        g["cumvol"] = g.groupby("date")["volume"].cumsum()
        pivc = g.pivot_table(index="date", columns="tod", values="cumvol", aggfunc="last")
        basec = pivc.rolling(5, min_periods=3).median().shift(1)
        # intraday VWAP
        tp_ = (g["high"] + g["low"] + g["close"]) / 3.0
        g["cumtpv"] = (tp_ * g["volume"]).groupby(g["date"]).cumsum()
        g["vwap"] = g["cumtpv"] / g["cumvol"].replace(0, np.nan)
        day_groups = g.groupby("date", sort=True)
        for day, d in day_groups:
            d = d.reset_index(drop=True)
            if len(d) < 10:
                continue
            day_open = float(d["open"].iloc[0])
            if day_open <= 0:
                continue
            # opening-range high (first or_bars bars) for ORB entries
            or_high = float(d["high"].iloc[:or_bars].max()) if len(d) > or_bars else 1e18
            start_i = or_bars if entry_mode == "orb" else 2
            hi_run = -1e18
            for i in range(start_i, len(d) - 1):    # need bar i+1 to enter
                row = d.iloc[i]
                if row.tod > cutoff_tod:
                    break
                hi_run = max(hi_run, float(d["high"].iloc[i - 1]))
                move = float(row.close) / day_open - 1
                if move < move_min:
                    continue
                if entry_mode == "orb":
                    if float(row.close) <= or_high:        # must break the opening range
                        continue
                elif entry_mode == "vwap":
                    vw = float(row.vwap) if np.isfinite(row.vwap) else 0.0
                    if vw <= 0 or float(row.close) < vw:   # must hold above VWAP
                        continue
                else:
                    if float(row.high) < hi_run:           # must be pressing day highs
                        continue
                if rvol_mode == "cum":
                    bc = None
                    try:
                        bc = basec.at[day, row.tod]
                    except KeyError:
                        pass
                    if bc is None or not np.isfinite(bc) or bc <= 0:
                        continue
                    rvol = float(row.cumvol) / float(bc)
                else:
                    bv = None
                    try:
                        bv = base.at[day, row.tod]
                    except KeyError:
                        pass
                    if bv is None or not np.isfinite(bv) or bv <= 0:
                        continue
                    rvol = float(row.volume) / float(bv)
                if rvol < rvol_min:
                    continue
                entry_bar = d.iloc[i + 1]
                entry = float(entry_bar.open)
                if entry <= 0:
                    continue
                if news_min > 0:
                    ns = fresh_news_score(news, sym, float(row.ts), lookback_h=news_lb, require_exo=req_exo)
                    if ns < news_min:
                        continue
                # ---- manage the trade bar-by-bar to EOD ----
                stop = entry * (1 - sl)
                target = entry * (1 + tp)
                locked = False
                ex, reason = None, "eod"
                for j in range(i + 1, len(d)):
                    b = d.iloc[j]
                    if not locked and float(b.high) >= entry * (1 + lock_trig):
                        stop = max(stop, entry * 1.001)   # green never goes red
                        locked = True
                    if float(b.low) <= stop:
                        ex, reason = stop, ("lock" if locked else "stop")
                        break
                    eff_tgt = entry * (1 + _decay_tp((j - i - 1) * 5)) if roi_decay else target
                    if not hold_to_eod and float(b.high) >= eff_tgt:
                        ex, reason = eff_tgt, "target"
                        break
                if ex is None:
                    ex = float(d["close"].iloc[-1])
                net = (ex / entry - 1) * 100 - COST_RT_PCT
                trades.append(dict(symbol=sym, date=str(day), tod=int(row.tod),
                                   move=round(move * 100, 2), rvol=round(rvol, 1),
                                   entry=entry, exit=ex, reason=reason,
                                   net=round(net, 3)))
                break   # one trade per symbol-day
    return pd.DataFrame(trades)


def stats(t):
    if t is None or len(t) == 0:
        return "n=0"
    wins = t[t.net > 0].net.sum()
    losses = t[t.net <= 0].net.sum()
    pf = wins / abs(losses) if losses < 0 else float("inf")
    return ("n=%4d  win=%5.1f%%  avg=%+.2f%%  PF=%.2f  total=%+.0f%%"
            % (len(t), (t.net > 0).mean() * 100, t.net.mean(), pf, t.net.sum()))


def refine(df, news):
    """Fair fight inside the news-covered window only (news feed starts
    2026-05-17): same-window no-news baseline vs news-gated variants, sweeping
    the entry side. Exits fixed at the user's spec."""
    df = df[df["date"] >= pd.Timestamp("2026-05-17").date()].copy()
    print("refine window: %s..%s  days=%d" % (df.date.min(), df.date.max(), df.date.nunique()))
    cfgs = [
        # (name, move_min, rvol_min, cutoff, news_min, news_lookback_h)
        ("BASELINE no-news  move>=2 cutoff13", 0.02, 2.0, 1300, 0.0, 24),
        ("news24h>=0.3 move>=2 cutoff11", 0.02, 2.0, 1100, 0.3, 24),
        ("news24h>=0.3 move>=2 cutoff13", 0.02, 2.0, 1300, 0.3, 24),
        ("news24h>=0.3 move>=2 ALLDAY(14:30)", 0.02, 2.0, 1430, 0.3, 24),
        ("FRESH6h>=0.3 move>=2 ALLDAY(14:30)", 0.02, 2.0, 1430, 0.3, 6),
        ("FRESH3h>=0.3 move>=2 ALLDAY(14:30)", 0.02, 2.0, 1430, 0.3, 3),
        ("FRESH6h>=0.3 move>=2 cutoff11", 0.02, 2.0, 1100, 0.3, 6),
    ]
    best_key, best_t = None, None
    for name, mv, rv, cut, nm, nlb in cfgs:
        t = simulate(df, news, mv, rv, cut, 0.035, 0.0175, 0.015, nm, news_lb=nlb)
        print("%-36s | %s" % (name, stats(t)))
        if len(t) >= 25 and (best_t is None or t.net.mean() > best_t.net.mean()):
            best_key, best_t = name, t
    if best_t is not None:
        print("\n=== refine best: %s ===" % best_key)
        print(stats(best_t))
        print("by exit reason:")
        for r, gg in best_t.groupby("reason"):
            print("   %-7s n=%4d  avg=%+.2f%%" % (r, len(gg), gg.net.mean()))
        bd = best_t.groupby("date")["net"].sum()
        print("daily P&L: green days %d / %d  worst day %+.1f%%  best day %+.1f%%"
              % ((bd > 0).sum(), len(bd), bd.min(), bd.max()))


def upgrades(df, news):
    """Head-to-head on the news-covered window: does VWAP entry, a live-consistent
    cumulative rvol, and an earnings-only catalyst beat the current sleeve?"""
    df = df[df["date"] >= pd.Timestamp("2026-05-17").date()].copy()
    print("upgrade window: %s..%s  days=%d\n" % (df.date.min(), df.date.max(), df.date.nunique()))
    # fixed exits = user spec; cutoff 11:00 (validated best); news>=0.3
    common = dict(move_min=0.02, rvol_min=2.0, cutoff_tod=1100, tp=0.035, sl=0.0175,
                  lock_trig=0.015, news_min=0.3, news_lb=24)
    configs = [
        ("CURRENT LIVE  highbreak+bucket+any", dict(entry_mode="highbreak", rvol_mode="bucket", news_mode="any")),
        ("bugfix: highbreak + CUM rvol (live-real)", dict(entry_mode="highbreak", rvol_mode="cum", news_mode="any")),
        ("B: VWAP entry + bucket rvol", dict(entry_mode="vwap", rvol_mode="bucket", news_mode="any")),
        ("B: VWAP entry + CUM rvol", dict(entry_mode="vwap", rvol_mode="cum", news_mode="any")),
        ("C: highbreak + EARNINGS-only news", dict(entry_mode="highbreak", rvol_mode="bucket", news_mode="earnings")),
        ("B+C: VWAP + CUM + EARNINGS-only", dict(entry_mode="vwap", rvol_mode="cum", news_mode="earnings")),
    ]
    for name, kw in configs:
        t = simulate(df, news, **common, **kw)
        print("%-42s | %s" % (name, stats(t)))


def strategies(df, news):
    """Borrowed buying strategies + exit upgrades, head-to-head on the news window."""
    df = df[df["date"] >= pd.Timestamp("2026-05-17").date()].copy()
    print("window: %s..%s  days=%d\n" % (df.date.min(), df.date.max(), df.date.nunique()))
    base = dict(rvol_min=2.0, cutoff_tod=1300, sl=0.0175, lock_trig=0.015, news_min=0.3, news_lb=24)
    cfgs = [
        # name, extra kwargs
        ("CURRENT: highbreak + catalyst + fixed3.5%", dict(move_min=0.02, tp=0.035, entry_mode="highbreak", rvol_mode="cum", news_mode="earnings")),
        ("CURRENT + TIME-DECAY target",               dict(move_min=0.02, tp=0.035, entry_mode="highbreak", rvol_mode="cum", news_mode="earnings", roi_decay=True)),
        ("ORB(30m) + catalyst + fixed3.5%",           dict(move_min=0.0,  tp=0.035, entry_mode="orb", rvol_mode="cum", news_mode="earnings")),
        ("ORB(30m) + catalyst + TIME-DECAY",          dict(move_min=0.0,  tp=0.035, entry_mode="orb", rvol_mode="cum", news_mode="earnings", roi_decay=True)),
        ("ORB(30m) NO-catalyst (pure structure)",     dict(move_min=0.0,  tp=0.035, entry_mode="orb", rvol_mode="cum", news_mode="any", news_min=0.0)),
        ("ORB(30m) NO-cat + TIME-DECAY",              dict(move_min=0.0,  tp=0.035, entry_mode="orb", rvol_mode="cum", news_mode="any", news_min=0.0, roi_decay=True)),
    ]
    for name, kw in cfgs:
        merged = dict(base); merged.update(kw)
        t = simulate(df, news, **merged)
        print("%-46s | %s" % (name, stats(t)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--best", action="store_true")
    ap.add_argument("--refine", action="store_true")
    ap.add_argument("--upgrades", action="store_true")
    ap.add_argument("--strategies", action="store_true")
    a = ap.parse_args()

    print("loading bars + news ...", flush=True)
    df = load_bars()
    news = load_news()
    days = df["date"].nunique()
    print("bars=%d  symbols=%d  days=%d  %s..%s" %
          (len(df), df.symbol.nunique(), days, df.date.min(), df.date.max()), flush=True)

    if a.strategies:
        strategies(df, news)
        return
    if a.upgrades:
        upgrades(df, news)
        return
    if a.refine:
        refine(df, news)
        return

    grid = []
    for move_min in (0.02, 0.03, 0.04):
        for rvol_min in (2.0, 3.0):
            for news_min in (0.0, 0.3):
                grid.append((move_min, rvol_min, news_min))

    print(f"\ncfg: entry cutoff 13:00 IST | TP +3.5% | SL -1.75% | lock @ +1.5% | EOD square-off | cost {COST_RT_PCT:.2f}%/rt")
    print("%-28s | %s" % ("config", "quick-exit result"))
    results = {}
    for move_min, rvol_min, news_min in grid:
        t = simulate(df, news, move_min, rvol_min, 1300, 0.035, 0.0175, 0.015, news_min)
        key = "move>=%.0f%% rvol>=%.0f news>=%.1f" % (move_min * 100, rvol_min, news_min)
        results[key] = t
        print("%-28s | %s" % (key, stats(t)))

    # baseline comparison on a mid config: quick-exit vs hold-to-EOD
    print("\n--- exit-style comparison (move>=3%, rvol>=2, no news gate) ---")
    q = simulate(df, news, 0.03, 2.0, 1300, 0.035, 0.0175, 0.015, 0.0)
    h = simulate(df, news, 0.03, 2.0, 1300, 0.035, 0.0175, 0.015, 0.0, hold_to_eod=True)
    print("  quick-exit (+3.5%% & lock): %s" % stats(q))
    print("  hold-to-EOD (same entries): %s" % stats(h))

    print("\n--- news impact (move>=3%, rvol>=2) ---")
    print("  without news gate: %s" % stats(results.get("move>=3% rvol>=2 news>=0.0")))
    print("  WITH news>=0.3   : %s" % stats(results.get("move>=3% rvol>=2 news>=0.3")))

    if a.best:
        best_key = max(results, key=lambda k: results[k].net.mean() if len(results[k]) > 30 else -9)
        t = results[best_key]
        print("\n=== best config: %s ===" % best_key)
        print(stats(t))
        print("by exit reason:")
        for r, gg in t.groupby("reason"):
            print("   %-7s n=%4d  avg=%+.2f%%" % (r, len(gg), gg.net.mean()))
        t["date"] = pd.to_datetime(t["date"])
        t["month"] = t["date"].dt.to_period("M")
        print("by month:")
        for m, gg in t.groupby("month"):
            print("   %s  n=%4d  avg=%+.2f%%  total=%+.1f%%" % (m, len(gg), gg.net.mean(), gg.net.sum()))


if __name__ == "__main__":
    main()
