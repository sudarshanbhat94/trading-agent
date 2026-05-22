from __future__ import annotations

from typing import Any


ACTIONABLE_OPPORTUNITY_STATES = {
    "BUY_NOW",
    "BUY_CANDIDATE",
    "PULLBACK_BUY_ZONE",
    "BREAKOUT_CONFIRMATION_NEEDED",
    "ACTIONABLE_WATCH",
}


def opportunity_state_from_signal_details(details: dict[str, Any]) -> dict[str, Any]:
    """Return product-facing opportunity state copy from a signal details payload."""

    details = details if isinstance(details, dict) else {}
    quality_gate = details.get("quality_gate") if isinstance(details.get("quality_gate"), dict) else {}
    return _build_opportunity_state(
        action=str(details.get("action") or "").upper(),
        quality_passed=bool(quality_gate.get("passed")),
        overall_score=_number(details.get("overall_score_pct")),
        overall_grade=str(details.get("overall_grade") or "").upper(),
        confluence=_number(details.get("confluence")),
        data_readiness=details.get("data_readiness") if isinstance(details.get("data_readiness"), dict) else {},
        entry=details.get("entry_quality") if isinstance(details.get("entry_quality"), dict) else {},
        breakout=details.get("breakout_quality") if isinstance(details.get("breakout_quality"), dict) else {},
        strategy_logic=details.get("strategy_logic_filters") if isinstance(details.get("strategy_logic_filters"), dict) else {},
        trade_plan={
            "entry_zone": details.get("entry_zone"),
            "stop_loss": details.get("stop_loss"),
            "targets": details.get("targets"),
        },
        failed_gates=details.get("failed_gates") if isinstance(details.get("failed_gates"), list) else [],
        hard_blocks=details.get("hard_blocks") if isinstance(details.get("hard_blocks"), list) else [],
        active_flags=details.get("active_flags") if isinstance(details.get("active_flags"), list) else [],
        risk_flags=details.get("risk_flags") if isinstance(details.get("risk_flags"), list) else [],
    )


