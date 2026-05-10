from __future__ import annotations

import asyncio
import json
import re
from dataclasses import replace
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from .account import AccountService
from .agent import TradingAgentService
from .auth import (
    auth_status,
    current_user,
    hash_password,
    login_user,
    logout_user,
    normalize_role,
    require_admin,
    require_user,
    validate_password,
    validate_username,
)
from .config import CONFIG_KEYS, CONFIG_SCHEMA, SECRET_FIELDS, Settings, public_settings, settings_from_overrides
from .db import Database
from .delivery_data import DeliveryDataService
from .institutional_feeds import FreeInstitutionalFeedsService
from .llm_brain import LLMBrain
from .macro import GlobalIntelligenceService
from .macro_calendar import MacroCalendarService
from .market_breadth import MarketBreadthService
from .market_data import MarketDataError, build_market_data_provider
from .models import utc_now
from .order_router import build_order_router
from .options_intelligence import OptionsIntelligenceService
from .paper_broker import PaperBroker
from .request_context import current_user_id
from .sector_rotation import SectorRotationService
from .sentiment import SentimentService
from .strategy import StrategyEngine
from .universe import UniverseService


base_settings = Settings()
db = Database(base_settings.database_path)
db.init()
settings = settings_from_overrides(base_settings, db.runtime_settings())
if settings.admin_password:
    db.ensure_default_admin_user(settings.admin_username, hash_password(settings.admin_password))
db.seed_universe(settings.universe_csv, disable_missing=settings.universe_source == "csv")


class WebSocketHub:
    def __init__(self) -> None:
        self.connections: set[WebSocket] = set()

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self.connections.add(websocket)

    def disconnect(self, websocket: WebSocket) -> None:
        self.connections.discard(websocket)

    async def broadcast(self, payload: dict[str, Any]) -> None:
        message = json.dumps(payload)
        dead: list[WebSocket] = []
        for websocket in self.connections:
            try:
                await websocket.send_text(message)
            except Exception:
                dead.append(websocket)
        for websocket in dead:
            self.disconnect(websocket)


hub = WebSocketHub()


class UserSignalSessionManager:
    def __init__(self) -> None:
        self._tasks: dict[int, asyncio.Task] = {}
        self._status: dict[int, dict[str, Any]] = {}
        self._cursors: dict[int, int] = {}

    def is_running(self, user_id: int) -> bool:
        task = self._tasks.get(user_id)
        return bool(task and not task.done())

    def status(self, user_id: int) -> dict[str, Any]:
        status = dict(self._status.get(user_id) or {})
        status.setdefault("running", self.is_running(user_id))
        status.setdefault("phase", "idle")
        status.setdefault("started_at", None)
        status.setdefault("last_cycle_at", None)
        status.setdefault("last_error", None)
        status.setdefault("last_credit_charge", 0.0)
        status.setdefault("last_llm_calls", 0)
        status.setdefault("last_decision_count", 0)
        status.setdefault("symbols_per_cycle", self._symbol_limit({}, db.average_signal_credit_charge()))
        status["running"] = self.is_running(user_id)
        return status

    def admin_summary(self) -> dict[str, Any]:
        active = [user_id for user_id in self._tasks if self.is_running(user_id)]
        return {
            "running_users": len(active),
            "active_user_ids": active,
            "sessions": {str(user_id): self.status(user_id) for user_id in active},
        }

    async def start(self, user: dict[str, Any]) -> dict[str, Any]:
        user_id = int(user["id"])
        if self.is_running(user_id):
            return _status_payload(user)
        estimated_charge = db.average_signal_credit_charge()
        can_spend, credit_summary = db.user_has_credit_for(user_id, estimated_charge)
        if not can_spend:
            raise HTTPException(
                status_code=402,
                detail=f"Insufficient credits or daily budget to start signals. Estimated need: {estimated_charge:.4f} credits.",
            )
        _market_data_provider_for_user(user)
        self._status[user_id] = {
            "running": True,
            "phase": "starting",
            "started_at": utc_now(),
            "last_cycle_at": None,
            "last_error": None,
            "last_credit_charge": 0.0,
            "last_llm_calls": 0,
            "last_decision_count": 0,
            "symbols_per_cycle": self._symbol_limit(credit_summary, estimated_charge),
        }
        self._tasks[user_id] = asyncio.create_task(self._loop(user_id))
        db.insert_agent_log(
            "INFO",
            "user_session",
            "user_signal_start",
            f"User signal session started for {user.get('username')}",
            {"user_id": user_id, "username": user.get("username"), "estimated_credit": estimated_charge},
        )
        return _status_payload(user)

    async def stop(self, user: dict[str, Any]) -> dict[str, Any]:
        user_id = int(user["id"])
        task = self._tasks.get(user_id)
        if task and not task.done():
            task.cancel()
        self._tasks.pop(user_id, None)
        status = self.status(user_id)
        status.update({"running": False, "phase": "idle", "stopped_at": utc_now()})
        self._status[user_id] = status
        db.insert_agent_log(
            "INFO",
            "user_session",
            "user_signal_stop",
            f"User signal session stopped for {user.get('username')}",
            {"user_id": user_id, "username": user.get("username")},
        )
        return _status_payload(user)

    async def stop_all(self) -> None:
        tasks = list(self._tasks.items())
        for user_id, task in tasks:
            if task and not task.done():
                task.cancel()
        for user_id, task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
            self._status[user_id] = {**self.status(user_id), "running": False, "phase": "idle", "stopped_at": utc_now()}
        self._tasks.clear()

    def select_universe(self, user_id: int, full_universe: list[dict[str, Any]], credit_summary: dict[str, Any], estimated_charge: float) -> list[dict[str, Any]]:
        if not full_universe:
            return []
        limit = self._symbol_limit(credit_summary, estimated_charge)
        if limit >= len(full_universe):
            return full_universe
        start = self._cursors.get(user_id, 0) % len(full_universe)
        selected = [full_universe[(start + index) % len(full_universe)] for index in range(limit)]
        self._cursors[user_id] = (start + limit) % len(full_universe)
        return selected

    def _symbol_limit(self, credit_summary: dict[str, Any], estimated_charge: float) -> int:
        base = int(settings.universe_symbols_per_cycle or 30)
        base = max(5, min(base, 50))
        remaining = float(credit_summary.get("daily_credits_remaining") or credit_summary.get("credit_balance") or 0.0)
        estimated = max(float(estimated_charge or 0.01), 0.01)
        if remaining and remaining <= estimated * 3:
            return min(base, 5)
        if remaining and remaining <= estimated * 8:
            return min(base, 10)
        if remaining and remaining <= estimated * 15:
            return min(base, 20)
        return base

    async def _loop(self, user_id: int) -> None:
        try:
            while True:
                status = self.status(user_id)
                status.update({"running": True, "phase": "cycle"})
                self._status[user_id] = status
                try:
                    result = await _run_user_signal_cycle(user_id)
                    self._status[user_id] = {**self.status(user_id), **result, "running": True, "phase": "sleep", "last_error": None}
                    await hub.broadcast(_status_payload())
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    message = f"{exc.__class__.__name__}: {exc}"
                    self._status[user_id] = {**self.status(user_id), "running": False, "phase": "idle", "last_error": message}
                    self._tasks.pop(user_id, None)
                    db.insert_agent_log(
                        "ERROR",
                        "user_session",
                        "user_signal_error",
                        f"User signal session stopped: {message}",
                        {"user_id": user_id, "error_type": exc.__class__.__name__},
                    )
                    await hub.broadcast(_status_payload())
                    return
                await asyncio.sleep(max(30, int(settings.agent_interval_seconds or 180)))
        finally:
            if self._tasks.get(user_id) and self._tasks[user_id].done():
                self._tasks.pop(user_id, None)


