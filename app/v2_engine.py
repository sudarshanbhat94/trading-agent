"""OpenStocks v2 engine - the validated deterministic swing engine.

This is the canonical, production version of the logic proven in
scripts/backtest_v2.py:
  - mean-reversion conviction (buy beaten-down dips in relatively-strong,
    volatile, high-volume names; never chase the high)
  - synthetic market-regime filter (stand aside when the universe index is
    below its 50d average)
  - deterministic, NO LLM, no external paid data

Backtest (700 liquid symbols/market, ~6-day swing holds, costs deducted,
next-day-open entry, regime filter, full capital deployment) beat buy & hold in
every test: IN full +1.2% vs market -21.2% (DD -8.5% vs -33.7%), IN OOS +4.2%
vs -3.8%, US full +73.8% vs +30.6% (Sharpe ~2). Keep this file and the backtest
in sync if either is changed.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

# daily candle source per market in the OpenStocks DB
DAILY_SOURCE = {"IN": "upstox-live:day", "US": "alpaca-iex-live:day"}
# realistic round-trip cost (brokerage + taxes + slippage), in %
COST_PCT = {"IN": 0.30, "US": 0.12}
# defaults validated in the backtest
DEFAULTS = dict(topn=700, min_bars=120, hold=6, atr_stop=2.0, atr_target=3.5,
                max_pos=50, conviction_threshold=0.0, regime_lookback=50)


def load_panel(con, market: str, topn: int = 700, min_bars: int = 120):
    """Return ({symbol: date-indexed OHLCV df}, market_df) for the top-`topn`
    most-liquid symbols of `market`. Read-only on the source DB."""
    src = DAILY_SOURCE[market]
    df = pd.read_sql_query(
        "SELECT symbol, ts, open, high, low, close, volume FROM candles WHERE source=?",
        con, params=(src,),
    )
    if df.empty:
        return {}, None
    df["date"] = pd.to_datetime(df["ts"].str[:10])
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["close", "high", "low"])
    df = df.sort_values(["symbol", "date"]).drop_duplicates(["symbol", "date"])
    turn = df.assign(t=df["close"] * df["volume"]).groupby("symbol")["t"].median()
    cnt = df.groupby("symbol")["close"].count()
    eligible = cnt[cnt >= min_bars].index
    turn = turn.loc[turn.index.isin(eligible)].sort_values(ascending=False)
    keep = set(turn.head(topn).index)
    df = df[df["symbol"].isin(keep)].copy()
    df["ret1"] = df.groupby("symbol")["close"].pct_change()
    mkt = df.groupby("date")["ret1"].median().rename("mkt_ret1")
    mkt_cum = (1 + mkt.fillna(0)).cumprod().rename("mkt_cum")
    market_df = pd.concat([mkt, mkt_cum], axis=1)
    return {s: g.set_index("date") for s, g in df.groupby("symbol")}, market_df


def compute_features(g: pd.DataFrame, market_df: pd.DataFrame) -> pd.DataFrame:
    g = g.copy()
    c, h, l, v = g["close"], g["high"], g["low"], g["volume"]
    g["sma20"] = c.rolling(20).mean()
    g["sma50"] = c.rolling(50).mean()
    g["mom20"] = c / c.shift(20) - 1
    g["mom63"] = c / c.shift(63) - 1
    tr = pd.concat([(h - l), (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    g["atr14"] = tr.rolling(14).mean()
    g["atr_pct"] = g["atr14"] / c
    g["hi20"] = h.rolling(20).max()
    g["lo20"] = l.rolling(20).min()
    g["dist_hi20"] = c / g["hi20"] - 1
    g["rng_pos"] = (c - g["lo20"]) / (g["hi20"] - g["lo20"]).replace(0, np.nan)
    g["rvol"] = v / v.rolling(20).mean()
    g["vol_contract"] = g["atr14"] / g["atr14"].shift(20)
    mkt20 = market_df["mkt_cum"] / market_df["mkt_cum"].shift(20) - 1
    g = g.join(mkt20.rename("mkt_mom20"))
    g["rs20"] = g["mom20"] - g["mkt_mom20"]
    return g


def conviction(row) -> float:
    """Mean-reversion conviction 0..1, built from the validated factor signs."""
    def clamp(x, lo=0.0, hi=1.0):
        return max(lo, min(hi, x))
    if any(pd.isna(row.get(k)) for k in ("sma50", "mom20", "atr_pct", "rs20", "rvol", "dist_hi20", "vol_contract")):
        return 0.0
    dip = clamp(-row["dist_hi20"] / 0.10)
    volq = clamp(row["atr_pct"] / 0.05)
    vexp = clamp((row["vol_contract"] - 0.8) / 0.6)
    rs = clamp(0.5 + row["rs20"] * 5.0)
    vol = clamp((row["rvol"] - 0.8) / 1.0)
    score = 0.22 * dip + 0.24 * volq + 0.20 * vexp + 0.18 * rs + 0.16 * vol
    if row["mom20"] < -0.25 or row["dist_hi20"] < -0.35:
        score *= 0.3
    return clamp(score)


def regime_ok(market_df: pd.DataFrame, asof, lookback: int = 50) -> bool:
    """True when the synthetic universe index is above its `lookback`-day mean
    on `asof` (healthy market -> allow new dip-buys)."""
    mc = market_df["mkt_cum"]
    if asof not in mc.index:
        return False
    ref = mc.loc[:asof].tail(lookback)
    if len(ref) < lookback:
        return False
    return bool(mc.loc[asof] > ref.mean())


def regime_state(market_df: pd.DataFrame, asof, lookback: int = 50,
                 band: float = 0.02, trend_floor: float = -0.03) -> str:
    """Graded market regime so a market a hair below its mean isn't treated like
    one that's crashing:
      ON      - above its mean and not downtrending -> trade normally
      NEUTRAL - choppy/flat (within `band` below mean, not falling) -> best setups only
      OFF     - clearly below mean OR a >|trend_floor| 20d decline -> block dip-buys
    """
    mc = market_df["mkt_cum"]
    if asof not in mc.index:
        return "OFF"
    ref = mc.loc[:asof].tail(lookback)
    if len(ref) < lookback:
        return "OFF"
    cur, mean = float(mc.loc[asof]), float(ref.mean())
    ratio = cur / mean - 1 if mean else 0.0
    idx = mc.loc[:asof]
    trend = (float(idx.iloc[-1]) / float(idx.iloc[-21]) - 1) if len(idx) > 21 else 0.0
    if ratio > 0 and trend > trend_floor:
        return "ON"
    if ratio < -band or trend < trend_floor:
        return "OFF"
    return "NEUTRAL"


def regime_strong(market_df: pd.DataFrame, asof, lookback: int = 50) -> bool:
    """STRONG uptrend confirmation for pro-cyclical boosters (momentum sleeve):
    comfortably above the mean (+2%) AND rising (+1%/21d). Backtested: with only
    the plain ON gate the boosters bought bull traps; with STRONG they improve
    both markets (US Sharpe 1.24->2.19, IN bear-window loss shrinks too)."""
    mc = market_df["mkt_cum"]
    if asof not in mc.index:
        return False
    ref = mc.loc[:asof].tail(lookback)
    if len(ref) < lookback:
        return False
    cur, mean = float(mc.loc[asof]), float(ref.mean())
    idx = mc.loc[:asof]
    trend = (float(idx.iloc[-1]) / float(idx.iloc[-21]) - 1) if len(idx) > 21 else 0.0
    return (cur / mean - 1) > 0.02 and trend > 0.01


def signals_for_date(syms: dict, market_df: pd.DataFrame, asof,
                     threshold: float, atr_stop: float, atr_target: float) -> list[dict]:
    """Ranked conviction signals for `asof`'s close. Each carries the trade plan
    (stop/target from ATR). Entry is intended at the NEXT bar's open."""
    out = []
    for sym, g in syms.items():
        if len(g) < 90 or asof not in g.index:
            continue
        gf = compute_features(g.loc[:asof], market_df)
        row = gf.loc[asof]
        sc = conviction(row)
        if sc < threshold:
            continue
        close = float(row["close"])
        atr = float(row["atr14"]) if not pd.isna(row["atr14"]) else 0.0
        if atr <= 0:
            continue
        out.append({
            "symbol": sym,
            "conviction": round(sc, 4),
            "ref_close": round(close, 4),
            "atr": round(atr, 4),
            "stop_from_entry_atr": atr_stop,
            "target_from_entry_atr": atr_target,
            "mom20": round(float(row["mom20"]), 4),
            "rs20": round(float(row["rs20"]), 4),
            "atr_pct": round(float(row["atr_pct"]), 4),
            "dist_hi20": round(float(row["dist_hi20"]), 4),
        })
    out.sort(key=lambda x: -x["conviction"])
    return out


