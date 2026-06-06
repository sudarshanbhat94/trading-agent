from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from .signal_quality import (
    ACTIONABLE_MIN_CONFLUENCE,
    FRESH_BUY_ALLOWED_GRADES,
    FRESH_BUY_MIN_SCORE,
    FRESH_BUY_WINDOW_MINUTES,
    auto_follow_quality_gate,
    trade_readiness_gate,
)


CANONICAL_TRADE_CONTRACT_VERSION = "2026-06-02-reset-v1"


def canonical_trade_readiness_gate(item: dict[str, Any]) -> dict[str, Any]:
    """Single public entry gate used by strategy, UI, manual follow, and auto-follow.

    The low-level safety checks remain deliberately boring and deterministic,
    but every result is normalized here so downstream code sees one primary
    blocker, one contract version, and the same diagnostic shape.
    """

    normalized = _normalized_item(item)
    gate = trade_readiness_gate(normalized)
    return _canonicalize_gate(gate, normalized)


def canonical_auto_follow_quality_gate(item: dict[str, Any]) -> dict[str, Any]:
    """Single auto-follow gate layered on the canonical entry gate."""

    normalized = _normalized_item(item)
    gate = auto_follow_quality_gate(normalized)
    return _canonicalize_gate(gate, normalized, auto_follow=True)


def canonical_trade_contract(item: dict[str, Any]) -> dict[str, Any]:
    """Return the complete trade contract for one idea/decision row."""

    normalized = _normalized_item(item)
    quality_gate = canonical_trade_readiness_gate(normalized)
    signal_state = canonical_signal_state_payload(normalized, quality_gate=quality_gate)
    setup_bucket = canonical_setup_bucket_payload(normalized, signal_state, quality_gate)
    auto_follow_gate = canonical_auto_follow_quality_gate({**normalized, "fresh_action": signal_state.get("fresh_action")})
    primary_blocker = _primary_blocker_from_payload(quality_gate, signal_state, setup_bucket, auto_follow_gate)
    secondary_blockers = _secondary_blockers(normalized, quality_gate, exclude=primary_blocker)
    market_region = _upper(_field(normalized, "market_region")) or _market_region_from_sources(normalized)
    return {
        "version": CANONICAL_TRADE_CONTRACT_VERSION,
        "market_region": market_region,
        "symbol": _upper(normalized.get("symbol")),
        "quality_gate": quality_gate,
        "auto_follow_gate": auto_follow_gate,
        "signal_state": signal_state,
        "setup_bucket": setup_bucket,
        "primary_blocker": primary_blocker,
        "secondary_blockers": secondary_blockers,
        "fresh_action": signal_state.get("fresh_action"),
        "trade_state": signal_state.get("trade_state"),
        "setup_bucket_code": setup_bucket.get("bucket"),
        "paper_follow_eligible": bool(auto_follow_gate.get("passed")),
        "buy_now": signal_state.get("fresh_action") == "BUY_NOW" and bool(quality_gate.get("passed")),
    }