def opportunity_state_from_decision_audit(audit: dict[str, Any], row: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return the same product-facing opportunity state from a decision audit."""

    audit = audit if isinstance(audit, dict) else {}
    row = row if isinstance(row, dict) else {}
    context = audit.get("context") if isinstance(audit.get("context"), dict) else {}
    context_summary = audit.get("context_summary") if isinstance(audit.get("context_summary"), dict) else {}
    full = _first_dict(context.get("full_spectrum_analysis"), context_summary.get("full_spectrum_summary"))
    risk_gates = audit.get("risk_gates") if isinstance(audit.get("risk_gates"), dict) else {}
    decision_gate = _first_dict(
        context.get("decision_gate_context"),
        risk_gates.get("decision_gate_context"),
    )
    system_audit = _first_dict(
        audit.get("system_gate_audit"),
        context.get("system_gate_audit"),
        risk_gates.get("system_gate_audit"),
        decision_gate.get("system_gate_audit"),
    )
    score_breakdown = audit.get("score_breakdown") if isinstance(audit.get("score_breakdown"), dict) else {}
    confluence = full.get("confluence_score") if isinstance(full.get("confluence_score"), dict) else {}
    trade_plan = full.get("trade_plan") if isinstance(full.get("trade_plan"), dict) else {}
    risk = full.get("risk_overrides") if isinstance(full.get("risk_overrides"), dict) else {}

    overall_score = _number(
        row.get("overall_score_pct"),
        audit.get("overall_score_pct"),
        system_audit.get("overall_score_pct"),
        score_breakdown.get("score_percent"),
    )
    action = str(row.get("action") or audit.get("final_action") or "").upper()
    quality_passed = (
        action == "BUY"
        and not bool(system_audit.get("hard_blocked"))
        and overall_score is not None
        and overall_score >= 70
        and str(system_audit.get("overall_grade") or audit.get("overall_grade") or "").upper() in {"A", "B"}
        and (_number(confluence.get("total")) or 0.0) >= 18
    )

    return _build_opportunity_state(
        action=action,
        quality_passed=quality_passed,
        overall_score=overall_score,
        overall_grade=str(system_audit.get("overall_grade") or audit.get("overall_grade") or "").upper(),
        confluence=_number(confluence.get("total")),
        data_readiness=_first_dict(
            context.get("data_readiness"),
            audit.get("data_readiness"),
            system_audit.get("data_readiness"),
            risk_gates.get("data_readiness"),
        ),
        entry=full.get("entry_quality") if isinstance(full.get("entry_quality"), dict) else {},
        breakout=full.get("breakout_quality") if isinstance(full.get("breakout_quality"), dict) else {},
        strategy_logic=full.get("strategy_logic_filters") if isinstance(full.get("strategy_logic_filters"), dict) else {},
        trade_plan=trade_plan,
        failed_gates=decision_gate.get("failed_gates") if isinstance(decision_gate.get("failed_gates"), list) else [],
        hard_blocks=system_audit.get("hard_blocks") if isinstance(system_audit.get("hard_blocks"), list) else [],
        active_flags=system_audit.get("active_flags") if isinstance(system_audit.get("active_flags"), list) else [],
        risk_flags=risk.get("flags") if isinstance(risk.get("flags"), list) else [],
    )


def is_signal_candidate_state(payload: dict[str, Any]) -> bool:
    if not isinstance(payload, dict):
        return False
    return bool(payload.get("publish_as_watch")) or str(payload.get("state") or "").upper() in ACTIONABLE_OPPORTUNITY_STATES


def _build_opportunity_state(
    *,
    action: str,
    quality_passed: bool,
    overall_score: float | None,
    overall_grade: str,
    confluence: float | None,
    data_readiness: dict[str, Any],
    entry: dict[str, Any],
    breakout: dict[str, Any],
    strategy_logic: dict[str, Any],
    trade_plan: dict[str, Any],
    failed_gates: list[Any],
    hard_blocks: list[Any],
    active_flags: list[Any],
    risk_flags: list[Any],
) -> dict[str, Any]:
    flags = _collect_flags(active_flags, hard_blocks, risk_flags)
    failed_reasons = _collect_failed_reasons(failed_gates)
    missing_data = _missing_data_labels(data_readiness)
    confluence_value = confluence or 0.0
    score_value = overall_score or 0.0

    extended = _entry_is_extended(entry, flags, failed_reasons)
    breakout_needs_confirmation = _breakout_needs_confirmation(breakout, strategy_logic, flags, failed_reasons)
    repeated_failed_breakouts = "REPEATED_FAILED_BREAKOUTS" in flags or any("failed_breakout" in item for item in failed_reasons)
    data_missing = (action == "BUY" and not data_readiness) or (
        bool(data_readiness) and data_readiness.get("trade_decision_ready") is not True
    )
    performance_warning = any("performance" in item for item in failed_reasons)
    hard_risk_block = _hard_risk_block(flags, failed_reasons)
    grade_allows_candidate = overall_grade not in {"D", "F"} or not overall_grade

    if action in {"SELL", "EXIT"}:
        state = "EXIT"
    elif action == "BUY" and quality_passed:
        state = "BUY_NOW"
    elif data_missing:
        state = "DATA_NEEDED"
    elif extended and confluence_value >= 16 and score_value >= 50:
        state = "PULLBACK_BUY_ZONE"
    elif (breakout_needs_confirmation or repeated_failed_breakouts) and confluence_value >= 14:
        state = "BREAKOUT_CONFIRMATION_NEEDED"
    elif hard_risk_block:
        state = "BLOCKED"
    elif confluence_value >= 18 and score_value >= 65 and grade_allows_candidate:
        state = "BUY_CANDIDATE"
    elif confluence_value >= 16 and score_value >= 55:
        state = "ACTIONABLE_WATCH"
    elif confluence_value >= 12 or score_value >= 50:
        state = "MONITOR"
    else:
        state = "BLOCKED"

    copy = _state_copy(state)
    reasons = _reason_list(
        state=state,
        score=overall_score,
        grade=overall_grade,
        confluence=confluence,
        missing_data=missing_data,
        entry=entry,
        breakout=breakout,
        performance_warning=performance_warning,
        failed_reasons=failed_reasons,
        flags=flags,
    )
    summary = _summary_for_state(state, reasons, copy["summary"])
    next_step = _next_step_for_state(state, trade_plan, missing_data)
    publish_as_watch = state in ACTIONABLE_OPPORTUNITY_STATES or (
        state == "DATA_NEEDED" and confluence_value >= 16 and score_value >= 65
    )

    return {
        "state": state,
        "label": copy["label"],
        "summary": summary,
        "next_step": next_step,
        "reasons": reasons,
        "publish_as_watch": publish_as_watch,
        "term_explanations": _term_explanations(state),
    }


def _state_copy(state: str) -> dict[str, str]:
    copy = {
        "BUY_NOW": {
            "label": "Ready to buy",
            "summary": "This passed the fresh-entry checks for score, grade, data, confirmation, and risk.",
        },
        "BUY_CANDIDATE": {
            "label": "Buy candidate",
            "summary": "Close to actionable, but one remaining confirmation or score/risk check is still missing.",
        },
        "PULLBACK_BUY_ZONE": {
            "label": "Wait for pullback",
            "summary": "The setup is interesting, but the current price is stretched from the ideal entry area.",
        },
        "BREAKOUT_CONFIRMATION_NEEDED": {
            "label": "Needs breakout confirmation",
            "summary": "The setup has potential, but breakout strength or volume confirmation is not reliable yet.",
        },
        "ACTIONABLE_WATCH": {
            "label": "Strong watchlist setup",
            "summary": "The stock has enough evidence to watch closely, but it has not passed every fresh-entry rule.",
        },
        "DATA_NEEDED": {
            "label": "Missing market evidence",
            "summary": "The setup cannot become trade-ready until required market data is available.",
        },
        "EXIT": {
            "label": "Exit review",
            "summary": "The latest decision is about reducing or closing risk, not opening a new trade.",
        },
        "MONITOR": {
            "label": "Monitor only",
            "summary": "There is not enough evidence for a fresh trade yet.",
        },
        "BLOCKED": {
            "label": "Avoid for now",
            "summary": "One or more risk or quality checks blocks a fresh BUY.",
        },
    }
    return copy.get(state, copy["MONITOR"])


def _summary_for_state(state: str, reasons: list[str], fallback: str) -> str:
    if state == "PULLBACK_BUY_ZONE":
        return "Good setup, wrong price. Wait for the stock to come back near the entry zone before treating it as actionable."
    if state == "BREAKOUT_CONFIRMATION_NEEDED":
        return "Potential breakout setup, but it needs stronger volume and follow-through before it deserves a BUY."
    if state == "ACTIONABLE_WATCH":
        return "Worth watching closely, but still one confirmation short of a fresh BUY."
    if state == "BUY_CANDIDATE":
        return "Close to actionable, but one final confirmation or quality check still needs to clear."
    if state == "DATA_NEEDED":
        return "Analysis found a possible setup, but required market evidence is missing for a trade-grade decision."
    if state == "BUY_NOW":
        return "Fresh BUY checks are clear: score, grade, confirmation, data, and risk all passed."
    return reasons[0] if reasons else fallback


def _next_step_for_state(state: str, trade_plan: dict[str, Any], missing_data: list[str]) -> str:
    entry_zone = trade_plan.get("entry_zone")
    if state == "BUY_NOW":
        return "Use the entry zone, stop, and targets shown below; position sizing still controls risk."
    if state == "PULLBACK_BUY_ZONE":
        if isinstance(entry_zone, list) and entry_zone:
            return "Wait for price to return to the displayed entry zone, then re-check volume and risk."
        return "Wait for price to cool off toward the breakout/pivot area, then re-check confirmation."
    if state == "BREAKOUT_CONFIRMATION_NEEDED":
        return "Wait for a strong close above the breakout level with better volume participation."
    if state == "ACTIONABLE_WATCH":
        return "Keep it on watch; it needs the remaining entry or risk checks to clear before any paper/live entry."
    if state == "BUY_CANDIDATE":
        return "Wait for the final check to clear; no paper/live entry until marked Ready to buy."
    if state == "DATA_NEEDED":
        if missing_data:
            return f"Connect or refresh this data before trading: {', '.join(missing_data[:4])}."
        return "Refresh the missing quote, candle, volume, or derivatives evidence before trading."
    if state == "EXIT":
        return "Review stop, target, and exit rules for any existing position."
    return "Do not enter now; wait for a cleaner scan."


def _reason_list(
    *,
    state: str,
    score: float | None,
    grade: str,
    confluence: float | None,
    missing_data: list[str],
    entry: dict[str, Any],
    breakout: dict[str, Any],
    performance_warning: bool,
    failed_reasons: set[str],
    flags: set[str],
) -> list[str]:
    reasons: list[str] = []
    if confluence is not None:
        reasons.append(f"Setup confirmation is {confluence:.0f}/26.")
    if score is not None:
        grade_text = f" with grade {grade}" if grade else ""
        reasons.append(f"Quality score is {score:.0f}%{grade_text}.")
    if state == "PULLBACK_BUY_ZONE":
        distance = _number(entry.get("distance_from_pivot_pct"))
        if distance is not None:
            reasons.append(f"Price is {abs(distance):.2f}% away from the pivot/ideal entry area.")
        else:
            reasons.append("Entry is stretched from the pivot/ideal buy area.")
    if state == "BREAKOUT_CONFIRMATION_NEEDED":
        if "LOW_VOLUME_RATIO" in flags or "WEAK_VOLUME_RATIO" in flags:
            reasons.append("Volume participation is still weak.")
        elif "SUSPECT_BREAKOUT_WITHOUT_VOLUME" in flags or "suspect_breakout_without_volume" in failed_reasons:
            reasons.append("Breakout has not been confirmed by volume.")
        else:
            reasons.append("Breakout follow-through is not clean enough yet.")
    if missing_data:
        reasons.append(f"Missing required evidence: {', '.join(missing_data[:4])}.")
    if performance_warning:
        reasons.append("Recent similar signals have underperformed, so the setup needs stronger proof.")
    if breakout.get("two_day_rule_failed"):
        reasons.append("The breakout failed the two-day confirmation check.")
    return _dedupe(reasons)[:5]


def _term_explanations(state: str) -> list[dict[str, str]]:
    terms = {
        "PULLBACK_BUY_ZONE": [
            {
                "term": "Pullback",
                "meaning": "A planned wait for price to come back closer to the ideal entry area instead of chasing an extended move.",
            },
            {
                "term": "Pivot",
                "meaning": "The price area where a breakout or trend turn should ideally be confirmed.",
            },
        ],
        "BREAKOUT_CONFIRMATION_NEEDED": [
            {
                "term": "Breakout confirmation",
                "meaning": "Price should hold above the breakout area and volume should show real participation.",
            },
            {
                "term": "Volume participation",
                "meaning": "More shares traded than normal, showing that the move has broader support.",
            },
        ],
        "ACTIONABLE_WATCH": [
            {
                "term": "Watchlist setup",
                "meaning": "A stock worth monitoring closely, but not a trade until all entry and risk checks pass.",
            }
        ],
        "BUY_CANDIDATE": [
            {
                "term": "Buy candidate",
                "meaning": "A strong setup that is near trade-ready, but still needs the final score, confirmation, or risk check to clear.",
            }
        ],
        "DATA_NEEDED": [
            {
                "term": "Market evidence",
                "meaning": "Fresh quote, candle, volume, delivery, options, or event data needed before the engine can trust a trade.",
            }
        ],
    }
    return terms.get(state, [])


def _entry_is_extended(entry: dict[str, Any], flags: set[str], failed_reasons: set[str]) -> bool:
    distance = _number(entry.get("distance_from_pivot_pct"))
    entry_grade = str(entry.get("entry_grade") or entry.get("grade") or "").upper()
    return (
        "PRICE_EXTENDED_FROM_PIVOT" in flags
        or entry_grade == "D"
        or (distance is not None and abs(distance) >= 5.0)
        or "extended_entry_no_new_longs" in failed_reasons
        or "price_extended_from_pivot" in failed_reasons
    )


def _breakout_needs_confirmation(
    breakout: dict[str, Any],
    strategy_logic: dict[str, Any],
    flags: set[str],
    failed_reasons: set[str],
) -> bool:
    breakout_volume = strategy_logic.get("breakout_volume") if isinstance(strategy_logic.get("breakout_volume"), dict) else {}
    quality = str(breakout.get("breakout_quality") or "").lower()
    volume_confirmed = bool(
        breakout.get("volume_expansion")
        or breakout.get("volume_confirmation")
        or breakout_volume.get("volume_confirmed")
        or breakout_volume.get("confirmed")
    )
    return (
        "SUSPECT_BREAKOUT_WITHOUT_VOLUME" in flags
        or "SUSPECT_BREAKOUT" in flags
        or "LOW_VOLUME_RATIO" in flags
        or "WEAK_VOLUME_RATIO" in flags
        or bool(breakout_volume.get("suspect_without_volume"))
        or (quality == "suspect" and not volume_confirmed)
        or "suspect_breakout_without_volume" in failed_reasons
        or "false_breakout_two_day_rule_failed" in failed_reasons
    )


def _hard_risk_block(flags: set[str], failed_reasons: set[str]) -> bool:
    hard_flags = {
        "DATA_READINESS_BLOCK",
        "DELIVERY_CONFLICT",
        "MTF_HARD_BLOCK",
        "EARNINGS_LOCKOUT",
        "EARNINGS_LOCKOUT_NOT_EVENT_DRIVEN",
        "POSITION_COUNT_LIMIT",
    }
    hard_reasons = {
        "risk_override_no_new_longs",
        "stage_analysis_not_stage2_markup",
        "delivery_distribution_no_new_longs",
        "options_max_pain_8pct_below_no_new_longs",
        "timeframe_alignment_conflict",
        "portfolio_concentration_correlation_too_high",
    }
    return bool(flags & hard_flags) or bool(failed_reasons & hard_reasons)


def _collect_flags(active_flags: list[Any], hard_blocks: list[Any], risk_flags: list[Any]) -> set[str]:
    flags = {str(flag or "").strip().upper() for flag in active_flags if str(flag or "").strip()}
    for block in hard_blocks:
        if isinstance(block, dict):
            value = block.get("flag") or block.get("code") or block.get("reason")
        else:
            value = block
        if str(value or "").strip():
            flags.add(str(value).strip().upper())
    for flag in risk_flags:
        if str(flag or "").strip():
            flags.add(str(flag).strip().upper())
    return flags


def _collect_failed_reasons(failed_gates: list[Any]) -> set[str]:
    reasons: set[str] = set()
    for gate in failed_gates:
        if isinstance(gate, dict):
            for key in ("reason", "gate", "name"):
                value = gate.get(key)
                if str(value or "").strip():
                    reasons.add(str(value).strip().lower())
        elif str(gate or "").strip():
            reasons.add(str(gate).strip().lower())
    return reasons


def _missing_data_labels(data_readiness: dict[str, Any]) -> list[str]:
    labels: list[str] = []
    for item in data_readiness.get("hard_gaps") or []:
        if isinstance(item, dict):
            labels.append(str(item.get("label") or item.get("key") or "").strip())
        else:
            labels.append(str(item or "").strip())
    for item in data_readiness.get("missing_data") or []:
        labels.append(str(item or "").strip())
    return [label for label in _dedupe(labels) if label]


def _first_dict(*values: Any) -> dict[str, Any]:
    for value in values:
        if isinstance(value, dict) and value:
            return value
    return {}


def _number(*values: Any) -> float | None:
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


def _dedupe(values: list[str]) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        cleaned = str(value or "").strip()
        key = cleaned.lower()
        if cleaned and key not in seen:
            output.append(cleaned)
            seen.add(key)
    return output
