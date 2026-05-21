from __future__ import annotations

from typing import Any


FRESH_BUY_MIN_SCORE = 70.0
FRESH_BUY_ALLOWED_GRADES = {"A", "B"}
DUPLICATE_BUY_COOLDOWN_HOURS = 48
AUTO_FOLLOW_REENTRY_COOLDOWN_HOURS = 48
ACTIONABLE_MIN_CONFLUENCE = 18.0


def fresh_buy_quality_gate(item: dict[str, Any]) -> dict[str, Any]:
    """Strict Phase-1 gate for a fresh tradeable BUY idea."""

    return trade_readiness_gate(item)


def trade_readiness_gate(item: dict[str, Any]) -> dict[str, Any]:
    """Canonical gate for anything that wants to present or execute a fresh BUY.

    Strategy, UI, manual follow, and auto-follow paths should all use this
    result instead of inventing local score/grade thresholds.
    """

    details = _details(item)
    signal_type = _upper(item.get("signal_type") or item.get("suggestion"))
    status = _upper(item.get("status"))
    action = _upper(item.get("action") or details.get("action") or signal_type)
    if signal_type in {"WATCH", "NO_TRADE", "EXIT"} or status == "WATCH":
        return _blocked("not_fresh_buy_signal", "Only a fresh BUY idea can be traded.")
    if action != "BUY" and signal_type != "BUY":
        return _blocked("not_buy_action", "Latest engine action is not BUY.")

    hard_blocked = bool(item.get("hard_blocked") or details.get("hard_blocked"))
    hard_blocks = details.get("hard_blocks") if isinstance(details.get("hard_blocks"), list) else []
    phase3 = details.get("strategy_logic_filters") if isinstance(details.get("strategy_logic_filters"), dict) else {}
    phase3_blocks = phase3.get("hard_blocks") if isinstance(phase3.get("hard_blocks"), list) else []
    if hard_blocked or hard_blocks or phase3_blocks:
        return _blocked("hard_blocked", "System hard blocks are present.")

    score = _number(item.get("overall_score_pct"), details.get("overall_score_pct"))
    if score is None or score < FRESH_BUY_MIN_SCORE:
        return _blocked(
            "overall_score_below_70",
            f"Overall score must be at least {FRESH_BUY_MIN_SCORE:.0f}.",
            overall_score_pct=score,
        )

    grade = _upper(item.get("overall_grade") or details.get("overall_grade"))
    if grade not in FRESH_BUY_ALLOWED_GRADES:
        return _blocked("grade_not_a_or_b", "Fresh BUY requires grade A or B.", overall_grade=grade or None)

    confluence = _number(item.get("confluence"), details.get("confluence"))
    if confluence is not None and confluence < ACTIONABLE_MIN_CONFLUENCE:
        return _blocked(
            "confluence_below_actionable_minimum",
            f"Fresh BUY requires confluence of at least {ACTIONABLE_MIN_CONFLUENCE:.0f}.",
            confluence=confluence,
        )

    risk_flags = _risk_flags(item, details)
    breakout = _breakout_payload(details)
    suspect_breakout = (
        str(breakout.get("breakout_quality") or "").lower() == "suspect"
        or any("suspect_breakout" in flag for flag in risk_flags)
    )
    volume_confirmed = bool(
        breakout.get("volume_expansion")
        or breakout.get("volume_confirmation")
        or details.get("volume_confirmation")
    )
    if suspect_breakout and not volume_confirmed:
        return _blocked(
            "suspect_breakout_without_volume",
            "Suspect breakout requires volume confirmation before auto-entry.",
            risk_flags=risk_flags,
        )

    data_readiness = item.get("data_readiness") if isinstance(item.get("data_readiness"), dict) else details.get("data_readiness")
    if not isinstance(data_readiness, dict):
        return _blocked("data_readiness_missing", "Fresh BUY requires Phase-2 data readiness evidence from a fresh scan.")
    if data_readiness.get("trade_decision_ready") is not True:
        missing = _missing_data_labels(data_readiness)
        message = "Phase-2 data readiness is not complete for a fresh trade decision."
        if missing:
            message = f"Missing Phase-2 data: {', '.join(missing[:4])}."
        return _blocked(
            "data_readiness_not_trade_ready",
            message,
            data_readiness=data_readiness,
            missing_data=missing,
        )

    return {
        "passed": True,
        "fresh_buy_allowed": True,
        "reason": "fresh_buy_quality_passed",
        "overall_score_pct": score,
        "overall_grade": grade,
        "min_score": FRESH_BUY_MIN_SCORE,
        "min_confluence": ACTIONABLE_MIN_CONFLUENCE,
        "allowed_grades": sorted(FRESH_BUY_ALLOWED_GRADES),
        "risk_flags": risk_flags,
        "data_readiness": data_readiness,
    }


