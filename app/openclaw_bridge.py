from __future__ import annotations

import asyncio
import hmac
import json
from typing import Any

import httpx
from fastapi import HTTPException
from starlette.requests import Request

from .config import Settings
from .db import Database
from .market_regions import normalize_market_region


def require_openclaw_bridge(request: Request, settings: Settings) -> None:
    if not settings.openclaw_bridge_enabled:
        raise HTTPException(status_code=403, detail="OpenClaw bridge is disabled.")
    if not settings.openclaw_bridge_token:
        raise HTTPException(status_code=503, detail="OPENCLAW_BRIDGE_TOKEN is not configured.")
    supplied = _bearer_token(request) or request.headers.get("x-openclaw-token", "")
    if not hmac.compare_digest(str(supplied), str(settings.openclaw_bridge_token)):
        raise HTTPException(status_code=401, detail="Invalid OpenClaw bridge token.")


def default_openclaw_user(settings: Settings, db: Database) -> dict[str, Any]:
    user = db.user_by_username(settings.openclaw_default_username)
    if not user:
        raise HTTPException(
            status_code=404,
            detail=f"OpenClaw default user '{settings.openclaw_default_username}' was not found.",
        )
    if not int(user.get("active") or 0):
        raise HTTPException(
            status_code=403,
            detail=f"OpenClaw default user '{settings.openclaw_default_username}' is inactive.",
        )
    return user


def bridge_context(db: Database, market: str = "BOTH", limit: int = 8) -> dict[str, Any]:
    market_region = normalize_market_region(market or "BOTH", default="BOTH")
    limit = max(1, min(int(limit or 8), 25))
    ideas = [_idea_summary(item) for item in db.latest_signal_ideas(limit, market_region=market_region)]
    decisions = [_decision_summary(item) for item in db.latest_decisions(limit, market_region=market_region)]
    orders = [_order_summary(item) for item in db.latest_orders(limit)]
    if market_region != "BOTH":
        orders = [item for item in orders if item.get("market_region") in {market_region, None}]
    return {
        "ok": True,
        "source": "openstocks",
        "market": market_region,
        "engine": {
            "market_session": db.get_state("market_session_context", {}),
            "market_breadth": db.get_state("market_breadth_context", {}),
            "self_audit": db.get_state("self_audit", {}),
            "llm_budget": db.get_state("llm_budget", {}),
        },
        "ideas": ideas,
        "decisions": decisions,
        "orders": orders,
        "positions": db.positions(),
        "strategy_plans": db.strategy_plans(),
        "instructions_for_openclaw": [
            "Use OpenStocks as the source of truth for market data, ideas, decisions, orders, and positions.",
            "Use /api/openclaw/analyze for a fresh symbol-level analysis before presenting a new trade idea.",
            "Do not infer live order placement from an idea; orders are confirmed only by the orders list.",
        ],
    }


def select_stock_candidates(db: Database, market: str = "BOTH", limit: int = 5) -> dict[str, Any]:
    market_region = normalize_market_region(market or "BOTH", default="BOTH")
    limit = max(1, min(int(limit or 5), 15))
    ideas = db.latest_signal_ideas(80, market_region=market_region)
    buy_ready = [
        item
        for item in ideas
        if str(item.get("signal_type") or item.get("suggestion") or "").upper() == "BUY"
        and str(item.get("lifecycle_status") or item.get("status") or "").upper()
        not in {"REJECTED", "EXPIRED", "STOPPED", "TARGET_3_HIT", "EXIT_SIGNAL"}
    ]
    if not buy_ready:
        buy_ready = [
            item
            for item in ideas
            if str(item.get("status") or "").upper() in {"ACTIVE", "WATCH", "MONITORING"}
        ]
    ranked = sorted(
        buy_ready,
        key=lambda item: (
            float(item.get("overall_score") or item.get("combined_score") or 0.0),
            float(item.get("confluence") or 0.0),
            float(item.get("current_return_pct") or 0.0),
        ),
        reverse=True,
    )[:limit]
    candidates = [_idea_summary(item) for item in ranked]
    return {
        "ok": True,
        "source": "openstocks",
        "market": market_region,
        "candidates": candidates,
        "next_step": "Call /api/openclaw/analyze for any candidate before sending a recommendation to WhatsApp.",
    }


