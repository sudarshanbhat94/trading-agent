from __future__ import annotations

import argparse
import asyncio
import csv
import json
import math
import os
import random
import sqlite3
import statistics
import sys
import time
from collections import defaultdict
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote as url_quote
from zoneinfo import ZoneInfo

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import Settings
from app.indicators import technical_snapshot
from app.models import Candle, Quote
from app.opportunity_scanner import OpportunityScanner
from app.market_day_regime import compute_market_day_regime
from app.raw_entry_model import evaluate_raw_entry
from app.trade_economics import exit_economics, minimum_auto_follow_notional


IST = ZoneInfo("Asia/Kolkata")


class HistoricalOpportunityScanner(OpportunityScanner):
    """Scanner variant that evaluates quote age as-of the replay bar, not wall-clock now."""

    def _metrics(self, price: float, quote: Quote, candles: list[Candle], market_region: str) -> dict[str, Any]:
        metrics = super()._metrics(price, quote, candles, market_region)
        metrics["quote_age_seconds"] = 0.0
        return metrics


async def run(args: argparse.Namespace) -> dict[str, Any]:
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    rows = _load_universe(Path(args.universe), args.symbols)
    if args.max_symbols and args.max_symbols > 0:
        rows = rows[: args.max_symbols]

    settings = replace(
        Settings(),
        dynamic_scan_sentiment_enabled=bool(args.include_sentiment),
        dynamic_scan_require_active_setup=False,
        dynamic_scan_min_score=0.0,
    )
    scanner = HistoricalOpportunityScanner(settings)
    scanner.sentiment_enabled = bool(args.include_sentiment)
    scanner.require_active_setup = False
    scanner.min_score = 0.0

    fetched = await _fetch_all(
        rows,
        str(args.base_url).rstrip("/"),
        str(args.interval),
        start,
        end,
        int(args.daily_lookback_days),
        int(args.concurrency),
        Path(args.cache_dir) if args.cache_dir else None,
        cache_only=bool(args.cache_only),
        request_spacing_seconds=max(0.0, float(args.request_spacing_seconds or 0.0)),
    )
    if args.fetch_only:
        return {
            "engine": "upstox_history_raw_opportunity_v1",
            "mode": "fetch_only",
            "window": {"start": start.isoformat(), "end": end.isoformat(), "interval": args.interval},
            "universe_symbols": len(rows),
            "symbols_with_daily": sum(1 for item in fetched.values() if item["daily"]),
            "symbols_with_intraday": sum(1 for item in fetched.values() if item["intraday"]),
            "daily_candles": sum(len(item["daily"]) for item in fetched.values()),
            "intraday_candles": sum(len(item["intraday"]) for item in fetched.values()),
            "fetch_diagnostics": _fetch_diagnostics(fetched),
        }

    sentiment_history = (
        _load_historical_sentiment(Path(args.database), start, end, int(args.sentiment_max_age_days))
        if args.include_sentiment and args.database
        else {}
    )
    universe_by_symbol = {str(row["symbol"]).upper(): row for row in rows}
    states_by_ts = _historical_quote_states(fetched, start, end)
    trades: list[dict[str, Any]] = []
    first_entry_keys: set[tuple[str, str]] = set()
    label_counts: dict[str, int] = defaultdict(int)
    blocker_counts: dict[str, int] = defaultdict(int)
    setup_counts: dict[str, int] = defaultdict(int)
    selected_counts: list[int] = []
    cycle_count = 0
    entry_ready_seen = 0

    for ts in sorted(states_by_ts):
        symbol_states = states_by_ts[ts]
        if not symbol_states:
            continue
        cycle_count += 1
        quotes: dict[str, Quote] = {}
        candle_sets: dict[str, dict[str, list[Candle]]] = {}
        sentiment_by_symbol: dict[str, dict[str, Any]] = {}
        cycle_rows: list[dict[str, Any]] = []
        market_day = _market_day(ts)
        for symbol, state in symbol_states.items():
            row = universe_by_symbol.get(symbol)
            if not row:
                continue
            daily_prior = _daily_before(fetched[symbol]["daily"], market_day)
            if len(daily_prior) < 55:
                continue
            quote = state["quote"]
            quotes[symbol] = quote
            candle_sets[symbol] = {
                "daily": daily_prior,
                "analysis": daily_prior,
                "intraday": [state["bar"]],
            }
            sentiment = _sentiment_at(
                sentiment_history.get(symbol) or [],
                ts,
                max_age_days=int(args.sentiment_max_age_days),
            )
            if sentiment:
                sentiment_by_symbol[symbol] = sentiment
            cycle_rows.append(row)
        if not cycle_rows:
            continue
        market_day_regime = compute_market_day_regime(
            cycle_rows,
            quotes,
            candle_sets,
            {
                "enabled": True,
                "breadth_regime": "neutral",
                "advance_decline_ratio": _advance_decline_ratio(cycle_rows, quotes, candle_sets),
            },
            market_region="IN",
        )
        result = scanner.rank(cycle_rows, quotes, candle_sets, sentiment_by_symbol=sentiment_by_symbol)
        selected_counts.append(len(result.selected_universe))
        for selected in result.selected_universe:
            symbol = str(selected.get("symbol") or "").upper()
            quote = quotes.get(symbol)
            if not quote:
                continue
            daily_prior = candle_sets.get(symbol, {}).get("daily") or []
            tech = _technical_for(daily_prior, quote)
            scan = selected.get("_opportunity_scan") if isinstance(selected.get("_opportunity_scan"), dict) else {}
            sentiment = sentiment_by_symbol.get(symbol) or {
                "score": 0.0,
                "confidence": 0.0,
                "headline_count": 0,
                "headlines": [],
                "events": [],
                "status": "NO_TIMESTAMP_SAFE_NEWS",
            }
            context = {
                "symbol": symbol,
                "market_region": "IN",
                "sector": row.get("sector"),
                "quote": quote.to_dict(),
                "technical_math": tech.to_dict(),
                "sentiment": sentiment,
                "data_readiness": {
                    "market_region": "IN",
                    "trade_decision_ready": True,
                    "sources": {
                        "quote": "upstox-history",
                        "intraday": f"upstox-history:{args.interval}",
                        "daily": "upstox-history:day",
                    },
                },
                "opportunity_scan": scan,
                "market_day_regime": market_day_regime,
                "full_spectrum_analysis": {
                    "liquidity_profile": scan.get("liquidity_profile") if isinstance(scan.get("liquidity_profile"), dict) else {},
                    "trade_plan": {},
                },
            }
            model = evaluate_raw_entry(context, settings)
            label = str(model.get("decision_label") or "UNKNOWN")
            label_counts[label] += 1
            setup_counts[str(model.get("setup_family") or "none")] += 1
            for blocker in model.get("entry_blockers") or []:
                if isinstance(blocker, dict):
                    blocker_counts[str(blocker.get("reason") or "unknown")] += 1
            if label != "ENTRY_READY":
                continue
            entry_ready_seen += 1
            entry_key = (market_day.isoformat(), symbol)
            if entry_key in first_entry_keys:
                continue
            first_entry_keys.add(entry_key)
            trade = _simulate_trade(symbol, quote, model, fetched[symbol]["intraday"], settings)
            if trade:
                trades.append(trade)

    return {
        "engine": "upstox_history_raw_opportunity_v1",
        "window": {"start": start.isoformat(), "end": end.isoformat(), "interval": args.interval},
        "include_sentiment": bool(args.include_sentiment),
        "sentiment_symbols": len(sentiment_history),
        "sentiment_max_age_days": int(args.sentiment_max_age_days),
        "universe_symbols": len(rows),
        "symbols_with_daily": sum(1 for item in fetched.values() if item["daily"]),
        "symbols_with_intraday": sum(1 for item in fetched.values() if item["intraday"]),
        "daily_candles": sum(len(item["daily"]) for item in fetched.values()),
        "intraday_candles": sum(len(item["intraday"]) for item in fetched.values()),
        "fetch_diagnostics": _fetch_diagnostics(fetched),
        "cycles": cycle_count,
        "avg_selected_per_cycle": round(statistics.mean(selected_counts), 2) if selected_counts else 0.0,
        "entry_ready_decisions_seen": entry_ready_seen,
        "first_symbol_day_entries": len(trades),
        "label_counts": dict(sorted(label_counts.items())),
        "setup_family_counts": dict(sorted(setup_counts.items(), key=lambda item: item[1], reverse=True)[:12]),
        "blocker_counts": dict(sorted(blocker_counts.items(), key=lambda item: item[1], reverse=True)[:12]),
        "overall": _summary(trades),
        "by_day": _group_summary(trades, "entry_day"),
        "by_setup": _group_summary(trades, "setup_family"),
        "worst": _sample(trades, reverse=False, limit=int(args.sample_limit)),
        "best": _sample(trades, reverse=True, limit=int(args.sample_limit)),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read-only raw_opportunity_v1 replay using Upstox historical candles.")
    parser.add_argument("--universe", default="data/universe.csv")
    parser.add_argument("--start", required=True, help="Inclusive YYYY-MM-DD market date.")
    parser.add_argument("--end", required=True, help="Exclusive YYYY-MM-DD market date.")
    parser.add_argument("--database", default="")
    parser.add_argument("--include-sentiment", action="store_true")
    parser.add_argument("--sentiment-max-age-days", type=int, default=7)
    parser.add_argument("--base-url", default="https://api.upstox.com/v2")
    parser.add_argument("--interval", default="30minute")
    parser.add_argument("--daily-lookback-days", type=int, default=420)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--request-spacing-seconds", type=float, default=0.25)
    parser.add_argument("--cache-dir", default="")
    parser.add_argument("--cache-only", action="store_true", help="Do not fetch missing symbols; replay cached coverage only.")
    parser.add_argument("--fetch-only", action="store_true", help="Populate cache and print coverage without replaying decisions.")
    parser.add_argument("--max-symbols", type=int, default=0)
    parser.add_argument("--symbols", default="", help="Comma-separated symbol allowlist.")
    parser.add_argument("--sample-limit", type=int, default=20)
    parser.add_argument("--json", action="store_true")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    report = asyncio.run(run(args))
    print(json.dumps(report, indent=2 if args.json else None, sort_keys=args.json))


def _load_universe(path: Path, symbols: str) -> list[dict[str, Any]]:
    allow = {item.strip().upper() for item in symbols.split(",") if item.strip()}
    rows: list[dict[str, Any]] = []
    with path.open() as handle:
        for row in csv.DictReader(handle):
            symbol = str(row.get("symbol") or "").strip().upper()
            exchange = str(row.get("exchange") or "").strip().upper()
            key = str(row.get("upstox_instrument_key") or "").strip()
            if not symbol or exchange != "NSE" or not key:
                continue
            if allow and symbol not in allow:
                continue
            rows.append({**row, "symbol": symbol, "exchange": "NSE", "market_region": "IN"})
    return rows


async def _fetch_all(
    rows: list[dict[str, Any]],
    base_url: str,
    interval: str,
    start: date,
    end: date,
    daily_lookback_days: int,
    concurrency: int,
    cache_dir: Path | None,
    *,
    cache_only: bool = False,
    request_spacing_seconds: float = 0.0,
) -> dict[str, dict[str, Any]]:
    if cache_dir:
        cache_dir.mkdir(parents=True, exist_ok=True)
    cached: dict[str, dict[str, Any]] = {}
    missing_rows: list[dict[str, Any]] = []
    for row in rows:
        symbol = str(row["symbol"]).upper()
        payload = _read_cache(cache_dir, symbol) if cache_dir else None
        if payload is not None:
            cached[symbol] = payload
        elif not cache_only:
            missing_rows.append(row)
    if cache_dir:
        print(
            f"fetch_cache cached={len(cached)} missing={len(missing_rows)} dir={cache_dir}",
            file=sys.stderr,
            flush=True,
        )
    results: dict[str, dict[str, Any]] = dict(cached)
    if not missing_rows:
        return results

    semaphore = asyncio.Semaphore(max(1, concurrency))
    rate_limiter = _AsyncRateLimiter(request_spacing_seconds)
    headers = {"Accept": "application/json", "User-Agent": "curl/8.5.0"}
    timeout = httpx.Timeout(20.0, connect=8.0)
    async with httpx.AsyncClient(headers=headers, timeout=timeout, follow_redirects=True) as client:
        tasks = [
            asyncio.create_task(
                _fetch_symbol(client, semaphore, rate_limiter, row, base_url, interval, start, end, daily_lookback_days)
            )
            for row in missing_rows
        ]
        completed = 0
        for task in asyncio.as_completed(tasks):
            symbol, payload = await task
            completed += 1
            results[symbol] = payload
            if cache_dir and (payload.get("daily") or payload.get("intraday")):
                _write_cache(cache_dir, symbol, payload)
            if completed % 100 == 0 or completed == len(tasks):
                print(
                    f"fetch_progress completed={completed}/{len(tasks)} total_cached={len(results)}",
                    file=sys.stderr,
                    flush=True,
                )
    return results


async def _fetch_symbol(
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    rate_limiter: "_AsyncRateLimiter",
    row: dict[str, Any],
    base_url: str,
    interval: str,
    start: date,
    end: date,
    daily_lookback_days: int,
) -> tuple[str, dict[str, Any]]:
    symbol = str(row["symbol"]).upper()
    key = url_quote(str(row["upstox_instrument_key"]), safe="")
    to_date = end - timedelta(days=1)
    daily_from = start - timedelta(days=max(60, daily_lookback_days))
    async with semaphore:
        daily_raw, daily_error = await _fetch_raw(
            client,
            f"{base_url}/historical-candle/{key}/day/{to_date.isoformat()}/{daily_from.isoformat()}",
            rate_limiter,
        )
        intraday_raw, intraday_error = await _fetch_raw(
            client,
            f"{base_url}/historical-candle/{key}/{interval}/{to_date.isoformat()}/{start.isoformat()}",
            rate_limiter,
        )
    daily = _parse_candles(symbol, daily_raw, "upstox-history:day")
    intraday = [
        candle
        for candle in _parse_candles(symbol, intraday_raw, f"upstox-history:{interval}")
        if start <= _market_day(_parse_ts(candle.ts)) < end
    ]
    return symbol, {
        "daily": sorted(daily, key=lambda item: _parse_ts(item.ts)),
        "intraday": sorted(intraday, key=lambda item: _parse_ts(item.ts)),
        "daily_error": daily_error,
        "intraday_error": intraday_error,
    }


async def _fetch_raw(
    client: httpx.AsyncClient,
    url: str,
    rate_limiter: "_AsyncRateLimiter",
) -> tuple[list[Any], str | None]:
    last_error = ""
    for attempt in range(1, 6):
        await rate_limiter.wait()
        try:
            response = await client.get(url)
            response.raise_for_status()
            candles = response.json().get("data", {}).get("candles", [])
            return (candles if isinstance(candles, list) else []), None
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            last_error = f"{type(exc).__name__}:{status}:{str(exc)[:140]}"
            if status in {429, 500, 502, 503, 504} and attempt < 5:
                await asyncio.sleep(min(18.0, 1.2 * (2 ** (attempt - 1))) + random.uniform(0.0, 0.75))
                continue
            return [], last_error
        except Exception as exc:
            last_error = f"{type(exc).__name__}:{str(exc)[:160]}"
            if attempt < 5:
                await asyncio.sleep(min(10.0, 0.8 * (2 ** (attempt - 1))) + random.uniform(0.0, 0.5))
                continue
            return [], last_error
    return [], last_error or "unknown_error"


class _AsyncRateLimiter:
    def __init__(self, spacing_seconds: float) -> None:
        self.spacing_seconds = max(0.0, float(spacing_seconds or 0.0))
        self._lock = asyncio.Lock()
        self._last_request = 0.0

    async def wait(self) -> None:
        if self.spacing_seconds <= 0:
            return
        async with self._lock:
            now = time.monotonic()
            delay = self.spacing_seconds - (now - self._last_request)
            if delay > 0:
                await asyncio.sleep(delay)
            self._last_request = time.monotonic()


def _read_cache(cache_dir: Path | None, symbol: str) -> dict[str, Any] | None:
    if not cache_dir:
        return None
    path = cache_dir / f"{symbol}.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        return {
            "daily": [Candle(**item) for item in data.get("daily", [])],
            "intraday": [Candle(**item) for item in data.get("intraday", [])],
            "daily_error": data.get("daily_error"),
            "intraday_error": data.get("intraday_error"),
        }
    except Exception:
        return None


