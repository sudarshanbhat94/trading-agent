"""Backtest the "take 1% out of the day's strongest mover" strategy.

This is the strategy the operator actually wants, and until now it could not be
tested here at all — the system stores daily bars only, so an intraday entry has
no price to enter at. With 5-minute history it becomes measurable.

The rule, stated precisely so the result means something:

  * each session, wait until `entry_minute` past the open (the mover has to
    reveal itself before you can buy it);
  * rank every liquid symbol by its move from that day's open, optionally
    requiring a volume surge;
  * buy the top `positions` names at that bar's close;
  * exit at +`target`%, or -`stop`%, or the last bar of the day, whichever
    comes first;
  * charge `cost`% round trip.

Two conservative choices, both of which make the result WORSE and are therefore
the honest way round:

  * if a bar's high reaches the target and its low reaches the stop, the stop is
    taken — intrabar order is unknowable, and assuming the good fill would
    inflate every number here;
  * entry is at the close of the ranking bar, not its open, so the strategy
    cannot buy at a price it only knew about in hindsight.
"""
from __future__ import annotations

import argparse
import os
import pathlib
import sqlite3

import pandas as pd
from collections import defaultdict
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
DB = os.environ.get("INTRADAY_DB", "/tmp/intraday.db")


def load(con, source="yahoo:5m"):
    """{date: {symbol: [(minute_from_open, o, h, l, c, v), ...]}}, bars ordered."""
    days: dict = defaultdict(lambda: defaultdict(list))
    # Two schemas exist: the scratch `candles`(source,...) the recorder first
    # wrote, and the retained `bars` table in var/intraday_yahoo.db which has no
    # source column. Detect rather than assume — the scratch DB is periodically
    # cleared, so the durable one is usually the only one present.
    tables = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if "candles" in tables:
        cursor = con.execute(
            "SELECT symbol,ts,open,high,low,close,volume FROM candles WHERE source=? ORDER BY ts",
            (source,))
    else:
        cursor = con.execute(
            "SELECT symbol,ts,open,high,low,close,volume FROM bars ORDER BY ts")
    for symbol, ts, o, h, l, c, v in cursor:
        # `candles` stores ISO strings, `bars` stores epoch seconds.
        raw = str(ts)
        if raw.isdigit():
            moment = datetime.fromtimestamp(int(raw), timezone.utc).astimezone(IST)
        else:
            moment = datetime.fromisoformat(raw).astimezone(IST)
        minute = moment.hour * 60 + moment.minute - (9 * 60 + 15)   # minutes since 09:15
        if minute < 0 or minute > 375:
            continue
        days[moment.date().isoformat()][symbol].append((minute, o, h, l, c, v or 0.0))
    return days


def simulate_day(bars_by_symbol, entry_minute, positions, target, stop, min_move, rvol_min):
    """One session. Returns a list of realised percentage returns, pre-cost."""
    candidates = []
    for symbol, bars in bars_by_symbol.items():
        if len(bars) < 12:
            continue
        day_open = bars[0][1]
        if not day_open or day_open <= 0:
            continue
        entry_bars = [b for b in bars if b[0] <= entry_minute]
        later = [b for b in bars if b[0] > entry_minute]
        if not entry_bars or not later:
            continue
        entry_price = entry_bars[-1][4]
        move = (entry_price / day_open - 1) * 100
        if move < min_move:
            continue
        # Volume surge: this bar's volume against the day's average so far.
        volumes = [b[5] for b in entry_bars if b[5] > 0]
        rvol = (entry_bars[-1][5] / (sum(volumes) / len(volumes))) if volumes else 0.0
        if rvol < rvol_min:
            continue
        candidates.append((move, symbol, entry_price, later))

    candidates.sort(reverse=True)
    results = []
    for move, symbol, entry_price, later in candidates[:positions]:
        target_price = entry_price * (1 + target / 100)
        stop_price = entry_price * (1 - stop / 100)
        outcome = None
        for _, o, h, l, c, v in later:
            if l <= stop_price:            # stop checked first — see module docstring
                outcome = -stop
                break
            if h >= target_price:
                outcome = target
                break
        if outcome is None:
            outcome = (later[-1][4] / entry_price - 1) * 100
        results.append(outcome)
    return results


