from __future__ import annotations

import json
import re
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from .account import AccountService
from .agent import TradingAgentService
from .auth import is_admin_request, login_admin, logout_admin, require_admin
from .config import CONFIG_KEYS, CONFIG_SCHEMA, SECRET_FIELDS, Settings, public_settings, settings_from_overrides
from .db import Database
from .delivery_data import DeliveryDataService
from .institutional_feeds import FreeInstitutionalFeedsService
from .llm_brain import LLMBrain
from .macro import GlobalIntelligenceService
from .macro_calendar import MacroCalendarService
from .market_breadth import MarketBreadthService
from .market_data import YahooMarketDataProvider, build_market_data_provider
from .order_router import build_order_router
from .paper_broker import PaperBroker
from .sector_rotation import SectorRotationService
from .sentiment import SentimentService
from .strategy import StrategyEngine


base_settings = Settings()
db = Database(base_settings.database_path)
db.init()
settings = settings_from_overrides(base_settings, db.runtime_settings())
db.seed_universe(settings.universe_csv)


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
    new_llm = LLMBrain(new_settings)
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
        interval_seconds=new_settings.agent_interval_seconds,
        cycle_timeout_seconds=new_settings.cycle_timeout_seconds,
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
llm = stack["llm"]
strategy = stack["strategy"]
agent = stack["agent"]

app = FastAPI(title="OpenTrade")
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")


@app.on_event("startup")
async def startup() -> None:
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
async def status() -> dict[str, Any]:
    snapshot = agent.snapshot()
    snapshot["runtime"] = {
        "market_data_provider": settings.market_data_provider,
        "execution_mode": settings.execution_mode,
        "llm_provider": settings.llm_provider,
        "llm_decision_mode": settings.llm_decision_mode,
        "llm_model_fallback_enabled": settings.llm_model_fallback_enabled,
        "llm_rolling_context_enabled": settings.llm_rolling_context_enabled,
    }
    return snapshot