def _write_cache(cache_dir: Path, symbol: str, payload: dict[str, Any]) -> None:
    path = cache_dir / f"{symbol}.json"
    tmp = cache_dir / f"{symbol}.json.tmp"
    data = {
        "daily": [item.to_dict() for item in payload.get("daily", [])],
        "intraday": [item.to_dict() for item in payload.get("intraday", [])],
        "daily_error": payload.get("daily_error"),
        "intraday_error": payload.get("intraday_error"),
    }
    tmp.write_text(json.dumps(data, separators=(",", ":")))
    tmp.replace(path)


def _parse_candles(symbol: str, raw: list[Any], source: str) -> list[Candle]:
    candles: list[Candle] = []
    for item in raw:
        if not isinstance(item, list) or len(item) < 6:
            continue
        try:
            candles.append(
                Candle(
                    symbol=symbol,
                    ts=str(item[0]),
                    open=float(item[1]),
                    high=float(item[2]),
                    low=float(item[3]),
                    close=float(item[4]),
                    volume=float(item[5] or 0.0),
                    source=source,
                )
            )
        except (TypeError, ValueError):
            continue
    return candles


def _historical_quote_states(
    fetched: dict[str, dict[str, Any]],
    start: date,
    end: date,
) -> dict[datetime, dict[str, dict[str, Any]]]:
    states_by_ts: dict[datetime, dict[str, dict[str, Any]]] = defaultdict(dict)
    for symbol, payload in fetched.items():
        by_day: dict[date, list[Candle]] = defaultdict(list)
        for bar in payload["intraday"]:
            day = _market_day(_parse_ts(bar.ts))
            if start <= day < end:
                by_day[day].append(bar)
        for _, bars in by_day.items():
            bars.sort(key=lambda item: _parse_ts(item.ts))
            day_open = bars[0].open
            day_high = day_open
            day_low = day_open
            cumulative_volume = 0.0
            for bar in bars:
                ts = _parse_ts(bar.ts)
                day_high = max(day_high, bar.high)
                day_low = min(day_low, bar.low)
                cumulative_volume += max(float(bar.volume or 0.0), 0.0)
                states_by_ts[ts][symbol] = {
                    "bar": bar,
                    "quote": Quote(
                        symbol=symbol,
                        price=bar.close,
                        source=bar.source,
                        asof=bar.ts,
                        open=day_open,
                        high=day_high,
                        low=day_low,
                        close=bar.close,
                        volume=cumulative_volume,
                    ),
                }
    return states_by_ts