def breakout_scan(db: Database, market: str = "BOTH", limit: int = 10) -> dict[str, Any]:
    market_region = normalize_market_region(market or "BOTH", default="BOTH")
    limit = max(1, min(int(limit or 10), 25))
    universe = db.get_universe(enabled_only=True, market_region=market_region)
    universe_by_symbol = {str(row.get("symbol") or "").upper(): row for row in universe}
    quote_rows = [
        row
        for row in db.latest_quotes()
        if str(row.get("symbol") or "").upper() in universe_by_symbol
        and (market_region == "BOTH" or str(row.get("market_region") or "").upper() == market_region)
    ]
    symbols = [str(row.get("symbol") or "").upper() for row in quote_rows]
    candle_sets = db.recent_candle_sets_by_symbol(symbols)
    candidates: list[dict[str, Any]] = []
    skipped = {"missing_candles": 0, "insufficient_history": 0, "missing_pivot": 0}
    for quote in quote_rows:
        symbol = str(quote.get("symbol") or "").upper()
        sets = candle_sets.get(symbol) or {}
        candles = sets.get("daily") or sets.get("analysis") or []
        if not candles:
            skipped["missing_candles"] += 1
            continue
        if len(candles) < 30:
            skipped["insufficient_history"] += 1
            continue
        row = universe_by_symbol.get(symbol, {})
        candidate = _breakout_candidate(row, quote, candles)
        if not candidate:
            skipped["missing_pivot"] += 1
            continue
        candidates.append(candidate)
    candidates.sort(
        key=lambda item: (
            float(item.get("scanner_score") or 0.0),
            float(item.get("volume_ratio_20d") or 0.0),
            -abs(float(item.get("distance_to_pivot_pct") or 99.0)),
        ),
        reverse=True,
    )
    for index, item in enumerate(candidates, start=1):
        item["rank"] = index
    shortlist = candidates[:limit]
    return {
        "ok": True,
        "source": "openstocks",
        "feed": "rule_based_volume_breakout_scan",
        "market": market_region,
        "scanned": len(quote_rows),
        "eligible_with_history": len(candidates),
        "skipped": skipped,
        "rules": [
            "Rank high relative volume first, but require usable price/candle history.",
            "Prefer price within 3% below to 5% above the prior 20/50-session pivot.",
            "Prefer Stage2-style trend: price above 20DMA/50DMA, 20DMA above 50DMA, 50DMA rising.",
            "Penalize extended entries, weak closes, failed two-day breakouts, and prices below 50DMA.",
        ],
        "candidates": shortlist,
        "best_candidate": shortlist[0] if shortlist else None,
        "next_step": "OpenClaw should deep-analyze only the top 1-3 candidates with /api/openclaw/analyze before suggesting a BUY.",
    }


