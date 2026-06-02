from __future__ import annotations

from typing import Any


FRESH_BUY_MIN_SCORE = 70.0
FRESH_BUY_ALLOWED_GRADES = {"A", "B"}
OPPORTUNITY_PROBE_MIN_SCORE = 62.0
OPPORTUNITY_PROBE_ALLOWED_GRADES = {"A", "B", "C"}
OPPORTUNITY_PROBE_MIN_CONFLUENCE = 16.0
OPPORTUNITY_PROBE_SIZE_MULTIPLIER = 0.35
DUPLICATE_BUY_COOLDOWN_HOURS = 48
AUTO_FOLLOW_REENTRY_COOLDOWN_HOURS = 48
FRESH_BUY_WINDOW_MINUTES = 20
ACTIONABLE_MIN_CONFLUENCE = 18.0
CAUTION_STOP_RISK_PCT = 5.5
HARD_STOP_RISK_PCT = 9.0
CAUTION_T1_DISTANCE_PCT = 10.0
HARD_T1_DISTANCE_PCT = 18.0
MIN_SIZE_MULTIPLIER = 0.25
_WAIT_ONLY_TRADE_WINDOWS = {
    "confirm_before_entry",
    "not_ready",
    "wait_for_pullback",
    "watch_for_ignition",
    "watch_for_pullback",
    "watch_only",
}
_LIVE_OPPORTUNITY_SOURCES = {"live_momentum_review", "live_quote_opportunity_scan"}
_LIVE_OPPORTUNITY_SETUPS = {
    "opening_ignition",
    "intraday_momentum",
    "top_gainer_momentum",
    "market_action_momentum",
    "price_shocker_reversal_breakout",
}
_BREAKOUT_OPPORTUNITY_SETUPS = {
    "52_week_high_volume_breakout",
    "breakout_continuation",
    "near_breakout",
    "broker_re_rating_breakout",
    "earnings_beat_gap_and_go",
}


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

    hard_entry_veto = _entry_hard_veto(item, details)
    if hard_entry_veto:
        return hard_entry_veto

    hard_blocked = bool(item.get("hard_blocked") or details.get("hard_blocked"))
    hard_blocks = details.get("hard_blocks") if isinstance(details.get("hard_blocks"), list) else []
    phase3 = details.get("strategy_logic_filters") if isinstance(details.get("strategy_logic_filters"), dict) else {}
    phase3_blocks = phase3.get("hard_blocks") if isinstance(phase3.get("hard_blocks"), list) else []
    playbook_probe = _top_gainers_playbook_probe(item, details)
    btst_probe = _btst_buy_probe(item, details)
    if playbook_probe:
        hard_blocks = _filter_playbook_absorbable_blocks(hard_blocks, playbook_probe)
        phase3_blocks = _filter_playbook_absorbable_blocks(phase3_blocks, playbook_probe)
        hard_blocked = bool(hard_blocks or phase3_blocks)
    if hard_blocked or hard_blocks or phase3_blocks:
        return _blocked("hard_blocked", "System hard blocks are present.")

    opportunity_probe = bool(playbook_probe) or bool(btst_probe) or _opportunity_probe_ready(item, details)
    probe_score_floor = (
        float(playbook_probe.get("min_score") or OPPORTUNITY_PROBE_MIN_SCORE)
        if playbook_probe
        else float(btst_probe.get("min_score") or 70.0)
        if btst_probe
        else OPPORTUNITY_PROBE_MIN_SCORE
        if opportunity_probe
        else FRESH_BUY_MIN_SCORE
    )
    min_score = max(float(probe_score_floor), FRESH_BUY_MIN_SCORE)
    min_confluence = (
        0.0
        if playbook_probe
        else _opportunity_probe_min_confluence(item, details)
        if opportunity_probe
        else ACTIONABLE_MIN_CONFLUENCE
    )
    allowed_grades = FRESH_BUY_ALLOWED_GRADES

    tradeability_score = _number(item.get("overall_score_pct"), details.get("overall_score_pct"))
    setup_score = _number(details.get("setup_score_pct"))
    score = tradeability_score
    if playbook_probe and _number(playbook_probe.get("quant_score")) is not None:
        score = tradeability_score
    if btst_probe and _number(btst_probe.get("btst_score")) is not None:
        score = tradeability_score
    if score is None or score < min_score:
        return _blocked(
            "overall_score_below_70",
            f"Overall score must be at least {min_score:.0f}.",
            overall_score_pct=score,
            min_score=min_score,
            min_confluence=min_confluence,
            allowed_grades=sorted(allowed_grades),
        )

    grade = _upper(item.get("overall_grade") or details.get("overall_grade"))
    if grade not in allowed_grades:
        return _blocked(
            "grade_not_a_or_b",
            "Fresh BUY requires grade A or B.",
            overall_grade=grade or None,
            min_score=min_score,
            min_confluence=min_confluence,
            allowed_grades=sorted(allowed_grades),
        )

    confluence = _number(item.get("confluence"), details.get("confluence"))
    if confluence is not None and confluence < min_confluence:
        return _blocked(
            "confluence_below_actionable_minimum",
            f"Fresh BUY requires confluence of at least {min_confluence:.0f}.",
            confluence=confluence,
            min_score=min_score,
            min_confluence=min_confluence,
            allowed_grades=sorted(allowed_grades),
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
    data_readiness_override_mode = (
        _opportunity_probe_data_readiness_override(item, details, data_readiness)
        if opportunity_probe
        else ""
    )
    data_readiness_override = bool(data_readiness_override_mode)
    if data_readiness.get("trade_decision_ready") is not True and not data_readiness_override:
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

    size_multiplier = 1.0
    cautions: list[str] = []
    missing_data: list[str] = []
    if _missing_sentiment_news(data_readiness):
        size_multiplier = min(size_multiplier, 0.50)
        cautions.append("news/sentiment not refreshed; use reduced paper size")
        missing_data.append("sentiment_news")
    soft_missing = _soft_missing_data_labels(data_readiness)
    if soft_missing:
        size_multiplier = min(size_multiplier, 0.50 if len(soft_missing) >= 3 else 0.75)
        cautions.append("supporting market data has gaps; use reduced paper size")
        missing_data.extend(soft_missing)
    if btst_probe:
        size_multiplier = min(size_multiplier, float(btst_probe.get("size_multiplier") or 0.75))
        cautions.append("BTST overnight setup; buy only with guarded size and sell/trim tomorrow if follow-through fails")
    elif opportunity_probe:
        size_multiplier = min(size_multiplier, OPPORTUNITY_PROBE_SIZE_MULTIPLIER)
        cautions.append("opportunity scan setup; use probe size until confirmation matures")
    if data_readiness_override:
        size_multiplier = min(size_multiplier, OPPORTUNITY_PROBE_SIZE_MULTIPLIER)
        if data_readiness_override_mode == "us_yahoo_reference_reduced_size":
            cautions.append("US Yahoo reference mode; use reduced paper size and do not live-auto-execute")
            missing_data.append("us_realtime_quote")
        else:
            cautions.append("live quote is available but intraday candles are stale; use probe size only")
            missing_data.append("stale_intraday_candles")
    macro_event = details.get("macro_event_context") if isinstance(details.get("macro_event_context"), dict) else {}
    if not macro_event and isinstance(item.get("macro_event_context"), dict):
        macro_event = item["macro_event_context"]
    if macro_event.get("is_monthly_expiry_eve"):
        expiry_size = _number(macro_event.get("expiry_size_multiplier"))
        expiry_cap = min(expiry_size if expiry_size is not None else OPPORTUNITY_PROBE_SIZE_MULTIPLIER, OPPORTUNITY_PROBE_SIZE_MULTIPLIER)
        size_multiplier = min(size_multiplier, expiry_cap)
        cautions.append("monthly expiry eve; use probe size until expiry risk clears")

    severe_flags = _severe_risk_flags(
        risk_flags,
        opportunity_probe=opportunity_probe,
        playbook_entry_ok=bool(playbook_probe),
        playbook_market_region=_upper(playbook_probe.get("market_region")) if playbook_probe else "",
    )
    if severe_flags:
        return _blocked(
            "severe_risk_flags",
            "Fresh BUY has severe risk flags; track only until those risks clear.",
            risk_flags=risk_flags,
            severe_risk_flags=severe_flags,
        )
    if risk_flags:
        size_multiplier = min(size_multiplier, 0.35)
        cautions.append("risk flags active; use probe size")

    t1_gate, t1_multiplier, t1_caution = _target_one_gate(item, details, playbook_probe=bool(playbook_probe))
    if t1_gate:
        return t1_gate
    if t1_multiplier is not None:
        size_multiplier = min(size_multiplier, t1_multiplier)
    if t1_caution:
        cautions.append(t1_caution)

    stop_gate, stop_multiplier, stop_caution = _stop_risk_gate(item, details)
    if stop_gate:
        return stop_gate
    if stop_multiplier is not None:
        size_multiplier = min(size_multiplier, stop_multiplier)
    if stop_caution:
        cautions.append(stop_caution)

    return {
        "passed": True,
        "fresh_buy_allowed": True,
        "reason": "fresh_buy_quality_passed",
        "overall_score_pct": score,
        "tradeability_score_pct": tradeability_score,
        "setup_score_pct": setup_score,
        "overall_grade": grade,
        "min_score": min_score,
        "min_confluence": min_confluence,
        "allowed_grades": sorted(allowed_grades),
        "risk_flags": risk_flags,
        "risk_warnings": cautions,
        "missing_data": list(dict.fromkeys(missing_data)),
        "opportunity_probe": opportunity_probe,
        "size_multiplier": round(max(size_multiplier, MIN_SIZE_MULTIPLIER), 4),
        "data_readiness": data_readiness,
    }


def auto_follow_quality_gate(item: dict[str, Any]) -> dict[str, Any]:
    gate = fresh_buy_quality_gate(item)
    if not gate.get("passed"):
        return gate
    risk_flags = gate.get("risk_flags")
    details = _details(item)
    if not isinstance(risk_flags, list):
        risk_flags = _risk_flags(item, details)
    severe_flags = _severe_risk_flags(
        risk_flags,
        playbook_entry_ok=bool(_top_gainers_playbook_probe(item, details)),
        playbook_market_region=_upper((_top_gainers_playbook_probe(item, details) or {}).get("market_region")),
    )
    if severe_flags:
        return _blocked(
            "auto_follow_severe_risk_flags",
            "Auto-paper will not enter ideas that the safety manager would immediately exit.",
            risk_flags=risk_flags,
            severe_risk_flags=severe_flags,
        )
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


def active_follow_safety_gate(item: dict[str, Any]) -> dict[str, Any]:
    """Safety gate for already-followed paper/live positions.

    Fresh-entry quality gates decide whether to open or add. Once a position is
    followed, the system should exit only on explicit invalidation, hard risk,
    or a proper stop/exit signal.
    """

    details = _details(item)
    signal_type = _upper(item.get("signal_type") or item.get("suggestion"))
    status = _upper(item.get("status"))
    action = _upper(item.get("action") or details.get("action") or signal_type)
    if action in {"SELL", "EXIT"} or signal_type in {"EXIT", "NO_TRADE"}:
        return _blocked("active_follow_exit_signal", "Latest engine action is an exit/no-trade signal.")
    if status in {"STOP_HIT", "EXIT_SIGNAL", "EXPIRED", "TARGET_3_HIT", "REJECTED"}:
        return _blocked("active_follow_not_tradeable_state", "Followed position moved into a closed/exit lifecycle state.")
    if status == "WATCH" or signal_type == "WATCH":
        return {
            "passed": True,
            "fresh_buy_allowed": False,
            "reason": "active_follow_watch_state_hold",
            "risk_flags": _risk_flags(item, details),
            "risk_warnings": [
                "latest idea is watch-only; keep the existing follow managed by stop, target, and hard invalidation"
            ],
        }

    hard_blocked = bool(item.get("hard_blocked") or details.get("hard_blocked"))
    hard_blocks = details.get("hard_blocks") if isinstance(details.get("hard_blocks"), list) else []
    phase3 = details.get("strategy_logic_filters") if isinstance(details.get("strategy_logic_filters"), dict) else {}
    phase3_blocks = phase3.get("hard_blocks") if isinstance(phase3.get("hard_blocks"), list) else []
    if hard_blocked or hard_blocks or phase3_blocks:
        return _blocked("active_follow_hard_blocked", "Followed position has explicit hard invalidation.")

    risk_flags = _risk_flags(item, details)
    severe_flags = _severe_risk_flags(risk_flags)
    if severe_flags:
        return _blocked(
            "active_follow_severe_risk_flags",
            "Followed position has severe risk flags.",
            risk_flags=risk_flags,
            severe_risk_flags=severe_flags,
        )
    return {
        "passed": True,
        "fresh_buy_allowed": False,
        "reason": "active_follow_safety_passed",
        "risk_flags": risk_flags,
        "risk_warnings": ["fresh-entry score changes do not force exit; manage by stop, target, and invalidation"],
    }


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
        "risk_warnings": gate.get("risk_warnings", []),
        "missing_data": gate.get("missing_data", []),
        "size_multiplier": gate.get("size_multiplier"),
        "opportunity_probe": gate.get("opportunity_probe"),
        "fresh_action": gate.get("fresh_action"),
    }


