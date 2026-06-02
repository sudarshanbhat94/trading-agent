from __future__ import annotations

import time
from datetime import date, datetime, timedelta
from typing import Any

from .config import Settings
from .db import Database
from .market_regions import normalize_market_region
from .models import utc_now


class MacroCalendarService:
    def __init__(self, settings: Settings, db: Database, earnings_calendar: dict[str, str] | None = None) -> None:
        self.settings = settings
        self.db = db
        self.earnings_calendar = {str(k).upper(): v for k, v in (earnings_calendar or {}).items()}
        self._persistent_earnings_cache: tuple[float, dict[str, str]] | None = None

    def _persistent_earnings_calendar(self) -> dict[str, str]:
        cached = self._persistent_earnings_cache
        if cached and time.monotonic() - cached[0] < 300:
            return dict(cached[1])
        stored = self.db.get_state("earnings_calendar", {})
        if not isinstance(stored, dict):
            fallback = self.db.get_state("pre_catalyst_calendar_enrichment", {})
            stored = fallback.get("earnings_by_symbol", {}) if isinstance(fallback, dict) else {}
        if not isinstance(stored, dict):
            result = dict(self.earnings_calendar)
            self._persistent_earnings_cache = (time.monotonic(), result)
            return dict(result)
        merged = dict(self.earnings_calendar)
        for symbol, value in stored.items():
            normalized = str(symbol or "").upper()
            if not normalized:
                continue
            if isinstance(value, dict):
                date_value = value.get("catalyst_date") or value.get("date") or value.get("earnings_date")
            else:
                date_value = value
            if date_value:
                merged[normalized] = str(date_value)
        self._persistent_earnings_cache = (time.monotonic(), merged)
        return merged

    async def event_context_for_cycle(self) -> dict[str, Any]:
        if not self.settings.enable_macro_calendar:
            return {"enabled": False, "updated_at": utc_now(), "events": [], "data_gap": "macro_calendar_disabled"}
        today = _today_ist()
        events = self.upcoming_events(30, today)
        context = {"enabled": True, "updated_at": utc_now(), "events": events[:30], "next_10": events[:10]}
        self.db.set_state("macro_calendar_context", context)
        self._log("INFO", "macro_calendar_cycle", "Macro calendar refreshed", {"events": len(events)})
        return context

    def event_context_for_date(
        self,
        event_date: date | str | None = None,
        symbol: str | None = None,
        market_region: str | None = None,
    ) -> dict[str, Any]:
        if not self.settings.enable_macro_calendar:
            return _neutral_event("macro_calendar_disabled")
        if normalize_market_region(market_region or "IN") == "US":
            neutral = _neutral_event("us_macro_calendar_not_configured")
            neutral.update({"enabled": True, "symbol": symbol, "market_region": "US", "date": (_coerce_date(event_date) or _today_ist()).isoformat()})
            return neutral
        day = _coerce_date(event_date) or _today_ist()
        monthly_expiry = _last_thursday(day.year, day.month)
        next_weekly = _nearest_weekly_expiry(day)
        is_monthly_expiry_day = day == monthly_expiry
        is_monthly_expiry_eve = day == monthly_expiry - timedelta(days=1)
        is_weekly_expiry_day = day.weekday() == 3 and not is_monthly_expiry_day
        is_expiry_day = is_monthly_expiry_day
        is_expiry_week = abs((monthly_expiry - day).days) <= 2 or abs((next_weekly - day).days) <= 2
        rbi_dates = [item["date"] for item in _static_events(day.year) + _static_events(day.year + 1) if item["type"] == "rbi_mpc_placeholder"]
        budget_dates = [item["date"] for item in _static_events(day.year) + _static_events(day.year + 1) if item["type"] == "union_budget_placeholder"]
        is_rbi_week = any(abs((_coerce_date(value) - day).days) <= 3 for value in rbi_dates if _coerce_date(value))
        is_budget_week = any(abs((_coerce_date(value) - day).days) <= 5 for value in budget_dates if _coerce_date(value))
        earnings_calendar = self._persistent_earnings_calendar()
        earnings_date = _coerce_date(earnings_calendar.get(str(symbol or "").upper()))
        earnings_days_away = (earnings_date - day).days if earnings_date else None
        earnings_trading_days_away = _trading_days_between(day, earnings_date) if earnings_date else None
        event_score = 0.0
        if is_monthly_expiry_day:
            event_score = max(event_score, 0.4)
        elif is_monthly_expiry_eve:
            event_score = max(event_score, 0.35)
        elif is_weekly_expiry_day:
            event_score = max(event_score, 0.15)
        elif is_expiry_week:
            event_score = max(event_score, 0.25)
        if is_rbi_week:
            event_score = max(event_score, 0.6)
        if is_budget_week:
            event_score = max(event_score, 0.7)
        if earnings_trading_days_away is not None and 0 <= earnings_trading_days_away <= 10:
            event_score = max(event_score, 0.9)
        recommended = "hold_for_clarity" if event_score > 0.5 else "reduce_size" if 0.3 <= event_score <= 0.5 else "normal"
        data_gaps = []
        if not earnings_calendar:
            data_gaps.append("earnings_calendar_empty")
        return {
            "enabled": True,
            "date": day.isoformat(),
            "symbol": symbol,
            "is_expiry_day": is_expiry_day,
            "is_monthly_expiry_day": is_monthly_expiry_day,
            "is_monthly_expiry_eve": is_monthly_expiry_eve,
            "is_weekly_expiry_day": is_weekly_expiry_day,
            "is_expiry_week": is_expiry_week,
            "expiry_type": "monthly" if is_monthly_expiry_day else "weekly" if is_weekly_expiry_day else None,
            "is_rbi_week": is_rbi_week,
            "is_budget_week": is_budget_week,
            "earnings_days_away": earnings_days_away,
            "earnings_trading_days_away": earnings_trading_days_away,
            "has_high_impact_event": event_score >= 0.3,
            "event_risk_score": round(event_score, 3),
            "recommended_action": recommended,
            "data_gaps": data_gaps,
        }

    def upcoming_events(self, days: int = 30, start: date | None = None) -> list[dict[str, Any]]:
        start = start or _today_ist()
        end = start + timedelta(days=days)
        events: list[dict[str, Any]] = []
        for year in {start.year, end.year}:
            events.extend(_static_events(year))
            for month in range(1, 13):
                expiry = _last_thursday(year, month)
                events.append({"date": expiry.isoformat(), "type": "monthly_derivatives_expiry", "scope": "market_wide"})
        cursor = start
        while cursor <= end:
            if cursor.weekday() == 3 and cursor != _last_thursday(cursor.year, cursor.month):
                events.append({"date": cursor.isoformat(), "type": "weekly_expiry", "scope": "market_wide"})
            cursor += timedelta(days=1)
        for symbol, value in self._persistent_earnings_calendar().items():
            earnings_date = _coerce_date(value)
            if earnings_date:
                events.append({"date": earnings_date.isoformat(), "type": "earnings", "scope": symbol, "symbols": [symbol]})
        return sorted(
            [event for event in events if start <= _coerce_date(event.get("date")) <= end],
            key=lambda item: (item["date"], item["type"]),
        )

    def _log(self, level: str, event: str, message: str, details: Any | None = None) -> None:
        try:
            self.db.insert_agent_log(level, "macro_calendar", event, message, details)
        except Exception:
            pass