def _breakout_candidate(row: dict[str, Any], quote: dict[str, Any], candles: list[Any]) -> dict[str, Any] | None:
    closes = [_num(_candle_value(candle, "close")) for candle in candles]
    highs = [_num(_candle_value(candle, "high")) for candle in candles]
    lows = [_num(_candle_value(candle, "low")) for candle in candles]
    volumes = [_num(_candle_value(candle, "volume")) for candle in candles]
    if len(closes) < 30 or len(highs) < 30 or not any(highs[:-1]):
        return None

    price = _num(quote.get("price")) or closes[-1]
    prior_20_highs = [value for value in highs[-21:-1] if value > 0]
    prior_50_highs = [value for value in highs[-51:-1] if value > 0]
    pivot_20 = max(prior_20_highs) if prior_20_highs else None
    pivot_50 = max(prior_50_highs) if prior_50_highs else pivot_20
    pivot = pivot_50 or pivot_20
    if not pivot:
        return None

    distance_to_pivot_pct = ((price - pivot) / pivot) * 100
    sma20 = _sma(closes, 20)
    sma50 = _sma(closes, 50) or _sma(closes, 30)
    previous_sma50 = _sma(closes[:-5], 50) or _sma(closes[:-5], 30)
    sma50_slope_pct = ((sma50 - previous_sma50) / previous_sma50) * 100 if sma50 and previous_sma50 else 0.0
    stage2_trend = bool(price > (sma20 or 0) and price > (sma50 or 0) and (sma20 or 0) > (sma50 or 0) and sma50_slope_pct > 0)

    latest_volume = _num(quote.get("volume")) or volumes[-1]
    avg_volume_20 = _avg([value for value in volumes[-21:-1] if value > 0])
    volume_ratio = latest_volume / avg_volume_20 if avg_volume_20 else 0.0
    turnover_value = price * latest_volume if price and latest_volume else 0.0

    last_high = highs[-1]
    last_low = lows[-1]
    close_position = (closes[-1] - last_low) / (last_high - last_low) if last_high > last_low else 0.5
    two_day_failed = _two_day_breakout_failed(closes, highs, pivot)
    close_above_pivot = price >= pivot
    near_pivot = -3.0 <= distance_to_pivot_pct <= 5.0
    clean_breakout = close_above_pivot and distance_to_pivot_pct <= 3.0 and volume_ratio >= 1.5 and close_position >= 0.65

    score = 0.0
    score += min(volume_ratio / 3.0, 1.0) * 25.0
    if 0 <= distance_to_pivot_pct <= 3:
        score += 30.0
    elif -3 <= distance_to_pivot_pct < 0:
        score += 8.0 + (1 - abs(distance_to_pivot_pct) / 3.0) * 18.0
    elif 3 < distance_to_pivot_pct <= 5:
        score += 12.0 + (1 - ((distance_to_pivot_pct - 3.0) / 2.0)) * 8.0
    elif -5 <= distance_to_pivot_pct < -3:
        score += 5.0
    if stage2_trend:
        score += 20.0
    else:
        if sma20 and price > sma20:
            score += 5.0
        if sma20 and sma50 and sma20 > sma50:
            score += 5.0
        if sma50_slope_pct > 0:
            score += 5.0
    if close_position >= 0.75:
        score += 10.0
    elif close_position >= 0.6:
        score += 5.0
    if turnover_value > 0:
        score += 5.0

    risk_flags: list[str] = []
    if two_day_failed:
        risk_flags.append("two_day_breakout_failed")
        score -= 20.0
    if distance_to_pivot_pct > 8:
        risk_flags.append("extended_more_than_8pct_from_pivot")
        score -= 30.0
    if volume_ratio and volume_ratio < 1:
        risk_flags.append("volume_below_20d_average")
        score -= 8.0
    if sma50 and price < sma50:
        risk_flags.append("below_50dma")
        score -= 15.0

    if clean_breakout:
        candidate_type = "breakout_now"
        readiness = "deep_analyze"
    elif near_pivot and distance_to_pivot_pct < 0:
        candidate_type = "near_pivot_watch"
        readiness = "deep_analyze" if score >= 45 else "watch"
    elif near_pivot:
        candidate_type = "post_breakout_watch"
        readiness = "deep_analyze" if score >= 50 else "watch"
    else:
        candidate_type = "volume_leader_not_near_breakout"
        readiness = "watch" if score >= 35 else "low_priority"

    return {
        "symbol": str(quote.get("symbol") or row.get("symbol") or "").upper(),
        "market_region": quote.get("market_region"),
        "company_name": quote.get("company_name") or row.get("name"),
        "exchange": quote.get("exchange") or row.get("exchange"),
        "sector": row.get("sector"),
        "industry": row.get("industry"),
        "price": round(price, 4),
        "source": quote.get("source"),
        "asof": quote.get("ts"),
        "scanner_score": round(max(0.0, min(score, 100.0)), 2),
        "candidate_type": candidate_type,
        "readiness": readiness,
        "pivot": round(pivot, 4),
        "prior_20d_pivot": round(pivot_20, 4) if pivot_20 else None,
        "prior_50d_pivot": round(pivot_50, 4) if pivot_50 else None,
        "distance_to_pivot_pct": round(distance_to_pivot_pct, 3),
        "volume": round(latest_volume, 2),
        "avg_volume_20d": round(avg_volume_20, 2) if avg_volume_20 else None,
        "volume_ratio_20d": round(volume_ratio, 3) if volume_ratio else 0.0,
        "turnover_value": round(turnover_value, 2),
        "sma20": round(sma20, 4) if sma20 else None,
        "sma50": round(sma50, 4) if sma50 else None,
        "sma50_slope_pct": round(sma50_slope_pct, 3),
        "stage2_trend": stage2_trend,
        "close_position_in_range": round(close_position, 3),
        "clean_breakout": clean_breakout,
        "near_pivot": near_pivot,
        "two_day_rule_failed": two_day_failed,
        "risk_flags": risk_flags,
        "plain_english": _breakout_plain_english(
            symbol=str(quote.get("symbol") or row.get("symbol") or "").upper(),
            candidate_type=candidate_type,
            distance_pct=distance_to_pivot_pct,
            volume_ratio=volume_ratio,
            stage2=stage2_trend,
            risk_flags=risk_flags,
        ),
    }