def _load_historical_sentiment(
    database: Path,
    start: date,
    end: date,
    max_age_days: int,
) -> dict[str, list[dict[str, Any]]]:
    if not database.exists():
        return {}
    start_dt = datetime.combine(start - timedelta(days=max(max_age_days, 1)), datetime.min.time(), tzinfo=timezone.utc)
    end_dt = datetime.combine(end, datetime.min.time(), tzinfo=timezone.utc)
    conn = sqlite3.connect(f"file:{database.resolve()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            select ts, symbol, score, headline_count, headlines_json, confidence, events_json
            from sentiment_events
            where ts >= ? and ts < ?
            order by symbol, ts, id
            """,
            (start_dt.isoformat(), end_dt.isoformat()),
        ).fetchall()
    finally:
        conn.close()
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        item = dict(row)
        ts = _parse_ts(str(item.get("ts") or ""))
        symbol = str(item.get("symbol") or "").upper()
        if not symbol:
            continue
        headlines = _json_load(item.get("headlines_json"))
        events = _json_load(item.get("events_json"))
        grouped[symbol].append(
            {
                "asof": ts.isoformat(),
                "score": float(item.get("score") or 0.0),
                "confidence": float(item.get("confidence") or 0.0),
                "headline_count": int(item.get("headline_count") or 0),
                "headlines": headlines if isinstance(headlines, list) else [],
                "events": events if isinstance(events, list) else [],
                "status": "TIMESTAMP_SAFE_HISTORICAL_NEWS",
            }
        )
    return dict(grouped)


def _sentiment_at(
    history: list[dict[str, Any]],
    decision_ts: datetime,
    *,
    max_age_days: int,
) -> dict[str, Any] | None:
    selected: dict[str, Any] | None = None
    selected_ts: datetime | None = None
    max_age = timedelta(days=max(int(max_age_days or 1), 1))
    for item in history:
        item_ts = _parse_ts(str(item.get("asof") or ""))
        if item_ts > decision_ts:
            break
        if decision_ts - item_ts <= max_age:
            selected = item
            selected_ts = item_ts
    if not selected or not selected_ts:
        return None
    return {**selected, "age_seconds": round((decision_ts - selected_ts).total_seconds(), 2)}


def _daily_before(candles: list[Candle], day: date) -> list[Candle]:
    return [candle for candle in candles if _market_day(_parse_ts(candle.ts)) < day]


def _technical_for(daily: list[Candle], quote: Quote):
    closes = [float(candle.close) for candle in daily if candle.close > 0]
    highs = [float(candle.high) for candle in daily if candle.high > 0]
    lows = [float(candle.low) for candle in daily if candle.low > 0]
    volumes = [float(candle.volume or 0.0) for candle in daily]
    closes.append(float(quote.price))
    highs.append(float(quote.high or quote.price))
    lows.append(float(quote.low or quote.price))
    volumes.append(float(quote.volume or 0.0))
    return technical_snapshot(closes, highs, lows, volumes)


def _advance_decline_ratio(
    rows: list[dict[str, Any]],
    quotes: dict[str, Quote],
    candle_sets: dict[str, dict[str, list[Candle]]],
) -> float:
    advancers = 0
    decliners = 0
    for row in rows:
        symbol = str(row.get("symbol") or "").upper()
        quote = quotes.get(symbol)
        daily = candle_sets.get(symbol, {}).get("daily") or []
        if not quote or not daily:
            continue
        prev_close = float(daily[-1].close or 0.0)
        if prev_close <= 0:
            continue
        if float(quote.price) > prev_close:
            advancers += 1
        elif float(quote.price) < prev_close:
            decliners += 1
    return round(advancers / max(decliners, 1), 4)


def _simulate_trade(
    symbol: str,
    quote: Quote,
    model: dict[str, Any],
    intraday: list[Candle],
    settings: Settings,
) -> dict[str, Any] | None:
    entry = float(quote.price or 0.0)
    if entry <= 0:
        return None
    entry_ts = _parse_ts(quote.asof)
    plan = model.get("trade_plan") if isinstance(model.get("trade_plan"), dict) else {}
    stop = _num(plan.get("stop_loss")) if isinstance(plan, dict) else None
    targets = plan.get("targets") if isinstance(plan, dict) else []
    target = _num((targets[0] or {}).get("price")) if isinstance(targets, list) and targets else None
    stop = stop if stop and stop < entry else entry * 0.965
    target = target if target and target > entry else entry * 1.063
    setup_family = str(model.get("setup_family") or "").strip().lower()
    holding_period = str(plan.get("holding_period") or "").strip().lower() if isinstance(plan, dict) else ""
    btst_mode = setup_family == "delivery_btst" or holding_period.startswith("btst")
    entry_day = _market_day(entry_ts)
    next_market_day: date | None = None
    exit_price = entry
    exit_reason = "no_future_candles"
    exit_ts = ""
    for bar in intraday:
        bar_ts = _parse_ts(bar.ts)
        if bar_ts <= entry_ts:
            continue
        bar_day = _market_day(bar_ts)
        if btst_mode:
            if bar_day != entry_day and next_market_day is None:
                next_market_day = bar_day
            if next_market_day is not None and bar_day > next_market_day:
                break
        if bar_ts > entry_ts + timedelta(days=7):
            break
        exit_price = float(bar.close)
        exit_reason = "open_or_period_mark"
        exit_ts = bar.ts
        if float(bar.low) <= stop:
            exit_price = float(stop)
            exit_reason = "stop"
            break
        if float(bar.high) >= target:
            exit_price = float(target)
            exit_reason = "target"
            break
    min_notional = minimum_auto_follow_notional(settings, "IN")
    qty = max(1, int(math.ceil(min_notional / entry))) if min_notional > 0 else 1
    economics = exit_economics(entry, exit_price, qty, "IN", settings)
    entry_notional = float(economics.get("entry_notional") or entry * qty)
    net_pnl = float(economics.get("estimated_net_pnl") or 0.0)
    return {
        "symbol": symbol,
        "market": "IN",
        "entry_day": _market_day(entry_ts).isoformat(),
        "entry_ts": quote.asof,
        "entry": round(entry, 4),
        "qty": qty,
        "exit": round(exit_price, 4),
        "exit_ts": exit_ts,
        "exit_reason": exit_reason,
        "gross_pct": round(((exit_price - entry) / entry) * 100.0, 4),
        "net_pct": round((net_pnl / entry_notional) * 100.0, 4) if entry_notional else 0.0,
        "net_pnl": round(net_pnl, 2),
        "cost": float(economics.get("estimated_round_trip_cost") or 0.0),
        "score": round(float(model.get("raw_score") or 0.0), 4),
        "base_score": round(float(model.get("base_score") or 0.0), 4),
        "setup_score": round(float(model.get("setup_score") or 0.0), 4),
        "setup_family": model.get("setup_family"),
        "setup": model.get("setup"),
        "components": model.get("components") if isinstance(model.get("components"), dict) else {},
        "setup_evidence": model.get("setup_evidence") if isinstance(model.get("setup_evidence"), dict) else {},
    }


def _summary(trades: list[dict[str, Any]]) -> dict[str, Any]:
    priced = [trade for trade in trades if trade["exit_reason"] != "no_future_candles"]
    gross = [float(trade["gross_pct"]) for trade in priced]
    net = [float(trade["net_pct"]) for trade in priced]
    return {
        "n": len(trades),
        "priced": len(priced),
        "targets": sum(1 for trade in priced if trade["exit_reason"] == "target"),
        "stops": sum(1 for trade in priced if trade["exit_reason"] == "stop"),
        "marks": sum(1 for trade in priced if trade["exit_reason"] == "open_or_period_mark"),
        "no_future": len(trades) - len(priced),
        "gross_win_rate": round(sum(1 for value in gross if value > 0) / len(gross), 4) if gross else 0.0,
        "net_win_rate": round(sum(1 for value in net if value > 0) / len(net), 4) if net else 0.0,
        "avg_gross_pct": round(statistics.mean(gross), 4) if gross else 0.0,
        "avg_net_pct": round(statistics.mean(net), 4) if net else 0.0,
        "median_net_pct": round(statistics.median(net), 4) if net else 0.0,
        "sum_net_pnl": round(sum(float(trade["net_pnl"]) for trade in priced), 2),
        "sum_cost": round(sum(float(trade["cost"]) for trade in priced), 2),
    }


def _group_summary(trades: list[dict[str, Any]], key: str) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for trade in trades:
        grouped[str(trade.get(key) or "unknown")].append(trade)
    return {group: _summary(items) for group, items in sorted(grouped.items())}


def _sample(trades: list[dict[str, Any]], *, reverse: bool, limit: int) -> list[dict[str, Any]]:
    priced = [trade for trade in trades if trade["exit_reason"] != "no_future_candles"]
    return sorted(priced, key=lambda item: float(item["net_pct"]), reverse=reverse)[: max(0, limit)]


def _fetch_diagnostics(fetched: dict[str, dict[str, Any]]) -> dict[str, Any]:
    daily_errors: dict[str, int] = defaultdict(int)
    intraday_errors: dict[str, int] = defaultdict(int)
    for payload in fetched.values():
        if payload.get("daily_error"):
            daily_errors[str(payload["daily_error"])] += 1
        if payload.get("intraday_error"):
            intraday_errors[str(payload["intraday_error"])] += 1
    return {
        "daily_errors": dict(sorted(daily_errors.items(), key=lambda item: item[1], reverse=True)[:8]),
        "intraday_errors": dict(sorted(intraday_errors.items(), key=lambda item: item[1], reverse=True)[:8]),
    }


def _json_load(value: Any) -> Any:
    try:
        return json.loads(value or "[]")
    except Exception:
        return []


def _parse_ts(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=IST)


def _market_day(ts: datetime) -> date:
    return ts.astimezone(IST).date()


def _num(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


if __name__ == "__main__":
    main()