def _static_events(year: int) -> list[dict[str, Any]]:
    rbi_months = [2, 4, 6, 8, 10, 12]
    events = [
        {"date": f"{year}-02-01", "type": "union_budget_placeholder", "scope": "market_wide"},
        {"date": f"{year}-06-15", "type": "advance_tax_payment", "scope": "market_wide"},
        {"date": f"{year}-09-15", "type": "advance_tax_payment", "scope": "market_wide"},
        {"date": f"{year}-12-15", "type": "advance_tax_payment", "scope": "market_wide"},
        {"date": f"{year}-03-15", "type": "advance_tax_payment", "scope": "market_wide"},
    ]
    for month in rbi_months:
        events.append({"date": date(year, month, min(8, 28)).isoformat(), "type": "rbi_mpc_placeholder", "scope": "market_wide"})
    return events


def _last_thursday(year: int, month: int) -> date:
    if month == 12:
        cursor = date(year, 12, 31)
    else:
        cursor = date(year, month + 1, 1) - timedelta(days=1)
    while cursor.weekday() != 3:
        cursor -= timedelta(days=1)
    return cursor


def _nearest_weekly_expiry(day: date) -> date:
    days_ahead = (3 - day.weekday()) % 7
    return day + timedelta(days=days_ahead)


def _today_ist() -> date:
    return (datetime.utcnow() + timedelta(hours=5, minutes=30)).date()


def _coerce_date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    if value in (None, ""):
        return None
    text = str(value)[:10]
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        return None


def _trading_days_between(start: date, end: date | None) -> int | None:
    if end is None:
        return None
    step = 1 if end >= start else -1
    cursor = start
    days = 0
    while cursor != end:
        cursor += timedelta(days=step)
        if cursor.weekday() < 5:
            days += step
    return days


def _neutral_event(reason: str) -> dict[str, Any]:
    return {
        "enabled": False,
        "is_expiry_day": False,
        "is_monthly_expiry_day": False,
        "is_monthly_expiry_eve": False,
        "is_weekly_expiry_day": False,
        "is_expiry_week": False,
        "expiry_type": None,
        "is_rbi_week": False,
        "is_budget_week": False,
        "earnings_days_away": None,
        "earnings_trading_days_away": None,
        "has_high_impact_event": False,
        "event_risk_score": 0.0,
        "recommended_action": "normal",
        "data_gap": reason,
    }
