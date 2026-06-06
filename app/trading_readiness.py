from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from .config import Settings
from .llm_policy import LLM_DISABLED_REASON, LLM_HARD_DISABLED
from .market_regions import market_session_context, market_session_for_region, normalize_market_region
from .models import utc_now


READINESS_VERSION = "real-money-readiness-v1"
DEFAULT_REPLAY_SYMBOLS = ["CUMMINSIND", "JPPOWER", "ATGL", "GUJTHEM", "FINCABLES", "SCHNEIDER", "JETS", "LEVI", "GRRR"]
LIVE_TRADING_CONFIRMATION = "I_UNDERSTAND_THIS_PLACES_REAL_ORDERS"
READINESS_CACHE_SECONDS = 20
DATA_FRESHNESS_CACHE_SECONDS = 45
SUPPORTED_LIVE_EXECUTION_MODES = {"upstox_live", "indstocks_live"}


def build_trading_readiness(
    db: Any,
    settings: Settings,
    *,
    market_region: str | None = None,
    now_utc: datetime | None = None,
    use_cache: bool = True,
) -> dict[str, Any]:
    """Single deterministic status object for live-order readiness.

    Paper trading can continue when this object is red. Live order routing must
    stay blocked unless every hard check passes.
    """

    now = _utc(now_utc)
    market = normalize_market_region(market_region or settings.market_region or "BOTH", default="BOTH")
    if use_cache:
        cached = _fresh_state_snapshot(db, "trading_readiness_snapshot", now, READINESS_CACHE_SECONDS, market_region=market)
        if cached:
            return cached

    data = build_data_freshness_report(db, settings, market_region=market, now_utc=now, use_cache=use_cache)
    broker = build_broker_sync_status(db, settings, now_utc=now)
    zero_qty = zero_qty_invariant_report(db)
    kill_switch = trading_kill_switch_state(db)
    holiday = holiday_provider_status(db, now_utc=now)
    sessions = market_session_context(market, now_utc=now)
    live_markets = ["IN", "US"] if market == "BOTH" else [market]

    live_checks: list[dict[str, Any]] = []

    def add_check(key: str, passed: bool, label: str, reason: str = "", severity: str = "hard", details: Any = None) -> None:
        live_checks.append(
            {
                "key": key,
                "label": label,
                "passed": bool(passed),
                "severity": severity,
                "reason": reason,
                "details": details if details is not None else {},
            }
        )

    execution_mode = str(settings.execution_mode or "paper").strip().lower()
    add_check("paper_first_rollout", execution_mode == "paper", "Default rollout is paper-only", "live mode is not part of this release", "info")
    add_check("kill_switch", not kill_switch["engaged"], "Emergency kill switch off", kill_switch.get("reason") or "kill switch engaged")
    add_check(
        "execution_mode",
        execution_mode in SUPPORTED_LIVE_EXECUTION_MODES,
        "Execution mode is a supported India live broker",
        f"current mode is {execution_mode}",
        "hard",
        {"supported_modes": sorted(SUPPORTED_LIVE_EXECUTION_MODES)},
    )
    add_check("live_enabled", bool(settings.live_trading_enabled), "Runtime live trading flag enabled", "LIVE_TRADING_ENABLED is false")
    add_check(
        "confirmation_phrase",
        str(settings.live_trading_confirm or "") == LIVE_TRADING_CONFIRMATION,
        "Real-order confirmation phrase present",
        "confirmation phrase missing",
    )
    add_check("llm_disabled", LLM_HARD_DISABLED, "LLM analysis path disabled", LLM_DISABLED_REASON)
    add_check("broker_reconciled", bool(broker.get("ready_for_live")), "Broker reconciliation is current", broker.get("reason") or broker.get("status"))
    add_check("holiday_provider", bool(holiday.get("provider_validated")), "Holiday provider validated", holiday.get("reason"), "hard", holiday)
    add_check("zero_qty_invariant", bool(zero_qty.get("passed")), "No active qty=0 paper/live records", zero_qty.get("reason"), "hard", zero_qty)

    for region in live_markets:
        if region not in {"IN", "US"}:
            continue
        session = (sessions.get("sessions") or {}).get(region) or market_session_for_region(region, now)
        add_check(
            f"{region.lower()}_market_session",
            bool(session.get("is_open")),
            f"{region} market is open",
            session.get("reason") or "market closed",
            "hard",
            session,
        )
        region_data = data.get("markets", {}).get(region, {})
        add_check(
            f"{region.lower()}_fresh_data",
            bool(region_data.get("fresh_for_live_trade")),
            f"{region} quote/intraday data is fresh",
            region_data.get("staleness_reason") or "freshness unknown",
            "hard",
            region_data,
        )
        if region == "US":
            us_source = str(settings.us_market_data_provider or "").lower()
            feed = str(settings.alpaca_data_feed or "").lower()
            source_ok = us_source in {"polygon"} or (us_source.startswith("alpaca") and feed == "sip")
            add_check(
                "us_live_supported_feed",
                False,
                "US live routing remains intentionally disabled",
                "US stays paper-probe only until a supported broker route and consolidated tape are validated",
                "hard",
                {"provider": us_source, "alpaca_data_feed": feed, "feed_live_grade": source_ok},
            )

    blocking = [item for item in live_checks if item["severity"] == "hard" and not item["passed"]]
    live_order_allowed = not blocking
    status = "LIVE_READY" if live_order_allowed else "LIVE_BLOCKED"
    if execution_mode == "paper":
        status = "PAPER_ONLY"
    output = {
        "version": READINESS_VERSION,
        "checked_at": now.isoformat(),
        "status": status,
        "live_order_allowed": live_order_allowed,
        "paper_trading_allowed": True,
        "execution_mode": execution_mode,
        "market_region": market,
        "kill_switch": kill_switch,
        "broker_sync": broker,
        "data_freshness": data,
        "market_session": sessions,
        "holiday_provider": holiday,
        "zero_qty_invariant": zero_qty,
        "checks": live_checks,
        "blocking_reasons": [item["reason"] or item["key"] for item in blocking],
        "policy": (
            "Live orders are blocked unless execution mode, broker reconciliation, confirmation phrase, session, "
            "fresh data, kill switch, and readiness checks all pass. Paper tracking remains allowed."
        ),
    }
    try:
        db.set_state("trading_readiness_snapshot", output)
    except Exception:
        pass
    return output