def quality_size_multiplier(gate: dict[str, Any], *, default: float = 1.0) -> float:
    value = _number(gate.get("size_multiplier") if isinstance(gate, dict) else None)
    if value is None:
        value = default
    return max(min(float(value), 1.0), 0.10)


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


def _entry_hard_veto(item: dict[str, Any], details: dict[str, Any]) -> dict[str, Any] | None:
    scan = item.get("opportunity_scan") if isinstance(item.get("opportunity_scan"), dict) else details.get("opportunity_scan")
    scan = scan if isinstance(scan, dict) else {}
    label = _upper(
        item.get("label")
        or details.get("label")
        or details.get("classification_label")
        or scan.get("label")
        or scan.get("bucket")
    )
    bucket = _upper(scan.get("bucket") or details.get("setup_bucket") or item.get("setup_bucket"))
    setup = str(scan.get("setup") or details.get("setup") or details.get("strategy") or "").strip().lower()
    best_strategy = details.get("best_strategy") if isinstance(details.get("best_strategy"), dict) else {}
    best_strategy_name = str(best_strategy.get("name") or details.get("best_strategy_name") or item.get("strategy") or setup).strip().lower()
    if best_strategy_name == "no_actionable_strategy" or setup == "no_actionable_strategy":
        return _blocked("no_actionable_strategy", "No actionable setup is present for a fresh BUY.")
    if label in {"ACTIONABLE_WATCH", "LOW_QUALITY_SHORT_COVERING", "LATE_CHASE_AVOID", "DATA_STALE_WATCH"} or bucket in {
        "ACTIONABLE_WATCH",
        "LOW_QUALITY_SHORT_COVERING",
        "DATA_STALE_WATCH",
        "LATE_CHASE_AVOID",
    }:
        classification = label or bucket
        return _blocked(
            classification.lower(),
            "This candidate is watch-only by classification and cannot be auto-entered.",
            classification_label=classification,
        )
    trade_window = _scan_trade_window(scan, details)
    if _is_wait_only_trade_window(trade_window):
        return _blocked(
            "opportunity_scan_wait_state",
            f"Entry window is {trade_window}; wait for pullback/live confirmation instead of auto-entering.",
            trade_window=trade_window,
        )
    if setup in {"extended_momentum_watch", "circuit_demand_lock", "pre_rally_fuel"}:
        return _blocked("missing_actionable_setup", "Setup is a watch state, not a fresh BUY entry.")
    if _upper(scan.get("only_buyers") or scan.get("only_buyer") or details.get("only_buyers")) in {"1", "TRUE", "YES"}:
        return _blocked("upper_circuit_only_buyers_watch", "Only-buyers/circuit demand is pullback-only until tradable liquidity appears.")
    circuit_text = " ".join(
        str(value or "").lower()
        for value in (
            scan.get("circuit_state"),
            scan.get("price_band"),
            scan.get("setup"),
            details.get("circuit_state"),
            details.get("classification_label"),
        )
    )
    if any(token in circuit_text for token in ("upper_circuit", "upper circuit", "only_buyer", "only buyer", "circuit_demand")):
        return _blocked("upper_circuit_only_buyers_watch", "Circuit/only-buyer moves are watch-for-pullback, not normal BUYs.")
    if any(token in setup for token in ("short_cover", "squeeze")) or any(
        token in str(label or bucket).lower() for token in ("short_cover", "squeeze")
    ):
        return _blocked("low_quality_short_covering", "Short-covering/squeeze bounces are watch or tiny-paper only.")
    day_gain = _number(scan.get("day_gain_pct"), details.get("day_gain_pct"))
    if day_gain is not None and day_gain >= 8.0:
        return _blocked(
            "late_chase_avoid",
            "Fresh BUY is blocked because the current-session move is already too extended.",
            day_gain_pct=day_gain,
        )
    pivot_extension = _number(
        scan.get("pivot_extension_pct"),
        scan.get("distance_from_pivot_pct"),
        details.get("pivot_extension_pct"),
        details.get("distance_from_pivot_pct"),
        (details.get("entry_quality") or {}).get("distance_from_pivot_pct") if isinstance(details.get("entry_quality"), dict) else None,
    )
    if pivot_extension is not None and pivot_extension > 5.0:
        return _blocked(
            "late_chase_avoid",
            "Fresh BUY is blocked because price is more than 5% above the pivot/entry zone.",
            pivot_extension_pct=pivot_extension,
        )
    if _us_etf_or_fund_watch_only(item, details):
        return _blocked(
            "us_etf_or_fund_watch_only",
            "US ETFs/funds are watch-only for this equities engine and cannot be auto-entered as normal BUYs.",
        )

    stale_reason = _stale_data_reason(item, details, scan)
    if stale_reason:
        return _blocked(stale_reason, "Fresh BUY is blocked until quote and live-confirmation data are from the current session.")

    technical_score = _number(
        item.get("technical_score"),
        details.get("technical_score"),
        details.get("technical_math_score"),
        (details.get("technical_math") or {}).get("score") if isinstance(details.get("technical_math"), dict) else None,
    )
    if technical_score is not None and technical_score < 0.50:
        return _blocked(
            "technical_score_below_0_50",
            "Fresh BUY requires technical score of at least 0.50.",
            technical_score=technical_score,
        )
    trend_text = " ".join(
        str(value or "").lower()
        for value in (
            item.get("technical_trend"),
            details.get("technical_trend"),
            details.get("trend"),
            details.get("stage"),
            scan.get("trend"),
        )
    )
    if any(token in trend_text for token in ("downtrend", "stage 4", "stage4", "bearish_trend")):
        return _blocked("downtrend", "Fresh BUY is blocked while the symbol is in a downtrend.")

    sentiment = details.get("sentiment") if isinstance(details.get("sentiment"), dict) else scan.get("sentiment")
    sentiment = sentiment if isinstance(sentiment, dict) else {}
    allow_overhang = label == "OVERHANG_REMOVAL_RERATE" or setup == "overhang_removal_rerate"
    sentiment_score = _number(sentiment.get("score"), details.get("sentiment_score"), item.get("sentiment_score"))
    sentiment_confidence = _number(sentiment.get("confidence"), details.get("sentiment_confidence"))
    negative_catalyst = bool(
        sentiment.get("negative_catalyst")
        or details.get("negative_catalyst")
        or scan.get("negative_catalyst")
        or (
            sentiment_score is not None
            and sentiment_score <= -0.30
            and (sentiment_confidence is None or sentiment_confidence >= 0.30)
        )
    )
    if negative_catalyst and not allow_overhang:
        return _blocked("negative_catalyst", "Negative catalyst/news tone blocks normal-conviction BUY.")
    return None