@app.get("/api/decisions/{decision_id}")
async def decision_detail(decision_id: int) -> dict[str, Any]:
    row = db.decision_by_id(decision_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Decision not found")
    return row


@app.get("/api/orders/{order_id}")
async def order_detail(order_id: int) -> dict[str, Any]:
    row = db.order_by_id(order_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Order not found")
    return row


@app.post("/api/analyze-symbol")
async def analyze_symbol(payload: dict[str, Any], request: Request) -> dict[str, Any]:
    require_admin(request, settings)
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

    if symbol not in quotes:
        try:
            fallback = YahooMarketDataProvider(settings)
            quotes = await fallback.get_quotes([row])
            candles = await fallback.get_candles([row])
        except Exception as exc:
            provider_error = f"{provider_error or ''} yahoo_fallback={exc.__class__.__name__}: {exc}".strip()

    quote = quotes.get(symbol)
    if quote is None:
        raise HTTPException(status_code=404, detail=f"No market quote found for {symbol}. Check the symbol spelling.")

    news = await sentiment.analyze_symbol_news(row)
    db.upsert_quotes(quotes)
    db.upsert_candles(candles)
    macro_context = db.get_state("macro_context", {})
    institutional_context = db.get_state("institutional_context", {})
    decisions = await strategy.evaluate(
        [row],
        quotes,
        broker.positions_by_symbol(),
        candles,
        macro_context,
        institutional_context,
        delivery_service,
        db.get_state("market_breadth_context", {}),
        db.get_state("sector_rotation_context", {}),
        macro_calendar,
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
        "candle_count": len(candles.get(symbol, [])),
        "news": news,
        "provider": quote.source,
        "provider_error": provider_error,
        "decision": decision_payload,
    }


@app.get("/api/account")
async def account_details() -> dict[str, Any]:
    return await account.snapshot()


@app.get("/api/config")
async def get_config() -> dict[str, Any]:
    return {
        "schema": CONFIG_SCHEMA,
        "settings": public_settings(settings),
    }


@app.get("/api/logs")
async def agent_logs(request: Request, limit: int = 300) -> dict[str, Any]:
    require_admin(request, settings)
    safe_limit = max(1, min(int(limit), 1000))
    return {"logs": db.latest_agent_logs(safe_limit)}


@app.get("/api/market-breadth")
async def market_breadth_snapshot() -> dict[str, Any]:
    return db.get_state("market_breadth_context", {})


@app.get("/api/sector-rotation")
async def sector_rotation_snapshot() -> dict[str, Any]:
    return db.get_state("sector_rotation_context", {})


@app.get("/api/macro-calendar")
async def macro_calendar_snapshot() -> dict[str, Any]:
    context = db.get_state("macro_calendar_context", {})
    if not context:
        return {"enabled": settings.enable_macro_calendar, "events": macro_calendar.upcoming_events(30)}
    return context


@app.post("/api/llm/test")
async def test_llm(request: Request) -> dict[str, Any]:
    require_admin(request, settings)
    return await llm.test_connection()


@app.post("/api/config")
async def update_config(payload: dict[str, Any], request: Request) -> dict[str, Any]:
    global settings, market_data, order_router, broker, account, sentiment, macro, institutional_feeds, delivery_service, market_breadth, sector_rotation, macro_calendar, llm, strategy, agent
    require_admin(request, settings)

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

    was_running = agent.running
    await agent.stop()
    await delivery_service.stop_background_task()
    db.update_runtime_settings(candidate_overrides)
    db.insert_agent_log(
        "INFO",
        "admin",
        "config_saved",
        "Runtime configuration saved",
        {
            "changed_keys": sorted(key for key in incoming.keys() if key in CONFIG_KEYS),
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
    llm = candidate_stack["llm"]
    strategy = candidate_stack["strategy"]
    agent = candidate_stack["agent"]
    delivery_service.start_background_task()
    if was_running:
        agent.start()

    snapshot = await status()
    await hub.broadcast(snapshot)
    return {"config": await get_config(), "status": snapshot}


@app.get("/api/auth/me")
async def auth_me(request: Request) -> dict[str, Any]:
    return {
        "admin": is_admin_request(request, settings),
        "admin_configured": bool(settings.admin_password),
    }


@app.post("/api/auth/login")
async def auth_login(payload: dict[str, Any], response: Response) -> dict[str, Any]:
    return login_admin(str(payload.get("username", "")), str(payload.get("password", "")), response, settings)


@app.post("/api/auth/logout")
async def auth_logout(response: Response) -> dict[str, bool]:
    return logout_admin(response)


@app.post("/api/control/start")
async def start_agent(request: Request) -> dict[str, Any]:
    require_admin(request, settings)
    db.insert_agent_log("INFO", "admin", "control_start", "Admin requested agent start")
    agent.start()
    snapshot = agent.snapshot()
    await hub.broadcast(snapshot)
    return snapshot


@app.post("/api/control/stop")
async def stop_agent(request: Request) -> dict[str, Any]:
    require_admin(request, settings)
    db.insert_agent_log("INFO", "admin", "control_stop", "Admin requested agent stop")
    await agent.stop()
    snapshot = agent.snapshot()
    await hub.broadcast(snapshot)
    return snapshot


@app.post("/api/control/run-once")
async def run_once(request: Request) -> dict[str, Any]:
    require_admin(request, settings)
    db.insert_agent_log("INFO", "admin", "control_run_once", "Admin requested one manual cycle")
    return await agent.run_once()


@app.post("/api/control/reset-demo")
async def reset_demo(request: Request) -> dict[str, Any]:
    require_admin(request, settings)
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
    snapshot = await status()
    await hub.broadcast(snapshot)
    return snapshot


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    await hub.connect(websocket)
    await websocket.send_text(json.dumps(agent.snapshot()))
    try:
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