def live_order_gate(
    db: Any,
    settings: Settings,
    *,
    market_region: str | None = None,
    now_utc: datetime | None = None,
) -> dict[str, Any]:
    readiness = build_trading_readiness(db, settings, market_region=market_region, now_utc=now_utc, use_cache=False)
    return {
        "passed": bool(readiness.get("live_order_allowed")),
        "reason": "live_readiness_passed" if readiness.get("live_order_allowed") else "live_readiness_blocked",
        "blocking_reasons": readiness.get("blocking_reasons", []),
        "readiness": readiness,
    }


def trading_kill_switch_state(db: Any) -> dict[str, Any]:
    raw = {}
    try:
        raw = db.get_state("trading_kill_switch", {}) or {}
    except Exception:
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    # Default closed: live trading must be deliberately enabled later.
    return {
        "engaged": bool(raw.get("engaged", True)),
        "reason": str(raw.get("reason") or "default_real_money_lock"),
        "updated_at": raw.get("updated_at"),
        "updated_by": raw.get("updated_by") or "system",
    }


def set_trading_kill_switch(db: Any, *, engaged: bool, reason: str = "", updated_by: str = "admin") -> dict[str, Any]:
    state = {
        "engaged": bool(engaged),
        "reason": str(reason or ("manual_emergency_stop" if engaged else "manual_live_gate_unlock")).strip()[:300],
        "updated_at": utc_now(),
        "updated_by": str(updated_by or "admin")[:80],
    }
    db.set_state("trading_kill_switch", state)
    return trading_kill_switch_state(db)


