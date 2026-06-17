"""Intraday backtest - opening-range breakout (ORB), squared off by close.

Tests the same-day opportunity the daily swing engine can't see: a stock that
breaks out of its opening range and runs intraday. Uses 30-minute bars (IN) /
1-minute bars (US) on the OpenStocks DB, read-only.

HONEST CAVEAT: only ~26 days of IN 30m and ~16 days of US 1m history exist, so
this is a PRELIMINARY read (small sample, single market regime), not the
14-month robustness the swing engine has.

  python3 backtest_intraday.py --market IN --or-min 30 --target-r 1.0
"""
from __future__ import annotations

import argparse
import sqlite3
import numpy as np
import pandas as pd

DB = "/opt/opentrade/var/trading_agent.db"
SRC = {"IN": "upstox-live:30minute", "US": "alpaca-iex-live:1minute"}
# intraday round-trip cost % (no delivery STT; brokerage+slippage). Conservative.
COST = {"IN": 0.12, "US": 0.05}
BAR_MIN = {"IN": 30, "US": 1}


def load(con, market, topn, yahoo=False):
    if yahoo:
        df = pd.read_sql_query(
            "SELECT symbol, ts, open, high, low, close, volume FROM bars WHERE market=?",
            con, params=(market,))
        if df.empty:
            return None
        df["dt"] = pd.to_datetime(df["ts"], unit="s", utc=True, errors="coerce")
    else:
        src = SRC[market]
        df = pd.read_sql_query(
            "SELECT symbol, ts, open, high, low, close, volume FROM candles WHERE source=?",
            con, params=(src,))
        if df.empty:
            return None
        df["dt"] = pd.to_datetime(df["ts"], utc=True, errors="coerce")
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["open", "high", "low", "close", "dt"]).sort_values(["symbol", "dt"])
    df["date"] = df["dt"].dt.date
    # liquidity: top-N symbols by median daily turnover
    daily_turn = (df.assign(t=df["close"] * df["volume"])
                    .groupby(["symbol", "date"])["t"].sum().groupby("symbol").median())
    keep = set(daily_turn.sort_values(ascending=False).head(topn).index)
    return df[df["symbol"].isin(keep)].copy()


def backtest(market, topn, or_min, target_r, stop_r, cost, side, mode, con, yahoo=False):
    df = load(con, market, topn, yahoo=yahoo)
    if df is None or df.empty:
        print(f"[{market}] no intraday data")
        return
    bar = 5 if yahoo else BAR_MIN[market]
    or_bars = max(1, or_min // bar)
    print(f"[{market}] {SRC[market]}  symbols={df['symbol'].nunique()} "
          f"days={df['date'].nunique()}  opening_range={or_min}m ({or_bars} bars) "
          f"mode={mode} target={target_r}R stop={stop_r}R cost={cost}% side={side}")
    trades = []
    for (sym, day), g in df.groupby(["symbol", "date"]):
        g = g.sort_values("dt")
        if len(g) < or_bars + 2:
            continue
        orb = g.iloc[:or_bars]
        rest = g.iloc[or_bars:]
        or_hi, or_lo = orb["high"].max(), orb["low"].min()
        rng = or_hi - or_lo
        if rng <= 0 or or_hi <= 0:
            continue
        bars = rest[["high", "low", "close"]].to_numpy()
        entry = stop = target = None
        outcome = None
        for k in range(len(bars)):
            h, l, c = bars[k]
            if entry is None:
                if mode == "breakout":
                    if side == "long" and h >= or_hi:
                        entry, stop, target = or_hi, or_hi - stop_r * rng, or_hi + target_r * rng
                    elif side == "short" and l <= or_lo:
                        entry, stop, target = or_lo, or_lo + stop_r * rng, or_lo - target_r * rng
                else:  # reversion: buy the dip to opening low, target back to opening high
                    if side == "long" and l <= or_lo:
                        entry, stop, target = or_lo, or_lo - stop_r * rng, or_lo + target_r * rng
                    elif side == "short" and h >= or_hi:
                        entry, stop, target = or_hi, or_hi + stop_r * rng, or_hi - target_r * rng
                continue
            if side == "long":
                if l <= stop:
                    outcome = (stop / entry - 1) * 100; break
                if h >= target:
                    outcome = (target / entry - 1) * 100; break
            else:
                if h >= stop:
                    outcome = (entry / stop - 1) * 100; break
                if l <= target:
                    outcome = (entry / target - 1) * 100; break
        if entry is None:
            continue
        if outcome is None:  # square off at close of last bar
            last = bars[-1][2]
            outcome = ((last / entry - 1) if side == "long" else (entry / last - 1)) * 100
        trades.append(outcome - cost)
    if not trades:
        print("  no trades")
        return
    t = np.array(trades)
    wins = (t > 0).sum()
    pf = t[t > 0].sum() / abs(t[t <= 0].sum() or 1)
    print(f"  TRADES n={len(t):,}  win={wins/len(t)*100:.1f}%  "
          f"expectancy(net)={t.mean():+.3f}%/trade  profit_factor={pf:.2f}  "
          f"avgW={t[t>0].mean():+.2f}% avgL={t[t<=0].mean():+.2f}%")
    print(f"  total net P&L (1 unit/trade): {t.sum():+.1f} units")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", choices=["IN", "US"], default="IN")
    ap.add_argument("--topn", type=int, default=400)
    ap.add_argument("--or-min", type=int, default=30, help="opening range minutes")
    ap.add_argument("--target-r", type=float, default=1.0)
    ap.add_argument("--stop-r", type=float, default=1.0)
    ap.add_argument("--side", choices=["long", "short"], default="long")
    ap.add_argument("--mode", choices=["breakout", "reversion"], default="breakout")
    ap.add_argument("--cost", type=float, default=None)
    ap.add_argument("--yahoo-db", default=None, help="use Yahoo intraday DB instead of main candles")
    args = ap.parse_args()
    cost = args.cost if args.cost is not None else COST[args.market]
    if args.yahoo_db:
        con = sqlite3.connect(f"file:{args.yahoo_db}?mode=ro", uri=True, timeout=120)
        backtest(args.market, args.topn, args.or_min, args.target_r, args.stop_r,
                 cost, args.side, args.mode, con, yahoo=True)
    else:
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=120)
        backtest(args.market, args.topn, args.or_min, args.target_r, args.stop_r,
                 cost, args.side, args.mode, con)


if __name__ == "__main__":
    main()