def _breakout_plain_english(
    *,
    symbol: str,
    candidate_type: str,
    distance_pct: float,
    volume_ratio: float,
    stage2: bool,
    risk_flags: list[str],
) -> str:
    if candidate_type == "breakout_now":
        setup = f"{symbol} is breaking above pivot with {volume_ratio:.1f}x volume"
    elif candidate_type == "near_pivot_watch":
        setup = f"{symbol} is {abs(distance_pct):.1f}% below pivot with {volume_ratio:.1f}x volume"
    elif candidate_type == "post_breakout_watch":
        setup = f"{symbol} is {distance_pct:.1f}% above pivot after a breakout attempt"
    else:
        setup = f"{symbol} has volume activity but is not close enough to a clean pivot"
    trend = "Stage2-style trend is present" if stage2 else "trend confirmation is incomplete"
    risk = f" Risks: {', '.join(risk_flags)}." if risk_flags else ""
    return f"{setup}. {trend}.{risk}"


def _two_day_breakout_failed(closes: list[float], highs: list[float], pivot: float) -> bool:
    if len(closes) < 4 or pivot <= 0:
        return False
    recent = list(zip(highs[-4:-1], closes[-4:-1]))
    for index, (high, close) in enumerate(recent):
        if high > pivot and close > pivot:
            later = closes[-3 + index :]
            return any(value < pivot for value in later)
    return False


def _candle_value(candle: Any, key: str) -> Any:
    if isinstance(candle, dict):
        return candle.get(key)
    return getattr(candle, key, None)


