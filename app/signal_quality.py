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
    playbook_probe = _top_gainers_playbook_probe(item, details)
    if playbook_probe:
        hard_blocks = _filter_playbook_absorbable_blocks(hard_blocks)
        phase3_blocks = _filter_playbook_absorbable_blocks(phase3_blocks)
        hard_blocked = bool(hard_blocks or phase3_blocks)
    if hard_blocked or hard_blocks or phase3_blocks:
        return _blocked("hard_blocked", "System hard blocks are present.")

    opportunity_probe = bool(playbook_probe) or _opportunity_probe_ready(item, details)
    min_score = (
        float(playbook_probe.get("min_score") or OPPORTUNITY_PROBE_MIN_SCORE)
        if playbook_probe
        else OPPORTUNITY_PROBE_MIN_SCORE
        if opportunity_probe
        else FRESH_BUY_MIN_SCORE
    )
    min_confluence = (
        0.0
        if playbook_probe
        else OPPORTUNITY_PROBE_MIN_CONFLUENCE
        if opportunity_probe
        else ACTIONABLE_MIN_CONFLUENCE
    )
    allowed_grades = OPPORTUNITY_PROBE_ALLOWED_GRADES if opportunity_probe else FRESH_BUY_ALLOWED_GRADES

    tradeability_score = _number(item.get("overall_score_pct"), details.get("overall_score_pct"))
    setup_score = _number(details.get("setup_score_pct"))
    score = tradeability_score
    if opportunity_probe and setup_score is not None:
        score = max(score or 0.0, setup_score)
    if playbook_probe and _number(playbook_probe.get("quant_score")) is not None:
        score = max(score or 0.0, float(playbook_probe["quant_score"]))
    if score is None or score < min_score:
        return _blocked(
            "overall_score_below_opportunity_probe_minimum" if opportunity_probe else "overall_score_below_70",
            f"Overall score must be at least {min_score:.0f}.",
            overall_score_pct=score,
            min_score=min_score,
            min_confluence=min_confluence,
            allowed_grades=sorted(allowed_grades),
        )

    grade = _upper(item.get("overall_grade") or details.get("overall_grade"))
    if opportunity_probe:
        grade = _upper(details.get("setup_grade") or grade)
    if playbook_probe:
        grade = _score_grade(score)
    if grade not in allowed_grades:
        return _blocked(
            "grade_not_a_b_or_c_for_opportunity_probe" if opportunity_probe else "grade_not_a_or_b",
            "Fresh BUY requires grade A or B." if not opportunity_probe else "Opportunity probe requires grade A, B, or C.",
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
    data_readiness_override = bool(
        opportunity_probe and _opportunity_probe_data_readiness_override(item, details, data_readiness)
    )
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
    if opportunity_probe:
        size_multiplier = min(size_multiplier, OPPORTUNITY_PROBE_SIZE_MULTIPLIER)
        cautions.append("opportunity scan setup; use probe size until confirmation matures")
    if data_readiness_override:
        size_multiplier = min(size_multiplier, OPPORTUNITY_PROBE_SIZE_MULTIPLIER)
        cautions.append("live quote is available but intraday candles are stale; use probe size only")
        missing_data.append("stale_intraday_candles")

    severe_flags = _severe_risk_flags(
        risk_flags,
        opportunity_probe=opportunity_probe,
        playbook_entry_ok=bool(playbook_probe),
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
    if not isinstance(risk_flags, list):
        details = _details(item)
        risk_flags = _risk_flags(item, details)
    severe_flags = _severe_risk_flags(risk_flags)
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
    if status in {"WATCH", "STOP_HIT", "EXIT_SIGNAL", "EXPIRED", "TARGET_3_HIT"} or signal_type == "WATCH":
        return _blocked("active_follow_not_tradeable_state", "Followed position moved into watch/closed state.")

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
    anti_codes = {
        _upper(flag.get("code"))
        for flag in playbook.get("anti_patterns") or []
        if isinstance(flag, dict)
    }
    if anti_codes & {"CHASING", "OPERATOR_RISK", "SHORT_COVER", "STAGE_TRAP", "ILLIQUID_BREAKOUT", "FAILED_BREAKOUT_RISK"}:
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
    min_score = 70.0 if signal == "STRONG BUY" else 55.0
    if quant_score < min_score:
        return {}
    return {
        "signal": signal,
        "quant_score": quant_score,
        "min_score": min_score,
        "entry": entry,
        "max_entry": max_entry,
        "stop": stop,
        "stop_risk_pct": stop_risk_pct,
    }


def _filter_playbook_absorbable_blocks(blocks: list[Any]) -> list[Any]:
    output: list[Any] = []
    for block in blocks:
        flag = ""
        if isinstance(block, dict):
            flag = _upper(block.get("flag") or block.get("gate") or block.get("reason"))
        else:
            flag = _upper(block)
        if "PRICE_EXTENDED_FROM_PIVOT" in flag:
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


def _opportunity_probe_data_readiness_override(item: dict[str, Any], details: dict[str, Any], data_readiness: dict[str, Any]) -> bool:
    scan = item.get("opportunity_scan") if isinstance(item.get("opportunity_scan"), dict) else details.get("opportunity_scan")
    if not isinstance(scan, dict):
        return False
    setup = str(scan.get("setup") or "").strip().lower()
    if not _live_quote_probe_data_ok(item, details, scan, setup):
        return False
    hard_gaps = data_readiness.get("hard_gaps") or []
    keys = {
        str(gap.get("key") or "").strip().lower()
        for gap in hard_gaps
        if isinstance(gap, dict) and str(gap.get("key") or "").strip()
    }
    return bool(keys) and keys <= {"in_intraday_candles"}


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
