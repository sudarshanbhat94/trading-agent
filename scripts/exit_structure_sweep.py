"""Exit-structure + universe-liquidity sweep over real cached entries.

Collects every WATCH/ENTRY_READY entry once (with its future intraday path and a
liquidity estimate), then re-simulates outcomes across a grid of stop%, target%,
and max-hold (in market days) to find the exit structure with the best
expectancy. Also splits by a turnover/liquidity floor and by setup family.
"""
from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import statistics
import sys
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_spec = importlib.util.spec_from_file_location(
    "_bt_hist", ROOT / "scripts" / "backtest_upstox_history_entry_authority.py"
)
bt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bt)

from app.config import Settings
from app.raw_entry_model import evaluate_raw_entry


def collect_entries(args) -> list[dict[str, Any]]:
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    rows = bt._load_universe(Path(args.universe), "")
    settings = replace(Settings(), dynamic_scan_sentiment_enabled=False,
                       dynamic_scan_require_active_setup=False, dynamic_scan_min_score=0.0)
    scanner = bt.HistoricalOpportunityScanner(settings)
    scanner.sentiment_enabled = False
    scanner.require_active_setup = False
    scanner.min_score = 0.0
    fetched = asyncio.run(bt._fetch_all(rows, "https://api.upstox.com/v2", args.interval,
                          start, end, 420, 4, Path(args.cache_dir), cache_only=True, request_spacing_seconds=0.0))
    universe_by_symbol = {str(r["symbol"]).upper(): r for r in rows}
    states_by_ts = bt._historical_quote_states(fetched, start, end)
    entries: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for ts in sorted(states_by_ts):
        ss = states_by_ts[ts]
        if not ss:
            continue
        quotes, candle_sets, cycle_rows = {}, {}, []
        mday = bt._market_day(ts)
        for symbol, state in ss.items():
            row = universe_by_symbol.get(symbol)
            if not row:
                continue
            dp = bt._daily_before(fetched[symbol]["daily"], mday)
            if len(dp) < 55:
                continue
            quotes[symbol] = state["quote"]
            candle_sets[symbol] = {"daily": dp, "analysis": dp, "intraday": [state["bar"]]}
            cycle_rows.append(row)
        if not cycle_rows:
            continue
        regime = bt.compute_market_day_regime(cycle_rows, quotes, candle_sets,
            {"enabled": True, "breadth_regime": "neutral",
             "advance_decline_ratio": bt._advance_decline_ratio(cycle_rows, quotes, candle_sets)}, market_region="IN")
        result = scanner.rank(cycle_rows, quotes, candle_sets, sentiment_by_symbol={})
        for sel in result.selected_universe:
            symbol = str(sel.get("symbol") or "").upper()
            quote = quotes.get(symbol)
            if not quote:
                continue
            dp = candle_sets.get(symbol, {}).get("daily") or []
            tech = bt._technical_for(dp, quote)
            scan = sel.get("_opportunity_scan") if isinstance(sel.get("_opportunity_scan"), dict) else {}
            ctx = {"symbol": symbol, "market_region": "IN", "sector": universe_by_symbol[symbol].get("sector"),
                   "quote": quote.to_dict(), "technical_math": tech.to_dict(),
                   "sentiment": {"score": 0.0, "confidence": 0.0, "headline_count": 0, "headlines": [], "events": [], "status": "NO_TIMESTAMP_SAFE_NEWS"},
                   "data_readiness": {"market_region": "IN", "trade_decision_ready": True,
                       "sources": {"quote": "upstox-history", "intraday": f"upstox-history:{args.interval}", "daily": "upstox-history:day"}},
                   "opportunity_scan": scan, "market_day_regime": regime,
                   "full_spectrum_analysis": {"liquidity_profile": scan.get("liquidity_profile") if isinstance(scan.get("liquidity_profile"), dict) else {}, "trade_plan": {}}}
            model = evaluate_raw_entry(ctx, settings)
            if str(model.get("decision_label")) not in ("WATCH", "ENTRY_READY"):
                continue
            key = (mday.isoformat(), symbol)
            if key in seen:
                continue
            seen.add(key)
            entry_ts = bt._parse_ts(quote.asof)
            # future intraday path
            path = []
            for bar in fetched[symbol]["intraday"]:
                bts = bt._parse_ts(bar.ts)
                if bts <= entry_ts:
                    continue
                path.append((bts.isoformat(), bt._market_day(bts).isoformat(), float(bar.high), float(bar.low), float(bar.close)))
            # 20-day avg turnover (price*vol) from daily prior
            tv = [float(c.close) * float(c.volume or 0.0) for c in dp[-20:]]
            turnover = statistics.mean(tv) if tv else 0.0
            entries.append({"symbol": symbol, "entry_day": mday.isoformat(), "entry": float(quote.price),
                            "score": float(model.get("raw_score") or 0.0), "family": str(model.get("setup_family")),
                            "turnover": turnover, "path": path})
    return entries


