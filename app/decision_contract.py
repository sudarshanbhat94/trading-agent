from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Iterable


def json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        parsed = json.loads(str(value))
    except (TypeError, json.JSONDecodeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed == parsed else None


def percent_score(value: Any) -> float | None:
    parsed = optional_float(value)
    if parsed is None:
        return None
    if 0 < parsed <= 1:
        parsed *= 100
    if parsed < 0:
        return None
    return round(max(0.0, min(100.0, parsed)), 1)


def combined_to_percent(value: Any) -> float | None:
    parsed = optional_float(value)
    if parsed is None:
        return None
    if -1.0 <= parsed <= 1.0:
        return round((parsed + 1.0) * 50.0, 1)
    return percent_score(parsed)


def first_score(*values: Any) -> float | None:
    for value in values:
        score = percent_score(value)
        if score is not None:
            return score
    return None


def _dict_at(source: Any, *keys: str) -> dict[str, Any]:
    current = source
    for key in keys:
        if not isinstance(current, dict):
            return {}
        current = current.get(key)
    return current if isinstance(current, dict) else {}


def _value_at(source: Any, *keys: str) -> Any:
    current = source
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _audit_from_row(row: dict[str, Any]) -> dict[str, Any]:
    details = row.get("details")
    if isinstance(details, dict):
        return details
    return json_object(row.get("details_json"))


def decision_rank_fields(row: dict[str, Any]) -> dict[str, Any]:
    audit = _audit_from_row(row)
    context = _dict_at(audit, "context")
    score_breakdown = _dict_at(audit, "score_breakdown")
    risk_gates = _dict_at(audit, "risk_gates")
    gate_context = _dict_at(risk_gates, "decision_gate_context")
    system_gate = (
        _dict_at(audit, "system_gate_audit")
        or _dict_at(context, "system_gate_audit")
        or _dict_at(risk_gates, "system_gate_audit")
        or _dict_at(gate_context, "system_gate_audit")
    )
    full_spectrum = _dict_at(context, "full_spectrum_analysis")
    signal_plan = _dict_at(full_spectrum, "signal_plan")
    scorecard = _dict_at(full_spectrum, "institutional_scorecard")

    overall_score = first_score(
        row.get("overall_score_pct"),
        audit.get("overall_score_pct"),
        system_gate.get("overall_score_pct"),
        signal_plan.get("overall_score_pct"),
        scorecard.get("total_score"),
    )
    deterministic_score = first_score(
        row.get("score_percent"),
        score_breakdown.get("score_percent"),
        _value_at(context, "score_breakdown", "score_percent"),
    )
    combined_score = combined_to_percent(score_breakdown.get("combined"))
    if combined_score is None:
        combined_score = combined_to_percent(row.get("combined_score"))
    confidence = percent_score(row.get("confidence"))

    ordered_candidates = [
        ("overall quality", overall_score),
        ("deterministic setup", deterministic_score),
        ("combined signal", combined_score),
        ("confidence fallback", confidence),
    ]
    source = "unscored"
    rank_score = 0.0
    for candidate_source, candidate_score in ordered_candidates:
        if candidate_score is not None:
            source = candidate_source
            rank_score = candidate_score
            break

    action = str(row.get("action") or audit.get("final_action") or "HOLD").upper()
    hard_blocked = bool(system_gate.get("hard_blocked"))
    failed_gates = gate_context.get("failed_gates") or []
    if action == "SELL":
        bucket = "risk_exit"
    elif action == "BUY" and rank_score >= 75 and not hard_blocked:
        bucket = "high_opportunity"
    elif action == "BUY" and not hard_blocked:
        bucket = "opportunity"
    elif hard_blocked or failed_gates:
        bucket = "blocked"
    else:
        bucket = "monitor"

    data_gaps = score_breakdown.get("data_gaps") or []
    if not isinstance(data_gaps, list):
        data_gaps = []
    rank_reason = source
    if bucket == "blocked":
        rank_reason = "blocked by gates"
    elif bucket == "risk_exit":
        rank_reason = "risk/exit decision"

    return {
        "rank_score": round(rank_score, 1),
        "rank_score_source": source,
        "rank_reason": rank_reason,
        "rank_bucket": bucket,
        "score_percent": deterministic_score,
        "overall_score_pct": overall_score,
        "confidence_pct": confidence,
        "combined_score_pct": combined_score,
        "data_gap_count": len(data_gaps),
    }


def annotate_decision_row(row: dict[str, Any]) -> dict[str, Any]:
    enriched = dict(row)
    enriched.update(decision_rank_fields(enriched))
    return enriched


def _timestamp_sort_value(row: dict[str, Any]) -> float:
    raw = str(row.get("ts") or row.get("asof") or "")
    if not raw:
        return 0.0
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _action_priority(row: dict[str, Any]) -> int:
    action = str(row.get("action") or "").upper()
    if action == "BUY":
        return 3
    if action == "SELL":
        return 2
    return 1


def ranked_decision_rows(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    annotated = [annotate_decision_row(dict(row)) for row in rows]
    return sorted(
        annotated,
        key=lambda row: (
            float(row.get("rank_score") or 0.0),
            _action_priority(row),
            _timestamp_sort_value(row),
            int(row.get("id") or 0),
        ),
        reverse=True,
    )


def current_decision_rows(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return one latest decision per symbol, then rank those current rows."""

    latest_by_symbol: dict[str, dict[str, Any]] = {}
    for row in rows:
        annotated = annotate_decision_row(dict(row))
        symbol = str(annotated.get("symbol") or "").upper()
        if not symbol:
            continue
        current = latest_by_symbol.get(symbol)
        if current is None:
            latest_by_symbol[symbol] = annotated
            continue
        candidate_key = (_timestamp_sort_value(annotated), int(annotated.get("id") or 0))
        current_key = (_timestamp_sort_value(current), int(current.get("id") or 0))
        if candidate_key > current_key:
            latest_by_symbol[symbol] = annotated
    return ranked_decision_rows(latest_by_symbol.values())


def normalize_trade_targets(targets: Any) -> list[dict[str, Any]]:
    if not isinstance(targets, list):
        return []
    normalized: list[dict[str, Any]] = []
    for index, target in enumerate(targets[:3], start=1):
        if isinstance(target, dict):
            payload = dict(target)
            price = optional_float(payload.get("price"))
            label = str(payload.get("label") or f"T{index}").upper()
        else:
            payload = {}
            price = optional_float(target)
            label = f"T{index}"
        if price is None or price <= 0:
            continue
        payload["label"] = label
        payload["price"] = round(price, 4)
        normalized.append(payload)

    previous_price: float | None = None
    for index, target in enumerate(normalized):
        price = optional_float(target.get("price"))
        if price is None:
            continue
        if previous_price is not None and price <= previous_price:
            min_gap = max(previous_price * 0.01, 0.01)
            original_price = price
            price = round(previous_price + min_gap, 4)
            target["price"] = price
            target["structure_reference"] = target.get("structure_reference", original_price)
            target["note"] = target.get("note") or "normalized so target ladder remains sequential"
            target["basis"] = target.get("basis") or "spacing_adjusted"
        target["label"] = str(target.get("label") or f"T{index + 1}").upper()
        previous_price = price
    return normalized