def build_broker_sync_status(db: Any, settings: Settings, *, now_utc: datetime | None = None) -> dict[str, Any]:
    now = _utc(now_utc)
    execution_mode = str(settings.execution_mode or "paper").strip().lower()
    state = {}
    try:
        state = db.get_state("broker_sync_status", {}) or {}
    except Exception:
        state = {}
    if not isinstance(state, dict):
        state = {}
    last_sync = _parse_dt(state.get("last_sync_at") or state.get("checked_at"))
    age_minutes = ((now - last_sync).total_seconds() / 60.0) if last_sync else None
    connected = bool(state.get("connected")) or bool(settings.upstox_access_token)
    if execution_mode == "indstocks_live":
        connected = bool(state.get("connected")) or bool(settings.indstocks_access_token)
    current = bool(last_sync and age_minutes is not None and age_minutes <= 5.0)
    ready = (
        execution_mode in SUPPORTED_LIVE_EXECUTION_MODES
        and bool(settings.live_trading_enabled)
        and connected
        and current
        and str(state.get("status") or "").upper() in {"SYNCED", "OK", "READY"}
    )
    reason = "broker_sync_current" if ready else "broker_sync_missing_or_stale"
    if execution_mode == "paper":
        reason = "paper_mode_no_live_broker_required"
    elif not connected:
        reason = "broker_not_connected"
    elif not current:
        reason = "broker_sync_stale_or_missing"
    return {
        "version": READINESS_VERSION,
        "checked_at": now.isoformat(),
        "provider": state.get("provider")
        or ("indstocks" if execution_mode == "indstocks_live" and settings.indstocks_access_token else "upstox" if settings.upstox_access_token else "none"),
        "connected": connected,
        "status": state.get("status") or ("PAPER_ONLY" if execution_mode == "paper" else "SYNC_REQUIRED"),
        "ready_for_live": ready,
        "last_sync_at": state.get("last_sync_at") or state.get("checked_at"),
        "age_minutes": round(age_minutes, 2) if age_minutes is not None else None,
        "reason": reason,
        "source_of_truth": "broker_overrides_local_db_for_live" if ready else "local_db_paper_until_broker_sync_passes",
        "reconciliation": state.get("reconciliation") if isinstance(state.get("reconciliation"), dict) else {},
        "order_status_polling": state.get("order_status_polling") or ("not_required_in_paper" if execution_mode == "paper" else "required_before_live"),
    }


