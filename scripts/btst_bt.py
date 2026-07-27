#!/usr/bin/env python3
"""BTST (Buy Today, Sell Tomorrow) backtest on real daily candles.

The idea: buy the CLOSE of a stock that had a strong momentum day (closed near
its high, up on volume, optionally a fresh NSE catalyst) and sell it the NEXT
session — capturing the overnight continuation. This is an OVERNIGHT hold, so it
carries gap risk in BOTH directions; the whole point of this test is to find out
whether the overnight edge on strong-close names is positive AFTER the down-gaps.

Substrate: candles 'upstox-live:day' (daily OHLCV) + sentiment_events for the
optional catalyst filter. Read-only on the OCI box.

Exits tested (you can only realistically sell at the open or hold the next day):
  * overnight   = next_open / close - 1      (pure BTST, sell at tomorrow's open)
  * next_close  = next_close / close - 1      (hold the whole next session)
  * gap_then_be = sell at open if gap>0 else exit at next close (simple rule)

Run:  /opt/opentrade/.venv/bin/python scripts/btst_bt.py --db /opt/opentrade/var/trading_agent.db
"""
from __future__ import annotations
import argparse, sqlite3, json, re, statistics
from collections import defaultdict

COST = 0.0025          # delivery round-trip (STT+brokerage+slippage), BTST is delivery
MIN_TURNOVER = 2.5e8   # Rs.25cr liquidity floor (same as the live lane)
RESULTS_KW = re.compile(r"(q[1-4]\b|quarter|net profit|\bpat\b|\bresults?\b|earnings|"
                        r"board meeting|order win|bags order|new order)", re.I)


def load_daily(db):
    con = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
    rows = con.execute(
        "SELECT symbol, substr(ts,1,10) d, open, high, low, close, volume "
        "FROM candles WHERE source='upstox-live:day' ORDER BY symbol, d").fetchall()
    con.close()
    daily = defaultdict(list)
    for sym, d, o, h, l, c, v in rows:
        if o and c and h and l:
            daily[sym].append((d, float(o), float(h), float(l), float(c), float(v or 0)))
    return daily


def catalyst_dates(db):
    """{(symbol,date)} that had a results/order-flavoured headline that day."""
    con = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
    rows = con.execute("SELECT symbol, substr(ts,1,10) d, headlines_json "
                       "FROM sentiment_events WHERE headlines_json IS NOT NULL").fetchall()
    con.close()
    out = set()
    for sym, d, hj in rows:
        try:
            heads = json.loads(hj) if hj else []
        except Exception:
            heads = []
        if any(isinstance(h, str) and RESULTS_KW.search(h) for h in heads):
            out.add((sym, d))
    return out


def stats(rs):
    if not rs:
        return None
    wins = [x for x in rs if x > 0]; loss = [x for x in rs if x <= 0]
    gp, gl = sum(wins), abs(sum(loss))
    pf = gp / gl if gl > 0 else float("inf")
    s = sorted(rs)
    return dict(n=len(rs), win=100 * len(wins) / len(rs), avg=100 * statistics.mean(rs),
                med=100 * statistics.median(rs), pf=pf,
                p95=100 * s[int(0.95 * (len(s) - 1))], p05=100 * s[int(0.05 * (len(s) - 1))])


def line(name, s):
    if not s:
        print(f"  {name:<30} (no trades)"); return
    pf = "inf" if s["pf"] == float("inf") else f"{s['pf']:.2f}"
    print(f"  {name:<30} n={s['n']:<5} win={s['win']:5.1f}%  avg={s['avg']:+5.2f}%  "
          f"med={s['med']:+5.2f}%  PF={pf:<5} p95={s['p95']:+5.1f} p05={s['p05']:+5.1f}")


def run(db, move_min, use_catalyst):
    daily = load_daily(db)
    cats = catalyst_dates(db) if use_catalyst else None
    overnight, nxt_close, gap_be = [], [], []
    n_cand = 0
    for sym, bars in daily.items():
        vols = [b[5] for b in bars]
        for i in range(21, len(bars) - 1):
            d, o, h, l, c, v = bars[i]
            av20 = statistics.mean(vols[i - 20:i]) or 0
            if av20 * c < MIN_TURNOVER:
                continue
            move = c / bars[i - 1][4] - 1                 # today's move vs prev close
            rng = h - l
            close_pos = (c - l) / rng if rng > 0 else 0   # 1.0 = closed at the high
            rvol = v / av20 if av20 else 0
            # BTST setup: strong up day, closed near the high, on above-avg volume
            if not (move >= move_min and close_pos >= 0.7 and rvol >= 1.5):
                continue
            if use_catalyst and (sym, d) not in cats:
                continue
            n_cand += 1
            no, nh, nl, nc = bars[i + 1][1], bars[i + 1][2], bars[i + 1][3], bars[i + 1][4]
            r_on = no / c - 1 - COST
            r_nc = nc / c - 1 - COST
            overnight.append(r_on)
            nxt_close.append(r_nc)
            gap_be.append(r_on if (no / c - 1) > 0 else r_nc)   # bank the gap, else ride to close
    return overnight, nxt_close, gap_be, n_cand


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="/opt/opentrade/var/trading_agent.db")
    a = ap.parse_args()
    print("\n=== BTST BACKTEST (buy strong-close momentum day, sell next session) ===")
    print("setup: day up >= X%, closed in top 30% of range, volume >= 1.5x avg, turnover>=25cr\n")
    for mv in (0.03, 0.05):
        for cat in (False, True):
            on, ncl, gbe, n = run(a.db, mv, cat)
            tag = f"move>={int(mv*100)}%" + (" + catalyst" if cat else "")
            print(f"-- {tag}  ({n} setups) --")
            line("  sell next OPEN (pure BTST)", stats(on))
            line("  hold to next CLOSE", stats(ncl))
            line("  bank gap else ride to close", stats(gbe))
            print()


if __name__ == "__main__":
    main()
