from __future__ import annotations

import html
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

import httpx

from .market_regions import market_region_for_row
from .models import utc_now


_MONEYCONTROL_CATEGORY_URLS = {
    "top-gainers": "https://www.moneycontrol.com/stocks/market-stats/top-gainers-nse/",
    "volume-shockers": "https://www.moneycontrol.com/stocks/market-stats/volume-shockers-nse/",
    "52-week-high": "https://www.moneycontrol.com/stocks/market-stats/52-week-high-nse/",
    "only-buyers": "https://www.moneycontrol.com/stocks/market-stats/only-buyers-nse/",
    "price-shockers": "https://www.moneycontrol.com/stocks/market-stats/price-shockers-nse/",
}

_CATEGORY_EVENT_TYPES = {
    "top-gainers": "TOP_GAINER",
    "volume-shockers": "VOLUME_SHOCKER",
    "52-week-high": "52_WEEK_HIGH",
    "only-buyers": "ONLY_BUYERS",
    "price-shockers": "PRICE_SHOCKER",
}

_YAHOO_SCREENER_EVENT_TYPES = {
    "day_gainers": "TOP_GAINER",
    "most_actives": "MOST_ACTIVE",
}


@dataclass(frozen=True)
class MarketActionEvent:
    symbol: str
    name: str
    event_types: list[str]
    source: str
    market_action_score: float
    strategy: str
    trade_window: str
    reason: str
    pct_change: float | None = None
    price: float | None = None
    high: float | None = None
    low: float | None = None
    open: float | None = None
    prev_close: float | None = None
    volume: float | None = None
    avg_volume: float | None = None
    volume_multiplier: float | None = None
    value_crore: float | None = None
    vwap: float | None = None
    market_region: str = "IN"
    sector: str = ""
    share_url: str = ""
    asof: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class MarketActionRadar:
    def __init__(self, settings: Any) -> None:
        self.settings = settings
        self.enabled = bool(getattr(settings, "market_action_radar_enabled", True))
        self.limit = max(0, int(getattr(settings, "market_action_radar_limit", 40) or 40))
        self.timeout_seconds = max(2.0, float(getattr(settings, "market_action_radar_timeout_seconds", 6.0) or 6.0))
        self.us_min_gain_pct = max(0.0, float(getattr(settings, "market_action_us_min_gain_pct", 3.0) or 3.0))
        self.us_volume_multiplier = max(1.0, float(getattr(settings, "market_action_us_volume_multiplier", 1.8) or 1.8))
        self.us_near_52w_pct = max(0.1, float(getattr(settings, "market_action_us_near_52w_pct", 2.0) or 2.0))
        self._yahoo_crumb: str | None = None
        categories = str(
            getattr(
                settings,
                "market_action_moneycontrol_categories",
                "top-gainers,volume-shockers,52-week-high,only-buyers,price-shockers",
            )
            or ""
        )
        self.categories = [
            item.strip()
            for item in categories.split(",")
            if item.strip() in _MONEYCONTROL_CATEGORY_URLS
        ]
        yahoo_screeners = str(getattr(settings, "market_action_yahoo_screeners", "day_gainers,most_actives") or "")
        self.yahoo_screeners = [
            item.strip()
            for item in yahoo_screeners.split(",")
            if item.strip() in _YAHOO_SCREENER_EVENT_TYPES
        ]

    async def scan(self, universe: list[dict[str, Any]]) -> dict[str, Any]:
        if not self.enabled:
            return {"enabled": False, "reason": "market_action_radar_disabled", "events": [], "events_by_symbol": {}}
        if self.limit <= 0:
            return {"enabled": False, "reason": "market_action_radar_limit_zero", "events": [], "events_by_symbol": {}}
        in_rows = [row for row in universe if market_region_for_row(row) == "IN"]
        us_rows = [row for row in universe if market_region_for_row(row) == "US"]
        in_symbols = {
            str(row.get("symbol") or "").strip().upper()
            for row in in_rows
        }
        if not in_symbols and not us_rows:
            return {"enabled": False, "reason": "no_supported_symbols_for_market_action_radar", "events": [], "events_by_symbol": {}}

        errors: list[str] = []
        events_by_symbol: dict[str, MarketActionEvent] = {}
        async with httpx.AsyncClient(
            timeout=self.timeout_seconds,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0", "Accept": "text/html,application/json"},
        ) as client:
            if in_symbols:
                for category in self.categories:
                    try:
                        response = await client.get(_MONEYCONTROL_CATEGORY_URLS[category])
                        response.raise_for_status()
                    except Exception as exc:
                        errors.append(f"{category}: {exc.__class__.__name__}: {str(exc)[:140]}")
                        continue
                    for raw in parse_moneycontrol_market_stats(response.text, category):
                        symbol = str(raw.get("symbol") or "").strip().upper()
                        if not symbol or symbol not in in_symbols:
                            continue
                        event = build_market_action_event(raw, category)
                        if not event:
                            continue
                        existing = events_by_symbol.get(symbol)
                        events_by_symbol[symbol] = merge_market_action_events(existing, event) if existing else event
            if us_rows:
                for event in await self._scan_us_yahoo_market_action(client, us_rows, errors):
                    existing = events_by_symbol.get(event.symbol)
                    events_by_symbol[event.symbol] = merge_market_action_events(existing, event) if existing else event

        events = sorted(events_by_symbol.values(), key=lambda item: item.market_action_score, reverse=True)[: self.limit]
        events_by_symbol = {event.symbol: event for event in events}
        by_market = {
            "IN": sum(1 for event in events if event.market_region == "IN"),
            "US": sum(1 for event in events if event.market_region == "US"),
        }
        return {
            "enabled": True,
            "source": "market_action_radar",
            "sources": {
                "IN": "moneycontrol_market_stats" if in_symbols else None,
                "US": "yahoo_quote_market_action" if us_rows else None,
            },
            "categories": self.categories,
            "yahoo_screeners": self.yahoo_screeners,
            "scanned_at": utc_now(),
            "events_found": len(events),
            "symbols": [event.symbol for event in events],
            "events": [event.to_dict() for event in events],
            "events_by_symbol": {symbol: event.to_dict() for symbol, event in events_by_symbol.items()},
            "by_market": by_market,
            "errors": errors[:5],
        }

    async def _scan_us_yahoo_market_action(
        self,
        client: httpx.AsyncClient,
        rows: list[dict[str, Any]],
        errors: list[str],
    ) -> list[MarketActionEvent]:
        rows_by_yahoo = {_yahoo_symbol(row): row for row in rows if str(row.get("symbol") or "").strip()}
        screener_events = await self._fetch_yahoo_screener_events(client, set(rows_by_yahoo), errors)
        events: list[MarketActionEvent] = []
        symbols = list(rows_by_yahoo)
        for index in range(0, len(symbols), 50):
            chunk = symbols[index : index + 50]
            try:
                data = await self._yahoo_json(
                    client,
                    "https://query1.finance.yahoo.com/v7/finance/quote",
                    params={"symbols": ",".join(chunk)},
                )
                quotes = data.get("quoteResponse", {}).get("result", [])
            except Exception as exc:
                errors.append(f"yahoo_quotes: {exc.__class__.__name__}: {str(exc)[:140]}")
                continue
            for item in quotes:
                yahoo_symbol = str(item.get("symbol") or "").strip().upper()
                row = rows_by_yahoo.get(yahoo_symbol)
                if not row:
                    continue
                event = build_yahoo_market_action_event(
                    item,
                    row,
                    screener_events.get(yahoo_symbol, []),
                    min_gain_pct=self.us_min_gain_pct,
                    volume_shocker_multiplier=self.us_volume_multiplier,
                    near_52w_pct=self.us_near_52w_pct,
                )
                if event:
                    events.append(event)
        return events

    async def _fetch_yahoo_screener_events(
        self,
        client: httpx.AsyncClient,
        yahoo_symbols: set[str],
        errors: list[str],
    ) -> dict[str, list[str]]:
        if not yahoo_symbols or not self.yahoo_screeners:
            return {}
        output: dict[str, list[str]] = {}
        for screener in self.yahoo_screeners:
            try:
                data = await self._yahoo_json(
                    client,
                    "https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved",
                    params={"scrIds": screener, "count": max(self.limit * 3, 100)},
                )
                results = (data.get("finance", {}).get("result") or [])
            except Exception as exc:
                errors.append(f"yahoo_screener:{screener}: {exc.__class__.__name__}: {str(exc)[:140]}")
                continue
            event_type = _YAHOO_SCREENER_EVENT_TYPES.get(screener)
            if not event_type:
                continue
            for result in results:
                for quote in result.get("quotes") or []:
                    symbol = str(quote.get("symbol") or "").strip().upper()
                    if symbol in yahoo_symbols:
                        output.setdefault(symbol, []).append(event_type)
        return output

    async def _yahoo_json(self, client: httpx.AsyncClient, url: str, params: dict[str, Any]) -> dict[str, Any]:
        response = await client.get(url, params=params)
        if response.status_code not in {401, 403}:
            response.raise_for_status()
            return response.json()
        crumb = await self._ensure_yahoo_crumb(client)
        if not crumb:
            response.raise_for_status()
        response = await client.get(url, params={**params, "crumb": crumb})
        if response.status_code in {401, 403}:
            self._yahoo_crumb = None
        response.raise_for_status()
        return response.json()

    async def _ensure_yahoo_crumb(self, client: httpx.AsyncClient) -> str | None:
        if self._yahoo_crumb:
            return self._yahoo_crumb
        try:
            await client.get("https://fc.yahoo.com", headers={"Accept": "*/*", "User-Agent": "Mozilla/5.0"})
            response = await client.get(
                "https://query1.finance.yahoo.com/v1/test/getcrumb",
                headers={"Accept": "*/*", "User-Agent": "Mozilla/5.0"},
            )
            response.raise_for_status()
        except Exception:
            return None
        crumb = response.text.strip()
        if not crumb or "<" in crumb or " " in crumb:
            return None
        self._yahoo_crumb = crumb
        return crumb