def _scan_trade_window(scan: dict[str, Any], details: dict[str, Any]) -> str:
    values = [
        scan.get("trade_window"),
        details.get("trade_window"),
    ]
    market_action = scan.get("market_action") if isinstance(scan.get("market_action"), dict) else {}
    rally = scan.get("rally_radar") if isinstance(scan.get("rally_radar"), dict) else {}
    values.extend([market_action.get("trade_window"), rally.get("trade_window")])
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _is_wait_only_trade_window(value: Any) -> bool:
    return str(value or "").strip().lower() in _WAIT_ONLY_TRADE_WINDOWS


def _stale_data_reason(item: dict[str, Any], details: dict[str, Any], scan: dict[str, Any]) -> str:
    data_readiness = item.get("data_readiness") if isinstance(item.get("data_readiness"), dict) else details.get("data_readiness")
    data_readiness = data_readiness if isinstance(data_readiness, dict) else {}
    freshness_gate = data_readiness.get("fresh_market_data_gate") if isinstance(data_readiness.get("fresh_market_data_gate"), dict) else {}
    if freshness_gate and freshness_gate.get("passed") is False:
        return str(freshness_gate.get("reason") or "stale_market_data")
    labels = {
        str(value or "").strip().lower()
        for value in data_readiness.get("missing_data") or []
        if str(value or "").strip()
    }
    for collection_key in ("hard_gaps", "soft_gaps"):
        for gap in data_readiness.get(collection_key) or []:
            if isinstance(gap, dict):
                for key in ("key", "label", "reason"):
                    value = str(gap.get(key) or "").strip().lower()
                    if value:
                        labels.add(value)
            else:
                labels.add(str(gap or "").strip().lower())
    data_quality = scan.get("data_quality") if isinstance(scan.get("data_quality"), dict) else {}
    labels.update(str(value or "").strip().lower() for value in data_quality.get("missing") or [] if str(value or "").strip())
    labels.update(
        str(value or "").strip().lower()
        for value in (item.get("missing_data") or details.get("missing_data") or [])
        if str(value or "").strip()
    )
    hard_stale_tokens = ("stale_quote", "prior_session", "previous_session", "moneycontrol_prior")
    if any(token in label for label in labels for token in hard_stale_tokens):
        return "stale_market_data"
    if any("stale_intraday" in label for label in labels) and not freshness_gate.get("passed"):
        return "stale_market_data"
    quote = item.get("quote") if isinstance(item.get("quote"), dict) else details.get("quote")
    quote = quote if isinstance(quote, dict) else {}
    source = str(quote.get("source") or data_quality.get("quote_source") or "").lower()
    if "moneycontrol" in source and any(token in source for token in ("prior", "previous", "delayed")):
        return "moneycontrol_prior_session_data"
    return ""