def sim(entry: float, entry_day: str, path, stop_pct: float, tgt_pct: float, max_days: int) -> tuple[float, str]:
    stop = entry * (1 - stop_pct / 100.0)
    tgt = entry * (1 + tgt_pct / 100.0)
    days_seen = []
    last_close = entry
    for _, day, high, low, close in path:
        if day not in days_seen:
            days_seen.append(day)
        if len(days_seen) > max_days:
            break
        last_close = close
        if low <= stop:
            return (stop - entry) / entry * 100.0, "stop"
        if high >= tgt:
            return (tgt - entry) / entry * 100.0, "target"
    return (last_close - entry) / entry * 100.0, "mark"


def stats(rs: list[float]) -> dict:
    if not rs:
        return {"n": 0}
    return {"n": len(rs), "win": round(sum(1 for r in rs if r > 0.05) / len(rs), 3),
            "avg_gross%": round(statistics.mean(rs), 3), "med%": round(statistics.median(rs), 3),
            "tot%": round(sum(rs), 1)}


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--universe", default="data/universe.csv")
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--interval", default="30minute")
    p.add_argument("--cache-dir", required=True)
    p.add_argument("--min-turnover", type=float, default=0.0, help="INR avg daily turnover floor")
    p.add_argument("--min-score", type=float, default=56.0)
    args = p.parse_args()

    entries = collect_entries(args)
    elig = [e for e in entries if e["score"] >= args.min_score and e["turnover"] >= args.min_turnover and e["path"]]
    print(f"collected={len(entries)} eligible(score>={args.min_score},turnover>={args.min_turnover:.0f})={len(elig)}")

    grid_stop = [3.5, 5.0, 7.0, 9.0]
    grid_tgt = [4.0, 6.0, 8.0, 12.0]
    grid_hold = [1, 3, 7]
    print("\n=== EXIT GRID (gross %, before costs) ===")
    best = []
    for s in grid_stop:
        for t in grid_tgt:
            for h in grid_hold:
                rs = [sim(e["entry"], e["entry_day"], e["path"], s, t, h)[0] for e in elig]
                st = stats(rs)
                best.append((st.get("avg_gross%", -99), s, t, h, st))
    best.sort(reverse=True)
    for avg, s, t, h, st in best[:12]:
        print(f"  stop={s:>4}% tgt={t:>4}% hold={h}d -> n={st['n']:4} win={st['win']:.3f} avg={st['avg_gross%']:+.3f}% med={st['med%']:+.3f}% tot={st['tot%']:+.0f}%")
    print("  ... worst:")
    for avg, s, t, h, st in best[-3:]:
        print(f"  stop={s:>4}% tgt={t:>4}% hold={h}d -> n={st['n']:4} win={st['win']:.3f} avg={st['avg_gross%']:+.3f}%")

    # liquidity split at best config
    _, bs, bt_, bh, _ = best[0]
    print(f"\n=== LIQUIDITY SPLIT at stop={bs}% tgt={bt_}% hold={bh}d ===")
    for floor in [0, 5e7, 1e8, 5e8, 1e9]:
        sub = [e for e in elig if e["turnover"] >= floor]
        rs = [sim(e["entry"], e["entry_day"], e["path"], bs, bt_, bh)[0] for e in sub]
        print(f"  turnover>={floor/1e7:>5.0f}cr -> {stats(rs)}")

    print(f"\n=== BY FAMILY at stop={bs}% tgt={bt_}% hold={bh}d (turnover>=10cr) ===")
    fams: dict[str, list[float]] = {}
    for e in elig:
        if e["turnover"] < 1e8:
            continue
        fams.setdefault(e["family"], []).append(sim(e["entry"], e["entry_day"], e["path"], bs, bt_, bh)[0])
    for fam, rs in sorted(fams.items(), key=lambda kv: -statistics.mean(kv[1]) if kv[1] else 0):
        print(f"  {fam:32} {stats(rs)}")


if __name__ == "__main__":
    main()