def parse_moneycontrol_market_stats(raw_html: str, category: str) -> list[dict[str, Any]]:
    payload = _next_data_payload(raw_html)
    if not payload:
        return []
    rows: list[dict[str, Any]] = []
    for candidate in _walk_json_lists(payload):
        if not _looks_like_moneycontrol_stock_list(candidate):
            continue
        for item in candidate:
            if isinstance(item, dict):
                row = dict(item)
                row["_market_stats_category"] = category
                rows.append(row)
        if rows:
            break
    return rows


def build_market_action_event(raw: dict[str, Any], category: str) -> MarketActionEvent | None:
    symbol = str(raw.get("symbol") or "").strip().upper()
    if not symbol:
        return None
    labels = raw.get("stockLabel") if isinstance(raw.get("stockLabel"), list) else []
    event_types = [_CATEGORY_EVENT_TYPES.get(category, "MARKET_ACTION")]
    for label in labels:
        if not isinstance(label, dict):
            continue
        text = f"{label.get('shortname') or ''} {label.get('name') or ''} {label.get('statement') or ''}".lower()
        if "vol" in text and "shocker" in text:
            event_types.append("VOLUME_SHOCKER")
        if "52" in text and "high" in text:
            event_types.append("52_WEEK_HIGH")
        if "ath" in text or "all time high" in text:
            event_types.append("ALL_TIME_HIGH")
        if "upper" in text or "only buyer" in text:
            event_types.append("ONLY_BUYERS")

    pct_change = _number(raw.get("currPerChange") or raw.get("perChange"))
    volume_multiplier = _number(raw.get("volMultiplier"))
    if volume_multiplier is not None and volume_multiplier >= 1.8:
        event_types.append("VOLUME_SHOCKER")
    if pct_change is not None and pct_change >= 6.0:
        event_types.append("STRONG_INTRADAY_GAIN")
    event_types = _unique(event_types)
    score = _market_action_score(event_types, pct_change, volume_multiplier, _number(raw.get("value")))
    strategy, trade_window = _market_action_strategy(event_types, pct_change, volume_multiplier)
    name = str(raw.get("stockName") or raw.get("name") or symbol).strip()
    reason_parts = [_event_type_label(item) for item in event_types[:4]]
    if pct_change is not None:
        reason_parts.append(f"{pct_change:.2f}% move")
    if volume_multiplier is not None:
        reason_parts.append(f"{volume_multiplier:.2f}x volume")
    return MarketActionEvent(
        symbol=symbol,
        name=name,
        event_types=event_types,
        source=f"moneycontrol:{category}",
        market_action_score=score,
        strategy=strategy,
        trade_window=trade_window,
        reason=", ".join(reason_parts),
        pct_change=pct_change,
        price=_number(raw.get("currentPrice")),
        high=_number(raw.get("high")),
        low=_number(raw.get("low")),
        open=_number(raw.get("open")),
        prev_close=_number(raw.get("prevClose")),
        volume=_number(raw.get("volume")),
        avg_volume=_number(raw.get("avgVol")),
        volume_multiplier=volume_multiplier,
        value_crore=_number(raw.get("value")),
        vwap=_number(raw.get("vwap")),
        sector=str(raw.get("slug") or "").split("/")[0].replace("-", " ").strip(),
        share_url=str(raw.get("shareUrl") or ""),
        asof=str(raw.get("dttime") or ""),
    )