def auto_follow_quality_gate(item: dict[str, Any]) -> dict[str, Any]:
    gate = fresh_buy_quality_gate(item)
    if not gate.get("passed"):
        return gate
    fresh_action = _upper(item.get("fresh_action"))
    if fresh_action and fresh_action != "BUY_NOW":
        return _blocked(
            "not_actionable_fresh_state",
            "Auto-paper only follows ideas marked Actionable by the current tradeability state.",
            fresh_action=fresh_action,
        )
    if is_duplicate_active_buy_refresh(item):
        return _blocked(
            "duplicate_active_buy_cooldown",
            "BUY is already active; do not auto-follow repeated refreshes.",
            duplicate_active_buy=True,
        )
    return gate


def is_duplicate_active_buy_refresh(item: dict[str, Any]) -> bool:
    details = _details(item)
    continuity = details.get("signal_continuity")
    if not isinstance(continuity, dict):
        return False
    return bool(continuity.get("duplicate_active_buy") or continuity.get("already_active_buy"))


def quality_skip_payload(gate: dict[str, Any]) -> dict[str, Any]:
    return {
        "reason": "phase1_quality_gate",
        "quality_reason": gate.get("reason"),
        "quality_message": gate.get("message"),
        "overall_score_pct": gate.get("overall_score_pct"),
        "overall_grade": gate.get("overall_grade"),
        "min_score": gate.get("min_score", FRESH_BUY_MIN_SCORE),
        "min_confluence": gate.get("min_confluence", ACTIONABLE_MIN_CONFLUENCE),
        "allowed_grades": gate.get("allowed_grades", sorted(FRESH_BUY_ALLOWED_GRADES)),
        "risk_flags": gate.get("risk_flags", []),
        "missing_data": gate.get("missing_data", []),
        "fresh_action": gate.get("fresh_action"),
    }


def _blocked(reason: str, message: str, **extra: Any) -> dict[str, Any]:
    return {
        "passed": False,
        "fresh_buy_allowed": False,
        "reason": reason,
        "message": message,
        "min_score": FRESH_BUY_MIN_SCORE,
        "min_confluence": ACTIONABLE_MIN_CONFLUENCE,
        "allowed_grades": sorted(FRESH_BUY_ALLOWED_GRADES),
        **extra,
    }


def _details(item: dict[str, Any]) -> dict[str, Any]:
    details = item.get("details")
    return details if isinstance(details, dict) else {}


def _risk_flags(item: dict[str, Any], details: dict[str, Any]) -> list[str]:
    flags = item.get("risk_flags")
    if not isinstance(flags, list):
        flags = details.get("risk_flags")
    if not isinstance(flags, list):
        return []
    return [str(flag or "").strip().lower() for flag in flags if str(flag or "").strip()]


def _breakout_payload(details: dict[str, Any]) -> dict[str, Any]:
    for key in ("breakout_quality", "breakout"):
        payload = details.get(key)
        if isinstance(payload, dict):
            return payload
    return {}


def _number(*values: Any) -> float | None:
    for value in values:
        if value in (None, ""):
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _upper(value: Any) -> str:
    return str(value or "").strip().upper()


def _missing_data_labels(data_readiness: dict[str, Any]) -> list[str]:
    labels: list[str] = []
    for item in data_readiness.get("hard_gaps") or []:
        if isinstance(item, dict):
            labels.append(str(item.get("label") or item.get("key") or "").strip())
        else:
            labels.append(str(item or "").strip())
    for item in data_readiness.get("missing_data") or []:
        labels.append(str(item or "").strip())
    return [label for label in dict.fromkeys(labels) if label]
