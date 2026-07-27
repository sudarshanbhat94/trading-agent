"""Meta-labeling research harness for the OpenStocks v2 engine.

Question this answers HONESTLY, on real OCI data:
  "Among the trades our live engine would actually take (swing_meanrev +
   gap_momentum), can a secondary ML model — trained only on point-in-time
   features — filter out the losers and lift win-rate / net return, out of
   sample?"

This is Marcos Lopez de Prado's meta-labeling (Advances in Financial ML):
  - PRIMARY model decides the SIDE (our existing deterministic conviction
    engine, replayed exactly via app.v2_engine).
  - TRIPLE-BARRIER labels each historical signal by replaying the engine's OWN
    exit plan (ATR stop / target / trailing / time) bar-by-bar on forward
    candles, charging realistic round-trip cost. label = 1 if the trade made
    money net of cost, else 0.
  - META model (gradient-boosted trees) learns P(win) from features known at
    signal time, and we only take signals above a train-chosen probability.
  - Evaluation is PURGED WALK-FORWARD with an embargo >= max hold, so a test
    fold never shares overlapping-outcome bars with its training fold.

We DON'T train on the 29 live paper trades (far too few). We generate thousands
of labeled signal-events by replaying the primary model over the full daily
candle history. The live trades are a separate sanity check.

Run on the OCI box (read-only on the candle DB):
  python3 scripts/meta_label_research.py --db /opt/opentrade/var/trading_agent.db \
      --market IN --topn 700 --out /tmp/meta_IN.csv
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import sqlite3
import sys

import numpy as np
import pandas as pd

# ---- load the REAL primary engine by file path (no app package import, so we
# don't drag in fastapi/config just to backtest) -----------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
_ENG_PATH = os.path.join(_HERE, "..", "app", "v2_engine.py")


def _load_engine():
    spec = importlib.util.spec_from_file_location("v2_engine", _ENG_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


eng = _load_engine()

# Trade plans copied verbatim from app/v2_live.py PLAN + GAP_TARGET so the
# label simulation matches what the engine actually does.
PLAN = {
    "swing_meanrev": dict(threshold=0.55, atr_stop=2.0, atr_target=3.5, trail=0.0, hold=8, regime_gated=True),
    "gap_momentum":  dict(threshold=0.0,  atr_stop=1.5, atr_target=0.0, trail=0.10, hold=20, regime_gated=False,
                          gap_target=0.10),  # GAP_TARGET["IN"]
}
GAP_MIN, GAP_MAX, GAP_RVOL_MIN = 0.03, 0.15, 1.5


# --------------------------------------------------------------------------- #
#  1. Panel + features (precomputed once per symbol == per-date, all windows   #
#     are backward-looking so a past row is identical either way)              #
# --------------------------------------------------------------------------- #
# Qlib Alpha158-style engineered factors (vectorized subset, non-redundant with
# the engine's existing features). Candle geometry + rolling momentum/volatility/
# trend-quality/price-volume factors the meta-model can learn from.
ALPHA_COLS = ["a_kmid", "a_klen", "a_kup", "a_klow", "a_roc5", "a_roc10",
              "a_std5", "a_std20", "a_std60", "a_corr20", "a_cntp20",
              "a_sump20", "a_vstd20", "a_wvma20"]


def _add_alpha_factors(g):
    eps = 1e-12
    c, o, h, l, v = g["close"], g["open"], g["high"], g["low"], g["volume"]
    g["a_kmid"] = (c - o) / (o + eps)                          # candle body
    g["a_klen"] = (h - l) / (o + eps)                          # candle range
    g["a_kup"] = (h - np.maximum(o, c)) / (o + eps)            # upper shadow
    g["a_klow"] = (np.minimum(o, c) - l) / (o + eps)           # lower shadow
    ret = c.pct_change()
    g["a_roc5"] = c / c.shift(5) - 1
    g["a_roc10"] = c / c.shift(10) - 1
    g["a_std5"] = ret.rolling(5).std()
    g["a_std20"] = ret.rolling(20).std()
    g["a_std60"] = ret.rolling(60).std()
    lv = np.log(v.replace(0, np.nan) + 1.0)
    g["a_corr20"] = c.rolling(20).corr(lv)                     # price-volume corr
    g["a_cntp20"] = (ret > 0).astype(float).rolling(20).mean()  # up-day fraction
    gain = ret.clip(lower=0)
    loss = (-ret).clip(lower=0)
    g["a_sump20"] = gain.rolling(20).sum() / (gain.rolling(20).sum() + loss.rolling(20).sum() + eps)
    g["a_vstd20"] = v.rolling(20).std() / (v.rolling(20).mean() + eps)
    g["a_wvma20"] = (ret.abs() * v).rolling(20).sum() / (v.rolling(20).sum() + eps)
    return g


def build_panel(db, market, topn):
    con = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
    try:
        syms, mdf = eng.load_panel(con, market, topn=topn)
    finally:
        con.close()
    feats = {}
    for sym, g in syms.items():
        gf = eng.compute_features(g, mdf)
        # gap = today's open vs yesterday's close; rvol already in features
        gf["gap"] = gf["open"] / gf["close"].shift(1) - 1
        gf = _add_alpha_factors(gf)
        feats[sym] = gf
    return syms, feats, mdf


# --------------------------------------------------------------------------- #
#  2. Triple-barrier label: replay the engine's own exit on forward candles    #
# --------------------------------------------------------------------------- #
def simulate_exit(g, entry_i, plan, atr):
    """Enter at bar entry_i's OPEN, then walk forward up to `hold` bars applying
    ATR stop / fixed target / trailing stop / time exit exactly like the engine.
    Returns (raw % return pre-cost, hold_days) or None if not enough forward
    bars. hold_days = trading bars the capital stays committed (for the
    portfolio sim to know when a slot frees up)."""
    n = len(g)
    if entry_i >= n:
        return None
    o = g["open"].to_numpy(); h = g["high"].to_numpy()
    lo = g["low"].to_numpy(); c = g["close"].to_numpy()
    entry = float(o[entry_i])
    if not np.isfinite(entry) or entry <= 0 or atr <= 0:
        return None
    stop = entry - plan["atr_stop"] * atr
    target = entry * (1 + plan["gap_target"]) if plan.get("gap_target") else (
        entry + plan["atr_target"] * atr if plan["atr_target"] else None)
    trail = plan.get("trail") or 0.0
    hold = plan["hold"]
    peak = entry
    last = min(entry_i + hold, n - 1)
    for j in range(entry_i, last + 1):
        if trail:
            peak = max(peak, float(h[j]))
            eff_stop = max(stop, peak * (1 - trail))
        else:
            eff_stop = stop
        # stop checked before target (conservative: assume adverse fill first)
        if float(lo[j]) <= eff_stop:
            return eff_stop / entry - 1, max(1, j - entry_i + 1)
        if target is not None and float(h[j]) >= target:
            return target / entry - 1, max(1, j - entry_i + 1)
    return float(c[last]) / entry - 1, max(1, last - entry_i + 1)  # time exit


# --------------------------------------------------------------------------- #
#  3. Assemble the labeled event dataset                                        #
# --------------------------------------------------------------------------- #
FEATURE_COLS = ["conviction", "mom20", "mom63", "rs20", "atr_pct", "dist_hi20",
                "rng_pos", "rvol", "vol_contract", "mkt_mom20", "gap_pct",
                "regime_on", "regime_neutral", "regime_strong", "is_gap",
                "day_rank", "day_breadth", "dow"] + ALPHA_COLS


def assemble(syms, feats, mdf, market):
    cost = eng.COST_PCT[market]
    dates = eng.complete_trading_dates(syms)
    date_index = {d: i for i, d in enumerate(dates)}
    # precompute regime per date once
    reg = {d: (eng.regime_state(mdf, d), eng.regime_strong(mdf, d)) for d in dates}
    rows = []
    for di, d in enumerate(dates):
        if di + 1 >= len(dates):
            break  # need a next bar to enter on
        state, strong = reg[d]
        day_events = []
        for sym, gf in feats.items():
            if d not in gf.index:
                continue
            g = syms[sym]
            if d not in g.index:
                continue
            row = gf.loc[d]
            atr = float(row["atr14"]) if pd.notna(row["atr14"]) else 0.0
            if atr <= 0:
                continue
            conv = eng.conviction(row)
            gap = float(row["gap"]) if pd.notna(row["gap"]) else np.nan
            cands = []
            # swing_meanrev candidate (regime-gated: engine blocks it when OFF)
            if conv >= PLAN["swing_meanrev"]["threshold"] and state != "OFF":
                cands.append(("swing_meanrev", conv, 0.0))
            # gap_momentum candidate (not regime-gated)
            if pd.notna(gap) and GAP_MIN <= gap <= GAP_MAX and float(row["rvol"]) >= GAP_RVOL_MIN:
                cands.append(("gap_momentum", round(min(gap / GAP_MAX, 1.0), 4), gap * 100))
            for strat, score, gap_pct in cands:
                entry_i = g.index.get_loc(d) + 1
                res = simulate_exit(g, entry_i, PLAN[strat], atr)
                if res is None:
                    continue
                raw, hold_days = res
                net = raw * 100 - cost  # net % after round-trip cost
                ev = dict(
                    date=str(d.date()), symbol=sym, strategy=strat,
                    conviction=round(float(score), 4),
                    mom20=float(row["mom20"]) if pd.notna(row["mom20"]) else 0.0,
                    mom63=float(row["mom63"]) if pd.notna(row["mom63"]) else 0.0,
                    rs20=float(row["rs20"]) if pd.notna(row["rs20"]) else 0.0,
                    atr_pct=float(row["atr_pct"]) if pd.notna(row["atr_pct"]) else 0.0,
                    dist_hi20=float(row["dist_hi20"]) if pd.notna(row["dist_hi20"]) else 0.0,
                    rng_pos=float(row["rng_pos"]) if pd.notna(row["rng_pos"]) else 0.0,
                    rvol=float(row["rvol"]) if pd.notna(row["rvol"]) else 0.0,
                    vol_contract=float(row["vol_contract"]) if pd.notna(row["vol_contract"]) else 0.0,
                    mkt_mom20=float(row["mkt_mom20"]) if pd.notna(row["mkt_mom20"]) else 0.0,
                    gap_pct=float(gap_pct),
                    regime_on=1 if state == "ON" else 0,
                    regime_neutral=1 if state == "NEUTRAL" else 0,
                    regime_strong=1 if strong else 0,
                    is_gap=1 if strat == "gap_momentum" else 0,
                    dow=d.dayofweek,
                    net_ret=round(net, 4),
                    hold_days=int(hold_days),
                    label=1 if net > 0 else 0,
                )
                for col in ALPHA_COLS:
                    ev[col] = round(float(row[col]), 6) if pd.notna(row.get(col)) else 0.0
                day_events.append(ev)
        # per-day breadth + conviction rank (point-in-time, known at eod d)
        day_events.sort(key=lambda e: -e["conviction"])
        for rk, e in enumerate(day_events):
            e["day_rank"] = rk
            e["day_breadth"] = len(day_events)
        rows.extend(day_events)
    df = pd.DataFrame(rows)
    return df


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="/opt/opentrade/var/trading_agent.db")
    ap.add_argument("--market", default="IN")
    ap.add_argument("--topn", type=int, default=700)
    ap.add_argument("--out", default="/tmp/meta_events.csv")
    a = ap.parse_args()
    print("loading panel %s topn=%d ..." % (a.market, a.topn), flush=True)
    syms, feats, mdf = build_panel(a.db, a.market, a.topn)
    print("symbols=%d  trading-dates=%d" % (len(syms), len(eng.complete_trading_dates(syms))), flush=True)
    df = assemble(syms, feats, mdf, a.market)
    if df.empty:
        print("NO EVENTS — check data")
        return
    df.to_csv(a.out, index=False)
    print("\n=== labeled event dataset -> %s ===" % a.out)
    print("events: %d   date range: %s .. %s" % (len(df), df["date"].min(), df["date"].max()))
    for strat, g in df.groupby("strategy"):
        wr = g["label"].mean() * 100
        avg = g["net_ret"].mean()
        print("  %-14s n=%5d  base win-rate=%5.1f%%  avg net/trade=%+.2f%%  total=%+.0f%%"
              % (strat, len(g), wr, avg, g["net_ret"].sum()))
    print("  %-14s n=%5d  base win-rate=%5.1f%%  avg net/trade=%+.2f%%"
          % ("ALL", len(df), df["label"].mean() * 100, df["net_ret"].mean()))


if __name__ == "__main__":
    main()
