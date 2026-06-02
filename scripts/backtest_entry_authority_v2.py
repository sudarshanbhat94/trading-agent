from __future__ import annotations

import argparse
import json
import math
import sqlite3
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import Settings
from app.raw_entry_model import evaluate_raw_entry
from app.trade_economics import exit_economics, minimum_auto_follow_notional


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only entry_authority_v2 backtest over stored decision contexts.")
    parser.add_argument("--database", default="./var/trading_agent.db")
    parser.add_argument("--start", default="")
    parser.add_argument("--end", default="")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    now = datetime.now(timezone.utc)
    start = _parse_bound(args.start) or now - timedelta(days=7)
    end = _parse_bound(args.end) or now
    db_path = Path(args.database).expanduser().resolve()
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    settings = Settings()
    universe = {row["symbol"]: row["exchange"] for row in conn.execute("select symbol, exchange from universe")}

    rows = conn.execute(
        """
        select id, ts, symbol, action, price, confidence, strategy, details_json
        from decisions
        where ts >= ? and ts < ?
        order by ts
        """,
        (start.isoformat(), end.isoformat()),
    ).fetchall()

    candidates: dict[tuple[str, str, str], dict[str, Any]] = {}
    context_rows = 0
    entry_ready_seen = 0
    label_counts: dict[str, int] = defaultdict(int)
    family_counts: dict[str, int] = defaultdict(int)
    blocker_counts: dict[str, int] = defaultdict(int)
    for row in rows:
        audit = _json(row["details_json"])
        context = _context_from_audit(audit)
        if not context:
            continue
        context_rows += 1
        context.setdefault("symbol", row["symbol"])
        context.setdefault("market_region", _market_for(row["symbol"], context, universe))
        context.setdefault("quote", {}).setdefault("price", row["price"])
        model = evaluate_raw_entry(context, settings)
        label = str(model.get("decision_label") or "UNKNOWN")
        label_counts[label] += 1
        family_counts[str(model.get("setup_family") or "none")] += 1
        for blocker in model.get("entry_blockers") or []:
            if isinstance(blocker, dict):
                blocker_counts[str(blocker.get("reason") or "unknown")] += 1
        if label != "ENTRY_READY":
            continue
        entry_ready_seen += 1
        ts = _parse_ts(row["ts"])
        if ts is None:
            continue
        market = str(model.get("market_region") or context.get("market_region") or _market_for(row["symbol"], context, universe)).upper()
        key = (market, _local_day(ts, market), str(row["symbol"]).upper())
        if key not in candidates:
            candidates[key] = {
                "id": row["id"],
                "ts": row["ts"],
                "symbol": str(row["symbol"]).upper(),
                "price": float(row["price"] or 0.0),
                "market": market,
                "model": model,
            }

    trades = [_simulate(conn, item, settings) for item in candidates.values()]
    trades = [trade for trade in trades if trade]
    report = {
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "decision_rows": len(rows),
        "context_rows": context_rows,
        "entry_ready_decisions_seen": entry_ready_seen,
        "first_symbol_day_entries": len(trades),
        "label_counts": dict(sorted(label_counts.items())),
        "setup_family_counts": dict(sorted(family_counts.items(), key=lambda item: item[1], reverse=True)[:12]),
        "blocker_counts": dict(sorted(blocker_counts.items(), key=lambda item: item[1], reverse=True)[:12]),
        "overall": _summary(trades),
        "by_market": _group_summary(trades, "market"),
        "by_day": _group_summary(trades, "entry_day"),
        "worst": _sample(trades, reverse=False),
        "best": _sample(trades, reverse=True),
    }
    print(json.dumps(report, indent=2 if args.json else None, sort_keys=args.json))


def _context_from_audit(audit: dict[str, Any]) -> dict[str, Any]:
    context = audit.get("context") if isinstance(audit.get("context"), dict) else {}
    if context:
        return dict(context)
    raw = audit.get("raw_entry_model") if isinstance(audit.get("raw_entry_model"), dict) else {}
    inputs = raw.get("inputs") if isinstance(raw.get("inputs"), dict) else {}
    if not inputs:
        risk = audit.get("risk_gates") if isinstance(audit.get("risk_gates"), dict) else {}
        gate = risk.get("decision_gate_context") if isinstance(risk.get("decision_gate_context"), dict) else {}
        raw = gate.get("raw_entry_model") if isinstance(gate.get("raw_entry_model"), dict) else {}
        inputs = raw.get("inputs") if isinstance(raw.get("inputs"), dict) else {}
    if not inputs:
        return {}
    return {
        "market_region": raw.get("market_region"),
        "quote": {"price": audit.get("price"), "source": inputs.get("quote_source")},
        "data_readiness": inputs.get("data_readiness") if isinstance(inputs.get("data_readiness"), dict) else {},
        "opportunity_scan": inputs.get("opportunity_scan") if isinstance(inputs.get("opportunity_scan"), dict) else {},
        "full_spectrum_analysis": {
            "liquidity_profile": inputs.get("liquidity") if isinstance(inputs.get("liquidity"), dict) else {}
        },
    }


