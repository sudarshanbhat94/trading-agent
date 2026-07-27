#!/usr/bin/env python3
"""Overnight-catalyst backtest: does buying a results-day name at the NEXT
session's OPEN (before the crowd finishes reacting) beat chasing it after it
has already run — and does reading the RESULTS QUALITY from the headline add
edge over taking every results event blindly?

Substrate (all real, read-only on the OCI box):
  * sentiment_events (trading_agent.db) — 2.5 months of per-symbol news with the
    actual results content in the headlines ("Q1 profit up 16%", "...shares fall").
  * candles source 'upstox-live:day' — daily OHLCV for the price outcomes.

Method (no look-ahead):
  1. Detect a "results event" per (symbol, date) from the headlines.
  2. Score its QUALITY from the text (beat/up vs miss/down) + the pipeline's own
     sentiment score.
  3. Enter at the OPEN of the first session strictly AFTER the news date; exit at
     that session's CLOSE (1-day) and +3 sessions. Charge round-trip cost.
  4. Bucket by quality and compare win%, avg net, profit-factor, tails.
  5. "Chase" reference: enter at that session's HIGH instead of the open — the
     worst-case of buying the pop late — to quantify what earliness is worth.
  6. Acceptance replay: print the most recent named events with quality + outcome.

Run on OCI:
  /opt/opentrade/.venv/bin/python scripts/overnight_catalyst_bt.py \
      --db /opt/opentrade/var/trading_agent.db
"""
from __future__ import annotations
import argparse, sqlite3, json, re, statistics, math
from collections import defaultdict

COST = 0.0020        # round-trip cost (brokerage+slippage), matched to live COST_SIDE*2
MIN_TURNOVER = 2.5e8 # Rs.25cr — same liquidity floor as the live volume_surge lane

# --- headline parsing -------------------------------------------------------
RESULTS_KW = re.compile(
    r"(q[1-4]\b|quarter|net profit|standalone|consolidated|\bpat\b|\bebitda\b|"
    r"\bresults?\b|earnings|board meeting|profit (?:rose|rises|jumps|surges|up|"
    r"fell|falls|declin|drop|down|slump))", re.I)
POS_KW = re.compile(
    r"(beat|record|multibag|surge|surges|jump|jumps|soar|rally|"
    r"rises|rose|grew|grow|strong|robust|up \d|higher|"
    r"profit[^.]{0,40}(?:up|rose|rises|jump|surge|grew|doubl|higher)|"
    r"(?:pat|revenue|profit|ebitda)[^.]{0,25}\bup\b|"
    r"\d{1,3}(?:\.\d+)?%[^.]{0,15}(?:rise|jump|surge|up|higher|growth|gain))", re.I)
NEG_KW = re.compile(
    r"(miss|disappoint|falls|fell|declin|slump|drop|down \d|\bloss\b|weak|lower|"
    r"profit[^.]{0,40}(?:down|fell|falls|declin|drop|slump|lower)|"
    r"(?:pat|revenue|profit|ebitda)[^.]{0,25}\bdown\b|"
    r"\d{1,3}(?:\.\d+)?%[^.]{0,15}(?:fall|drop|down|declin|lower|slump))", re.I)


def load_daily(db):
    con = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
    rows = con.execute(
        "SELECT symbol, substr(ts,1,10) d, open, high, low, close, volume "
        "FROM candles WHERE source='upstox-live:day' ORDER BY symbol, d").fetchall()
    con.close()
    daily = defaultdict(list)
    for sym, d, o, h, l, c, v in rows:
        if o and c:
            daily[sym].append((d, float(o), float(h), float(l), float(c), float(v or 0)))
    return daily


def avg_turnover(bars, upto_i, win=20):
    seg = bars[max(0, upto_i - win):upto_i]
    if not seg:
        return 0.0
    return statistics.mean(b[5] * b[4] for b in seg)


def load_events(db):
    """One results-event per (symbol, date) with a quality score in ~[-3,+3]."""
    con = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
    rows = con.execute(
        "SELECT symbol, substr(ts,1,10) d, score, headlines_json "
        "FROM sentiment_events WHERE headlines_json IS NOT NULL "
        "ORDER BY symbol, ts").fetchall()
    con.close()
    ev = {}   # (sym,date) -> dict
    for sym, d, score, hj in rows:
        try:
            heads = json.loads(hj) if hj else []
        except Exception:
            heads = []
        if not heads:
            continue
        text = " || ".join(h for h in heads if isinstance(h, str))
        if not RESULTS_KW.search(text):
            continue                       # not a results-flavoured event
        pos = len(POS_KW.findall(text))
        neg = len(NEG_KW.findall(text))
        qual = (pos - neg) + (score or 0.0) * 2.0   # blend text tally + pipeline score
        key = (sym, d)
        prev = ev.get(key)
        if prev is None or abs(qual) > abs(prev["qual"]):
            ev[key] = dict(sym=sym, date=d, score=score or 0.0, pos=pos, neg=neg,
                           qual=qual, head=heads[0][:90])
    return ev


def next_session(bars, after_date):
    for i, b in enumerate(bars):
        if b[0] > after_date:
            return i
    return None