def build_agent_stack(new_settings: Settings) -> dict[str, Any]:
    new_market_data = build_market_data_provider(new_settings)
    new_order_router = build_order_router(new_settings, db)
    new_broker = PaperBroker(new_settings, db, new_order_router)
    new_account = AccountService(new_settings, db)
    new_sentiment = SentimentService(new_settings, db)
    new_macro = GlobalIntelligenceService(new_settings)
    new_institutional_feeds = FreeInstitutionalFeedsService(new_settings)
    new_delivery_service = DeliveryDataService(new_settings, db)
    new_market_breadth = MarketBreadthService(new_settings, db)
    new_sector_rotation = SectorRotationService(new_settings, db)
    new_macro_calendar = MacroCalendarService(new_settings, db)
    new_universe_service = UniverseService(new_settings, db)
    new_options_intelligence = OptionsIntelligenceService(new_settings, db)
    new_llm = LLMBrain(new_settings, db)
    new_strategy = StrategyEngine(new_settings, new_sentiment, new_llm)
    new_agent = TradingAgentService(
        db=db,
        market_data=new_market_data,
        broker=new_broker,
        strategy=new_strategy,
        macro=new_macro,
        institutional_feeds=new_institutional_feeds,
        delivery_service=new_delivery_service,
        market_breadth=new_market_breadth,
        sector_rotation=new_sector_rotation,
        macro_calendar=new_macro_calendar,
        options_intelligence=new_options_intelligence,
        interval_seconds=new_settings.agent_interval_seconds,
        cycle_timeout_seconds=new_settings.cycle_timeout_seconds,
        universe_symbols_per_cycle=new_settings.universe_symbols_per_cycle,
        on_update=hub.broadcast,
    )
    return {
        "market_data": new_market_data,
        "order_router": new_order_router,
        "broker": new_broker,
        "account": new_account,
        "sentiment": new_sentiment,
        "macro": new_macro,
        "institutional_feeds": new_institutional_feeds,
        "delivery_service": new_delivery_service,
        "market_breadth": new_market_breadth,
        "sector_rotation": new_sector_rotation,
        "macro_calendar": new_macro_calendar,
        "universe_service": new_universe_service,
        "options_intelligence": new_options_intelligence,
        "llm": new_llm,
        "strategy": new_strategy,
        "agent": new_agent,
    }


stack = build_agent_stack(settings)
market_data = stack["market_data"]
order_router = stack["order_router"]
broker = stack["broker"]
account = stack["account"]
sentiment = stack["sentiment"]
macro = stack["macro"]
institutional_feeds = stack["institutional_feeds"]
delivery_service = stack["delivery_service"]
market_breadth = stack["market_breadth"]
sector_rotation = stack["sector_rotation"]
macro_calendar = stack["macro_calendar"]
universe_service = stack["universe_service"]
options_intelligence = stack["options_intelligence"]
llm = stack["llm"]
strategy = stack["strategy"]
agent = stack["agent"]
user_signal_sessions = UserSignalSessionManager()

app = FastAPI(title="OpenTrade")
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")


@app.on_event("startup")
async def startup() -> None:
    await universe_service.refresh_if_enabled()
    delivery_service.start_background_task()
    if settings.auto_start_agent:
        agent.start()


@app.on_event("shutdown")
async def shutdown() -> None:
    await agent.stop()
    await user_signal_sessions.stop_all()
    await delivery_service.stop_background_task()


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "provider": market_data.source_name,
            "llm_enabled": llm.enabled,
            "llm_model": llm.model if llm.enabled else "offline",
            "execution_mode": settings.execution_mode,
        },
    )


@app.get("/api/status")
async def status(request: Request) -> dict[str, Any]:
    user = require_user(request, settings, db)
    return _status_payload(user)


def _status_payload(user: dict[str, Any] | None = None) -> dict[str, Any]:
    snapshot = agent.snapshot()
    snapshot["runtime"] = {
        "market_data_provider": settings.market_data_provider,
        "execution_mode": settings.execution_mode,
        "llm_provider": settings.llm_provider,
        "llm_decision_mode": settings.llm_decision_mode,
        "llm_model": settings.deepseek_model if settings.llm_provider == "deepseek" else "offline",
        "llm_thinking_enabled": settings.llm_thinking_enabled,
        "llm_reasoning_effort": settings.llm_reasoning_effort,
        "llm_rolling_context_enabled": settings.llm_rolling_context_enabled,
    }
    if user and user.get("role") != "admin":
        snapshot["user_signal_session"] = user_signal_sessions.status(int(user["id"]))
    else:
        snapshot["user_signal_sessions"] = user_signal_sessions.admin_summary()
    return snapshot