def _num(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _avg(values: list[float]) -> float:
    values = [value for value in values if value is not None]
    return sum(values) / len(values) if values else 0.0


def _sma(values: list[float], length: int) -> float | None:
    clean = [value for value in values if value > 0]
    if len(clean) < length:
        return None
    recent = clean[-length:]
    return sum(recent) / length


class OpenClawNotifier:
    def __init__(self, settings: Settings, db: Database) -> None:
        self.settings = settings
        self.db = db

    async def notify_cycle_events(self) -> dict[str, Any]:
        if not self._enabled:
            return {"enabled": False, "reason": "webhook_not_configured"}
        summary = {"enabled": True, "ideas_sent": 0, "orders_sent": 0, "errors": []}
        if self.settings.openclaw_notify_ideas:
            idea_result = await self._notify_new_ideas()
            summary["ideas_sent"] = idea_result.get("sent", 0)
            summary["errors"].extend(idea_result.get("errors", []))
        if self.settings.openclaw_notify_orders:
            order_result = await self._notify_new_orders()
            summary["orders_sent"] = order_result.get("sent", 0)
            summary["errors"].extend(order_result.get("errors", []))
        return summary

    async def send_test(self) -> dict[str, Any]:
        if not self._enabled:
            raise HTTPException(status_code=503, detail="Configure OPENCLAW_WEBHOOK_URL or OPENCLAW_NOTIFY_TARGET first.")
        return await self._deliver(
            "openstocks.test",
            "OpenStocks is connected to OpenClaw. WhatsApp notifications can now be routed from this bridge.",
            {"source": "openstocks", "kind": "test"},
        )

    @property
    def _enabled(self) -> bool:
        return bool(
            self.settings.openclaw_bridge_enabled
            and (self.settings.openclaw_webhook_url or self.settings.openclaw_notify_target)
        )

    async def _notify_new_ideas(self) -> dict[str, Any]:
        latest = self.db.latest_signal_ideas(80)
        max_id = max((int(item.get("id") or 0) for item in latest), default=0)
        cursor_key = "openclaw_last_notified_idea_id"
        last_id = int(self.db.get_state(cursor_key, 0) or 0)
        if last_id <= 0:
            self.db.set_state(cursor_key, max_id)
            return {"sent": 0, "errors": [], "initialized": True}
        new_items = sorted(
            [item for item in latest if int(item.get("id") or 0) > last_id],
            key=lambda item: int(item.get("id") or 0),
        )
        sent = 0
        errors: list[str] = []
        for item in new_items:
            summary = _idea_summary(item)
            try:
                await self._deliver("openstocks.idea", _idea_message(summary), summary)
                sent += 1
                last_id = max(last_id, int(item.get("id") or 0))
            except Exception as exc:
                errors.append(f"{item.get('symbol')}: {exc.__class__.__name__}: {str(exc)[:160]}")
        if sent:
            self.db.set_state(cursor_key, last_id)
        return {"sent": sent, "errors": errors}

    async def _notify_new_orders(self) -> dict[str, Any]:
        latest = self.db.latest_orders(80)
        max_id = max((int(item.get("id") or 0) for item in latest), default=0)
        cursor_key = "openclaw_last_notified_order_id"
        last_id = int(self.db.get_state(cursor_key, 0) or 0)
        if last_id <= 0:
            self.db.set_state(cursor_key, max_id)
            return {"sent": 0, "errors": [], "initialized": True}
        new_items = sorted(
            [item for item in latest if int(item.get("id") or 0) > last_id],
            key=lambda item: int(item.get("id") or 0),
        )
        sent = 0
        errors: list[str] = []
        for item in new_items:
            summary = _order_summary(item)
            try:
                await self._deliver("openstocks.order", _order_message(summary), summary)
                sent += 1
                last_id = max(last_id, int(item.get("id") or 0))
            except Exception as exc:
                errors.append(f"{item.get('symbol')}: {exc.__class__.__name__}: {str(exc)[:160]}")
        if sent:
            self.db.set_state(cursor_key, last_id)
        return {"sent": sent, "errors": errors}

    async def _deliver(self, event: str, message: str, data: dict[str, Any]) -> dict[str, Any]:
        if self.settings.openclaw_webhook_url:
            return await self._post(event, message, data)
        return await self._send_cli(message, data)

    async def _post(self, event: str, message: str, data: dict[str, Any]) -> dict[str, Any]:
        headers = {"Content-Type": "application/json", "X-OpenStocks-Event": event}
        if self.settings.openclaw_webhook_secret:
            headers["Authorization"] = f"Bearer {self.settings.openclaw_webhook_secret}"
        payload = {
            "event": event,
            "source": "openstocks",
            "message": message,
            "text": message,
            "data": data,
        }
        async with httpx.AsyncClient(timeout=12) as client:
            response = await client.post(self.settings.openclaw_webhook_url, json=payload, headers=headers)
            response.raise_for_status()
            try:
                body: Any = response.json()
            except ValueError:
                body = response.text[:300]
        return {"ok": True, "status_code": response.status_code, "response": body}

    async def _send_cli(self, message: str, data: dict[str, Any]) -> dict[str, Any]:
        target = str(self.settings.openclaw_notify_target or "").strip()
        if not target:
            raise RuntimeError("OPENCLAW_NOTIFY_TARGET is not configured")
        channel = str(self.settings.openclaw_notify_channel or "whatsapp").strip() or "whatsapp"
        command = [
            self.settings.openclaw_cli_path or "openclaw",
            "message",
            "send",
            "--channel",
            channel,
            "--target",
            target,
            "--message",
            message,
        ]
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=20)
        if process.returncode != 0:
            error = stderr.decode("utf-8", "replace").strip() or stdout.decode("utf-8", "replace").strip()
            raise RuntimeError(f"OpenClaw CLI send failed: {error[:300]}")
        return {
            "ok": True,
            "delivery": "openclaw_cli",
            "channel": channel,
            "target": target,
            "response": stdout.decode("utf-8", "replace").strip()[:500],
            "data_id": data.get("id"),
        }


