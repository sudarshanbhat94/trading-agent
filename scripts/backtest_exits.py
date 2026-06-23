"""A/B backtest: current exits vs proposed (breakeven + ATR-chandelier trail).

Validates the exit-strategy change on real history for BOTH strategies and BOTH
markets before it goes live. Holds entries constant; only the exit rule varies.

  swing (mean-reversion): OLD = stop/target/time (no trail)
                          NEW = stop + breakeven(>=1 ATR) + chandelier(k*ATR) + target/time
  gap (momentum):         CUR = 10% trailing stop
                          NEW = breakeven + chandelier(k*ATR) trail

Run on the OCI box (has the candle history):
  /opt/opentrade/.venv/bin/python3 scripts/backtest_exits.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, "/opt/opentrade")
sys.path.insert(0, "/opt/opentrade/scripts")

import sqlite3
import numpy as np
import pandas as pd

import backtest_v2 as bt
import gap_momentum as gm

DB = "/opt/opentrade/var/trading_agent.db"
COST = {"IN": 0.30, "US": 0.12}
START = None   # full available history


# ---------------------------------------------------------------- swing portfolio
def swing(con, market, mode, k=2.5, be_atr=1.0, topn=700, thresh=0.55, hold=8,
          max_pos=10, atr_stop=2.0, atr_target=3.5):
    syms, mdf = bt.load_market(con, market, topn)
    reg = (mdf["mkt_cum"] > mdf["mkt_cum"].rolling(50).mean())
    bars, all_dates = {}, set()
    for sym, g in syms.items():
        if len(g) < 90:
            continue
        g = bt.features(g, mdf)
        g["conv"] = g.apply(bt.conviction, axis=1)
        d = {}
        for ts, r in g.iterrows():
            if pd.isna(r["close"]) or pd.isna(r["open"]):
                continue
            d[ts] = (r["open"], r["high"], r["low"], r["close"], r["atr14"], r["conv"])
            all_dates.add(ts)
        bars[sym] = d
    dates = sorted(all_dates)
    if START:
        dates = [d for d in dates if d >= pd.Timestamp(START)]
    cash = equity = 100000.0
    pos, pending, curve, gb = {}, [], [], []
    n = wins = 0
    cside = COST[market] / 200.0
    peakeq = equity
    maxdd = 0.0
    for di, d in enumerate(dates):
        slots = max_pos - len(pos)
        for sym, conv in pending:
            if slots <= 0:
                break
            b = bars.get(sym, {}).get(d)
            if not b or sym in pos:
                continue
            o, _, _, _, atr, _ = b
            if not (o > 0 and atr > 0):
                continue
            alloc = min(equity / max_pos, cash)
            if alloc <= 0:
                continue
            sh = alloc / o
            cash -= sh * o * (1 + cside)
            pos[sym] = dict(shares=sh, entry=o, stop=o - atr_stop * atr,
                            target=o + atr_target * atr, eday=di, peak=o)
            slots -= 1
        pending = []
        for sym in list(pos.keys()):
            b = bars.get(sym, {}).get(d)
            if not b:
                continue
            o, h, l, c, atr, conv = b
            p = pos[sym]
            p["peak"] = max(p["peak"], h)
            eff = p["stop"]
            if atr > 0 and p["peak"] >= p["entry"] + be_atr * atr and mode in ("new", "be"):
                eff = max(eff, p["entry"])             # breakeven lock
            if mode == "new" and atr > 0:
                eff = max(eff, p["peak"] - k * atr)    # chandelier trail
            expx = None
            if l <= eff:
                expx = min(eff, o)
            elif h >= p["target"]:
                expx = max(p["target"], o)
            elif di - p["eday"] >= hold:
                expx = c
            if expx is not None:
                realized = (expx / p["entry"] - 1) * 100
                gb.append((p["peak"] / p["entry"] - 1) * 100 - realized)
                cash += p["shares"] * expx * (1 - cside)
                n += 1
                wins += expx > p["entry"]
                del pos[sym]
        mtm = sum(pb["shares"] * (bars.get(s, {}).get(d, (0, 0, 0, pb["entry"]))[3] or pb["entry"])
                  for s, pb in pos.items())
        equity = cash + mtm
        peakeq = max(peakeq, equity)
        maxdd = max(maxdd, (peakeq - equity) / peakeq * 100)
        curve.append(equity)
        if reg is not None and bool(reg.get(d, False)):
            cands = []
            for sym, dd in bars.items():
                b = dd.get(d)
                if b and b[5] >= thresh and sym not in pos:
                    cands.append((sym, b[5]))
            cands.sort(key=lambda x: -x[1])
            pending = cands[:max_pos]
    tot = (equity / 100000.0 - 1) * 100
    return dict(ret=tot, n=n, win=(wins / n * 100 if n else 0), dd=maxdd,
                giveback=(float(np.median(gb)) if gb else 0.0))


# ---------------------------------------------------------------- gap per-trade
def gap(con, market, mode, trail=0.10, k=3.0, be_atr=1.0, atr_target=0.0,
        g_thr=0.08, t_tight=0.04, min_turn=2e7,
        gap_min=0.03, gap_max=0.15, rvol_min=1.5, atr_stop=1.5, max_days=20):
    syms, mcum = gm.load(con, market, min_turn)
    reg = mcum > mcum.rolling(50).mean()
    mmom20 = mcum / mcum.shift(20) - 1
    rets, gbs = [], []
    cost = COST[market]
    for s, g in syms.items():
        if len(g) < 80:
            continue
        c, h, l, o, v = g.close, g.high, g.low, g.open, g.volume
        tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
        atr = tr.rolling(14).mean()
        rvol = v / v.rolling(20).mean()
        gp = o / c.shift(1) - 1
        idx = g.index
        i = 0
        while i < len(g) - 1:
            if not ((gap_min <= gp.iloc[i] <= gap_max) and rvol.iloc[i] >= rvol_min and atr.iloc[i] > 0):
                i += 1
                continue
            entry = o.iloc[i + 1] if i + 1 < len(g) else None
            if not entry or entry <= 0:
                i += 1
                continue
            a = atr.iloc[i]
            stop = entry - atr_stop * a
            target = entry * (1 + atr_target) if atr_target else None   # % target (e.g. 0.08 = +8%)
            peak = entry
            expx = None
            j = i + 1
            while j < min(i + 1 + max_days, len(g)):
                hi, lo, op = h.iloc[j], l.iloc[j], o.iloc[j]
                peak = max(peak, hi)
                gain = peak / entry - 1
                if mode == "cur":
                    eff = max(stop, peak * (1 - trail))
                elif mode == "tiered":           # wide trail early, tight once it's a real winner
                    eff = max(stop, peak * (1 - (t_tight if gain >= g_thr else trail)))
                elif mode == "target":           # take profit at a % target, else 10% trail
                    eff = max(stop, peak * (1 - trail))
                else:                            # new: breakeven + chandelier
                    eff = stop
                    if peak >= entry + be_atr * a:
                        eff = max(eff, entry)
                    eff = max(eff, peak - k * a)
                if target and hi >= target:      # profit target hit first (conservative: fill at target or gap-open)
                    expx = max(target, op) if op > target else target
                    break
                if lo <= eff:
                    expx = eff
                    break
                j += 1
            if expx is None:
                expx = c.iloc[min(i + max_days, len(g) - 1)]
            ret = (expx / entry - 1) * 100 - cost
            gbs.append((peak / entry - 1) * 100 - (expx / entry - 1) * 100)
            rets.append(ret)
            i = j + 1
    if not rets:
        return dict(n=0, exp=0, win=0, pf=0, giveback=0)
    t = pd.Series(rets)
    pf = t[t > 0].sum() / abs(t[t <= 0].sum() or 1)
    return dict(n=len(t), exp=float(t.mean()), win=float((t > 0).mean() * 100),
                pf=float(pf), giveback=float(np.median(gbs)))


def main():
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=180)
    print("=" * 78)
    print("SWING (mean-reversion) — OLD stop/target/time  vs  NEW breakeven+chandelier")
    print("=" * 78)
    for mkt in ("IN", "US"):
        o = swing(con, mkt, "old")
        print(f"[{mkt}] OLD          ret={o['ret']:+7.1f}%  win={o['win']:4.1f}%  n={o['n']:4d}  maxDD={o['dd']:4.1f}%  med_giveback={o['giveback']:4.1f}pp")
        be = swing(con, mkt, "be", be_atr=3.0)
        print(f"[{mkt}] BE@3ATR      ret={be['ret']:+7.1f}%  win={be['win']:4.1f}%  n={be['n']:4d}  maxDD={be['dd']:4.1f}%  med_giveback={be['giveback']:4.1f}pp")
        for k in (2.5,):
            nw = swing(con, mkt, "new", k=k)
            print(f"[{mkt}] NEW k={k:>3}    ret={nw['ret']:+7.1f}%  win={nw['win']:4.1f}%  n={nw['n']:4d}  maxDD={nw['dd']:4.1f}%  med_giveback={nw['giveback']:4.1f}pp")
    print()
    print("=" * 78)
    print("GAP (momentum) — profit-protection sweep: target & tiered (give room early, lock late)")
    print("=" * 78)

    def show(mkt, label, r):
        print(f"[{mkt}] {label:18s} exp={r['exp']:+5.2f}%/trade  win={r['win']:4.1f}%  PF={r['pf']:.2f}  n={r['n']:5d}  med_giveback={r['giveback']:4.1f}pp")

    for mkt in ("IN", "US"):
        show(mkt, "CUR trail10%", gap(con, mkt, "cur", trail=0.10))
        # tiered: 10% trail until +G, then tighten to T (locks PARAS-type runners)
        for g_thr, t_tight in ((0.06, 0.03), (0.08, 0.04), (0.10, 0.05)):
            show(mkt, f"tiered {int(g_thr*100)}->{int(t_tight*100)}%",
                 gap(con, mkt, "tiered", trail=0.10, g_thr=g_thr, t_tight=t_tight))
        # fixed % profit target (+ keep 10% trail underneath)
        for tg in (0.08, 0.12, 0.15):
            show(mkt, f"target +{int(tg*100)}%", gap(con, mkt, "target", trail=0.10, atr_target=tg))
        print()
    con.close()


if __name__ == "__main__":
    main()
