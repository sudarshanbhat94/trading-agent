"""Gap-momentum swing strategy - the edge found by the big-move detector.

Entry: stock gaps up into [gap_min, gap_max]% on volume (rvol>thr), optional
relative-strength + market-regime filters. Enter NEXT-day open (conservative).
Exit: ATR initial stop, then a trailing stop to ride the run (the detector
showed +9% avg max move left on the table by a fixed hold). India-first.
"""
from __future__ import annotations
import argparse, sqlite3
import numpy as np, pandas as pd

DB = "/opt/opentrade/var/trading_agent.db"
DAILY = {"IN": "upstox-live:day", "US": "alpaca-iex-live:day"}
COST = {"IN": 0.30, "US": 0.12}


def load(con, market, min_turn):
    df = pd.read_sql_query("SELECT symbol,ts,open,high,low,close,volume FROM candles WHERE source=?",
                           con, params=(DAILY[market],))
    df["date"] = pd.to_datetime(df["ts"].str[:10])
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["close"]).sort_values(["symbol", "date"]).drop_duplicates(["symbol", "date"])
    turn = df.assign(t=df.close * df.volume).groupby("symbol").t.median()
    df = df[df.symbol.isin(set(turn[turn > min_turn].index))]
    df["ret1"] = df.groupby("symbol").close.pct_change()
    mkt = df.groupby("date").ret1.median()
    mcum = (1 + mkt.fillna(0)).cumprod()
    return {s: g.set_index("date") for s, g in df.groupby("symbol")}, mcum


def run(market, min_turn, gap_min, gap_max, rvol_min, trail, atr_stop, max_days,
        rs_filter, regime, cost, start, con):
    syms, mcum = load(con, market, min_turn)
    reg = mcum > mcum.rolling(50).mean()
    mmom20 = mcum / mcum.shift(20) - 1
    trades = []
    for s, g in syms.items():
        if len(g) < 80:
            continue
        c, h, l, o, v = g.close, g.high, g.low, g.open, g.volume
        tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
        atr = tr.rolling(14).mean()
        rvol = v / v.rolling(20).mean()
        gap = o / c.shift(1) - 1
        rs20 = (c / c.shift(20) - 1) - mmom20.reindex(g.index)
        idx = g.index
        i = 0
        while i < len(g) - 1:
            ok = (gap_min <= gap.iloc[i] <= gap_max) and (rvol.iloc[i] >= rvol_min) and atr.iloc[i] > 0
            if ok and rs_filter and not (rs20.iloc[i] > 0):
                ok = False
            if ok and regime and not bool(reg.get(idx[i], False)):
                ok = False
            if not ok:
                i += 1; continue
            entry = o.iloc[i + 1] if i + 1 < len(g) else None
            if not entry or entry <= 0:
                i += 1; continue
            a = atr.iloc[i]
            stop = entry - atr_stop * a
            peak = entry
            exit_px = None
            j = i + 1
            while j < min(i + 1 + max_days, len(g)):
                hi, lo, cl = h.iloc[j], l.iloc[j], c.iloc[j]
                peak = max(peak, hi)
                tstop = max(stop, peak * (1 - trail))
                if lo <= tstop:
                    exit_px = min(tstop, o.iloc[j] if o.iloc[j] < tstop else tstop); exit_px = tstop; break
                j += 1
            if exit_px is None:
                exit_px = c.iloc[min(i + max_days, len(g) - 1)]
            ret = (exit_px / entry - 1) * 100 - cost
            if not start or idx[i] >= pd.Timestamp(start):
                trades.append({"sym": s, "date": idx[i], "ret": ret, "days": j - i})
            i = j + 1   # no overlapping trade in same name
    if not trades:
        print("  no trades"); return
    t = pd.DataFrame(trades)
    n = len(t); w = (t.ret > 0).mean() * 100
    pf = t.ret[t.ret > 0].sum() / abs(t.ret[t.ret <= 0].sum() or 1)
    big = (t.ret >= 15).mean() * 100
    print(f"  gap[{gap_min:.0%},{gap_max:.0%}] rvol>{rvol_min} trail{trail:.0%} atrstop{atr_stop} "
          f"rs={rs_filter} regime={regime}{' OOS' if start else ''}")
    print(f"  TRADES n={n:,} win={w:.1f}% net_exp={t.ret.mean():+.2f}%/trade PF={pf:.2f} "
          f"avgW={t.ret[t.ret>0].mean():+.1f}% avgL={t.ret[t.ret<=0].mean():+.1f}% "
          f">15%winners={big:.0f}% avg_hold={t.days.mean():.0f}d")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", default="IN")
    ap.add_argument("--min-turn", type=float, default=2e7)
    ap.add_argument("--gap-min", type=float, default=0.03)
    ap.add_argument("--gap-max", type=float, default=0.15)
    ap.add_argument("--rvol", type=float, default=1.5)
    ap.add_argument("--trail", type=float, default=0.10)
    ap.add_argument("--atr-stop", type=float, default=1.5)
    ap.add_argument("--max-days", type=int, default=20)
    ap.add_argument("--rs", action="store_true")
    ap.add_argument("--regime", action="store_true")
    ap.add_argument("--start", default=None)
    a = ap.parse_args()
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=120)
    run(a.market, a.min_turn, a.gap_min, a.gap_max, a.rvol, a.trail, a.atr_stop,
        a.max_days, a.rs, a.regime, COST[a.market], a.start, con)
    con.close()


if __name__ == "__main__":
    main()
