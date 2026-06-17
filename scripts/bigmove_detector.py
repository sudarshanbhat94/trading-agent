"""Big-move detector - multi-signal hunt for stocks about to run hard.

Tests several orthogonal 'ignition' signals (not one) on daily data, and scores
each by what actually matters for catching runners:
  precision = P(big move | signal),  lift = precision / base rate,
  recall    = % of all big movers flagged,  net expectancy after costs.
A signal is useful only if lift > 1 AND net expectancy > baseline.

Big move := forward 10-day MAX gain >= 15% (it ran, catchable with a trail).
Enter next-day open. India-first.
"""
from __future__ import annotations
import argparse, sqlite3
import numpy as np, pandas as pd

DB = "/opt/opentrade/var/trading_agent.db"
DAILY = {"IN": "upstox-live:day", "US": "alpaca-iex-live:day"}
COST = {"IN": 0.30, "US": 0.12}
BIG = 0.15      # 15%+ forward max move
HOLD = 10


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
    return df, mcum


def deliv(con):
    cols = [r[1] for r in con.execute("PRAGMA table_info(delivery_data)")]
    dc = "date" if "date" in cols else "ts"
    pc = [c for c in cols if "deliv" in c.lower() and "pct" in c.lower()] or [c for c in cols if "deliv" in c.lower()]
    if not pc:
        return {}
    d = pd.read_sql_query(f"SELECT symbol,{dc} AS d,{pc[0]} AS dp FROM delivery_data", con)
    d["date"] = pd.to_datetime(d["d"].astype(str).str[:10], errors="coerce")
    d["dp"] = pd.to_numeric(d["dp"], errors="coerce")
    d = d.dropna(subset=["date", "dp"])
    return {s: g.set_index("date").dp.sort_index() for s, g in d.groupby("symbol")}


def build(df, mcum, dmap):
    frames = []
    mmom20 = mcum / mcum.shift(20) - 1
    for s, g in df.groupby("symbol"):
        g = g.set_index("date").copy()
        if len(g) < 80:
            continue
        c, h, l, v = g.close, g.high, g.low, g.volume
        g["sma20"] = c.rolling(20).mean(); g["sma50"] = c.rolling(50).mean()
        tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
        g["atr"] = tr.rolling(14).mean(); g["atr_pct"] = g.atr / c
        g["vol_contract"] = g.atr / g.atr.shift(20)
        g["rvol"] = v / v.rolling(20).mean()
        g["vol_z"] = (v - v.rolling(20).mean()) / v.rolling(20).std()
        g["hi20"] = h.rolling(20).max(); g["hi60"] = h.rolling(60).max()
        g["dist_hi20"] = c / g.hi20 - 1
        g["brk20"] = c > g.hi20.shift(1)
        g["brk60"] = c > g.hi60.shift(1)
        g["range_comp"] = (g.hi20 - l.rolling(20).min()) / c       # base width (tight = small)
        g["mom20"] = c / c.shift(20) - 1
        g["rs20"] = g.mom20 - mmom20.reindex(g.index)
        if s in dmap:
            dp = dmap[s].reindex(g.index).ffill(limit=5)
            g["deliv"] = dp; g["deliv_sp"] = dp - dp.rolling(20, min_periods=5).mean()
        else:
            g["deliv"] = np.nan; g["deliv_sp"] = np.nan
        # forward outcome: enter next open, 10-day hold + 10-day MAX
        nopen = g.open.shift(-1)
        g["fwd"] = g.close.shift(-(HOLD + 1)) / nopen - 1
        fmax = h.shift(-1).rolling(HOLD).max().shift(-(HOLD - 1))
        g["fwd_max"] = fmax / nopen - 1
        frames.append(g)
    return pd.concat(frames)


def evaluate(allf, cost):
    base = (allf["fwd_max"] >= BIG).mean()
    print(f"  base rate P(big +{int(BIG*100)}% in {HOLD}d) = {base*100:.1f}%   "
          f"baseline mean fwd = {allf['fwd'].mean()*100:+.2f}%")
    R = allf["range_comp"]
    dets = {
        "squeeze_breakout": (allf.vol_contract < 0.9) & allf.brk20 & (allf.rvol > 1.5),
        "vol_surge_strength": (allf.vol_z > 2) & (allf.rs20 > 0) & (allf.ret1 > 0.02),
        "pocket_pivot": (allf.ret1 > 0.04) & (allf.rvol > 2.5) & (allf.close > allf.sma20),
        "coiled_spring": (allf.vol_contract < 0.7) & (R < R.quantile(0.3)) & (allf.rs20 > 0),
        "new60_high_vol": allf.brk60 & (allf.rvol > 2) & (allf.rs20 > 0),
        "delivery_accum": (allf.deliv_sp > 5) & (allf.rvol > 1.3) & (allf.rs20 > 0),
        "gap_momentum": (allf.open / allf.close.shift(1) - 1 > 0.03) & (allf.rvol > 1.5),
    }
    print(f"  {'detector':20s} {'signals':>8} {'precision':>9} {'lift':>5} {'recall':>7} {'net_fwd':>8} {'maxavg':>7}")
    for name, mask in dets.items():
        sub = allf[mask & allf.fwd.notna()]
        if len(sub) < 50:
            print(f"  {name:20s} {len(sub):>8}  (too few)"); continue
        prec = (sub.fwd_max >= BIG).mean()
        lift = prec / base if base else 0
        recall = (mask & (allf.fwd_max >= BIG)).sum() / (allf.fwd_max >= BIG).sum()
        net = sub.fwd.mean() * 100 - cost
        maxavg = sub.fwd_max.mean() * 100
        flag = "  <==" if lift > 1.3 and net > 0 else ""
        print(f"  {name:20s} {len(sub):>8} {prec*100:>8.1f}% {lift:>5.2f} {recall*100:>6.1f}% "
              f"{net:>+7.2f}% {maxavg:>+6.2f}%{flag}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", default="IN")
    ap.add_argument("--min-turn", type=float, default=2e7)
    a = ap.parse_args()
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=120)
    df, mcum = load(con, a.market, a.min_turn)
    dmap = deliv(con) if a.market == "IN" else {}
    allf = build(df, mcum, dmap)
    print(f"[{a.market}] {df.symbol.nunique()} symbols, {len(allf):,} rows, "
          f"delivery_syms={len(dmap)}, cost={COST[a.market]}%")
    evaluate(allf, COST[a.market])
    con.close()


if __name__ == "__main__":
    main()
