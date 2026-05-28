from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .market_regions import market_region_for_row, market_session_for_region
from .models import Candle, Quote


HARD = "hard"
SOFT = "soft"
INFO = "info"


def assess_phase2_data_readiness(
    *,
    row: dict[str, Any],
    quote: Quote,
    timeframe_candles: dict[str, list[Candle]],
    sentiment: dict[str, Any],
    delivery_data: dict[str, Any] | None,
    options_data: dict[str, Any] | None,
    sector_context: dict[str, Any] | None,
    market_breadth: dict[str, Any] | None,
    macro_event_context: dict[str, Any] | None,
    institutional_context: dict[str, Any] | None,
    full_spectrum: dict[str, Any] | None = None,
    execution_mode: str = "paper",
) -> dict[str, Any]:
    """Market-specific Phase-2 data checklist for fresh trade decisions.

    Fresh trade decisions require market-specific quote, candle, event, and
    flow confirmations. For US swing signals, fresh Yahoo quotes can be used
    as a reference-grade price source when SIP/Polygon is unavailable.
    """

    market = market_region_for_row(row)
    mode = str(execution_mode or "paper").strip().lower()
    full_spectrum = full_spectrum or {}
    timeframe_candles = timeframe_candles or {}
    daily = timeframe_candles.get("daily") or timeframe_candles.get("analysis") or []
    intraday = timeframe_candles.get("intraday") or []
    delivery_data = delivery_data or {}
    options_data = options_data or {}
    sector_context = sector_context or {}
    market_breadth = market_breadth or {}
    macro_event_context = macro_event_context or {}
    institutional_context = institutional_context or {}

    checks: list[dict[str, Any]] = []

    def check(key: str, label: str, available: bool, severity: str, source: Any = None, note: str = "") -> None:
        checks.append(
            {
                "key": key,
                "label": label,
                "available": bool(available),
                "severity": severity,
                "source": source,
                "note": note,
            }
        )

    quote_source = str(quote.source or "")
    daily_source = _last_source(daily)
    intraday_source = _last_source(intraday)
    sentiment_status = str(sentiment.get("status") or "").upper()
    sentiment_headlines = sentiment.get("headlines") if isinstance(sentiment.get("headlines"), list) else []
    sentiment_events = sentiment.get("events") if isinstance(sentiment.get("events"), list) else []
    volume_ratio = _first_float(
        (full_spectrum.get("liquidity_profile") or {}).get("volume_ratio_20"),
        (full_spectrum.get("price_volume_divergence") or {}).get("volume_ratio_20"),
        (full_spectrum.get("primary_filters") or {}).get("volume_ratio_min_1_5"),
    )
    if volume_ratio is None:
        volume_ratio = _volume_ratio(daily)

    check("quote_price", "Valid quote price", float(quote.price or 0.0) > 0, HARD, quote_source)
    check("daily_history", "Daily history for trend/base checks", len(daily) >= 55, HARD, daily_source, f"{len(daily)} candles")
    check("volume_baseline", "Volume baseline / unusual-volume check", volume_ratio is not None, HARD, daily_source)
    check("sentiment_news", "News/sentiment source checked", sentiment_status != "DATA_MISSING", SOFT, sentiment.get("source"))

    freshness_gate = _fresh_market_data_gate(market, quote, intraday)

    if market == "US":
        quote_age_minutes = _quote_age_minutes(quote.asof)
        yahoo_quote_ok = (
            _source_has(quote_source, ("yahoo",))
            and quote_age_minutes is not None
            and quote_age_minutes <= 20
        )
        yahoo_daily_confirmation_ok = yahoo_quote_ok and len(daily) >= 55 and _source_has(str(daily_source or ""), ("yahoo",))
        consolidated_source_ok = _source_has(quote_source, ("alpaca-sip", "polygon"))
        sip_minute_ok = len(intraday) >= 20 and _source_has(str(intraday_source or ""), ("alpaca-sip", "polygon"))
        iex_quote_ok = _source_has(quote_source, ("alpaca-iex", "iex"))
        iex_minute_ok = len(intraday) >= 20 and _source_has(str(intraday_source or ""), ("alpaca-iex", "iex"))
        paper_iex_reference_ok = mode == "paper" and iex_quote_ok
        paper_iex_minute_ok = mode == "paper" and iex_minute_ok and (paper_iex_reference_ok or yahoo_quote_ok)
        realtime_source_ok = consolidated_source_ok or yahoo_quote_ok or paper_iex_reference_ok
        minute_source_ok = sip_minute_ok or yahoo_daily_confirmation_ok or paper_iex_minute_ok
        quote_note = (
            f"Yahoo reference quote age {quote_age_minutes:.1f}m"
            if yahoo_quote_ok and quote_age_minutes is not None
            else "Alpaca IEX reference quote; paper validation only"
            if paper_iex_reference_ok
            else ""
        )
        minute_note = (
            "Yahoo reference mode uses fresh quote plus daily bars for swing-signal confirmation"
            if yahoo_daily_confirmation_ok and not sip_minute_ok
            else "Alpaca IEX bars are venue-limited; paper validation uses reduced size and separate freshness/live-confirmation gates"
            if paper_iex_minute_ok and not sip_minute_ok
            else f"{len(intraday)} candles"
        )
        earnings_checked = not _has_gap(macro_event_context, "earnings_calendar_empty")
        analyst_checked = _contains_any(sentiment_headlines, ("analyst", "upgrade", "downgrade", "price target")) or _events_have(
            sentiment_events,
            ("analyst_upgrade", "analyst_downgrade"),
        )
        sec_checked = _contains_any(
            sentiment_headlines,
            ("sec", "8-k", "10-q", "10-k", "filing", "edgar"),
        ) or bool(row.get("sec_filings_checked_at") or row.get("cik"))
        options_flow_checked = _source_has(str(options_data.get("source") or ""), ("options_flow", "alpaca", "polygon")) or bool(
            options_data.get("flow_available")
        )
        short_interest_checked = bool(row.get("short_interest") or row.get("short_interest_pct") or options_data.get("short_interest"))

        check(
            "us_realtime_quote",
            "US fresh quote from Alpaca SIP, Polygon, or Yahoo reference",
            realtime_source_ok,
            HARD,
            quote_source,
            quote_note,
        )
        check(
            "us_minute_bars",
            "US minute bars or Yahoo swing-confirmation bars",
            minute_source_ok,
            HARD,
            intraday_source or daily_source,
            minute_note,
        )
        check(
            "us_consolidated_tape",
            "US consolidated SIP/Polygon tape",
            consolidated_source_ok,
            SOFT,
            quote_source,
            "Paper may use IEX/Yahoo reference data with reduced size; live-grade execution needs consolidated tape.",
        )
        check("us_earnings_date", "US earnings-date/event calendar", earnings_checked, HARD, macro_event_context.get("source"))
        check("us_sec_filings", "SEC filings / EDGAR event check", sec_checked, SOFT, row.get("cik") or sentiment.get("source"))
        check("us_analyst_revisions", "Analyst revisions / rating changes", analyst_checked, SOFT, sentiment.get("source"))
        check("us_options_flow", "Options flow / options activity", options_flow_checked, SOFT, options_data.get("source"))
        check("us_short_interest", "Short-interest context", short_interest_checked, SOFT, row.get("short_interest_source"))
    else:
        live_quote_ok = _source_has(quote_source, ("upstox", "kite", "nubra", "indstocks-live"))
        intraday_ok = len(intraday) >= 20 and _source_has(intraday_source, ("upstox", "kite", "nubra", "indstocks-live"))
        delivery_ok = bool(delivery_data.get("available")) or _first_float(delivery_data.get("delivery_pct"), delivery_data.get("delivery_score")) is not None
        feeds = institutional_context.get("feeds") if isinstance(institutional_context.get("feeds"), dict) else {}
        flags = _symbol_flags(institutional_context, row)
        announcements_ok = _feed_ok(feeds.get("corporate_announcements")) or bool(flags.get("official_announcements_count") is not None)
        bulk_block_ok = _feed_ok(feeds.get("bulk_deals")) or bool(flags.get("bulk_deals_count") is not None)
        fii_dii_ok = _feed_ok(feeds.get("fii_dii"))
        india_vix_ok = bool(((feeds.get("indices") or {}).get("items") or {}).get("INDIA VIX"))
        breadth_ok = bool(market_breadth.get("breadth_regime")) and market_breadth.get("breadth_regime") != "neutral_unavailable"
        option_status = str(options_data.get("status") or "")
        fno_not_applicable = option_status == "not_fno_no_stock_options" or str(options_data.get("data_gap") or "") == "symbol_not_in_fno_no_stock_options"
        option_ok = fno_not_applicable or option_status == "ok" or bool(options_data.get("available"))

        check("in_live_quote", "India live quote from Upstox/Kite/Nubra", live_quote_ok, HARD, quote_source)
        check("in_intraday_candles", "India intraday candles", intraday_ok, HARD, intraday_source, f"{len(intraday)} candles")
        check("in_delivery_pct", "Delivery percentage / delivery trend", delivery_ok, SOFT, delivery_data.get("source"))
        check("in_corporate_announcements", "NSE/BSE corporate announcements", announcements_ok, SOFT, "nse_bse_corporate_announcements")
        check("in_bulk_block_deals", "Bulk/block deal feed", bulk_block_ok, SOFT, "nse_bse_bulk_block_deals")
        check("in_fii_dii", "FII/DII flow", fii_dii_ok, SOFT, "nse_fii_dii")
        check("in_india_vix", "India VIX", india_vix_ok, SOFT, "nse_indices")
        check("in_sector_breadth", "Sector / market breadth", breadth_ok, SOFT, market_breadth.get("source"))
        check("in_options_oi", "Option chain / OI for F&O names", option_ok, SOFT, options_data.get("source"))

    hard_gaps = [item for item in checks if not item["available"] and item["severity"] == HARD]
    soft_gaps = [item for item in checks if not item["available"] and item["severity"] == SOFT]
    available = [item for item in checks if item["available"]]
    score = max(0.0, 100.0 - (len(hard_gaps) * 18.0) - (len(soft_gaps) * 5.0))
    return {
        "phase": 2,
        "market_region": market,
        "mode": "strict",
        "execution_mode": mode,
        "screening_ready": not any(item["key"] in {"quote_price", "daily_history"} for item in hard_gaps),
        "trade_decision_ready": not hard_gaps,
        "score_pct": round(score, 1),
        "grade": _grade(score),
        "hard_gaps": hard_gaps,
        "soft_gaps": soft_gaps,
        "available": available,
        "missing_data": [item["key"] for item in hard_gaps + soft_gaps],
        "fresh_market_data_gate": freshness_gate,
        "sources": {
            "quote": quote_source,
            "daily": daily_source,
            "intraday": intraday_source,
            "sentiment": sentiment.get("source"),
        },
        "policy": (
            "US BUY signals may use fresh Yahoo reference quotes plus daily bars for swing confirmation when SIP/Polygon is unavailable. SEC, analyst, options, and short-interest context remain explicit evidence gaps when unavailable."
            if market == "US"
            else "India fresh trades require valid live broker quote/candles and volume baseline. Delivery, announcements, breadth, and OI gaps stay visible and reduce sizing, but do not block a price-volume paper opportunity by themselves."
        ),
    }