def _simulate(conn: sqlite3.Connection, item: dict[str, Any], settings: Settings) -> dict[str, Any] | None:
    entry = float(item.get("price") or 0.0)
    if entry <= 0:
        return None
    market = str(item.get("market") or "IN").upper()
    entry_ts = _parse_ts(item.get("ts"))
    if entry_ts is None:
        return None
    plan = item.get("model", {}).get("trade_plan") if isinstance(item.get("model"), dict) else {}
    stop = _num(plan.get("stop_loss")) if isinstance(plan, dict) else None
    targets = plan.get("targets") if isinstance(plan, dict) else []
    target = _num((targets[0] or {}).get("price")) if isinstance(targets, list) and targets else None
    stop = stop if stop and stop < entry else entry * 0.965
    target = target if target and target > entry else entry * 1.063
    candles = _future_candles(conn, str(item["symbol"]), market, entry_ts)
    reason = "no_future_candles"
    exit_price = entry
    source = ""
    for candle_ts, high, low, close, src in candles:
        if candle_ts > entry_ts + timedelta(days=7):
            break
        source = src
        exit_price = close
        reason = "open_or_period_mark"
        if low <= stop:
            exit_price = stop
            reason = "stop"
            break
        if high >= target:
            exit_price = target
            reason = "target"
            break
    min_notional = minimum_auto_follow_notional(settings, market)
    qty = max(1, int(math.ceil(min_notional / entry))) if min_notional > 0 else 1
    economics = exit_economics(entry, exit_price, qty, market, settings)
    entry_notional = float(economics.get("entry_notional") or entry * qty)
    net_pnl = float(economics.get("estimated_net_pnl") or 0.0)
    return {
        "symbol": item["symbol"],
        "market": market,
        "entry_day": _local_day(entry_ts, market),
        "entry_ts": item["ts"],
        "entry": round(entry, 4),
        "qty": qty,
        "exit": round(exit_price, 4),
        "exit_reason": reason,
        "source": source,
        "gross_pct": round(((exit_price - entry) / entry) * 100.0, 4),
        "net_pct": round((net_pnl / entry_notional) * 100.0, 4) if entry_notional else 0.0,
        "net_pnl": round(net_pnl, 2),
        "cost": float(economics.get("estimated_round_trip_cost") or 0.0),
        "score": round(float(item.get("model", {}).get("raw_score") or 0.0), 4),
        "setup_family": item.get("model", {}).get("setup_family"),
    }


def _future_candles(conn: sqlite3.Connection, symbol: str, market: str, entry_ts: datetime) -> list[tuple[datetime, float, float, float, str]]:
    sources = ["upstox-live:30minute", "upstox-live:day"] if market == "IN" else [
        "alpaca-iex-live:1minute",
        "alpaca-iex-live:day",
        "yahoo-delayed",
    ]
    for source in sources:
        rows = conn.execute(
            "select ts, high, low, close, source from candles where symbol = ? and source = ? order by ts",
            (symbol, source),
        ).fetchall()
        parsed = []
        for row in rows:
            ts = _parse_ts(row["ts"])
            if ts and ts > entry_ts:
                parsed.append((ts, float(row["high"]), float(row["low"]), float(row["close"]), row["source"]))
        if parsed:
            return parsed
    return []


def _summary(trades: list[dict[str, Any]]) -> dict[str, Any]:
    priced = [trade for trade in trades if trade["exit_reason"] != "no_future_candles"]
    gross = [float(trade["gross_pct"]) for trade in priced]
    net = [float(trade["net_pct"]) for trade in priced]
    return {
        "n": len(trades),
        "priced": len(priced),
        "targets": sum(1 for trade in priced if trade["exit_reason"] == "target"),
        "stops": sum(1 for trade in priced if trade["exit_reason"] == "stop"),
        "marks": sum(1 for trade in priced if trade["exit_reason"] == "open_or_period_mark"),
        "no_future": len(trades) - len(priced),
        "gross_win_rate": round(sum(1 for value in gross if value > 0) / len(gross), 4) if gross else 0.0,
        "avg_gross_pct": round(statistics.mean(gross), 4) if gross else 0.0,
        "net_win_rate": round(sum(1 for value in net if value > 0) / len(net), 4) if net else 0.0,
        "avg_net_pct": round(statistics.mean(net), 4) if net else 0.0,
        "median_net_pct": round(statistics.median(net), 4) if net else 0.0,
        "sum_net_pnl": round(sum(float(trade["net_pnl"]) for trade in priced), 2),
        "sum_cost": round(sum(float(trade["cost"]) for trade in priced), 2),
    }


def _group_summary(trades: list[dict[str, Any]], key: str) -> dict[str, Any]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for trade in trades:
        groups[str(trade.get(key) or "?")].append(trade)
    return {name: _summary(rows) for name, rows in sorted(groups.items())}


def _sample(trades: list[dict[str, Any]], *, reverse: bool) -> list[dict[str, Any]]:
    priced = [trade for trade in trades if trade["exit_reason"] != "no_future_candles"]
    return sorted(priced, key=lambda trade: float(trade["net_pct"]), reverse=reverse)[:10]


def _market_for(symbol: str, context: dict[str, Any], universe: dict[str, str]) -> str:
    market = str(context.get("market_region") or "").upper()
    if market in {"IN", "US"}:
        return market
    exchange = str(universe.get(str(symbol).upper()) or "").upper()
    return "IN" if exchange in {"NSE", "BSE"} else "US"


def _local_day(ts: datetime, market: str) -> str:
    return (ts + timedelta(hours=5, minutes=30)).date().isoformat() if market == "IN" else ts.date().isoformat()


def _parse_bound(value: str) -> datetime | None:
    if not value:
        return None
    if len(value) == 10:
        value = f"{value}T00:00:00+00:00"
    return _parse_ts(value)


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _json(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _num(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


if __name__ == "__main__":
    main()