@app.get("/api/decisions/{decision_id}")
async def decision_detail(decision_id: int, request: Request) -> dict[str, Any]:
    require_user(request, settings, db)
    row = db.decision_by_id(decision_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Decision not found")
    return row


@app.get("/api/orders/{order_id}")
async def order_detail(order_id: int, request: Request) -> dict[str, Any]:
    require_user(request, settings, db)
    row = db.order_by_id(order_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Order not found")
    return row


@app.post("/api/analyze-symbol")
async def analyze_symbol(payload: dict[str, Any], request: Request) -> dict[str, Any]:
    user = _require_signal_user(request)
    user_id = int(user["id"])
    estimated_charge = db.average_signal_credit_charge()
    can_spend, credit_before = db.user_has_credit_for(user_id, estimated_charge)
    if not can_spend:
        raise HTTPException(
            status_code=402,
            detail=(
                "Insufficient credits or daily credit budget for this analysis. "
                f"Estimated need: {estimated_charge:.4f} credits."
            ),
        )
    symbol = _normalize_symbol(str(payload.get("symbol", "")))
    if not symbol:
        raise HTTPException(status_code=400, detail="Enter a valid NSE symbol, for example SUZLON or INFY.")

    row = db.universe_row(symbol) or _manual_universe_row(symbol)
    user_market_data = _market_data_provider_for_user(user)
    user_strategy, budget_policy = _strategy_for_user_budget(user, credit_before, estimated_charge)
    provider_error: str | None = None
    usage_after_id = db.latest_llm_usage_id()
    context_token = current_user_id.set(user_id)
    try:
        quotes = await user_market_data.get_quotes([row])
        candles = await user_market_data.get_candles([row])
    except Exception as exc:
        provider_error = f"{exc.__class__.__name__}: {exc}"
        quotes = {}
        candles = {}
    finally:
        current_user_id.reset(context_token)

    quote = quotes.get(symbol)
    if quote is None:
        detail = f"No Upstox market quote found for {symbol}. Check the symbol spelling and Upstox instrument key."
        if provider_error:
            detail = f"{detail} Provider error: {provider_error}"
        raise HTTPException(status_code=404, detail=detail)

    context_token = current_user_id.set(user_id)
    try:
        news = await sentiment.analyze_symbol_news(row)
    finally:
        current_user_id.reset(context_token)
    db.upsert_quotes(quotes)
    db.upsert_candles(candles)
    candle_sets = db.recent_candle_sets_by_symbol([symbol])
    analysis_candles = {
        item_symbol: sets.get("analysis") or sets.get("daily") or sets.get("intraday") or []
        for item_symbol, sets in candle_sets.items()
    }
    macro_context = db.get_state("macro_context", {})
    institutional_context = db.get_state("institutional_context", {})
    options_context = db.get_state("options_intelligence_context", {})
    context_token = current_user_id.set(user_id)
    try:
        decisions = await user_strategy.evaluate(
            [row],
            quotes,
            broker.positions_by_symbol(),
            analysis_candles,
            macro_context,
            institutional_context,
            options_context,
            delivery_service,
            db.get_state("market_breadth_context", {}),
            db.get_state("sector_rotation_context", {}),
            macro_calendar,
            candle_sets,
            (db.latest_portfolio() or {}).get("equity"),
        )
    finally:
        current_user_id.reset(context_token)
    if not decisions:
        raise HTTPException(status_code=500, detail=f"Analysis produced no decision for {symbol}.")

    usage = db.llm_usage_cost_since(user_id, usage_after_id)
    try:
        credit_after = db.charge_user_credits(
            user_id,
            usage["cost_usd"],
            f"Symbol analysis {symbol}",
            {
                "symbol": symbol,
                "llm_usage": usage,
                "provider": quote.source,
                "estimated_credit_before": estimated_charge,
                "budget_policy": budget_policy,
            },
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=402,
            detail=(
                "The analysis completed, but the final LLM charge exceeded the user's available credits. "
                "Add credits or raise the daily budget before running another signal."
            ),
        ) from exc
    decision = _attach_user_to_decision(decisions[0], user, credit_after, budget_policy)
    decision_payload = decision.to_dict()
    decision_payload["details"] = _json_object(decision.details_json)
    public_credit_before = _public_credit_summary(credit_before)
    public_credit_after = _public_credit_summary(credit_after)
    db.insert_agent_log(
        "INFO",
        "manual_analysis",
        "symbol_analyzed",
        f"Manual analysis completed for {symbol}",
        {
            "symbol": symbol,
            "action": decision.action,
            "confidence": decision.confidence,
            "provider_error": provider_error,
            "credit_charge": round(max(float(credit_before.get("credit_balance", 0.0)) - float(credit_after.get("credit_balance", 0.0)), 0.0), 6),
            "budget_policy": budget_policy,
        },
    )
    return {
        "ok": True,
        "manual_only": True,
        "message": "Analysis completed. This does not place an order; autonomous cycles still handle trading.",
        "symbol": symbol,
        "quote": quote.to_dict(),
        "candle_count": len(analysis_candles.get(symbol, [])),
        "timeframe_candle_counts": {
            key: len(value)
            for key, value in (candle_sets.get(symbol) or {}).items()
        },
        "news": news,
        "provider": quote.source,
        "provider_error": provider_error,
        "credit_usage": {
            "before": public_credit_before,
            "after": public_credit_after,
            "llm_usage": _public_llm_usage(usage),
            "budget_policy": budget_policy,
        },
        "decision": decision_payload,
    }


@app.get("/api/me/credits")
async def my_credit_summary(request: Request) -> dict[str, Any]:
    user = require_user(request, settings, db)
    summary = db.user_credit_summary(int(user["id"]))
    return {
        "ok": True,
        "credits": _public_credit_summary(summary),
        "usage_policy": {
            "daily_limit": summary.get("daily_credit_limit", 0.0),
            "daily_remaining": summary.get("daily_credits_remaining", 0.0),
            "estimated_signal_credit": db.average_signal_credit_charge(),
            "low_budget_mode": "OpenTrade automatically uses a leaner analysis path when today's remaining credits are tight.",
        },
    }


@app.post("/api/me/credits/daily-limit")
async def set_my_daily_credit_limit(payload: dict[str, Any], request: Request) -> dict[str, Any]:
    user = _require_signal_user(request)
    daily_limit = _positive_float(payload.get("daily_credit_limit"), field="daily_credit_limit")
    summary = db.update_user_daily_credit_limit(int(user["id"]), daily_limit)
    db.insert_agent_log(
        "INFO",
        "credits",
        "user_daily_limit_updated",
        f"{user['username']} updated daily credit budget",
        {"user_id": user["id"], "daily_credit_limit": daily_limit},
    )
    return {"ok": True, "credits": _public_credit_summary(summary)}


@app.post("/api/me/upstox/auth-url")
async def my_upstox_auth_url(payload: dict[str, Any], request: Request) -> dict[str, Any]:
    user = _require_signal_user(request)
    stored = db.user_by_id(int(user["id"])) or {}
    api_key = str(payload.get("api_key") or stored.get("upstox_api_key") or "").strip()
    redirect_uri = str(payload.get("redirect_uri") or stored.get("upstox_redirect_uri") or f"{str(request.base_url).rstrip('/')}/upstox/callback").strip()
    base_url = str(payload.get("base_url") or stored.get("upstox_api_base_url") or settings.upstox_api_base_url).rstrip("/")
    if not api_key:
        raise HTTPException(status_code=400, detail="Enter your Upstox API key first.")
    if not redirect_uri:
        raise HTTPException(status_code=400, detail="Enter the exact Upstox redirect URI configured in your Upstox app.")
    auth_url = f"{base_url}/login/authorization/dialog?{urlencode({'response_type': 'code', 'client_id': api_key, 'redirect_uri': redirect_uri})}"
    return {
        "ok": True,
        "auth_url": auth_url,
        "message": "Open this URL, login to Upstox, then paste the returned code or full redirect URL into OpenTrade.",
    }


@app.post("/api/me/upstox/connect")
async def my_upstox_connect(payload: dict[str, Any], request: Request) -> dict[str, Any]:
    user = _require_signal_user(request)
    stored = db.user_by_id(int(user["id"])) or {}
    api_key = str(payload.get("api_key") or stored.get("upstox_api_key") or "").strip()
    api_secret = str(payload.get("api_secret") or stored.get("upstox_api_secret") or "").strip()
    redirect_uri = str(payload.get("redirect_uri") or stored.get("upstox_redirect_uri") or "").strip()
    base_url = str(payload.get("base_url") or stored.get("upstox_api_base_url") or settings.upstox_api_base_url).rstrip("/")
    code = _extract_oauth_code(str(payload.get("code") or ""))
    if not all([api_key, api_secret, redirect_uri, code]):
        raise HTTPException(status_code=400, detail="Upstox connect needs API key, API secret, redirect URI, and authorization code.")

    token_data = await _exchange_upstox_code(api_key, api_secret, redirect_uri, base_url, code)
    access_token = str(token_data.get("access_token") or "").strip()
    if not access_token:
        raise HTTPException(status_code=502, detail=f"Upstox token exchange did not return access_token. Response keys: {list(token_data)[:10]}")
    updated_user = db.update_user_broker(
        int(user["id"]),
        {
            "upstox_api_key": api_key,
            "upstox_api_secret": api_secret,
            "upstox_redirect_uri": redirect_uri,
            "upstox_access_token": access_token,
            "upstox_api_base_url": base_url,
        },
    )
    db.insert_agent_log(
        "INFO",
        "upstox",
        "user_upstox_connected",
        f"Upstox connected for user {user['username']}",
        {"user_id": user["id"], "username": user["username"], "base_url": base_url, "token_type": token_data.get("token_type")},
    )
    return {
        "ok": True,
        "message": "Upstox connected for this user. Symbol analysis will use this user's market feed.",
        "user": updated_user,
        "token_type": token_data.get("token_type"),
        "upstox_user_id": token_data.get("user_id"),
    }


@app.post("/api/me/kite/connect")
async def my_kite_connect(payload: dict[str, Any], request: Request) -> dict[str, Any]:
    user = _require_signal_user(request)
    api_key = str(payload.get("api_key") or "").strip()
    access_token = str(payload.get("access_token") or "").strip()
    if not all([api_key, access_token]):
        raise HTTPException(status_code=400, detail="Kite connect needs API key and access token.")
    updated_user = db.update_user_broker(
        int(user["id"]),
        {
            "kite_api_key": api_key,
            "kite_access_token": access_token,
        },
    )
    db.insert_agent_log(
        "INFO",
        "kite",
        "user_kite_saved",
        f"Kite credentials saved for user {user['username']}",
        {"user_id": user["id"], "username": user["username"]},
    )
    return {
        "ok": True,
        "message": "Kite credentials saved. Upstox is still required for full candle analytics in this build.",
        "user": updated_user,
    }


@app.get("/api/account")
async def account_details(request: Request) -> dict[str, Any]:
    require_user(request, settings, db)
    return await account.snapshot()


@app.get("/api/performance")
async def performance_summary(request: Request) -> dict[str, Any]:
    require_user(request, settings, db)
    return db.performance_summary()


@app.get("/api/config")
async def get_config(request: Request) -> dict[str, Any]:
    require_user(request, settings, db)
    return _config_payload()


def _config_payload() -> dict[str, Any]:
    return {"schema": CONFIG_SCHEMA, "settings": public_settings(settings)}


@app.get("/api/logs")
async def agent_logs(request: Request, limit: int = 300) -> dict[str, Any]:
    require_admin(request, settings, db)
    safe_limit = max(1, min(int(limit), 1000))
    return {"logs": db.latest_agent_logs(safe_limit)}


@app.get("/api/market-breadth")
async def market_breadth_snapshot(request: Request) -> dict[str, Any]:
    require_user(request, settings, db)
    return db.get_state("market_breadth_context", {})


@app.get("/api/sector-rotation")
async def sector_rotation_snapshot(request: Request) -> dict[str, Any]:
    require_user(request, settings, db)
    return db.get_state("sector_rotation_context", {})


@app.get("/api/options-intelligence")
async def options_intelligence_snapshot(request: Request) -> dict[str, Any]:
    require_user(request, settings, db)
    return db.get_state("options_intelligence_context", {})


@app.get("/api/macro-calendar")
async def macro_calendar_snapshot(request: Request) -> dict[str, Any]:
    require_user(request, settings, db)
    context = db.get_state("macro_calendar_context", {})
    if not context:
        return {"enabled": settings.enable_macro_calendar, "events": macro_calendar.upcoming_events(30)}
    return context


@app.get("/api/universe")
async def universe_snapshot(request: Request) -> dict[str, Any]:
    require_user(request, settings, db)
    return {
        "settings": {
            "source": settings.universe_source,
            "symbols_per_cycle": settings.universe_symbols_per_cycle,
            "nse_refresh_on_start": settings.nse_universe_refresh_on_start,
            "nse_series": settings.nse_universe_series,
        },
        "summary": db.universe_summary(),
        "refresh_status": universe_service.status(),
    }


@app.post("/api/universe/refresh")
async def refresh_universe(request: Request) -> dict[str, Any]:
    require_admin(request, settings, db)
    status = await universe_service.refresh_nse_equity()
    return {"status": status, "summary": db.universe_summary()}


@app.post("/api/llm/test")
async def test_llm(request: Request) -> dict[str, Any]:
    require_admin(request, settings, db)
    return await llm.test_connection()


@app.get("/api/llm/usage")
async def llm_usage(request: Request) -> dict[str, Any]:
    require_user(request, settings, db)
    return db.llm_usage_summary(100)


@app.post("/api/upstox/auth-url")
async def upstox_auth_url(payload: dict[str, Any], request: Request) -> dict[str, Any]:
    require_admin(request, settings, db)
    api_key = str(payload.get("api_key") or settings.upstox_api_key).strip()
    redirect_uri = str(payload.get("redirect_uri") or settings.upstox_redirect_uri).strip()
    base_url = str(payload.get("base_url") or settings.upstox_api_base_url).rstrip("/")
    if not api_key:
        raise HTTPException(status_code=400, detail="Enter and save your Upstox API key first.")
    if not redirect_uri:
        raise HTTPException(status_code=400, detail="Enter the exact Upstox redirect URI configured in your Upstox app.")
    auth_url = f"{base_url}/login/authorization/dialog?{urlencode({'response_type': 'code', 'client_id': api_key, 'redirect_uri': redirect_uri})}"
    return {
        "ok": True,
        "auth_url": auth_url,
        "message": "Open this URL, login to Upstox, then paste the returned code or full redirect URL into OpenTrade.",
    }


@app.post("/api/upstox/connect")
async def upstox_connect(payload: dict[str, Any], request: Request) -> dict[str, Any]:
    require_admin(request, settings, db)
    api_key = str(payload.get("api_key") or settings.upstox_api_key).strip()
    api_secret = str(payload.get("api_secret") or settings.upstox_api_secret).strip()
    redirect_uri = str(payload.get("redirect_uri") or settings.upstox_redirect_uri).strip()
    base_url = str(payload.get("base_url") or settings.upstox_api_base_url).rstrip("/")
    code = _extract_oauth_code(str(payload.get("code") or ""))
    if not all([api_key, api_secret, redirect_uri, code]):
        raise HTTPException(status_code=400, detail="Upstox connect needs API key, API secret, redirect URI, and authorization code.")

    token_data = await _exchange_upstox_code(api_key, api_secret, redirect_uri, base_url, code)
    access_token = str(token_data.get("access_token") or "").strip()
    if not access_token:
        raise HTTPException(status_code=502, detail=f"Upstox token exchange did not return access_token. Response keys: {list(token_data)[:10]}")

    current_overrides = db.runtime_settings()
    candidate_overrides = dict(current_overrides)
    candidate_overrides.update(
        {
            "market_data_provider": "upstox",
            "upstox_api_key": api_key,
            "upstox_api_secret": api_secret,
            "upstox_redirect_uri": redirect_uri,
            "upstox_access_token": access_token,
            "upstox_api_base_url": base_url,
        }
    )
    candidate_settings = settings_from_overrides(Settings(), candidate_overrides)
    try:
        candidate_stack = build_agent_stack(candidate_settings)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Upstox access token saved but provider failed to initialize: {exc}") from exc
    result = await _apply_runtime_stack(
        candidate_overrides=candidate_overrides,
        candidate_settings=candidate_settings,
        candidate_stack=candidate_stack,
        changed_keys=[
            "market_data_provider",
            "upstox_api_key",
            "upstox_api_secret",
            "upstox_redirect_uri",
            "upstox_access_token",
            "upstox_api_base_url",
        ],
        component="upstox",
        event="connected",
        message="Upstox access token connected and saved",
    )
    db.insert_agent_log(
        "INFO",
        "upstox",
        "access_token_saved",
        "Upstox access token saved for market data",
        {
            "base_url": base_url,
            "redirect_uri": redirect_uri,
            "token_type": token_data.get("token_type"),
            "user_id": token_data.get("user_id"),
        },
    )
    return {
        "ok": True,
        "message": "Upstox connected. Access token saved and market data provider rebuilt.",
        "provider": result["status"].get("provider"),
        "token_type": token_data.get("token_type"),
        "user_id": token_data.get("user_id"),
        "status": result["status"],
        "config": result["config"],
    }


@app.get("/upstox/callback", response_class=HTMLResponse)
async def upstox_callback(request: Request) -> HTMLResponse:
    code = _extract_oauth_code(str(request.url))
    error = request.query_params.get("error") or request.query_params.get("error_description")
    body = (
        f"<h1>Upstox authorization code received</h1><p>Paste this code into OpenTrade Upstox Connect.</p>"
        f"<textarea style='width:100%;height:120px'>{code}</textarea>"
        if code
        else f"<h1>Upstox authorization did not return a code</h1><p>{error or 'No code found in callback URL.'}</p>"
    )
    return HTMLResponse(f"<!doctype html><html><body>{body}</body></html>")


@app.post("/api/config")
async def update_config(payload: dict[str, Any], request: Request) -> dict[str, Any]:
    require_admin(request, settings, db)

    incoming = payload.get("settings", payload)
    if not isinstance(incoming, dict):
        raise HTTPException(status_code=400, detail="settings payload must be an object")

    current_overrides = db.runtime_settings()
    candidate_overrides = dict(current_overrides)
    for key, value in incoming.items():
        if key not in CONFIG_KEYS:
            continue
        if key in SECRET_FIELDS and value == "":
            continue
        candidate_overrides[key] = value

    candidate_settings = settings_from_overrides(Settings(), candidate_overrides)
    try:
        candidate_stack = build_agent_stack(candidate_settings)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid configuration: {exc}") from exc

    return await _apply_runtime_stack(
        candidate_overrides=candidate_overrides,
        candidate_settings=candidate_settings,
        candidate_stack=candidate_stack,
        changed_keys=sorted(key for key in incoming.keys() if key in CONFIG_KEYS),
        component="admin",
        event="config_saved",
        message="Runtime configuration saved",
    )


async def _apply_runtime_stack(
    candidate_overrides: dict[str, Any],
    candidate_settings: Settings,
    candidate_stack: dict[str, Any],
    changed_keys: list[str],
    component: str,
    event: str,
    message: str,
) -> dict[str, Any]:
    global settings, market_data, order_router, broker, account, sentiment, macro, institutional_feeds, delivery_service, market_breadth, sector_rotation, macro_calendar, universe_service, options_intelligence, llm, strategy, agent
    was_running = agent.running
    await agent.stop()
    await delivery_service.stop_background_task()
    db.update_runtime_settings(candidate_overrides)
    db.insert_agent_log(
        "INFO",
        component,
        event,
        message,
        {
            "changed_keys": changed_keys,
            "was_running": was_running,
        },
    )

    settings = candidate_settings
    market_data = candidate_stack["market_data"]
    order_router = candidate_stack["order_router"]
    broker = candidate_stack["broker"]
    account = candidate_stack["account"]
    sentiment = candidate_stack["sentiment"]
    macro = candidate_stack["macro"]
    institutional_feeds = candidate_stack["institutional_feeds"]
    delivery_service = candidate_stack["delivery_service"]
    market_breadth = candidate_stack["market_breadth"]
    sector_rotation = candidate_stack["sector_rotation"]
    macro_calendar = candidate_stack["macro_calendar"]
    universe_service = candidate_stack["universe_service"]
    options_intelligence = candidate_stack["options_intelligence"]
    llm = candidate_stack["llm"]
    strategy = candidate_stack["strategy"]
    agent = candidate_stack["agent"]
    await universe_service.refresh_if_enabled()
    delivery_service.start_background_task()
    if was_running:
        agent.start()

    snapshot = _status_payload()
    await hub.broadcast(snapshot)
    return {"config": _config_payload(), "status": snapshot}


@app.get("/api/auth/me")
async def auth_me(request: Request) -> dict[str, Any]:
    return auth_status(request, settings, db)


@app.post("/api/auth/login")
async def auth_login(payload: dict[str, Any], response: Response) -> dict[str, Any]:
    return login_user(str(payload.get("username", "")), str(payload.get("password", "")), response, settings, db)


@app.post("/api/auth/logout")
async def auth_logout(response: Response) -> dict[str, bool]:
    return logout_user(response)


@app.get("/api/users")
async def list_users(request: Request) -> dict[str, Any]:
    require_admin(request, settings, db)
    return {"users": db.list_users()}


@app.post("/api/users")
async def create_user(payload: dict[str, Any], request: Request) -> dict[str, Any]:
    admin = require_admin(request, settings, db)
    username = validate_username(str(payload.get("username", "")))
    password = validate_password(str(payload.get("password", "")))
    role = normalize_role(str(payload.get("role", "user")))
    active = bool(payload.get("active", True))
    starting_credits = _positive_float(payload.get("starting_credits", payload.get("credit_balance", 0)), field="starting_credits")
    daily_credit_limit = _positive_float(payload.get("daily_credit_limit", 0), field="daily_credit_limit")
    if db.user_by_username(username):
        raise HTTPException(status_code=409, detail="Username already exists")
    user = db.create_user(username, hash_password(password), role=role, active=active)
    if daily_credit_limit:
        db.update_user_daily_credit_limit(int(user["id"]), daily_credit_limit)
    if starting_credits:
        db.adjust_user_credits(
            int(user["id"]),
            starting_credits,
            f"Initial credit allocation for {username}",
            {"created_by": admin.get("username"), "source": "create_user"},
        )
    users = db.list_users()
    public_user = next((item for item in users if int(item["id"]) == int(user["id"])), user)
    db.insert_agent_log(
        "INFO",
        "admin",
        "user_created",
        f"Admin created user {username}",
        {
            "created_by": admin.get("username"),
            "username": username,
            "role": role,
            "active": active,
            "starting_credits": starting_credits,
            "daily_credit_limit": daily_credit_limit,
        },
    )
    return {"ok": True, "user": public_user, "users": users}


@app.patch("/api/users/{user_id}")
async def update_user(user_id: int, payload: dict[str, Any], request: Request) -> dict[str, Any]:
    admin = require_admin(request, settings, db)
    existing = db.user_by_id(user_id)
    if not existing:
        raise HTTPException(status_code=404, detail="User not found")
    role = normalize_role(str(payload["role"])) if "role" in payload else None
    active = bool(payload["active"]) if "active" in payload else None
    password_hash = hash_password(validate_password(str(payload["password"]))) if payload.get("password") else None
    daily_credit_limit = _positive_float(payload["daily_credit_limit"], field="daily_credit_limit") if "daily_credit_limit" in payload else None
    if existing.get("role") == "admin" and db.active_admin_count() <= 1:
        would_remove_admin = (role is not None and role != "admin") or active is False
        if would_remove_admin:
            raise HTTPException(status_code=400, detail="At least one active admin user is required.")
    user = db.update_user(user_id, role=role, active=active, password_hash=password_hash)
    if daily_credit_limit is not None:
        db.update_user_daily_credit_limit(user_id, daily_credit_limit)
        user = next((item for item in db.list_users() if int(item["id"]) == user_id), user)
    db.insert_agent_log(
        "INFO",
        "admin",
        "user_updated",
        f"Admin updated user {existing.get('username')}",
        {
            "updated_by": admin.get("username"),
            "user_id": user_id,
            "role_changed": role is not None,
            "active_changed": active is not None,
            "password_changed": password_hash is not None,
            "daily_credit_limit_changed": daily_credit_limit is not None,
        },
    )
    return {"ok": True, "user": user, "users": db.list_users()}


@app.get("/api/admin/credits")
async def admin_credit_summary(request: Request) -> dict[str, Any]:
    require_admin(request, settings, db)
    return db.admin_credit_usage_summary()


@app.post("/api/users/{user_id}/credits")
async def adjust_user_credit_balance(user_id: int, payload: dict[str, Any], request: Request) -> dict[str, Any]:
    admin = require_admin(request, settings, db)
    existing = db.user_by_id(user_id)
    if not existing:
        raise HTTPException(status_code=404, detail="User not found")
    amount = _float_value(payload.get("amount"), field="amount")
    if amount == 0:
        raise HTTPException(status_code=400, detail="Credit amount cannot be zero.")
    description = str(payload.get("description") or "Admin credit adjustment").strip()
    summary = db.adjust_user_credits(
        user_id,
        amount,
        description,
        {"updated_by": admin.get("username"), "username": existing.get("username")},
        entry_type="allocation" if amount > 0 else "adjustment",
    )
    db.insert_agent_log(
        "INFO",
        "credits",
        "admin_credit_adjustment",
        f"Admin adjusted credits for {existing.get('username')}",
        {"updated_by": admin.get("username"), "user_id": user_id, "amount": amount},
    )
    return {"ok": True, "credits": summary, "admin": db.admin_credit_usage_summary(), "users": db.list_users()}


@app.post("/api/users/{user_id}/assign-runtime-upstox")
async def assign_runtime_upstox(user_id: int, request: Request) -> dict[str, Any]:
    admin = require_admin(request, settings, db)
    existing = db.user_by_id(user_id)
    if not existing:
        raise HTTPException(status_code=404, detail="User not found")
    runtime_settings = db.runtime_settings()
    runtime_upstox = {
        "upstox_api_key": (runtime_settings.get("upstox_api_key") or settings.upstox_api_key),
        "upstox_api_secret": (runtime_settings.get("upstox_api_secret") or settings.upstox_api_secret),
        "upstox_redirect_uri": (runtime_settings.get("upstox_redirect_uri") or settings.upstox_redirect_uri),
        "upstox_access_token": (runtime_settings.get("upstox_access_token") or settings.upstox_access_token),
        "upstox_api_base_url": (runtime_settings.get("upstox_api_base_url") or settings.upstox_api_base_url),
    }
    if not runtime_upstox["upstox_access_token"]:
        raise HTTPException(status_code=400, detail="No runtime Upstox access token is available to assign.")
    updated_user = db.assign_runtime_upstox_to_user(user_id, runtime_upstox)
    db.insert_agent_log(
        "INFO",
        "upstox",
        "runtime_upstox_assigned",
        f"Admin assigned runtime Upstox credentials to {existing.get('username')}",
        {"updated_by": admin.get("username"), "user_id": user_id, "username": existing.get("username")},
    )
    return {"ok": True, "user": updated_user, "users": db.list_users()}


@app.post("/api/control/start")
async def start_agent(request: Request) -> dict[str, Any]:
    user = require_user(request, settings, db)
    if user.get("role") != "admin":
        return await user_signal_sessions.start(user)
    db.insert_agent_log("INFO", "admin", "control_start", "Admin requested agent start")
    agent.start()
    snapshot = agent.snapshot()
    await hub.broadcast(snapshot)
    return _status_payload(user)


@app.post("/api/control/stop")
async def stop_agent(request: Request) -> dict[str, Any]:
    user = require_user(request, settings, db)
    if user.get("role") != "admin":
        return await user_signal_sessions.stop(user)
    db.insert_agent_log("INFO", "admin", "control_stop", "Admin requested agent stop")
    await agent.stop()
    snapshot = agent.snapshot()
    await hub.broadcast(snapshot)
    return _status_payload(user)


@app.post("/api/control/run-once")
async def run_once(request: Request) -> dict[str, Any]:
    require_admin(request, settings, db)
    db.insert_agent_log("INFO", "admin", "control_run_once", "Admin requested one manual cycle")
    return await agent.run_once()


@app.post("/api/control/reset-demo")
async def reset_demo(request: Request) -> dict[str, Any]:
    require_admin(request, settings, db)
    was_running = agent.running
    await agent.stop()
    db.reset_trading_ledger(settings.initial_cash_inr)
    db.insert_agent_log(
        "WARN",
        "admin",
        "demo_reset",
        "Demo trading ledger reset",
        {"initial_cash_inr": settings.initial_cash_inr, "was_running": was_running},
    )
    if was_running:
        agent.start()
    snapshot = _status_payload()
    await hub.broadcast(snapshot)
    return snapshot


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    user = current_user(websocket, settings, db)
    if not user:
        await websocket.close(code=1008)
        return
    await hub.connect(websocket)
    try:
        await websocket.send_text(json.dumps(_status_payload(user)))
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        hub.disconnect(websocket)


def _normalize_symbol(value: str) -> str:
    symbol = value.strip().upper()
    symbol = symbol.removeprefix("NSE:")
    symbol = symbol.removesuffix(".NS")
    if not re.fullmatch(r"[A-Z0-9&-]{1,24}", symbol):
        return ""
    return symbol


def _require_signal_user(request: Request) -> dict[str, Any]:
    user = require_user(request, settings, db)
    if user.get("role") == "admin":
        raise HTTPException(
            status_code=403,
            detail="Admin manages users, credits, and broker connections only. Use a user account for signals and trading.",
        )
    return user


def _market_data_provider_for_user(user: dict[str, Any]):
    stored = db.user_by_id(int(user["id"])) or {}
    if stored.get("upstox_access_token"):
        user_settings = replace(
            settings,
            market_data_provider="upstox",
            upstox_api_key=str(stored.get("upstox_api_key") or ""),
            upstox_api_secret=str(stored.get("upstox_api_secret") or ""),
            upstox_redirect_uri=str(stored.get("upstox_redirect_uri") or settings.upstox_redirect_uri),
            upstox_access_token=str(stored.get("upstox_access_token") or ""),
            upstox_api_base_url=str(stored.get("upstox_api_base_url") or settings.upstox_api_base_url).rstrip("/"),
        )
        return build_market_data_provider(user_settings)
    if stored.get("kite_access_token"):
        raise HTTPException(
            status_code=400,
            detail="Kite is saved for this user, but full analytics currently require Upstox candles. Connect Upstox or ask admin to assign the runtime Upstox feed.",
        )
    raise HTTPException(
        status_code=400,
        detail="No Upstox account is connected for this user. Connect Upstox from Account or ask admin to assign the runtime Upstox feed.",
    )


def _strategy_for_user_budget(
    user: dict[str, Any],
    credit_summary: dict[str, Any],
    estimated_charge: float,
) -> tuple[StrategyEngine, dict[str, Any]]:
    daily_remaining = float(credit_summary.get("daily_credits_remaining") or 0.0)
    balance = float(credit_summary.get("credit_balance") or 0.0)
    available = min(daily_remaining, balance)
    threshold = max(float(estimated_charge or 0.01) * 3.0, 0.03)
    policy = {
        "mode": "full_context",
        "model": settings.deepseek_model,
        "daily_credits_remaining": round(daily_remaining, 6),
        "estimated_signal_credit": round(float(estimated_charge or 0.0), 6),
    }
    if settings.deepseek_model != "deepseek-v4-flash" and 0 < available <= threshold:
        budget_settings = replace(
            settings,
            deepseek_model="deepseek-v4-flash",
            llm_max_tokens=min(int(settings.llm_max_tokens or 4096), 2048),
            llm_rolling_context_max_chunks=1 if int(settings.llm_rolling_context_max_chunks or 0) == 0 else min(int(settings.llm_rolling_context_max_chunks), 1),
        )
        policy.update(
            {
                "mode": "low_credit_guard",
                "model": budget_settings.deepseek_model,
                "reason": "daily credit budget is close to the estimated signal cost",
            }
        )
        return StrategyEngine(budget_settings, sentiment, LLMBrain(budget_settings, db)), policy
    return strategy, policy


def _attach_user_to_decision(
    decision: Any,
    user: dict[str, Any],
    credit_summary: dict[str, Any],
    budget_policy: dict[str, Any],
) -> Any:
    details = _json_object(decision.details_json)
    details["signal_user"] = {
        "user_id": int(user["id"]),
        "username": user.get("username"),
        "credit_usage": _public_credit_summary(credit_summary),
        "budget_policy": budget_policy,
    }
    return replace(decision, details_json=json.dumps(details, default=str, separators=(",", ":")))


async def _run_user_signal_cycle(user_id: int) -> dict[str, Any]:
    user = db.user_by_id(user_id)
    if not user or not user.get("active") or user.get("role") == "admin":
        raise RuntimeError("user signal session requires an active non-admin user")

    estimated_charge = db.average_signal_credit_charge()
    can_spend, credit_before = db.user_has_credit_for(user_id, estimated_charge)
    if not can_spend:
        raise RuntimeError(f"insufficient credits or daily budget; estimated need {estimated_charge:.4f} credits")

    full_universe = db.get_universe(enabled_only=True)
    universe = user_signal_sessions.select_universe(user_id, full_universe, credit_before, estimated_charge)
    if not universe:
        raise RuntimeError("no enabled universe symbols available for user signal session")

    user_market_data = _market_data_provider_for_user(user)
    user_strategy, budget_policy = _strategy_for_user_budget(user, credit_before, estimated_charge)
    usage_after_id = db.latest_llm_usage_id()
    usage: dict[str, Any] = {"calls": 0, "cost_usd": 0.0, "total_tokens": 0, "input_chars": 0, "output_chars": 0}
    credit_after = credit_before
    decisions: list[Any] = []
    cycle_error: BaseException | None = None
    context_token = current_user_id.set(user_id)
    try:
        quotes = await user_market_data.get_quotes(universe)
        if not quotes:
            raise MarketDataError(f"{user_market_data.source_name} returned no quotes for user signal session")
        candles_fresh = await user_market_data.get_candles(universe)
        db.upsert_quotes(quotes)
        db.upsert_candles(candles_fresh)
        candle_sets = db.recent_candle_sets_by_symbol([row["symbol"] for row in universe])
        candles = {
            symbol: sets.get("analysis") or sets.get("daily") or sets.get("intraday") or []
            for symbol, sets in candle_sets.items()
        }
        decisions = await user_strategy.evaluate(
            universe,
            quotes,
            {},
            candles,
            db.get_state("macro_context", {}),
            db.get_state("institutional_context", {}),
            db.get_state("options_intelligence_context", {}),
            delivery_service,
            db.get_state("market_breadth_context", {}),
            db.get_state("sector_rotation_context", {}),
            macro_calendar,
            candle_sets,
            (db.latest_portfolio() or {}).get("equity"),
        )
    except BaseException as exc:
        cycle_error = exc
    finally:
        current_user_id.reset(context_token)

    usage = db.llm_usage_cost_since(user_id, usage_after_id)
    if usage.get("calls"):
        credit_after = db.charge_user_credits(
            user_id,
            usage["cost_usd"],
            "Autonomous signal cycle",
            {
                "symbols": [row["symbol"] for row in universe],
                "symbol_count": len(universe),
                "llm_usage": usage,
                "provider": user_market_data.source_name,
                "estimated_credit_before": estimated_charge,
                "budget_policy": budget_policy,
            },
        )
    if cycle_error is not None:
        raise cycle_error

    tagged_decisions = [
        _attach_user_to_decision(decision, user, credit_after, budget_policy)
        for decision in decisions
    ]
    if tagged_decisions:
        db.insert_decisions(tagged_decisions)

    action_counts: dict[str, int] = {}
    for decision in tagged_decisions:
        action_counts[decision.action] = action_counts.get(decision.action, 0) + 1
    credit_charge = round(
        max(float(credit_before.get("credit_balance", 0.0)) - float(credit_after.get("credit_balance", 0.0)), 0.0),
        6,
    )
    db.insert_agent_log(
        "INFO",
        "user_session",
        "user_signal_cycle",
        f"User signal cycle completed for {user.get('username')}",
        {
            "user_id": user_id,
            "username": user.get("username"),
            "symbols": len(universe),
            "decisions": len(tagged_decisions),
            "action_counts": action_counts,
            "llm_calls": usage.get("calls", 0),
            "llm_tokens": usage.get("total_tokens", 0),
            "credit_charge": credit_charge,
            "daily_credits_remaining": credit_after.get("daily_credits_remaining"),
            "budget_policy": budget_policy,
        },
    )
    return {
        "last_cycle_at": utc_now(),
        "last_error": None,
        "last_credit_charge": credit_charge,
        "last_llm_calls": usage.get("calls", 0),
        "last_llm_tokens": usage.get("total_tokens", 0),
        "last_decision_count": len(tagged_decisions),
        "last_action_counts": action_counts,
        "symbols_per_cycle": len(universe),
        "credit_balance": credit_after.get("credit_balance"),
        "daily_credits_remaining": credit_after.get("daily_credits_remaining"),
        "budget_policy": budget_policy,
    }


async def _exchange_upstox_code(
    api_key: str,
    api_secret: str,
    redirect_uri: str,
    base_url: str,
    code: str,
) -> dict[str, Any]:
    token_url = f"{base_url}/login/authorization/token"
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            response = await client.post(
                token_url,
                headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
                data={
                    "code": code,
                    "client_id": api_key,
                    "client_secret": api_secret,
                    "redirect_uri": redirect_uri,
                    "grant_type": "authorization_code",
                },
            )
            response.raise_for_status()
            return response.json()
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=exc.response.status_code, detail=f"Upstox token exchange failed: {exc.response.text[:300]}") from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Upstox token exchange failed: {exc.__class__.__name__}: {exc}") from exc


def _public_credit_summary(summary: dict[str, Any]) -> dict[str, Any]:
    output = {
        "user_id": summary.get("user_id"),
        "username": summary.get("username"),
        "credit_balance": summary.get("credit_balance", 0.0),
        "daily_credit_limit": summary.get("daily_credit_limit", 0.0),
        "credits_used_today": summary.get("credits_used_today", 0.0),
        "daily_credits_remaining": summary.get("daily_credits_remaining", 0.0),
        "today": {
            "credits_used": (summary.get("today") or {}).get("credits_used", 0.0),
            "entries": (summary.get("today") or {}).get("entries", 0),
        },
        "all_time": {
            "credits_used": (summary.get("all_time") or {}).get("credits_used", 0.0),
            "entries": (summary.get("all_time") or {}).get("entries", 0),
        },
    }
    if "ledger" in summary:
        output["ledger"] = [
            {
                "id": row.get("id"),
                "ts": row.get("ts"),
                "entry_type": row.get("entry_type"),
                "amount": row.get("amount"),
                "balance_after": row.get("balance_after"),
                "description": row.get("description"),
                "details": _public_credit_details(row.get("details") or {}),
            }
            for row in summary.get("ledger", [])
        ]
    return output


def _public_credit_details(details: dict[str, Any]) -> dict[str, Any]:
    output = dict(details)
    if isinstance(output.get("llm_usage"), dict):
        output["llm_usage"] = _public_llm_usage(output["llm_usage"])
    return output


def _public_llm_usage(usage: dict[str, Any]) -> dict[str, Any]:
    return {
        "calls": int(usage.get("calls") or 0),
        "total_tokens": int(usage.get("total_tokens") or 0),
        "input_chars": int(usage.get("input_chars") or 0),
        "output_chars": int(usage.get("output_chars") or 0),
    }


def _positive_float(value: Any, *, field: str) -> float:
    numeric = _float_value(value, field=field)
    if numeric < 0:
        raise HTTPException(status_code=400, detail=f"{field} cannot be negative.")
    return numeric


def _float_value(value: Any, *, field: str) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"{field} must be a number.") from exc


def _extract_oauth_code(value: str) -> str:
    raw = value.strip()
    if not raw:
        return ""
    if "code=" in raw:
        parsed = urlparse(raw)
        query = parse_qs(parsed.query)
        code = (query.get("code") or [""])[0]
        if code:
            return code.strip()
    return raw


def _manual_universe_row(symbol: str) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "name": symbol,
        "exchange": "NSE",
        "yahoo_symbol": f"{symbol}.NS",
        "kite_symbol": f"NSE:{symbol}",
        "upstox_instrument_key": "",
        "nubra_symbol": symbol,
        "nubra_ref_id": None,
        "sector": "manual",
        "base_price": 100,
        "enabled": 1,
    }


def _json_object(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}") if isinstance(value, str) else value
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        return {}
