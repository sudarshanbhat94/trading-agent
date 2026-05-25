from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from typing import Any

from .full_spectrum import _stage_analysis
from .market_regions import market_region_for_row
from .models import Candle, Quote, utc_now
from .strategy_presets import evaluate_strategy_presets


PRE_CATALYST_WATCH = "PRE_CATALYST_WATCH"
EARNINGS_VCP_BREAKOUT = "EARNINGS_VCP_BREAKOUT"
OVERHANG_REMOVAL_RERATE = "OVERHANG_REMOVAL_RERATE"
SECTOR_ROTATION_LEADER = "SECTOR_ROTATION_LEADER"
LOW_QUALITY_SHORT_COVERING = "LOW_QUALITY_SHORT_COVERING"
LATE_CHASE_AVOID = "LATE_CHASE_AVOID"


@dataclass(frozen=True)
class OpportunityCandidate:
    symbol: str
    label: str
    confidence: float
    score: float
    market_region: str
    catalyst_type: str
    catalyst_date: str | None
    setup_summary: str
    entry_zone: dict[str, float | None]
    pivot: float | None
    invalidation_level: float | None
    key_reasons: list[str]
    supporting_signals: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_pre_catalyst_watchlist(
    universe: list[dict[str, Any]],
    quotes: dict[str, Quote],
    candle_sets: dict[str, dict[str, list[Candle]]],
    *,
    sentiment_by_symbol: dict[str, dict[str, Any]] | None = None,
    macro_calendar_context: dict[str, Any] | None = None,
    sector_rotation_context: dict[str, Any] | None = None,
    macro_context: dict[str, Any] | None = None,
    market_action_summary: dict[str, Any] | None = None,
    previous_state: dict[str, Any] | None = None,
    settings: Any | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build a deterministic two-layer discovery view.

    Layer 1 creates pre-catalyst watch candidates from existing setup, stage,
    sentiment, sector, and calendar evidence. Layer 2 promotes or blocks those
    candidates when live market-action evidence arrives.
    """

    if settings is not None and not bool(getattr(settings, "pre_catalyst_engine_enabled", True)):
        return {"enabled": False, "reason": "pre_catalyst_engine_disabled", "candidates": [], "live_confirmations": []}

    sentiment_by_symbol = sentiment_by_symbol or {}
    macro_calendar_context = macro_calendar_context or {}
    sector_rotation_context = sector_rotation_context or {}
    macro_context = macro_context or {}
    market_action_summary = market_action_summary or {}
    previous_state = previous_state or {}
    now = now or datetime.now(timezone.utc)
    candidate_limit = max(1, int(getattr(settings, "pre_catalyst_candidate_limit", 40) if settings is not None else 40) or 40)
    min_score = _clamp(
        float(getattr(settings, "pre_catalyst_min_score", 0.56) if settings is not None else 0.56),
        0.0,
        1.0,
    )

    calendar = enrich_catalyst_calendar(
        universe,
        macro_calendar_context=macro_calendar_context,
        sentiment_by_symbol=sentiment_by_symbol,
        previous_state=previous_state.get("calendar_enrichment") if isinstance(previous_state, dict) else {},
        now=now,
    )
    rs_profiles = _relative_strength_profiles(universe, candle_sets)
    sector_leaders = detect_sector_rotation_leaders(
        universe,
        quotes,
        candle_sets,
        macro_context=macro_context,
        sector_rotation_context=sector_rotation_context,
        rs_profiles=rs_profiles,
    )
    market_events = _events_by_symbol(market_action_summary)
    previous_candidates = {
        str(item.get("symbol") or "").upper(): item
        for item in (previous_state.get("candidates") if isinstance(previous_state, dict) else []) or []
        if isinstance(item, dict)
    }

    candidates: list[OpportunityCandidate] = []
    log_events: list[dict[str, Any]] = []
    missing_history = 0
    missing_quote = 0
    data_gaps: dict[str, int] = {}

    for row in universe:
        symbol = str(row.get("symbol") or "").upper()
        if not symbol:
            continue
        quote = quotes.get(symbol)
        if not quote:
            missing_quote += 1
            continue
        candles = _analysis_candles(candle_sets.get(symbol) or {})
        if len(candles) < 30:
            missing_history += 1
            _count(data_gaps, "insufficient_history")
            continue

        sentiment = sentiment_by_symbol.get(symbol) or {}
        setup = _setup_profile(candles, quote)
        stage = _stage_analysis(candles, quote.price, {})
        catalyst = calendar["by_symbol"].get(symbol) or _missing_calendar(symbol)
        overhang = detect_overhang_removal(row, quote, candles, sentiment)
        sector_leader = sector_leaders.get(symbol) or {}
        short_covering = detect_short_covering_bounce(row, quote, candles, sentiment, market_events.get(symbol))
        score_profile = _pre_catalyst_score(
            row=row,
            quote=quote,
            candles=candles,
            setup=setup,
            stage=stage,
            catalyst=catalyst,
            sentiment=sentiment,
            rs=rs_profiles.get(symbol) or {},
            sector_leader=sector_leader,
            overhang=overhang,
            short_covering=short_covering,
            settings=settings,
        )
        label = classify_opportunity(
            setup=setup,
            catalyst=catalyst,
            overhang=overhang,
            sector_leader=sector_leader,
            short_covering=short_covering,
            live_confirmation=None,
            score=score_profile["score"],
            min_score=min_score,
        )
        if label == "" or (score_profile["score"] < min_score and not overhang.get("detected") and not sector_leader.get("detected") and not short_covering.get("detected")):
            continue

        candidate = _candidate_from_parts(
            row=row,
            quote=quote,
            label=label,
            score_profile=score_profile,
            setup=setup,
            catalyst=catalyst,
            stage=stage,
            sentiment=sentiment,
            rs=rs_profiles.get(symbol) or {},
            sector_leader=sector_leader,
            overhang=overhang,
            short_covering=short_covering,
        )
        candidates.append(candidate)
        log_events.append({"event": "watchlist_candidate", "symbol": symbol, "label": label, "reasons": candidate.key_reasons[:5]})

    candidates.sort(key=lambda item: (item.score, item.confidence), reverse=True)
    candidates = candidates[:candidate_limit]
    live_confirmations: list[dict[str, Any]] = []
    for candidate in candidates:
        symbol = candidate.symbol
        quote = quotes.get(symbol)
        if not quote:
            continue
        live = confirm_live_breakout(
            candidate.to_dict(),
            quote,
            candle_sets.get(symbol) or {},
            market_events.get(symbol),
            sentiment_by_symbol.get(symbol) or {},
        )
        if live.get("label") and live.get("label") != PRE_CATALYST_WATCH:
            live_confirmations.append(live)

    current_symbols = {candidate.symbol for candidate in candidates}
    previous_symbols = set(previous_candidates)
    for symbol in sorted(current_symbols - previous_symbols):
        current = next((item for item in candidates if item.symbol == symbol), None)
        log_events.append({"event": "entered_watchlist", "symbol": symbol, "label": current.label if current else "", "reasons": (current.key_reasons if current else [])[:5]})
    for symbol in sorted(previous_symbols - current_symbols):
        prior = previous_candidates.get(symbol) or {}
        log_events.append({"event": "exited_watchlist", "symbol": symbol, "previous_label": prior.get("label"), "reason": "no_longer_meets_pre_catalyst_score_or_data_requirements"})

    payload = {
        "enabled": True,
        "source": "pre_catalyst_engine",
        "generated_at": utc_now(),
        "mode": "two_layer_pre_catalyst_and_live_confirmation",
        "raw_symbols": len(universe),
        "quoted_symbols": len(quotes),
        "symbols_with_history": len(universe) - missing_history,
        "missing_quote_symbols": missing_quote,
        "missing_history_symbols": missing_history,
        "candidate_limit": candidate_limit,
        "min_score": min_score,
        "candidates": [candidate.to_dict() for candidate in candidates],
        "live_confirmations": live_confirmations,
        "calendar_enrichment": calendar,
        "sector_rotation_leaders": list(sector_leaders.values())[:candidate_limit],
        "label_counts": _counts([candidate.label for candidate in candidates] + [item.get("label") for item in live_confirmations]),
        "data_gaps": data_gaps,
        "log_events": log_events[-80:],
    }
    return payload


def enrich_catalyst_calendar(
    universe: list[dict[str, Any]],
    *,
    macro_calendar_context: dict[str, Any] | None = None,
    sentiment_by_symbol: dict[str, dict[str, Any]] | None = None,
    previous_state: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    macro_calendar_context = macro_calendar_context or {}
    sentiment_by_symbol = sentiment_by_symbol or {}
    previous_state = previous_state or {}
    now = now or datetime.now(timezone.utc)
    today = now.date()
    stored = previous_state.get("earnings_by_symbol") if isinstance(previous_state, dict) else {}
    earnings_by_symbol: dict[str, dict[str, Any]] = {
        str(symbol).upper(): dict(value)
        for symbol, value in (stored or {}).items()
        if isinstance(value, dict)
    }

    for event in macro_calendar_context.get("events") or []:
        if not isinstance(event, dict):
            continue
        event_type = str(event.get("type") or "").lower()
        if event_type not in {"earnings", "result", "results"}:
            continue
        event_date = _parse_date(event.get("date"))
        symbols = event.get("symbols") if isinstance(event.get("symbols"), list) else []
        scope = str(event.get("scope") or "").upper()
        if scope and scope != "MARKET_WIDE":
            symbols = [*symbols, scope]
        for symbol in symbols:
            normalized = str(symbol or "").upper()
            if not normalized:
                continue
            days = (event_date - today).days if event_date else None
            earnings_by_symbol[normalized] = {
                "available": bool(event_date),
                "catalyst_type": "earnings",
                "catalyst_date": event_date.isoformat() if event_date else None,
                "days_to_catalyst": days,
                "source": "macro_calendar",
                "data_gap": None if event_date else "earnings_date_missing",
            }

    by_symbol: dict[str, dict[str, Any]] = {}
    missing = 0
    inferred_recent = 0
    for row in universe:
        symbol = str(row.get("symbol") or "").upper()
        if not symbol:
            continue
        known = dict(earnings_by_symbol.get(symbol) or {})
        if known:
            by_symbol[symbol] = known
            continue
        inferred = _infer_catalyst_from_sentiment(sentiment_by_symbol.get(symbol) or {})
        if inferred:
            by_symbol[symbol] = inferred
            inferred_recent += 1
            continue
        by_symbol[symbol] = _missing_calendar(symbol)
        missing += 1

    return {
        "enabled": True,
        "source": "macro_calendar+sentiment+persistent_state",
        "updated_at": utc_now(),
        "status": "ok" if missing == 0 else "partial",
        "known_earnings_symbols": sum(1 for item in by_symbol.values() if item.get("catalyst_type") == "earnings" and item.get("catalyst_date")),
        "inferred_recent_catalyst_symbols": inferred_recent,
        "missing_earnings_symbols": missing,
        "data_gaps": [] if missing == 0 else ["earnings_calendar_missing_for_some_symbols"],
        "earnings_by_symbol": earnings_by_symbol,
        "by_symbol": by_symbol,
    }


def detect_overhang_removal(
    row: dict[str, Any],
    quote: Quote,
    candles: list[Candle],
    sentiment: dict[str, Any] | None,
) -> dict[str, Any]:
    sentiment = sentiment or {}
    closes = [float(candle.close) for candle in candles if candle.close]
    if len(closes) < 45:
        return {"detected": False, "score": 0.0}
    ret_63 = _return_pct(closes, 63)
    ret_126 = _return_pct(closes, 126)
    sma50 = _sma(closes, 50)
    sma200 = _sma(closes, 200)
    weak_trend = (ret_63 is not None and ret_63 <= -8.0) or (ret_126 is not None and ret_126 <= -12.0) or bool(sma50 and quote.price < sma50)
    below_major = bool(sma200 and quote.price < sma200)
    events = [event for event in sentiment.get("events") or [] if isinstance(event, dict)]
    text = " ".join(
        [
            str(sentiment.get("headlines") or ""),
            " ".join(str(event.get("title") or event.get("headline") or event.get("summary") or "") for event in events),
        ]
    ).lower()
    has_overhang = any(token in text for token in ("probe", "lawsuit", "litigation", "fraud", "governance", "regulatory", "penalty", "charges", "debt"))
    has_resolution = any(token in text for token in ("dropped", "dismissed", "settled", "settlement", "approved", "cleared", "relief", "resolved", "resolution", "withdrawn"))
    positive_legal = any(
        str(event.get("event_type") or "").lower() in {"legal_regulatory", "fraud_governance"}
        and float(event.get("score") or 0.0) > 0.15
        for event in events
    )
    detected = bool(weak_trend and has_overhang and (has_resolution or positive_legal))
    score = 0.0
    if detected:
        score = 0.56 + (0.12 if has_resolution else 0.0) + (0.08 if below_major else 0.0)
    return {
        "detected": detected,
        "score": round(_clamp(score, 0.0, 1.0), 4),
        "weak_trend": weak_trend,
        "below_major_average": below_major,
        "has_overhang_language": has_overhang,
        "has_resolution_language": has_resolution,
        "ret_63_pct": _round(ret_63),
        "ret_126_pct": _round(ret_126),
    }


def detect_sector_rotation_leaders(
    universe: list[dict[str, Any]],
    quotes: dict[str, Quote],
    candle_sets: dict[str, dict[str, list[Candle]]],
    *,
    macro_context: dict[str, Any] | None = None,
    sector_rotation_context: dict[str, Any] | None = None,
    rs_profiles: dict[str, dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    macro_context = macro_context or {}
    sector_rotation_context = sector_rotation_context or {}
    rs_profiles = rs_profiles or _relative_strength_profiles(universe, candle_sets)
    drivers = _macro_beneficiary_drivers(macro_context)
    if not drivers and not sector_rotation_context:
        return {}

    sector_returns: dict[str, list[float]] = {}
    for row in universe:
        symbol = str(row.get("symbol") or "").upper()
        sector = _sector(row)
        rs = rs_profiles.get(symbol) or {}
        ret = _float_or_none(rs.get("return_20_pct"))
        if sector and ret is not None:
            sector_returns.setdefault(sector, []).append(ret)
    sector_scores = {
        sector: sum(values) / len(values)
        for sector, values in sector_returns.items()
        if values
    }
    output: dict[str, dict[str, Any]] = {}
    for row in universe:
        symbol = str(row.get("symbol") or "").upper()
        quote = quotes.get(symbol)
        if not symbol or not quote:
            continue
        sector = _sector(row)
        rs = rs_profiles.get(symbol) or {}
        percentile = float(rs.get("percentile_63") or 0.0)
        sector_context = _symbol_sector_context(symbol, sector, sector_rotation_context)
        matched_drivers = [driver for driver in drivers if _sector_matches_driver(sector, driver)]
        sector_tailwind = bool(sector_context.get("sector_tailwind")) or bool(matched_drivers)
        if not sector_tailwind:
            continue
        sector_score = float(sector_context.get("sector_rotation_score") or 0.0)
        if not sector_score:
            sector_score = _clamp((sector_scores.get(sector, 0.0) or 0.0) / 12.0, -1.0, 1.0)
        if percentile < 65 and sector_score < 0.2:
            continue
        output[symbol] = {
            "detected": True,
            "symbol": symbol,
            "sector": sector,
            "score": round(_clamp(0.42 + percentile / 200.0 + max(sector_score, 0.0) * 0.22, 0.0, 1.0), 4),
            "sector_rotation_score": round(sector_score, 4),
            "rs_percentile_63": round(percentile, 2),
            "drivers": matched_drivers or ["sector_rotation_tailwind"],
            "reason": f"{sector} leadership with RS percentile {percentile:.0f}",
        }
    return output


def detect_short_covering_bounce(
    row: dict[str, Any],
    quote: Quote,
    candles: list[Candle],
    sentiment: dict[str, Any] | None,
    market_action: dict[str, Any] | None = None,
) -> dict[str, Any]:
    sentiment = sentiment or {}
    market_action = market_action or {}
    closes = [float(candle.close) for candle in candles if candle.close]
    if len(closes) < 35:
        return {"detected": False, "score": 0.0}
    sma50 = _sma(closes, 50)
    sma200 = _sma(closes, 200)
    below_major = bool((sma50 and quote.price < sma50) or (sma200 and quote.price < sma200))
    ret_63 = _return_pct(closes, 63)
    day_gain = _day_gain_pct(quote)
    events = [event for event in sentiment.get("events") or [] if isinstance(event, dict)]
    text = " ".join(
        [
            str(sentiment.get("headlines") or ""),
            " ".join(str(event.get("title") or event.get("headline") or event.get("summary") or "") for event in events),
        ]
    ).lower()
    negative_tone = (
        float(sentiment.get("score") or 0.0) <= -0.12
        or any(str(event.get("event_type") or "").lower() in {"analyst_downgrade", "debt_liquidity", "fraud_governance"} for event in events)
        or any(token in text for token in ("sell rating", "downgrade", "cash burn", "weak demand", "market share loss", "debt", "default"))
    )
    squeeze_hint = bool(row.get("short_interest") or row.get("short_float_pct") or market_action.get("strategy") == "top_gainer_momentum")
    weak_prior = (ret_63 is not None and ret_63 <= -12.0) or below_major
    detected = bool(weak_prior and negative_tone and (day_gain >= 3.0 or squeeze_hint))
    score = 0.0
    if detected:
        score = 0.62 + (0.08 if day_gain >= 5.0 else 0.0) + (0.06 if squeeze_hint else 0.0)
    return {
        "detected": detected,
        "score": round(_clamp(score, 0.0, 1.0), 4),
        "weak_prior_trend": weak_prior,
        "below_major_average": below_major,
        "negative_tone": negative_tone,
        "squeeze_hint": squeeze_hint,
        "day_gain_pct": _round(day_gain),
        "ret_63_pct": _round(ret_63),
        "position_size_hint": "tiny_only",
    }


def confirm_live_breakout(
    candidate: dict[str, Any],
    quote: Quote,
    candle_set: dict[str, list[Candle]],
    market_action: dict[str, Any] | None = None,
    sentiment: dict[str, Any] | None = None,
) -> dict[str, Any]:
    market_action = market_action or {}
    sentiment = sentiment or {}
    daily = candle_set.get("daily") or candle_set.get("analysis") or []
    intraday = candle_set.get("intraday") or []
    pivot = _float_or_none(candidate.get("pivot"))
    if pivot is None:
        pivot = _setup_features(daily, quote).get("pivot")
    pivot = _float_or_none(pivot)
    day_gain = _day_gain_pct(quote)
    gap_pct = _gap_pct(quote, daily)
    extension = ((quote.price - pivot) / pivot) * 100.0 if pivot and pivot > 0 else 0.0
    volume_ratio = _live_volume_ratio(quote, daily)
    vwap = _vwap(intraday)
    vwap_hold = bool(vwap and quote.price >= vwap) if vwap else _range_position(quote) >= 0.60
    range_hold = _first_range_hold(intraday, quote.price)
    market_action_events = [str(item).upper() for item in market_action.get("event_types", []) if str(item or "").strip()]
    breakout = bool((pivot and quote.price >= pivot) or "52_WEEK_HIGH" in market_action_events or "VOLUME_SHOCKER" in market_action_events)
    volume_confirmed = bool(volume_ratio >= 1.35 or "VOLUME_SHOCKER" in market_action_events)
    catalyst_confirmed = _sentiment_has_positive_catalyst(sentiment) or bool(market_action_events)
    too_extended = extension > 5.0 or day_gain >= 8.0

    if too_extended and breakout:
        label = LATE_CHASE_AVOID
    elif candidate.get("label") == LOW_QUALITY_SHORT_COVERING:
        label = LOW_QUALITY_SHORT_COVERING
    elif breakout and vwap_hold and range_hold and volume_confirmed and catalyst_confirmed:
        if candidate.get("catalyst_type") == "earnings":
            label = EARNINGS_VCP_BREAKOUT
        elif candidate.get("label") == OVERHANG_REMOVAL_RERATE:
            label = OVERHANG_REMOVAL_RERATE
        elif candidate.get("label") == SECTOR_ROTATION_LEADER:
            label = SECTOR_ROTATION_LEADER
        else:
            label = EARNINGS_VCP_BREAKOUT if _sentiment_has_event(sentiment, "earnings") else PRE_CATALYST_WATCH
    else:
        label = PRE_CATALYST_WATCH

    confirmation_score = _clamp(
        (0.22 if breakout else 0.0)
        + (0.18 if vwap_hold else 0.0)
        + (0.14 if range_hold else 0.0)
        + (0.20 if volume_confirmed else 0.0)
        + (0.16 if catalyst_confirmed else 0.0)
        + (0.10 if not too_extended else -0.16),
        0.0,
        1.0,
    )
    reasons = []
    if breakout:
        reasons.append("breakout or market-action event active")
    if vwap_hold:
        reasons.append("VWAP/range hold active")
    if range_hold:
        reasons.append("first range hold active")
    if volume_confirmed:
        reasons.append("volume confirmation active")
    if too_extended:
        reasons.append("late chase risk; too extended from pivot")
    return {
        "symbol": candidate.get("symbol"),
        "label": label,
        "confidence": round(min(float(candidate.get("confidence") or 0.0) * 0.45 + confirmation_score * 0.55, 1.0), 4),
        "score": round(confirmation_score, 4),
        "pivot": _round(pivot),
        "day_gain_pct": _round(day_gain),
        "gap_pct": _round(gap_pct),
        "extension_from_pivot_pct": _round(extension),
        "volume_ratio": _round(volume_ratio),
        "vwap": _round(vwap),
        "breakout": breakout,
        "vwap_hold": vwap_hold,
        "first_range_hold": range_hold,
        "volume_confirmed": volume_confirmed,
        "catalyst_confirmed": catalyst_confirmed,
        "key_reasons": reasons,
        "source_candidate": candidate,
    }


def classify_opportunity(
    *,
    setup: dict[str, Any],
    catalyst: dict[str, Any],
    overhang: dict[str, Any],
    sector_leader: dict[str, Any],
    short_covering: dict[str, Any],
    live_confirmation: dict[str, Any] | None,
    score: float,
    min_score: float,
) -> str:
    if short_covering.get("detected"):
        return LOW_QUALITY_SHORT_COVERING
    if live_confirmation and live_confirmation.get("label") == LATE_CHASE_AVOID:
        return LATE_CHASE_AVOID
    if live_confirmation and live_confirmation.get("label"):
        return str(live_confirmation["label"])
    if overhang.get("detected"):
        return OVERHANG_REMOVAL_RERATE
    if sector_leader.get("detected") and score >= min_score:
        return SECTOR_ROTATION_LEADER
    if catalyst.get("catalyst_type") == "earnings" and setup.get("pre_catalyst_ready") and score >= min_score:
        return PRE_CATALYST_WATCH
    if setup.get("pre_catalyst_ready") and score >= min_score:
        return PRE_CATALYST_WATCH
    return ""


def _candidate_from_parts(
    *,
    row: dict[str, Any],
    quote: Quote,
    label: str,
    score_profile: dict[str, Any],
    setup: dict[str, Any],
    catalyst: dict[str, Any],
    stage: dict[str, Any],
    sentiment: dict[str, Any],
    rs: dict[str, Any],
    sector_leader: dict[str, Any],
    overhang: dict[str, Any],
    short_covering: dict[str, Any],
) -> OpportunityCandidate:
    symbol = str(row.get("symbol") or "").upper()
    pivot = _float_or_none(setup.get("pivot"))
    entry_zone = {
        "low": _round(pivot * 0.995) if pivot else None,
        "high": _round(pivot * 1.02) if pivot else None,
    }
    invalidation = _float_or_none(setup.get("invalidation_level"))
    reasons: list[str] = []
    reasons.extend(score_profile.get("reasons") or [])
    if catalyst.get("catalyst_type") == "earnings":
        if catalyst.get("catalyst_date"):
            reasons.append(f"earnings/result date {catalyst['catalyst_date']}")
        else:
            reasons.append("recent earnings/results catalyst inferred from news")
    if overhang.get("detected"):
        reasons.append("overhang removal/re-rate pattern detected")
    if sector_leader.get("detected"):
        reasons.append(sector_leader.get("reason") or "sector rotation leader")
    if short_covering.get("detected"):
        reasons.append("low-quality bounce; conservative watch only")
    setup_summary = (
        "tight VCP/base near pivot"
        if setup.get("tight_base") or setup.get("progressive_contraction")
        else "pre-catalyst technical setup"
    )
    return OpportunityCandidate(
        symbol=symbol,
        label=label,
        confidence=round(_clamp(score_profile["score"] * 0.88 + score_profile.get("evidence_quality", 0.0) * 0.12, 0.0, 1.0), 4),
        score=round(score_profile["score"], 4),
        market_region=market_region_for_row(row),
        catalyst_type=str(catalyst.get("catalyst_type") or "unknown"),
        catalyst_date=catalyst.get("catalyst_date"),
        setup_summary=setup_summary,
        entry_zone=entry_zone,
        pivot=_round(pivot),
        invalidation_level=_round(invalidation),
        key_reasons=_unique(reasons)[:8],
        supporting_signals={
            "setup": setup,
            "stage": {
                "stage": stage.get("stage"),
                "stage_confidence": stage.get("stage_confidence"),
                "buy_permitted": stage.get("buy_permitted"),
            },
            "relative_strength": rs,
            "sector_rotation": sector_leader,
            "sentiment": _sentiment_summary(sentiment),
            "overhang_removal": overhang,
            "short_covering": short_covering,
            "score_components": score_profile.get("components"),
        },
    )


def _pre_catalyst_score(
    *,
    row: dict[str, Any],
    quote: Quote,
    candles: list[Candle],
    setup: dict[str, Any],
    stage: dict[str, Any],
    catalyst: dict[str, Any],
    sentiment: dict[str, Any],
    rs: dict[str, Any],
    sector_leader: dict[str, Any],
    overhang: dict[str, Any],
    short_covering: dict[str, Any],
    settings: Any | None,
) -> dict[str, Any]:
    market_region = market_region_for_row(row)
    min_turnover = (
        float(getattr(settings, "dynamic_scan_min_turnover_usd", 2_000_000.0) or 2_000_000.0)
        if market_region == "US" and settings is not None
        else float(getattr(settings, "dynamic_scan_min_turnover_inr", 50_000_000.0) or 50_000_000.0)
        if settings is not None
        else 2_000_000.0 if market_region == "US" else 50_000_000.0
    )
    catalyst_score = _catalyst_proximity_score(catalyst)
    setup_score = float(setup.get("score") or 0.0)
    dryup_score = 1.0 if setup.get("volume_dryup") else 0.0
    rs_score = _clamp(float(rs.get("percentile_63") or 0.0) / 100.0, 0.0, 1.0)
    sector_score = _clamp(float(sector_leader.get("score") or 0.0), 0.0, 1.0)
    turnover = float(quote.price or 0.0) * float(quote.volume or 0.0)
    liquidity = _clamp(turnover / max(min_turnover * 3.0, 1.0), 0.0, 1.0)
    extension_score = _extension_score(setup)
    news_quality = _news_quality_score(sentiment)
    if overhang.get("detected"):
        news_quality = max(news_quality, float(overhang.get("score") or 0.0))
    if short_covering.get("detected"):
        news_quality = min(news_quality, 0.25)
    stage_score = 1.0 if stage.get("buy_permitted") else 0.45 if stage.get("stage") == "Stage1_Base" else 0.15
    score = (
        catalyst_score * 0.18
        + setup_score * 0.20
        + dryup_score * 0.11
        + rs_score * 0.14
        + sector_score * 0.11
        + liquidity * 0.10
        + extension_score * 0.10
        + news_quality * 0.10
        + stage_score * 0.06
    )
    if overhang.get("detected"):
        score = max(score, float(overhang.get("score") or 0.0))
    if sector_leader.get("detected"):
        score = max(score, 0.58 + sector_score * 0.18)
    if short_covering.get("detected"):
        score = max(score, float(short_covering.get("score") or 0.0))
    reasons = []
    if setup.get("progressive_contraction"):
        reasons.append("base contraction")
    if setup.get("volume_dryup"):
        reasons.append("volume dry-up")
    if setup.get("near_pivot"):
        reasons.append("near pivot without extension")
    if rs_score >= 0.7:
        reasons.append("rising relative strength")
    if sector_score >= 0.55:
        reasons.append("sector rotation support")
    if liquidity >= 0.5:
        reasons.append("liquidity pass")
    if news_quality >= 0.45:
        reasons.append("news/catalyst quality support")
    if catalyst_score >= 0.75:
        reasons.append("near known catalyst window")
    return {
        "score": round(_clamp(score, 0.0, 1.0), 4),
        "evidence_quality": round(_clamp((setup_score + rs_score + liquidity + news_quality) / 4.0, 0.0, 1.0), 4),
        "components": {
            "catalyst_proximity": round(catalyst_score, 4),
            "setup_quality": round(setup_score, 4),
            "volume_dryup": round(dryup_score, 4),
            "relative_strength": round(rs_score, 4),
            "sector_strength": round(sector_score, 4),
            "liquidity": round(liquidity, 4),
            "extension_from_pivot": round(extension_score, 4),
            "news_quality": round(news_quality, 4),
            "stage": round(stage_score, 4),
        },
        "reasons": reasons,
    }


def _setup_profile(candles: list[Candle], quote: Quote) -> dict[str, Any]:
    strategy_signals = evaluate_strategy_presets(candles, quote.price)
    by_name = {signal.name: signal for signal in strategy_signals}
    features = _setup_features(candles, quote)
    signal_score = max(
        float((by_name.get(name).score if by_name.get(name) else 0.0) or 0.0)
        for name in ("vcp_breakout", "minervini_trend_template", "darvas_box_breakout", "aggressive_relative_strength_breakout")
    )
    score = max(
        signal_score,
        0.0
        + (0.24 if features.get("progressive_contraction") else 0.0)
        + (0.20 if features.get("volume_dryup") else 0.0)
        + (0.18 if features.get("tight_base") else 0.0)
        + (0.18 if features.get("near_pivot") else 0.0)
        + (0.12 if not features.get("extended_from_pivot") else -0.10),
    )
    return {
        **features,
        "score": round(_clamp(score, 0.0, 1.0), 4),
        "strategy_scores": {signal.name: signal.score for signal in strategy_signals},
        "strategy_notes": {signal.name: signal.notes[:4] for signal in strategy_signals if signal.name in {"vcp_breakout", "minervini_trend_template", "darvas_box_breakout", "aggressive_relative_strength_breakout"}},
        "pre_catalyst_ready": bool(
            features.get("near_pivot")
            and not features.get("extended_from_pivot")
            and (features.get("progressive_contraction") or features.get("volume_dryup") or signal_score >= 0.55)
        ),
    }


def _setup_features(candles: list[Candle], quote: Quote) -> dict[str, Any]:
    if len(candles) < 30:
        return {"available": False, "score": 0.0}
    base = candles[-65:] if len(candles) >= 65 else candles[-45:] if len(candles) >= 45 else candles[-30:]
    setup = base[:-1] if len(base) > 1 else base
    thirds = _split_evenly(setup, 3)
    contraction_ranges = [_base_range_pct(segment) for segment in thirds]
    progressive = (
        len(contraction_ranges) == 3
        and all(value is not None for value in contraction_ranges)
        and contraction_ranges[1] <= contraction_ranges[0] * 0.9
        and contraction_ranges[2] <= contraction_ranges[1] * 0.9
    )
    highs = [candle.high for candle in setup if candle.high]
    lows = [candle.low for candle in setup if candle.low]
    if not highs or not lows:
        return {"available": False, "score": 0.0}
    pivot = max(highs)
    base_low = min(lows)
    base_width = ((pivot - base_low) / base_low) * 100.0 if base_low else 100.0
    early_volume = _mean(candle.volume for candle in setup[: max(10, len(setup) // 3)])
    late_volume = _mean(candle.volume for candle in setup[-10:])
    volume_dryup = bool(early_volume) and late_volume <= early_volume * 0.78
    distance_to_pivot = ((pivot - quote.price) / pivot) * 100.0 if pivot else 100.0
    extension = ((quote.price - pivot) / pivot) * 100.0 if pivot else 0.0
    near_pivot = -2.0 <= distance_to_pivot <= 6.0
    invalidation = max(base_low, pivot * 0.92) if pivot else base_low
    return {
        "available": True,
        "pivot": round(pivot, 4),
        "base_low": round(base_low, 4),
        "base_width_pct": round(base_width, 4),
        "contraction_ranges_pct": [_round(value) for value in contraction_ranges],
        "progressive_contraction": progressive,
        "volume_dryup": volume_dryup,
        "tight_base": base_width <= 28.0,
        "near_pivot": near_pivot,
        "distance_to_pivot_pct": round(distance_to_pivot, 4),
        "extension_from_pivot_pct": round(max(extension, 0.0), 4),
        "extended_from_pivot": extension > 5.0,
        "invalidation_level": round(invalidation, 4),
    }


def _relative_strength_profiles(
    universe: list[dict[str, Any]],
    candle_sets: dict[str, dict[str, list[Candle]]],
) -> dict[str, dict[str, Any]]:
    profiles: dict[str, dict[str, Any]] = {}
    returns_by_market: dict[str, list[float]] = {}
    for row in universe:
        symbol = str(row.get("symbol") or "").upper()
        candles = _analysis_candles(candle_sets.get(symbol) or {})
        closes = [float(candle.close) for candle in candles if candle.close]
        ret63 = _return_pct(closes, 63)
        ret20 = _return_pct(closes, 20)
        if ret63 is None:
            continue
        market = market_region_for_row(row)
        returns_by_market.setdefault(market, []).append(ret63)
        profiles[symbol] = {
            "available": True,
            "market_region": market,
            "return_20_pct": _round(ret20),
            "return_63_pct": _round(ret63),
            "trend": "rising" if (ret20 or 0.0) > 0 and (ret63 or 0.0) > 0 else "weak",
        }
    sorted_by_market = {market: sorted(values) for market, values in returns_by_market.items() if values}
    for symbol, profile in profiles.items():
        values = sorted_by_market.get(profile["market_region"]) or []
        ret = _float_or_none(profile.get("return_63_pct"))
        if ret is None or not values:
            continue
        profile["percentile_63"] = round(_percentile_rank(ret, values), 2)
        if profile["percentile_63"] >= 80:
            profile["bucket"] = "leadership"
        elif profile["percentile_63"] >= 60:
            profile["bucket"] = "rising"
        elif profile["percentile_63"] < 40:
            profile["bucket"] = "lagging"
        else:
            profile["bucket"] = "neutral"
    return profiles


def _macro_beneficiary_drivers(macro_context: dict[str, Any]) -> list[str]:
    drivers: list[str] = []
    for item in macro_context.get("markets") or []:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("symbol") or "")
        label = str(item.get("label") or "").lower()
        change = _float_or_none(item.get("change_pct")) or 0.0
        if symbol == "CL=F" or "crude" in label:
            if change <= -0.8:
                drivers.append("crude_down")
            elif change >= 1.0:
                drivers.append("crude_up")
        if symbol == "INR=X" or "usd/inr" in label:
            if change <= -0.4:
                drivers.append("rupee_strength")
            elif change >= 0.4:
                drivers.append("rupee_weakness")
        if symbol in {"^IXIC", "QQQ"} and change >= 1.0:
            drivers.append("us_tech_risk_on")
    text = " ".join(
        [
            str(macro_context.get("rationale") or ""),
            str((macro_context.get("news") or {}).get("headlines") or ""),
        ]
    ).lower()
    if "yield" in text and any(token in text for token in ("down", "fall", "eases", "cool")):
        drivers.append("rates_down")
    if "bank" in text and any(token in text for token in ("rally", "gain", "strength")):
        drivers.append("banking_strength")
    return _unique(drivers)


def _sector_matches_driver(sector: str, driver: str) -> bool:
    text = sector.lower()
    mapping = {
        "crude_down": ("airline", "aviation", "paint", "chemical", "tyre", "tire", "logistic", "consumer", "oil marketing", "omc"),
        "crude_up": ("oil", "gas", "energy", "upstream"),
        "rates_down": ("bank", "financial", "realty", "auto", "housing", "nbfc", "growth", "technology"),
        "rupee_strength": ("airline", "paint", "import", "oil marketing", "consumer"),
        "rupee_weakness": ("it", "technology", "pharma", "export", "textile", "chemical"),
        "banking_strength": ("bank", "financial", "nbfc"),
        "us_tech_risk_on": ("technology", "software", "semiconductor", "internet", "growth"),
    }
    return any(token in text for token in mapping.get(driver, ()))


def _symbol_sector_context(symbol: str, sector: str, sector_rotation_context: dict[str, Any]) -> dict[str, Any]:
    contexts = []
    if "symbols" in sector_rotation_context or "sectors" in sector_rotation_context:
        contexts.append(sector_rotation_context)
    for region in ("IN", "US", "BOTH"):
        if isinstance(sector_rotation_context.get(region), dict):
            contexts.append(sector_rotation_context[region])
    for context in contexts:
        symbols = context.get("symbols") or {}
        if symbol in symbols:
            return symbols[symbol]
        sectors = context.get("sectors") or {}
        if sector in sectors:
            item = sectors[sector]
            return {
                "sector": sector,
                "sector_rank": item.get("sector_rank"),
                "sector_stage": item.get("sector_stage"),
                "sector_tier": item.get("sector_tier"),
                "sector_tailwind": item.get("sector_stage") in {"accumulation", "markup"} and item.get("sector_tier") in {"top_quartile", "upper_mid"},
                "sector_headwind": item.get("sector_stage") == "distribution",
                "sector_rotation_score": item.get("sector_rotation_score"),
            }
    return {}


def _sentiment_has_positive_catalyst(sentiment: dict[str, Any]) -> bool:
    if bool(sentiment.get("positive_catalyst")):
        return True
    return _news_quality_score(sentiment) >= 0.45


def _sentiment_has_event(sentiment: dict[str, Any], event_type: str) -> bool:
    target = event_type.lower()
    for event in sentiment.get("events") or []:
        if isinstance(event, dict) and str(event.get("event_type") or "").lower() == target:
            return True
    return False


def _infer_catalyst_from_sentiment(sentiment: dict[str, Any]) -> dict[str, Any] | None:
    events = [event for event in sentiment.get("events") or [] if isinstance(event, dict)]
    if not events:
        return None
    for event in events:
        event_type = str(event.get("event_type") or "").lower()
        if event_type in {"earnings", "guidance", "analyst_upgrade", "order_win", "legal_regulatory"} and float(event.get("confidence") or 0.0) >= 0.35:
            catalyst_type = "earnings" if event_type == "earnings" else event_type
            return {
                "available": True,
                "catalyst_type": catalyst_type,
                "catalyst_date": None,
                "days_to_catalyst": None,
                "source": "sentiment_inferred_recent_catalyst",
                "data_gap": "exact_catalyst_date_unknown",
            }
    return None


def _catalyst_proximity_score(catalyst: dict[str, Any]) -> float:
    if not catalyst or not catalyst.get("available"):
        return 0.18
    days = _float_or_none(catalyst.get("days_to_catalyst"))
    if days is None:
        return 0.62
    if 0 <= days <= 2:
        return 1.0
    if 3 <= days <= 5:
        return 0.85
    if 6 <= days <= 10:
        return 0.68
    if 11 <= days <= 20:
        return 0.38
    return 0.20


def _news_quality_score(sentiment: dict[str, Any]) -> float:
    score = float(sentiment.get("score") or 0.0)
    confidence = float(sentiment.get("confidence") or 0.0)
    events = [event for event in sentiment.get("events") or [] if isinstance(event, dict)]
    high_quality = sum(1 for event in events if float(event.get("confidence") or 0.0) >= 0.45 and float(event.get("source_weight") or 0.5) >= 0.65)
    if score <= -0.25:
        return 0.0
    return _clamp(max(score, 0.0) * confidence + min(high_quality * 0.12, 0.36), 0.0, 1.0)


def _extension_score(setup: dict[str, Any]) -> float:
    distance = _float_or_none(setup.get("distance_to_pivot_pct"))
    extension = _float_or_none(setup.get("extension_from_pivot_pct")) or 0.0
    if extension > 8:
        return 0.0
    if extension > 5:
        return 0.25
    if distance is not None and -1.5 <= distance <= 4.0:
        return 1.0
    if distance is not None and 4.0 < distance <= 8.0:
        return 0.62
    return 0.35


def _events_by_symbol(market_action_summary: dict[str, Any]) -> dict[str, dict[str, Any]]:
    events = market_action_summary.get("events_by_symbol") if isinstance(market_action_summary, dict) else {}
    if isinstance(events, dict):
        return {str(symbol).upper(): value for symbol, value in events.items() if isinstance(value, dict)}
    output: dict[str, dict[str, Any]] = {}
    for event in market_action_summary.get("events") or []:
        if isinstance(event, dict) and event.get("symbol"):
            output[str(event["symbol"]).upper()] = event
    return output


def _sentiment_summary(sentiment: dict[str, Any]) -> dict[str, Any]:
    return {
        "score": sentiment.get("score"),
        "confidence": sentiment.get("confidence"),
        "headline_count": sentiment.get("headline_count") or len(sentiment.get("headlines") or []),
        "event_types": [
            str(event.get("event_type") or "")
            for event in (sentiment.get("events") or [])[:5]
            if isinstance(event, dict)
        ],
    }


def _missing_calendar(symbol: str) -> dict[str, Any]:
    return {
        "available": False,
        "symbol": symbol,
        "catalyst_type": "unknown",
        "catalyst_date": None,
        "days_to_catalyst": None,
        "source": "calendar_missing",
        "data_gap": "earnings_calendar_missing_for_symbol",
    }


def _analysis_candles(candle_set: dict[str, list[Candle]]) -> list[Candle]:
    return candle_set.get("analysis") or candle_set.get("daily") or candle_set.get("intraday") or []


def _sector(row: dict[str, Any]) -> str:
    return str(row.get("sector") or row.get("industry") or "Unclassified").strip() or "Unclassified"


def _day_gain_pct(quote: Quote) -> float:
    open_price = _float_or_none(quote.open)
    if not open_price or open_price <= 0:
        return 0.0
    return ((float(quote.price) - open_price) / open_price) * 100.0


def _gap_pct(quote: Quote, daily: list[Candle]) -> float:
    if not daily:
        prev_close = _float_or_none(quote.close)
    else:
        prev_close = _float_or_none(daily[-1].close)
    open_price = _float_or_none(quote.open)
    if not prev_close or not open_price:
        return 0.0
    return ((open_price - prev_close) / prev_close) * 100.0


def _live_volume_ratio(quote: Quote, daily: list[Candle]) -> float:
    volume = _float_or_none(quote.volume) or 0.0
    if not daily or volume <= 0:
        return 0.0
    baseline = _mean(candle.volume for candle in daily[-21:-1]) if len(daily) >= 21 else _mean(candle.volume for candle in daily[:-1])
    return volume / baseline if baseline else 0.0


def _range_position(quote: Quote) -> float:
    high = _float_or_none(quote.high)
    low = _float_or_none(quote.low)
    if high is None or low is None or high <= low:
        return 0.5
    return _clamp((float(quote.price) - low) / (high - low), 0.0, 1.0)


def _first_range_hold(intraday: list[Candle], price: float) -> bool:
    if len(intraday) < 2:
        return True
    first5 = intraday[: min(5, len(intraday))]
    first15 = intraday[: min(15, len(intraday))]
    first30 = intraday[: min(30, len(intraday))]
    lows = [min(candle.low for candle in bucket) for bucket in (first5, first15, first30) if bucket]
    highs = [max(candle.high for candle in bucket) for bucket in (first5, first15, first30) if bucket]
    if not lows or not highs:
        return True
    return price >= max(lows) and (price >= min(highs) or price >= intraday[-1].close)


def _vwap(candles: list[Candle]) -> float | None:
    usable = [candle for candle in candles if candle.volume and candle.high >= candle.low]
    total_volume = sum(float(candle.volume or 0.0) for candle in usable)
    if total_volume <= 0:
        return None
    total_value = sum(((candle.high + candle.low + candle.close) / 3.0) * float(candle.volume or 0.0) for candle in usable)
    return total_value / total_volume


def _base_range_pct(candles: list[Candle]) -> float | None:
    if not candles:
        return None
    lows = [candle.low for candle in candles if candle.low]
    highs = [candle.high for candle in candles if candle.high]
    if not lows or not highs:
        return None
    low = min(lows)
    return ((max(highs) - low) / low) * 100.0 if low else None


def _split_evenly(values: list[Candle], parts: int) -> list[list[Candle]]:
    if parts <= 0 or not values:
        return []
    size = max(1, len(values) // parts)
    output = []
    for index in range(parts):
        start = index * size
        end = (index + 1) * size if index < parts - 1 else len(values)
        output.append(values[start:end])
    return output


def _sma(values: list[float], window: int) -> float | None:
    if len(values) < window:
        return None
    return _mean(values[-window:])


def _return_pct(values: list[float], window: int) -> float | None:
    if len(values) <= window:
        return None
    base = values[-window - 1]
    if not base:
        return None
    return ((values[-1] - base) / base) * 100.0


def _percentile_rank(value: float, sorted_values: list[float]) -> float:
    if not sorted_values:
        return 50.0
    below = sum(1 for item in sorted_values if item <= value)
    return (below / len(sorted_values)) * 100.0


def _parse_date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)[:10]).date()
    except ValueError:
        return None


def _mean(values: Any) -> float:
    items = [float(value or 0.0) for value in values]
    return sum(items) / len(items) if items else 0.0


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _round(value: Any, digits: int = 4) -> float | None:
    numeric = _float_or_none(value)
    return round(numeric, digits) if numeric is not None else None


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(float(value or 0.0), high))


def _unique(values: list[Any]) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        output.append(text)
        seen.add(text)
    return output


def _count(values: dict[str, int], key: str) -> None:
    values[key] = values.get(key, 0) + 1


def _counts(values: list[Any]) -> dict[str, int]:
    output: dict[str, int] = {}
    for value in values:
        key = str(value or "").strip()
        if key:
            output[key] = output.get(key, 0) + 1
    return output
