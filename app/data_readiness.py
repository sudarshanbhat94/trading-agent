from __future__ import annotations

from typing import Any

from .market_regions import market_region_for_row
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

    Free or delayed daily data is acceptable for broad screening only. Fresh
    trade decisions, including paper trades, require the same market-specific
    quote, candle, event, and flow confirmations as live analysis.
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

    if market == "US":
        realtime_source_ok = _source_has(quote_source, ("alpaca-sip", "polygon"))
        minute_source_ok = len(intraday) >= 20 and _source_has(intraday_source, ("alpaca-sip", "polygon"))
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
            "US consolidated real-time quote from Alpaca SIP/Polygon",
            realtime_source_ok,
            HARD,
            quote_source,
        )
        check(
            "us_minute_bars",
            "US minute bars from Alpaca SIP/Polygon",
            minute_source_ok,
            HARD,
            intraday_source,
            f"{len(intraday)} candles",
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
        check("in_delivery_pct", "Delivery percentage / delivery trend", delivery_ok, HARD, delivery_data.get("source"))
        check("in_corporate_announcements", "NSE/BSE corporate announcements", announcements_ok, HARD, "nse_bse_corporate_announcements")
        check("in_bulk_block_deals", "Bulk/block deal feed", bulk_block_ok, SOFT, "nse_bse_bulk_block_deals")
        check("in_fii_dii", "FII/DII flow", fii_dii_ok, SOFT, "nse_fii_dii")
        check("in_india_vix", "India VIX", india_vix_ok, SOFT, "nse_indices")
        check("in_sector_breadth", "Sector / market breadth", breadth_ok, HARD, market_breadth.get("source"))
        check("in_options_oi", "Option chain / OI for F&O names", option_ok, SOFT if fno_not_applicable else HARD, options_data.get("source"))

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
        "sources": {
            "quote": quote_source,
            "daily": daily_source,
            "intraday": intraday_source,
            "sentiment": sentiment.get("source"),
        },
        "policy": (
            "Free or delayed US data is screening-only unless it satisfies the same real-time quote, minute-bar, and earnings checks required for trade decisions. SEC, analyst, options, and short-interest context remain explicit evidence gaps when unavailable."
            if market == "US"
            else "India fresh trades need live broker candles plus NSE/BSE event, delivery, breadth, and OI context where applicable, whether the execution is paper or live."
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