def _us_etf_or_fund_watch_only(item: dict[str, Any], details: dict[str, Any]) -> bool:
    data_readiness = item.get("data_readiness") if isinstance(item.get("data_readiness"), dict) else details.get("data_readiness")
    data_readiness = data_readiness if isinstance(data_readiness, dict) else {}
    market = _upper(item.get("market_region") or details.get("market_region") or data_readiness.get("market_region"))
    if market != "US":
        return False
    full = details.get("full_spectrum_analysis") if isinstance(details.get("full_spectrum_analysis"), dict) else {}
    fundamental = full.get("fundamental_quality") if isinstance(full.get("fundamental_quality"), dict) else details.get("fundamental_quality")
    fundamental = fundamental if isinstance(fundamental, dict) else {}
    fields = (
        item.get("security_type"),
        item.get("quote_type"),
        details.get("security_type"),
        details.get("quote_type"),
        fundamental.get("security_type"),
        fundamental.get("quote_type"),
        fundamental.get("sector"),
        fundamental.get("industry"),
    )
    text = " ".join(str(value or "").upper() for value in fields)
    return any(token in text for token in ("ETF", "ETN", "FUND"))


def _risk_flags(item: dict[str, Any], details: dict[str, Any]) -> list[str]:
    flags = item.get("risk_flags")
    if not isinstance(flags, list):
        flags = details.get("risk_flags")
    if not isinstance(flags, list):
        return []
    return [str(flag or "").strip().lower() for flag in flags if str(flag or "").strip()]


