"""Backtest popular Indian intraday strategies on real 5m data (Yahoo intraday db).
Strategies traders actually use: VWAP reclaim, SuperTrend(10,3), EMA 9/21 cross.
Long-only, square off by close, round-trip cost deducted. India-first.

  python3 backtest_strategies.py --market IN --strat vwap
"""
from __future__ import annotations
import argparse, sqlite3
import numpy as np, pandas as pd

YDB = "/opt/opentrade/var/intraday_yahoo.db"
COST = {"IN": 0.12, "US": 0.05}


def load(con, market):
    df = pd.read_sql_query("SELECT symbol,ts,open,high,low,close,volume FROM bars WHERE market=?",
                           con, params=(market,))
    df["dt"] = pd.to_datetime(df["ts"], unit="s", utc=True)
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["open", "high", "low", "close"]).sort_values(["symbol", "dt"])
    df["date"] = df["dt"].dt.date
    return df


def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


def supertrend(g, period=10, mult=3.0):
    h, l, c = g["high"], g["low"], g["close"]
    tr = pd.concat([(h - l), (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    atr = tr.rolling(period).mean()
    hl2 = (h + l) / 2
    up, dn = hl2 - mult * atr, hl2 + mult * atr
    st = pd.Series(index=g.index, dtype=float)
    dir_up = True
    fu = fl = np.nan
    for i in range(len(g)):
        if i == 0 or np.isnan(atr.iloc[i]):
            st.iloc[i] = np.nan; fu = up.iloc[i]; fl = dn.iloc[i]; continue
        fu = max(up.iloc[i], fu) if c.iloc[i - 1] > fu else up.iloc[i]
        fl = min(dn.iloc[i], fl) if c.iloc[i - 1] < fl else dn.iloc[i]
        if dir_up:
            if c.iloc[i] < fu:
                dir_up = False
        else:
            if c.iloc[i] > fl:
                dir_up = True
        st.iloc[i] = 1 if dir_up else 0
    return st  # 1=long regime, 0=short regime


def trades_from_signal(g, long_in, cost, stop_pct, target_pct, max_trades):
    """Enter at close on signal; exit on fixed stop / target / EOD. Cap trades/day
    to curb overtrading. Returns list of net % returns."""
    h, l, c = g["high"].to_numpy(), g["low"].to_numpy(), g["close"].to_numpy()
    out = []
    entry = stop = target = None
    ntr = 0
    for i in range(len(g)):
        if entry is None and long_in[i] and ntr < max_trades:
            entry = c[i]; stop = entry * (1 - stop_pct / 100); target = entry * (1 + target_pct / 100); ntr += 1
        elif entry is not None:
            if l[i] <= stop:
                out.append((stop / entry - 1) * 100 - cost); entry = None
            elif h[i] >= target:
                out.append((target / entry - 1) * 100 - cost); entry = None
            elif i == len(g) - 1:
                out.append((c[i] / entry - 1) * 100 - cost); entry = None
    return out


def run(market, strat, cost, stop_pct, target_pct, max_trades, vol_filter, con):
    df = load(con, market)
    print(f"[{market}] {strat} stop={stop_pct}% target={target_pct}% max/day={max_trades} "
          f"volfilter={vol_filter}  symbols={df['symbol'].nunique()} days={df['date'].nunique()}")
    allr = []
    for (sym, day), g in df.groupby(["symbol", "date"]):
        g = g.sort_values("dt").reset_index(drop=True)
        if len(g) < 25:
            continue
        c = g["close"]
        rvol = g["volume"] / g["volume"].rolling(20, min_periods=5).mean()
        if strat == "vwap":
            tp = (g["high"] + g["low"] + g["close"]) / 3
            vwap = (tp * g["volume"]).cumsum() / g["volume"].cumsum().replace(0, np.nan)
            above = (c > vwap).to_numpy()
            long_in = above & ~np.r_[False, above[:-1]]
        elif strat == "ema":
            up = (ema(c, 9) > ema(c, 21)).to_numpy()
            long_in = up & ~np.r_[False, up[:-1]]
        elif strat == "supertrend":
            up = (supertrend(g).to_numpy() == 1)
            long_in = up & ~np.r_[False, up[:-1]]
        else:
            return
        if vol_filter:
            long_in = long_in & (rvol.to_numpy() > 1.2)   # only fire on volume surge
        allr += trades_from_signal(g, long_in, cost, stop_pct, target_pct, max_trades)
    if not allr:
        print("  no trades"); return
    t = np.array(allr)
    w = (t > 0).sum()
    pf = t[t > 0].sum() / abs(t[t <= 0].sum() or 1)
    print(f"  TRADES n={len(t):,} win={w/len(t)*100:.1f}% expectancy(net)={t.mean():+.3f}%/trade "
          f"PF={pf:.2f} total={t.sum():+.0f} units")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", default="IN")
    ap.add_argument("--strat", choices=["vwap", "ema", "supertrend"], default="vwap")
    ap.add_argument("--cost", type=float, default=None)
    ap.add_argument("--stop", type=float, default=0.5)
    ap.add_argument("--target", type=float, default=1.0)
    ap.add_argument("--max-trades", type=int, default=2)
    ap.add_argument("--vol-filter", action="store_true")
    a = ap.parse_args()
    cost = a.cost if a.cost is not None else COST[a.market]
    con = sqlite3.connect(f"file:{YDB}?mode=ro", uri=True, timeout=120)
    run(a.market, a.strat, cost, a.stop, a.target, a.max_trades, a.vol_filter, con)


if __name__ == "__main__":
    main()