def data_readiness_score(context: dict[str, Any]) -> float:
    readiness = context.get("data_readiness") if isinstance(context.get("data_readiness"), dict) else {}
    return max(min((float(readiness.get("score_pct") or 0.0) / 100.0) * 2.0 - 1.0, 1.0), -1.0)


def _source_has(source: str, tokens: tuple[str, ...]) -> bool:
    normalized = str(source or "").lower()
    return any(token.lower() in normalized for token in tokens)


def _last_source(candles: list[Candle]) -> str | None:
    return str(candles[-1].source) if candles else None


def _has_gap(payload: dict[str, Any], gap: str) -> bool:
    gaps = payload.get("data_gaps")
    return isinstance(gaps, list) and gap in gaps


def _quote_age_minutes(asof: Any) -> float | None:
    if not asof:
        return None
    try:
        parsed = datetime.fromisoformat(str(asof).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max((datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds() / 60.0, 0.0)


def _fresh_market_data_gate(market: str, quote: Quote, intraday: list[Candle]) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    try:
        session = market_session_for_region(market, now)
    except Exception:
        session = {"region": market, "is_open": False, "status": "unknown", "reason": "session_lookup_failed"}
    quote_dt = _parse_ts(quote.asof)
    latest_intraday_dt = _parse_ts(intraday[-1].ts) if intraday else None
    source = str(quote.source or "").lower()
    max_age = _max_quote_age_minutes(market, source)
    quote_age = ((now - quote_dt.astimezone(timezone.utc)).total_seconds() / 60.0) if quote_dt else None

    def blocked(reason: str, message: str, *, stale_label: str = "DATA_STALE_WATCH") -> dict[str, Any]:
        return {
            "passed": False,
            "reason": reason,
            "message": message,
            "label": stale_label,
            "market_region": market,
            "is_market_open": bool(session.get("is_open")),
            "session": session,
            "quote_source": quote.source,
            "quote_asof": quote.asof,
            "quote_age_minutes": round(quote_age, 2) if quote_age is not None else None,
            "max_quote_age_minutes": max_age,
            "latest_intraday_ts": intraday[-1].ts if intraday else None,
            "checked_at": now.isoformat(),
            "policy": "watch_only_until_current_session_data_confirms",
        }

    if "moneycontrol" in source:
        return blocked(
            "moneycontrol_not_live_trade_feed",
            "Moneycontrol market-action data is validation/feedback only and cannot be treated as a live trade quote.",
        )
    if not quote_dt:
        return blocked("quote_timestamp_missing", "Fresh BUY requires a quote timestamp from the current market session.")
    if session.get("is_open") is not True:
        return blocked("market_closed_live_buy_blocked", "Market is closed; use this only as prep/watch data until the next live session.")
    if quote_age is not None and quote_age < -2:
        return blocked("quote_timestamp_in_future", "Quote timestamp is ahead of the current clock; wait for a clean feed refresh.")
    if quote_age is None or quote_age > max_age:
        return blocked("quote_stale_for_current_session", "Quote is too old for a fresh BUY decision.")
    if not _timestamp_in_current_session_date(quote_dt, session):
        return blocked("quote_not_current_session", "Quote timestamp is not from the current valid market session.")
    if latest_intraday_dt is not None and not _timestamp_in_current_session_date(latest_intraday_dt, session):
        return blocked("intraday_not_current_session", "Latest intraday candle is not from the current valid market session.")
    if latest_intraday_dt is not None:
        intraday_age = (now - latest_intraday_dt.astimezone(timezone.utc)).total_seconds() / 60.0
        if intraday_age > max(max_age * 2, 20.0):
            return blocked("intraday_stale_for_current_session", "Latest intraday candle is too old for live confirmation.")

    return {
        "passed": True,
        "reason": "current_session_data",
        "message": "Quote and intraday timestamps are acceptable for the current live session.",
        "label": "LIVE_DATA_READY",
        "market_region": market,
        "is_market_open": True,
        "session": session,
        "quote_source": quote.source,
        "quote_asof": quote.asof,
        "quote_age_minutes": round(quote_age, 2) if quote_age is not None else None,
        "max_quote_age_minutes": max_age,
        "latest_intraday_ts": intraday[-1].ts if intraday else None,
        "checked_at": now.isoformat(),
        "policy": "fresh_buy_allowed_to_reach_quality_gates",
    }


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _timestamp_in_current_session_date(value: datetime, session: dict[str, Any]) -> bool:
    local_time = _parse_ts(session.get("local_time"))
    if local_time is None:
        return True
    return value.astimezone(local_time.tzinfo).date() == local_time.date()


def _max_quote_age_minutes(market: str, source: str) -> float:
    normalized = str(source or "").lower()
    if market == "US" and "yahoo" in normalized:
        return 20.0
    if market == "US":
        return 5.0
    return 8.0


def _contains_any(values: list[Any], needles: tuple[str, ...]) -> bool:
    haystack = " ".join(str(value or "").lower() for value in values)
    return any(needle in haystack for needle in needles)


def _events_have(events: list[Any], labels: tuple[str, ...]) -> bool:
    for event in events:
        if not isinstance(event, dict):
            continue
        label = str(event.get("label") or event.get("event_type") or event.get("category") or "").lower()
        if label in labels:
            return True
    return False


def _symbol_flags(institutional_context: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
    flags = institutional_context.get("symbol_flags") if isinstance(institutional_context.get("symbol_flags"), dict) else {}
    return flags.get(str(row.get("symbol") or "").upper()) or {}


def _feed_ok(feed: Any) -> bool:
    return isinstance(feed, dict) and str(feed.get("status") or "").lower() in {"ok", "partial_or_empty"}


def _volume_ratio(candles: list[Candle]) -> float | None:
    volumes = [float(item.volume or 0.0) for item in candles if float(item.volume or 0.0) > 0]
    if len(volumes) < 21:
        return None
    avg = sum(volumes[-21:-1]) / 20
    return volumes[-1] / avg if avg > 0 else None


def _first_float(*values: Any) -> float | None:
    for value in values:
        if value in (None, ""):
            continue
        if isinstance(value, bool):
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _grade(score: float) -> str:
    if score >= 85:
        return "A"
    if score >= 70:
        return "B"
    if score >= 55:
        return "C"
    if score >= 40:
        return "D"
    return "F"