def _bearer_token(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    prefix = "bearer "
    return auth[len(prefix) :].strip() if auth.lower().startswith(prefix) else ""


def _idea_summary(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": item.get("id"),
        "symbol": item.get("symbol"),
        "market_region": item.get("market_region"),
        "company_name": item.get("company_name"),
        "signal_type": item.get("signal_type") or item.get("suggestion"),
        "status": item.get("status"),
        "lifecycle_status": item.get("lifecycle_status"),
        "latest_price": item.get("latest_price") or item.get("price"),
        "current_return_pct": item.get("current_return_pct"),
        "combined_score": item.get("combined_score"),
        "overall_score": item.get("overall_score"),
        "confluence": item.get("confluence"),
        "entry_zone": item.get("entry_zone"),
        "targets": item.get("targets", []),
        "stop_loss": item.get("stop_loss"),
        "risk_flags": item.get("risk_flags", []),
        "decision_readiness": item.get("decision_readiness"),
        "reason": item.get("reason") or item.get("plain_english_reason"),
    }


def _decision_summary(item: dict[str, Any]) -> dict[str, Any]:
    details = _json_object(item.get("details_json"))
    return {
        "id": item.get("id"),
        "ts": item.get("ts"),
        "symbol": item.get("symbol"),
        "market_region": item.get("market_region"),
        "action": item.get("action"),
        "confidence": item.get("confidence"),
        "price": item.get("price"),
        "strategy": item.get("strategy"),
        "reason": item.get("reason"),
        "decision_path": details.get("decision_path"),
        "risk_flags": details.get("risk_flags", []),
        "gate_failures": details.get("gate_failures", []),
    }


def _order_summary(item: dict[str, Any]) -> dict[str, Any]:
    details = _json_object(item.get("details_json"))
    return {
        "id": item.get("id"),
        "ts": item.get("ts"),
        "symbol": item.get("symbol"),
        "market_region": item.get("market_region"),
        "side": item.get("side"),
        "qty": item.get("qty"),
        "price": item.get("price"),
        "notional": item.get("notional"),
        "status": item.get("status"),
        "reason": item.get("reason"),
        "execution": details.get("execution", details),
    }


def _idea_message(item: dict[str, Any]) -> str:
    symbol = item.get("symbol") or "UNKNOWN"
    action = item.get("signal_type") or "IDEA"
    score = _pct(item.get("overall_score") or item.get("combined_score"))
    price = item.get("latest_price")
    return (
        f"OpenStocks idea: {symbol} {action}. "
        f"Score {score}; price {_number(price)}; status {item.get('status') or 'unknown'}. "
        f"Reason: {item.get('reason') or item.get('decision_readiness') or 'Review in OpenStocks.'}"
    )


def _order_message(item: dict[str, Any]) -> str:
    symbol = item.get("symbol") or "UNKNOWN"
    return (
        f"OpenStocks order: {item.get('side')} {symbol} x{item.get('qty')} at {_number(item.get('price'))}. "
        f"Status {item.get('status')}; notional {_number(item.get('notional'))}. "
        f"Reason: {item.get('reason') or 'Order recorded.'}"
    )


def _json_object(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        decoded = json.loads(str(raw))
    except json.JSONDecodeError:
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _pct(value: Any) -> str:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return "n/a"
    if abs(numeric) <= 1:
        numeric *= 100
    return f"{numeric:.1f}%"


def _number(value: Any) -> str:
    try:
        return f"{float(value):,.2f}"
    except (TypeError, ValueError):
        return "n/a"