def _severe_risk_flags(
    risk_flags: list[str],
    *,
    opportunity_probe: bool = False,
    playbook_entry_ok: bool = False,
    playbook_market_region: str = "",
) -> list[str]:
    severe_tokens = _opportunity_probe_hard_risk_tokens() if opportunity_probe else (
        "hard_block",
        "no_new_longs",
        "false_breakout_risk",
        "climax",
        "distribution",
        "stop_hit",
        "earnings_lockout",
        "delivery_conflict",
    )
    reduce_size_exceptions = ("reduce_size", "small_size", "probe")
    severe: list[str] = []
    for flag in risk_flags:
        normalized = str(flag or "").strip().lower()
        if not normalized:
            continue
        if playbook_entry_ok and "price_extended_from_pivot" in normalized:
            continue
        if (
            playbook_entry_ok
            and playbook_market_region == "US"
            and ("possible_circuit" in normalized or "extreme_atr_volatility" in normalized)
        ):
            continue
        if any(exception in normalized for exception in reduce_size_exceptions):
            continue
        if any(token in normalized for token in severe_tokens):
            severe.append(normalized)
    return severe


def _opportunity_probe_hard_risk_tokens() -> tuple[str, ...]:
    return (
        "hard_block",
        "asm_surveillance",
        "delivery_conflict",
        "distribution",
        "mtf",
        "timeframe_conflict",
        "stop_hit",
        "climax",
        "earnings_lockout",
        "corporate_event_risk",
        "circuit",
        "price_extended_from_pivot",
    )


def _breakout_payload(details: dict[str, Any]) -> dict[str, Any]:
    for key in ("breakout_quality", "breakout"):
        payload = details.get(key)
        if isinstance(payload, dict):
            return payload
    return {}


def _opportunity_probe_ready(item: dict[str, Any], details: dict[str, Any]) -> bool:
    if _explicit_opportunity_probe_ready(item, details):
        return True
    scan = item.get("opportunity_scan") if isinstance(item.get("opportunity_scan"), dict) else details.get("opportunity_scan")
    if not isinstance(scan, dict):
        scan = {}
    if _top_gainers_playbook_probe(item, details):
        return True
    review = details.get("live_momentum_review")
    if not isinstance(review, dict):
        review = scan.get("live_momentum_review") if isinstance(scan.get("live_momentum_review"), dict) else {}
    if bool(
        review.get("strategy_ready")
        or review.get("early_ignition_ready")
        or review.get("live_momentum_ready")
        or review.get("market_action_breakout_ready")
    ):
        return True

    setup = str(scan.get("setup") or "").strip().lower()
    if setup in {"extended_momentum_watch", "pre_rally_fuel", "circuit_demand_lock"}:
        return False
    data_quality = scan.get("data_quality") if isinstance(scan.get("data_quality"), dict) else {}
    live_quote_probe_ok = _live_quote_probe_data_ok(item, details, scan, setup)
    if data_quality.get("actionable_data_ready") is False and not data_quality.get("probe_only") and not live_quote_probe_ok:
        return False
    bucket = str(scan.get("bucket") or "").strip().lower()
    score = _number(scan.get("score")) or 0.0
    if bucket == "actionable" and setup in {
        "opening_ignition",
        "intraday_momentum",
        "btst_buy_candidate",
        "breakout_continuation",
        "near_breakout",
        "news_catalyst",
        "52_week_high_volume_breakout",
        "broker_re_rating_breakout",
        "earnings_beat_gap_and_go",
        "market_action_momentum",
        "top_gainer_momentum",
        "price_shocker_reversal_breakout",
    }:
        return score >= 0.60 or bool(data_quality.get("actionable_data_ready")) or live_quote_probe_ok
    return False


def _opportunity_probe_min_confluence(item: dict[str, Any], details: dict[str, Any]) -> float:
    explicit = _explicit_opportunity_probe_min_confluence(item, details)
    if explicit is not None:
        return explicit
    source, setup, scan_score = _opportunity_probe_profile_hint(item, details)
    return _opportunity_probe_min_confluence_from_profile(source, setup, scan_score)


def _explicit_opportunity_probe_ready(item: dict[str, Any], details: dict[str, Any]) -> bool:
    for payload in _opportunity_probe_payloads(item, details):
        if payload.get("ready") is True:
            return True
    return False


def _explicit_opportunity_probe_min_confluence(item: dict[str, Any], details: dict[str, Any]) -> float | None:
    for payload in _opportunity_probe_payloads(item, details):
        value = _number(payload.get("min_confluence"))
        if value is not None:
            return value
    return None