def canonical_signal_state_payload(
    item: dict[str, Any],
    *,
    quality_gate: dict[str, Any] | None = None,
) -> dict[str, Any]:
    details = _details(item)
    signal_type = _upper(item.get("signal_type") or item.get("suggestion"))
    status = _upper(item.get("status"))
    lifecycle = str(details.get("lifecycle_status") or item.get("lifecycle_status") or status or "active").lower()
    latest_action = _upper(details.get("latest_system_action") or details.get("action") or item.get("action") or signal_type or "HOLD")
    continuity = details.get("signal_continuity") if isinstance(details.get("signal_continuity"), dict) else {}
    current_return = _float(item.get("current_return_pct")) or 0.0
    latest_monitor_reason = details.get("latest_monitor_reason")
    original_buy_reason = details.get("original_buy_reason")
    why_changed = details.get("why_changed") if isinstance(details.get("why_changed"), dict) else {}
    if not why_changed:
        why_changed = _why_changed_payload(original_buy_reason, latest_monitor_reason, latest_action, continuity)
    drawdown_review = _drawdown_review_state(item, details)
    follow = item.get("user_follow") if isinstance(item.get("user_follow"), dict) else {}
    follow_status = _upper(follow.get("status") or item.get("follow_status"))
    followed_qty = _int(follow.get("qty") if follow else item.get("qty")) or 0
    followed_active = follow_status in {"ACTIVE", "LIVE_REQUESTED", "LIVE_EXIT_REQUESTED"} and followed_qty > 0
    follow_exited = follow_status in {"EXITED", "REJECTED", "CANCELLED", "CANCELED"}
    fresh_buy_recent = _recent_dt(item.get("last_seen_at"))
    readiness = quality_gate if isinstance(quality_gate, dict) else canonical_trade_readiness_gate(item)

    if status == "STOP_HIT" or lifecycle == "stopped":
        display_signal = "Stopped"
        fresh_action = "EXITED"
        trade_state = "STOP_HIT"
        class_name = "negative"
        reason = "Idea invalidated by stop."
    elif status == "EXIT_SIGNAL" or lifecycle == "exit_signal" or signal_type == "EXIT":
        display_signal = "Exit"
        fresh_action = "EXIT"
        trade_state = "EXIT_SIGNAL"
        class_name = "negative"
        reason = "Exit signal is active for this idea."
    elif status == "EXPIRED" or lifecycle == "expired":
        display_signal = "Expired"
        fresh_action = "EXPIRED"
        trade_state = "EXPIRED"
        class_name = "warning"
        reason = "Idea timeline has expired."
    elif signal_type == "BUY" and status in {"ACTIVE", "TARGET_1_HIT", "TARGET_2_HIT"}:
        duplicate_active = bool(continuity.get("duplicate_active_buy") or continuity.get("already_active_buy"))
        follow_mode = _upper(follow.get("mode") or item.get("mode"))
        if followed_active and follow_mode == "PAPER":
            display_signal = "Paper Entered"
            fresh_action = "NO_FRESH_ADD"
            reason = "Paper position is already entered; this idea is now being monitored."
        elif followed_active:
            display_signal = "Position Monitor"
            fresh_action = "NO_FRESH_ADD"
            reason = "Position is already active; this idea is now being monitored."
        elif follow_exited:
            display_signal = "No Fresh Add"
            fresh_action = "NO_FRESH_ADD"
            reason = "Your previous follow is closed; wait for a new fresh BUY before entering again."
        elif duplicate_active:
            display_signal = "Already Active"
            fresh_action = "NO_FRESH_ADD"
            reason = why_changed.get("summary") or "Already active; repeated BUY is monitor/no fresh add during cooldown."
        elif not readiness.get("passed"):
            display_signal = "Watch"
            fresh_action = "WATCH"
            reason = readiness.get("message") or "BUY thesis needs stronger quality/data before it is actionable."
        elif latest_action == "BUY" and not continuity and readiness.get("passed") and fresh_buy_recent:
            display_signal = "Actionable"
            fresh_action = "BUY_NOW"
            reason = "Fresh BUY passed the current entry and risk gates."
        elif latest_action == "BUY" and not continuity and readiness.get("passed"):
            display_signal = "No Fresh Add"
            fresh_action = "NO_FRESH_ADD"
            reason = "BUY is older than the fresh-entry window; keep monitoring unless a new BUY confirmation appears."
        elif drawdown_review["risk_review"]:
            display_signal = "Risk Review"
            fresh_action = "NO_FRESH_ADD"
            reason = drawdown_review["reason"] or "Active BUY is in adverse movement; review risk before adding."
        else:
            display_signal = "Position Monitor"
            fresh_action = "NO_FRESH_ADD"
            reason = why_changed.get("summary") or "Active BUY remains valid; latest cycle is monitor/no fresh add until a new BUY confirmation or exit."
        trade_state = {
            "Paper Entered": "PAPER_ENTERED",
            "Actionable": "ACTIONABLE",
            "No Fresh Add": "POSITION_MONITOR",
            "Already Active": "POSITION_MONITOR",
            "Position Monitor": "POSITION_MONITOR",
            "Watch": "WATCH",
        }.get(display_signal, "POSITION_MONITOR")
        class_name = "warning" if display_signal == "Watch" else "open"
        if drawdown_review["risk_review"]:
            trade_state = "RISK_REVIEW"
            class_name = "warning"
        if current_return < 0:
            reason = f"{reason} Current return is {current_return:.2f}% from the original signal."
    elif signal_type == "WATCH" or status == "WATCH" or lifecycle == "watch":
        display_signal = "Watch"
        fresh_action = "WATCH"
        trade_state = "WATCH"
        class_name = "warning"
        reason = "Setup is being monitored but is not a fresh BUY."
    elif followed_active and drawdown_review["risk_review"]:
        display_signal = "Risk Review"
        fresh_action = "NO_FRESH_ADD"
        trade_state = "RISK_REVIEW"
        class_name = "warning"
        reason = drawdown_review["reason"] or "Followed position needs risk review before adding."
        if current_return < 0:
            reason = f"{reason} Current return is {current_return:.2f}% from the original signal."
    elif followed_active:
        display_signal = "Position Monitor"
        fresh_action = "NO_FRESH_ADD"
        trade_state = "POSITION_MONITOR"
        class_name = "open"
        reason = why_changed.get("summary") or "Followed position is active; this is position monitoring, not a fresh entry."
    else:
        display_signal = "Monitor"
        fresh_action = "NO_TRADE"
        trade_state = "MONITORING"
        class_name = "neutral"
        reason = "No fresh trade action is active."

    if continuity:
        reason = str(why_changed.get("summary") or continuity.get("reason") or reason)
    primary_blocker = None if readiness.get("passed") else readiness.get("primary_blocker") or readiness.get("reason")
    return {
        "display_signal": display_signal,
        "fresh_action": fresh_action,
        "fresh_action_label": {
            "BUY_NOW": "Actionable",
            "NO_FRESH_ADD": "No Fresh Add",
            "WATCH": "Watch",
            "EXIT": "Exit",
            "EXITED": "Exited",
            "EXPIRED": "Expired",
            "NO_TRADE": "No Trade",
        }.get(fresh_action, display_signal),
        "trade_state": trade_state,
        "trade_state_label": display_signal,
        "class_name": class_name,
        "latest_system_action": latest_action,
        "display_reason": reason,
        "primary_blocker": primary_blocker,
        "original_buy_reason": original_buy_reason,
        "latest_monitor_reason": latest_monitor_reason,
        "why_changed": why_changed,
        "risk_review": drawdown_review,
        "canonical_version": CANONICAL_TRADE_CONTRACT_VERSION,
    }


