"""Offline backtester to A/B the decision-core redesign vs current behaviour.

Read-only. Replays real stored signals against stored candle history so we only
change live logic on proven improvement.

Stages:
  1. VALIDATE  - replay candles with a pure "hold" policy and check that the
                 engine reproduces each signal's recorded peak/worst return.
                 If MFE/MAE track the recorded peak/worst, the engine is trusted.
  2. EXIT A/B  - same entries, compare exit policies:
                   current  = fixed stop + target ladder, hold to stop/last target
                   harvest  = breakeven after +1R, ATR-trail after +1.5R,
                              partial at T1, hard time stop
  3. ENTRY A/B - apply an anti-chase entry filter and compare expectancy of the
                 admitted vs rejected sets under the same exit policy.

Usage (on the OCI box):
  /opt/opentrade/.venv/bin/python3 backtest_redesign.py --stage validate --limit 400
  /opt/opentrade/.venv/bin/python3 backtest_redesign.py --stage exit --limit 4000
  /opt/opentrade/.venv/bin/python3 backtest_redesign.py --stage entry --limit 4000
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import statistics as st
from datetime import datetime, timedelta

DB = "/opt/opentrade/var/trading_agent.db"

# Daily candle sources per market, tried in order for best coverage.
DAILY_SOURCES = {
    "IN": ["upstox-live:day", "yahoo-delayed", "indstocks-live:1day"],
    "US": ["alpaca-iex-live:day", "yahoo-delayed"],
}
INTRADAY_SOURCE = {"IN": "upstox-live:30minute", "US": "alpaca-iex-live:1minute"}


def fnum(x):
    try:
        v = float(x)
        return v if v == v else None
    except (TypeError, ValueError):
        return None


def parse_ts(s: str):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None


def deep_find(o, key, depth=0):
    if depth > 5 or not isinstance(o, dict):
        return None
    if key in o:
        return o[key]
    for v in o.values():
        r = deep_find(v, key, depth + 1)
        if r is not None:
            return r
    return None


def load_signals(con, limit, since="2026-05-27", until="2026-06-09"):
    c = con.cursor()
    rows = c.execute(
        """SELECT id, symbol, first_seen_at, last_seen_at, entry_price, peak_return_pct,
                  worst_return_pct, current_return_pct, status, signal_type,
                  overall_score_pct, confluence, details_json
           FROM signal_ideas
           WHERE first_seen_at BETWEEN ? AND ? AND entry_price IS NOT NULL
                 AND details_json IS NOT NULL
           ORDER BY id ASC LIMIT ?""",
        (since, until, limit),
    ).fetchall()
    out = []
    for (sid, sym, fseen, lseen, entry, peak, worst, cur, status, stype,
         score, conf, dj) in rows:
        try:
            d = json.loads(dj)
        except (TypeError, json.JSONDecodeError):
            continue
        entry = fnum(entry)
        stop = fnum(d.get("stop_loss")) or fnum(deep_find(d, "stop")) or fnum(deep_find(d, "hard_stop"))
        targets = []
        tg = d.get("targets") or deep_find(d, "targets") or []
        if isinstance(tg, list):
            for t in tg:
                p = fnum(t.get("price")) if isinstance(t, dict) else fnum(t)
                if p:
                    targets.append(p)
        market = str(d.get("market_region") or "IN").upper()
        if market not in ("IN", "US"):
            market = "IN"
        if not entry or not stop or entry <= stop:
            continue  # need a valid long risk leg
        out.append({
            "id": sid, "symbol": sym, "entry_ts": fseen, "last_ts": lseen, "entry": entry,
            "stop": stop, "targets": targets, "market": market,
            "atr_pct": fnum(d.get("atr_pct")) or fnum(deep_find(d, "atr_pct")),
            "day_gain_pct": fnum(d.get("day_gain_pct")) or fnum(deep_find(d, "day_gain_pct")),
            "high_dist_pct": fnum(d.get("day_high_distance_pct")) or fnum(deep_find(d, "day_high_distance_pct")),
            "range_pos": fnum(d.get("day_range_position")) or fnum(deep_find(d, "day_range_position")),
            "score": fnum(score), "confluence": fnum(conf), "status": status,
            "rec_peak": fnum(peak), "rec_worst": fnum(worst), "rec_cur": fnum(cur),
        })
    return out


def fetch_candles(con, symbol, market, after_ts, horizon_bars):
    """Return daily bars (ts,o,h,l,c) for trading days strictly after the entry
    date. Timestamps are compared as parsed UTC datetimes (the string compare was
    wrong: IN candles carry +05:30, signals carry +00:00). The hold starts on the
    next daily bar so day-0 intraday action cannot leak in (no look-ahead)."""
    entry_dt = parse_ts(after_ts)
    if entry_dt is None:
        return [], None
    floor_date = (entry_dt.date()).isoformat()  # date prefix compare is tz-safe
    c = con.cursor()
    for src in DAILY_SOURCES.get(market, DAILY_SOURCES["IN"]):
        rows = c.execute(
            """SELECT ts, open, high, low, close FROM candles
               WHERE symbol=? AND source=? AND ts >= ?
               ORDER BY ts ASC""",
            (symbol, src, floor_date),
        ).fetchall()
        bars = []
        for ts, o, h, l, cl in rows:
            cdt = parse_ts(ts)
            if cdt is None or cdt.date() <= entry_dt.date():
                continue  # only days strictly after entry day
            o, h, l, cl = fnum(o), fnum(h), fnum(l), fnum(cl)
            if None in (h, l, cl):
                continue
            bars.append((ts, o, h, l, cl))
            if len(bars) >= horizon_bars:
                break
        if len(bars) >= 2:
            return bars, src
    return [], None


def fetch_intraday(con, symbol, market, entry_ts, days=2, cap=1200):
    """Intraday bars strictly after the entry moment, day-0 inclusive, up to
    `days` trading days. Returns (bars, src) where each bar carries its date."""
    entry_dt = parse_ts(entry_ts)
    if entry_dt is None:
        return [], None
    src = INTRADAY_SOURCE[market]
    rows = con.execute(
        """SELECT ts, open, high, low, close FROM candles
           WHERE symbol=? AND source=? AND ts >= ?
           ORDER BY ts ASC LIMIT ?""",
        (symbol, src, entry_dt.date().isoformat(), cap),
    ).fetchall()
    bars, dates = [], []
    for ts, o, h, l, cl in rows:
        cdt = parse_ts(ts)
        if cdt is None or cdt <= entry_dt:
            continue  # strictly after entry moment (same day, later bars OK)
        o, h, l, cl = fnum(o), fnum(h), fnum(l), fnum(cl)
        if None in (h, l, cl):
            continue
        d = cdt.date()
        if not dates:
            dates.append(d)
        elif d != dates[-1]:
            if len(dates) >= days:
                break
            dates.append(d)
        bars.append((ts, o, h, l, cl, d))
    return (bars, src) if len(bars) >= 2 else ([], None)


def simulate_intraday(sig, bars, policy):
    """Intraday exit policies. bars carry a trailing date field.
      eod_exit         : exit at the close of the entry day (day-0 discipline)
      hold             : hold to end of window (overnight + next day)
      intraday_harvest : stop at stop_loss; take half at T1; trail remainder by
                         0.5*ATR after +1R; force-exit remainder at day-0 close."""
    entry, stop0 = sig["entry"], sig["stop"]
    risk = entry - stop0
    targets = sig["targets"] or [entry + 2 * risk]
    atr_abs = (sig["atr_pct"] / 100.0 * entry) if sig["atr_pct"] else risk
    day0 = bars[0][5]
    stop = stop0
    peak_price = entry
    booked, frac = 0.0, 1.0
    partial = False
    mfe = mae = 0.0
    realized, reason = None, "window_end"
    for ts, o, h, l, cl, d in bars:
        mfe = max(mfe, (h - entry) / risk)
        mae = min(mae, (l - entry) / risk)
        peak_price = max(peak_price, h)
        if policy == "intraday_harvest" and mfe >= 1.0:
            stop = max(stop, peak_price - 0.5 * atr_abs, entry)
        # conservative: stop before target within the bar
        if policy in ("intraday_harvest",) and l <= stop:
            realized = booked + frac * (min(stop, o) - entry) / entry * 100
            reason = "stop" if stop <= stop0 + 1e-9 else "trail"
            break
        if policy == "r_target":
            # fixed profit target at +1R, protective stop, remainder to EOD
            if l <= stop0:
                realized = (min(stop0, o) - entry) / entry * 100
                reason = "stop"
                break
            if h >= entry + 1.0 * risk:
                realized = ((entry + 1.0 * risk) - entry) / entry * 100
                reason = "target_1R"
                break
        if policy == "intraday_harvest" and not partial and h >= targets[0]:
            booked += 0.5 * (targets[0] - entry) / entry * 100
            frac = 0.5
            partial = True
            stop = max(stop, entry)
        if policy in ("eod_exit", "intraday_harvest", "r_target") and d == day0:
            last_day0_close = cl  # remember; exit when day changes
        if policy in ("eod_exit", "intraday_harvest", "r_target") and d != day0:
            # first bar of day-1: close out at the previous day-0 close
            realized = booked + frac * (last_day0_close - entry) / entry * 100
            reason = "eod"
            break
    if realized is None:
        last = bars[-1][4]
        realized = booked + frac * (last - entry) / entry * 100
        reason = reason if reason != "window_end" else "window_end"
    return {"realized": realized, "reason": reason, "mfe_pct": mfe * risk / entry * 100,
            "mae_pct": mae * risk / entry * 100, "bars": len(bars)}


def simulate(sig, bars, policy):
    """Walk bars, apply exit policy. Returns dict with realized return + diagnostics.
    Conservative intrabar rule: if a bar's low breaches the stop, count the stop
    first (worst-case), before checking the target."""
    entry, stop0 = sig["entry"], sig["stop"]
    risk = entry - stop0
    targets = sig["targets"] or [entry + 2 * risk]
    atr_abs = (sig["atr_pct"] / 100.0 * entry) if sig["atr_pct"] else risk
    stop = stop0
    mfe = 0.0  # max favourable, in R
    mae = 0.0  # max adverse, in R
    peak_price = entry
    realized = None
    reason = "time_exit"
    partial_done = False
    booked = 0.0   # return contribution from partial at T1
    frac = 1.0     # remaining position fraction

    for i, (ts, o, h, l, cl) in enumerate(bars):
        r_hi = (h - entry) / risk
        r_lo = (l - entry) / risk
        mfe = max(mfe, r_hi)
        mae = min(mae, r_lo)
        peak_price = max(peak_price, h)

        if policy == "hold_track":
            continue  # pure replay: never exit, just record MFE/MAE for validation

        if policy == "harvest":
            # dynamic stop management based on MFE seen so far
            if mfe >= 1.5:
                trail = peak_price - 1.0 * atr_abs       # ATR trail
                lock = entry + 0.5 * (peak_price - entry)  # lock half the move
                stop = max(stop, trail, lock, entry)
            elif mfe >= 1.0:
                stop = max(stop, entry)                  # breakeven

        # conservative: stop checked before target within the bar
        if l <= stop:
            exit_px = min(stop, o)  # gap-through fills at open
            realized = booked + frac * (exit_px - entry) / entry * 100
            reason = "stop" if stop > stop0 - 1e-9 and stop >= stop0 else "stop"
            if stop >= entry - 1e-9 and stop > stop0:
                reason = "trail_or_breakeven"
            break

        # target handling
        if policy == "harvest" and not partial_done and h >= targets[0]:
            booked += 0.5 * (targets[0] - entry) / entry * 100  # take half at T1
            frac = 0.5
            partial_done = True
            stop = max(stop, entry)  # de-risk remainder
        final_target = targets[-1]
        if h >= final_target:
            exit_px = max(final_target, o if o > final_target else final_target)
            realized = booked + frac * (exit_px - entry) / entry * 100
            reason = "target"
            break

    if realized is None:
        last_close = bars[-1][4]
        realized = booked + frac * (last_close - entry) / entry * 100
        reason = "time_exit"
    return {
        "realized": realized, "reason": reason,
        "mfe_pct": mfe * (risk / entry) * 100, "mae_pct": mae * (risk / entry) * 100,
        "mfe_R": mfe, "mae_R": mae, "bars": len(bars),
    }


def summarize(results, label):
    rr = [r["realized"] for r in results]
    if not rr:
        print(f"  {label}: no data")
        return None
    wins = [x for x in rr if x > 0]
    losses = [x for x in rr if x <= 0]
    wr = len(wins) / len(rr) * 100
    aw = st.mean(wins) if wins else 0.0
    al = st.mean(losses) if losses else 0.0
    exp = st.mean(rr)
    reasons = {}
    for r in results:
        reasons[r["reason"]] = reasons.get(r["reason"], 0) + 1
    print(f"  {label:10s} n={len(rr):>5} win={wr:5.1f}% avgW={aw:+5.2f}% avgL={al:+5.2f}% "
          f"exp={exp:+.3f}% reasons={reasons}")
    return {"n": len(rr), "win": wr, "exp": exp, "avgW": aw, "avgL": al}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", choices=["validate", "exit", "entry", "intraday"], default="validate")
    ap.add_argument("--market", choices=["IN", "US", "ALL"], default="ALL")
    ap.add_argument("--limit", type=int, default=400)
    ap.add_argument("--horizon", type=int, default=12, help="max daily bars to replay")
    ap.add_argument("--since", default="2026-05-27")
    ap.add_argument("--until", default="2026-06-09")
    args = ap.parse_args()

    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=60)
    sigs = load_signals(con, args.limit, since=args.since, until=args.until)
    if args.market != "ALL":
        sigs = [s for s in sigs if s["market"] == args.market]
    print(f"loaded {len(sigs)} signals with valid risk leg (since {args.since}, market={args.market})")

    # attach candles (intraday for the intraday stage, daily otherwise)
    enriched = []
    no_candles = 0
    for s in sigs:
        if args.stage == "intraday":
            bars, src = fetch_intraday(con, s["symbol"], s["market"], s["entry_ts"])
        else:
            bars, src = fetch_candles(con, s["symbol"], s["market"], s["entry_ts"], args.horizon)
        if not bars:
            no_candles += 1
            continue
        s["_bars"] = bars
        s["_src"] = src
        enriched.append(s)
    print(f"{len(enriched)} signals with replayable candles ({no_candles} skipped, no candles)\n")

    if args.stage == "intraday":
        day0_mfe = [( (max(b[2] for b in s["_bars"] if b[5]==s["_bars"][0][5]) - s["entry"])/s["entry"]*100 ) for s in enriched]
        eod = [simulate_intraday(s, s["_bars"], "eod_exit") for s in enriched]
        hold = [simulate_intraday(s, s["_bars"], "hold") for s in enriched]
        harv = [simulate_intraday(s, s["_bars"], "intraday_harvest") for s in enriched]
        rtgt = [simulate_intraday(s, s["_bars"], "r_target") for s in enriched]
        print("INTRADAY A/B (entry at signal time, day-0 inclusive):")
        print(f"  day-0 MFE available: mean {st.mean(day0_mfe):+.2f}%  median {st.median(day0_mfe):+.2f}%  "
              f"(how much intraday upside exists after entry)")
        summarize(eod, "eod_exit")
        summarize(hold, "hold_2d")
        summarize(harv, "intraday_h")
        summarize(rtgt, "r_target")
        # Does the score actually predict intraday outcome? (eod_exit realized)
        print("\n  EDGE BY SCORE BUCKET (eod_exit realized):")
        for lo, hi in [(0, 70), (70, 80), (80, 90), (90, 201)]:
            idx = [i for i, s in enumerate(enriched)
                   if s["score"] is not None and lo <= s["score"] < hi]
            if idx:
                rr = [eod[i]["realized"] for i in idx]
                wr = sum(1 for x in rr if x > 0) / len(rr) * 100
                print(f"    score {lo}-{hi:<3} n={len(idx):>5} win={wr:4.1f}% exp={st.mean(rr):+.3f}%")
        print("  EDGE BY CONFLUENCE BUCKET (eod_exit realized):")
        for lo, hi in [(0, 16), (16, 18), (18, 22), (22, 40)]:
            idx = [i for i, s in enumerate(enriched)
                   if s["confluence"] is not None and lo <= s["confluence"] < hi]
            if idx:
                rr = [eod[i]["realized"] for i in idx]
                wr = sum(1 for x in rr if x > 0) / len(rr) * 100
                print(f"    conf {lo}-{hi:<3} n={len(idx):>5} win={wr:4.1f}% exp={st.mean(rr):+.3f}%")
        print("  EDGE BY ENTRY EXTENSION (day_gain at entry; chase vs pullback):")
        for lo, hi, lab in [(-99, 0, "down (<0%)"), (0, 2, "mild 0-2%"),
                            (2, 4, "up 2-4%"), (4, 99, "extended >4%")]:
            idx = [i for i, s in enumerate(enriched)
                   if s["day_gain_pct"] is not None and lo <= s["day_gain_pct"] < hi]
            if idx:
                rr = [eod[i]["realized"] for i in idx]
                wr = sum(1 for x in rr if x > 0) / len(rr) * 100
                print(f"    {lab:14s} n={len(idx):>5} win={wr:4.1f}% exp={st.mean(rr):+.3f}%")
        return

    if args.stage == "validate":
        cur_diffs, peak_ok, worst_ok = [], 0, 0
        peak_n = worst_n = 0
        sample = []
        for s in enriched:
            last_dt = parse_ts(s["last_ts"]) or parse_ts(s["entry_ts"])
            window = [b for b in s["_bars"] if parse_ts(b[0]) and parse_ts(b[0]) <= last_dt] or s["_bars"][:1]
            sim = simulate(s, window, policy="hold_track")
            comp_cur = (window[-1][4] - s["entry"]) / s["entry"] * 100  # close at last_seen
            if s["rec_cur"] is not None:
                cur_diffs.append(comp_cur - s["rec_cur"])
            if s["rec_peak"] is not None:
                peak_n += 1
                peak_ok += 1 if sim["mfe_pct"] >= s["rec_peak"] - 0.5 else 0
            if s["rec_worst"] is not None:
                worst_n += 1
                worst_ok += 1 if sim["mae_pct"] <= s["rec_worst"] + 0.5 else 0
            if len(sample) < 8:
                sample.append((s["symbol"], round(comp_cur, 2), s["rec_cur"],
                               round(sim["mfe_pct"], 2), s["rec_peak"]))
        print("VALIDATION (daily replay vs recorded tracking):")
        print("  sample [sym, computed_current%, rec_current, mfe%, rec_peak]:")
        for row in sample:
            print("   ", row)
        if cur_diffs:
            within = sum(1 for d in cur_diffs if abs(d) <= 1.0) / len(cur_diffs) * 100
            print(f"\n  computed_current vs rec_current: mean diff {st.mean(cur_diffs):+.2f}pp  "
                  f"median {st.median(cur_diffs):+.2f}pp  |within 1pp: {within:.0f}%")
        if peak_n:
            print(f"  MFE >= recorded_peak:  {peak_ok}/{peak_n} ({peak_ok/peak_n*100:.0f}%)  (daily high bounds tracked peak)")
        if worst_n:
            print(f"  MAE <= recorded_worst: {worst_ok}/{worst_n} ({worst_ok/worst_n*100:.0f}%)  (daily low bounds tracked worst)")
        return

    if args.stage == "exit":
        cur = [dict(simulate(s, s["_bars"], "current"), id=s["id"]) for s in enriched]
        har = [dict(simulate(s, s["_bars"], "harvest"), id=s["id"]) for s in enriched]
        print("EXIT POLICY A/B (same entries):")
        a = summarize(cur, "current")
        b = summarize(har, "harvest")
        if a and b:
            print(f"\n  expectancy delta (harvest - current): {b['exp']-a['exp']:+.3f}% per trade")
            cap_cur = st.mean([min(r['realized'], r['mfe_pct']) for r in cur])
            print(f"  current avg realized {a['exp']:+.2f}% vs avg MFE available "
                  f"{st.mean([r['mfe_pct'] for r in cur]):+.2f}%")
        return

    if args.stage == "entry":
        def is_chase(s):
            # already extended and pinned near the day high
            return (s["day_gain_pct"] is not None and s["day_gain_pct"] >= 4.0
                    and s["high_dist_pct"] is not None and s["high_dist_pct"] <= 1.0
                    and (s["range_pos"] is None or s["range_pos"] >= 0.8))
        admitted = [s for s in enriched if not is_chase(s)]
        rejected = [s for s in enriched if is_chase(s)]
        print(f"ANTI-CHASE ENTRY FILTER: admitted={len(admitted)} rejected(chase)={len(rejected)}")
        res_adm = [simulate(s, s["_bars"], "harvest") for s in admitted]
        res_rej = [simulate(s, s["_bars"], "harvest") for s in rejected]
        res_all = [simulate(s, s["_bars"], "harvest") for s in enriched]
        print("\n(harvest exits throughout)")
        summarize(res_all, "ALL")
        summarize(res_adm, "admitted")
        summarize(res_rej, "rejected")
        return


if __name__ == "__main__":
    main()