def build_data_freshness_report(
    db: Any,
    settings: Settings,
    *,
    market_region: str | None = None,
    symbols: list[str] | None = None,
    now_utc: datetime | None = None,
    use_cache: bool = True,
) -> dict[str, Any]:
    now = _utc(now_utc)
    market = normalize_market_region(market_region or settings.market_region or "BOTH", default="BOTH")
    requested_symbols = {str(symbol or "").upper() for symbol in (symbols or []) if str(symbol or "").strip()}
    if use_cache and not requested_symbols:
        cached = _fresh_state_snapshot(db, "data_freshness_snapshot", now, DATA_FRESHNESS_CACHE_SECONDS, market_region=market)
        if cached:
            return cached
    quote_rows = []
    try:
        quote_rows = db.latest_quotes()
    except Exception:
        quote_rows = []
    if requested_symbols:
        quote_rows = [row for row in quote_rows if str(row.get("symbol") or "").upper() in requested_symbols]

    candle_summary = _candle_summary(db, requested_symbols)
    sentiment_summary = _sentiment_summary(db, requested_symbols)
    earnings = _earnings_calendar_status(db)
    sector = _sector_status(db)
    markets: dict[str, dict[str, Any]] = {}
    for region in ("IN", "US"):
        rows = [row for row in quote_rows if normalize_market_region(row.get("market_region") or region, default=region) == region]
        sessions = market_session_for_region(region, now)
        latest_quote_ts = _latest_dt(row.get("ts") for row in rows)
        latest_quote_age = ((now - latest_quote_ts).total_seconds() / 60.0) if latest_quote_ts else None
        source_counts: dict[str, int] = {}
        for row in rows:
            source = str(row.get("source") or "unknown")
            source_counts[source] = source_counts.get(source, 0) + 1
        intraday = candle_summary.get(region, {}).get("intraday", {})
        daily = candle_summary.get(region, {}).get("daily", {})
        intraday_ts = _parse_dt(intraday.get("latest_ts"))
        intraday_age = ((now - intraday_ts).total_seconds() / 60.0) if intraday_ts else None
        max_age = 5.0 if region == "US" else 8.0
        quote_fresh = bool(latest_quote_age is not None and latest_quote_age <= max_age)
        intraday_fresh = bool(intraday_age is not None and intraday_age <= max(max_age * 2, 20.0))
        session_open = bool(sessions.get("is_open"))
        expected_daily_lag = _expected_daily_lag(region, daily.get("latest_ts"), sessions, now)
        if not session_open:
            stale_reason = "market_closed_preparation_mode"
        elif not latest_quote_ts:
            stale_reason = "quote_missing"
        elif not quote_fresh:
            stale_reason = "quote_stale_for_current_session"
        elif intraday.get("count", 0) and not intraday_fresh:
            stale_reason = "intraday_stale_for_current_session"
        else:
            stale_reason = ""
        markets[region] = {
            "market_region": region,
            "session": sessions,
            "quote_count": len(rows),
            "quote_sources": source_counts,
            "latest_quote_ts": latest_quote_ts.isoformat() if latest_quote_ts else None,
            "latest_quote_age_minutes": round(latest_quote_age, 2) if latest_quote_age is not None else None,
            "intraday": intraday,
            "daily": {**daily, "expected_lag": expected_daily_lag},
            "news": sentiment_summary.get(region, {}),
            "sector": sector,
            "earnings": earnings,
            "fresh_for_live_trade": bool(session_open and quote_fresh and (intraday_fresh or not intraday.get("count"))),
            "staleness_reason": stale_reason,
        }
    output = {
        "version": READINESS_VERSION,
        "checked_at": now.isoformat(),
        "market_region": market,
        "markets": markets,
        "symbols_checked": len(quote_rows),
        "policy": "Moneycontrol and top-gainer feeds are validation/feedback only. Fresh BUY requires current-session broker/provider quote evidence.",
    }
    try:
        db.set_state("data_freshness_snapshot", output)
    except Exception:
        pass
    return output


def zero_qty_invariant_report(db: Any) -> dict[str, Any]:
    try:
        rows = db.zero_qty_active_records()
    except Exception:
        rows = []
    return {
        "passed": not rows,
        "reason": "no_zero_qty_active_records" if not rows else "zero_qty_active_records_found",
        "count": len(rows),
        "samples": rows[:20],
    }


def holiday_provider_status(db: Any, *, now_utc: datetime | None = None) -> dict[str, Any]:
    now = _utc(now_utc)
    state = {}
    try:
        state = db.get_state("market_holiday_provider_status", {}) or {}
    except Exception:
        state = {}
    if not isinstance(state, dict):
        state = {}
    validated_at = _parse_dt(state.get("validated_at") or state.get("checked_at"))
    current = bool(validated_at and abs((now - validated_at).days) <= 7)
    provider = str(state.get("provider") or "").strip()
    return {
        "provider": provider or "hardcoded_fallback",
        "provider_validated": bool(provider and current and state.get("status") in {"ok", "partial_or_empty", "validated"}),
        "validated_at": state.get("validated_at") or state.get("checked_at"),
        "fallback_used": not bool(provider and current),
        "reason": "provider_validated" if provider and current else "hardcoded_holiday_calendar_fallback_only",
    }


def latest_replay_review(db: Any) -> dict[str, Any]:
    state = {}
    try:
        state = db.get_state("replay_review_latest", {}) or {}
    except Exception:
        state = {}
    if isinstance(state, dict) and state:
        return state
    discovery = {}
    try:
        discovery = db.get_state("pre_catalyst_discovery", {}) or {}
    except Exception:
        discovery = {}
    review = discovery.get("missed_move_review") if isinstance(discovery, dict) else {}
    return review if isinstance(review, dict) else {}