def canonical_setup_bucket_payload(
    item: dict[str, Any],
    state: dict[str, Any],
    quality_gate: dict[str, Any] | None = None,
) -> dict[str, str]:
    details = _details(item)
    status = _upper(item.get("status"))
    signal_type = _upper(item.get("signal_type") or item.get("suggestion"))
    risk_flags = details.get("risk_flags") if isinstance(details.get("risk_flags"), list) else []
    classification = ""
    if isinstance(details.get("classification"), dict):
        classification = _upper(details["classification"].get("classification"))
    cap = _float(details.get("allocation_cap_multiplier"))
    readiness = quality_gate if isinstance(quality_gate, dict) else canonical_trade_readiness_gate(item)

    if status in {"STOP_HIT", "EXIT_SIGNAL", "EXPIRED", "TARGET_3_HIT"} or signal_type == "EXIT":
        return {"bucket": "AVOID", "label": "Avoid", "reason": "Idea is closed, invalidated, or in exit mode."}
    if state.get("trade_state") == "RISK_REVIEW":
        return {"bucket": "RISK_REVIEW", "label": "Risk Review", "reason": "Adverse move is outside normal noise; do not add without review."}
    if state.get("fresh_action") == "WATCH" or state.get("trade_state") == "WATCH":
        return {"bucket": "WATCH", "label": "Watch", "reason": "Setup is not actionable yet."}

    readiness_size = _float(readiness.get("size_multiplier")) or 1.0
    readiness_warnings = readiness.get("risk_warnings") if isinstance(readiness.get("risk_warnings"), list) else []
    full_size_ready = readiness_size >= 0.75 and not readiness_warnings
    if signal_type == "BUY" and state.get("fresh_action") == "BUY_NOW" and readiness.get("passed") and full_size_ready and not risk_flags and classification != "SPECULATIVE":
        return {"bucket": "ACTIONABLE", "label": "Actionable", "reason": "Fresh BUY with strong score, confluence, and no active risk flags."}
    if signal_type == "BUY":
        if not readiness.get("passed"):
            return {
                "bucket": "WATCH",
                "label": "Watch",
                "reason": readiness.get("message") or "BUY thesis is present, but it is not trade-ready.",
            }
        if readiness.get("passed") and full_size_ready and classification != "SPECULATIVE" and not risk_flags and not (cap is not None and cap <= 0.3):
            return {"bucket": "ACTIONABLE", "label": "Actionable", "reason": "BUY thesis is active and risk checks are acceptable."}
        return {
            "bucket": "SMALL_SIZE_ONLY",
            "label": "Small Size Only",
            "reason": readiness.get("message") or "BUY thesis exists, but risk/data/quality profile requires reduced size.",
        }
    if signal_type == "WATCH" or status == "WATCH":
        return {"bucket": "WATCH", "label": "Watch", "reason": "Setup is not actionable yet."}
    return {"bucket": "AVOID", "label": "Avoid", "reason": "No active trade setup is available."}


