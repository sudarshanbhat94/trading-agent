"""Read-only strategy refinement analysis.

Replays the opportunity scanner + raw entry model over cached Upstox history and
simulates the trade outcome for EVERY WATCH/ENTRY_READY candidate (not just the
ones above the live entry floor). Outputs expectancy bucketed by raw_score and by
setup family so we can choose an evidence-based entry threshold and prune
negative-expectancy setups.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import statistics
import sys
from collections import defaultdict
from dataclasses import replace
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Load the existing backtester module to reuse its tested helpers.
_spec = importlib.util.spec_from_file_location(
    "_bt_hist", ROOT / "scripts" / "backtest_upstox_history_entry_authority.py"
)
bt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bt)

from app.config import Settings
from app.raw_entry_model import evaluate_raw_entry


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--universe", default="data/universe.csv")
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--interval", default="30minute")
    p.add_argument("--cache-dir", required=True)
    p.add_argument("--max-symbols", type=int, default=0)
    p.add_argument("--jsonl", default="")
    args = p.parse_args()

    import asyncio
    from datetime import date

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    rows = bt._load_universe(Path(args.universe), "")
    if args.max_symbols and args.max_symbols > 0:
        rows = rows[: args.max_symbols]

    settings = replace(
        Settings(),
        dynamic_scan_sentiment_enabled=False,
        dynamic_scan_require_active_setup=False,
        dynamic_scan_min_score=0.0,
    )
    scanner = bt.HistoricalOpportunityScanner(settings)
    scanner.sentiment_enabled = False
    scanner.require_active_setup = False
    scanner.min_score = 0.0

    fetched = asyncio.run(
        bt._fetch_all(rows, args.interval and "https://api.upstox.com/v2", args.interval,
                      start, end, 420, 4, Path(args.cache_dir), cache_only=True,
                      request_spacing_seconds=0.0)
    )
    universe_by_symbol = {str(r["symbol"]).upper(): r for r in rows}
    states_by_ts = bt._historical_quote_states(fetched, start, end)

    candidates: list[dict[str, Any]] = []
    seen_keys: set[tuple[str, str]] = set()
    for ts in sorted(states_by_ts):
        symbol_states = states_by_ts[ts]
        if not symbol_states:
            continue
        quotes = {}
        candle_sets = {}
        cycle_rows = []
        market_day = bt._market_day(ts)
        for symbol, state in symbol_states.items():
            row = universe_by_symbol.get(symbol)
            if not row:
                continue
            daily_prior = bt._daily_before(fetched[symbol]["daily"], market_day)
            if len(daily_prior) < 55:
                continue
            quotes[symbol] = state["quote"]
            candle_sets[symbol] = {"daily": daily_prior, "analysis": daily_prior, "intraday": [state["bar"]]}
            cycle_rows.append(row)
        if not cycle_rows:
            continue
        regime = bt.compute_market_day_regime(
            cycle_rows, quotes, candle_sets,
            {"enabled": True, "breadth_regime": "neutral",
             "advance_decline_ratio": bt._advance_decline_ratio(cycle_rows, quotes, candle_sets)},
            market_region="IN",
        )
        result = scanner.rank(cycle_rows, quotes, candle_sets, sentiment_by_symbol={})
        for selected in result.selected_universe:
            symbol = str(selected.get("symbol") or "").upper()
            quote = quotes.get(symbol)
            if not quote:
                continue
            daily_prior = candle_sets.get(symbol, {}).get("daily") or []
            tech = bt._technical_for(daily_prior, quote)
            scan = selected.get("_opportunity_scan") if isinstance(selected.get("_opportunity_scan"), dict) else {}
            context = {
                "symbol": symbol, "market_region": "IN", "sector": universe_by_symbol[symbol].get("sector"),
                "quote": quote.to_dict(), "technical_math": tech.to_dict(),
                "sentiment": {"score": 0.0, "confidence": 0.0, "headline_count": 0, "headlines": [], "events": [],
                              "status": "NO_TIMESTAMP_SAFE_NEWS"},
                "data_readiness": {"market_region": "IN", "trade_decision_ready": True,
                                   "sources": {"quote": "upstox-history", "intraday": f"upstox-history:{args.interval}",
                                               "daily": "upstox-history:day"}},
                "opportunity_scan": scan, "market_day_regime": regime,
                "full_spectrum_analysis": {"liquidity_profile": scan.get("liquidity_profile") if isinstance(scan.get("liquidity_profile"), dict) else {}, "trade_plan": {}},
            }
            model = evaluate_raw_entry(context, settings)
            label = str(model.get("decision_label") or "UNKNOWN")
            if label not in ("WATCH", "ENTRY_READY"):
                continue
            key = (market_day.isoformat(), symbol)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            trade = bt._simulate_trade(symbol, quote, model, fetched[symbol]["intraday"], settings)
            if not trade or trade["exit_reason"] == "no_future_candles":
                continue
            trade["decision_label"] = label
            candidates.append(trade)

    _report(candidates, args.jsonl)


def _bucket(score: float) -> str:
    lo = int(score // 4 * 4)
    return f"{lo:02d}-{lo+4:02d}"


def _stats(items: list[dict[str, Any]]) -> dict[str, Any]:
    net = [float(t["net_pct"]) for t in items]
    gross = [float(t["gross_pct"]) for t in items]
    if not net:
        return {}
    return {
        "n": len(items),
        "win": round(sum(1 for v in net if v > 0) / len(net), 3),
        "avg_net%": round(statistics.mean(net), 3),
        "med_net%": round(statistics.median(net), 3),
        "avg_gross%": round(statistics.mean(gross), 3),
        "expectancy_sum_pnl": round(sum(float(t["net_pnl"]) for t in items), 0),
        "targets": sum(1 for t in items if t["exit_reason"] == "target"),
        "stops": sum(1 for t in items if t["exit_reason"] == "stop"),
    }


def _report(cands: list[dict[str, Any]], jsonl: str) -> None:
    if jsonl:
        with open(jsonl, "w") as fh:
            for c in cands:
                fh.write(json.dumps(c) + "\n")
    by_score = defaultdict(list)
    by_fam = defaultdict(list)
    for c in cands:
        by_score[_bucket(float(c["score"]))].append(c)
        by_fam[str(c.get("setup_family"))].append(c)
    cum = {}
    for thr in (56, 58, 60, 62, 64, 66, 68, 70, 72, 74):
        sub = [c for c in cands if float(c["score"]) >= thr]
        cum[thr] = _stats(sub)
    print(json.dumps({
        "total_priced_candidates": len(cands),
        "overall": _stats(cands),
        "by_score_bucket": {k: _stats(v) for k, v in sorted(by_score.items())},
        "cumulative_at_or_above_threshold": cum,
        "by_setup_family": {k: _stats(v) for k, v in sorted(by_fam.items(), key=lambda kv: -len(kv[1]))},
    }, indent=2))


if __name__ == "__main__":
    main()
