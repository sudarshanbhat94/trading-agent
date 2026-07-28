"""Backtest the catalyst-continuation strategy.

The idea came from looking at one day's biggest Nifty-200 gainers and asking
what they had in common. Most were NOT reacting to news that morning — Kalyan's
update was three weeks old, Eternal's results six days old — they were
continuations of a catalyst that had already landed, in names still trending.

That is a testable claim, and unlike "buy the day's biggest mover" it does not
require predicting a surprise. You are tracking who already has fuel.

The rule:
  * a name gets a material NSE filing (results / order / corp_action);
  * `pre_close` is its close on the session BEFORE the filing;
  * on each later session within `window` sessions, if the name is still
    trading above `pre_close` by at least `min_hold_pct`, it is a candidate;
  * buy at the next session's OPEN — never the same close the signal used;
  * exit after `hold` sessions, or on a `stop` / `target`, whichever first.

Conservative choices, so the number is not flattered:
  * entry at the next open, so nothing is bought at a price the signal saw;
  * a filing timestamped after 15:30 is actionable only from the NEXT session;
  * if a bar's range covers both stop and target, the stop is taken;
  * delivery-style costs (~0.25% round trip), not intraday rates, because
    these are multi-day holds.
"""
from __future__ import annotations

import argparse
import json
import os
from collections import defaultdict
from datetime import datetime

CANDLES = os.environ.get("CONT_CANDLES", "/tmp/n200_daily.json")
CATALYSTS = os.environ.get("CONT_CATALYSTS", "/tmp/cat_all.json")


def load_candles(path=CANDLES):
    """symbol -> ordered list of (date, open, high, low, close, volume)."""
    bars = defaultdict(list)
    for symbol, date, o, h, l, c, v in json.load(open(path)):
        if None in (o, h, l, c):
            continue
        bars[symbol].append((date, float(o), float(h), float(l), float(c), float(v or 0)))
    for symbol in bars:
        bars[symbol].sort()
    return dict(bars)


def load_catalysts(path=CATALYSTS):
    """symbol -> sorted list of (actionable_date, category).

    A filing after 15:30 cannot be traded that session, so it becomes
    actionable the following calendar day. The backtest then maps that to the
    next available session.
    """
    out = defaultdict(list)
    for symbol, an_dt, category in json.load(open(path)):
        try:
            moment = datetime.strptime(str(an_dt).strip(), "%d-%b-%Y %H:%M:%S")
        except (TypeError, ValueError):
            continue
        date = moment.date()
        if moment.hour >= 15 or (moment.hour == 15 and moment.minute >= 30):
            date = date.fromordinal(date.toordinal() + 1)
        out[symbol].append((date.isoformat(), category))
    for symbol in out:
        out[symbol].sort()
    return dict(out)


def simulate(bars, catalysts, window=10, min_hold_pct=0.0, hold=5,
             stop=0.04, target=0.08, cost=0.25, max_per_day=3):
    """Run the strategy. Returns (trades, per-symbol detail)."""
    sessions = sorted({b[0] for symbol in bars for b in bars[symbol]})
    index = {symbol: {b[0]: i for i, b in enumerate(bars[symbol])} for symbol in bars}

    signals = defaultdict(list)          # session -> [(symbol, category, move_since)]
    for symbol, events in catalysts.items():
        series = bars.get(symbol)
        if not series:
            continue
        pos = index[symbol]
        for actionable, category in events:
            # First session on or after the filing becomes actionable.
            start = next((d for d in sessions if d >= actionable and d in pos), None)
            if start is None:
                continue
            i0 = pos[start]
            if i0 == 0:
                continue
            pre_close = series[i0 - 1][4]
            if pre_close <= 0:
                continue
            for i in range(i0, min(i0 + window, len(series) - 1)):
                date, _o, _h, _l, close, _v = series[i]
                move = close / pre_close - 1
                if move >= min_hold_pct:
                    signals[date].append((symbol, category, move))

    trades = []
    for date in sessions:
        todays = signals.get(date)
        if not todays:
            continue
        todays.sort(key=lambda x: -x[2])          # strongest continuation first
        for symbol, category, move in todays[:max_per_day]:
            series, pos = bars[symbol], index[symbol]
            i = pos[date]
            if i + 1 >= len(series):
                continue
            entry = series[i + 1][1]              # NEXT session's open
            if entry <= 0:
                continue
            stop_price = entry * (1 - stop)
            target_price = entry * (1 + target)
            outcome = None
            for j in range(i + 1, min(i + 1 + hold, len(series))):
                _d, _o, high, low, close, _v = series[j]
                if low <= stop_price:             # stop first — see module docstring
                    outcome = -stop * 100
                    break
                if high >= target_price:
                    outcome = target * 100
                    break
            if outcome is None:
                last = series[min(i + hold, len(series) - 1)][4]
                outcome = (last / entry - 1) * 100
            trades.append({"date": date, "symbol": symbol, "category": category,
                           "move_since_catalyst": round(move * 100, 2),
                           "return_pct": round(outcome - cost, 3)})
    return trades


def summarise(trades):
    if not trades:
        return {"trades": 0}
    returns = [t["return_pct"] for t in trades]
    wins = [r for r in returns if r > 0]
    equity = 1.0
    for r in returns:
        equity *= (1 + r / 100)
    return {
        "trades": len(returns),
        "win_rate": round(len(wins) / len(returns) * 100, 1),
        "avg_pct": round(sum(returns) / len(returns), 3),
        "best": round(max(returns), 2),
        "worst": round(min(returns), 2),
        "compounded_pct": round((equity - 1) * 100, 1),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--window", type=int, default=10)
    parser.add_argument("--min-hold", type=float, default=0.0)
    parser.add_argument("--hold", type=int, default=5)
    parser.add_argument("--stop", type=float, default=0.04)
    parser.add_argument("--target", type=float, default=0.08)
    parser.add_argument("--cost", type=float, default=0.25)
    parser.add_argument("--max-per-day", type=int, default=3)
    args = parser.parse_args()
    trades = simulate(load_candles(), load_catalysts(), args.window, args.min_hold,
                      args.hold, args.stop, args.target, args.cost, args.max_per_day)
    for key, value in summarise(trades).items():
        print(f"  {key:18s} {value}")


if __name__ == "__main__":
    main()