def build_yahoo_market_action_event(
    raw: dict[str, Any],
    row: dict[str, Any],
    screener_event_types: list[str] | None = None,
    *,
    min_gain_pct: float = 3.0,
    volume_shocker_multiplier: float = 1.8,
    near_52w_pct: float = 2.0,
) -> MarketActionEvent | None:
    symbol = str(row.get("symbol") or raw.get("symbol") or "").strip().upper()
    if not symbol:
        return None
    event_types = [str(item).strip().upper() for item in (screener_event_types or []) if str(item or "").strip()]
    pct_change = _number(raw.get("regularMarketChangePercent") or raw.get("regularMarketPercentChange"))
    price = _number(raw.get("regularMarketPrice") or raw.get("postMarketPrice") or raw.get("preMarketPrice"))
    high = _number(raw.get("regularMarketDayHigh"))
    low = _number(raw.get("regularMarketDayLow"))
    open_price = _number(raw.get("regularMarketOpen"))
    prev_close = _number(raw.get("regularMarketPreviousClose"))
    volume = _number(raw.get("regularMarketVolume"))
    avg_volume = _number(raw.get("averageDailyVolume10Day")) or _number(raw.get("averageDailyVolume3Month"))
    volume_multiplier = (volume / avg_volume) if volume and avg_volume else None
    week_52_high = _number(raw.get("fiftyTwoWeekHigh"))
    near_52w = False
    if week_52_high and week_52_high > 0:
        threshold = week_52_high * (1.0 - near_52w_pct / 100.0)
        near_52w = bool((price and price >= threshold) or (high and high >= threshold))
    if pct_change is not None and pct_change >= min_gain_pct:
        event_types.append("TOP_GAINER")
    if pct_change is not None and pct_change >= max(6.0, min_gain_pct * 1.6):
        event_types.append("STRONG_INTRADAY_GAIN")
    if volume_multiplier is not None and volume_multiplier >= volume_shocker_multiplier:
        event_types.append("VOLUME_SHOCKER")
    if near_52w:
        event_types.append("52_WEEK_HIGH")
    if pct_change is not None and pct_change >= 4.0 and volume_multiplier and volume_multiplier >= 1.3:
        event_types.append("PRICE_SHOCKER")
    event_types = _unique(event_types)
    material_events = set(event_types) - {"MOST_ACTIVE"}
    if not material_events:
        return None
    traded_value = (price or 0.0) * (volume or 0.0)
    value_score = (traded_value / 1_000_000.0) if traded_value else None
    score = _market_action_score(event_types, pct_change, volume_multiplier, value_score)
    strategy, trade_window = _market_action_strategy(event_types, pct_change, volume_multiplier)
    name = str(raw.get("shortName") or raw.get("longName") or row.get("name") or symbol).strip()
    reason_parts = [_event_type_label(item) for item in event_types[:4]]
    if pct_change is not None:
        reason_parts.append(f"{pct_change:.2f}% move")
    if volume_multiplier is not None:
        reason_parts.append(f"{volume_multiplier:.2f}x volume")
    if near_52w and week_52_high:
        reason_parts.append("near 52-week high")
    return MarketActionEvent(
        symbol=symbol,
        name=name,
        event_types=event_types,
        source="yahoo:market_action",
        market_action_score=score,
        strategy=strategy,
        trade_window=trade_window,
        reason=", ".join(reason_parts),
        pct_change=pct_change,
        price=price,
        high=high,
        low=low,
        open=open_price,
        prev_close=prev_close,
        volume=volume,
        avg_volume=avg_volume,
        volume_multiplier=volume_multiplier,
        value_crore=None,
        vwap=None,
        market_region="US",
        sector=str(row.get("sector") or raw.get("sector") or ""),
        share_url=f"https://finance.yahoo.com/quote/{_yahoo_symbol(row)}",
        asof=_epoch_to_iso(raw.get("regularMarketTime")),
    )


