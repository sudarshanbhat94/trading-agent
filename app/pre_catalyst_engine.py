from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from .full_spectrum import _stage_analysis
from .market_regions import market_region_for_row
from .models import Candle, Quote, utc_now
from .strategy_presets import evaluate_strategy_presets


PRE_CATALYST_WATCH = "PRE_CATALYST_WATCH"
READY_AT_OPEN = "READY_AT_OPEN"
NEAR_BREAKOUT = "NEAR_BREAKOUT"
EARNINGS_VCP_BREAKOUT = "EARNINGS_VCP_BREAKOUT"
OVERHANG_REMOVAL_RERATE = "OVERHANG_REMOVAL_RERATE"
SECTOR_ROTATION_LEADER = "SECTOR_ROTATION_LEADER"
LOW_QUALITY_SHORT_COVERING = "LOW_QUALITY_SHORT_COVERING"
LATE_CHASE_AVOID = "LATE_CHASE_AVOID"
DATA_STALE_WATCH = "DATA_STALE_WATCH"
UC_PRE_BREAKOUT_WATCH = "UC_PRE_BREAKOUT_WATCH"
PRE_MOMENTUM_EXPANSION_WATCH = "PRE_MOMENTUM_EXPANSION_WATCH"


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
    market_action_history = build_market_action_history(
        market_action_summary,
        previous_state=previous_state.get("market_action_history") if isinstance(previous_state, dict) else {},
        now=now,
    )
    previous_candidates = {
        str(item.get("symbol") or "").upper(): item
        for item in (previous_state.get("candidates") if isinstance(previous_state, dict) else []) or []
        if isinstance(item, dict)
    }
    missed_move_memory_by_symbol = _missed_move_memory_by_symbol(previous_state)

    candidates: list[OpportunityCandidate] = []
    log_events: list[dict[str, Any]] = []
    missing_history = 0
    missing_quote = 0
    symbols_with_history = 0
    data_gaps: dict[str, int] = {}

    for row in universe:
        symbol = str(row.get("symbol") or "").upper()
        if not symbol:
            continue
        candles = _analysis_candles(candle_sets.get(symbol) or {})
        if len(candles) < 30:
            missing_history += 1
            _count(data_gaps, "insufficient_history")
            continue
        symbols_with_history += 1
        quote = quotes.get(symbol)
        if not quote:
            quote = _daily_close_quote(symbol, candles)
            missing_quote += 1
            _count(data_gaps, "quote_missing_used_daily_close")
        if not quote:
            _count(data_gaps, "missing_quote_and_no_daily_close")
            continue

        sentiment = sentiment_by_symbol.get(symbol) or {}
        setup = _setup_profile(candles, quote)
        stage = _stage_analysis(candles, quote.price, {})
        catalyst = calendar["by_symbol"].get(symbol) or _missing_calendar(symbol)
        overhang = detect_overhang_removal(row, quote, candles, sentiment)
        sector_leader = sector_leaders.get(symbol) or {}
        rs_profile = rs_profiles.get(symbol) or {}
        current_market_event = market_events.get(symbol)
        action_history = (market_action_history.get("by_symbol") or {}).get(symbol) or {}
        short_covering = detect_short_covering_bounce(row, quote, candles, sentiment, current_market_event)
        uc_pre_breakout = detect_uc_pre_breakout(
            row,
            quote,
            candles,
            sentiment,
            setup,
            rs_profile,
            market_action_history=action_history,
            market_action=current_market_event,
        )
        momentum_expansion = detect_pre_move_expansion(
            row,
            quote,
            candles,
            sentiment,
            setup,
            rs_profile,
            sector_leader,
            market_action_history=action_history,
        )
        missed_move_memory = missed_move_memory_by_symbol.get(symbol)
        if missed_move_memory:
            momentum_expansion = _apply_missed_move_memory_to_expansion(
                momentum_expansion,
                missed_move_memory,
            )
        score_profile = _pre_catalyst_score(
            row=row,
            quote=quote,
            candles=candles,
            setup=setup,
            stage=stage,
            catalyst=catalyst,
            sentiment=sentiment,
            rs=rs_profile,
            sector_leader=sector_leader,
            overhang=overhang,
            short_covering=short_covering,
            uc_pre_breakout=uc_pre_breakout,
            momentum_expansion=momentum_expansion,
            settings=settings,
        )
        label = classify_opportunity(
            setup=setup,
            catalyst=catalyst,
            overhang=overhang,
            sector_leader=sector_leader,
            short_covering=short_covering,
            uc_pre_breakout=uc_pre_breakout,
            momentum_expansion=momentum_expansion,
            live_confirmation=None,
            score=score_profile["score"],
            min_score=min_score,
        )
        if label == "" or (
            score_profile["score"] < min_score
            and not overhang.get("detected")
            and not sector_leader.get("detected")
            and not short_covering.get("detected")
            and not uc_pre_breakout.get("detected")
            and not momentum_expansion.get("detected")
        ):
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
            rs=rs_profile,
            sector_leader=sector_leader,
            overhang=overhang,
            short_covering=short_covering,
            uc_pre_breakout=uc_pre_breakout,
            momentum_expansion=momentum_expansion,
        )
        candidates.append(candidate)
        log_events.append({"event": "watchlist_candidate", "symbol": symbol, "label": label, "reasons": candidate.key_reasons[:5]})

    candidates.sort(key=lambda item: (item.score, item.confidence), reverse=True)
    all_candidate_dicts = [candidate.to_dict() for candidate in candidates]
    candidates = _balanced_candidate_selection(candidates, candidate_limit)
    candidate_dicts = [candidate.to_dict() for candidate in candidates]
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
    missed_move_review = review_missed_moves(
        market_action_summary,
        previous_state=previous_state,
        current_candidates=all_candidate_dicts,
        live_confirmations=live_confirmations,
        now=now,
        min_move_pct=_missed_move_min_pct_for_universe(settings, universe),
    )

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
        "symbols_with_history": symbols_with_history,
        "analysis_quote_fallback_symbols": data_gaps.get("quote_missing_used_daily_close", 0),
        "missing_quote_symbols": missing_quote,
        "missing_history_symbols": missing_history,
        "candidate_limit": candidate_limit,
        "candidate_pool_count": len(all_candidate_dicts),
        "candidate_pool": [_compact_candidate_for_pool(item) for item in all_candidate_dicts],
        "min_score": min_score,
        "candidates": candidate_dicts,
        "live_confirmations": live_confirmations,
        "groups": _watchlist_groups(candidate_dicts, live_confirmations),
        "calendar_enrichment": calendar,
        "sector_rotation_leaders": list(sector_leaders.values())[:candidate_limit],
        "market_action_history": market_action_history,
        "missed_move_review": missed_move_review,
        "missed_move_memory_count": len(missed_move_memory_by_symbol),
        "label_counts": _counts([candidate.label for candidate in candidates] + [item.get("label") for item in live_confirmations]),
        "data_gaps": data_gaps,
        "log_events": log_events[-80:],
    }
    return payload