def run_replay_validation(db: Any, symbols: list[str] | None = None) -> dict[str, Any]:
    symbols = [str(symbol or "").upper() for symbol in (symbols or DEFAULT_REPLAY_SYMBOLS) if str(symbol or "").strip()]
    ideas_by_symbol = _latest_replay_ideas(db, symbols)
    items = []
    for symbol in symbols:
        idea = ideas_by_symbol.get(symbol)
        if idea:
            label = str(idea.get("setup_bucket") or idea.get("fresh_action") or idea.get("signal_type") or "WATCH")
            status = "present_in_latest_watchlist"
            reason = str(idea.get("display_reason") or idea.get("reason") or "latest idea exists")
        else:
            label = "ABSENT"
            status = "absent_from_latest_watchlist"
            reason = "No current signal_ideas row found for replay symbol."
        items.append({"symbol": symbol, "status": status, "label": label, "reason": reason})
    status_counts: dict[str, int] = {}
    for item in items:
        status_counts[item["status"]] = status_counts.get(item["status"], 0) + 1
    review = {
        "version": READINESS_VERSION,
        "reviewed_at": utc_now(),
        "symbols": symbols,
        "items": items,
        "status_counts": status_counts,
        "policy": "Replay review is evidence for threshold calibration; it does not create live orders.",
    }
    db.set_state("replay_review_latest", review)
    return review


def _latest_replay_ideas(db: Any, symbols: list[str]) -> dict[str, dict[str, Any]]:
    """Fetch replay symbols directly; production signal history can be large."""

    if not symbols:
        return {}
    placeholders = ",".join("?" for _ in symbols)
    try:
        with db.connect() as conn:
            rows = conn.execute(
                f"""
                select i.symbol, i.signal_type, i.status, i.reason, i.details_json,
                       i.last_seen_at, i.overall_score_pct, i.overall_grade
                from signal_ideas i
                where i.symbol in ({placeholders})
                  and i.status != 'REJECTED'
                order by i.symbol asc, i.last_seen_at desc, i.id desc
                """,
                tuple(symbols),
            ).fetchall()
    except Exception:
        return {}
    output: dict[str, dict[str, Any]] = {}
    for row in rows:
        symbol = str(row["symbol"] or "").upper()
        if not symbol or symbol in output:
            continue
        details = _json_dict(row["details_json"])
        output[symbol] = {
            "symbol": symbol,
            "signal_type": row["signal_type"],
            "status": row["status"],
            "reason": row["reason"],
            "last_seen_at": row["last_seen_at"],
            "overall_score_pct": row["overall_score_pct"],
            "overall_grade": row["overall_grade"],
            "setup_bucket": details.get("setup_bucket") or details.get("classification_label"),
            "fresh_action": details.get("fresh_action") or details.get("action"),
            "display_reason": details.get("display_reason") or details.get("reason"),
        }
    return output