def _canonicalize_gate(
    gate: dict[str, Any],
    item: dict[str, Any],
    *,
    auto_follow: bool = False,
) -> dict[str, Any]:
    payload = dict(gate if isinstance(gate, dict) else {})
    payload.setdefault("passed", False)
    payload.setdefault("fresh_buy_allowed", bool(payload.get("passed")))
    payload.setdefault("min_score", FRESH_BUY_MIN_SCORE)
    payload.setdefault("min_confluence", ACTIONABLE_MIN_CONFLUENCE)
    payload.setdefault("allowed_grades", sorted(FRESH_BUY_ALLOWED_GRADES))
    payload["canonical_version"] = CANONICAL_TRADE_CONTRACT_VERSION
    payload["gate_scope"] = "auto_follow" if auto_follow else "fresh_entry"
    if payload.get("passed"):
        payload["primary_blocker"] = None
        payload["secondary_blockers"] = _secondary_blockers(item, payload)
    else:
        payload["primary_blocker"] = payload.get("primary_blocker") or payload.get("reason") or "trade_not_ready"
        payload["secondary_blockers"] = _secondary_blockers(item, payload, exclude=payload["primary_blocker"])
    return payload


def _normalized_item(item: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(item if isinstance(item, dict) else {})
    details = _details(normalized)
    if details:
        normalized["details"] = details
    for key in ("data_readiness", "quote", "market_region", "overall_score_pct", "overall_grade", "confluence"):
        if key not in normalized and key in details:
            normalized[key] = details[key]
    if "action" not in normalized and details.get("action"):
        normalized["action"] = details.get("action")
    _apply_live_probe_freshness_override(normalized)
    return normalized


def _active_monitor_follow_allowed(item: dict[str, Any], gate: dict[str, Any]) -> bool:
    if str(gate.get("reason") or "") not in {"not_actionable_fresh_state", "duplicate_active_buy_cooldown"}:
        return False
    signal_type = _upper(item.get("signal_type") or item.get("suggestion"))
    status = _upper(item.get("status"))
    if signal_type != "BUY" or status not in {"ACTIVE", "TARGET_1_HIT", "TARGET_2_HIT"}:
        return False
    follow = item.get("user_follow") if isinstance(item.get("user_follow"), dict) else {}
    follow_status = _upper(follow.get("status"))
    if follow_status in {"ACTIVE", "LIVE_REQUESTED", "LIVE_EXIT_REQUESTED"} and (_int(follow.get("qty")) or 0) > 0:
        return False
    details = _details(item)
    continuity = details.get("signal_continuity") if isinstance(details.get("signal_continuity"), dict) else {}
    if not (continuity.get("duplicate_active_buy") or continuity.get("already_active_buy")):
        return False
    return _recent_dt(item.get("last_seen_at"))


def _apply_live_probe_freshness_override(item: dict[str, Any]) -> None:
    details = _details(item)
    data_readiness = item.get("data_readiness") if isinstance(item.get("data_readiness"), dict) else details.get("data_readiness")
    scan = item.get("opportunity_scan") if isinstance(item.get("opportunity_scan"), dict) else details.get("opportunity_scan")
    quote = item.get("quote") if isinstance(item.get("quote"), dict) else details.get("quote")
    if not isinstance(data_readiness, dict) or not isinstance(scan, dict) or not isinstance(quote, dict):
        return
    if isinstance(data_readiness.get("fresh_market_data_gate"), dict):
        return
    probe = _ready_live_probe(details)
    if not probe:
        return
    data_quality = scan.get("data_quality") if isinstance(scan.get("data_quality"), dict) else {}
    missing = {str(value or "").strip().lower() for value in data_quality.get("missing") or [] if str(value or "").strip()}
    hard_gap_keys = {
        str(gap.get("key") or "").strip().lower()
        for gap in data_readiness.get("hard_gaps") or []
        if isinstance(gap, dict) and str(gap.get("key") or "").strip()
    }
    if missing - {"stale_intraday_candles"}:
        return
    if hard_gap_keys and hard_gap_keys - {"in_intraday_candles"}:
        return
    quote_source = str(quote.get("source") or "").lower()
    if not any(token in quote_source for token in ("upstox", "kite", "nubra")):
        return
    has_live_ohlcv = all((_float(quote.get(key)) or 0.0) > 0 for key in ("price", "open", "high", "low", "volume"))
    if not has_live_ohlcv:
        return
    updated = dict(data_readiness)
    updated["fresh_market_data_gate"] = {
        "passed": True,
        "reason": "live_quote_ready_intraday_reference_stale",
        "canonical_override": probe.get("data_quality_override") or probe.get("source"),
    }
    item["data_readiness"] = updated
    details["data_readiness"] = updated
    item["details"] = details


def _ready_live_probe(details: dict[str, Any]) -> dict[str, Any]:
    for payload in _probe_payloads(details):
        source = str(payload.get("source") or "").strip().lower()
        override = str(payload.get("data_quality_override") or "").strip().lower()
        if payload.get("ready") is True and (
            source in {"live_momentum_review", "live_quote_opportunity_scan"}
            or override in {
                "live_momentum_review_with_trade_ready_data",
                "live_quote_ohlcv_used_for_probe",
                "live_quote_intraday_candles_stale",
            }
        ):
            return payload
    return {}


def _probe_payloads(details: dict[str, Any]) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []

    def collect(value: Any) -> None:
        if isinstance(value, dict):
            payloads.append(value)

    collect(details.get("opportunity_probe"))
    gate_context = details.get("decision_gate_context") if isinstance(details.get("decision_gate_context"), dict) else {}
    collect(gate_context.get("opportunity_probe"))
    risk_gates = details.get("risk_gates") if isinstance(details.get("risk_gates"), dict) else {}
    collect(risk_gates.get("opportunity_probe"))
    nested = risk_gates.get("decision_gate_context") if isinstance(risk_gates.get("decision_gate_context"), dict) else {}
    collect(nested.get("opportunity_probe"))
    return payloads


def _primary_blocker_from_payload(
    quality_gate: dict[str, Any],
    signal_state: dict[str, Any],
    setup_bucket: dict[str, Any],
    auto_follow_gate: dict[str, Any],
) -> str | None:
    if not quality_gate.get("passed"):
        return str(quality_gate.get("primary_blocker") or quality_gate.get("reason") or "trade_not_ready")
    if signal_state.get("fresh_action") != "BUY_NOW":
        return str(signal_state.get("primary_blocker") or signal_state.get("fresh_action") or "not_actionable_now").lower()
    if not auto_follow_gate.get("passed"):
        return str(auto_follow_gate.get("primary_blocker") or auto_follow_gate.get("reason") or "auto_follow_not_ready")
    if setup_bucket.get("bucket") in {"WATCH", "AVOID", "RISK_REVIEW"}:
        return str(setup_bucket.get("bucket") or "").lower()
    return None


def _secondary_blockers(item: dict[str, Any], gate: dict[str, Any], *, exclude: Any = None) -> list[str]:
    blockers: list[str] = []
    for value in gate.get("risk_flags") or []:
        blockers.append(str(value or "").strip().lower())
    for value in gate.get("missing_data") or []:
        blockers.append(str(value or "").strip().lower())
    data_readiness = _field(item, "data_readiness")
    if isinstance(data_readiness, dict):
        for key in ("hard_gaps", "soft_gaps", "missing_data"):
            for gap in data_readiness.get(key) or []:
                if isinstance(gap, dict):
                    label = gap.get("key") or gap.get("label") or gap.get("reason")
                else:
                    label = gap
                if label:
                    blockers.append(str(label).strip().lower())
    reason = str(gate.get("reason") or "").strip().lower()
    if reason and not gate.get("passed"):
        blockers.append(reason)
    excluded = str(exclude or "").strip().lower()
    return [item for item in dict.fromkeys(blockers) if item and item != excluded]


def _details(item: dict[str, Any]) -> dict[str, Any]:
    details = item.get("details")
    return details if isinstance(details, dict) else {}


def _field(item: dict[str, Any], key: str) -> Any:
    if key in item:
        return item.get(key)
    details = _details(item)
    return details.get(key)


def _market_region_from_sources(item: dict[str, Any]) -> str:
    data_readiness = _field(item, "data_readiness")
    if isinstance(data_readiness, dict):
        region = _upper(data_readiness.get("market_region"))
        if region:
            return region
        sources = data_readiness.get("sources") if isinstance(data_readiness.get("sources"), dict) else {}
        quote_source = str(sources.get("quote") or "").lower()
        if any(token in quote_source for token in ("upstox", "kite", "nubra", "indstocks")):
            return "IN"
        if any(token in quote_source for token in ("alpaca", "polygon", "yahoo")):
            return "US"
    return ""


def _recent_dt(value: Any) -> bool:
    parsed = _parse_dt(value)
    if parsed is None:
        return True
    return datetime.now(timezone.utc) - parsed <= timedelta(minutes=FRESH_BUY_WINDOW_MINUTES)


def _parse_dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _drawdown_review_state(item: dict[str, Any], details: dict[str, Any]) -> dict[str, Any]:
    entry = _float(item.get("entry_price"))
    latest = _float(item.get("latest_price") or item.get("price"))
    stop = _float(details.get("stop_loss"))
    current_return = _float(item.get("current_return_pct")) or 0.0
    worst_return = _float(item.get("worst_return_pct")) or current_return
    near_stop = bool((details.get("drawdown_status") or {}).get("near_stop")) if isinstance(details.get("drawdown_status"), dict) else False
    risk_used_pct = 0.0
    if entry and latest and stop and entry > stop:
        risk_used_pct = max(min(((entry - latest) / (entry - stop)) * 100.0, 100.0), 0.0)
    review = near_stop or risk_used_pct >= 55.0 or current_return <= -2.0 or worst_return <= -3.0
    return {
        "risk_review": bool(review),
        "risk_used_pct": round(risk_used_pct, 2),
        "reason": (
            f"Adverse move needs review: return {current_return:.2f}%, worst {worst_return:.2f}%, risk used {risk_used_pct:.0f}%."
            if review
            else ""
        ),
    }


def _why_changed_payload(
    original_buy_reason: Any,
    latest_monitor_reason: Any,
    latest_action: str,
    continuity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    latest_action = _upper(latest_action) or "HOLD"
    original = _short_reason(original_buy_reason, 260)
    latest = _short_reason(latest_monitor_reason, 320)
    continuity = continuity if isinstance(continuity, dict) else {}
    if continuity.get("duplicate_active_buy") or continuity.get("already_active_buy"):
        summary = "Already active. Repeated BUY is treated as position monitoring, not a new entry."
    elif latest_action == "BUY":
        summary = "Fresh BUY is currently confirmed by the latest engine cycle."
    elif original and latest:
        summary = f"BUY preserved. Latest engine action {latest_action} because {latest}"
    elif original:
        summary = f"BUY preserved. Latest engine action {latest_action}; no fresh add until a new BUY confirmation or exit."
    elif latest:
        summary = f"Latest engine action {latest_action} because {latest}"
    else:
        summary = f"Latest engine action {latest_action}; no fresh entry is active."
    return {
        "preserved": bool(continuity),
        "latest_engine_action": latest_action,
        "summary": summary,
        "original_buy_reason": original,
        "latest_monitor_reason": latest,
    }


def _short_reason(value: Any, limit: int = 220) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: max(limit - 1, 0)].rstrip() + "..."


def _float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed == parsed else None


def _int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _upper(value: Any) -> str:
    return str(value or "").strip().upper()
