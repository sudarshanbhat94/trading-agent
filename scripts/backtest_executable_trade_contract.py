from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = Path(__file__).resolve().parent
for path in (ROOT, SCRIPT_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from app.config import Settings
from app.market_day_regime import (
    REGIME_BROAD_RALLY,
    REGIME_FADE_RISK,
    REGIME_NEUTRAL_CHOP,
    REGIME_SELECTIVE_RALLY,
)
from app.raw_entry_model import evaluate_raw_entry
from app.signal_quality import auto_follow_quality_gate
from backtest_entry_authority_v2 import (
    _context_from_audit,
    _group_summary,
    _json,
    _local_day,
    _market_for,
    _parse_bound,
    _parse_ts,
    _sample,
    _simulate,
    _summary,
)


def _num(*values: Any) -> float | None:
    for value in values:
        if value in (None, ""):
            continue
        try:
            parsed = float(str(value).replace(",", ""))
        except (TypeError, ValueError):
            continue
        if parsed == parsed:
            return parsed
    return None


def _has_usable_regime(context: dict[str, Any]) -> bool:
    regime = context.get("market_day_regime") if isinstance(context.get("market_day_regime"), dict) else {}
    state = str(regime.get("state") or "").strip().lower()
    checked = int(float(regime.get("checked_symbols") or 0))
    return bool(state and state != "no_live_regime" and checked > 0)


def _infer_replay_regimes(rows: list[sqlite3.Row], universe: dict[str, str]) -> dict[tuple[str, str], dict[str, Any]]:
    buckets: dict[tuple[str, str], list[dict[str, float]]] = defaultdict(list)
    for row in rows:
        audit = _json(row["details_json"])
        context = _context_from_audit(audit)
        if not context:
            continue
        ts = _parse_ts(row["ts"])
        if ts is None:
            continue
        market = str(context.get("market_region") or _market_for(row["symbol"], context, universe)).upper()
        scan = context.get("opportunity_scan") if isinstance(context.get("opportunity_scan"), dict) else {}
        day_gain = _num(scan.get("day_gain_pct"))
        range_position = _num(scan.get("day_range_position"))
        high_distance = _num(scan.get("day_high_distance_pct"))
        volume_ratio = _num(scan.get("volume_ratio"), scan.get("projected_volume_ratio"))
        if day_gain is None:
            continue
        buckets[(market, _local_day(ts, market))].append(
            {
                "day_gain": day_gain,
                "range_position": range_position if range_position is not None else 0.5,
                "high_distance": high_distance if high_distance is not None else 99.0,
                "volume_ratio": volume_ratio if volume_ratio is not None else 0.0,
            }
        )

    regimes: dict[tuple[str, str], dict[str, Any]] = {}
    for key, items in buckets.items():
        checked = len(items)
        if checked < 20:
            continue
        advancers = sum(1 for item in items if item["day_gain"] > 0.0)
        strong = sum(1 for item in items if item["day_gain"] >= 1.5 and item["range_position"] >= 0.62)
        leaders = sum(1 for item in items if item["day_gain"] >= 3.0 and item["range_position"] >= 0.75)
        fading = sum(1 for item in items if item["day_gain"] < -0.75 or (item["high_distance"] >= 2.5 and item["range_position"] <= 0.45))
        advancer_pct = advancers / checked
        strong_pct = strong / checked
        leader_pct = leaders / checked
        fade_pct = fading / checked
        avg_gain = sum(item["day_gain"] for item in items) / checked
        avg_volume_ratio = sum(item["volume_ratio"] for item in items) / checked
        if advancer_pct >= 0.58 and strong_pct >= 0.18 and fade_pct <= 0.28 and avg_gain > 0.45:
            state = REGIME_BROAD_RALLY
            score = 70.0 + min((advancer_pct - 0.58) * 80.0 + strong_pct * 35.0 + max(avg_gain, 0.0) * 2.5, 20.0)
            momentum_allowed = True
            selective_allowed = True
        elif advancer_pct >= 0.45 and (strong_pct >= 0.10 or leader_pct >= 0.035) and fade_pct <= 0.38:
            state = REGIME_SELECTIVE_RALLY
            score = 52.0 + min(strong_pct * 60.0 + leader_pct * 120.0 + max(avg_gain, 0.0) * 1.5, 18.0)
            momentum_allowed = False
            selective_allowed = True
        elif advancer_pct <= 0.36 or fade_pct >= 0.48:
            state = REGIME_FADE_RISK
            score = -20.0 - min(fade_pct * 30.0 + max(-avg_gain, 0.0) * 3.0, 25.0)
            momentum_allowed = False
            selective_allowed = False
        else:
            state = REGIME_NEUTRAL_CHOP
            score = 5.0 + (advancer_pct - 0.50) * 20.0
            momentum_allowed = False
            selective_allowed = False
        market, day = key
        regimes[key] = {
            "enabled": True,
            "market_region": market,
            "state": state,
            "score": round(score, 4),
            "momentum_allowed": momentum_allowed,
            "selective_momentum_allowed": selective_allowed,
            "checked_symbols": checked,
            "advancer_pct": round(advancer_pct, 4),
            "strong_advancer_pct": round(strong_pct, 4),
            "leader_pct": round(leader_pct, 4),
            "fade_pct": round(fade_pct, 4),
            "avg_day_gain_pct": round(avg_gain, 4),
            "avg_volume_ratio": round(avg_volume_ratio, 4),
            "allowed_setup_families": ["live_momentum"] if momentum_allowed or selective_allowed else [],
            "replay_inferred": True,
            "replay_day": day,
            "reasons": [
                "offline_replay_regime_inferred_from_stored_day_gain_and_range",
                f"advancers={advancer_pct:.2f}",
                f"strong={strong_pct:.2f}",
                f"fade={fade_pct:.2f}",
            ],
        }
    return regimes


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only replay of executable raw-entry paper-follow contract.")
    parser.add_argument("--database", default="./var/trading_agent.db")
    parser.add_argument("--start", default="")
    parser.add_argument("--end", default="")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--ablate-gates", default="", help="Comma-separated primary blockers to include in ablation output.")
    parser.add_argument("--by-market", action="store_true", help="Include market/blocker ablation grouping.")
    parser.add_argument("--by-setup-family", action="store_true", help="Include setup-family/blocker ablation grouping.")
    parser.add_argument("--csv", default="", help="Optional CSV path for first-symbol/day ablation rows.")
    parser.add_argument(
        "--infer-replay-regime",
        action="store_true",
        help="Infer market/day regime from stored replay contexts for ablation only; does not affect live gating.",
    )
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
        select id, ts, symbol, action, price, confidence, technical_score, strategy, details_json
        from decisions
        where ts >= ? and ts < ?
        order by ts
        """,
        (start.isoformat(), end.isoformat()),
    ).fetchall()

    candidates: dict[tuple[str, str, str], dict[str, Any]] = {}
    ablation_candidates: dict[tuple[str, str, str], dict[str, Any]] = {}
    context_rows = 0
    entry_ready_seen = 0
    executable_seen = 0
    label_counts: dict[str, int] = defaultdict(int)
    setup_counts: dict[str, int] = defaultdict(int)
    executable_blockers: dict[str, int] = defaultdict(int)
    raw_blockers: dict[str, int] = defaultdict(int)

    replay_regimes = _infer_replay_regimes(rows, universe) if args.infer_replay_regime else {}

    for row in rows:
        audit = _json(row["details_json"])
        context = _context_from_audit(audit)
        if not context:
            continue
        context_rows += 1
        context.setdefault("symbol", row["symbol"])
        context.setdefault("market_region", _market_for(row["symbol"], context, universe))
        context.setdefault("quote", {}).setdefault("price", row["price"])
        ts = _parse_ts(row["ts"])
        market = str(context.get("market_region") or _market_for(row["symbol"], context, universe)).upper()
        replay_regime = replay_regimes.get((market, _local_day(ts, market))) if ts else None
        gate_context = dict(context)
        if replay_regime and not _has_usable_regime(context):
            gate_context["market_day_regime"] = replay_regime
        model = evaluate_raw_entry(gate_context, settings)
        label = str(model.get("decision_label") or "UNKNOWN")
        label_counts[label] += 1
        setup_counts[str(model.get("setup_family") or "none")] += 1
        for blocker in model.get("entry_blockers") or []:
            if isinstance(blocker, dict):
                raw_blockers[str(blocker.get("reason") or "unknown")] += 1
        if label != "ENTRY_READY":
            continue
        entry_ready_seen += 1
        if ts is None:
            continue
        market = str(model.get("market_region") or gate_context.get("market_region") or _market_for(row["symbol"], gate_context, universe)).upper()
        gate = auto_follow_quality_gate(_gate_item(row, gate_context, model, market))
        reason = "EXECUTABLE" if gate.get("passed") else str(gate.get("reason") or "unknown")
        key = (market, _local_day(ts, market), str(row["symbol"]).upper())
        if key not in ablation_candidates:
            ablation_candidates[key] = {
                "id": row["id"],
                "ts": row["ts"],
                "symbol": str(row["symbol"]).upper(),
                "price": float(row["price"] or 0.0),
                "market": market,
                "model": model,
                "executable_gate": gate,
                "primary_blocker": reason,
                "context": gate_context,
                "replay_regime_inferred": bool(replay_regime and not _has_usable_regime(context)),
                "strategy": row["strategy"],
            }
        if not gate.get("passed"):
            executable_blockers[reason] += 1
            continue
        executable_seen += 1
        if key not in candidates:
            candidates[key] = {
                "id": row["id"],
                "ts": row["ts"],
                "symbol": str(row["symbol"]).upper(),
                "price": float(row["price"] or 0.0),
                "market": market,
                "model": model,
                "executable_gate": gate,
            }

    trades = [_simulate(conn, item, settings) for item in candidates.values()]
    trades = [trade for trade in trades if trade]
    ablation_rows = _ablation_rows(conn, ablation_candidates.values(), settings, args.ablate_gates)
    if args.csv:
        _write_ablation_csv(args.csv, ablation_rows)
    report = {
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "replay_regime_inferred": bool(args.infer_replay_regime),
        "replay_regime_days": len(replay_regimes),
        "decision_rows": len(rows),
        "context_rows": context_rows,
        "entry_ready_decisions_seen": entry_ready_seen,
        "executable_decisions_seen": executable_seen,
        "first_symbol_day_entries": len(trades),
        "label_counts": dict(sorted(label_counts.items())),
        "setup_family_counts": dict(sorted(setup_counts.items(), key=lambda item: item[1], reverse=True)[:12]),
        "raw_blocker_counts": dict(sorted(raw_blockers.items(), key=lambda item: item[1], reverse=True)[:12]),
        "executable_blocker_counts": dict(sorted(executable_blockers.items(), key=lambda item: item[1], reverse=True)[:12]),
        "overall": _summary(trades),
        "by_market": _group_summary(trades, "market"),
        "by_day": _group_summary(trades, "entry_day"),
        "worst": _sample(trades, reverse=False),
        "best": _sample(trades, reverse=True),
        "ablation": {
            "raw_first_symbol_day_candidates": len(ablation_rows),
            "classification_counts": _count_by(ablation_rows, "suppression_classification"),
            "classification_reason_counts": _count_by(ablation_rows, "suppression_classification_reason"),
            "candidate_false_negative_blockers": _count_by_filtered(
                ablation_rows,
                "primary_blocker",
                "suppression_classification",
                "candidate_false_negative",
            ),
            "context_dependent_blockers": _count_by_filtered(
                ablation_rows,
                "primary_blocker",
                "suppression_classification",
                "context_dependent_blocked",
            ),
            "ambiguous_blockers": _count_by_filtered(
                ablation_rows,
                "primary_blocker",
                "suppression_classification",
                "ambiguous",
            ),
            "correctly_blocked_blockers": _count_by_filtered(
                ablation_rows,
                "primary_blocker",
                "suppression_classification",
                "correctly_blocked",
            ),
            "paper_probe_eligible": sum(1 for row in ablation_rows if row.get("paper_probe_eligible")),
            "paper_probe_blocker_counts": _paper_probe_blocker_counts(ablation_rows),
            "paper_probe_blocker_counts_by_market": _paper_probe_blocker_counts_by(ablation_rows, "market"),
            "paper_probe_blocker_counts_by_primary_blocker": _paper_probe_blocker_counts_by(ablation_rows, "primary_blocker"),
            "paper_probe_blocker_counts_by_market_primary_blocker": _paper_probe_blocker_counts_by(
                ablation_rows,
                "market_primary_blocker",
            ),
            "by_primary_blocker": _group_summary(ablation_rows, "primary_blocker"),
            "by_classification_primary_blocker": _group_summary(ablation_rows, "classification_primary_blocker"),
            "by_market_classification": _group_summary(ablation_rows, "market_classification"),
        },
    }
    if args.by_market:
        report["ablation"]["by_market_primary_blocker"] = _group_summary(ablation_rows, "market_primary_blocker")
    if args.by_setup_family:
        report["ablation"]["by_setup_family_primary_blocker"] = _group_summary(ablation_rows, "setup_family_primary_blocker")
    print(json.dumps(report, indent=2 if args.json else None, sort_keys=args.json))


def _gate_item(row: sqlite3.Row, context: dict[str, Any], model: dict[str, Any], market: str) -> dict[str, Any]:
    full = context.get("full_spectrum_analysis") if isinstance(context.get("full_spectrum_analysis"), dict) else {}
    confluence = full.get("confluence_score") if isinstance(full.get("confluence_score"), dict) else {}
    risk = full.get("risk_overrides") if isinstance(full.get("risk_overrides"), dict) else {}
    plan = model.get("trade_plan") if isinstance(model.get("trade_plan"), dict) else {}
    quote = context.get("quote") if isinstance(context.get("quote"), dict) else {}
    price = float(row["price"] or quote.get("price") or 0.0)
    details = {
        "action": "BUY",
        "market_region": market,
        "strategy": row["strategy"],
        "latest_price": price,
        "stop_loss": plan.get("stop_loss"),
        "targets": plan.get("targets"),
        "entry_zone": plan.get("entry_zone"),
        "raw_entry_model": model,
        "data_readiness": context.get("data_readiness") if isinstance(context.get("data_readiness"), dict) else {},
        "market_day_regime": context.get("market_day_regime") if isinstance(context.get("market_day_regime"), dict) else {},
        "risk_flags": risk.get("flags") if isinstance(risk.get("flags"), list) else [],
        "quote": quote,
    }
    return {
        "symbol": row["symbol"],
        "action": "BUY",
        "signal_type": "BUY",
        "status": "ACTIVE",
        "fresh_action": "BUY_NOW",
        "strategy": row["strategy"],
        "last_seen_at": row["ts"],
        "latest_price": price,
        "overall_score_pct": model.get("raw_score"),
        "overall_grade": model.get("grade"),
        "confluence": confluence.get("total") or (float(model.get("raw_score") or 0.0) / 4.0),
        "confidence": row["confidence"],
        "technical_score": row["technical_score"],
        "market_region": market,
        "market_day_regime": details["market_day_regime"],
        "data_readiness": details["data_readiness"],
        "details": details,
    }


def _ablation_rows(
    conn: sqlite3.Connection,
    candidates: Any,
    settings: Settings,
    ablate_gates: str,
) -> list[dict[str, Any]]:
    selected = {
        item.strip()
        for item in str(ablate_gates or "").split(",")
        if item.strip()
    }
    rows: list[dict[str, Any]] = []
    for item in candidates:
        gate = item.get("executable_gate") if isinstance(item.get("executable_gate"), dict) else {}
        primary = str(item.get("primary_blocker") or "unknown")
        if selected and primary not in selected:
            continue
        trade = _simulate(conn, item, settings)
        if not trade:
            continue
        model = item.get("model") if isinstance(item.get("model"), dict) else {}
        context = item.get("context") if isinstance(item.get("context"), dict) else {}
        suppression_classification, suppression_reason = _suppression_classification_details(primary, gate, trade)
        trade.update(
            {
                "primary_blocker": primary,
                "market_primary_blocker": f"{item.get('market')}::{primary}",
                "setup_family": str(model.get("setup_family") or "none"),
                "setup_family_primary_blocker": f"{model.get('setup_family') or 'none'}::{primary}",
                "classification_primary_blocker": f"{suppression_classification}::{primary}",
                "market_classification": f"{item.get('market')}::{suppression_classification}",
                "paper_probe_eligible": bool(gate.get("paper_probe_eligible")),
                "paper_probe_blockers": gate.get("paper_probe_blockers") or [],
                "paper_probe_hard_risk_flags": gate.get("paper_probe_hard_risk_flags") or [],
                "paper_probe_severe_risk_flags": gate.get("paper_probe_severe_risk_flags") or [],
                "suppression_classification": suppression_classification,
                "suppression_classification_reason": suppression_reason,
                "quote_source": _quote_source(context, model),
                "data_source": _data_source(context, model),
                "risk_flags": gate.get("risk_flags") or [],
                "stop_target_valid": _stop_target_valid(model, float(item.get("price") or 0.0)),
                "replay_regime_inferred": bool(item.get("replay_regime_inferred")),
                "strategy": item.get("strategy"),
            }
        )
        rows.append(trade)
    return rows


def _suppression_classification(primary: str, gate: dict[str, Any], trade: dict[str, Any]) -> str:
    return _suppression_classification_details(primary, gate, trade)[0]


def _suppression_classification_details(primary: str, gate: dict[str, Any], trade: dict[str, Any]) -> tuple[str, str]:
    if primary == "EXECUTABLE":
        return "executable_survivor", "strict_executable_contract_passed"
    if gate.get("paper_probe_eligible"):
        return "candidate_false_negative", "passes_paper_probe_recovery_contract"
    hard_reasons = {
        "severe_risk_flags_present",
        "auto_follow_severe_risk_flags",
        "market_closed_live_buy_blocked",
        "stale_market_data",
        "moneycontrol_prior_session_data",
        "auto_follow_stop_missing_or_invalid",
        "auto_follow_target_missing",
        "auto_follow_reward_risk_below_minimum",
        "auto_follow_strategy_not_profitability_approved",
    }
    blockers = {str(value or "") for value in gate.get("paper_probe_blockers") or []}
    hard_probe_blockers = blockers & {
        "paper_probe_hard_risk_flags_present",
        "severe_risk_flags_present",
    }
    if primary in hard_reasons:
        return "correctly_blocked", f"hard_primary_blocker:{primary}"
    if hard_probe_blockers:
        return "correctly_blocked", f"hard_probe_blocker:{','.join(sorted(hard_probe_blockers))}"
    if trade.get("exit_reason") == "no_future_candles":
        return "ambiguous", "future_outcome_missing"
    if trade.get("exit_reason") == "stop" or float(trade.get("net_pct") or 0.0) < 0.0:
        return "correctly_blocked", "simulated_outcome_was_negative_or_stop"
    context_probe_blockers = blockers & {
        "paper_probe_requires_realtime_us_quote",
        "paper_probe_no_live_regime",
        "paper_probe_regime_not_supportive",
        "paper_probe_regime_momentum_not_allowed",
    }
    quality_probe_blockers = blockers & {
        "paper_probe_score_below_minimum",
        "paper_probe_confluence_below_minimum",
        "paper_probe_confidence_below_minimum",
        "paper_probe_technical_score_below_floor",
        "auto_follow_score_below_strict_minimum",
        "auto_follow_confluence_below_strict_minimum",
    }
    if context_probe_blockers and not quality_probe_blockers:
        return "context_dependent_blocked", f"needs_live_context:{','.join(sorted(context_probe_blockers))}"
    if context_probe_blockers:
        return "ambiguous", f"mixed_context_and_quality_blockers:{','.join(sorted(context_probe_blockers | quality_probe_blockers))}"
    if quality_probe_blockers:
        return "ambiguous", f"quality_probe_blockers:{','.join(sorted(quality_probe_blockers))}"
    return "ambiguous", "not_enough_evidence_to_reclassify"


def _quote_source(context: dict[str, Any], model: dict[str, Any]) -> str:
    values: list[str] = []
    quote = context.get("quote") if isinstance(context.get("quote"), dict) else {}
    if quote.get("source"):
        values.append(str(quote.get("source")))
    inputs = model.get("inputs") if isinstance(model.get("inputs"), dict) else {}
    if inputs.get("quote_source"):
        values.append(str(inputs.get("quote_source")))
    readiness = context.get("data_readiness") if isinstance(context.get("data_readiness"), dict) else {}
    sources = readiness.get("sources") if isinstance(readiness.get("sources"), dict) else {}
    if sources.get("quote"):
        values.append(str(sources.get("quote")))
    return " ".join(dict.fromkeys(values))


def _data_source(context: dict[str, Any], model: dict[str, Any]) -> str:
    values = [_quote_source(context, model)]
    readiness = context.get("data_readiness") if isinstance(context.get("data_readiness"), dict) else {}
    sources = readiness.get("sources") if isinstance(readiness.get("sources"), dict) else {}
    values.extend(str(value) for value in sources.values() if value)
    return " ".join(dict.fromkeys(value for value in values if value))


def _stop_target_valid(model: dict[str, Any], entry: float) -> bool:
    plan = model.get("trade_plan") if isinstance(model.get("trade_plan"), dict) else {}
    stop = plan.get("stop_loss")
    targets = plan.get("targets") if isinstance(plan.get("targets"), list) else []
    target = (targets[0] or {}).get("price") if targets else None
    try:
        return bool(float(stop) < entry < float(target))
    except (TypeError, ValueError):
        return False


def _count_by(rows: list[dict[str, Any]], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        value = str(row.get(key) or "?")
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: item[1], reverse=True))


def _count_by_filtered(rows: list[dict[str, Any]], key: str, filter_key: str, filter_value: str) -> dict[str, int]:
    return _count_by(
        [
            row
            for row in rows
            if str(row.get(filter_key) or "") == filter_value
        ],
        key,
    )


def _paper_probe_blocker_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        for blocker in row.get("paper_probe_blockers") or []:
            value = str(blocker or "").strip()
            if value:
                counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: item[1], reverse=True))


def _paper_probe_blocker_counts_by(rows: list[dict[str, Any]], key: str) -> dict[str, dict[str, int]]:
    grouped: dict[str, dict[str, int]] = {}
    for row in rows:
        group = str(row.get(key) or "?")
        counts = grouped.setdefault(group, {})
        for blocker in row.get("paper_probe_blockers") or []:
            value = str(blocker or "").strip()
            if value:
                counts[value] = counts.get(value, 0) + 1
    return {
        group: dict(sorted(counts.items(), key=lambda item: item[1], reverse=True))
        for group, counts in sorted(grouped.items())
    }


def _write_ablation_csv(path: str, rows: list[dict[str, Any]]) -> None:
    target = Path(path).expanduser()
    fields = [
        "symbol",
        "market",
        "entry_day",
        "setup_family",
        "primary_blocker",
        "suppression_classification",
        "suppression_classification_reason",
        "classification_primary_blocker",
        "market_classification",
        "paper_probe_eligible",
        "paper_probe_blockers",
        "paper_probe_hard_risk_flags",
        "paper_probe_severe_risk_flags",
        "quote_source",
        "data_source",
        "stop_target_valid",
        "replay_regime_inferred",
        "exit_reason",
        "net_pct",
        "net_pnl",
        "risk_flags",
    ]
    with target.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    key: json.dumps(row.get(key), sort_keys=True) if isinstance(row.get(key), (list, dict)) else row.get(key)
                    for key in fields
                }
            )
if __name__ == "__main__":
    main()