def merge_market_action_events(existing: MarketActionEvent | None, incoming: MarketActionEvent) -> MarketActionEvent:
    if existing is None:
        return incoming
    event_types = _unique([*existing.event_types, *incoming.event_types])
    score = max(existing.market_action_score, incoming.market_action_score)
    strategy, trade_window = _market_action_strategy(event_types, incoming.pct_change or existing.pct_change, incoming.volume_multiplier or existing.volume_multiplier)
    primary = incoming if incoming.market_action_score >= existing.market_action_score else existing
    return MarketActionEvent(
        **{
            **primary.to_dict(),
            "event_types": event_types,
            "source": f"{existing.source},{incoming.source}",
            "market_action_score": score,
            "strategy": strategy,
            "trade_window": trade_window,
            "reason": ", ".join(_unique([*_split_reason(existing.reason), *_split_reason(incoming.reason)]))[:260],
        }
    )


def _next_data_payload(raw_html: str) -> dict[str, Any] | None:
    match = re.search(r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>', raw_html, flags=re.I | re.S)
    if not match:
        return None
    try:
        return json.loads(html.unescape(match.group(1)))
    except json.JSONDecodeError:
        return None


def _walk_json_lists(value: Any) -> list[list[Any]]:
    found: list[list[Any]] = []
    if isinstance(value, list):
        found.append(value)
        for item in value:
            found.extend(_walk_json_lists(item))
    elif isinstance(value, dict):
        for item in value.values():
            found.extend(_walk_json_lists(item))
    return found


def _looks_like_moneycontrol_stock_list(value: list[Any]) -> bool:
    stock_like = 0
    for item in value[:8]:
        if not isinstance(item, dict):
            continue
        keys = set(item)
        if {"symbol", "stockName", "currentPrice"}.issubset(keys) and ("perChange" in keys or "currPerChange" in keys):
            stock_like += 1
    return stock_like >= 1


def _market_action_score(
    event_types: list[str],
    pct_change: float | None,
    volume_multiplier: float | None,
    value_crore: float | None,
) -> float:
    score = 20.0
    weights = {
        "TOP_GAINER": 18.0,
        "VOLUME_SHOCKER": 20.0,
        "52_WEEK_HIGH": 24.0,
        "ALL_TIME_HIGH": 28.0,
        "ONLY_BUYERS": 26.0,
        "PRICE_SHOCKER": 14.0,
        "STRONG_INTRADAY_GAIN": 12.0,
        "MOST_ACTIVE": 8.0,
    }
    score += sum(weights.get(item, 0.0) for item in set(event_types))
    if pct_change is not None:
        score += min(max(pct_change, 0.0) * 2.2, 22.0)
    if volume_multiplier is not None:
        score += min(max(volume_multiplier - 1.0, 0.0) * 4.0, 18.0)
    if value_crore is not None:
        score += min(max(value_crore, 0.0) / 100.0, 8.0)
    return round(max(0.0, min(100.0, score)), 2)


def _market_action_strategy(
    event_types: list[str],
    pct_change: float | None,
    volume_multiplier: float | None,
) -> tuple[str, str]:
    events = set(event_types)
    if "ONLY_BUYERS" in events:
        return "circuit_demand_lock", "watch_for_pullback"
    if ("52_WEEK_HIGH" in events or "ALL_TIME_HIGH" in events) and ("VOLUME_SHOCKER" in events or (volume_multiplier or 0.0) >= 1.8):
        return "52_week_high_volume_breakout", "actionable_if_vwap_holds"
    if "VOLUME_SHOCKER" in events and (pct_change or 0.0) >= 3.0:
        return "market_action_momentum", "actionable_if_not_extended"
    if "PRICE_SHOCKER" in events:
        return "price_shocker_reversal_breakout", "confirm_before_entry"
    return "top_gainer_momentum", "confirm_before_entry"


def _event_type_label(value: str) -> str:
    return value.lower().replace("_", " ")


def _number(value: Any) -> float | None:
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    raw = raw.replace(",", "").replace("%", "")
    if raw.lower() in {"infinity", "infinity.00", "infin,ity.00", "nan", "-"}:
        return None
    raw = re.sub(r"[^0-9.\-]", "", raw)
    if raw in {"", "-", ".", "-."}:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _yahoo_symbol(row: dict[str, Any]) -> str:
    explicit = str(row.get("yahoo_symbol") or "").strip().upper()
    if explicit:
        return explicit
    exchange = str(row.get("exchange") or "").strip().upper()
    symbol = str(row.get("symbol") or "").strip().upper()
    if exchange == "NSE":
        return f"{symbol}.NS"
    if exchange == "BSE":
        return f"{symbol}.BO"
    return symbol


def _epoch_to_iso(value: Any) -> str:
    try:
        timestamp = float(value)
    except (TypeError, ValueError):
        return ""
    if timestamp <= 0:
        return ""
    try:
        return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()
    except (OSError, OverflowError, ValueError):
        return ""


def _unique(values: list[str]) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = str(value or "").strip()
        if not normalized or normalized in seen:
            continue
        output.append(normalized)
        seen.add(normalized)
    return output


def _split_reason(value: str) -> list[str]:
    return [item.strip() for item in str(value or "").split(",") if item.strip()]