def gap_signals_for_date(syms: dict, market_df: pd.DataFrame, asof,
                         gap_min: float = 0.03, gap_max: float = 0.15,
                         rvol_min: float = 1.5, atr_stop: float = 1.5,
                         trail: float = 0.10) -> list[dict]:
    """Gap-momentum signals for `asof`: stock gapped up into [gap_min,gap_max]
    on volume. Entry intended NEXT bar open; exit = ATR stop + trailing stop.
    Validated OOS on IN: +1.25%/trade, PF 1.40, avg winner +10.9%. NOT
    regime-gated (gaps are catalyst-driven)."""
    out = []
    for sym, g in syms.items():
        if len(g) < 30 or asof not in g.index:
            continue
        gi = g.loc[:asof]
        if len(gi) < 25:
            continue
        gf = compute_features(gi, market_df)
        row = gf.iloc[-1]
        prev_close = float(gi["close"].iloc[-2])
        today_open = float(row["open"])
        if prev_close <= 0:
            continue
        gap = today_open / prev_close - 1
        rvol = float(row["rvol"]) if not pd.isna(row["rvol"]) else 0.0
        atr = float(row["atr14"]) if not pd.isna(row["atr14"]) else 0.0
        if gap_min <= gap <= gap_max and rvol >= rvol_min and atr > 0:
            out.append({
                "symbol": sym,
                "conviction": round(min(gap / gap_max, 1.0), 4),  # rank by gap strength
                "ref_close": round(float(row["close"]), 4),
                "atr": round(atr, 4),
                "stop_from_entry_atr": atr_stop,
                "target_from_entry_atr": 0.0,   # gap uses trailing, no fixed target
                "trail": trail,
                "gap_pct": round(gap * 100, 2),
                "rvol": round(rvol, 2),
            })
    out.sort(key=lambda x: -x["conviction"])
    return out


def latest_trading_date(syms: dict):
    dates = set()
    for g in syms.values():
        if len(g):
            dates.add(g.index.max())
    return max(dates) if dates else None


def complete_trading_dates(syms: dict, min_coverage: float = 0.5):
    """Trading dates where at least `min_coverage` of the universe has a bar.
    Filters out the bleeding-edge date whose daily bars are only partially
    ingested (the live feed loads symbols alphabetically through the day)."""
    from collections import Counter
    cnt = Counter()
    for g in syms.values():
        for d in g.index:
            cnt[d] += 1
    n = max(len(syms), 1)
    return sorted(d for d, c in cnt.items() if c >= min_coverage * n)