def _opportunity_probe_payloads(item: dict[str, Any], details: dict[str, Any]) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    seen: set[int] = set()

    def collect(value: Any) -> None:
        if not isinstance(value, dict):
            return
        marker = id(value)
        if marker in seen:
            return
        seen.add(marker)
        payloads.append(value)

    for container in (item, details):
        collect(container.get("opportunity_probe"))
        gate_context = container.get("decision_gate_context")
        if isinstance(gate_context, dict):
            collect(gate_context.get("opportunity_probe"))
        risk_gates = container.get("risk_gates")
        if isinstance(risk_gates, dict):
            collect(risk_gates.get("opportunity_probe"))
            nested_gate_context = risk_gates.get("decision_gate_context")
            if isinstance(nested_gate_context, dict):
                collect(nested_gate_context.get("opportunity_probe"))
        context = container.get("context")
        if isinstance(context, dict):
            nested_gate_context = context.get("decision_gate_context")
            if isinstance(nested_gate_context, dict):
                collect(nested_gate_context.get("opportunity_probe"))
    return payloads


def _opportunity_probe_profile_hint(item: dict[str, Any], details: dict[str, Any]) -> tuple[str, str, float]:
    scan = item.get("opportunity_scan") if isinstance(item.get("opportunity_scan"), dict) else details.get("opportunity_scan")
    scan = scan if isinstance(scan, dict) else {}
    review = details.get("live_momentum_review")
    if not isinstance(review, dict):
        review = scan.get("live_momentum_review") if isinstance(scan.get("live_momentum_review"), dict) else {}

    source = ""
    setup = str(scan.get("setup") or review.get("setup") or details.get("setup") or item.get("setup") or "").strip().lower()
    scan_score = _number(scan.get("score"), review.get("scan_score"), review.get("score")) or 0.0
    for payload in _opportunity_probe_payloads(item, details):
        payload_source = str(payload.get("source") or "").strip().lower()
        payload_setup = str(payload.get("setup") or "").strip().lower()
        payload_score = _number(payload.get("scan_score"), payload.get("score"))
        if payload_source:
            source = payload_source
        if payload_setup:
            setup = payload_setup
        if payload_score is not None:
            scan_score = payload_score
        if source or setup:
            break

    if not source:
        if bool(
            review.get("strategy_ready")
            or review.get("early_ignition_ready")
            or review.get("live_momentum_ready")
            or review.get("market_action_breakout_ready")
        ):
            source = "live_momentum_review"
        elif scan and _live_quote_probe_data_ok(item, details, scan, setup):
            source = "live_quote_opportunity_scan"
        elif scan:
            source = "opportunity_scan"
    return source, setup, scan_score


def _opportunity_probe_min_confluence_from_profile(source: Any, setup: str, scan_score: float) -> float:
    normalized_source = str(source or "").strip().lower()
    normalized_setup = str(setup or "").strip().lower()
    score = float(scan_score or 0.0)
    if normalized_source in _LIVE_OPPORTUNITY_SOURCES:
        if normalized_setup in _LIVE_OPPORTUNITY_SETUPS:
            return 6.0 if score >= 0.85 else 10.0
        if normalized_setup in _BREAKOUT_OPPORTUNITY_SETUPS:
            return 10.0 if score >= 0.82 else 12.0
        return 12.0
    if normalized_source == "opportunity_scan":
        return 12.0
    return OPPORTUNITY_PROBE_MIN_CONFLUENCE


def _top_gainers_playbook_probe(item: dict[str, Any], details: dict[str, Any]) -> dict[str, Any]:
    scan = item.get("opportunity_scan") if isinstance(item.get("opportunity_scan"), dict) else details.get("opportunity_scan")
    if not isinstance(scan, dict):
        return {}
    playbook = scan.get("top_gainers_playbook") if isinstance(scan.get("top_gainers_playbook"), dict) else {}
    signal = _upper(playbook.get("final_signal"))
    if signal not in {"STRONG BUY", "MODERATE BUY"}:
        return {}
    if playbook.get("hard_excluded") or playbook.get("hard_excludes"):
        return {}
    catalyst = playbook.get("catalyst_review") if isinstance(playbook.get("catalyst_review"), dict) else {}
    if catalyst.get("catalyst_confirmed") is not True:
        return {}
    anti_codes = {
        _upper(flag.get("code"))
        for flag in playbook.get("anti_patterns") or []
        if isinstance(flag, dict)
    }
    if anti_codes & {"CHASING", "OPERATOR_RISK", "SHORT_COVER", "STAGE_TRAP", "ILLIQUID_BREAKOUT", "FAILED_BREAKOUT_RISK"}:
        return {}
    weinstein = playbook.get("weinstein") if isinstance(playbook.get("weinstein"), dict) else {}
    stage = str(weinstein.get("stage") or "").strip()
    if stage in {"Stage 3", "Stage 4"}:
        return {}
    levels = playbook.get("levels") if isinstance(playbook.get("levels"), dict) else {}
    price = _number(item.get("latest_price"), item.get("price"), details.get("latest_price"), playbook.get("cmp"))
    entry = _number(levels.get("entry"))
    max_entry = _number(levels.get("max_entry"))
    stop = _number(levels.get("stop"))
    if price is None or entry is None or max_entry is None or stop is None:
        return {}
    if price > max_entry * 1.0005 or stop >= price:
        return {}
    stop_risk_pct = ((price - stop) / price) * 100.0
    if stop_risk_pct > 9.0:
        return {}
    quant_score = _number(playbook.get("quant_score"))
    if quant_score is None:
        return {}
    min_score = 70.0
    if quant_score < min_score:
        return {}
    return {
        "signal": signal,
        "quant_score": quant_score,
        "min_score": min_score,
        "market_region": _upper(playbook.get("market_region") or item.get("market_region") or details.get("market_region")),
        "stage": stage,
        "entry": entry,
        "max_entry": max_entry,
        "stop": stop,
        "stop_risk_pct": stop_risk_pct,
    }