def point_in_time_filter(topn=300):
    """date -> frozenset of names screenable that day, from the DAILY candles.

    The intraday DB only ever recorded the ~150 names that were most liquid
    WHEN THE RECORDER WAS WRITTEN. Backtesting on it therefore asks "how did
    today's liquid names behave?", which is the same look-ahead that inflated
    this strategy from +1.6% to +18.7%. Screening each date against a
    point-in-time universe removes it.
    """
    import sys
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    from backtest_v2 import DB as DAILY_DB, load_market
    con = sqlite3.connect(f"file:{DAILY_DB}?mode=ro", uri=True, timeout=120)
    try:
        _syms, _mkt, eligible_at = load_market(con, "IN", topn, asof=True)
    finally:
        con.close()
    return eligible_at


def run(entry_minute=30, positions=1, target=1.0, stop=0.5, min_move=1.0,
        rvol_min=1.0, cost=0.10, source="yahoo:5m", asof=False, topn=300):
    con = sqlite3.connect(DB)
    days = load(con, source)
    con.close()
    eligible_at = point_in_time_filter(topn) if asof else None

    per_day, all_trades = [], []
    for date in sorted(days):
        bars = days[date]
        if eligible_at is not None:
            ok = eligible_at(pd.Timestamp(str(date)[:10]))
            bars = {sym: b for sym, b in bars.items()
                    if sym.upper().replace(".NS", "") in ok}
            if not bars:
                per_day.append(0.0)
                continue
        returns = simulate_day(bars, entry_minute, positions, target, stop,
                               min_move, rvol_min)
        net = [r - cost for r in returns]
        all_trades.extend(net)
        # Equal split across the day's positions, so a day is one unit of capital.
        per_day.append(sum(net) / len(net) if net else 0.0)

    equity = 1.0
    for day in per_day:
        equity *= (1 + day / 100)
    traded = [d for d in per_day if d != 0.0]
    wins = [t for t in all_trades if t > 0]
    return {
        "sessions": len(per_day),
        "sessions_traded": len(traded),
        "trades": len(all_trades),
        "win_rate": round(len(wins) / len(all_trades) * 100, 1) if all_trades else 0.0,
        "avg_trade_pct": round(sum(all_trades) / len(all_trades), 3) if all_trades else 0.0,
        "avg_day_pct": round(sum(per_day) / len(per_day), 3) if per_day else 0.0,
        "total_return_pct": round((equity - 1) * 100, 2),
        "best_day": round(max(per_day), 2) if per_day else 0.0,
        "worst_day": round(min(per_day), 2) if per_day else 0.0,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--entry-minute", type=int, default=30)
    parser.add_argument("--positions", type=int, default=1)
    parser.add_argument("--target", type=float, default=1.0)
    parser.add_argument("--stop", type=float, default=0.5)
    parser.add_argument("--min-move", type=float, default=1.0)
    parser.add_argument("--rvol-min", type=float, default=1.0)
    parser.add_argument("--cost", type=float, default=0.10)
    parser.add_argument("--universe", choices=["recorded", "pit"], default="recorded",
                        help="pit = point-in-time screen against daily candles; "
                             "recorded = whatever the intraday recorder happened to "
                             "capture, which carries the original look-ahead")
    args = parser.parse_args()
    result = run(args.entry_minute, args.positions, args.target, args.stop,
                 args.min_move, args.rvol_min, args.cost,
                 asof=args.universe == "pit")
    for key, value in result.items():
        print(f"  {key:18s} {value}")


if __name__ == "__main__":
    main()
