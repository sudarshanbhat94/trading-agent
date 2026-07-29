"""Does the CE/PE call actually predict, and does it pay for theta?

Two separate questions, and the second is the one that decides whether index
options are worth trading at all:

  1. DIRECTION — when the engine says CE, does the index rise more often than
     chance? Measured on the index itself, so it is a clean read of the signal.
  2. PAYS FOR DECAY — an option loses value every day it is held, so being
     right is not enough. A call that is right 55% of the time can still lose
     money once theta is charged.

Nothing here is a substitute for the first live trade, but it is the only
honest gate available before risking premium: unlike every intraday capability
built this week, index F&O has real history — 404 sessions.

Deliberately conservative:
  * the call is computed on data up to and INCLUDING day t, and the return is
    measured from day t+1's open, so no decision uses a price it could not have
    seen;
  * theta is charged as a flat daily percentage of premium, which is generous
    to the strategy near expiry, where real decay accelerates;
  * a "no trade" day is scored as zero, not skipped, because sitting out is a
    real outcome and averaging only over trading days flatters the engine.

Read-only.
  .venv/bin/python scripts/index_call_backtest.py --symbol NIFTY
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import index_direction  # noqa: E402

FO_DB = os.environ.get("FO_DB", "/opt/opentrade/var/fo.db")
# Rough daily time-decay on a near-the-money weekly option, as a share of the
# premium paid. Real theta accelerates into expiry; a flat rate understates the
# cost, which biases this test IN FAVOUR of trading.
THETA_PER_DAY = 0.06
# An at-the-money option's value moves roughly half as fast as the index in
# points, but on a much smaller base — a 1% index move is worth far more than 1%
# of the premium. This is the leverage that makes the trade worth taking at all.
DELTA = 0.5


def load(symbol, db=FO_DB):
    """Near-month futures OHLCV per session, plus put/call OI for that day."""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=60)
    bars = con.execute(
        "SELECT date, open, high, low, close, volume FROM ("
        "  SELECT date, open, high, low, close, volume,"
        "         ROW_NUMBER() OVER (PARTITION BY date ORDER BY expiry) rn"
        "    FROM fo_bhav WHERE symbol=? AND instrument='FUT' AND close>0"
        ") WHERE rn=1 ORDER BY date", (symbol,)).fetchall()
    oi = {d: (p or 0, c or 0) for d, p, c in con.execute(
        "SELECT date,"
        " SUM(CASE WHEN opt_type='PE' THEN oi ELSE 0 END),"
        " SUM(CASE WHEN opt_type='CE' THEN oi ELSE 0 END)"
        " FROM fo_bhav WHERE symbol=? AND instrument='OPT' GROUP BY date", (symbol,))}
    con.close()
    return bars, oi


def run(symbol, hold, min_conf, db=FO_DB):
    bars, oi = load(symbol, db)
    if len(bars) < 80:
        print(f"[{symbol}] only {len(bars)} sessions — not enough")
        return None
    dates = [b[0] for b in bars]
    o = [float(b[1] or 0) for b in bars]
    h = [float(b[2] or 0) for b in bars]
    lo = [float(b[3] or 0) for b in bars]
    c = [float(b[4] or 0) for b in bars]
    v = [float(b[5] or 0) for b in bars]

    calls, gross, net, per_day = [], [], [], []
    for i in range(55, len(bars) - hold - 1):
        put_oi, call_oi = oi.get(dates[i], (0, 0))
        verdict = index_direction.decide(o[:i + 1], h[:i + 1], lo[:i + 1],
                                         c[:i + 1], v[:i + 1],
                                         put_oi=put_oi, call_oi=call_oi)
        side = verdict["call"]
        if not side or verdict["confidence"] < min_conf:
            per_day.append(0.0)                 # sitting out is a real outcome
            continue
        entry = o[i + 1]                        # next session's open — never today's close
        exit_px = c[min(i + hold, len(c) - 1)]
        if entry <= 0:
            continue
        move = (exit_px / entry - 1) * 100
        underlying = move if side == "CE" else -move
        # premium %: leverage on the index move, less decay for days held
        premium = underlying / DELTA - THETA_PER_DAY * 100 * hold
        calls.append((dates[i], side, underlying, premium))
        gross.append(underlying)
        net.append(premium)
        per_day.append(premium)

    if not calls:
        print(f"[{symbol}] the engine never issued a call at confidence >= {min_conf}")
        return dict(n=0)

    right = sum(1 for _d, _s, u, _p in calls if u > 0)
    wins = [x for x in net if x > 0]
    print(f"[{symbol}] {len(bars)} sessions, hold {hold}d, confidence >= {min_conf}")
    print(f"  calls issued        {len(calls)}  ({len(calls) / len(bars) * 100:.0f}% of sessions)")
    print(f"  direction correct   {right / len(calls) * 100:.1f}%  "
          f"(coin flip = 50%)")
    print(f"  index move per call {statistics.mean(gross):+.3f}%")
    print(f"  PREMIUM per call    {statistics.mean(net):+.2f}%   "
          f"win {len(wins) / len(net) * 100:.1f}%")
    print(f"  best / worst        {max(net):+.1f}% / {min(net):+.1f}%")
    print(f"  theta charged       {THETA_PER_DAY * 100 * hold:.1f}% of premium over {hold}d")
    return dict(n=len(calls), accuracy=right / len(calls) * 100,
                gross=statistics.mean(gross), net=statistics.mean(net))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="NIFTY")
    ap.add_argument("--hold", type=int, default=3)
    ap.add_argument("--min-confidence", type=float, default=0.6)
    ap.add_argument("--all", action="store_true")
    args = ap.parse_args()
    symbols = ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"] if args.all else [args.symbol]
    results = {}
    for symbol in symbols:
        results[symbol] = run(symbol, args.hold, args.min_confidence)
        print()
    good = [s for s, r in results.items() if r and r.get("n", 0) >= 20 and r.get("net", 0) > 0]
    if good:
        print(f"==> positive after theta on: {', '.join(good)}")
        print("    Small samples. Treat as 'worth a live trial', not as an edge.")
    else:
        print("==> NOTHING is positive after theta. Do not enable auto-trade.")


if __name__ == "__main__":
    main()