def _missed_move_memory_by_symbol(previous_state: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    previous_state = previous_state if isinstance(previous_state, dict) else {}
    review = previous_state.get("missed_move_review") if isinstance(previous_state.get("missed_move_review"), dict) else {}
    rows = review.get("items") if isinstance(review.get("items"), list) else []
    output: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        status = str(row.get("status") or "").strip()
        if status not in {"absent_from_prior_watchlist", "caught_same_cycle", "stale_watch_before_move"}:
            continue
        event_types = [str(item).upper() for item in row.get("event_types") or [] if str(item or "").strip()]
        market_action = row.get("market_action") if isinstance(row.get("market_action"), dict) else {}
        move_pct = _float_or_none(row.get("move_pct")) or 0.0
        volume_multiplier = _float_or_none(market_action.get("volume_multiplier")) or 0.0
        event_bonus = 0.0
        if "TOP_GAINER" in event_types or move_pct >= 5.0:
            event_bonus += 0.04
        if "VOLUME_SHOCKER" in event_types or volume_multiplier >= 3.0:
            event_bonus += 0.04
        if {"52_WEEK_HIGH", "ALL_TIME_HIGH", "PRICE_SHOCKER"} & set(event_types):
            event_bonus += 0.04
        output[symbol] = {
            "symbol": symbol,
            "status": status,
            "move_pct": _round(move_pct),
            "event_types": event_types,
            "volume_multiplier": _round(volume_multiplier),
            "score_floor": round(_clamp(0.60 + event_bonus, 0.0, 0.74), 4),
            "source": "missed_move_review_feedback",
            "reason": row.get("reason") or "missed-move review flagged this symbol for earlier watchlist coverage",
        }
    return output


def _apply_missed_move_memory_to_expansion(
    momentum_expansion: dict[str, Any],
    memory: dict[str, Any],
) -> dict[str, Any]:
    if not isinstance(memory, dict):
        return momentum_expansion
    base = dict(momentum_expansion) if isinstance(momentum_expansion, dict) else {}
    score_floor = _float_or_none(memory.get("score_floor")) or 0.60
    base_score = _float_or_none(base.get("score")) or 0.0
    evidence = dict(base.get("evidence") or {}) if isinstance(base.get("evidence"), dict) else {}
    evidence["missed_move_memory"] = {
        "status": memory.get("status"),
        "move_pct": memory.get("move_pct"),
        "event_types": memory.get("event_types") or [],
        "volume_multiplier": memory.get("volume_multiplier"),
        "source": memory.get("source"),
    }
    reasons = [str(item) for item in base.get("reasons") or [] if str(item or "").strip()]
    reasons.append("missed-move review feedback: similar symbol moved without prior watch coverage")
    return {
        **base,
        "detected": True,
        "score": round(max(base_score, score_floor), 4),
        "source": "pre_move_expansion+missed_move_review_feedback",
        "memory_boosted": True,
        "memory_status": memory.get("status"),
        "reasons": _unique(reasons)[:8],
        "evidence": evidence,
    }


def _missed_move_min_pct_for_universe(settings: Any, universe: list[dict[str, Any]]) -> float:
    if settings is None:
        return 5.0
    markets = {market_region_for_row(row) for row in universe if row.get("symbol")}
    values: list[float] = []
    if not markets or "IN" in markets:
        values.append(float(getattr(settings, "missed_move_min_move_pct_in", 5.0) or 5.0))
    if "US" in markets:
        values.append(float(getattr(settings, "missed_move_min_move_pct_us", 3.0) or 3.0))
    return min(values) if values else float(getattr(settings, "missed_move_min_move_pct_in", 5.0) or 5.0)


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


def build_market_action_history(
    market_action_summary: dict[str, Any] | None,
    *,
    previous_state: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    market_action_summary = market_action_summary or {}
    previous_state = previous_state or {}
    now = now or datetime.now(timezone.utc)
    today_key = now.date().isoformat()
    previous_by_symbol = previous_state.get("by_symbol") if isinstance(previous_state, dict) else {}
    by_symbol: dict[str, dict[str, Any]] = {
        str(symbol).upper(): dict(item)
        for symbol, item in (previous_by_symbol or {}).items()
        if isinstance(item, dict)
    }

    raw_events = market_action_summary.get("events") or []
    if not raw_events and isinstance(market_action_summary.get("events_by_symbol"), dict):
        raw_events = [
            {**value, "symbol": symbol}
            for symbol, value in (market_action_summary.get("events_by_symbol") or {}).items()
            if isinstance(value, dict)
        ]
    for event in raw_events:
        if not isinstance(event, dict):
            continue
        symbol = str(event.get("symbol") or "").upper()
        if not symbol:
            continue
        event_types = _unique([str(item).upper() for item in event.get("event_types") or []])
        prior = by_symbol.get(symbol) or {}
        active_dates = [
            str(item)
            for item in (prior.get("active_dates") or [])
            if str(item or "").strip()
        ][-30:]
        first_seen_today = today_key not in active_dates
        if first_seen_today:
            active_dates.append(today_key)
        seen_count = int(prior.get("seen_count") or 0) + (1 if first_seen_today else 0)
        only_buyers_days = int(prior.get("only_buyers_days") or 0) + (1 if first_seen_today and "ONLY_BUYERS" in event_types else 0)
        top_gainer_days = int(prior.get("top_gainer_days") or 0) + (1 if first_seen_today and "TOP_GAINER" in event_types else 0)
        volume_shocker_days = int(prior.get("volume_shocker_days") or 0) + (1 if first_seen_today and "VOLUME_SHOCKER" in event_types else 0)
        strong_mover_days = int(prior.get("strong_mover_days") or 0) + (
            1
            if first_seen_today
            and (
                "STRONG_INTRADAY_GAIN" in event_types
                or (_float_or_none(event.get("pct_change")) or 0.0) >= 5.0
            )
            else 0
        )
        by_symbol[symbol] = {
            "symbol": symbol,
            "seen_count": seen_count,
            "only_buyers_days": only_buyers_days,
            "top_gainer_days": top_gainer_days,
            "volume_shocker_days": volume_shocker_days,
            "strong_mover_days": strong_mover_days,
            "active_dates": active_dates,
            "last_seen_at": utc_now(),
            "last_event_types": event_types,
            "last_strategy": event.get("strategy"),
            "last_trade_window": event.get("trade_window"),
            "last_pct_change": _round(event.get("pct_change")),
            "last_volume_multiplier": _round(event.get("volume_multiplier")),
            "last_reason": event.get("reason"),
        }

    return {
        "enabled": True,
        "source": "market_action_radar_persistent_rollup",
        "updated_at": utc_now(),
        "symbols": len(by_symbol),
        "by_symbol": by_symbol,
    }


def review_missed_moves(
    market_action_summary: dict[str, Any] | None,
    *,
    previous_state: dict[str, Any] | None = None,
    current_candidates: list[dict[str, Any]] | None = None,
    live_confirmations: list[dict[str, Any]] | None = None,
    now: datetime | None = None,
    min_move_pct: float = 5.0,
) -> dict[str, Any]:
    """Compare today's confirmed movers with the prior discovery state.

    This is a deterministic calibration ledger: it explains whether a mover
    was absent, watched, stale, or correctly avoided before it appeared in the
    late market-action feed.
    """

    market_action_summary = market_action_summary or {}
    previous_state = previous_state or {}
    current_candidates = current_candidates or []
    live_confirmations = live_confirmations or []
    previous_rows = [
        item
        for collection in (previous_state.get("candidates") or [], previous_state.get("candidate_pool") or [])
        for item in collection
        if isinstance(item, dict) and str(item.get("symbol") or "").strip()
    ]
    previous_candidates = {
        str(item.get("symbol") or "").upper(): item
        for item in previous_rows
    }
    current_by_symbol = {
        str(item.get("symbol") or "").upper(): item
        for item in current_candidates
        if isinstance(item, dict) and str(item.get("symbol") or "").strip()
    }
    rows: list[dict[str, Any]] = []
    review_events = _events_by_symbol(market_action_summary)
    for live in live_confirmations:
        if not isinstance(live, dict):
            continue
        symbol = str(live.get("symbol") or "").strip().upper()
        if not symbol or symbol in review_events:
            continue
        review_events[symbol] = {
            **live,
            "symbol": symbol,
            "event_types": _unique([*(live.get("event_types") or []), "LIVE_CONFIRMATION"]),
            "pct_change": (
                live.get("pct_change")
                or live.get("move_pct")
                or live.get("current_return_pct")
                or live.get("return_pct")
            ),
            "source": live.get("source") or "pre_catalyst_live_confirmation",
            "strategy": live.get("setup") or live.get("strategy"),
            "trade_window": live.get("trade_window") or "live_confirmation",
        }
    for symbol, event in sorted(review_events.items()):
        event_types = {str(item or "").upper() for item in event.get("event_types") or []}
        move_pct = _float_or_none(
            event.get("pct_change")
            or event.get("day_gain_pct")
            or event.get("change_pct")
            or event.get("percent_change")
        )
        top_mover = bool(
            (move_pct is not None and move_pct >= min_move_pct)
            or event_types
            & {
                "TOP_GAINER",
                "PRICE_SHOCKER",
                "VOLUME_SHOCKER",
                "ONLY_BUYERS",
                "52_WEEK_HIGH",
                "STRONG_INTRADAY_GAIN",
                "LIVE_CONFIRMATION",
            }
        )
        if not top_mover:
            continue
        prior = previous_candidates.get(symbol)
        current = current_by_symbol.get(symbol)
        prior_label = str((prior or {}).get("label") or "")
        current_label = str((current or {}).get("label") or "")
        status = "absent_from_prior_watchlist"
        reasons = ["not present in previous pre-catalyst candidates"]
        if prior_label == DATA_STALE_WATCH:
            status = "stale_watch_before_move"
            reasons = ["previously seen but blocked by stale data"]
        elif prior_label == LATE_CHASE_AVOID:
            status = "correctly_avoided_late_chase"
            reasons = ["previously identified as too extended/locked; no chase"]
        elif prior_label == LOW_QUALITY_SHORT_COVERING:
            status = "low_quality_watch_before_move"
            reasons = ["previously classified as low-quality squeeze/bounce"]
        elif prior:
            status = "correctly_watched_before_move"
            reasons = [f"prior label {prior_label or 'WATCH'} was present before top-mover confirmation"]
        elif current:
            status = "caught_same_cycle"
            reasons = [f"current discovery label {current_label or 'WATCH'} exists, but prior-day watch was absent"]
        if "ONLY_BUYERS" in event_types:
            reasons.append("current event has only-buyers/circuit demand; keep pullback policy")
        rows.append(
            {
                "symbol": symbol,
                "status": status,
                "move_pct": _round(move_pct),
                "event_types": sorted(event_types),
                "previous_label": prior_label or None,
                "current_label": current_label or None,
                "reason": "; ".join(reasons),
                "market_action": {
                    "strategy": event.get("strategy"),
                    "trade_window": event.get("trade_window"),
                    "volume_multiplier": _round(event.get("volume_multiplier")),
                    "source": event.get("source"),
                },
            }
        )
    status_counts = _counts([row["status"] for row in rows])
    hints: list[str] = []
    if status_counts.get("absent_from_prior_watchlist"):
        hints.append("Review pre-move compression, RS, sector-rank, and news thresholds for absent winners.")
    if status_counts.get("stale_watch_before_move"):
        hints.append("Fix stale quote/candle coverage before open; stale watches must not disappear from dashboards.")
    if status_counts.get("correctly_avoided_late_chase"):
        hints.append("Keep late-chase policy; show these as pullback candidates, not normal BUY.")
    return {
        "enabled": True,
        "source": "market_action_vs_prior_pre_catalyst_watchlist",
        "generated_at": (now or datetime.now(timezone.utc)).isoformat(),
        "min_move_pct": min_move_pct,
        "reviewed_movers": len(rows),
        "status_counts": status_counts,
        "items": rows[:80],
        "tuning_hints": hints,
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


def detect_uc_pre_breakout(
    row: dict[str, Any],
    quote: Quote,
    candles: list[Candle],
    sentiment: dict[str, Any] | None,
    setup: dict[str, Any],
    rs: dict[str, Any],
    *,
    market_action_history: dict[str, Any] | None = None,
    market_action: dict[str, Any] | None = None,
) -> dict[str, Any]:
    market = market_region_for_row(row)
    if market != "IN":
        return {"detected": False, "score": 0.0, "market_region": market}
    market_action_history = market_action_history or {}
    market_action = market_action or {}
    event_types = {str(item or "").upper() for item in market_action.get("event_types") or []}
    already_locked = "ONLY_BUYERS" in event_types or str(market_action.get("strategy") or "") == "circuit_demand_lock"
    if already_locked:
        return {
            "detected": True,
            "score": 0.68,
            "status": "already_locked_no_chase",
            "event_types": sorted(event_types),
            "trade_window": "wait_for_pullback",
            "position_size_hint": "none_until_tradable_pullback",
            "reason": "stock is already in only-buyers/upper-circuit demand lock",
        }

    if len(candles) < 35 or quote.price <= 0:
        return {"detected": False, "score": 0.0, "reason": "insufficient_history"}
    if quote.price < 10:
        return {"detected": False, "score": 0.0, "reason": "below_min_price_for_uc_watch"}

    closes = [float(candle.close) for candle in candles if candle.close]
    if len(closes) < 35:
        return {"detected": False, "score": 0.0, "reason": "insufficient_closes"}
    returns = _daily_returns_pct(candles)
    recent_returns = returns[-20:]
    limit_like_days = 0
    strong_days = 0
    for candle, ret in zip(candles[-len(recent_returns):], recent_returns):
        close_position = _candle_close_position(candle)
        if ret >= 4.75 and close_position >= 0.80:
            limit_like_days += 1
        if 3.0 <= ret <= 15.5 and close_position >= 0.70:
            strong_days += 1

    latest = candles[-1]
    close_near_high = _candle_close_position(latest) >= 0.82
    near_high = bool(setup.get("near_prior_high") or setup.get("near_pivot"))
    quiet_or_dry = bool(setup.get("quiet_range_contraction") or setup.get("volume_dryup") or setup.get("pre_rally_compression"))
    history_only_buyers = int(market_action_history.get("only_buyers_days") or 0)
    history_strong = int(market_action_history.get("strong_mover_days") or 0)
    price_band_memory = bool(limit_like_days or history_only_buyers)
    market_cap_cr = _market_cap_crore(row)
    free_float_pct = _float_or_none(row.get("free_float_pct") or row.get("free_float"))
    low_float_hint = bool(
        (market_cap_cr is not None and 200 <= market_cap_cr <= 5_000)
        or (free_float_pct is not None and free_float_pct <= 35)
    )
    avg_volume = _mean(candle.volume for candle in candles[-20:])
    avg_price = _mean(candle.close for candle in candles[-20:])
    avg_turnover = avg_price * avg_volume
    liquidity_ok = avg_turnover >= 5_000_000 and quote.price >= 10
    accumulation = _accumulation_profile(candles)
    rs_score = _clamp(float(rs.get("percentile_63") or 0.0) / 100.0, 0.0, 1.0)
    news_score = _news_quality_score(sentiment or {})
    negative_tone = _negative_news_tone(sentiment or {})
    extension = _float_or_none(setup.get("extension_from_pivot_pct")) or 0.0
    day_gain = _day_gain_pct(quote)
    not_already_chasing = extension <= 5.0 and day_gain < 7.5
    uc_signature = bool(price_band_memory or low_float_hint or history_strong >= 2)
    score = _clamp(
        (0.14 if near_high else 0.0)
        + (0.12 if close_near_high else 0.0)
        + (0.12 if quiet_or_dry else 0.0)
        + (0.14 if price_band_memory else 0.0)
        + (0.10 if low_float_hint else 0.0)
        + (0.10 if accumulation.get("score", 0.0) >= 0.55 else 0.0)
        + (0.12 * rs_score)
        + (0.08 if liquidity_ok else 0.0)
        + (0.08 if news_score >= 0.30 else 0.0)
        + (0.08 if history_strong >= 2 else 0.0),
        0.0,
        1.0,
    )
    detected = bool(
        score >= 0.58
        and uc_signature
        and near_high
        and close_near_high
        and liquidity_ok
        and not_already_chasing
        and not negative_tone
    )
    return {
        "detected": detected,
        "score": round(score, 4),
        "status": "pre_breakout_watch" if detected else "not_enough_uc_evidence",
        "limit_like_days_20": limit_like_days,
        "strong_mover_days_20": strong_days,
        "history_only_buyers_days": history_only_buyers,
        "history_strong_mover_days": history_strong,
        "price_band_memory": price_band_memory,
        "low_float_hint": low_float_hint,
        "market_cap_cr": _round(market_cap_cr),
        "avg_turnover": _round(avg_turnover, 2),
        "liquidity_ok": liquidity_ok,
        "close_near_day_high": close_near_high,
        "near_breakout_zone": near_high,
        "quiet_or_volume_dryup": quiet_or_dry,
        "accumulation": accumulation,
        "rs_percentile_63": _round(rs.get("percentile_63"), 2),
        "news_score": _round(news_score),
        "negative_tone": negative_tone,
        "position_size_hint": "watch_only_until_live_confirmation",
    }


def detect_pre_move_expansion(
    row: dict[str, Any],
    quote: Quote,
    candles: list[Candle],
    sentiment: dict[str, Any] | None,
    setup: dict[str, Any],
    rs: dict[str, Any],
    sector_leader: dict[str, Any],
    *,
    market_action_history: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if len(candles) < 45 or quote.price <= 0:
        return {"detected": False, "score": 0.0, "reason": "insufficient_history"}
    sentiment = sentiment or {}
    market_action_history = market_action_history or {}
    closes = [float(candle.close) for candle in candles if candle.close]
    ret_5 = _return_pct(closes, 5)
    ret_20 = _return_pct(closes, 20)
    ret_63 = _return_pct(closes, 63)
    sma20 = _sma(closes, 20)
    sma50 = _sma(closes, 50)
    trend_aligned = bool(sma20 and quote.price >= sma20 and (not sma50 or sma20 >= sma50 * 0.98))
    atr5 = _average_true_range_pct(candles[-6:])
    atr20 = _average_true_range_pct(candles[-21:])
    volatility_compression = bool(atr5 is not None and atr20 is not None and atr5 <= atr20 * 0.78)
    accumulation = _accumulation_profile(candles)
    rs_score = _clamp(float(rs.get("percentile_63") or 0.0) / 100.0, 0.0, 1.0)
    news_score = _news_quality_score(sentiment)
    sector_score = _clamp(float(sector_leader.get("score") or 0.0), 0.0, 1.0)
    history_strong = int(market_action_history.get("strong_mover_days") or 0)
    near_breakout = bool(setup.get("near_pivot") or setup.get("near_prior_high") or setup.get("pre_rally_compression"))
    dry_or_tight = bool(setup.get("volume_dryup") or setup.get("quiet_range_contraction") or volatility_compression)
    accumulation_ready = float(accumulation.get("score") or 0.0) >= 0.55
    momentum_bias = bool(
        accumulation_ready
        or news_score >= 0.30
        or sector_score >= 0.55
        or ((ret_5 or 0.0) >= 2.5 and (ret_20 or 0.0) >= 6.0)
        or history_strong >= 2
    )
    extension = _float_or_none(setup.get("extension_from_pivot_pct")) or 0.0
    day_gain = _day_gain_pct(quote)
    too_late = extension > 5.0 or day_gain >= 8.0
    negative_tone = _negative_news_tone(sentiment)
    score = _clamp(
        (0.14 if near_breakout else 0.0)
        + (0.12 if dry_or_tight else 0.0)
        + (0.14 if trend_aligned else 0.0)
        + (0.14 * rs_score)
        + (0.12 * float(accumulation.get("score") or 0.0))
        + (0.10 if volatility_compression else 0.0)
        + (0.10 if (ret_20 or 0.0) > 4.0 else 0.0)
        + (0.08 if news_score >= 0.30 else 0.0)
        + (0.08 if sector_score >= 0.55 else 0.0)
        + (0.06 if history_strong >= 2 else 0.0),
        0.0,
        1.0,
    )
    detected = bool(score >= 0.60 and near_breakout and dry_or_tight and momentum_bias and not too_late and not negative_tone)
    return {
        "detected": detected,
        "score": round(score, 4),
        "status": "pre_expansion_watch" if detected else "not_enough_expansion_evidence",
        "return_5_pct": _round(ret_5),
        "return_20_pct": _round(ret_20),
        "return_63_pct": _round(ret_63),
        "trend_aligned": trend_aligned,
        "volatility_compression": volatility_compression,
        "atr_5_pct": _round(atr5),
        "atr_20_pct": _round(atr20),
        "near_breakout_zone": near_breakout,
        "dry_or_tight": dry_or_tight,
        "accumulation": accumulation,
        "rs_percentile_63": _round(rs.get("percentile_63"), 2),
        "sector_score": _round(sector_score),
        "news_score": _round(news_score),
        "history_strong_mover_days": history_strong,
        "too_late": too_late,
        "negative_tone": negative_tone,
        "position_size_hint": "normal_if_live_confirmation_holds",
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
    intraday_fresh = _intraday_candles_match_quote_session(intraday, quote)
    usable_intraday = intraday if intraday_fresh else []
    market_region = _quote_market_region(quote)
    source = str(quote.source or "").lower()
    market_action_events = [str(item).upper() for item in market_action.get("event_types", []) if str(item or "").strip()]
    stale_market_action = bool(
        market_action.get("prior_session")
        or market_action.get("stale")
        or str(market_action.get("freshness") or "").lower() in {"stale", "prior_session", "previous_session"}
        or "PRIOR_SESSION" in market_action_events
        or "STALE_DATA" in market_action_events
    )
    pivot = _float_or_none(candidate.get("pivot"))
    if pivot is None:
        pivot = _setup_features(daily, quote).get("pivot")
    pivot = _float_or_none(pivot)
    day_gain = _day_gain_pct(quote)
    gap_pct = _gap_pct(quote, daily)
    extension = ((quote.price - pivot) / pivot) * 100.0 if pivot and pivot > 0 else 0.0
    volume_ratio = _live_volume_ratio(quote, daily)
    vwap = _vwap(usable_intraday)
    vwap_hold = bool(vwap and quote.price >= vwap) if vwap else _range_position(quote) >= 0.60
    range_hold = _first_range_hold(usable_intraday, quote.price) if usable_intraday else False
    breakout = bool((pivot and quote.price >= pivot) or "52_WEEK_HIGH" in market_action_events or "VOLUME_SHOCKER" in market_action_events)
    volume_threshold = 2.0 if market_region == "US" and any(token in source for token in ("yahoo", "iex")) else 1.5
    volume_confirmed = bool(volume_ratio >= volume_threshold or ("VOLUME_SHOCKER" in market_action_events and volume_ratio >= 1.20))
    catalyst_confirmed = _sentiment_has_positive_catalyst(sentiment) or bool(market_action_events)
    too_extended = extension > 5.0 or day_gain >= 8.0
    demand_locked = "ONLY_BUYERS" in market_action_events or str(market_action.get("strategy") or "") == "circuit_demand_lock"
    sector_participation = _sector_participation_ok(candidate, market_action)
    data_stale = bool(stale_market_action or (breakout and not intraday_fresh))

    if demand_locked:
        label = LATE_CHASE_AVOID
    elif too_extended and breakout:
        label = LATE_CHASE_AVOID
    elif data_stale:
        label = DATA_STALE_WATCH
    elif candidate.get("label") == LOW_QUALITY_SHORT_COVERING:
        label = LOW_QUALITY_SHORT_COVERING
    elif breakout and intraday_fresh and vwap_hold and range_hold and volume_confirmed and catalyst_confirmed and sector_participation:
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
        + (0.06 if sector_participation else -0.04)
        + (0.10 if intraday_fresh else -0.10)
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
    if not intraday_fresh:
        reasons.append("waiting for fresh intraday candle confirmation")
    if stale_market_action:
        reasons.append("market-action data is prior-session/stale; watch only")
    if not sector_participation:
        reasons.append("sector participation is not confirmed")
    if too_extended:
        reasons.append("late chase risk; too extended from pivot")
    if demand_locked:
        reasons.append("upper-circuit/only-buyer demand lock; wait for tradable pullback")
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
        "volume_threshold": volume_threshold,
        "vwap": _round(vwap),
        "intraday_fresh": intraday_fresh,
        "data_stale": data_stale,
        "market_region": market_region,
        "breakout": breakout,
        "vwap_hold": vwap_hold,
        "first_range_hold": range_hold,
        "volume_confirmed": volume_confirmed,
        "catalyst_confirmed": catalyst_confirmed,
        "sector_participation": sector_participation,
        "demand_locked": demand_locked,
        "fresh_action": "BUY_NOW" if label in {EARNINGS_VCP_BREAKOUT, OVERHANG_REMOVAL_RERATE, SECTOR_ROTATION_LEADER} else "WATCH",
        "trade_window": "actionable_if_entry_zone_holds" if label in {EARNINGS_VCP_BREAKOUT, OVERHANG_REMOVAL_RERATE, SECTOR_ROTATION_LEADER} else "watch_only",
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
    uc_pre_breakout: dict[str, Any],
    momentum_expansion: dict[str, Any],
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
    if uc_pre_breakout.get("status") == "already_locked_no_chase":
        return LATE_CHASE_AVOID
    if uc_pre_breakout.get("detected") and score >= min_score:
        return UC_PRE_BREAKOUT_WATCH
    if sector_leader.get("detected") and score >= min_score:
        return SECTOR_ROTATION_LEADER
    if catalyst.get("catalyst_type") == "earnings" and setup.get("pre_catalyst_ready") and score >= min_score:
        return PRE_CATALYST_WATCH
    if momentum_expansion.get("detected") and score >= min_score:
        return PRE_MOMENTUM_EXPANSION_WATCH
    if setup.get("pre_catalyst_ready") and score >= min_score:
        return PRE_CATALYST_WATCH
    return ""


def _watchlist_groups(candidates: list[dict[str, Any]], live_confirmations: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = {
        READY_AT_OPEN: [],
        NEAR_BREAKOUT: [],
        PRE_CATALYST_WATCH: [],
        UC_PRE_BREAKOUT_WATCH: [],
        SECTOR_ROTATION_LEADER: [],
        OVERHANG_REMOVAL_RERATE: [],
        LOW_QUALITY_SHORT_COVERING: [],
        LATE_CHASE_AVOID: [],
        DATA_STALE_WATCH: [],
    }
    for item in candidates:
        label = str(item.get("label") or PRE_CATALYST_WATCH)
        setup = item.get("supporting_signals", {}).get("setup", {}) if isinstance(item.get("supporting_signals"), dict) else {}
        extension = _float_or_none(setup.get("extension_from_pivot_pct"))
        near_pivot = bool(setup.get("near_pivot") or setup.get("near_prior_high"))
        score = _float_or_none(item.get("score")) or 0.0
        bucket = label if label in groups else PRE_CATALYST_WATCH
        if label in {LOW_QUALITY_SHORT_COVERING, LATE_CHASE_AVOID, DATA_STALE_WATCH}:
            groups[bucket].append(item)
        elif score >= 0.70 and near_pivot and (extension is None or -4.0 <= extension <= 2.0):
            groups[READY_AT_OPEN].append(item)
        elif near_pivot and (extension is None or -4.0 <= extension <= 4.0):
            groups[NEAR_BREAKOUT].append(item)
        else:
            groups[bucket].append(item)
    for live in live_confirmations:
        label = str(live.get("label") or "")
        if label in groups:
            groups[label].append(live)
    for key, items in groups.items():
        seen: set[str] = set()
        unique: list[dict[str, Any]] = []
        for item in sorted(items, key=lambda row: float(row.get("score") or 0.0), reverse=True):
            symbol = str(item.get("symbol") or "").upper()
            if symbol and symbol in seen:
                continue
            if symbol:
                seen.add(symbol)
            unique.append(item)
        groups[key] = unique
    return groups


def _balanced_candidate_selection(candidates: list[OpportunityCandidate], candidate_limit: int) -> list[OpportunityCandidate]:
    limit = max(1, int(candidate_limit or 1))
    ordered = sorted(candidates, key=lambda item: (item.score, item.confidence), reverse=True)
    if len(ordered) <= limit:
        return ordered
    by_market: dict[str, list[OpportunityCandidate]] = {}
    for item in ordered:
        market = str(item.market_region or "OTHER").upper()
        by_market.setdefault(market, []).append(item)
    market_order = [market for market in ("IN", "US") if by_market.get(market)]
    market_order.extend(sorted(market for market in by_market if market not in {"IN", "US"}))
    if len(market_order) <= 1 or limit < len(market_order):
        return ordered[:limit]

    selected: list[OpportunityCandidate] = []
    selected_symbols: set[str] = set()
    base_quota = max(1, limit // len(market_order))
    for market in market_order:
        for item in by_market.get(market, [])[:base_quota]:
            if len(selected) >= limit:
                break
            selected.append(item)
            selected_symbols.add(item.symbol)
    for item in ordered:
        if len(selected) >= limit:
            break
        if item.symbol in selected_symbols:
            continue
        selected.append(item)
        selected_symbols.add(item.symbol)
    return sorted(selected, key=lambda item: (item.score, item.confidence), reverse=True)


def _compact_candidate_for_pool(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "symbol": item.get("symbol"),
        "label": item.get("label"),
        "confidence": item.get("confidence"),
        "score": item.get("score"),
        "market_region": item.get("market_region"),
        "catalyst_type": item.get("catalyst_type"),
        "catalyst_date": item.get("catalyst_date"),
        "setup_summary": item.get("setup_summary"),
        "entry_zone": item.get("entry_zone"),
        "pivot": item.get("pivot"),
        "invalidation_level": item.get("invalidation_level"),
        "key_reasons": (item.get("key_reasons") or [])[:6],
    }


def _quote_market_region(quote: Quote) -> str:
    source = str(quote.source or "").lower()
    symbol = str(quote.symbol or "").upper()
    if any(token in source for token in ("alpaca", "polygon", "iex", "sip")) or "." not in symbol and source.startswith("yahoo"):
        return "US"
    return "IN"


def _sector_participation_ok(candidate: dict[str, Any], market_action: dict[str, Any]) -> bool:
    supporting = candidate.get("supporting_signals") if isinstance(candidate.get("supporting_signals"), dict) else {}
    sector = supporting.get("sector_rotation") if isinstance(supporting.get("sector_rotation"), dict) else {}
    if sector.get("detected") or sector.get("sector_tailwind"):
        return True
    raw = market_action.get("sector_participation")
    if raw is None:
        raw = market_action.get("sector_confirmation")
    if raw is None:
        return True
    if isinstance(raw, bool):
        return raw
    score = _float_or_none(raw)
    return score is None or score >= 0.0


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
    uc_pre_breakout: dict[str, Any],
    momentum_expansion: dict[str, Any],
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
    if uc_pre_breakout.get("status") == "already_locked_no_chase":
        reasons.append("already upper-circuit/only-buyer locked; do not chase")
    elif uc_pre_breakout.get("detected"):
        reasons.append("UC/price-band precursor pattern detected")
    if momentum_expansion.get("detected"):
        reasons.append("pre-move expansion setup for possible 5-15% move")
    setup_summary = (
        "already locked; wait for pullback"
        if uc_pre_breakout.get("status") == "already_locked_no_chase"
        else "UC/price-band precursor near breakout"
        if uc_pre_breakout.get("detected")
        else "pre-move expansion setup"
        if momentum_expansion.get("detected")
        else
        "tight VCP/base near pivot"
        if setup.get("tight_base") or setup.get("progressive_contraction")
        else "pre-catalyst technical setup"
    )
    catalyst_type = str(catalyst.get("catalyst_type") or "unknown")
    if uc_pre_breakout.get("detected") and catalyst_type == "unknown":
        catalyst_type = "price_band_demand"
    elif momentum_expansion.get("detected") and catalyst_type == "unknown":
        catalyst_type = "technical_expansion"
    return OpportunityCandidate(
        symbol=symbol,
        label=label,
        confidence=round(_clamp(score_profile["score"] * 0.88 + score_profile.get("evidence_quality", 0.0) * 0.12, 0.0, 1.0), 4),
        score=round(score_profile["score"], 4),
        market_region=market_region_for_row(row),
        catalyst_type=catalyst_type,
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
            "uc_pre_breakout": uc_pre_breakout,
            "pre_move_expansion": momentum_expansion,
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
    uc_pre_breakout: dict[str, Any],
    momentum_expansion: dict[str, Any],
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
    pre_rally_score = float(setup.get("pre_rally_score") or 0.0)
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
    uc_score = _clamp(float(uc_pre_breakout.get("score") or 0.0), 0.0, 1.0)
    expansion_score = _clamp(float(momentum_expansion.get("score") or 0.0), 0.0, 1.0)
    stage_score = 1.0 if stage.get("buy_permitted") else 0.45 if stage.get("stage") == "Stage1_Base" else 0.15
    score = (
        catalyst_score * 0.12
        + setup_score * 0.20
        + pre_rally_score * 0.14
        + dryup_score * 0.10
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
    if uc_pre_breakout.get("detected"):
        score = max(score, uc_score)
    if momentum_expansion.get("detected"):
        score = max(score, expansion_score)
    if setup.get("pre_rally_compression") and rs_score >= 0.58 and liquidity >= 0.22 and extension_score >= 0.55:
        score = max(score, 0.54 + min(pre_rally_score, 1.0) * 0.12 + min(rs_score, 1.0) * 0.07)
    event_dryup_near_pivot = (
        catalyst.get("catalyst_type") == "earnings"
        and catalyst_score >= 0.60
        and bool(setup.get("volume_dryup"))
        and bool(setup.get("near_pivot") or setup.get("near_prior_high"))
        and extension_score >= 0.62
        and liquidity >= 0.15
    )
    if event_dryup_near_pivot:
        score = max(
            score,
            0.79
            + (0.04 if setup.get("pre_rally_compression") else 0.0)
            + (0.03 if setup.get("quiet_range_contraction") else 0.0)
            + (0.02 if rs_score >= 0.45 else 0.0),
        )
    reasons = []
    if setup.get("pre_rally_compression"):
        reasons.append("pre-rally compression near breakout zone")
    if setup.get("progressive_contraction"):
        reasons.append("base contraction")
    if setup.get("quiet_range_contraction"):
        reasons.append("quiet range contraction")
    if setup.get("volume_dryup"):
        reasons.append("volume dry-up")
    if setup.get("near_pivot"):
        reasons.append("near pivot without extension")
    elif setup.get("near_prior_high"):
        reasons.append("near prior high without extension")
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
    elif event_dryup_near_pivot:
        reasons.append("earnings/news dry-up precursor")
    if uc_pre_breakout.get("status") == "already_locked_no_chase":
        reasons.append("already in only-buyers/upper-circuit; no chase")
    elif uc_pre_breakout.get("detected"):
        reasons.append("UC/price-band demand precursor")
    if momentum_expansion.get("detected"):
        reasons.append("pre-move expansion pressure")
    return {
        "score": round(_clamp(score, 0.0, 1.0), 4),
        "evidence_quality": round(_clamp((setup_score + rs_score + liquidity + news_quality) / 4.0, 0.0, 1.0), 4),
        "components": {
            "catalyst_proximity": round(catalyst_score, 4),
            "setup_quality": round(setup_score, 4),
            "pre_rally_compression": round(pre_rally_score, 4),
            "volume_dryup": round(dryup_score, 4),
            "relative_strength": round(rs_score, 4),
            "sector_strength": round(sector_score, 4),
            "liquidity": round(liquidity, 4),
            "extension_from_pivot": round(extension_score, 4),
            "news_quality": round(news_quality, 4),
            "stage": round(stage_score, 4),
            "uc_pre_breakout": round(uc_score, 4),
            "pre_move_expansion": round(expansion_score, 4),
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
        + (0.16 if features.get("pre_rally_compression") else 0.0)
        + (0.10 if features.get("near_prior_high") else 0.0)
        + (0.12 if not features.get("extended_from_pivot") else -0.10),
    )
    return {
        **features,
        "score": round(_clamp(score, 0.0, 1.0), 4),
        "strategy_scores": {signal.name: signal.score for signal in strategy_signals},
        "strategy_notes": {signal.name: signal.notes[:4] for signal in strategy_signals if signal.name in {"vcp_breakout", "minervini_trend_template", "darvas_box_breakout", "aggressive_relative_strength_breakout"}},
        "pre_catalyst_ready": bool(
            (features.get("near_pivot") or features.get("pre_rally_compression"))
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
    closes = [candle.close for candle in setup if candle.close]
    if not highs or not lows:
        return {"available": False, "score": 0.0}
    pivot = max(highs)
    base_low = min(lows)
    base_width = ((pivot - base_low) / base_low) * 100.0 if base_low else 100.0
    high_20 = max(highs[-20:]) if len(highs) >= 20 else pivot
    high_55 = max(highs[-55:]) if len(highs) >= 55 else pivot
    low_10 = min(lows[-10:]) if len(lows) >= 10 else min(lows)
    high_10 = max(highs[-10:]) if len(highs) >= 10 else max(highs)
    last_close = closes[-1] if closes else quote.price
    ten_day_range_pct = ((high_10 - low_10) / low_10) * 100.0 if low_10 else 100.0
    early_volume = _mean(candle.volume for candle in setup[: max(10, len(setup) // 3)])
    late_volume = _mean(candle.volume for candle in setup[-10:])
    volume_dryup = bool(early_volume) and late_volume <= early_volume * 0.78
    distance_to_pivot = ((pivot - quote.price) / pivot) * 100.0 if pivot else 100.0
    distance_to_20d_high = ((high_20 - quote.price) / high_20) * 100.0 if high_20 else 100.0
    distance_to_55d_high = ((high_55 - quote.price) / high_55) * 100.0 if high_55 else 100.0
    extension = ((quote.price - pivot) / pivot) * 100.0 if pivot else 0.0
    near_pivot = -2.0 <= distance_to_pivot <= 6.0
    near_prior_high = (
        -1.5 <= distance_to_20d_high <= 8.0
        or -1.5 <= distance_to_55d_high <= 8.0
    )
    quiet_range_contraction = ten_day_range_pct <= min(10.0, max(4.0, base_width * 0.42))
    closes_near_top = bool(pivot and last_close >= pivot * 0.88)
    pre_rally_score = _clamp(
        (0.24 if near_pivot else 0.0)
        + (0.18 if near_prior_high else 0.0)
        + (0.18 if quiet_range_contraction else 0.0)
        + (0.16 if volume_dryup else 0.0)
        + (0.14 if progressive else 0.0)
        + (0.10 if closes_near_top else 0.0)
        + (0.08 if base_width <= 32.0 else 0.0),
        0.0,
        1.0,
    )
    pre_rally_compression = bool(
        near_prior_high
        and not extension > 5.0
        and closes_near_top
        and (
            quiet_range_contraction
            or volume_dryup
            or progressive
            or (base_width <= 24.0 and ten_day_range_pct <= 12.0)
        )
    )
    invalidation = max(base_low, pivot * 0.92) if pivot else base_low
    return {
        "available": True,
        "pivot": round(pivot, 4),
        "base_low": round(base_low, 4),
        "prior_20d_high": round(high_20, 4),
        "prior_55d_high": round(high_55, 4),
        "base_width_pct": round(base_width, 4),
        "last_10d_range_pct": round(ten_day_range_pct, 4),
        "contraction_ranges_pct": [_round(value) for value in contraction_ranges],
        "progressive_contraction": progressive,
        "volume_dryup": volume_dryup,
        "quiet_range_contraction": quiet_range_contraction,
        "tight_base": base_width <= 28.0,
        "near_pivot": near_pivot,
        "near_prior_high": near_prior_high,
        "distance_to_pivot_pct": round(distance_to_pivot, 4),
        "distance_to_20d_high_pct": round(distance_to_20d_high, 4),
        "distance_to_55d_high_pct": round(distance_to_55d_high, 4),
        "extension_from_pivot_pct": round(max(extension, 0.0), 4),
        "extended_from_pivot": extension > 5.0,
        "pre_rally_compression": pre_rally_compression,
        "pre_rally_score": round(pre_rally_score, 4),
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


def _daily_close_quote(symbol: str, candles: list[Candle]) -> Quote | None:
    if not candles:
        return None
    candle = candles[-1]
    if not candle.close or candle.close <= 0:
        return None
    return Quote(
        symbol=symbol,
        price=float(candle.close),
        source=f"{candle.source}:analysis-close",
        asof=str(candle.ts),
        open=float(candle.open),
        high=float(candle.high),
        low=float(candle.low),
        close=float(candle.close),
        volume=float(candle.volume or 0.0),
    )


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


def _intraday_candles_match_quote_session(intraday: list[Candle], quote: Quote) -> bool:
    if not intraday:
        return False
    quote_dt = _parse_datetime(getattr(quote, "asof", None))
    candle_dt = _parse_datetime(getattr(intraday[-1], "ts", None))
    if quote_dt is None or candle_dt is None:
        return False
    market_tz = ZoneInfo("America/New_York") if str(quote.source or "").lower().startswith(("alpaca", "polygon", "yahoo")) else ZoneInfo("Asia/Kolkata")
    return quote_dt.astimezone(market_tz).date() == candle_dt.astimezone(market_tz).date()


def _vwap(candles: list[Candle]) -> float | None:
    usable = [candle for candle in candles if candle.volume and candle.high >= candle.low]
    total_volume = sum(float(candle.volume or 0.0) for candle in usable)
    if total_volume <= 0:
        return None
    total_value = sum(((candle.high + candle.low + candle.close) / 3.0) * float(candle.volume or 0.0) for candle in usable)
    return total_value / total_volume


def _daily_returns_pct(candles: list[Candle]) -> list[float]:
    returns: list[float] = []
    for index in range(1, len(candles)):
        prev = _float_or_none(candles[index - 1].close)
        current = _float_or_none(candles[index].close)
        if prev and current is not None:
            returns.append(((current - prev) / prev) * 100.0)
    return returns


def _candle_close_position(candle: Candle) -> float:
    high = _float_or_none(candle.high)
    low = _float_or_none(candle.low)
    close = _float_or_none(candle.close)
    if high is None or low is None or close is None or high <= low:
        return 0.5
    return _clamp((close - low) / (high - low), 0.0, 1.0)


def _accumulation_profile(candles: list[Candle]) -> dict[str, Any]:
    window = candles[-12:] if len(candles) >= 12 else candles[:]
    if len(window) < 4:
        return {"available": False, "score": 0.0}
    up_volume = 0.0
    down_volume = 0.0
    up_days = 0
    down_days = 0
    for index, candle in enumerate(window):
        previous_close = _float_or_none(window[index - 1].close) if index > 0 else _float_or_none(candle.open)
        close = _float_or_none(candle.close)
        volume = float(candle.volume or 0.0)
        if close is None or previous_close is None:
            continue
        if close >= previous_close:
            up_volume += volume
            up_days += 1
        else:
            down_volume += volume
            down_days += 1
    volume_ratio = up_volume / down_volume if down_volume > 0 else (up_volume if up_volume > 0 else 0.0)
    recent_volume = _mean(candle.volume for candle in candles[-5:])
    prior_volume = _mean(candle.volume for candle in candles[-25:-5]) if len(candles) >= 25 else _mean(candle.volume for candle in candles[:-5])
    volume_trend = recent_volume / prior_volume if prior_volume else 0.0
    close_near_high_days = sum(1 for candle in window if _candle_close_position(candle) >= 0.70)
    score = _clamp(
        (0.30 if volume_ratio >= 1.20 else 0.0)
        + (0.25 if up_days >= down_days + 2 else 0.0)
        + (0.20 if volume_trend >= 1.10 else 0.0)
        + (0.15 if close_near_high_days >= max(3, len(window) // 3) else 0.0)
        + (0.10 if up_days > down_days else 0.0),
        0.0,
        1.0,
    )
    return {
        "available": True,
        "score": round(score, 4),
        "up_days": up_days,
        "down_days": down_days,
        "up_down_volume_ratio": _round(volume_ratio),
        "recent_volume_trend": _round(volume_trend),
        "close_near_high_days": close_near_high_days,
    }


def _average_true_range_pct(candles: list[Candle]) -> float | None:
    if len(candles) < 2:
        return None
    ranges: list[float] = []
    for index in range(1, len(candles)):
        candle = candles[index]
        prev_close = _float_or_none(candles[index - 1].close)
        if prev_close is None or prev_close <= 0:
            continue
        true_range = max(
            float(candle.high) - float(candle.low),
            abs(float(candle.high) - prev_close),
            abs(float(candle.low) - prev_close),
        )
        ranges.append((true_range / prev_close) * 100.0)
    return _mean(ranges) if ranges else None


def _market_cap_crore(row: dict[str, Any]) -> float | None:
    value = _float_or_none(
        row.get("market_cap_cr")
        or row.get("mcap_cr")
        or row.get("market_cap_crore")
        or row.get("market_cap")
    )
    if value is None:
        return None
    if value > 10_000_000:
        return value / 10_000_000
    return value


def _negative_news_tone(sentiment: dict[str, Any]) -> bool:
    score = _float_or_none(sentiment.get("score")) or 0.0
    events = [event for event in sentiment.get("events") or [] if isinstance(event, dict)]
    text = " ".join(
        [
            str(sentiment.get("headlines") or ""),
            " ".join(str(event.get("title") or event.get("headline") or event.get("summary") or "") for event in events),
        ]
    ).lower()
    severe_terms = ("fraud", "default", "insolvency", "bankruptcy", "downgrade", "sell rating", "pledge", "forensic")
    has_resolution = any(token in text for token in ("cleared", "settled", "resolved", "approved", "withdrawn", "dismissed"))
    severe_event = any(
        str(event.get("event_type") or "").lower() in {"analyst_downgrade", "debt_liquidity", "fraud_governance"}
        and float(event.get("score") or 0.0) < -0.10
        for event in events
    )
    return bool((score <= -0.30 or severe_event or any(token in text for token in severe_terms)) and not has_resolution)


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


def _parse_datetime(raw: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


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
