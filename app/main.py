from __future__ import annotations

import json
import re
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
from .market_data import build_market_data_provider
from .order_router import build_order_router
from .options_intelligence import OptionsIntelligenceService
from .paper_broker import PaperBroker
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
    require_user(request, settings, db)
    return _status_payload()


def _status_payload() -> dict[str, Any]:
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
    require_user(request, settings, db)
    symbol = _normalize_symbol(str(payload.get("symbol", "")))
    if not symbol:
        raise HTTPException(status_code=400, detail="Enter a valid NSE symbol, for example SUZLON or INFY.")

    row = db.universe_row(symbol) or _manual_universe_row(symbol)
    provider_error: str | None = None
    try:
        quotes = await market_data.get_quotes([row])
        candles = await market_data.get_candles([row])
    except Exception as exc:
        provider_error = f"{exc.__class__.__name__}: {exc}"
        quotes = {}
        candles = {}

    quote = quotes.get(symbol)
    if quote is None:
        detail = f"No Upstox market quote found for {symbol}. Check the symbol spelling and Upstox instrument key."
        if provider_error:
            detail = f"{detail} Provider error: {provider_error}"
        raise HTTPException(status_code=404, detail=detail)

    news = await sentiment.analyze_symbol_news(row)
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
    decisions = await strategy.evaluate(
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
    if not decisions:
        raise HTTPException(status_code=500, detail=f"Analysis produced no decision for {symbol}.")

    decision = decisions[0]
    decision_payload = decision.to_dict()
    decision_payload["details"] = _json_object(decision.details_json)
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
        "decision": decision_payload,
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
            token_data = response.json()
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=exc.response.status_code, detail=f"Upstox token exchange failed: {exc.response.text[:300]}") from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Upstox token exchange failed: {exc.__class__.__name__}: {exc}") from exc

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
    if db.user_by_username(username):
        raise HTTPException(status_code=409, detail="Username already exists")
    user = db.create_user(username, hash_password(password), role=role, active=active)
    db.insert_agent_log(
        "INFO",
        "admin",
        "user_created",
        f"Admin created user {username}",
        {"created_by": admin.get("username"), "username": username, "role": role, "active": active},
    )
    return {"ok": True, "user": user, "users": db.list_users()}


@app.patch("/api/users/{user_id}")
async def update_user(user_id: int, payload: dict[str, Any], request: Request) -> dict[str, Any]:
    admin = require_admin(request, settings, db)
    existing = db.user_by_id(user_id)
    if not existing:
        raise HTTPException(status_code=404, detail="User not found")
    role = normalize_role(str(payload["role"])) if "role" in payload else None
    active = bool(payload["active"]) if "active" in payload else None
    password_hash = hash_password(validate_password(str(payload["password"]))) if payload.get("password") else None
    if existing.get("role") == "admin" and db.active_admin_count() <= 1:
        would_remove_admin = (role is not None and role != "admin") or active is False
        if would_remove_admin:
            raise HTTPException(status_code=400, detail="At least one active admin user is required.")
    user = db.update_user(user_id, role=role, active=active, password_hash=password_hash)
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
        },
    )
    return {"ok": True, "user": user, "users": db.list_users()}


@app.post("/api/control/start")
async def start_agent(request: Request) -> dict[str, Any]:
    require_admin(request, settings, db)
    db.insert_agent_log("INFO", "admin", "control_start", "Admin requested agent start")
    agent.start()
    snapshot = agent.snapshot()
    await hub.broadcast(snapshot)
    return snapshot


@app.post("/api/control/stop")
async def stop_agent(request: Request) -> dict[str, Any]:
    require_admin(request, settings, db)
    db.insert_agent_log("INFO", "admin", "control_stop", "Admin requested agent stop")
    await agent.stop()
    snapshot = agent.snapshot()
    await hub.broadcast(snapshot)
    return snapshot


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
    if not current_user(websocket, settings, db):
        await websocket.close(code=1008)
        return
    await hub.connect(websocket)
    try:
        await websocket.send_text(json.dumps(agent.snapshot()))
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
