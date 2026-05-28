from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo


INDIA_EXCHANGES = {"NSE", "BSE"}
US_EXCHANGES = {"NASDAQ", "NYSE", "AMEX", "ARCA", "NYSEARCA", "BATS", "OTC"}

INDIA_TRADING_HOLIDAYS: dict[str, str] = {
    # NSE/BSE equity trading holidays for calendar year 2026.
    "2026-01-26": "Republic Day",
    "2026-03-03": "Holi",
    "2026-03-26": "Ram Navami",
    "2026-03-31": "Mahavir Jayanti",
    "2026-04-03": "Good Friday",
    "2026-04-14": "Dr. Baba Saheb Ambedkar Jayanti",
    "2026-05-01": "Maharashtra Day",
    "2026-05-28": "Bakri Id",
    "2026-06-26": "Muharram",
    "2026-09-14": "Ganesh Chaturthi",
    "2026-10-02": "Mahatma Gandhi Jayanti",
    "2026-10-20": "Dussehra",
    "2026-11-10": "Diwali-Balipratipada",
    "2026-11-24": "Prakash Gurpurb Sri Guru Nanak Dev",
    "2026-12-25": "Christmas",
}


def normalize_market_region(value: Any, default: str = "IN") -> str:
    region = str(value or default).strip().upper()
    return region if region in {"IN", "US", "BOTH"} else default


def exchange_for_row(row: dict[str, Any]) -> str:
    return str(row.get("exchange") or "").strip().upper()


def market_region_for_row(row: dict[str, Any]) -> str:
    exchange = exchange_for_row(row)
    if exchange in INDIA_EXCHANGES:
        return "IN"
    if exchange in US_EXCHANGES:
        return "US"
    yahoo_symbol = str(row.get("yahoo_symbol") or "").strip().upper()
    if yahoo_symbol.endswith(".NS") or yahoo_symbol.endswith(".BO"):
        return "IN"
    return "US" if exchange else "IN"


def row_matches_market_region(row: dict[str, Any], market_region: str | None) -> bool:
    region = normalize_market_region(market_region or "BOTH", default="BOTH")
    if region == "BOTH":
        return True
    return market_region_for_row(row) == region


def filter_universe_for_market(
    universe: list[dict[str, Any]],
    market_region: str | None,
) -> list[dict[str, Any]]:
    region = normalize_market_region(market_region or "BOTH", default="BOTH")
    if region == "BOTH":
        return universe
    return [row for row in universe if market_region_for_row(row) == region]


def market_session_for_region(region: str, now_utc: datetime | None = None) -> dict[str, Any]:
    normalized = normalize_market_region(region, default="IN")
    if normalized == "BOTH":
        raise ValueError("market_session_for_region expects IN or US, not BOTH")
    now = now_utc or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    if normalized == "US":
        tz = ZoneInfo("America/New_York")
        open_at = time(9, 30)
        close_at = time(16, 0)
        label = "US regular session"
    else:
        tz = ZoneInfo("Asia/Kolkata")
        open_at = time(9, 15)
        close_at = time(15, 30)
        label = "NSE regular session"

    local_now = now.astimezone(tz)
    open_dt = datetime.combine(local_now.date(), open_at, tzinfo=tz)
    close_dt = datetime.combine(local_now.date(), close_at, tzinfo=tz)
    is_weekday = local_now.weekday() < 5
    holiday_name = _market_holiday_name(normalized, local_now.date())
    is_holiday = bool(holiday_name)
    is_open = is_weekday and not is_holiday and open_dt <= local_now <= close_dt
    next_open = _next_session_open(normalized, local_now, open_at, tz)
    closed_reason = "trading_holiday" if is_holiday else "outside_regular_session_or_weekend"
    return {
        "region": normalized,
        "label": label,
        "is_open": is_open,
        "status": "open" if is_open else "closed",
        "local_time": local_now.isoformat(),
        "open_time": open_dt.isoformat(),
        "close_time": close_dt.isoformat(),
        "next_open": next_open.isoformat(),
        "reason": "regular_session" if is_open else closed_reason,
        "holiday": holiday_name or "",
    }


def market_session_context(
    market_region: str | None,
    universe: list[dict[str, Any]] | None = None,
    now_utc: datetime | None = None,
) -> dict[str, Any]:
    requested = normalize_market_region(market_region or "BOTH", default="BOTH")
    regions = ["IN", "US"] if requested == "BOTH" else [requested]
    if universe:
        present_regions = {market_region_for_row(row) for row in universe}
        regions = [region for region in regions if region in present_regions] or regions
    sessions = {region: market_session_for_region(region, now_utc) for region in regions}
    open_regions = [region for region, session in sessions.items() if session.get("is_open")]
    return {
        "market_region": requested,
        "checked_at": (now_utc or datetime.now(timezone.utc)).isoformat(),
        "is_any_market_open": bool(open_regions),
        "open_regions": open_regions,
        "closed_regions": [region for region in regions if region not in open_regions],
        "sessions": sessions,
        "data_policy": "scan_open_markets_only",
    }


def filter_universe_for_open_markets(
    universe: list[dict[str, Any]],
    session_context: dict[str, Any],
) -> list[dict[str, Any]]:
    open_regions = set(session_context.get("open_regions") or [])
    if not open_regions:
        return []
    return [row for row in universe if market_region_for_row(row) in open_regions]


def _next_session_open(region: str, local_now: datetime, open_at: time, tz: ZoneInfo) -> datetime:
    candidate_date = local_now.date()
    if local_now.time() >= open_at or local_now.weekday() >= 5:
        candidate_date += timedelta(days=1)
    while candidate_date.weekday() >= 5 or _market_holiday_name(region, candidate_date):
        candidate_date += timedelta(days=1)
    return datetime.combine(candidate_date, open_at, tzinfo=tz)


def _market_holiday_name(region: str, session_date: date) -> str:
    if normalize_market_region(region, default="IN") == "IN":
        return INDIA_TRADING_HOLIDAYS.get(session_date.isoformat(), "")
    return ""