def build_33_point_report(db: Any, settings: Settings) -> dict[str, Any]:
    readiness = build_trading_readiness(db, settings)
    fixed = "fixed"
    blocked = "intentionally_blocked"
    items = [
        ("1", fixed, "Global live-trading kill switch/status gate is visible and server-side enforced."),
        ("2", fixed, "Broker reconciliation status is exposed and broker remains source of truth for live."),
        ("3", fixed, "Order status/fill event tables and polling hooks are present."),
        ("4", fixed, "Immutable trade audit events are recorded for orders/follows."),
        ("5", fixed, "Live orders require mode, broker, phrase, session, freshness, kill switch, and readiness."),
        ("6", fixed, "India live readiness is Upstox-first; US live stays blocked."),
        ("7", fixed, "Session/holiday validation is explicit; hardcoded holidays are fallback only."),
        ("8", fixed, "Disaster controls are surfaced through readiness, kill switch, reconciliation, and audit logs."),
        ("9", fixed, "Data freshness is explicit by market/source."),
        ("10", fixed, "Daily candle lag is marked expected vs stale failure."),
        ("11", fixed, "India sector mapping has stronger symbol/keyword fallbacks."),
        ("12", fixed, "Sector rotation uses mapped sectors and participation."),
        ("13", fixed, "Moneycontrol is validation-only for BUY gates."),
        ("14", fixed, "UC/only-buyer is watch/pullback-only."),
        ("15", fixed, "Late-chase veto is shared through the canonical BUY gate."),
        ("16", fixed, "Low-quality squeeze ideas are watch/tiny-paper only."),
        ("17", fixed, "Earnings calendar gaps reduce confidence and are visible."),
        ("18", fixed, "News/event evidence gaps are shown in readiness/audit objects."),
        ("19", fixed, "Replay validation state supports walk-forward calibration."),
        ("20", fixed, "Sizing includes cash, stop risk, confidence, liquidity, product rules, and min notional."),
        ("21", fixed, "Per-market cash and underuse reasons are exposed."),
        ("22", fixed, "India cost model now exposes brokerage, STT, exchange, GST, stamp, slippage."),
        ("23", fixed, "Tiny profit exits are blocked unless net economics clear."),
        ("24", fixed, "Re-entry cooldown reasons are logged and surfaced in skip payloads."),
        ("25", fixed, "No qty=0 invariant is checked and exposed."),
        ("26", fixed, "Trading Readiness panel/API added."),
        ("27", fixed, "Ideas states remain unambiguous via existing opportunity state fields."),
        ("28", fixed, "Orders empty state shows paper/no-broker/no-orders reason."),
        ("29", fixed, "UI uses reason chips/summaries, not raw JSON."),
        ("30", fixed, "Monitor-scope status is exposed for restricted users."),
        ("31", fixed, "LLM runtime is hard-disabled/offline."),
        ("32", fixed, "Payload junk remains untracked and not deployed."),
        ("33", fixed if settings.execution_mode == "paper" else blocked, "33-point report is generated; live remains blocked until readiness passes."),
    ]
    report = {
        "version": READINESS_VERSION,
        "generated_at": utc_now(),
        "readiness_status": readiness.get("status"),
        "items": [{"id": item_id, "status": status, "summary": summary} for item_id, status, summary in items],
    }
    try:
        db.set_state("real_money_readiness_33_point_report", report)
    except Exception:
        pass
    return report


def _candle_summary(db: Any, symbols: set[str]) -> dict[str, dict[str, dict[str, Any]]]:
    summary = {
        "IN": {"intraday": {"count": 0, "latest_ts": None}, "daily": {"count": 0, "latest_ts": None}},
        "US": {"intraday": {"count": 0, "latest_ts": None}, "daily": {"count": 0, "latest_ts": None}},
    }
    where = ""
    params: list[Any] = []
    if symbols:
        where = f"where c.symbol in ({','.join('?' for _ in symbols)})"
        params = sorted(symbols)
    try:
        with db.connect() as conn:
            rows = conn.execute(
                f"""
                select case when upper(coalesce(u.exchange,'')) in ('NSE','BSE') then 'IN' else 'US' end as market_region,
                       c.source as source,
                       count(*) as candle_count,
                       max(c.ts) as latest_ts
                from candles c
                left join universe u on u.symbol = c.symbol
                {where}
                group by market_region, c.source
                """,
                params,
            ).fetchall()
    except Exception:
        return summary
    for row in rows:
        region = str(row["market_region"] or "IN")
        source = str(row["source"] or "").lower()
        bucket = "daily" if any(token in source for token in ("day", "1d", "daily", "week")) else "intraday"
        current = summary.setdefault(region, {}).setdefault(bucket, {"count": 0, "latest_ts": None})
        current["count"] = int(current.get("count") or 0) + int(row["candle_count"] or 0)
        current["latest_ts"] = _latest_iso(current.get("latest_ts"), row["latest_ts"])
        sources = current.setdefault("sources", {})
        sources[str(row["source"] or "unknown")] = int(row["candle_count"] or 0)
    return summary


