"""Backtest the pre-trade factor-investigation pipeline.

Replays the SAME logic the live engine now uses — cross-sectional multi-factor
composite, graded regime (ON/NEUTRAL/OFF), hard gates (liquidity/drawdown/sector),
ATR/volatility position sizing — over history, and compares it to:
  OLD    : conviction-ranked mean-reversion (the prior logic)
  MARKET : the synthetic universe index (buy & hold)

Reports total return, CAGR, max drawdown, Sharpe, #trades, win rate.

  /opt/opentrade/.venv/bin/python3 scripts/backtest_investigation.py
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
from app import v2_engine as eng, factor_investigation as fi

DB = "/opt/opentrade/var/trading_agent.db"
COST = {"IN": 0.30, "US": 0.12}
START = "2024-01-01"
TOPN = 700
MAXPOS = 10
ATR_STOP = 2.0
ATR_TARGET = 3.5
HOLD = 8


def _rsi(c, n=14):
    d = c.diff()
    up = d.clip(lower=0).rolling(n).mean()
    dn = (-d.clip(upper=0)).rolling(n).mean()
    return (100 - 100 / (1 + up / dn.replace(0, np.nan))).fillna(50)


def _z(row):
    sd = row.std()
    if not sd or np.isnan(sd):
        return pd.Series(50.0, index=row.index)
    z = ((row - row.mean()) / sd).clip(-3, 3)
    return (50 + z / 3 * 50).clip(0, 100)


def build_matrices(syms, mdf):
    cols = {}
    for sym, g in syms.items():
        if len(g) < 150:
            continue
        gf = eng.compute_features(g, mdf)
        c, v = gf["close"], g["volume"]
        d = pd.DataFrame(index=gf.index)
        for k in ("open", "high", "low", "close", "atr14", "atr_pct", "mom20", "mom63",
                  "rs20", "rvol", "dist_hi20", "rng_pos", "vol_contract", "sma20", "sma50"):
            d[k] = gf[k] if k in gf else g[k]
        d["rsi"] = _rsi(c)
        d["sma50_slope"] = gf["sma50"] / gf["sma50"].shift(5) - 1
        d["trend_align"] = ((c > gf["sma20"]).astype(int) + (gf["sma20"] > gf["sma50"]).astype(int)
                            + (d["sma50_slope"] > 0).astype(int))
        d["turnover"] = (c * v).rolling(20).median()
        d["dd252"] = c / c.rolling(252, min_periods=60).max() - 1
        d["vol_trend"] = v.rolling(5).mean() / v.rolling(20).mean()
        d["conv"] = gf.apply(bt.conviction, axis=1)
        cols[sym] = d
    keys = list(next(iter(cols.values())).columns)
    return {k: pd.DataFrame({s: cols[s][k] for s in cols}) for k in keys}


def composite(M, d):
    c = lambda f: M[f].loc[d]
    valid = c("sma50").notna() & c("atr14").notna() & c("rs20").notna() & c("rvol").notna() & (c("close") > 0)
    idx = valid[valid].index
    if len(idx) < 20:
        return None
    def C(f):
        return c(f).reindex(idx)
    trend = (C("trend_align") / 3 * 60 + _z(C("sma50_slope")) * 0.4).clip(0, 100)
    momentum = (_z(C("mom63")) * 0.5 + _z(C("mom20")) * 0.3 + (100 - (C("rsi") - 50).abs() * 1.5).clip(0, 100) * 0.2)
    rel = _z(C("rs20"))
    volu = _z(C("rvol")) * 0.6 + _z(C("vol_trend")) * 0.4
    ideal = 0.025
    volq = (100 - ((C("atr_pct") - ideal).abs() / ideal * 55).clip(0, 100))
    setup_mr = ((-C("dist_hi20") / 0.10).clip(0, 1) * 55 + (1 - C("rng_pos")).clip(0, 1) * 45)
    liq = _z(C("turnover"))
    w = fi.WEIGHTS["swing_meanrev"]
    comp = (w["trend"] * trend + w["momentum"] * momentum + w["rel_strength"] * rel + w["volume"] * volu
            + w["vol_quality"] * volq + w["setup"] * setup_mr + w["liquidity"] * liq)
    return pd.DataFrame({"composite": comp, "atr_pct": C("atr_pct"), "turnover": C("turnover"), "dd252": C("dd252")})


def _metrics(curve, n_trades, wins):
    eq = pd.Series([e for _, e in curve])
    ret = (eq.iloc[-1] / eq.iloc[0] - 1) * 100
    days = max((curve[-1][0] - curve[0][0]).days, 1)
    cagr = ((eq.iloc[-1] / eq.iloc[0]) ** (365 / days) - 1) * 100
    peak = eq.cummax()
    maxdd = ((peak - eq) / peak).max() * 100
    dr = eq.pct_change().dropna()
    sharpe = (dr.mean() / dr.std() * np.sqrt(252)) if dr.std() else 0
    return dict(ret=ret, cagr=cagr, maxdd=maxdd, sharpe=sharpe, n=n_trades,
                win=(wins / n_trades * 100 if n_trades else 0))


def run(market, mode, M, mdf, extend=False, maxpos=MAXPOS, sweep=False, mom=False, maxatr=0.0):
    reg_mean = mdf["mkt_cum"].rolling(50).mean()
    reg_trend = mdf["mkt_cum"] / mdf["mkt_cum"].shift(21) - 1
    dates = [d for d in M["close"].index if d >= pd.Timestamp(START)]
    cash = equity = 100000.0
    pos, pending, curve = {}, [], []
    n = wins = 0
    cside = COST[market] / 200.0
    MOM_CAP = 5                          # momentum sleeve: at most 5 of the book
    last_state = "OFF"                   # yesterday's regime, for the sweep gate
    for di, d in enumerate(dates):
        # execute pending at open
        for sym, sz, strat in pending:
            if len(pos) >= maxpos or sym in pos:
                continue
            o = M["open"][sym].get(d, np.nan)
            atr = M["atr14"][sym].get(d, np.nan)
            if not (o > 0 and atr > 0):
                continue
            if maxatr and atr / o > maxatr:      # cap worst-case stop distance
                continue
            alloc = min(equity / maxpos * sz, cash)
            if alloc <= 0:
                continue
            sh = alloc / o
            cash -= sh * o * (1 + cside)
            pos[sym] = dict(sh=sh, entry=o, stop=o - ATR_STOP * atr,
                            tgt=(0.0 if strat == "mom" else o + ATR_TARGET * atr),
                            atr=atr, eday=di, peak=o, strat=strat)
        pending = []
        # exits
        for sym in list(pos):
            hi = M["high"][sym].get(d, np.nan); lo = M["low"][sym].get(d, np.nan); cl = M["close"][sym].get(d, np.nan)
            if np.isnan(cl):
                continue
            p = pos[sym]
            p["peak"] = max(p["peak"], hi if hi == hi else cl)
            eff = p["stop"]
            if p["peak"] >= p["entry"] + 3.0 * p["atr"]:   # breakeven lock (matches live)
                eff = max(eff, p["entry"])
            if p.get("ext") or p.get("strat") == "mom":    # momentum / extended: trail
                eff = max(eff, p["peak"] - 2.5 * p["atr"])
            px = None
            hold_cap = 40 if p.get("strat") == "mom" else HOLD
            if lo <= eff:
                px = min(eff, M["open"][sym].get(d, eff))
            elif p["tgt"] and hi >= p["tgt"]:
                px = max(p["tgt"], M["open"][sym].get(d, p["tgt"]))
            elif di - p["eday"] >= hold_cap:
                # winner-extension: at the clock, keep winners (>1 ATR up) on a
                # trail instead of force-selling; stagnant trades still exit.
                if extend and p.get("strat") != "mom" and cl >= p["entry"] + 1.0 * p["atr"] and di - p["eday"] < 20:
                    p["ext"] = True
                else:
                    px = cl
            if px is not None:
                cash += p["sh"] * px * (1 - cside)
                n += 1; wins += px > p["entry"]
                del pos[sym]
        # cash equitization: idle cash above a one-slot reserve earns the index
        # return instead of zero (paper equivalent of sweeping into NIFTYBEES/VOO).
        # REGIME-GATED: only while the market is trending up — in OFF/NEUTRAL the
        # cash IS the defense (an always-on sweep turned IN -7% into -20%).
        if sweep and last_state == "STRONG":
            mret_d = mdf["mkt_ret1"].get(d, 0.0)
            if mret_d == mret_d:
                reserve = equity / maxpos
                idle = max(0.0, cash - reserve)
                cash += idle * mret_d
        def _px(s, fb):
            x = M["close"][s].get(d, fb)
            return fb if (x is None or x != x or x <= 0) else x
        mtm = sum(pb["sh"] * _px(s, pb["entry"]) for s, pb in pos.items())
        equity = cash + mtm
        curve.append((d, equity))
        # regime
        mc = mdf["mkt_cum"].get(d, np.nan)
        rm = reg_mean.get(d, np.nan); rt = reg_trend.get(d, 0)
        if np.isnan(mc) or np.isnan(rm):
            continue
        ratio = mc / rm - 1
        state = "ON" if (ratio > 0 and rt > -0.03) else ("OFF" if (ratio < -0.02 or rt < -0.03) else "NEUTRAL")
        # boosters (momentum sleeve, cash sweep) demand STRONG trend confirmation:
        # comfortably above the mean AND rising - regular ON was full of bull traps
        strong = ratio > 0.02 and rt > 0.01
        last_state = "STRONG" if strong else state
        slots = maxpos - len(pos)
        if slots <= 0:
            continue
        if mode == "NEW":
            if state == "OFF":
                continue
            cp = composite(M, d)
            if cp is None:
                continue
            buy_min = fi.BUY_MIN + (8.0 if state == "NEUTRAL" else 0.0)
            elig = cp[(cp["composite"] >= buy_min) & (cp["turnover"] >= fi.LIQ_FLOOR[market])
                      & (cp["dd252"] >= fi.MAX_DRAWDOWN)]
            elig = elig.sort_values("composite", ascending=False)
            for sym, r in elig.head(slots).iterrows():
                if sym not in pos and sym not in {t[0] for t in pending}:
                    pending.append((sym, float(np.clip(0.025 / max(r["atr_pct"], 0.005), 0.4, 1.4)), "mr"))
        elif mode == "HYBRID":
            # momentum-breakout sleeve FIRST (it was starved when meanrev filled
            # every slot): confirmed uptrend only — near 52w high on volume with
            # positive 3m momentum, trail-exit.
            if mom and strong:
                mom_open = sum(1 for p in pos.values() if p.get("strat") == "mom")
                room = min(MOM_CAP - mom_open, slots)
                if room > 0:
                    dd = M["dd252"].loc[d]; rv = M["rvol"].loc[d]; m63 = M["mom63"].loc[d]
                    ap = M["atr_pct"].loc[d]; to = M["turnover"].loc[d]
                    elig = (dd >= -0.02) & (rv >= 1.5) & (m63 > 0.10) & (to >= fi.LIQ_FLOOR[market])
                    cands = m63[elig].dropna().sort_values(ascending=False)
                    for sym in cands.index:
                        if room <= 0:
                            break
                        if sym in pos or sym in {t[0] for t in pending}:
                            continue
                        sz = float(np.clip(0.025 / max(float(ap.get(sym, 0.025)) or 0.025, 0.005), 0.4, 1.4))
                        pending.append((sym, sz, "mom")); room -= 1
            # PROVEN conviction ranking + the investigation's gates + vol sizing
            if state != "OFF":
                conv = M["conv"].loc[d].dropna()
                ranked = conv[conv >= 0.55].sort_values(ascending=False)
                cp = composite(M, d)
                added = len(pending)
                for sym in ranked.index:
                    if added >= slots:
                        break
                    if sym in pos or sym in {t[0] for t in pending}:
                        continue
                    sz = 1.0
                    if cp is not None and sym in cp.index:
                        row = cp.loc[sym]
                        if row["turnover"] < fi.LIQ_FLOOR[market] or row["dd252"] < fi.MAX_DRAWDOWN:
                            continue
                        sz = float(np.clip(0.025 / max(row["atr_pct"], 0.005), 0.4, 1.4))
                    pending.append((sym, sz, "mr")); added += 1
        else:  # OLD: conviction-ranked, simple regime on
            if not (ratio > 0):
                continue
            conv = M["conv"].loc[d].dropna()
            elig = conv[conv >= 0.55].sort_values(ascending=False)
            for sym in elig.head(slots).index:
                if sym not in pos and sym not in {t[0] for t in pending}:
                    pending.append((sym, 1.0, "mr"))
    return _metrics(curve, n, wins), curve[-1][1]


def main():
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=180)
    print(f"backtest window from {START}, topn={TOPN}\n")
    for market in ("IN", "US"):
        syms, mdf = bt.load_market(con, market, TOPN)
        M = build_matrices(syms, mdf)
        mkt = mdf["mkt_cum"]
        mkt = mkt[mkt.index >= pd.Timestamp(START)]
        mret = (mkt.iloc[-1] / mkt.iloc[0] - 1) * 100
        print(f"===== {market} (universe={len(M['close'].columns)}) =====")
        print(f"  MARKET buy&hold: {mret:+.1f}%")
        for label, kw in (("mom base", dict(mom=True)),
                          ("mom atr<4.5%", dict(mom=True, maxatr=0.045)),
                          ("mom atr<3.5%", dict(mom=True, maxatr=0.035))):
            m, _ = run(market, "HYBRID", M, mdf, maxpos=14, **kw)
            print(f"  {label:12s}: ret={m['ret']:+7.1f}%  CAGR={m['cagr']:+6.1f}%  maxDD={m['maxdd']:4.1f}%  "
                  f"Sharpe={m['sharpe']:.2f}  trades={m['n']:4d}  win={m['win']:.0f}%")
        print()
    con.close()


if __name__ == "__main__":
    main()