def _btst_buy_probe(item: dict[str, Any], details: dict[str, Any]) -> dict[str, Any]:
    scan = item.get("opportunity_scan") if isinstance(item.get("opportunity_scan"), dict) else details.get("opportunity_scan")
    if not isinstance(scan, dict) or str(scan.get("setup") or "").strip().lower() != "btst_buy_candidate":
        return {}
    btst = scan.get("btst") if isinstance(scan.get("btst"), dict) else {}
    if not btst.get("detected"):
        return {}
    data_quality = scan.get("data_quality") if isinstance(scan.get("data_quality"), dict) else {}
    if data_quality.get("actionable_data_ready") is False:
        return {}
    if str(scan.get("bucket") or "").strip().lower() != "actionable":
        return {}
    score = _number(btst.get("score"), scan.get("score"))
    if score is None:
        return {}
    score_pct = score * 100.0 if score <= 1.0 else score
    if score_pct < 70.0:
        return {}
    checks = btst.get("checks") if isinstance(btst.get("checks"), dict) else {}
    required = {
        "liquidity_ok",
        "trend_ok",
        "range_ok",
        "day_move_ok",
        "not_extended",
        "volume_ok",
        "overnight_risk_ok",
        "sentiment_ok",
    }
    if any(checks.get(key) is False for key in required):
        return {}
    return {
        "btst_score": score_pct,
        "min_score": 70.0,
        "size_multiplier": 0.75,
        "entry_zone": btst.get("entry_zone"),
        "stop_loss": btst.get("stop_loss"),
        "target1": btst.get("target1"),
    }


def _filter_playbook_absorbable_blocks(blocks: list[Any], playbook_probe: dict[str, Any]) -> list[Any]:
    output: list[Any] = []
    for block in blocks:
        flag = ""
        value = None
        if isinstance(block, dict):
            flag = _upper(block.get("flag") or block.get("gate") or block.get("reason"))
            value = block.get("value") if "value" in block else block
        else:
            flag = _upper(block)
        if "PRICE_EXTENDED_FROM_PIVOT" in flag:
            continue
        if "GRADE_VIOLATION" in flag:
            continue
        if "MTF_HARD_BLOCK" in flag and playbook_probe.get("stage") == "Stage 2":
            continue
        if "DATA_READINESS_BLOCK" in flag and _playbook_reference_data_block_absorbable(value, playbook_probe):
            continue
        output.append(block)
    return output


def _live_quote_probe_data_ok(item: dict[str, Any], details: dict[str, Any], scan: dict[str, Any], setup: str) -> bool:
    data_quality = scan.get("data_quality") if isinstance(scan.get("data_quality"), dict) else {}
    missing = {str(value or "").strip().lower() for value in data_quality.get("missing") or [] if str(value or "").strip()}
    if missing and missing - {"stale_intraday_candles"}:
        return False
    if setup not in {
        "opening_ignition",
        "intraday_momentum",
        "btst_buy_candidate",
        "breakout_continuation",
        "near_breakout",
        "news_catalyst",
        "52_week_high_volume_breakout",
        "broker_re_rating_breakout",
        "earnings_beat_gap_and_go",
        "market_action_momentum",
        "top_gainer_momentum",
        "price_shocker_reversal_breakout",
    }:
        return False
    quote = item.get("quote") if isinstance(item.get("quote"), dict) else details.get("quote")
    quote = quote if isinstance(quote, dict) else {}
    data_readiness = item.get("data_readiness") if isinstance(item.get("data_readiness"), dict) else details.get("data_readiness")
    data_readiness = data_readiness if isinstance(data_readiness, dict) else {}
    sources = data_readiness.get("sources") if isinstance(data_readiness.get("sources"), dict) else {}
    source = str(quote.get("source") or data_quality.get("quote_source") or sources.get("quote") or "").lower()
    if not any(token in source for token in ("upstox", "kite", "nubra")):
        return False
    has_live_ohlcv = all((_number(quote.get(key)) or 0.0) > 0 for key in ("price", "open", "high", "low", "volume"))
    turnover = _number(scan.get("turnover")) or 0.0
    projected_turnover = _number(scan.get("projected_turnover")) or 0.0
    liquidity_ok = turnover >= 50_000_000 or projected_turnover >= 150_000_000
    if has_live_ohlcv:
        return liquidity_ok
    return data_readiness.get("trade_decision_ready") is True and liquidity_ok


def _opportunity_probe_data_readiness_override(item: dict[str, Any], details: dict[str, Any], data_readiness: dict[str, Any]) -> str:
    scan = item.get("opportunity_scan") if isinstance(item.get("opportunity_scan"), dict) else details.get("opportunity_scan")
    if not isinstance(scan, dict):
        return ""
    playbook_probe = _top_gainers_playbook_probe(item, details)
    if playbook_probe and _playbook_reference_data_readiness_absorbable(item, details, data_readiness, playbook_probe):
        return "us_yahoo_reference_reduced_size"
    setup = str(scan.get("setup") or "").strip().lower()
    if not _live_quote_probe_data_ok(item, details, scan, setup):
        return ""
    hard_gaps = data_readiness.get("hard_gaps") or []
    keys = {
        str(gap.get("key") or "").strip().lower()
        for gap in hard_gaps
        if isinstance(gap, dict) and str(gap.get("key") or "").strip()
    }
    if bool(keys) and keys <= {"in_intraday_candles"}:
        return "live_quote_intraday_candles_stale"
    return ""


