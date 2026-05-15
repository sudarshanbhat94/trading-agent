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