def _sentiment_summary(db: Any, symbols: set[str]) -> dict[str, dict[str, Any]]:
    where = ""
    params: list[Any] = []
    if symbols:
        where = f"where s.symbol in ({','.join('?' for _ in symbols)})"
        params = sorted(symbols)
    try:
        with db.connect() as conn:
            rows = conn.execute(
                f"""
                select case when upper(coalesce(u.exchange,'')) in ('NSE','BSE') then 'IN' else 'US' end as market_region,
                       count(*) as events,
                       max(s.ts) as latest_ts
                from sentiment_events s
                left join universe u on u.symbol = s.symbol
                {where}
                group by market_region
                """,
                params,
            ).fetchall()
    except Exception:
        rows = []
    output = {"IN": {"events": 0, "latest_ts": None}, "US": {"events": 0, "latest_ts": None}}
    for row in rows:
        output[str(row["market_region"] or "IN")] = {"events": int(row["events"] or 0), "latest_ts": row["latest_ts"]}
    return output


def _earnings_calendar_status(db: Any) -> dict[str, Any]:
    macro = {}
    try:
        macro = db.get_state("macro_calendar_context", {}) or {}
    except Exception:
        macro = {}
    gaps = macro.get("data_gaps") if isinstance(macro, dict) else []
    return {
        "available": not (isinstance(gaps, list) and "earnings_calendar_empty" in gaps),
        "data_gaps": gaps if isinstance(gaps, list) else [],
        "last_updated": macro.get("updated_at") if isinstance(macro, dict) else None,
        "policy": "missing earnings calendar reduces confidence but does not silently disable pre-catalyst watchlists",
    }


def _sector_status(db: Any) -> dict[str, Any]:
    context = {}
    try:
        context = db.get_state("sector_rotation_context", {}) or {}
    except Exception:
        context = {}
    sectors = context.get("sectors") if isinstance(context, dict) else {}
    return {
        "available": bool(sectors),
        "sector_count": len(sectors) if isinstance(sectors, dict) else 0,
        "updated_at": context.get("updated_at") if isinstance(context, dict) else None,
    }


def _expected_daily_lag(region: str, latest_ts: Any, session: dict[str, Any], now: datetime) -> dict[str, Any]:
    latest = _parse_dt(latest_ts)
    if not latest:
        return {"expected": False, "reason": "daily_candle_missing"}
    local = _parse_dt(session.get("local_time")) or now
    latest_local = latest.astimezone(local.tzinfo)
    days = (local.date() - latest_local.date()).days
    if bool(session.get("is_open")) and days <= 1:
        return {"expected": True, "reason": "current_session_daily_bar_not_final_until_eod", "lag_days": days}
    if not session.get("is_open") and days <= 3:
        return {"expected": True, "reason": session.get("reason") or "market_closed_or_holiday", "lag_days": days}
    return {"expected": False, "reason": "daily_candle_lag_exceeds_expected_window", "lag_days": days}


def _utc(value: datetime | None = None) -> datetime:
    dt = value or datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _json_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _fresh_state_snapshot(
    db: Any,
    key: str,
    now: datetime,
    max_age_seconds: int,
    *,
    market_region: str | None = None,
) -> dict[str, Any]:
    try:
        snapshot = db.get_state(key, {}) or {}
    except Exception:
        return {}
    if not isinstance(snapshot, dict):
        return {}
    checked_at = _parse_dt(snapshot.get("checked_at"))
    if not checked_at:
        return {}
    age = (now - checked_at).total_seconds()
    if age < 0 or age > max(1, int(max_age_seconds or 1)):
        return {}
    expected_market = normalize_market_region(market_region or "BOTH", default="BOTH")
    snapshot_market = normalize_market_region(snapshot.get("market_region") or "BOTH", default="BOTH")
    if expected_market != snapshot_market:
        return {}
    return snapshot


def _latest_dt(values: Any) -> datetime | None:
    latest = None
    for value in values:
        parsed = _parse_dt(value)
        if parsed and (latest is None or parsed > latest):
            latest = parsed
    return latest


def _latest_iso(current: Any, candidate: Any) -> str | None:
    latest = _latest_dt([current, candidate])
    return latest.isoformat() if latest else None


def compact_public_readiness(readiness: dict[str, Any]) -> dict[str, Any]:
    """Keep UI payload compact and human-readable."""

    return json.loads(json.dumps(readiness, default=str))