def _playbook_reference_data_readiness_absorbable(
    item: dict[str, Any],
    details: dict[str, Any],
    data_readiness: dict[str, Any],
    playbook_probe: dict[str, Any],
) -> bool:
    if playbook_probe.get("market_region") != "US":
        return False
    quote = item.get("quote") if isinstance(item.get("quote"), dict) else details.get("quote")
    quote = quote if isinstance(quote, dict) else {}
    sources = data_readiness.get("sources") if isinstance(data_readiness.get("sources"), dict) else {}
    source = str(quote.get("source") or sources.get("quote") or "").lower()
    if "yahoo" not in source:
        return False
    hard_gaps = data_readiness.get("hard_gaps") or []
    keys = {
        str(gap.get("key") or "").strip().lower()
        for gap in hard_gaps
        if isinstance(gap, dict) and str(gap.get("key") or "").strip()
    }
    return bool(keys) and keys <= {"us_realtime_quote", "us_minute_bars", "us_sec_filings"}


def _playbook_reference_data_block_absorbable(value: Any, playbook_probe: dict[str, Any]) -> bool:
    if playbook_probe.get("market_region") != "US":
        return False
    if isinstance(value, dict) and "hard_gaps" in value:
        keys = {
            str(gap.get("key") or "").strip().lower()
            for gap in value.get("hard_gaps") or []
            if isinstance(gap, dict) and str(gap.get("key") or "").strip()
        }
    elif isinstance(value, dict):
        key = str(value.get("key") or "").strip().lower()
        keys = {key} if key else set()
    else:
        keys = set()
    return bool(keys) and keys <= {"us_realtime_quote", "us_minute_bars", "us_sec_filings"}


def _missing_sentiment_news(data_readiness: dict[str, Any]) -> bool:
    for collection_key in ("soft_gaps", "hard_gaps", "missing_data"):
        for item in data_readiness.get(collection_key) or []:
            if isinstance(item, dict):
                key = str(item.get("key") or item.get("label") or "").strip().lower()
            else:
                key = str(item or "").strip().lower()
            if key == "sentiment_news" or ("sentiment" in key and "news" in key):
                return True
    return False


def _soft_missing_data_labels(data_readiness: dict[str, Any]) -> list[str]:
    labels: list[str] = []
    for item in data_readiness.get("soft_gaps") or []:
        if isinstance(item, dict):
            key = str(item.get("key") or item.get("label") or "").strip()
        else:
            key = str(item or "").strip()
        normalized = key.lower()
        if not key or normalized == "sentiment_news" or ("sentiment" in normalized and "news" in normalized):
            continue
        labels.append(key)
    return list(dict.fromkeys(labels))


def _target_one_gate(
    item: dict[str, Any],
    details: dict[str, Any],
    *,
    playbook_probe: bool = False,
) -> tuple[dict[str, Any] | None, float | None, str]:
    t1 = _target_one(item.get("target_status"), details.get("target_status"), details.get("targets"))
    if not t1:
        return None, None, ""
    probability = str(t1.get("probability_label") or t1.get("probability") or "").strip().lower()
    distance = _number(t1.get("distance_pct"))
    hard_distance = 22.0 if playbook_probe else HARD_T1_DISTANCE_PCT
    if distance is not None and distance > hard_distance:
        return _blocked(
            "target_1_too_far_for_fresh_entry",
            f"T1 is {distance:.1f}% away; wait for a better entry before paper/live follow.",
            target_1=t1,
            hard_t1_distance_pct=hard_distance,
        ), None, ""
    if probability in {"low", "low_probability", "low probability"}:
        return _blocked(
            "target_1_low_probability",
            "T1 is marked as stretch/low probability, so this is Watch only until price gives a better entry.",
            target_1=t1,
        ), None, ""
    if probability == "stretch" or (distance is not None and distance > CAUTION_T1_DISTANCE_PCT):
        return None, 0.50, "T1 is stretched; use reduced paper size"
    return None, None, ""


def _target_one(*collections: Any) -> dict[str, Any] | None:
    for collection in collections:
        if not isinstance(collection, list):
            continue
        for index, target in enumerate(collection):
            if not isinstance(target, dict):
                continue
            label = str(target.get("label") or f"T{index + 1}").strip().upper()
            if label == "T1" or index == 0:
                return target
    return None


def _stop_risk_gate(item: dict[str, Any], details: dict[str, Any]) -> tuple[dict[str, Any] | None, float | None, str]:
    stop_status = details.get("stop_status") if isinstance(details.get("stop_status"), dict) else {}
    price = _number(item.get("latest_price"), item.get("price"), item.get("entry_price"), details.get("latest_price"))
    stop = _number(item.get("stop_loss"), details.get("stop_loss"), stop_status.get("price"))
    if price is None or stop is None or price <= 0 or stop <= 0 or stop >= price:
        return None, None, ""
    risk_pct = ((price - stop) / price) * 100.0
    if risk_pct > HARD_STOP_RISK_PCT:
        return _blocked(
            "stop_risk_too_wide",
            f"Stop risk is {risk_pct:.1f}%; wait for a tighter entry before paper/live follow.",
            stop_loss=stop,
            price=price,
            stop_risk_pct=round(risk_pct, 4),
            hard_stop_risk_pct=HARD_STOP_RISK_PCT,
        ), None, ""
    if risk_pct > CAUTION_STOP_RISK_PCT:
        return None, 0.50, f"stop risk is {risk_pct:.1f}%; use reduced paper size"
    return None, None, ""


def _number(*values: Any) -> float | None:
    for value in values:
        if value in (None, ""):
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _score_grade(score: float | None) -> str:
    value = _number(score)
    if value is None:
        return ""
    if value >= 80:
        return "A"
    if value >= 70:
        return "B"
    if value >= 55:
        return "C"
    return "D"


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
