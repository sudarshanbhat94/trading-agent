from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any


RESULT_SEASON_MONTHS = {1, 2, 4, 5, 7, 8, 10, 11}
ENTRY_DOWNGRADE = {"A": "B", "B": "C", "C": "WATCH", "WATCH": "WATCH", "D": "D"}
VALID_ENTRY_GRADES = {"A", "B", "C"}


def capital_position_limit(equity: float | None) -> int:
    value = float(equity or 0.0)
    if value <= 25_000:
        return 4
    if value <= 75_000:
        return 7
    if value <= 150_000:
        return 12
    return 15


def evaluate_rules_for_context(
    context: dict[str, Any],
    positions: dict[str, dict[str, Any]],
    portfolio_equity: float | None,
    market_health: dict[str, Any] | None = None,
    macro_calendar_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    full = context.get("full_spectrum_analysis") or {}
    quote = context.get("quote") or {}
    sentiment = context.get("sentiment") or {}
    macro_event = context.get("macro_event_context") or {}
    sector_context = full.get("sector_rotation") or context.get("sector_rotation") or {}
    delivery = full.get("delivery_accumulation") or context.get("delivery_data") or {}
    entry = full.get("entry_quality") or {}
    breakout = full.get("breakout_quality") or {}
    divergence = full.get("price_volume_divergence") or {}
    alignment = ((full.get("trend_context") or {}).get("timeframe_alignment") or {})
    options_oi = full.get("options_oi") or context.get("options_intelligence") or {}

    hard_blocks: list[dict[str, Any]] = []
    soft_flags: list[dict[str, Any]] = []
    active_flags: list[str] = []

    def hard(flag: str, reason: str, value: Any = None) -> None:
        active_flags.append(flag)
        hard_blocks.append({"flag": flag, "reason": reason, "value": value})

    def soft(flag: str, reason: str, value: Any = None) -> None:
        active_flags.append(flag)
        soft_flags.append({"flag": flag, "reason": reason, "value": value})

    def data_gap(flag: str, reason: str, value: Any = None) -> None:
        soft_flags.append({"flag": flag, "reason": reason, "value": value, "data_gap": True})

    price = price_integrity(quote, market_health)
    if price.get("price_mismatch"):
        hard("PRICE_MISMATCH", "two available price references differ by more than 1%", price)

    options_available = options_oi.get("status") == "ok" and bool(options_oi.get("available", True))
    options_status = str(options_oi.get("status") or "")
    options_data_gap = str(options_oi.get("data_gap") or "")
    options_note = None
    if not options_available:
        if options_status == "not_fno_no_stock_options" or options_data_gap == "symbol_not_in_fno_no_stock_options":
            options_note = "Options intelligence unavailable — stock is not F&O listed."
        else:
            options_note = "Options intelligence unavailable — F&O eligibility was not verified, so options-derived signals are removed."
    options = {
        "available": options_available,
        "status": options_oi.get("status"),
        "source": options_oi.get("audit_label") or options_oi.get("source"),
        "note": options_note,
    }

    sector = str(sector_context.get("sector") or context.get("sector") or "").strip()
    industry = str(sector_context.get("industry") or context.get("industry") or "").strip()
    sector_missing = not sector or sector.lower() in {"none", "unknown", "unclassified", "na", "n/a", "-"}
    industry_missing = not industry or industry.lower() in {"none", "unknown", "unclassified", "na", "n/a", "-"}
    sector_metadata_missing = sector_missing
    if sector_metadata_missing:
        soft("SECTOR_MISSING", "sector classification is missing; exclude from sector rotation/concentration math", {"sector": sector, "industry": industry})
    elif industry_missing:
        data_gap("INDUSTRY_MISSING", "industry classification is missing; sector-level checks remain usable", {"sector": sector, "industry": industry})

    entry_grade = str(entry.get("entry_grade") or "").upper()
    if entry_grade not in VALID_ENTRY_GRADES and entry_grade != "WATCH":
        hard("GRADE_VIOLATION", "entry grade is absent or undefined", entry_grade or None)

    sentiment_audit = sentiment_integrity(sentiment)
    effective_entry_grade = entry_grade or "WATCH"
    if sentiment_audit["status"] == "DATA_MISSING":
        effective_entry_grade = ENTRY_DOWNGRADE.get(effective_entry_grade, "WATCH")
    if effective_entry_grade == "WATCH":
        hard("GRADE_VIOLATION", "WATCH-grade or sentiment-downgraded WATCH setup cannot be opened", {"entry_grade": entry_grade, "effective_entry_grade": effective_entry_grade})

    earnings = earnings_calendar_audit(macro_event, macro_calendar_context)
    if earnings.get("stale_or_empty") and earnings.get("result_season"):
        soft_flags.append(
            {
                "flag": "EARNINGS_CALENDAR_UNAVAILABLE",
                "reason": "earnings calendar is unavailable during result season; reduce size but do not reject the setup without a known event date",
                "value": earnings,
                "sizing": "reduce_only",
            }
        )
    if earnings.get("known_earnings_block"):
        hard("EARNINGS_LOCKOUT", "known earnings date is within 10 trading days", earnings)

    classification = classify_stock(full)
    delivery_bias = str(delivery.get("net_bias") or delivery.get("trend_direction") or delivery.get("bias") or "neutral").lower()
    if delivery_bias == "distribution":
        active_flags.append("DELIVERY_CONFLICT")
        delivery["delivery_conflict"] = True
        distribution_sessions = _delivery_distribution_sessions(delivery)
        if not context.get("position", {}).get("qty"):
            hard("DELIVERY_CONFLICT", "delivery bias is distribution; do not open or add to long exposure", delivery)
        else:
            soft("DELIVERY_CONFLICT", "long position conflicts with distribution delivery; review trailing stop", delivery)
    else:
        distribution_sessions = 0

    suspect_breakout = breakout.get("breakout_quality") == "suspect" or bool(divergence.get("ad_price_divergence"))
    if suspect_breakout:
        soft("SUSPECT_BREAKOUT", "breakout is suspect or AD-line divergence is present; size must be reduced by 50%", {"breakout": breakout, "divergence": divergence})

    mtf_grade = str(alignment.get("alignment_grade") or "").upper()
    if mtf_grade == "D":
        hard("MTF_HARD_BLOCK", "multi-timeframe alignment grade D blocks new entries", mtf_grade)

    open_positions = len([row for row in positions.values() if float(row.get("qty") or 0) > 0])
    position_limit = capital_position_limit(portfolio_equity)
    capital_ok = open_positions < position_limit
    if not capital_ok and not context.get("position", {}).get("qty"):
        hard(
            "POSITION_COUNT_LIMIT",
            "capital pool maximum open-position count reached",
            {"open_positions": open_positions, "position_limit": position_limit, "portfolio_equity": portfolio_equity},
        )

    allocation_cap = float(classification["max_allocation_multiplier"])
    if effective_entry_grade == "WATCH":
        allocation_cap = 0.0
    if mtf_grade == "C":
        allocation_cap = min(allocation_cap, 0.3)
    if suspect_breakout:
        allocation_cap *= 0.5
    if "GRADE_VIOLATION" in active_flags:
        allocation_cap = 0.0

    institutional_quality_allowed = (
        effective_entry_grade in {"A", "B"}
        and classification["classification"] == "FUNDAMENTAL"
        and not active_flags
        and not sector_metadata_missing
        and delivery_bias in {"accumulation", "neutral", ""}
        and not earnings.get("stale_or_empty")
        and sentiment_audit["status"] != "DATA_MISSING"
    )

    audit = {
        "rule_version": 1,
        "hard_blocked": bool(hard_blocks),
        "hard_blocks": hard_blocks,
        "soft_flags": soft_flags,
        "active_flags": _unique(active_flags),
        "price": price,
        "options": options,
        "sector": {
            "sector": sector or None,
            "industry": industry or None,
            "sector_missing": sector_metadata_missing,
            "industry_missing": industry_missing,
        },
        "entry": {"entry_grade": entry_grade or None, "effective_entry_grade": effective_entry_grade},
        "sentiment": sentiment_audit,
        "earnings": earnings,
        "classification": classification,
        "delivery": {"bias": delivery_bias or "neutral", "conflict": delivery_bias == "distribution", "distribution_sessions": distribution_sessions},
        "breakout": {"suspect": suspect_breakout, "breakout_quality": breakout.get("breakout_quality")},
        "mtf": {"alignment_grade": mtf_grade or None},
        "capital": {
            "portfolio_equity": portfolio_equity,
            "open_positions": open_positions,
            "position_limit": position_limit,
            "within_limit_for_new_entries": capital_ok,
        },
        "allocation_cap_multiplier": round(max(min(allocation_cap, 1.0), 0.0), 4),
        "institutional_quality_allowed": institutional_quality_allowed,
    }
    audit["overall_score_pct"] = rule_quality_score_pct(audit)
    audit["overall_grade"] = _score_grade(audit["overall_score_pct"])
    return audit


def price_integrity(quote: dict[str, Any], market_health: dict[str, Any] | None = None) -> dict[str, Any]:
    source = str(quote.get("source") or (market_health or {}).get("provider") or "unknown")
    ts = quote.get("asof") or quote.get("ts")
    market_open = bool((market_health or {}).get("is_market_open")) if market_health else is_nse_regular_session_now()
    mode = (market_health or {}).get("mode")
    label = "live" if market_open and "live" in source and mode != "stale" else "last traded price (LTP)"
    price = _float_or_none(quote.get("price"))
    close = _float_or_none(quote.get("close"))
    if not market_open and price is not None and close is not None and close > 0:
        close_gap_pct = abs((price - close) / close) * 100
        label = "last close" if close_gap_pct <= 0.05 else "last traded price (LTP)"
    elif not market_open:
        label = "last traded price (LTP)"
    reference, reference_source = _independent_reference(quote, source)
    mismatch_pct = None
    price_mismatch = False
    if price and reference:
        mismatch_pct = ((price - reference) / reference) * 100
        price_mismatch = abs(mismatch_pct) > 1.0
    return {
        "label": _price_label(label, ts),
        "source": source,
        "timestamp": ts,
        "market_open": market_open,
        "price": price,
        "reference_price": reference,
        "reference_source": reference_source,
        "mismatch_pct": round(mismatch_pct, 4) if mismatch_pct is not None else None,
        "price_mismatch": price_mismatch,
        "note": "single source only" if reference is None else None,
    }


def classify_stock(full_spectrum: dict[str, Any]) -> dict[str, Any]:
    fundamental = full_spectrum.get("fundamental_quality") or {}
    metrics = fundamental.get("metrics") if isinstance(fundamental.get("metrics"), dict) else fundamental
    revenue_growth = _first_float(metrics, "revenue_growth_yoy_pct", "revenue_growth_yoy", "sales_growth_yoy_pct")
    pat_growth = _first_float(metrics, "pat_growth_yoy_pct", "profit_growth_yoy_pct", "pat_growth_yoy")
    ocf_positive = metrics.get("operating_cash_flow_positive")
    if ocf_positive is None:
        ocf = _first_float(metrics, "operating_cash_flow", "cfo")
        ocf_positive = ocf is not None and ocf > 0
    loss_making = bool(metrics.get("loss_making")) or str(fundamental.get("quality_bucket") or "").lower() == "event_risk"
    revenue_qoq = _first_float(metrics, "revenue_growth_qoq_pct", "revenue_qoq_pct")
    data_available = revenue_growth is not None and pat_growth is not None and ocf_positive is not None
    if data_available and revenue_growth >= 10 and pat_growth >= 10 and bool(ocf_positive):
        classification = "FUNDAMENTAL"
        cap = 1.0
        reason = "revenue growth, PAT growth, and operating cash flow criteria are met"
    elif loss_making or (revenue_qoq is not None and revenue_qoq < 0) or not data_available:
        classification = "SPECULATIVE"
        cap = 0.3
        reason = "fundamental data is unavailable or does not clear loss/revenue checks"
    else:
        classification = "MOMENTUM"
        cap = 0.6
        reason = "price/volume setup exists but not all fundamental criteria are met"
    return {
        "classification": classification,
        "max_allocation_multiplier": cap,
        "reason": reason,
        "metrics": {
            "revenue_growth_yoy_pct": revenue_growth,
            "pat_growth_yoy_pct": pat_growth,
            "operating_cash_flow_positive": bool(ocf_positive) if ocf_positive is not None else None,
            "revenue_growth_qoq_pct": revenue_qoq,
            "loss_making": loss_making,
            "fundamental_data_available": data_available,
        },
    }


def sentiment_integrity(sentiment: dict[str, Any]) -> dict[str, Any]:
    score = _float_or_none(sentiment.get("score"))
    confidence = _float_or_none(sentiment.get("confidence")) or 0.0
    headline_count = int(sentiment.get("headline_count") or len(sentiment.get("headlines") or []) or 0)
    has_source = headline_count > 0 or bool(sentiment.get("source")) or confidence > 0.0
    missing = score is None or abs(score) < 1e-12 or not has_source
    return {
        "score": score if score is not None else 0.0,
        "status": "DATA_MISSING" if missing else "AVAILABLE",
        "confidence": confidence,
        "headline_count": headline_count,
        "source": sentiment.get("source"),
    }


def earnings_calendar_audit(
    macro_event_context: dict[str, Any],
    macro_calendar_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    context = macro_calendar_context or {}
    updated_at = context.get("updated_at")
    updated_dt = _parse_dt(updated_at)
    stale = updated_dt is None or (datetime.now(timezone.utc) - updated_dt).days > 7
    events = context.get("events") or []
    earnings_events = [event for event in events if isinstance(event, dict) and event.get("type") == "earnings"]
    result_season = is_result_season()
    days = macro_event_context.get("earnings_days_away")
    trading_days = macro_event_context.get("earnings_trading_days_away")
    if trading_days is not None:
        known_block = 0 <= int(trading_days) <= 10
    else:
        known_block = days is not None and 0 <= int(days) <= 14
    empty = "earnings_calendar_empty" in (macro_event_context.get("data_gaps") or []) or not earnings_events
    return {
        "updated_at": updated_at,
        "stale_or_empty": stale or empty,
        "result_season": result_season,
        "earnings_days_away": days,
        "earnings_trading_days_away": trading_days,
        "known_earnings_block": known_block,
        "earnings_events_loaded": len(earnings_events),
    }


def build_position_summary(
    position: dict[str, Any],
    quote: dict[str, Any] | None = None,
    market_health: dict[str, Any] | None = None,
    macro_calendar_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    details = _json_object(position.get("details_json"))
    opened_decision = details.get("opened_from_decision") if isinstance(details.get("opened_from_decision"), dict) else {}
    decision_details = _json_object(opened_decision.get("details_json"))
    if not decision_details:
        decision_details = _json_object(opened_decision.get("details"))
    context = dict(decision_details.get("context") or {})
    if not context and isinstance(details.get("context"), dict):
        context = dict(details.get("context") or {})
    has_original_context = bool(context)
    context["position"] = {
        "symbol": position.get("symbol"),
        "qty": position.get("qty", 0),
        "avg_price": position.get("avg_price", 0),
        "market_price": position.get("market_price", 0),
        "updated_at": position.get("updated_at"),
        "strategy": position.get("strategy"),
    }
    if quote:
        context["quote"] = quote
    if has_original_context:
        audit = evaluate_rules_for_context(
            context,
            {str(position.get("symbol")): position},
            _float_or_none((market_health or {}).get("portfolio_equity")) or 0.0,
            market_health,
            macro_calendar_context,
        )
    else:
        audit = details.get("system_gate_audit") if isinstance(details.get("system_gate_audit"), dict) else {}
        if audit:
            audit = _audit_with_current_price(audit, quote, market_health)
        else:
            audit = evaluate_rules_for_context(
                {"symbol": position.get("symbol"), "quote": quote or {}, "position": context["position"]},
                {str(position.get("symbol")): position},
                _float_or_none((market_health or {}).get("portfolio_equity")) or 0.0,
                market_health,
                macro_calendar_context,
            )
    flags = set(audit.get("active_flags") or [])
    action = "HOLD"
    distribution_sessions = int((audit.get("delivery") or {}).get("distribution_sessions") or 0)
    if "PRICE_MISMATCH" in flags:
        action = "HARD BLOCKED"
    elif "DELIVERY_CONFLICT" in flags and distribution_sessions >= 3:
        action = "EXIT"
    elif "DELIVERY_CONFLICT" in flags:
        action = "TRAIL STOP"
    elif "GRADE_VIOLATION" in flags:
        action = "REVIEW"
    elif audit.get("hard_blocked"):
        action = "HARD BLOCKED"
    reason = _position_reason(action, audit)
    return {
        "symbol": position.get("symbol"),
        "classification": audit["classification"]["classification"],
        "entry_grade": audit["entry"]["entry_grade"],
        "effective_entry_grade": audit["entry"]["effective_entry_grade"],
        "mtf_grade": audit["mtf"]["alignment_grade"],
        "delivery_bias": audit["delivery"]["bias"],
        "sentiment_score": audit["sentiment"]["score"],
        "sentiment_status": audit["sentiment"]["status"],
        "price_label": audit["price"]["label"],
        "price_source": audit["price"]["source"],
        "price_timestamp": audit["price"]["timestamp"],
        "active_flags": audit["active_flags"],
        "overall_score_pct": audit.get("overall_score_pct"),
        "overall_grade": audit.get("overall_grade"),
        "recommended_action": action,
        "reason": reason,
        "rule_audit": audit,
    }


def build_self_audit(
    positions: list[dict[str, Any]],
    quotes: list[dict[str, Any]],
    portfolio: dict[str, Any] | None,
    market_health: dict[str, Any] | None,
    macro_calendar_context: dict[str, Any] | None,
) -> dict[str, Any]:
    quote_map = {str(row.get("symbol")): row for row in quotes}
    equity = _float_or_none((portfolio or {}).get("equity")) or 0.0
    enriched_health = dict(market_health or {})
    enriched_health["portfolio_equity"] = equity
    summaries = [
        build_position_summary(position, quote_map.get(str(position.get("symbol"))), enriched_health, macro_calendar_context)
        for position in positions
    ]
    total = len(summaries)
    speculative = sum(1 for item in summaries if item.get("classification") == "SPECULATIVE")
    position_limit = capital_position_limit(equity)
    overall_scores = [
        float(item.get("overall_score_pct"))
        for item in summaries
        if item.get("overall_score_pct") is not None
    ]
    portfolio_score = round(sum(overall_scores) / len(overall_scores), 1) if overall_scores else 100.0
    if total > position_limit:
        portfolio_score = max(portfolio_score - 12.0, 0.0)
    if speculative == total and total:
        portfolio_score = max(portfolio_score - 10.0, 0.0)
    return {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "overall_score_pct": round(portfolio_score, 1),
        "overall_grade": _score_grade(portfolio_score),
        "grade_violation_count": sum(1 for item in summaries if "GRADE_VIOLATION" in (item.get("active_flags") or [])),
        "delivery_conflict_count": sum(1 for item in summaries if "DELIVERY_CONFLICT" in (item.get("active_flags") or [])),
        "price_mismatch_count": sum(1 for item in summaries if "PRICE_MISMATCH" in (item.get("active_flags") or [])),
        "earnings_calendar_last_updated": (macro_calendar_context or {}).get("updated_at"),
        "speculative_positions": speculative,
        "open_positions": total,
        "speculative_pct_of_open_positions": round((speculative / total) * 100, 2) if total else 0.0,
        "capital_pool_equity": equity,
        "position_limit": position_limit,
        "capital_pool_within_position_count_rule": total <= position_limit,
        "positions": summaries,
    }


def is_result_season(now: datetime | None = None) -> bool:
    now_ist = now or (datetime.now(timezone.utc) + timedelta(hours=5, minutes=30))
    return now_ist.month in RESULT_SEASON_MONTHS


def is_nse_regular_session_now() -> bool:
    now_ist = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
    if now_ist.weekday() >= 5:
        return False
    current_minutes = now_ist.hour * 60 + now_ist.minute
    return (9 * 60 + 15) <= current_minutes <= (15 * 60 + 30)


def _price_label(label: str, ts: Any) -> str:
    parsed = _parse_dt(ts)
    if label == "live" and parsed:
        ist = parsed + timedelta(hours=5, minutes=30)
        return f"live [{ist.strftime('%H:%M IST')}]"
    if parsed:
        ist = parsed + timedelta(hours=5, minutes=30)
        return f"{label} [{ist.date().isoformat()}]"
    return label


def _position_reason(action: str, audit: dict[str, Any]) -> str:
    if action == "HARD BLOCKED":
        block = (audit.get("hard_blocks") or [{}])[0]
        return str(block.get("reason") or "hard gate failed")
    if action == "TRAIL STOP":
        return "Delivery distribution conflicts with the long thesis; trail stop and do not add."
    if action == "EXIT":
        return "Delivery conflict has persisted for 3 sessions; exit recommendation is triggered."
    if action == "REVIEW":
        flags = set(audit.get("active_flags") or [])
        reasons: list[str] = []
        if "GRADE_VIOLATION" in flags:
            reasons.append("entry grade is WATCH or missing")
        if "SECTOR_MISSING" in flags:
            reasons.append("sector or industry classification is missing")
        if "SUSPECT_BREAKOUT" in flags:
            reasons.append("breakout confirmation is suspect")
        if (audit.get("sentiment") or {}).get("status") == "DATA_MISSING":
            reasons.append("sentiment data is missing")
        if (audit.get("classification") or {}).get("classification") == "SPECULATIVE":
            reasons.append("fundamental proof is missing or weak")
        if reasons:
            return "Review required: " + "; ".join(reasons[:3]) + "."
        return "Review required before any fresh allocation."
    return "No hard block detected; continue monitoring stop, targets, and event risk."


def rule_quality_score_pct(audit: dict[str, Any]) -> float:
    """Human-readable production readiness score, not a profit probability."""
    score = 100.0
    flags = set(audit.get("active_flags") or [])
    flag_penalties = {
        "PRICE_MISMATCH": 50.0,
        "GRADE_VIOLATION": 35.0,
        "DELIVERY_CONFLICT": 25.0,
        "SUSPECT_BREAKOUT": 15.0,
        "SECTOR_MISSING": 10.0,
        "MTF_HARD_BLOCK": 30.0,
        "EARNINGS_LOCKOUT": 35.0,
        "POSITION_COUNT_LIMIT": 25.0,
    }
    for flag in flags:
        score -= flag_penalties.get(flag, 8.0)
    if audit.get("hard_blocked"):
        score -= 20.0

    classification = (audit.get("classification") or {}).get("classification")
    if classification == "SPECULATIVE":
        score -= 25.0
    elif classification == "MOMENTUM":
        score -= 12.0

    sentiment = audit.get("sentiment") or {}
    if sentiment.get("status") == "DATA_MISSING":
        score -= 12.0

    entry = audit.get("entry") or {}
    effective_entry = entry.get("effective_entry_grade")
    if effective_entry == "A":
        score += 4.0
    elif effective_entry == "C":
        score -= 8.0
    elif effective_entry == "WATCH":
        score -= 20.0
    elif not effective_entry:
        score -= 18.0

    mtf = (audit.get("mtf") or {}).get("alignment_grade")
    if mtf == "A":
        score += 4.0
    elif mtf == "C":
        score -= 8.0
    elif mtf == "D":
        score -= 25.0
    elif not mtf:
        score -= 6.0

    delivery = audit.get("delivery") or {}
    if delivery.get("bias") == "accumulation":
        score += 4.0
    elif delivery.get("bias") == "distribution":
        score -= 12.0

    if audit.get("institutional_quality_allowed"):
        score += 8.0
    return round(max(min(score, 100.0), 0.0), 1)


def _score_grade(score: float | int | None) -> str:
    value = float(score or 0.0)
    if value >= 85:
        return "A"
    if value >= 70:
        return "B"
    if value >= 55:
        return "C"
    if value >= 40:
        return "D"
    return "F"


def _delivery_distribution_sessions(delivery: dict[str, Any]) -> int:
    payload = delivery.get("score_payload") if isinstance(delivery.get("score_payload"), dict) else {}
    nested = payload.get("trend") if isinstance(payload.get("trend"), dict) else {}
    for value in (
        payload.get("distribution_streak"),
        delivery.get("distribution_streak"),
        delivery.get("distribution_days"),
        nested.get("distribution_days"),
    ):
        try:
            return max(int(value), 0)
        except (TypeError, ValueError):
            continue
    return 1


def _independent_reference(quote: dict[str, Any], primary_source: str) -> tuple[float | None, str | None]:
    references = quote.get("independent_references") or quote.get("price_sources") or {}
    if isinstance(references, dict):
        for ref_source, ref_value in references.items():
            if str(ref_source) == primary_source:
                continue
            numeric = _float_or_none(ref_value.get("price") if isinstance(ref_value, dict) else ref_value)
            if numeric is not None and numeric > 0:
                return numeric, str(ref_source)
    reference = _float_or_none(quote.get("reference_price"))
    reference_source = quote.get("reference_source")
    if reference is not None and reference > 0 and reference_source and str(reference_source) != primary_source:
        return reference, str(reference_source)
    return None, None


def _audit_with_current_price(
    audit: dict[str, Any],
    quote: dict[str, Any] | None,
    market_health: dict[str, Any] | None,
) -> dict[str, Any]:
    output = dict(audit)
    if quote:
        price = price_integrity(quote, market_health)
        output["price"] = price
        flags = list(output.get("active_flags") or [])
        hard_blocks = list(output.get("hard_blocks") or [])
        if price.get("price_mismatch") and "PRICE_MISMATCH" not in flags:
            flags.append("PRICE_MISMATCH")
            hard_blocks.append({
                "flag": "PRICE_MISMATCH",
                "reason": "two available price references differ by more than 1%",
                "value": price,
            })
        output["active_flags"] = _unique(flags)
        output["hard_blocks"] = hard_blocks
        output["hard_blocked"] = bool(hard_blocks)
    return output


def _first_float(data: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = _float_or_none(data.get(key))
        if value is not None:
            return value
    return None


def _float_or_none(value: Any) -> float | None:
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _json_object(value: Any) -> dict[str, Any]:
    try:
        parsed = value if isinstance(value, dict) else __import__("json").loads(value or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _unique(items: list[str]) -> list[str]:
    output: list[str] = []
    for item in items:
        if item and item not in output:
            output.append(item)
    return output