def run(db):
    daily = load_daily(db)
    events = load_events(db)
    idx = {sym: {b[0]: i for i, b in enumerate(bars)} for sym, bars in daily.items()}

    trades = []   # each: dict with quality + returns
    for (sym, date), e in events.items():
        bars = daily.get(sym)
        if not bars:
            continue
        i = next_session(bars, date)
        if i is None or i + 1 >= len(bars):
            continue
        if avg_turnover(bars, i) < MIN_TURNOVER:
            continue
        o = bars[i][1]; hi = bars[i][2]; c1 = bars[i][4]
        prev_c = bars[i - 1][4]                       # results-day close (already moved?)
        gap = o / prev_c - 1                          # how much it already gapped at entry
        ret_open_1d = (c1 / o - 1) - COST
        # +3 sessions hold
        j = min(i + 3, len(bars) - 1)
        ret_open_3d = (bars[j][4] / o - 1) - COST
        # chase reference: bought the intraday pop (day high) instead of the open
        ret_chase_1d = (c1 / hi - 1) - COST
        trades.append(dict(sym=sym, date=date, qual=e["qual"], pos=e["pos"],
                           neg=e["neg"], score=e["score"], head=e["head"], gap=gap,
                           r1=ret_open_1d, r3=ret_open_3d, rchase=ret_chase_1d))
    return trades


def stats(rows, key):
    rs = [r[key] for r in rows]
    if not rs:
        return None
    wins = [x for x in rs if x > 0]; loss = [x for x in rs if x <= 0]
    gp = sum(wins); gl = abs(sum(loss))
    pf = gp / gl if gl > 0 else float("inf")
    return dict(n=len(rs), win=100 * len(wins) / len(rs), avg=100 * statistics.mean(rs),
                med=100 * statistics.median(rs), pf=pf,
                p95=100 * sorted(rs)[int(0.95 * (len(rs) - 1))],
                p05=100 * sorted(rs)[int(0.05 * (len(rs) - 1))])


def line(name, s):
    if not s:
        print(f"  {name:<26} (no trades)"); return
    pf = "inf" if s["pf"] == float("inf") else f"{s['pf']:.2f}"
    print(f"  {name:<26} n={s['n']:<4} win={s['win']:5.1f}%  avg={s['avg']:+5.2f}%  "
          f"med={s['med']:+5.2f}%  PF={pf:<5}  p95={s['p95']:+5.1f}  p05={s['p05']:+5.1f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="/opt/opentrade/var/trading_agent.db")
    a = ap.parse_args()
    t = run(a.db)
    if not t:
        print("no trades built — check data"); return
    dates = sorted(r["date"] for r in t)
    print(f"\n=== OVERNIGHT-CATALYST BACKTEST  ({dates[0]} .. {dates[-1]}) ===")
    print(f"results events with a tradeable next-session + liquidity>=25cr: {len(t)}\n")

    print("ENTER AT NEXT-SESSION OPEN, hold to that day's CLOSE (1-day):")
    line("ALL results events", stats(t, "r1"))
    line("QUALITY > 0 (beat-ish)", stats([r for r in t if r["qual"] > 0], "r1"))
    line("QUALITY >= 1.0 (clear beat)", stats([r for r in t if r["qual"] >= 1.0], "r1"))
    line("QUALITY <= 0 (miss/ambig)", stats([r for r in t if r["qual"] <= 0], "r1"))

    print("\nSAME ENTRIES, hold +3 sessions:")
    line("ALL results events", stats(t, "r3"))
    line("QUALITY >= 1.0 (clear beat)", stats([r for r in t if r["qual"] >= 1.0], "r3"))

    print("\nCHASE REFERENCE (bought the day HIGH instead of the open), 1-day:")
    line("ALL results events", stats(t, "rchase"))
    line("QUALITY >= 1.0 (clear beat)", stats([r for r in t if r["qual"] >= 1.0], "rchase"))

    # correlation: does quality track next-day return?
    qs = [r["qual"] for r in t]; rr = [r["r1"] for r in t]
    if len(qs) > 5 and statistics.pstdev(qs) > 0 and statistics.pstdev(rr) > 0:
        mq, mr = statistics.mean(qs), statistics.mean(rr)
        cov = sum((q - mq) * (x - mr) for q, x in zip(qs, rr)) / len(qs)
        corr = cov / (statistics.pstdev(qs) * statistics.pstdev(rr))
        print(f"\ncorr(quality, next-day open->close return) = {corr:+.3f}  "
              f"(>0 means the results read has signal)")

    print("\nCONTINUATION TEST — bucket by how much it ALREADY gapped at entry "
          "(open vs results-day close), then open->close:")
    line("faded/flat   gap<=0%", stats([r for r in t if r["gap"] <= 0], "r1"))
    line("mild up      0-3%", stats([r for r in t if 0 < r["gap"] <= 0.03], "r1"))
    line("strong up    3-7%", stats([r for r in t if 0.03 < r["gap"] <= 0.07], "r1"))
    line("booming     >7%", stats([r for r in t if r["gap"] > 0.07], "r1"))
    print("  (if 'strong/booming' stays positive -> ride the momentum; if negative -> it fades/reverts)")

    print("\n=== ACCEPTANCE REPLAY — 12 most recent detected events ===")
    for r in sorted(t, key=lambda x: x["date"], reverse=True)[:12]:
        print(f"  {r['date']}  {r['sym']:<12} qual={r['qual']:+5.2f} "
              f"(pos{r['pos']}/neg{r['neg']} score{r['score']:+.2f})  "
              f"open->close={r['r1']*100:+5.2f}%  | {r['head']}")


if __name__ == "__main__":
    main()
