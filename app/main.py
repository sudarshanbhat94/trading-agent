from __future__ import annotations

import asyncio
import json
import re
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse
from uuid import uuid4

import httpx
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.gzip import GZipMiddleware
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
from .llm_policy import (
    LLM_DISABLED_REASON,
    LLM_HARD_DISABLED,
    assigned_llm_from_payload as _policy_assigned_llm_from_payload,
    runtime_overrides_without_llm as _runtime_overrides_without_llm,
    settings_without_llm as _settings_without_llm,
)
from .llm_usage import credit_breakdown_for_usage
from .macro import GlobalIntelligenceService
from .macro_calendar import MacroCalendarService
from .market_breadth import MarketBreadthService
from .market_day_regime import compute_market_day_regimes
from .market_data import (
    AlpacaMarketDataProvider,
    MarketDataError,
    build_market_data_provider,
    normalize_indstocks_access_token,
    normalize_upstox_access_token,
)
from .market_regions import filter_universe_for_open_markets, market_region_for_row, market_session_context, normalize_market_region
from .models import Decision, utc_now
from .order_router import build_order_router
from .options_intelligence import OptionsIntelligenceService
from .openclaw_bridge import (
    OpenClawNotifier,
    breakout_scan,
    bridge_context,
    default_openclaw_user,
    require_openclaw_bridge,
    select_stock_candidates,
)
from .paper_broker import PaperBroker
from .request_context import current_llm_usage_scope, current_user_id
from .rally_plan import build_rally_plan, build_rally_plan_by_market
from .sector_rotation import SectorRotationService
from .signal_quality import (
    AUTO_FOLLOW_REENTRY_COOLDOWN_HOURS,
    FRESH_BUY_WINDOW_MINUTES,
    auto_follow_quality_gate,
    fresh_buy_quality_gate,
    quality_size_multiplier,
    quality_skip_payload,
)
from .sentiment import SentimentService
from .strategy import StrategyEngine
from .trade_economics import auto_follow_sizing
from .trading_readiness import (
    build_33_point_report,
    build_broker_sync_status,
    build_data_freshness_report,
    build_trading_readiness,
    latest_replay_review,
    live_order_gate,
    run_replay_validation,
    set_trading_kill_switch,
)
from .universe import UniverseService


base_settings = _settings_without_llm(Settings())
db = Database(base_settings.database_path)
db.init()
settings = _settings_without_llm(settings_from_overrides(base_settings, _runtime_overrides_without_llm(db.runtime_settings())))
if settings.admin_password:
    db.ensure_default_admin_user(settings.admin_username, hash_password(settings.admin_password))
if LLM_HARD_DISABLED:
    db.update_runtime_settings(_runtime_overrides_without_llm(db.runtime_settings()))
    with db.connect() as conn:
        conn.execute(
            """
            update users
            set assigned_llm_provider = 'offline',
                assigned_llm_model = 'offline',
                updated_at = ?
            where coalesce(assigned_llm_provider, '') != 'offline'
               or coalesce(assigned_llm_model, '') != 'offline'
            """,
            (utc_now(),),
        )
db.seed_universe(settings.universe_csv, disable_missing=settings.universe_source == "csv")
if settings.us_universe_csv.exists():
    db.seed_universe(settings.us_universe_csv, disable_missing=False)


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


def _estimated_signal_credit_charge() -> float:
    if LLM_HARD_DISABLED:
        return 0.0
    return db.average_signal_credit_charge(tokens_per_credit=settings.credit_tokens_per_credit)


def _credit_billing_for_usage(usage: dict[str, Any]) -> dict[str, Any]:
    return credit_breakdown_for_usage(
        usage,
        tokens_per_credit=settings.credit_tokens_per_credit,
        margin_pct=settings.credit_platform_margin_pct,
    )


def _exception_message(exc: BaseException) -> str:
    text = str(exc).strip()
    return f"{exc.__class__.__name__}: {text}" if text else exc.__class__.__name__


def _normalize_signal_execution_mode(value: Any) -> str:
    mode = str(value or "SIGNAL_ONLY").strip().upper()
    aliases = {
        "SIGNALS": "SIGNAL_ONLY",
        "SIGNAL": "SIGNAL_ONLY",
        "SIGNAL_ONLY": "SIGNAL_ONLY",
        "PAPER": "AUTO_PAPER",
        "AUTO_PAPER": "AUTO_PAPER",
        "LIVE": "AUTO_LIVE",
        "AUTO_LIVE": "AUTO_LIVE",
    }
    return aliases.get(mode, "SIGNAL_ONLY")


def _signal_execution_mode_message(mode: str) -> str:
    normalized = _normalize_signal_execution_mode(mode)
    if normalized == "AUTO_PAPER":
        return "BUY ideas are paper-followed automatically within your paper cash, and stop/exit signals close followed paper ideas."
    if normalized == "AUTO_LIVE":
        return "BUY ideas and exit signals create guarded live requests only when your personal broker guard passes; US live trading remains disabled."
    return "Signals are saved only. You can track, paper, or live-follow ideas manually."


def _is_recoverable_user_signal_exception(exc: BaseException) -> bool:
    original = exc.original if isinstance(exc, UserSignalCycleError) else exc
    if isinstance(original, (asyncio.TimeoutError, TimeoutError, httpx.TimeoutException, MarketDataError)):
        return True
    name = original.__class__.__name__.lower()
    return "timeout" in name or "connect" in name


class UserSignalCycleError(RuntimeError):
    def __init__(self, phase: str, original: BaseException, details: dict[str, Any] | None = None) -> None:
        self.phase = phase
        self.original = original
        self.details = details or {}
        self.recoverable = _is_recoverable_user_signal_exception(original)
        super().__init__(f"{phase}: {_exception_message(original)}")


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
        status.setdefault("last_llm_activity", {})
        status.setdefault("auto_trade", {})
        status.setdefault("last_decision_count", 0)
        status.setdefault("symbols_per_cycle", self._symbol_limit({}, _estimated_signal_credit_charge()))
        monitor_symbols = db.user_monitor_symbols(user_id)
        status["monitor_scope"] = "CUSTOM" if monitor_symbols else "DYNAMIC_OPPORTUNITY"
        status["monitor_symbols_count"] = len(monitor_symbols)
        status["monitor_symbols_sample"] = monitor_symbols[:12]
        status["running"] = self.is_running(user_id)
        return status

    def admin_summary(self) -> dict[str, Any]:
        active = [user_id for user_id in self._tasks if self.is_running(user_id)]
        return {
            "running_users": len(active),
            "active_user_ids": active,
            "sessions": {str(user_id): self.status(user_id) for user_id in active},
        }

    def update_phase(self, user_id: int, phase: str, details: dict[str, Any] | None = None) -> None:
        status = self.status(user_id)
        status.update(
            {
                "running": self.is_running(user_id) or bool(status.get("running")),
                "phase": phase,
                "phase_details": details or {},
                "phase_updated_at": utc_now(),
            }
        )
        self._status[user_id] = status

    async def start(self, user: dict[str, Any]) -> dict[str, Any]:
        user_id = int(user["id"])
        if self.is_running(user_id):
            return _status_payload(user)
        estimated_charge = _estimated_signal_credit_charge()
        can_spend, credit_summary = db.user_has_credit_for(user_id, estimated_charge)
        if not can_spend:
            raise HTTPException(
                status_code=402,
                detail=f"Insufficient credits or daily budget to start signals. Estimated need: {estimated_charge:.4f} credits.",
            )
        _market_data_provider_for_user(user, settings.market_region)
        monitor_symbols = db.user_monitor_symbols(user_id)
        self._status[user_id] = {
            "running": True,
            "phase": "starting",
            "started_at": utc_now(),
            "last_cycle_at": None,
            "last_error": None,
            "last_credit_charge": 0.0,
            "last_llm_calls": 0,
            "last_llm_activity": {},
            "last_decision_count": 0,
            "symbols_per_cycle": self._symbol_limit(credit_summary, estimated_charge),
            "monitor_scope": "CUSTOM" if monitor_symbols else "DYNAMIC_OPPORTUNITY",
            "monitor_symbols_count": len(monitor_symbols),
            "monitor_symbols_sample": monitor_symbols[:12],
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
        monitor_symbols = db.user_monitor_symbols(user_id)
        if monitor_symbols:
            monitor_universe = self._monitor_universe(user_id, full_universe, limit, monitor_symbols)
            return monitor_universe
        opportunity_universe = self._opportunity_universe(user_id, full_universe, limit)
        if opportunity_universe:
            return opportunity_universe
        if limit >= len(full_universe):
            return full_universe
        start = self._cursors.get(user_id, 0) % len(full_universe)
        selected = [full_universe[(start + index) % len(full_universe)] for index in range(limit)]
        self._cursors[user_id] = (start + limit) % len(full_universe)
        return selected

    def _monitor_universe(
        self,
        user_id: int,
        full_universe: list[dict[str, Any]],
        limit: int,
        symbols: list[str],
    ) -> list[dict[str, Any]]:
        row_by_symbol = {str(row.get("symbol") or "").upper(): row for row in full_universe}
        custom_rows = [row_by_symbol[symbol] for symbol in symbols if symbol in row_by_symbol]
        if not custom_rows:
            return []
        capped_limit = max(1, min(limit, len(custom_rows)))
        start = self._cursors.get(user_id, 0) % len(custom_rows)
        selected = [custom_rows[(start + index) % len(custom_rows)] for index in range(capped_limit)]
        self._cursors[user_id] = (start + capped_limit) % len(custom_rows)
        return selected

    def _opportunity_universe(
        self,
        user_id: int,
        full_universe: list[dict[str, Any]],
        limit: int,
    ) -> list[dict[str, Any]]:
        if not getattr(settings, "dynamic_opportunity_scan_enabled", True):
            return []
        scan = db.get_state("opportunity_scan", {})
        if not isinstance(scan, dict) or not scan.get("enabled"):
            return []
        candidates = scan.get("top_candidates") or []
        if not isinstance(candidates, list):
            return []
        row_by_symbol = {str(row.get("symbol") or "").upper(): row for row in full_universe}
        symbols = [
            str(item.get("symbol") or "").upper()
            for item in candidates
            if isinstance(item, dict) and str(item.get("symbol") or "").upper() in row_by_symbol
        ]
        if not symbols:
            return []
        capped_limit = max(1, min(limit, len(symbols)))
        start = self._cursors.get(user_id, 0) % len(symbols)
        ordered = [symbols[(start + index) % len(symbols)] for index in range(len(symbols))]
        selected = [row_by_symbol[symbol] for symbol in ordered[:capped_limit]]
        self._cursors[user_id] = (start + capped_limit) % len(symbols)
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
                    message = _exception_message(exc)
                    recoverable = _is_recoverable_user_signal_exception(exc)
                    error_details: dict[str, Any] = {
                        "user_id": user_id,
                        "error_type": exc.__class__.__name__,
                        "recoverable": recoverable,
                    }
                    if isinstance(exc, UserSignalCycleError):
                        error_details.update(
                            {
                                "phase": exc.phase,
                                "original_error_type": exc.original.__class__.__name__,
                                "original_error": str(exc.original)[:300],
                                "cycle_details": exc.details,
                            }
                        )
                    if recoverable:
                        self._status[user_id] = {
                            **self.status(user_id),
                            "running": True,
                            "phase": "sleep",
                            "last_error": message,
                            "last_cycle_failed_at": utc_now(),
                        }
                        db.insert_agent_log(
                            "WARN",
                            "user_session",
                            "user_signal_retry_scheduled",
                            f"User signal cycle had a transient error and will retry: {message}",
                            error_details,
                        )
                        await hub.broadcast(_status_payload())
                    else:
                        self._status[user_id] = {
                            **self.status(user_id),
                            "running": False,
                            "phase": "idle",
                            "last_error": message,
                        }
                        self._tasks.pop(user_id, None)
                        db.insert_agent_log(
                            "ERROR",
                            "user_session",
                            "user_signal_error",
                            f"User signal session stopped: {message}",
                            error_details,
                        )
                        await hub.broadcast(_status_payload())
                        return
                await asyncio.sleep(max(30, int(settings.agent_interval_seconds or 180)))
        finally:
            if self._tasks.get(user_id) and self._tasks[user_id].done():
                self._tasks.pop(user_id, None)


def build_agent_stack(new_settings: Settings) -> dict[str, Any]:
    new_settings = _settings_without_llm(new_settings)
    new_market_data = build_market_data_provider(new_settings)
    new_order_router = build_order_router(new_settings, db)
    new_broker = PaperBroker(new_settings, db, new_order_router)
    new_account = AccountService(new_settings, db)
    monitor_settings = _settings_without_llm(replace(
        new_settings,
        llm_provider="offline",
        llm_decision_mode="offline",
        enable_llm_sentiment=False,
    ))
    strategy_settings = _settings_without_llm(replace(new_settings, enable_llm_sentiment=False))
    new_sentiment = SentimentService(monitor_settings, db)
    new_macro = GlobalIntelligenceService(new_settings)
    new_institutional_feeds = FreeInstitutionalFeedsService(new_settings)
    new_delivery_service = DeliveryDataService(new_settings, db)
    new_market_breadth = MarketBreadthService(new_settings, db)
    new_sector_rotation = SectorRotationService(new_settings, db)
    new_macro_calendar = MacroCalendarService(new_settings, db)
    new_universe_service = UniverseService(new_settings, db)
    new_options_intelligence = OptionsIntelligenceService(new_settings, db)
    new_openclaw_notifier = OpenClawNotifier(new_settings, db)
    monitor_llm = LLMBrain(strategy_settings, db)
    admin_llm = LLMBrain(new_settings, db)
    new_strategy = StrategyEngine(strategy_settings, new_sentiment, monitor_llm)
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
        market_region=new_settings.market_region,
        universe_symbols_per_cycle=new_settings.universe_symbols_per_cycle,
        execute_trades=False,
        on_update=hub.broadcast,
        openclaw_notifier=new_openclaw_notifier,
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
        "openclaw_notifier": new_openclaw_notifier,
        "llm": admin_llm,
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
openclaw_notifier = stack["openclaw_notifier"]
llm = stack["llm"]
strategy = stack["strategy"]
agent = stack["agent"]
user_signal_sessions = UserSignalSessionManager()
maintenance_task: asyncio.Task | None = None
position_mark_task: asyncio.Task | None = None

app = FastAPI(title="OpenStocks")
app.add_middleware(GZipMiddleware, minimum_size=1024)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")


COMMON_SYMBOL_ALIASES = {
    "SBI": "SBIN",
    "STATEBANK": "SBIN",
    "STATEBANKOFINDIA": "SBIN",
    "STATEBANKINDIA": "SBIN",
    "M&M": "M&M",
    "MM": "M&M",
}


def _db_maintenance_policy(current_settings: Settings) -> dict[str, Any]:
    return {
        "enabled": current_settings.enable_db_maintenance,
        "interval_hours": current_settings.db_maintenance_interval_hours,
        "full_audit_keep_latest": current_settings.db_retention_full_audit_keep_latest,
        "hold_decision_days": current_settings.db_retention_hold_decision_days,
        "full_audit_days": current_settings.db_retention_full_audit_days,
        "market_tick_days": current_settings.db_retention_market_tick_days,
        "sentiment_days": current_settings.db_retention_sentiment_days,
        "llm_usage_days": current_settings.db_retention_llm_usage_days,
        "delivery_days": current_settings.db_retention_delivery_days,
        "candle_rows_per_symbol_source": current_settings.db_retention_candle_rows_per_symbol_source,
        "vacuum": current_settings.db_retention_vacuum,
    }


def _quote_source_counts(quotes: dict[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for quote in quotes.values():
        source = str(getattr(quote, "source", "") or "unknown")
        counts[source] = counts.get(source, 0) + 1
    return counts


def _position_quote_refresh_rows(
    active_rows: list[dict[str, Any]],
    session_context: dict[str, Any],
    refresh_region: str,
) -> tuple[list[dict[str, Any]], bool]:
    if not settings.skip_market_data_when_closed:
        return active_rows, False

    refresh_rows = filter_universe_for_open_markets(active_rows, session_context)
    seen = {str(row.get("symbol") or "").upper() for row in refresh_rows}
    closed_us_polling = (
        normalize_market_region(refresh_region or "BOTH", default="BOTH") in {"US", "BOTH"}
        and settings.us_market_data_provider in {"alpaca", "alpaca_yahoo"}
        and bool(settings.alpaca_api_key and settings.alpaca_api_secret)
    )
    if not closed_us_polling:
        return refresh_rows, False

    added_closed_us = False
    for row in active_rows:
        symbol = str(row.get("symbol") or "").upper()
        if not symbol or symbol in seen or market_region_for_row(row) != "US":
            continue
        refresh_rows.append(row)
        seen.add(symbol)
        added_closed_us = True
    return refresh_rows, added_closed_us


async def _position_quote_refresh_loop() -> None:
    last_error = ""
    last_idle_signature = ""
    while True:
        sleep_seconds = max(1.0, float(getattr(settings, "position_quote_refresh_seconds", 1.0) or 1.0))
        try:
            if not getattr(settings, "position_quote_refresh_enabled", True):
                await asyncio.sleep(sleep_seconds)
                continue

            refresh_region = normalize_market_region(settings.market_region or "BOTH", default="BOTH")
            active_rows = db.active_position_universe(market_region=refresh_region)
            if not active_rows:
                idle_signature = f"idle:no_active_{refresh_region.lower()}_positions"
                if idle_signature != last_idle_signature:
                    last_idle_signature = idle_signature
                    db.set_state(
                        "position_quote_refresh",
                        {
                            "enabled": True,
                            "status": "idle",
                            "market_region": refresh_region,
                            "reason": "no_active_positions",
                            "updated_at": utc_now(),
                        },
                    )
                await asyncio.sleep(sleep_seconds)
                continue

            session_context = market_session_context(refresh_region, active_rows)
            refresh_rows, closed_us_polling = _position_quote_refresh_rows(active_rows, session_context, refresh_region)
            if not refresh_rows:
                active_symbols = [row.get("symbol") for row in active_rows]
                idle_signature = f"paused:markets_closed:{refresh_region}:{','.join(str(symbol) for symbol in active_symbols)}"
                if idle_signature != last_idle_signature:
                    last_idle_signature = idle_signature
                    db.set_state(
                        "position_quote_refresh",
                        {
                            "enabled": True,
                            "status": "paused",
                            "market_region": refresh_region,
                            "reason": "markets_closed",
                            "active_symbols": active_symbols,
                            "open_regions": session_context.get("open_regions"),
                            "closed_regions": session_context.get("closed_regions"),
                            "updated_at": utc_now(),
                        },
                    )
                await asyncio.sleep(sleep_seconds)
                continue

            quotes = await market_data.get_quotes(refresh_rows)
            if quotes:
                db.upsert_quotes(quotes)
                broker.sync_marks(quotes)
                marked = db.refresh_active_position_marks(quotes.keys())
                updated_at = utc_now()
                db.set_state(
                    "position_quote_refresh",
                    {
                        "enabled": True,
                        "status": "running",
                        "market_region": refresh_region,
                        "interval_seconds": sleep_seconds,
                        "active_symbols": [row.get("symbol") for row in active_rows],
                        "refreshed_symbols": sorted(quotes.keys()),
                        "closed_us_polling": closed_us_polling,
                        "open_regions": session_context.get("open_regions"),
                        "closed_regions": session_context.get("closed_regions"),
                        "quote_count": len(quotes),
                        "marked_positions": marked,
                        "source_counts": _quote_source_counts(quotes),
                        "updated_at": updated_at,
                    },
                )
                await hub.broadcast(
                    {
                        "event": "position_marks_refreshed",
                        "market_region": refresh_region,
                        "symbols": sorted(quotes.keys()),
                        "updated_at": updated_at,
                    }
                )
                last_error = ""
                last_idle_signature = ""
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            message = _exception_message(exc)
            db.set_state(
                "position_quote_refresh",
                {"enabled": True, "status": "error", "error": message, "updated_at": utc_now()},
            )
            if message != last_error:
                last_error = message
                db.insert_agent_log(
                    "WARN",
                    "position_marks",
                    "fast_quote_refresh_failed",
                    f"Fast position quote refresh failed: {message}",
                    {"error_type": exc.__class__.__name__, "error": str(exc)[:500]},
                )
        await asyncio.sleep(sleep_seconds)


async def _maintenance_loop() -> None:
    while True:
        try:
            summary = await asyncio.to_thread(db.run_data_retention, _db_maintenance_policy(settings), False)
            if summary.get("ran"):
                await hub.broadcast(agent.snapshot())
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            db.insert_agent_log(
                "WARN",
                "maintenance",
                "db_retention_failed",
                f"Database retention maintenance failed: {_exception_message(exc)}",
                {"error_type": exc.__class__.__name__, "error": str(exc)[:500]},
            )
        await asyncio.sleep(3600)


@app.on_event("startup")
async def startup() -> None:
    global maintenance_task, position_mark_task
    await universe_service.refresh_if_enabled()
    delivery_service.start_background_task()
    maintenance_task = asyncio.create_task(_maintenance_loop())
    position_mark_task = asyncio.create_task(_position_quote_refresh_loop())
    if settings.auto_start_agent:
        agent.start()


@app.on_event("shutdown")
async def shutdown() -> None:
    if maintenance_task:
        maintenance_task.cancel()
        try:
            await maintenance_task
        except asyncio.CancelledError:
            pass
    if position_mark_task:
        position_mark_task.cancel()
        try:
            await position_mark_task
        except asyncio.CancelledError:
            pass
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
    return await asyncio.to_thread(_status_payload, user)


@app.get("/api/signals/search")
async def signal_search(request: Request) -> dict[str, Any]:
    user = require_user(request, settings, db)
    query = str(request.query_params.get("q") or "").strip()
    market = normalize_market_region(request.query_params.get("market") or "BOTH", default="BOTH")
    try:
        limit = max(1, min(int(request.query_params.get("limit") or 120), 300))
    except (TypeError, ValueError):
        limit = 120
    if not query:
        return {"query": "", "market": market, "results": []}
    rows = db.search_decision_summaries(query, limit=limit, market_region=market)
    results = []
    for row in rows:
        item = dict(row)
        item["detail_url"] = f"/api/decisions/{item.get('id')}"
        if user.get("role") != "admin":
            item.pop("details_json", None)
        results.append(item)
    return {"query": query, "market": market, "count": len(results), "results": results}


@app.get("/api/market-indices")
async def market_indices(request: Request) -> dict[str, Any]:
    require_user(request, settings, db)
    market = normalize_market_region(request.query_params.get("market") or "IN", default="IN")
    if market == "US":
        return {"market": "US", "status": "quotes", "items": {}}
    indices = await institutional_feeds.indices_now()
    if indices.get("status") == "ok":
        institutional_context = db.get_state("institutional_context", {})
        if isinstance(institutional_context, dict):
            feeds = institutional_context.setdefault("feeds", {})
            feeds["indices"] = indices
            db.set_state("institutional_context", institutional_context)
    return {"market": "IN", **indices}


def _compact_tracked_idea(row: dict[str, Any]) -> dict[str, Any]:
    user_follow = row.get("user_follow") if isinstance(row.get("user_follow"), dict) else {}
    follow_details = row.get("follow_details") if isinstance(row.get("follow_details"), dict) else {}
    mark_state = follow_details.get("mark_state") if isinstance(follow_details.get("mark_state"), dict) else {}
    keys = (
        "id",
        "idea_id",
        "follow_id",
        "symbol",
        "company_name",
        "market_region",
        "exchange",
        "sector",
        "industry",
        "mode",
        "qty",
        "follow_entry_price",
        "follow_latest_price",
        "latest_price",
        "invested_amount",
        "unrealized_pnl",
        "return_pct",
        "followed_at",
        "follow_updated_at",
        "strategy",
        "signal_type",
        "status",
        "suggestion",
        "targets",
        "target_status",
        "highest_target_hit",
        "lifecycle_status",
        "stop_status",
        "stop_loss",
        "entry_zone",
        "risk_flags",
        "expires_at",
        "days_to_expiry",
        "timeline",
        "quote_updated_at",
        "quote_source",
    )
    compact = {key: row.get(key) for key in keys if key in row}
    compact["marked_at"] = mark_state.get("last_mark_at") or row.get("follow_updated_at")
    compact["user_follow"] = {
        key: user_follow.get(key)
        for key in (
            "id",
            "user_id",
            "idea_id",
            "mode",
            "status",
            "qty",
            "entry_price",
            "latest_price",
            "invested_amount",
            "unrealized_pnl",
            "return_pct",
            "created_at",
            "updated_at",
        )
        if key in user_follow
    }
    return compact


_SIGNAL_IDEA_LIST_KEYS = (
    "id",
    "idea_id",
    "symbol",
    "company_name",
    "name",
    "market_region",
    "exchange",
    "sector",
    "industry",
    "strategy",
    "plan_code",
    "signal_type",
    "suggestion",
    "display_signal",
    "status",
    "price",
    "latest_price",
    "entry_price",
    "current_return_pct",
    "peak_return_pct",
    "worst_return_pct",
    "confidence",
    "combined_score",
    "confluence",
    "overall_score_pct",
    "overall_grade",
    "reason",
    "display_reason",
    "decision_readiness",
    "tier",
    "fresh_action",
    "fresh_action_label",
    "trade_state",
    "latest_system_action",
    "execution_state",
    "execution_state_label",
    "execution_state_note",
    "why_changed",
    "setup_bucket",
    "setup_bucket_label",
    "setup_bucket_reason",
    "opportunity_state",
    "opportunity_label",
    "opportunity_summary",
    "opportunity_next_step",
    "opportunity_reasons",
    "opportunity_terms",
    "entry_zone",
    "stop_loss",
    "expires_at",
    "days_to_expiry",
    "lifecycle_status",
    "highest_target_hit",
    "detail_url",
    "latest_decision_id",
    "decision_id",
    "watchlist_source",
    "quote_updated_at",
    "quote_source",
    "catalyst_type",
    "catalyst_date",
    "earnings_date",
    "news_quality",
    "headline_count",
)


def _compact_targets(values: Any, limit: int = 4) -> list[dict[str, Any]]:
    targets = values if isinstance(values, list) else []
    output: list[dict[str, Any]] = []
    for item in targets[:limit]:
        if not isinstance(item, dict):
            continue
        output.append(
            {
                key: item.get(key)
                for key in ("label", "price", "hit", "distance_pct", "basis", "probability_label", "suggested_exit_pct")
                if key in item
            }
        )
    return output


def _compact_user_follow(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    keys = (
        "id",
        "user_id",
        "idea_id",
        "mode",
        "status",
        "qty",
        "entry_price",
        "latest_price",
        "invested_amount",
        "unrealized_pnl",
        "return_pct",
        "created_at",
        "updated_at",
    )
    return {key: raw.get(key) for key in keys if key in raw}


def _compact_signal_idea(row: dict[str, Any]) -> dict[str, Any]:
    details = row.get("details") if isinstance(row.get("details"), dict) else {}
    full = details.get("full_spectrum") if isinstance(details.get("full_spectrum"), dict) else {}
    event_risk = full.get("corporate_event_risk") if isinstance(full.get("corporate_event_risk"), dict) else {}
    news_sentiment = full.get("news_sentiment") if isinstance(full.get("news_sentiment"), dict) else {}
    item = {key: row.get(key) for key in _SIGNAL_IDEA_LIST_KEYS if key in row}
    item["targets"] = _compact_targets(row.get("targets") or details.get("targets"))
    item["target_status"] = _compact_targets(row.get("target_status") or details.get("target_status"))
    if isinstance(row.get("timeline"), dict):
        item["timeline"] = {
            key: row["timeline"].get(key)
            for key in ("plan_code", "max_days", "started_at", "expires_at", "days_left", "label", "name")
            if key in row["timeline"]
        }
    elif isinstance(details.get("timeline"), dict):
        item["timeline"] = {
            key: details["timeline"].get(key)
            for key in ("plan_code", "max_days", "started_at", "expires_at", "days_left", "label", "name")
            if key in details["timeline"]
        }
    if isinstance(row.get("risk_flags"), list):
        item["risk_flags"] = row["risk_flags"][:6]
    elif isinstance(details.get("risk_flags"), list):
        item["risk_flags"] = details["risk_flags"][:6]
    if isinstance(row.get("stop_status"), dict):
        item["stop_status"] = {
            key: row["stop_status"].get(key)
            for key in ("price", "hit", "hit_at")
            if key in row["stop_status"]
        }
    elif isinstance(details.get("stop_status"), dict):
        item["stop_status"] = {
            key: details["stop_status"].get(key)
            for key in ("price", "hit", "hit_at")
            if key in details["stop_status"]
        }
    for target_key, source_key in (
        ("catalyst_type", "event_type"),
        ("catalyst_date", "earnings_date"),
        ("earnings_date", "earnings_date"),
        ("headline_count", "headline_count"),
    ):
        if item.get(target_key) in (None, ""):
            item[target_key] = event_risk.get(source_key) or news_sentiment.get(source_key)
    if item.get("news_quality") in (None, ""):
        item["news_quality"] = news_sentiment.get("bias") or news_sentiment.get("quality")
    follow = _compact_user_follow(row.get("user_follow"))
    if follow:
        item["user_follow"] = follow
    return item


def _compact_signal_ideas(rows: Any) -> list[dict[str, Any]]:
    return [_compact_signal_idea(row) for row in (rows or []) if isinstance(row, dict)]


def _compact_market_action_event(item: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "symbol",
        "name",
        "market_region",
        "exchange",
        "source",
        "strategy",
        "event_types",
        "pct_change",
        "volume_multiplier",
        "market_action_score",
        "score",
        "reason",
        "ts",
        "price",
    )
    output = {key: item.get(key) for key in keys if key in item}
    if isinstance(output.get("event_types"), list):
        output["event_types"] = output["event_types"][:4]
    return output


def _compact_playbook_record(item: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "symbol",
        "name",
        "sector",
        "market_region",
        "gain_pct",
        "final_signal",
        "tier",
        "quant_score",
        "volume_ratio",
        "tier_reasons",
        "reason",
    )
    output = {key: item.get(key) for key in keys if key in item}
    for nested_key in ("levels", "catalyst_review", "weinstein", "vcp", "relative_strength"):
        nested = item.get(nested_key)
        if isinstance(nested, dict):
            output[nested_key] = {
                key: nested.get(key)
                for key in (
                    "pivot",
                    "stop",
                    "catalyst_type",
                    "catalyst_strength",
                    "stage",
                    "score",
                    "rs_rank",
                )
                if key in nested
            }
    anti_patterns = item.get("anti_patterns")
    if isinstance(anti_patterns, list):
        output["anti_patterns"] = [
            {key: row.get(key) for key in ("code", "label", "reason") if key in row}
            for row in anti_patterns[:3]
            if isinstance(row, dict)
        ]
    audit = item.get("audit_trail")
    if isinstance(audit, dict):
        output["audit_trail"] = {key: audit.get(key) for key in ("watch", "avoid", "buy") if key in audit}
    if isinstance(output.get("tier_reasons"), list):
        output["tier_reasons"] = output["tier_reasons"][:4]
    return output


def _compact_playbook(playbook: Any) -> dict[str, Any]:
    if not isinstance(playbook, dict):
        return {}
    output = {
        key: value
        for key, value in playbook.items()
        if key
        not in {
            "records",
            "tomorrow_watchlist",
            "do_not_chase",
            "by_symbol",
            "raw",
        }
        and not isinstance(value, (list, dict))
    }
    for key in ("signal_summary", "tier_summary"):
        if isinstance(playbook.get(key), dict):
            output[key] = dict(playbook[key])
    output["records"] = [_compact_playbook_record(row) for row in (playbook.get("records") or [])[:30] if isinstance(row, dict)]
    output["tomorrow_watchlist"] = [
        _compact_playbook_record(row) for row in (playbook.get("tomorrow_watchlist") or [])[:20] if isinstance(row, dict)
    ]
    output["do_not_chase"] = [
        {"symbol": row.get("symbol"), "reason": row.get("reason"), "market_region": row.get("market_region")}
        for row in (playbook.get("do_not_chase") or [])[:20]
        if isinstance(row, dict)
    ]
    return output


def _compact_market_action_radar(source: Any) -> dict[str, Any]:
    if not isinstance(source, dict):
        return {}
    events = [_compact_market_action_event(row) for row in (source.get("events") or [])[:60] if isinstance(row, dict)]
    output = {
        key: value
        for key, value in source.items()
        if key not in {"events", "events_by_symbol", "by_market"} and not isinstance(value, (list, dict))
    }
    output["events"] = events
    output["events_by_symbol"] = {
        str(row.get("symbol") or "").upper(): row
        for row in events
        if row.get("symbol")
    }
    if isinstance(source.get("by_market"), dict):
        output["by_market"] = {
            str(market): _compact_market_action_radar(raw)
            for market, raw in source["by_market"].items()
            if isinstance(raw, dict)
        }
    return output


def _compact_pre_catalyst_candidate(item: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "symbol",
        "label",
        "market_region",
        "confidence",
        "score",
        "catalyst_type",
        "catalyst_date",
        "setup_summary",
        "pivot",
        "entry_zone",
        "invalidation_level",
        "key_reasons",
        "supporting_signals",
        "sector",
        "industry",
        "liquidity",
    )
    output = {key: item.get(key) for key in keys if key in item}
    for key in ("key_reasons", "supporting_signals"):
        if isinstance(output.get(key), list):
            output[key] = output[key][:6]
    return output


def _compact_pre_catalyst_discovery(discovery: Any) -> dict[str, Any]:
    if not isinstance(discovery, dict):
        return {}
    output = {
        key: value
        for key, value in discovery.items()
        if key
        not in {
            "calendar_enrichment",
            "market_action_history",
            "log_events",
            "candidates",
            "live_confirmations",
        }
        and not isinstance(value, (list, dict))
    }
    for key in ("label_counts", "data_gaps"):
        if isinstance(discovery.get(key), dict):
            output[key] = dict(discovery[key])
    calendar = discovery.get("calendar_enrichment") if isinstance(discovery.get("calendar_enrichment"), dict) else {}
    if calendar:
        output["calendar_enrichment"] = {
            key: calendar.get(key)
            for key in (
                "enabled",
                "source",
                "updated_at",
                "status",
                "known_earnings_symbols",
                "inferred_recent_catalyst_symbols",
                "missing_earnings_symbols",
                "data_gaps",
            )
            if key in calendar
        }
    output["candidates"] = [
        _compact_pre_catalyst_candidate(row)
        for row in (discovery.get("candidates") or [])[:60]
        if isinstance(row, dict)
    ]
    output["live_confirmations"] = [
        _compact_pre_catalyst_candidate(row)
        for row in (discovery.get("live_confirmations") or [])[:40]
        if isinstance(row, dict)
    ]
    output["candidate_count"] = len(output["candidates"])
    output["live_confirmation_count"] = len(output["live_confirmations"])
    return output


def _compact_tomorrow_plan_item(item: dict[str, Any]) -> dict[str, Any]:
    details = item.get("details") if isinstance(item.get("details"), dict) else {}
    keys = (
        "id",
        "idea_id",
        "symbol",
        "name",
        "market_region",
        "exchange",
        "sector",
        "industry",
        "plan_date",
        "prepared_at",
        "section",
        "section_rank",
        "sort_order",
        "action",
        "strategy",
        "score",
        "confidence",
        "trigger_price",
        "max_entry",
        "stop_loss",
        "target1",
        "target2",
        "validation",
        "rationale",
    )
    output = {key: item.get(key) for key in keys if key in item}
    for key in ("validation", "rationale"):
        if output.get(key) not in (None, ""):
            text = str(output[key]).strip()
            output[key] = text if len(text) <= 280 else f"{text[:277].rstrip()}..."
    if output.get("name") in (None, ""):
        output["name"] = details.get("name") or item.get("symbol")
    for key in ("entry_zone", "fresh_action", "overall_grade", "overall_score_pct", "current_return_pct"):
        if key in details and key not in output:
            output[key] = details.get(key)
    opportunity_state = details.get("opportunity_state")
    if isinstance(opportunity_state, dict):
        output["opportunity_state"] = {
            key: opportunity_state.get(key)
            for key in ("state", "label", "summary", "next_step")
            if key in opportunity_state
        }
        if isinstance(opportunity_state.get("reasons"), list):
            output["opportunity_state"]["reasons"] = [str(value)[:160] for value in opportunity_state["reasons"][:4]]
    elif opportunity_state not in (None, ""):
        output["opportunity_state"] = str(opportunity_state)[:220]
    if isinstance(details.get("risk_flags"), list):
        output["risk_flags"] = [str(value)[:120] for value in details["risk_flags"][:4]]
    if isinstance(details.get("failed_gates"), list):
        output["failed_gates"] = [
            {
                key: gate.get(key)
                for key in ("gate", "label", "reason", "severity")
                if isinstance(gate, dict) and key in gate
            }
            if isinstance(gate, dict)
            else {"gate": str(gate)[:160]}
            for gate in details["failed_gates"][:4]
        ]
    if isinstance(details.get("target_status"), list):
        output["target_status"] = _compact_targets(details["target_status"], limit=3)
    return output


def _compact_tomorrow_plan(plan: Any) -> dict[str, Any]:
    if not isinstance(plan, dict):
        return {}
    output = {
        key: value
        for key, value in plan.items()
        if key not in {"items", "sections", "by_market", "raw", "details"}
        and not isinstance(value, (list, dict))
    }
    if isinstance(plan.get("summary"), dict):
        output["summary"] = dict(plan["summary"])
    if isinstance(plan.get("preopen_rules"), list):
        output["preopen_rules"] = [
            {key: row.get(key) for key in ("time", "action", "reason") if key in row}
            for row in plan["preopen_rules"][:6]
            if isinstance(row, dict)
        ]
    has_by_market = isinstance(plan.get("by_market"), dict)
    items = [_compact_tomorrow_plan_item(row) for row in (plan.get("items") or []) if isinstance(row, dict)]
    if items and not has_by_market:
        output["items"] = items[:80]
    elif not has_by_market:
        output["items"] = []
    if has_by_market:
        output["by_market"] = {
            str(market): _compact_tomorrow_plan(raw)
            for market, raw in plan["by_market"].items()
            if isinstance(raw, dict)
        }
    return output


def _compact_rally_plan_item(item: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "symbol",
        "name",
        "market_region",
        "section",
        "stage",
        "action",
        "strategy",
        "score",
        "why",
        "what",
        "how",
        "trigger_price",
        "max_entry",
        "stop_loss",
        "target1",
        "invalidation",
        "blockers",
    )
    output = {key: item.get(key) for key in keys if key in item}
    for key in ("why", "what", "how", "invalidation"):
        if output.get(key) not in (None, ""):
            text = str(output[key]).strip()
            output[key] = text if len(text) <= 360 else f"{text[:357].rstrip()}..."
    if isinstance(item.get("evidence"), dict):
        evidence = item["evidence"]
        output["evidence"] = {
            key: value
            for key, value in evidence.items()
            if key in {"regime", "pre_catalyst", "live_confirmation", "tomorrow_plan", "market_action_radar", "opportunity_scan", "big_runner"}
        }
    return output


def _compact_rally_plan(plan: Any) -> dict[str, Any]:
    if not isinstance(plan, dict):
        return {}
    output = {
        key: value
        for key, value in plan.items()
        if key not in {"items", "sections", "by_market"} and not isinstance(value, (list, dict))
    }
    if isinstance(plan.get("regime"), dict):
        output["regime"] = {
            key: plan["regime"].get(key)
            for key in ("state", "score", "momentum_allowed", "summary", "reasons")
            if key in plan["regime"]
        }
    if isinstance(plan.get("source_status"), dict):
        output["source_status"] = plan["source_status"]
    if isinstance(plan.get("section_labels"), dict):
        output["section_labels"] = plan["section_labels"]
    has_by_market = isinstance(plan.get("by_market"), dict)
    if has_by_market:
        output["by_market"] = {
            str(market): _compact_rally_plan(raw)
            for market, raw in plan["by_market"].items()
            if isinstance(raw, dict)
        }
        return output
    items = [_compact_rally_plan_item(row) for row in (plan.get("items") or []) if isinstance(row, dict)]
    output["items"] = items[:100]
    if isinstance(plan.get("sections"), dict):
        output["sections"] = {
            section: [_compact_rally_plan_item(row) for row in rows[:30] if isinstance(row, dict)]
            for section, rows in plan["sections"].items()
            if isinstance(rows, list)
        }
    return output


def _compact_opportunity_scan(scan: Any) -> dict[str, Any]:
    if not isinstance(scan, dict):
        return {}
    output = {
        key: value
        for key, value in scan.items()
        if key
        not in {
            "top_candidates",
            "top_rally_radar",
            "top_big_runner_candidates",
            "top_fast_movers",
            "top_market_action",
            "btst_buy_candidates",
            "market_action_radar",
            "top_gainers_playbook",
            "top_gainers_playbook_by_market",
            "by_market",
        }
        and not isinstance(value, (list, dict))
    }
    for key in ("bucket_counts", "setup_counts", "rejected_counts", "filters", "raw_scan_policy", "tomorrow_plan"):
        if isinstance(scan.get(key), dict):
            output[key] = dict(scan[key])
    for key, limit in (
        ("top_candidates", 40),
        ("top_rally_radar", 25),
        ("top_big_runner_candidates", 25),
        ("top_fast_movers", 20),
        ("top_market_action", 20),
        ("btst_buy_candidates", 12),
    ):
        output[key] = [
            _compact_pre_catalyst_candidate(row) | _compact_market_action_event(row)
            for row in (scan.get(key) or [])[:limit]
            if isinstance(row, dict)
        ]
    output["market_action_radar"] = _compact_market_action_radar(scan.get("market_action_radar", {}))
    output["top_gainers_playbook"] = _compact_playbook(scan.get("top_gainers_playbook", {}))
    if isinstance(scan.get("top_gainers_playbook_by_market"), dict):
        output["top_gainers_playbook_by_market"] = {
            str(market): _compact_playbook(raw)
            for market, raw in scan["top_gainers_playbook_by_market"].items()
            if isinstance(raw, dict)
        }
    if isinstance(scan.get("by_market"), dict):
        output["by_market"] = {
            str(market): _compact_opportunity_scan(raw)
            for market, raw in scan["by_market"].items()
            if isinstance(raw, dict)
        }
    return output


def _compact_dashboard_payload(payload: dict[str, Any]) -> dict[str, Any]:
    for key in ("suggestions", "signal_ideas"):
        if isinstance(payload.get(key), list):
            payload[key] = _compact_signal_ideas(payload[key])
    if isinstance(payload.get("suggestions"), list):
        payload["signal_ideas"] = payload.get("suggestions", [])
    if isinstance(payload.get("suggestions_by_market"), dict):
        payload["suggestions_by_market"] = {
            market: _compact_signal_ideas(rows)
            for market, rows in payload["suggestions_by_market"].items()
        }
    if isinstance(payload.get("tracked_ideas"), list):
        payload["tracked_ideas"] = [_compact_tracked_idea(row) for row in payload["tracked_ideas"] if isinstance(row, dict)]
    if isinstance(payload.get("tracked_ideas_by_market"), dict):
        payload["tracked_ideas_by_market"] = {
            market: [_compact_tracked_idea(row) for row in (rows or []) if isinstance(row, dict)]
            for market, rows in payload["tracked_ideas_by_market"].items()
        }
    if isinstance(payload.get("opportunity_scan"), dict):
        payload["opportunity_scan"] = _compact_opportunity_scan(payload["opportunity_scan"])
    if isinstance(payload.get("market_action_radar"), dict):
        payload["market_action_radar"] = _compact_market_action_radar(payload["market_action_radar"])
    if isinstance(payload.get("pre_catalyst_discovery"), dict):
        payload["pre_catalyst_discovery"] = _compact_pre_catalyst_discovery(payload["pre_catalyst_discovery"])
    if isinstance(payload.get("tomorrow_plan"), dict):
        payload["tomorrow_plan"] = _compact_tomorrow_plan(payload["tomorrow_plan"])
    if isinstance(payload.get("rally_plan"), dict):
        payload["rally_plan"] = _compact_rally_plan(payload["rally_plan"])
    return payload


def _position_marks_payload(user: dict[str, Any]) -> dict[str, Any]:
    if user.get("role") == "admin":
        return {
            "ok": True,
            "updated_at": utc_now(),
            "tracked_ideas": [],
            "tracked_ideas_by_market": {"IN": [], "US": []},
            "follow_history": [],
            "follow_history_by_market": {"IN": [], "US": []},
            "positions": [],
            "portfolio": {},
            "portfolio_by_market": {},
            "paper": {"positions": [], "follow_history": [], "closed_positions": []},
        }

    user_id = int(user["id"])
    paper_cash_by_market = _user_paper_cash_by_market(user)
    tracked_ideas = db.user_followed_signal_ideas(user_id, 100)
    realized_pnl_by_market = db.user_follow_realized_pnl_by_market(user_id)
    user_portfolio = _user_follow_portfolio(
        tracked_ideas,
        db.latest_portfolio() or {},
        paper_cash_by_market=paper_cash_by_market,
        realized_pnl_by_market=realized_pnl_by_market,
    )
    positions = _user_follow_positions(tracked_ideas)
    compact_tracked_ideas = [_compact_tracked_idea(row) for row in tracked_ideas]
    compact_by_market = _rows_by_market(compact_tracked_ideas)
    equity_curve_by_market = _user_equity_curve_by_market(
        tracked_ideas,
        user_portfolio,
        paper_cash_by_market,
    )
    paper = {
        "positions": positions,
        "portfolio": user_portfolio,
        "portfolio_by_market": user_portfolio.get("portfolio_by_market", {}),
        "cash_pool_by_market": paper_cash_by_market,
        "realized_pnl_by_market": realized_pnl_by_market,
        "cash_by_market": {
            market: row.get("cash", 0.0)
            for market, row in (user_portfolio.get("portfolio_by_market") or {}).items()
        },
    }
    return {
        "ok": True,
        "updated_at": utc_now(),
        "tracked_ideas": compact_tracked_ideas,
        "tracked_ideas_by_market": compact_by_market,
        "positions": positions,
        "portfolio": user_portfolio,
        "portfolio_by_market": user_portfolio.get("portfolio_by_market", {}),
        "paper_cash_pool_by_market": paper_cash_by_market,
        "paper_realized_pnl_by_market": realized_pnl_by_market,
        "equity_curve_by_market": equity_curve_by_market,
        "equity_curve": _combined_equity_curve(equity_curve_by_market, user_portfolio),
        "paper": paper,
    }


@app.get("/api/position-marks")
async def position_marks(request: Request, response: Response) -> dict[str, Any]:
    user = require_user(request, settings, db)
    response.headers["Cache-Control"] = "no-store, max-age=0"
    return await asyncio.to_thread(_position_marks_payload, user)


def _monitor_symbols_for_user(user: dict[str, Any] | None) -> list[str]:
    if not user or user.get("role") == "admin":
        return []
    return db.user_monitor_symbols(int(user["id"]))


def _latest_signal_ideas_for_user(
    user_id: int,
    limit: int,
    *,
    market_region: str | None = None,
    monitor_symbols: list[str] | None = None,
) -> list[dict[str, Any]]:
    symbols = monitor_symbols if monitor_symbols is not None else db.user_monitor_symbols(user_id)
    return db.latest_signal_ideas(limit, user_id=user_id, market_region=market_region, symbols=symbols or None)


def _latest_decision_summaries_for_user(
    user_id: int,
    limit: int,
    *,
    market_region: str | None = None,
    monitor_symbols: list[str] | None = None,
) -> list[dict[str, Any]]:
    symbols = monitor_symbols if monitor_symbols is not None else db.user_monitor_symbols(user_id)
    return db.latest_decision_summaries(limit, market_region=market_region, symbols=symbols or None)


def _followed_signal_ideas_for_user(
    user_id: int,
    limit: int,
    *,
    market_region: str | None = None,
    monitor_symbols: list[str] | None = None,
) -> list[dict[str, Any]]:
    symbols = monitor_symbols if monitor_symbols is not None else db.user_monitor_symbols(user_id)
    return db.user_followed_signal_ideas(
        user_id,
        limit,
        market_region=market_region,
        symbols=symbols or None,
    )


def _follow_history_for_user(
    user_id: int,
    limit: int,
    *,
    market_region: str | None = None,
    monitor_symbols: list[str] | None = None,
) -> list[dict[str, Any]]:
    symbols = monitor_symbols if monitor_symbols is not None else db.user_monitor_symbols(user_id)
    return db.user_follow_history(
        user_id,
        limit,
        market_region=market_region,
        symbols=symbols or None,
    )


def _monitor_watchlist_for_user(
    user_id: int,
    *,
    market_region: str | None = None,
    monitor_symbols: list[str] | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    symbols = monitor_symbols if monitor_symbols is not None else db.user_monitor_symbols(user_id)
    if not symbols:
        return []
    return db.monitor_watchlist_rows(
        symbols,
        user_id=user_id,
        market_region=market_region,
        limit=limit,
    )


def _filter_strategy_plans_for_symbols(plans: list[dict[str, Any]], monitor_symbols: list[str]) -> list[dict[str, Any]]:
    if not monitor_symbols:
        return plans
    allowed = {str(symbol or "").upper() for symbol in monitor_symbols if str(symbol or "").strip()}
    filtered_plans: list[dict[str, Any]] = []
    for plan in plans:
        item = dict(plan)
        constituents = [
            dict(row)
            for row in (plan.get("constituents") or [])
            if str((row or {}).get("symbol") or "").upper() in allowed
        ]
        by_market: dict[str, list[dict[str, Any]]] = {}
        for market, rows in (plan.get("constituents_by_market") or {}).items():
            by_market[str(market)] = [
                dict(row)
                for row in (rows or [])
                if str((row or {}).get("symbol") or "").upper() in allowed
            ]
        item["constituents"] = constituents
        item["constituents_by_market"] = by_market
        item["top_symbols"] = [row.get("symbol") for row in constituents[:5]]
        item["active_idea_count"] = len(constituents)
        filtered_plans.append(item)
    return filtered_plans


def _filter_tomorrow_plan_for_symbols(plan: dict[str, Any], monitor_symbols: list[str]) -> dict[str, Any]:
    if not monitor_symbols or not isinstance(plan, dict):
        return plan
    allowed = {str(symbol or "").upper() for symbol in monitor_symbols if str(symbol or "").strip()}

    def row_allowed(row: Any) -> bool:
        return isinstance(row, dict) and str(row.get("symbol") or "").upper() in allowed

    def filter_single(raw: dict[str, Any]) -> dict[str, Any]:
        output = dict(raw)
        items = [dict(item) for item in (raw.get("items") or []) if row_allowed(item)]
        sections: dict[str, list[dict[str, Any]]] = {}
        for section, rows in (raw.get("sections") or {}).items():
            sections[str(section)] = [dict(item) for item in (rows or []) if row_allowed(item)]
        if not sections and items:
            for item in items:
                sections.setdefault(str(item.get("section") or ""), []).append(item)
        summary = dict(raw.get("summary") or {})
        for section, rows in sections.items():
            summary[section] = len(rows)
        for section in ("ready_at_open", "btst_buys", "near_breakout", "news_watch", "position_actions", "avoid"):
            summary.setdefault(section, 0)
            if section not in sections:
                summary[section] = 0
        summary["total_items"] = len(items)
        output["items"] = items
        output["sections"] = sections
        output["summary"] = summary
        output["monitor_scope"] = "CUSTOM"
        output["monitor_symbols_count"] = len(allowed)
        return output

    by_market = plan.get("by_market") if isinstance(plan.get("by_market"), dict) else {}
    if by_market:
        output = dict(plan)
        output["by_market"] = {
            str(market): filter_single(raw if isinstance(raw, dict) else {})
            for market, raw in by_market.items()
        }
        if plan.get("items") or plan.get("sections"):
            output.update(filter_single(plan))
            output["by_market"] = {
                str(market): filter_single(raw if isinstance(raw, dict) else {})
                for market, raw in by_market.items()
            }
        output["monitor_scope"] = "CUSTOM"
        output["monitor_symbols_count"] = len(allowed)
        return output
    return filter_single(plan)


def _tomorrow_plan_for_user(user: dict[str, Any], market_region: str = "BOTH") -> dict[str, Any]:
    plan = db.latest_tomorrow_plan(market_region)
    return _filter_tomorrow_plan_for_symbols(plan, _monitor_symbols_for_user(user))


def _filter_rally_plan_for_symbols(plan: dict[str, Any], monitor_symbols: list[str]) -> dict[str, Any]:
    if not monitor_symbols or not isinstance(plan, dict):
        return plan
    allowed = {str(symbol or "").upper() for symbol in monitor_symbols if str(symbol or "").strip()}

    def allowed_row(row: Any) -> bool:
        return isinstance(row, dict) and str(row.get("symbol") or "").upper() in allowed

    def filter_single(raw: dict[str, Any]) -> dict[str, Any]:
        output = dict(raw)
        items = [dict(row) for row in (raw.get("items") or []) if allowed_row(row)]
        sections: dict[str, list[dict[str, Any]]] = {}
        for section, rows in (raw.get("sections") or {}).items():
            sections[str(section)] = [dict(row) for row in (rows or []) if allowed_row(row)]
        output["items"] = items
        output["sections"] = sections
        output["monitor_scope"] = "CUSTOM"
        output["monitor_symbols_count"] = len(allowed)
        return output

    by_market = plan.get("by_market") if isinstance(plan.get("by_market"), dict) else {}
    if by_market:
        output = dict(plan)
        output["by_market"] = {
            str(market): filter_single(raw if isinstance(raw, dict) else {})
            for market, raw in by_market.items()
        }
        output["monitor_scope"] = "CUSTOM"
        output["monitor_symbols_count"] = len(allowed)
        return output
    return filter_single(plan)


def _latest_market_day_regime(market_region: str) -> dict[str, Any]:
    region = normalize_market_region(market_region or "BOTH", default="BOTH")
    quote_rows = db.latest_quotes(limit=None, market_region=region)
    symbols = [str(row.get("symbol") or "").upper() for row in quote_rows if row.get("symbol")]
    candle_sets = db.recent_candle_sets_by_symbol(symbols)
    quotes = {
        str(row.get("symbol") or "").upper(): {
            "price": row.get("price"),
            "open": row.get("open"),
            "high": row.get("high"),
            "low": row.get("low"),
            "close": row.get("close"),
        }
        for row in quote_rows
        if row.get("symbol")
    }
    return compute_market_day_regimes(
        quote_rows,
        quotes,
        candle_sets,
        db.get_state("market_breadth_context", {}),
        market_region=region,
    )


def _rally_plan_for_user(user: dict[str, Any], market_region: str = "BOTH") -> dict[str, Any]:
    region = normalize_market_region(market_region or "BOTH", default="BOTH")
    user_id = int(user["id"]) if user.get("role") != "admin" else None
    monitor_symbols = _monitor_symbols_for_user(user)
    signal_ideas = db.latest_signal_ideas(
        120,
        user_id=user_id,
        market_region=region,
        symbols=monitor_symbols or None,
    )
    base_kwargs = {
        "market_day_regime": _latest_market_day_regime(region),
        "pre_catalyst": db.get_state("pre_catalyst_discovery", {}),
        "tomorrow_plan": db.latest_tomorrow_plan(region),
        "opportunity_scan": db.get_state("opportunity_scan", {}),
        "market_action_radar": db.get_state("market_action_radar", {}),
        "signal_ideas": signal_ideas,
    }
    plan = build_rally_plan_by_market(**base_kwargs) if region == "BOTH" else build_rally_plan(market_region=region, **base_kwargs)
    db.set_state("rally_plan", plan)
    return _filter_rally_plan_for_symbols(plan, monitor_symbols)


def _strategy_plans_for_user(user: dict[str, Any]) -> list[dict[str, Any]]:
    return _filter_strategy_plans_for_symbols(db.strategy_plans(), _monitor_symbols_for_user(user))


def _status_payload(user: dict[str, Any] | None = None) -> dict[str, Any]:
    is_admin = bool(user and user.get("role") == "admin")
    snapshot = agent.snapshot(lightweight=not is_admin)
    snapshot["tomorrow_plan"] = _tomorrow_plan_for_user(user, "BOTH") if user else db.latest_tomorrow_plan("BOTH")
    snapshot["rally_plan"] = _filter_rally_plan_for_symbols(db.get_state("rally_plan", {}), _monitor_symbols_for_user(user)) if user else db.get_state("rally_plan", {})
    trading_readiness = build_trading_readiness(db, settings, market_region=settings.market_region)
    snapshot["trading_readiness"] = trading_readiness
    snapshot["data_freshness"] = trading_readiness.get("data_freshness", {})
    snapshot["broker_sync_status"] = trading_readiness.get("broker_sync", {})
    snapshot["replay_review_latest"] = latest_replay_review(db)
    snapshot["real_money_readiness_report"] = build_33_point_report(db, settings)
    snapshot["runtime"] = {
        "market_region": settings.market_region,
        "market_data_provider": settings.market_data_provider,
        "execution_mode": settings.execution_mode,
        "llm_provider": settings.llm_provider if is_admin else "assigned",
        "llm_decision_mode": settings.llm_decision_mode,
    }
    if is_admin:
        snapshot["runtime"].update(
            {
                "llm_model": _model_name_for_settings(settings),
                "llm_thinking_enabled": settings.llm_thinking_enabled,
                "llm_reasoning_effort": settings.llm_reasoning_effort,
                "llm_rolling_context_enabled": settings.llm_rolling_context_enabled,
            }
        )
    else:
        snapshot["llm_usage"] = _public_llm_usage_summary(snapshot.get("llm_usage", {}))
    if user and not is_admin:
        user_id = int(user["id"])
        monitor_symbols = db.user_monitor_symbols(user_id)
        paper_cash_by_market = _user_paper_cash_by_market(user)
        paper_exit_manager = db.manage_user_follow_exits(user_id, cost_settings=settings)
        tracked_ideas = _followed_signal_ideas_for_user(user_id, 100, monitor_symbols=monitor_symbols)
        follow_history = _follow_history_for_user(user_id, 500, monitor_symbols=monitor_symbols)
        realized_pnl_by_market = db.user_follow_realized_pnl_by_market(user_id)
        user_positions = _user_follow_positions(tracked_ideas)
        snapshot["decisions"] = _with_detail_urls(
            _latest_decision_summaries_for_user(user_id, 80, monitor_symbols=monitor_symbols),
            "decisions",
        )
        snapshot["decisions_by_market"] = {
            "IN": _with_detail_urls(
                _latest_decision_summaries_for_user(user_id, 80, market_region="IN", monitor_symbols=monitor_symbols),
                "decisions",
            ),
            "US": _with_detail_urls(
                _latest_decision_summaries_for_user(user_id, 80, market_region="US", monitor_symbols=monitor_symbols),
                "decisions",
            ),
        }
        snapshot["suggestions"] = _latest_signal_ideas_for_user(user_id, 50, monitor_symbols=monitor_symbols)
        snapshot["signal_ideas"] = snapshot["suggestions"]
        snapshot["suggestions_by_market"] = {
            "IN": _latest_signal_ideas_for_user(user_id, 30, market_region="IN", monitor_symbols=monitor_symbols),
            "US": _latest_signal_ideas_for_user(user_id, 30, market_region="US", monitor_symbols=monitor_symbols),
        }
        snapshot["monitor_watchlist"] = _monitor_watchlist_for_user(user_id, monitor_symbols=monitor_symbols)
        snapshot["monitor_watchlist_by_market"] = {
            "IN": _monitor_watchlist_for_user(user_id, market_region="IN", monitor_symbols=monitor_symbols),
            "US": _monitor_watchlist_for_user(user_id, market_region="US", monitor_symbols=monitor_symbols),
        }
        snapshot["tracked_ideas"] = tracked_ideas
        snapshot["tracked_ideas_by_market"] = {
            "IN": _followed_signal_ideas_for_user(user_id, 100, market_region="IN", monitor_symbols=monitor_symbols),
            "US": _followed_signal_ideas_for_user(user_id, 100, market_region="US", monitor_symbols=monitor_symbols),
        }
        snapshot["follow_history"] = follow_history
        snapshot["follow_history_by_market"] = _rows_by_market(follow_history)
        broker_orders: list[dict[str, Any]] = []
        paper_orders = _follow_history_order_events(follow_history)
        snapshot["orders"] = broker_orders
        snapshot["broker_orders"] = broker_orders
        snapshot["paper_orders"] = paper_orders
        user_portfolio = _user_follow_portfolio(
            tracked_ideas,
            snapshot.get("portfolio", {}),
            paper_cash_by_market=paper_cash_by_market,
            realized_pnl_by_market=realized_pnl_by_market,
        )
        snapshot["positions"] = user_positions
        snapshot["portfolio"] = user_portfolio
        snapshot["portfolio_by_market"] = user_portfolio.get("portfolio_by_market", {})
        snapshot["paper_cash_pool_by_market"] = paper_cash_by_market
        snapshot["paper_realized_pnl_by_market"] = realized_pnl_by_market
        snapshot["strategy_plans"] = _filter_strategy_plans_for_symbols(db.strategy_plans(), monitor_symbols)
        snapshot["performance"] = db.performance_summary(user_id=user_id)
        snapshot["paper_exit_manager"] = paper_exit_manager
        snapshot["equity_curve_by_market"] = _user_equity_curve_by_market(
            tracked_ideas,
            user_portfolio,
            paper_cash_by_market,
        )
        snapshot["equity_curve"] = _combined_equity_curve(snapshot["equity_curve_by_market"], user_portfolio)
        shared_status = user_signal_sessions.status(user_id)
        shared_status["signal_execution_mode"] = _normalize_signal_execution_mode(user.get("signal_execution_mode"))
        shared_status["signal_execution_mode_message"] = _signal_execution_mode_message(shared_status["signal_execution_mode"])
        if shared_status.get("running"):
            shared_status.update({"shared_backend": False, "message": "Your personal signal cycle is running."})
        else:
            shared_status.update(
                {
                    "running": bool(snapshot.get("running")),
                    "phase": snapshot.get("cycle", {}).get("phase", "shared_backend"),
                    "last_cycle_at": snapshot.get("last_cycle_at"),
                    "last_error": snapshot.get("last_error"),
                    "shared_backend": True,
                    "message": "Signals come from the shared backend engine. Use Run Now to start your own credit-budgeted scan.",
                    "auto_trade": snapshot.get("shared_auto_trade") or shared_status.get("auto_trade") or {},
                }
            )
        snapshot["user_signal_session"] = _sanitize_private_llm_metadata(shared_status)
    else:
        snapshot["suggestions"] = db.latest_signal_ideas(50)
        snapshot["signal_ideas"] = snapshot["suggestions"]
        snapshot["suggestions_by_market"] = {
            "IN": db.latest_signal_ideas(30, market_region="IN"),
            "US": db.latest_signal_ideas(30, market_region="US"),
        }
        snapshot["tracked_ideas"] = []
        snapshot["tracked_ideas_by_market"] = {"IN": [], "US": []}
        snapshot["strategy_plans"] = db.strategy_plans()
        snapshot["user_signal_sessions"] = user_signal_sessions.admin_summary()
    return _compact_dashboard_payload(snapshot)


def _follow_history_order_events(follow_history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for row in follow_history:
        symbol = row.get("symbol")
        market = row.get("market_region")
        mode = str(row.get("mode_label") or row.get("mode") or "Paper").strip()
        mode_code = str(row.get("mode") or "").strip().upper()
        status = str(row.get("status") or row.get("state") or "").upper()
        simulated = mode_code != "LIVE"
        entry_qty = int(row.get("entry_qty") or row.get("qty") or 0)
        entry_price = float(row.get("entry_price") or 0.0)
        if symbol and entry_qty > 0 and entry_price > 0:
            entry_status = "LIVE_REQUESTED" if mode_code == "LIVE" and status == "LIVE_REQUESTED" else "PAPER_OPENED"
            events.append(
                {
                    "id": f"follow-{row.get('follow_id')}-entry",
                    "record_type": "paper_follow_event" if simulated else "live_follow_request",
                    "execution_source": "user_idea_follows",
                    "is_broker_order": False,
                    "is_paper": simulated,
                    "ts": row.get("opened_at") or row.get("updated_at"),
                    "symbol": symbol,
                    "side": "BUY",
                    "strategy": row.get("strategy") or "paper_follow",
                    "qty": entry_qty,
                    "price": entry_price,
                    "notional": round(entry_qty * entry_price, 2),
                    "status": entry_status,
                    "status_label": "PAPER ENTRY" if simulated else "LIVE REQUEST",
                    "reason": f"{'Simulated paper' if simulated else mode} follow opened from signal idea",
                    "market_region": market,
                    "exchange": row.get("exchange"),
                    "product": "PAPER FOLLOW" if simulated else "LIVE FOLLOW REQUEST",
                    "order_type": "SIMULATED" if simulated else "REQUEST",
                    "details": row,
                }
            )
        closed_qty = int(row.get("closed_qty") or 0)
        exit_price = float(row.get("exit_price") or 0.0)
        if symbol and closed_qty > 0 and exit_price > 0:
            exit_action = str(row.get("exit_action") or "").upper()
            partial_reduce = exit_action == "REDUCE" or (row.get("state") == "OPEN" and closed_qty < entry_qty)
            side = "REDUCE" if partial_reduce else "SELL"
            exit_status = (
                "REQUESTED"
                if status == "LIVE_EXIT_REQUESTED"
                else "PARTIAL"
                if partial_reduce
                else "EXITED"
                if row.get("state") == "CLOSED"
                else status or "EXIT_PENDING"
            )
            events.append(
                {
                    "id": f"follow-{row.get('follow_id')}-exit",
                    "record_type": "paper_follow_event" if simulated else "live_follow_request",
                    "execution_source": "user_idea_follows",
                    "is_broker_order": False,
                    "is_paper": simulated,
                    "ts": row.get("closed_at") or row.get("updated_at"),
                    "symbol": symbol,
                    "side": side,
                    "strategy": row.get("strategy") or "paper_follow",
                    "qty": closed_qty,
                    "price": exit_price,
                    "notional": round(closed_qty * exit_price, 2),
                    "status": "PAPER_REDUCED" if simulated and partial_reduce else "PAPER_EXITED" if simulated else exit_status,
                    "status_label": "PAPER REDUCE" if simulated and partial_reduce else "PAPER EXIT" if simulated else "LIVE EXIT REQUEST",
                    "reason": row.get("exit_reason") or f"{'Simulated paper' if simulated else mode} follow exit",
                    "market_region": market,
                    "exchange": row.get("exchange"),
                    "product": "PAPER FOLLOW" if simulated else "LIVE FOLLOW REQUEST",
                    "order_type": "SIMULATED" if simulated else "REQUEST",
                    "details": row,
                }
            )
    return sorted(events, key=lambda item: str(item.get("ts") or ""), reverse=True)[:500]


def _with_detail_urls(rows: list[dict[str, Any]], collection: str) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["detail_url"] = f"/api/{collection}/{item.get('id')}"
        output.append(item)
    return output


def _position_target_at(targets: list[Any], index: int) -> dict[str, Any]:
    if index >= len(targets):
        return {}
    target = targets[index]
    if not isinstance(target, dict):
        return {}
    return {
        "label": target.get("label") or f"T{index + 1}",
        "price": target.get("price"),
        "hit": bool(target.get("hit")),
        "basis": target.get("basis"),
        "probability_label": target.get("probability_label"),
        "suggested_exit_pct": target.get("suggested_exit_pct"),
        "distance_pct": target.get("distance_pct"),
    }


def _follow_position_exit_plan(item: dict[str, Any]) -> dict[str, Any]:
    target_status = item.get("target_status") if isinstance(item.get("target_status"), list) else []
    targets = target_status or (item.get("targets") if isinstance(item.get("targets"), list) else [])
    details = item.get("details") if isinstance(item.get("details"), dict) else {}
    timeline = item.get("timeline") if isinstance(item.get("timeline"), dict) else {}
    return {
        "horizon": timeline.get("label") or timeline.get("name") or "swing_3_to_7_days",
        "entry_zone": item.get("entry_zone") or details.get("entry_zone"),
        "stop_loss": item.get("stop_loss") or details.get("stop_loss"),
        "target_1": _position_target_at(targets, 0),
        "target_2": _position_target_at(targets, 1),
        "target_3": _position_target_at(targets, 2),
        "invalidation": details.get("invalidation") or {},
        "monitoring_checklist": details.get("monitoring_checklist") or [],
        "plan": "Use the stop as the hard invalidation. Book partial profit at T1, reduce again near T2, and trail or close near T3 unless risk asks for an earlier exit.",
    }


_APP_LOCAL_TZ = timezone(timedelta(hours=5, minutes=30))


def _position_opened_today(raw: Any) -> bool:
    if not raw:
        return False
    try:
        opened_at = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    if opened_at.tzinfo is None:
        opened_at = opened_at.replace(tzinfo=timezone.utc)
    return opened_at.astimezone(_APP_LOCAL_TZ).date() == datetime.now(_APP_LOCAL_TZ).date()


def _user_follow_positions(tracked_ideas: list[dict[str, Any]]) -> list[dict[str, Any]]:
    positions: list[dict[str, Any]] = []
    for item in tracked_ideas:
        mode = str(item.get("mode") or "").upper()
        qty = int(item.get("qty") or 0)
        if mode not in {"PAPER", "LIVE"} or qty <= 0:
            continue
        entry_price = float(item.get("follow_entry_price") or item.get("entry_price") or 0.0)
        latest_price = float(item.get("follow_latest_price") or item.get("latest_price") or entry_price)
        previous_close = _float_or_none(item.get("previous_close"))
        unrealized_pnl = round((latest_price - entry_price) * qty, 2)
        stock_day_change_pct = round(((latest_price - previous_close) / previous_close) * 100.0, 4) if previous_close and previous_close > 0 else None
        opened_today = _position_opened_today(item.get("followed_at"))
        if opened_today:
            today_pnl = unrealized_pnl
            today_pnl_pct = round(((latest_price - entry_price) / entry_price) * 100.0, 4) if entry_price > 0 else None
            today_pnl_source = "entry_today"
        else:
            today_pnl = round((latest_price - previous_close) * qty, 2) if previous_close and previous_close > 0 else None
            today_pnl_pct = stock_day_change_pct
            today_pnl_source = "previous_close" if today_pnl is not None else "unavailable"
        exit_management = item.get("follow_details", {}).get("exit_management", {}) if isinstance(item.get("follow_details"), dict) else {}
        mark_state = item.get("follow_details", {}).get("mark_state", {}) if isinstance(item.get("follow_details"), dict) else {}
        managed_action = str(exit_management.get("last_action_label") or "").strip()
        managed_reason = str(exit_management.get("last_reason") or "").strip()
        exit_plan = _follow_position_exit_plan(item)
        target_status = item.get("target_status") if isinstance(item.get("target_status"), list) else []
        targets = item.get("targets") if isinstance(item.get("targets"), list) else []
        position_summary = {
            "symbol": item.get("symbol"),
            "classification": "PAPER" if mode == "PAPER" else "LIVE_REQUEST",
            "overall_score_pct": item.get("overall_score_pct", 0),
            "overall_grade": item.get("overall_grade", "-"),
            "entry_grade": item.get("details", {}).get("full_spectrum", {}).get("entry_quality", {}).get("entry_grade", "-"),
            "mtf_grade": item.get("details", {}).get("full_spectrum", {}).get("trend_context", {}).get("timeframe_alignment", {}).get("alignment_grade", "-"),
            "delivery_bias": item.get("details", {}).get("full_spectrum", {}).get("delivery_accumulation", {}).get("net_bias", "-"),
            "active_flags": item.get("risk_flags", []),
            "recommended_action": managed_action or "TRACK",
            "reason": managed_reason or "User paper-tracked idea; live engine will keep marking P&L against latest price.",
            "price_label": "LTP",
            "price_source": item.get("quote_source") or "signal_idea_follow",
            "quote_timestamp": item.get("quote_updated_at"),
            "price_timestamp": item.get("follow_updated_at"),
            "mark_timestamp": mark_state.get("last_mark_at") or item.get("follow_updated_at"),
        }
        positions.append(
            {
                "symbol": item.get("symbol"),
                "company_name": item.get("company_name"),
                "market_region": item.get("market_region") or "IN",
                "exchange": item.get("exchange"),
                "sector": item.get("sector"),
                "industry": item.get("industry"),
                "mode": mode,
                "mode_label": "Paper" if mode == "PAPER" else "Live request",
                "qty": qty,
                "avg_price": entry_price,
                "market_price": latest_price,
                "previous_close": previous_close,
                "previous_close_at": item.get("previous_close_at"),
                "today_pnl": today_pnl,
                "day_pnl": today_pnl,
                "day_change_pct": stock_day_change_pct,
                "today_pnl_pct": today_pnl_pct,
                "position_day_change_pct": today_pnl_pct,
                "today_pnl_source": today_pnl_source,
                "day_pnl_source": today_pnl_source,
                "entry_zone": item.get("entry_zone"),
                "stop_loss": item.get("stop_loss"),
                "stop_status": item.get("stop_status"),
                "targets": targets,
                "target_status": target_status,
                "highest_target_hit": item.get("highest_target_hit", "NONE"),
                "lifecycle_status": item.get("lifecycle_status"),
                "return_pct": item.get("return_pct", 0),
                "execution_state": item.get("execution_state"),
                "execution_state_label": item.get("execution_state_label"),
                "realized_pnl": 0.0,
                "unrealized_pnl": unrealized_pnl,
                "opened_at": item.get("followed_at"),
                "updated_at": item.get("follow_updated_at"),
                "marked_at": mark_state.get("last_mark_at") or item.get("follow_updated_at"),
                "quote_updated_at": item.get("quote_updated_at"),
                "quote_source": item.get("quote_source"),
                "strategy": item.get("strategy"),
                "exit_plan": exit_plan,
                "details_json": json.dumps(
                    {
                        "source": "user_idea_follow",
                        "follow_id": item.get("follow_id"),
                        "idea_id": item.get("idea_id"),
                        "mode": mode,
                        "return_pct": item.get("return_pct", 0),
                        "exit_management": exit_management,
                        "entry_zone": item.get("entry_zone"),
                        "stop_loss": item.get("stop_loss"),
                        "stop_status": item.get("stop_status"),
                        "targets": targets,
                        "target_status": target_status,
                        "highest_target_hit": item.get("highest_target_hit", "NONE"),
                        "lifecycle_status": item.get("lifecycle_status"),
                    },
                    separators=(",", ":"),
                ),
                "position_summary": position_summary,
            }
        )
    return positions


def _rows_by_market(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    output: dict[str, list[dict[str, Any]]] = {"IN": [], "US": []}
    for row in rows:
        market = normalize_market_region(row.get("market_region") or "IN", default="IN")
        output.setdefault(market, []).append(row)
    return output


def _user_follow_portfolio(
    tracked_ideas: list[dict[str, Any]],
    fallback: dict[str, Any],
    market_region: str | None = None,
    paper_cash_by_market: dict[str, Any] | None = None,
    realized_pnl_by_market: dict[str, Any] | None = None,
) -> dict[str, Any]:
    region = normalize_market_region(market_region or "BOTH", default="BOTH")
    if region == "BOTH":
        by_market = {
            "IN": _user_follow_portfolio(
                tracked_ideas,
                fallback,
                "IN",
                paper_cash_by_market=paper_cash_by_market,
                realized_pnl_by_market=realized_pnl_by_market,
            ),
            "US": _user_follow_portfolio(
                tracked_ideas,
                fallback,
                "US",
                paper_cash_by_market=paper_cash_by_market,
                realized_pnl_by_market=realized_pnl_by_market,
            ),
        }
        return {
            **(fallback or {}),
            "cash": round(sum(float(row["cash"]) for row in by_market.values()), 2),
            "cash_deficit": round(sum(float(row.get("cash_deficit") or 0.0) for row in by_market.values()), 2),
            "invested": round(sum(float(row["invested"]) for row in by_market.values()), 2),
            "market_value": round(sum(float(row["market_value"]) for row in by_market.values()), 2),
            "equity": round(sum(float(row["equity"]) for row in by_market.values()), 2),
            "realized_pnl": round(sum(float(row.get("realized_pnl") or 0.0) for row in by_market.values()), 2),
            "unrealized_pnl": round(sum(float(row["unrealized_pnl"]) for row in by_market.values()), 2),
            "portfolio_by_market": by_market,
        }
    base_cash = _paper_base_cash_for_market(fallback, region, paper_cash_by_market=paper_cash_by_market)
    paper_items = [
        item
        for item in tracked_ideas
        if str(item.get("mode") or "").upper() == "PAPER" and int(item.get("qty") or 0) > 0
        and normalize_market_region(item.get("market_region") or "IN", default="IN") == region
    ]
    invested = sum(float(item.get("invested_amount") or 0.0) for item in paper_items)
    market_value = sum(float(item.get("follow_latest_price") or item.get("latest_price") or 0.0) * int(item.get("qty") or 0) for item in paper_items)
    unrealized = sum(float(item.get("unrealized_pnl") or 0.0) for item in paper_items)
    try:
        realized_pnl = float((realized_pnl_by_market or {}).get(region) or 0.0)
    except (TypeError, ValueError):
        realized_pnl = 0.0
    raw_cash = base_cash + realized_pnl - invested
    cash = max(raw_cash, 0.0)
    return {
        "market_region": region,
        "currency": "USD" if region == "US" else "INR",
        "cash": round(cash, 2),
        "cash_deficit": round(abs(min(raw_cash, 0.0)), 2),
        "starting_cash": round(base_cash, 2),
        "invested": round(invested, 2),
        "market_value": round(market_value, 2),
        "equity": round(raw_cash + market_value, 2),
        "realized_pnl": round(realized_pnl, 2),
        "unrealized_pnl": round(unrealized, 2),
    }


def _user_equity_curve_by_market(
    tracked_ideas: list[dict[str, Any]],
    user_portfolio: dict[str, Any],
    paper_cash_by_market: dict[str, Any] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    by_market: dict[str, list[dict[str, Any]]] = {}
    portfolio_by_market = user_portfolio.get("portfolio_by_market") or {}
    for market in ("IN", "US"):
        portfolio = portfolio_by_market.get(market) or _user_follow_portfolio(
            tracked_ideas,
            user_portfolio,
            market,
            paper_cash_by_market=paper_cash_by_market,
        )
        rows = [
            item
            for item in tracked_ideas
            if str(item.get("mode") or "").upper() == "PAPER"
            and int(item.get("qty") or 0) > 0
            and normalize_market_region(item.get("market_region") or "IN", default="IN") == market
        ]
        if not rows:
            by_market[market] = []
            continue
        first_ts = min(str(item.get("followed_at") or item.get("first_seen_at") or utc_now()) for item in rows)
        last_ts = max(str(item.get("follow_updated_at") or item.get("last_seen_at") or utc_now()) for item in rows)
        base_cash = _paper_base_cash_for_market(user_portfolio, market, paper_cash_by_market=paper_cash_by_market)
        current_equity = float(portfolio.get("equity") or base_cash)
        by_market[market] = [
            {"ts": first_ts, "equity": round(base_cash, 2)},
            {"ts": last_ts, "equity": round(current_equity, 2)},
        ]
    return by_market


def _combined_equity_curve(
    equity_by_market: dict[str, list[dict[str, Any]]],
    user_portfolio: dict[str, Any],
) -> list[dict[str, Any]]:
    timestamps = [
        row.get("ts")
        for rows in (equity_by_market or {}).values()
        for row in rows
        if row.get("ts")
    ]
    if not timestamps:
        return []
    return [
        {"ts": min(timestamps), "equity": round(float(user_portfolio.get("cash", 0.0)) + float(user_portfolio.get("invested", 0.0)), 2)},
        {"ts": max(timestamps), "equity": round(float(user_portfolio.get("equity", 0.0)), 2)},
    ]


def _paper_base_cash_for_market(
    fallback: dict[str, Any] | None,
    market_region: str,
    paper_cash_by_market: dict[str, Any] | None = None,
) -> float:
    if isinstance(paper_cash_by_market, dict) and market_region in paper_cash_by_market:
        try:
            return max(float(paper_cash_by_market.get(market_region) or 0.0), 0.0)
        except (TypeError, ValueError):
            pass
    if settings.initial_cash_inr:
        return float(settings.initial_cash_inr)
    by_market = (fallback or {}).get("portfolio_by_market") or db.get_state("portfolio_by_market", {})
    if isinstance(by_market, dict):
        row = by_market.get(market_region) or {}
        try:
            invested = float(row.get("invested") or 0.0)
            cash = float(row.get("cash") or 0.0)
            if cash > 0 or invested > 0:
                return cash + invested
        except (TypeError, ValueError, AttributeError):
            pass
    return float(settings.initial_cash_inr or 0.0)


def _user_paper_cash_by_market(user: dict[str, Any] | None) -> dict[str, float]:
    defaults = {"IN": float(settings.initial_cash_inr or 0.0), "US": float(settings.initial_cash_inr or 0.0)}
    raw = (user or {}).get("paper_cash_by_market")
    if not isinstance(raw, dict):
        return defaults
    output: dict[str, float] = {}
    for market in ("IN", "US"):
        value = raw.get(market)
        try:
            output[market] = defaults[market] if value is None else max(float(value), 0.0)
        except (TypeError, ValueError):
            output[market] = defaults[market]
    return output


@app.get("/api/decisions/{decision_id}")
async def decision_detail(decision_id: int, request: Request) -> dict[str, Any]:
    user = require_user(request, settings, db)
    row = db.decision_by_id(decision_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Decision not found")
    if user.get("role") != "admin":
        row = _sanitize_decision_row_for_user(row)
    return row


@app.get("/api/orders/{order_id}")
async def order_detail(order_id: int, request: Request) -> dict[str, Any]:
    user = require_user(request, settings, db)
    row = db.order_by_id(order_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Order not found")
    if user.get("role") != "admin":
        row = _sanitize_order_row_for_user(row)
    return row


@app.post("/api/orders/{order_id}/cancel")
async def cancel_order(order_id: int, request: Request) -> dict[str, Any]:
    user = require_user(request, settings, db)
    try:
        row = db.cancel_order(order_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if user.get("role") != "admin":
        row = _sanitize_order_row_for_user(row)
    return {"ok": True, "order": row}


@app.post("/api/positions/{symbol}/exit")
async def manual_exit_position(symbol: str, request: Request, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    user = require_user(request, settings, db)
    symbol = str(symbol or "").strip().upper()
    if not symbol:
        raise HTTPException(status_code=400, detail="Symbol is required.")
    market_region = normalize_market_region((payload or {}).get("market_region") or "BOTH", default="BOTH")
    if user.get("role") != "admin":
        exited = db.exit_user_follow_position(
            int(user["id"]),
            symbol,
            None if market_region == "BOTH" else market_region,
            reason="manual_exit_from_positions",
        )
        if not exited:
            raise HTTPException(status_code=404, detail=f"No open tracked paper/live position found for {symbol}.")
        db.insert_agent_log(
            "INFO",
            "user_position",
            "manual_exit",
            f"{user.get('username')} manually exited {symbol}",
            {"user_id": user.get("id"), "symbol": symbol, "market_region": market_region, "exited_count": len(exited)},
        )
        snapshot = _status_payload(user)
        await hub.broadcast(snapshot)
        return snapshot

    positions = db.positions()
    row = next(
        (
            item
            for item in positions
            if str(item.get("symbol") or "").upper() == symbol
            and (market_region == "BOTH" or normalize_market_region(item.get("market_region") or "IN") == market_region)
        ),
        None,
    )
    if not row:
        raise HTTPException(status_code=404, detail=f"No open broker position found for {symbol}.")
    price = _latest_quote_price(symbol, row.get("market_price") or row.get("avg_price") or 0.0)
    decision = Decision(
        symbol=symbol,
        action="SELL",
        confidence=1.0,
        price=float(price),
        technical_score=0.0,
        sentiment_score=0.0,
        reason="Manual exit requested from Positions",
        asof=utc_now(),
        strategy="manual_exit",
        details_json=json.dumps(
            {
                "audit_version": 1,
                "decision_path": "manual_exit",
                "manual_exit": True,
                "requested_by": user.get("username"),
                "market_region": market_region,
            },
            separators=(",", ":"),
        ),
    )
    db.insert_decisions([decision])
    broker.execute(decision, float((db.latest_portfolio() or {}).get("equity") or settings.initial_cash_inr or 0.0))
    snapshot = _status_payload(user)
    await hub.broadcast(snapshot)
    return snapshot


def _latest_quote_price(symbol: str, fallback: Any = 0.0) -> float:
    try:
        with db.connect() as conn:
            row = conn.execute("select price from latest_quotes where symbol = ?", (symbol,)).fetchone()
        if row and row["price"] is not None:
            return float(row["price"])
    except Exception:
        pass
    try:
        return float(fallback or 0.0)
    except (TypeError, ValueError):
        return 0.0


@app.post("/api/analyze-symbol")
async def analyze_symbol(payload: dict[str, Any], request: Request) -> dict[str, Any]:
    user = _require_signal_user(request)
    return await _analyze_symbol_for_user(payload, user)


async def _analyze_symbol_for_user(payload: dict[str, Any], user: dict[str, Any]) -> dict[str, Any]:
    user_id = int(user["id"])
    force_llm = False
    estimated_charge = _estimated_signal_credit_charge()
    can_spend, credit_before = db.user_has_credit_for(user_id, estimated_charge)
    if not can_spend:
        raise HTTPException(
            status_code=402,
            detail=(
                "Insufficient credits or daily credit budget for this analysis. "
                f"Estimated need: {estimated_charge:.4f} credits."
            ),
        )
    requested_input = str(payload.get("symbol", ""))
    market_region = _payload_market_region(payload)
    symbol, row, resolution = _resolve_analysis_symbol(requested_input, market_region=market_region)
    if not symbol:
        examples = "AAPL, MSFT, or NVDA" if market_region == "US" else "SBIN, SUZLON, or INFY"
        raise HTTPException(status_code=400, detail=f"Enter a valid {market_region} symbol or company name, for example {examples}.")

    user_market_data = _market_data_provider_for_user(user, market_region)
    user_strategy, budget_policy = _strategy_for_user_budget(user, credit_before, estimated_charge)
    provider_error: str | None = None
    usage_after_id = db.latest_llm_usage_id()
    usage_scope = f"manual_analysis:{user_id}:{uuid4().hex}"
    scope_token = current_llm_usage_scope.set(usage_scope)
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
        if _provider_error_is_upstox_auth(provider_error):
            raise HTTPException(
                status_code=401,
                detail=(
                    "Upstox rejected the saved analytics token while fetching quotes. "
                    "The token is incorrect, expired, revoked, or not enabled for market data. "
                    "Save a fresh Upstox access token in Account or Admin Broker settings."
                ),
            )
        if _provider_error_is_indstocks_auth(provider_error):
            raise HTTPException(
                status_code=401,
                detail=(
                    "The legacy INDstocks feed is disabled for analytics. "
                    "Save a fresh Upstox access token in Account or Admin Broker settings."
                ),
            )
        suggestions = _symbol_suggestions(requested_input, limit=3, market_region=market_region)
        suggestion_text = ""
        if suggestions:
            suggestion_text = " Try " + ", ".join(f"{item['symbol']} ({item['name']})" for item in suggestions[:3]) + "."
        detail = f"No {market_region} market quote found for {symbol}.{suggestion_text} Check the symbol spelling and market data provider."
        if provider_error:
            detail = f"{detail} Provider error: {provider_error}"
        raise HTTPException(status_code=404, detail=detail)

    if row.get("upstox_instrument_key"):
        db.upsert_universe_rows([row], disable_missing=False)

    reference_data = await _analysis_reference_data(row, quote.to_dict(), candles.get(symbol, []), market_region)
    row_for_analysis = {**row, **reference_data.get("row_fields", {})}
    context_token = current_user_id.set(user_id)
    try:
        sentiment_settings = replace(
            _llm_settings_for_user(db.user_by_id(user_id) or user),
            enable_llm_sentiment=False,
        )
        user_sentiment = SentimentService(sentiment_settings, db)
        news = await user_sentiment.analyze_symbol_news(row_for_analysis)
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
        decisions = await asyncio.to_thread(
            lambda: asyncio.run(
                user_strategy.evaluate(
                    [row_for_analysis],
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
            )
        )
    finally:
        current_user_id.reset(context_token)
    if not decisions:
        raise HTTPException(status_code=500, detail=f"Analysis produced no decision for {symbol}.")

    manual_llm_review = await _manual_llm_review_if_requested(
        requested=force_llm,
        decision=decisions[0],
        strategy=user_strategy,
        user_id=user_id,
    )
    decisions[0] = manual_llm_review["decision"]

    usage = db.llm_usage_cost_for_scope(user_id, usage_scope, usage_after_id)
    llm_activity = _llm_activity_from_decisions(decisions, usage)
    if manual_llm_review["status"] != "not_requested":
        llm_activity["manual_review"] = manual_llm_review["public"]
        if manual_llm_review["status"] == "disabled" and not int(usage.get("calls") or 0):
            llm_activity.update(
                {
                    "status": "disabled",
                    "message": "OpenStocks View was requested, but this user has no enabled review provider/API key. No review credits were used.",
                    "billable": False,
                    "credits_charged": 0.0,
                    "raw_provider_credits": 0.0,
                }
            )
        elif manual_llm_review["status"] == "failed" and not int(usage.get("calls") or 0):
            llm_activity.update(
                {
                    "status": "manual_review_failed",
                    "message": "OpenStocks View was requested, but the review failed before a billable provider response. No review credits were used.",
                    "billable": False,
                    "credits_charged": 0.0,
                    "raw_provider_credits": 0.0,
                }
            )
    billing = _credit_billing_for_usage(usage)
    try:
        credit_after = credit_before
        if usage.get("calls") and llm_activity.get("billable"):
            credit_after = db.charge_user_credits(
                user_id,
                billing["base_credits"],
                f"Symbol analysis {symbol}",
                {
                    "symbol": symbol,
                    "llm_usage": usage,
                    "llm_activity": llm_activity,
                    "provider": quote.source,
                    "estimated_credit_before": estimated_charge,
                    "budget_policy": budget_policy,
                    "credit_billing": {
                        "tokens_per_credit": billing["tokens_per_credit"],
                        "total_tokens": billing["total_tokens"],
                        "charged_credits": billing["charged_credits"],
                    },
                    "admin_billing": {
                        "base_credits": billing["base_credits"],
                        "platform_margin_pct": billing["platform_margin_pct"],
                        "platform_margin_credits": billing["platform_margin_credits"],
                        "api_cost_usd": billing["api_cost_usd"],
                    },
                },
                margin_pct=billing["platform_margin_pct"],
                minimum_charge=0.0,
            )
    except ValueError as exc:
        raise HTTPException(
            status_code=402,
            detail=(
                "The analysis completed, but the final OpenStocks View charge exceeded the user's available credits. "
                "Add credits or raise the daily budget before running another signal."
            ),
        ) from exc
    decision = _attach_user_to_decision(decisions[0], user, credit_after, budget_policy)
    decision_payload = decision.to_dict()
    decision_payload["details"] = _json_object(decision.details_json)
    decision_payload = _sanitize_decision_payload_for_user(decision_payload)
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
            "llm_activity": llm_activity,
            "credit_charge": round(max(float(credit_before.get("credit_balance", 0.0)) - float(credit_after.get("credit_balance", 0.0)), 0.0), 6),
            "budget_policy": budget_policy,
            "usage_scope": usage_scope,
        },
    )
    current_llm_usage_scope.reset(scope_token)
    return {
        "ok": True,
        "manual_only": True,
        "message": "Analysis completed. This does not place an order; autonomous cycles still handle trading.",
        "symbol": symbol,
        "market": market_region,
        "requested_symbol": resolution.get("requested_symbol"),
        "resolved_from": resolution.get("resolved_from"),
        "quote": quote.to_dict(),
        "company_name": row_for_analysis.get("name") or reference_data.get("fundamentals", {}).get("company_name"),
        "fundamentals": reference_data.get("fundamentals", {}),
        "reference_data": {
            "source": reference_data.get("source"),
            "data_gaps": reference_data.get("data_gaps", []),
            "unavailable_fields": reference_data.get("unavailable_fields", []),
            "derived_from_candles": reference_data.get("derived_from_candles", []),
            "sources": reference_data.get("sources", []),
            "field_sources": reference_data.get("field_sources", {}),
            "reference_errors": reference_data.get("reference_errors", []),
        },
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
            "llm_activity": llm_activity,
            "budget_policy": _public_budget_policy(budget_policy),
            "llm_disabled": LLM_HARD_DISABLED,
            "llm_disabled_reason": LLM_DISABLED_REASON,
        },
        "decision": decision_payload,
    }


def _payload_bool(value: Any, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return default


async def _manual_llm_review_if_requested(
    *,
    requested: bool,
    decision: Decision,
    strategy: StrategyEngine,
    user_id: int,
) -> dict[str, Any]:
    public: dict[str, Any] = {
        "requested": bool(requested),
        "executed": False,
        "status": "not_requested",
        "prefilter_bypassed": False,
    }
    if not requested:
        return {"decision": decision, "status": "not_requested", "public": public}

    details = _json_object(decision.details_json)
    if LLM_HARD_DISABLED:
        details["manual_llm_review"] = {
            "requested": True,
            "executed": False,
            "status": "disabled",
            "reason": LLM_DISABLED_REASON,
        }
        public.update(details["manual_llm_review"])
        return {
            "decision": replace(decision, details_json=json.dumps(details, default=str, separators=(",", ":"))),
            "status": "disabled",
            "public": public,
        }

    decision_path = str(details.get("decision_path") or "")
    if details.get("llm_output") or decision_path.startswith("llm_"):
        public.update(
            {
                "executed": True,
                "status": "already_reviewed",
                "reason": "The strategy engine already used OpenStocks View for this manual analysis.",
                "decision_path": decision_path,
            }
        )
        return {"decision": decision, "status": "already_reviewed", "public": public}

    if not strategy.llm.enabled or strategy.llm.settings.llm_provider == "offline":
        details["manual_llm_review"] = {
            "requested": True,
            "executed": False,
            "status": "disabled",
            "reason": "OpenStocks View provider/API key is not enabled for this user.",
        }
        public.update(details["manual_llm_review"])
        return {
            "decision": replace(decision, details_json=json.dumps(details, default=str, separators=(",", ":"))),
            "status": "disabled",
            "public": public,
        }

    context = details.get("context") if isinstance(details.get("context"), dict) else {}
    context = dict(context or {})
    context.setdefault("symbol", decision.symbol)
    if not isinstance(context.get("quote"), dict):
        context["quote"] = {"price": decision.price}
    if not isinstance(context.get("technical_math"), dict):
        context["technical_math"] = {"score": decision.technical_score}
    if not isinstance(context.get("sentiment"), dict):
        context["sentiment"] = {"score": decision.sentiment_score}
    if not isinstance(context.get("best_strategy"), dict):
        context["best_strategy"] = {"name": decision.strategy}
    context["manual_analysis"] = {
        "force_llm": True,
        "source": "symbol_analysis",
        "original_decision_path": decision_path,
        "original_action": decision.action,
    }
    context["manual_llm_review"] = {
        "requested": True,
        "prefilter_bypassed": True,
        "reason": "Manual symbol analysis explicitly requested OpenStocks View evidence.",
    }

    context_token = current_user_id.set(user_id)
    try:
        reviewed = await strategy.llm.review(decision, context)
    except Exception as exc:
        details["manual_llm_review"] = {
            "requested": True,
            "executed": False,
            "status": "failed",
            "prefilter_bypassed": True,
            "reason": f"{exc.__class__.__name__}: {exc}"[:500],
            "original_decision_path": decision_path,
        }
        public.update(details["manual_llm_review"])
        return {
            "decision": replace(decision, details_json=json.dumps(details, default=str, separators=(",", ":"))),
            "status": "failed",
            "public": public,
        }
    finally:
        current_user_id.reset(context_token)

    reviewed_details = _json_object(reviewed.details_json)
    reviewed_details["manual_llm_review"] = {
        "requested": True,
        "executed": True,
        "status": "completed",
        "prefilter_bypassed": True,
        "original_decision_path": decision_path,
        "original_action": decision.action,
        "final_action": reviewed.action,
        "note": "Manual Analyze bypassed the scanner candidate lane for OpenStocks View evidence; system risk gates still control tradability.",
    }
    public.update(reviewed_details["manual_llm_review"])
    return {
        "decision": replace(reviewed, details_json=json.dumps(reviewed_details, default=str, separators=(",", ":"))),
        "status": "completed",
        "public": public,
    }


@app.get("/api/openclaw/health")
async def openclaw_health(request: Request) -> dict[str, Any]:
    require_openclaw_bridge(request, settings)
    user = default_openclaw_user(settings, db)
    return {
        "ok": True,
        "source": "openstocks",
        "bridge_enabled": settings.openclaw_bridge_enabled,
        "default_user": user.get("username"),
        "webhook_configured": bool(settings.openclaw_webhook_url),
        "agent_running": agent.running,
        "last_cycle_at": agent.snapshot().get("last_cycle_at"),
    }


@app.get("/api/openclaw/context")
async def openclaw_context(request: Request, market: str = "BOTH", limit: int = 8) -> dict[str, Any]:
    require_openclaw_bridge(request, settings)
    return bridge_context(db, market=market, limit=limit)


@app.post("/api/openclaw/select-stocks")
async def openclaw_select_stocks(payload: dict[str, Any], request: Request) -> dict[str, Any]:
    require_openclaw_bridge(request, settings)
    market = str(payload.get("market") or "BOTH")
    limit = int(payload.get("limit") or 5)
    result = select_stock_candidates(db, market=market, limit=limit)
    db.insert_agent_log(
        "INFO",
        "openclaw",
        "select_stocks",
        "OpenClaw requested stock candidates",
        {"market": result.get("market"), "candidate_count": len(result.get("candidates", []))},
    )
    return result


@app.get("/api/openclaw/raw/breakout-scan")
async def openclaw_breakout_scan(request: Request, market: str = "BOTH", limit: int = 10) -> dict[str, Any]:
    require_openclaw_bridge(request, settings)
    result = breakout_scan(db, market=market, limit=limit)
    db.insert_agent_log(
        "INFO",
        "openclaw",
        "breakout_scan",
        "OpenClaw requested volume and breakout candidate scan",
        {
            "market": result.get("market"),
            "scanned": result.get("scanned"),
            "candidate_count": len(result.get("candidates", [])),
            "best_symbol": (result.get("best_candidate") or {}).get("symbol"),
        },
    )
    return result


@app.post("/api/openclaw/analyze")
async def openclaw_analyze_symbol(payload: dict[str, Any], request: Request) -> dict[str, Any]:
    require_openclaw_bridge(request, settings)
    user = default_openclaw_user(settings, db)
    result = await _analyze_symbol_for_user(payload, user)
    result["requested_by"] = "openclaw"
    result["openclaw_default_user"] = user.get("username")
    return result


@app.post("/api/openclaw/run-cycle")
async def openclaw_run_cycle(request: Request) -> dict[str, Any]:
    require_openclaw_bridge(request, settings)
    db.insert_agent_log("INFO", "openclaw", "run_cycle", "OpenClaw requested one agent cycle")
    return await agent.run_once()


@app.post("/api/openclaw/notify-test")
async def openclaw_notify_test(request: Request) -> dict[str, Any]:
    require_openclaw_bridge(request, settings)
    return await openclaw_notifier.send_test()


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
            "estimated_signal_credit": _estimated_signal_credit_charge(),
            "low_budget_mode": "The market scan ranks eligible symbols first; credits apply to the reviewed shortlist.",
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


@app.post("/api/me/signal-execution-mode")
async def set_my_signal_execution_mode(payload: dict[str, Any], request: Request) -> dict[str, Any]:
    user = _require_signal_user(request)
    mode = _normalize_signal_execution_mode(payload.get("signal_execution_mode") or payload.get("mode"))
    updated_user = db.update_user_signal_execution_mode(int(user["id"]), mode)
    if not updated_user:
        raise HTTPException(status_code=404, detail="User not found")
    db.insert_agent_log(
        "INFO",
        "user_session",
        "signal_execution_mode_updated",
        f"{user['username']} updated signal execution mode to {mode}",
        {"user_id": user["id"], "signal_execution_mode": mode},
    )
    return {
        "ok": True,
        "signal_execution_mode": mode,
        "user": updated_user,
        "message": _signal_execution_mode_message(mode),
    }


@app.post("/api/me/monitor-symbols")
async def set_my_monitor_symbols(payload: dict[str, Any], request: Request) -> dict[str, Any]:
    user = _require_signal_user(request)
    raw_symbols = payload.get("symbols", payload.get("monitor_symbols", payload.get("watchlist", "")))
    requested = db.normalize_monitor_symbols(raw_symbols)
    accepted, invalid = _validated_monitor_symbols(requested)
    if requested and not accepted:
        raise HTTPException(status_code=400, detail=f"No valid enabled symbols found: {', '.join(invalid[:12])}")
    updated_user = db.update_user_monitor_symbols(int(user["id"]), accepted)
    if not updated_user:
        raise HTTPException(status_code=404, detail="User not found")
    db.insert_agent_log(
        "INFO",
        "user_session",
        "user_monitor_symbols_updated",
        f"{user['username']} updated monitored symbols",
        {
            "user_id": user["id"],
            "monitor_symbols_count": len(accepted),
            "invalid_symbols": invalid[:20],
            "monitor_scope": "CUSTOM" if accepted else "DYNAMIC_OPPORTUNITY",
        },
    )
    return {
        "ok": True,
        "user": updated_user,
        "monitor_symbols": accepted,
        "monitor_symbols_count": len(accepted),
        "monitor_scope": "CUSTOM" if accepted else "DYNAMIC_OPPORTUNITY",
        "invalid_symbols": invalid,
        "message": (
            f"Monitoring {len(accepted)} custom symbol(s)."
            if accepted
            else "Custom symbol list cleared. Dynamic opportunity scan is active."
        ),
    }


@app.post("/api/me/paper-cash")
async def set_my_paper_cash(payload: dict[str, Any], request: Request) -> dict[str, Any]:
    user = _require_signal_user(request)
    cash_payload = payload.get("cash_by_market") if isinstance(payload.get("cash_by_market"), dict) else {}
    has_india = any(key in payload for key in ("india_cash", "cash_in", "IN")) or "IN" in cash_payload
    has_us = any(key in payload for key in ("us_cash", "cash_us", "US")) or "US" in cash_payload
    if not has_india and not has_us:
        raise HTTPException(status_code=400, detail="Enter India Cash or US Cash to update.")

    cash_in = None
    cash_us = None
    if has_india:
        cash_in = _positive_float(
            payload.get("india_cash", payload.get("cash_in", payload.get("IN", cash_payload.get("IN")))),
            field="India cash",
        )
    if has_us:
        cash_us = _positive_float(
            payload.get("us_cash", payload.get("cash_us", payload.get("US", cash_payload.get("US")))),
            field="US cash",
        )
    updated_user = db.update_user_paper_cash(int(user["id"]), cash_in=cash_in, cash_us=cash_us)
    if not updated_user:
        raise HTTPException(status_code=404, detail="User not found")

    paper_cash_by_market = _user_paper_cash_by_market(updated_user)
    tracked_ideas = db.user_followed_signal_ideas(int(user["id"]), 100)
    follow_history = _follow_history_for_user(int(user["id"]), 500, monitor_symbols=monitor_symbols)
    realized_pnl_by_market = db.user_follow_realized_pnl_by_market(int(user["id"]))
    account_payload = await account.snapshot()
    user_portfolio = _user_follow_portfolio(
        tracked_ideas,
        account_payload["paper"].get("portfolio", {}),
        paper_cash_by_market=paper_cash_by_market,
        realized_pnl_by_market=realized_pnl_by_market,
    )
    db.insert_agent_log(
        "INFO",
        "account",
        "user_paper_cash_updated",
        f"{user['username']} updated paper cash",
        {"user_id": user["id"], "cash_by_market": paper_cash_by_market},
    )
    return {
        "ok": True,
        "paper_cash_by_market": paper_cash_by_market,
        "paper": {
            "cash": user_portfolio["cash"],
            "cash_pool_by_market": paper_cash_by_market,
            "realized_pnl_by_market": realized_pnl_by_market,
            "cash_by_market": {
                market: row.get("cash", 0.0)
                for market, row in (user_portfolio.get("portfolio_by_market") or {}).items()
            },
            "portfolio": user_portfolio,
            "portfolio_by_market": user_portfolio.get("portfolio_by_market", {}),
            "positions": _user_follow_positions(tracked_ideas),
            "follow_history": follow_history,
            "closed_positions": [row for row in follow_history if str(row.get("state") or "").upper() != "OPEN"],
        },
    }


@app.post("/api/me/indstocks/connect")
async def my_indstocks_connect(payload: dict[str, Any], request: Request) -> dict[str, Any]:
    user = _require_signal_user(request)
    raise HTTPException(status_code=410, detail="INDstocks analytics is disabled. Use Upstox or Kite in Account.")
    token = normalize_indstocks_access_token(payload.get("access_token") or payload.get("token") or "")
    base_url = str(payload.get("base_url") or settings.indstocks_api_base_url).rstrip("/")
    if not token:
        raise HTTPException(status_code=400, detail="Paste your INDstocks access token first.")
    await _validate_indstocks_access_token(token, base_url)
    updated_user = db.update_user_broker(
        int(user["id"]),
        {
            "indstocks_access_token": token,
            "indstocks_api_base_url": base_url,
        },
    )
    db.insert_agent_log(
        "INFO",
        "indstocks",
        "user_indstocks_token_saved",
        f"INDstocks token saved for user {user['username']}",
        {"user_id": user["id"], "username": user["username"], "base_url": base_url},
    )
    return {
        "ok": True,
        "message": "INDstocks token saved for this user. Symbol analysis will use this user's market feed.",
        "user": updated_user,
        "token_type": "access_token",
    }


async def _validate_indstocks_access_token(token: str, base_url: str) -> None:
    headers = {
        "Accept": "application/json,text/csv,*/*",
        "Authorization": normalize_indstocks_access_token(token),
    }
    try:
        async with httpx.AsyncClient(timeout=12, headers=headers, follow_redirects=True) as client:
            response = await client.get(f"{base_url.rstrip('/')}/market/quotes/full", params={"scrip-codes": "NSE_2885"})
            response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in {401, 403}:
            raise HTTPException(
                status_code=401,
                detail=(
                    "INDstocks rejected this token. It is incorrect, expired, revoked, or not enabled for market data. "
                    "Generate a fresh INDstocks access token and save it again."
                ),
            ) from exc
        raise HTTPException(
            status_code=502,
            detail=f"INDstocks token check failed with HTTP {exc.response.status_code}: {exc.response.text[:160]}",
        ) from exc
    except httpx.TimeoutException as exc:
        raise HTTPException(status_code=504, detail="INDstocks token check timed out. Try again in a moment.") from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"INDstocks token check failed: {exc.__class__.__name__}: {exc}") from exc


async def _validate_upstox_access_token(token: str, base_url: str) -> None:
    token = normalize_upstox_access_token(token)
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
    }
    try:
        async with httpx.AsyncClient(timeout=12, headers=headers, follow_redirects=True) as client:
            response = await client.get(
                f"{base_url.rstrip('/')}/market-quote/quotes",
                params={"instrument_key": "NSE_EQ|INE002A01018"},
            )
            response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in {401, 403}:
            raise HTTPException(
                status_code=401,
                detail=(
                    "Upstox rejected this token. It is incorrect, expired, revoked, or not enabled for market data. "
                    "Generate a fresh access token and save it again."
                ),
            ) from exc
        raise HTTPException(
            status_code=exc.response.status_code,
            detail=f"Upstox token check failed with HTTP {exc.response.status_code}: {exc.response.text[:160]}",
        ) from exc
    except httpx.TimeoutException as exc:
        raise HTTPException(status_code=504, detail="Upstox token check timed out. Try again in a moment.") from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Upstox token check failed: {exc.__class__.__name__}: {exc}") from exc


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
        "message": "Open this URL, login to Upstox, then paste the returned code or full redirect URL into OpenStocks.",
    }


@app.post("/api/me/upstox/connect")
async def my_upstox_connect(payload: dict[str, Any], request: Request) -> dict[str, Any]:
    user = _require_signal_user(request)
    stored = db.user_by_id(int(user["id"])) or {}
    direct_token = normalize_upstox_access_token(payload.get("access_token") or payload.get("token") or "")
    base_url = str(payload.get("base_url") or stored.get("upstox_api_base_url") or settings.upstox_api_base_url).rstrip("/")
    if direct_token:
        await _validate_upstox_access_token(direct_token, base_url)
        updated_user = db.update_user_broker(
            int(user["id"]),
            {
                "upstox_access_token": direct_token,
                "upstox_api_base_url": base_url,
                "upstox_token_scope": "user",
            },
        )
        db.insert_agent_log(
            "INFO",
            "upstox",
            "user_upstox_token_saved",
            f"Upstox analytics token saved for user {user['username']}",
            {"user_id": user["id"], "username": user["username"], "base_url": base_url},
        )
        return {
            "ok": True,
            "message": "Upstox token saved for this user. Symbol analysis will use this user's market feed.",
            "user": updated_user,
            "token_type": "analytics_token",
        }
    api_key = str(payload.get("api_key") or stored.get("upstox_api_key") or "").strip()
    api_secret = str(payload.get("api_secret") or stored.get("upstox_api_secret") or "").strip()
    redirect_uri = str(payload.get("redirect_uri") or stored.get("upstox_redirect_uri") or "").strip()
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
            "upstox_token_scope": "user",
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
            "kite_token_scope": "user",
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
        "message": "Kite credentials saved. This user can use Kite as a personal analytics override and live-trading broker guard.",
        "user": updated_user,
    }


@app.get("/api/trading-readiness")
async def trading_readiness_status(request: Request) -> dict[str, Any]:
    require_user(request, settings, db)
    return {"ok": True, "trading_readiness": build_trading_readiness(db, settings, market_region=settings.market_region)}


@app.get("/api/data-freshness")
async def data_freshness_status(request: Request) -> dict[str, Any]:
    require_user(request, settings, db)
    market_region = normalize_market_region(request.query_params.get("market") or settings.market_region or "BOTH", default="BOTH")
    symbols = [
        item.strip().upper()
        for item in str(request.query_params.get("symbols") or "").split(",")
        if item.strip()
    ]
    return {
        "ok": True,
        "data_freshness": build_data_freshness_report(db, settings, market_region=market_region, symbols=symbols or None),
    }


@app.get("/api/broker-sync/status")
async def broker_sync_status(request: Request) -> dict[str, Any]:
    user = require_user(request, settings, db)
    if user.get("role") != "admin":
        try:
            account_payload = await account.snapshot(user)
            broker_payload = account_payload.get("broker_sync") or {}
            db.set_state("broker_sync_status", broker_payload)
            return {"ok": True, "broker_sync": broker_payload}
        except Exception as exc:
            fallback = build_broker_sync_status(db, settings)
            fallback["status"] = "SYNC_ERROR"
            fallback["reason"] = _exception_message(exc)
            return {"ok": False, "broker_sync": fallback}
    return {"ok": True, "broker_sync": build_broker_sync_status(db, settings)}


@app.get("/api/replay-review/latest")
async def replay_review_latest(request: Request) -> dict[str, Any]:
    require_user(request, settings, db)
    return {"ok": True, "replay_review": latest_replay_review(db)}


@app.post("/api/admin/emergency-kill-switch")
async def admin_emergency_kill_switch(payload: dict[str, Any], request: Request) -> dict[str, Any]:
    user = require_admin(request, settings, db)
    switch = set_trading_kill_switch(
        db,
        engaged=bool(payload.get("engaged", True)),
        reason=str(payload.get("reason") or ""),
        updated_by=str(user.get("username") or "admin"),
    )
    db.insert_agent_log("WARN", "admin", "emergency_kill_switch", "Trading kill switch updated", switch)
    return {"ok": True, "kill_switch": switch, "trading_readiness": build_trading_readiness(db, settings)}


@app.post("/api/admin/broker-reconcile/dry-run")
async def admin_broker_reconcile_dry_run(request: Request) -> dict[str, Any]:
    require_admin(request, settings, db)
    status = build_broker_sync_status(db, settings)
    db.set_state("broker_sync_status", {**status, "dry_run_at": utc_now(), "status": status.get("status") or "DRY_RUN"})
    return {"ok": True, "mode": "dry_run", "broker_sync": status}


@app.post("/api/admin/broker-reconcile/apply")
async def admin_broker_reconcile_apply(request: Request) -> dict[str, Any]:
    require_admin(request, settings, db)
    gate = live_order_gate(db, settings, market_region="IN")
    status = build_broker_sync_status(db, settings)
    if not gate.get("passed"):
        return {
            "ok": False,
            "mode": "apply_blocked",
            "reason": "live_readiness_not_passed",
            "blocking_reasons": gate.get("blocking_reasons", []),
            "broker_sync": status,
        }
    db.set_state("broker_sync_status", {**status, "applied_at": utc_now(), "status": "APPLY_NOOP_NO_BROKER_DIFF"})
    return {"ok": True, "mode": "apply", "broker_sync": status}


@app.post("/api/admin/replay-validation/run")
async def admin_replay_validation_run(request: Request, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    require_admin(request, settings, db)
    symbols = payload.get("symbols") if isinstance(payload, dict) else None
    if isinstance(symbols, str):
        symbols = [item.strip().upper() for item in symbols.split(",") if item.strip()]
    review = run_replay_validation(db, symbols if isinstance(symbols, list) else None)
    return {"ok": True, "replay_review": review}


@app.get("/api/account")
async def account_details(request: Request) -> dict[str, Any]:
    user = require_user(request, settings, db)
    payload = await account.snapshot(user)
    if user.get("role") != "admin":
        paper_cash_by_market = _user_paper_cash_by_market(user)
        tracked_ideas = db.user_followed_signal_ideas(int(user["id"]), 100)
        follow_history = db.user_follow_history(int(user["id"]), 500)
        realized_pnl_by_market = db.user_follow_realized_pnl_by_market(int(user["id"]))
        user_portfolio = _user_follow_portfolio(
            tracked_ideas,
            payload["paper"].get("portfolio", {}),
            paper_cash_by_market=paper_cash_by_market,
            realized_pnl_by_market=realized_pnl_by_market,
        )
        payload["tracked_ideas"] = tracked_ideas
        payload["follow_history"] = follow_history
        payload["follow_history_by_market"] = _rows_by_market(follow_history)
        payload["paper"]["positions"] = _user_follow_positions(tracked_ideas)
        payload["paper"]["follow_history"] = follow_history
        payload["paper"]["closed_positions"] = [row for row in follow_history if str(row.get("state") or "").upper() != "OPEN"]
        payload["paper"]["portfolio"] = user_portfolio
        payload["paper"]["portfolio_by_market"] = user_portfolio.get("portfolio_by_market", {})
        payload["paper"]["cash_pool_by_market"] = paper_cash_by_market
        payload["paper"]["realized_pnl_by_market"] = realized_pnl_by_market
        payload["paper"]["cash_by_market"] = {
            market: row.get("cash", 0.0)
            for market, row in (user_portfolio.get("portfolio_by_market") or {}).items()
        }
        payload["paper"]["cash"] = user_portfolio["cash"]
        payload["signal_execution_mode"] = _normalize_signal_execution_mode(user.get("signal_execution_mode"))
        payload["signal_execution_mode_message"] = _signal_execution_mode_message(payload["signal_execution_mode"])
        monitor_symbols = db.user_monitor_symbols(int(user["id"]))
        payload["monitor_symbols"] = monitor_symbols
        payload["monitor_symbols_count"] = len(monitor_symbols)
        payload["monitor_scope"] = "CUSTOM" if monitor_symbols else "DYNAMIC_OPPORTUNITY"
    return payload


@app.get("/api/performance")
async def performance_summary(request: Request) -> dict[str, Any]:
    user = require_user(request, settings, db)
    return db.performance_summary(user_id=int(user["id"]) if user.get("role") != "admin" else None)


@app.get("/api/config")
async def get_config(request: Request) -> dict[str, Any]:
    user = require_user(request, settings, db)
    return _config_payload() if user.get("role") == "admin" else _public_config_payload()


def _config_payload() -> dict[str, Any]:
    return {"schema": CONFIG_SCHEMA, "settings": public_settings(settings)}


def _public_config_payload() -> dict[str, Any]:
    return {
        "schema": [],
        "settings": {
            "agent_interval_seconds": settings.agent_interval_seconds,
            "cycle_timeout_seconds": settings.cycle_timeout_seconds,
            "llm_timeout_seconds": settings.llm_timeout_seconds,
            "credit_tokens_per_credit": settings.credit_tokens_per_credit,
            "market_region": settings.market_region,
        },
    }


@app.get("/api/logs")
async def agent_logs(request: Request, limit: int = 300) -> dict[str, Any]:
    require_admin(request, settings, db)
    safe_limit = max(1, min(int(limit), 1000))
    return {"logs": db.latest_agent_logs(safe_limit)}


@app.get("/api/market-breadth")
async def market_breadth_snapshot(request: Request) -> dict[str, Any]:
    require_user(request, settings, db)
    return db.get_state("market_breadth_context", {})


@app.get("/api/opportunity-scan")
async def opportunity_scan_snapshot(request: Request) -> dict[str, Any]:
    require_user(request, settings, db)
    return db.get_state("opportunity_scan", {})


@app.get("/api/decision-diagnostics")
async def decision_diagnostics_snapshot(request: Request) -> dict[str, Any]:
    require_user(request, settings, db)
    return db.get_state("decision_diagnostics", {})


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
            "market_region": settings.market_region,
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
    if LLM_HARD_DISABLED:
        return {
            "ok": False,
            "provider": "offline",
            "model": "offline",
            "reason": LLM_DISABLED_REASON,
        }
    return await llm.test_connection()


@app.post("/api/alpaca/connect")
async def alpaca_connect(payload: dict[str, Any], request: Request) -> dict[str, Any]:
    require_admin(request, settings, db)
    candidate_overrides, changed_keys = _alpaca_candidate_overrides(payload)
    candidate_settings = _settings_without_llm(settings_from_overrides(Settings(), candidate_overrides))
    if not candidate_settings.alpaca_api_key or not candidate_settings.alpaca_api_secret:
        raise HTTPException(status_code=400, detail="Paste the Alpaca API key and secret first.")

    symbol = _alpaca_test_symbol(payload)
    provider = AlpacaMarketDataProvider(candidate_settings)
    try:
        quotes = await provider.get_quotes([{"symbol": symbol, "exchange": "NASDAQ"}])
    except Exception as exc:
        raise HTTPException(status_code=400, detail=_alpaca_connection_error(exc, candidate_settings.alpaca_data_feed)) from exc
    quote = quotes.get(symbol)
    if not quote:
        raise HTTPException(status_code=502, detail=f"Alpaca connected, but no quote was returned for {symbol}.")

    try:
        candidate_stack = build_agent_stack(candidate_settings)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Alpaca credentials are valid but provider failed to initialize: {exc}") from exc

    result = await _apply_runtime_stack(
        candidate_overrides=candidate_overrides,
        candidate_settings=candidate_settings,
        candidate_stack=candidate_stack,
        changed_keys=changed_keys,
        component="alpaca",
        event="connected",
        message="Alpaca US market data connected and saved",
    )
    db.insert_agent_log(
        "INFO",
        "alpaca",
        "connected",
        "Alpaca US market data credentials validated and saved",
        {
            "base_url": candidate_settings.alpaca_data_base_url,
            "feed": candidate_settings.alpaca_data_feed,
            "provider": candidate_settings.us_market_data_provider,
            "test_symbol": symbol,
            "market_region": candidate_settings.market_region,
        },
    )
    return {
        "ok": True,
        "message": "Alpaca connected. US market data now uses the configured Alpaca feed.",
        "provider": result["status"].get("provider"),
        "us_market_data_provider": candidate_settings.us_market_data_provider,
        "feed": candidate_settings.alpaca_data_feed,
        "base_url": candidate_settings.alpaca_data_base_url,
        "test_quote": {
            "symbol": quote.symbol,
            "price": quote.price,
            "source": quote.source,
            "asof": quote.asof,
        },
        "status": result["status"],
        "config": result["config"],
    }


def _alpaca_candidate_overrides(payload: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    incoming = payload.get("settings", payload)
    if not isinstance(incoming, dict):
        incoming = {}
    connect_keys = {
        "market_region",
        "us_market_data_provider",
        "us_intraday_candle_lookback_days",
        "us_daily_candle_lookback_days",
        "alpaca_api_key",
        "alpaca_api_secret",
        "alpaca_data_base_url",
        "alpaca_data_feed",
    }
    current_overrides = db.runtime_settings()
    candidate_overrides = dict(current_overrides)
    changed: set[str] = set()
    for key in connect_keys:
        if key not in incoming or key not in CONFIG_KEYS:
            continue
        value = incoming[key]
        if key in SECRET_FIELDS and value == "":
            continue
        candidate_overrides[key] = value
        changed.add(key)

    provider = str(candidate_overrides.get("us_market_data_provider") or settings.us_market_data_provider or "").lower()
    if provider not in {"alpaca", "alpaca_yahoo"}:
        candidate_overrides["us_market_data_provider"] = "alpaca_yahoo"
        changed.add("us_market_data_provider")
    return _runtime_overrides_without_llm(candidate_overrides), sorted(changed)


def _alpaca_test_symbol(payload: dict[str, Any]) -> str:
    symbol = str(payload.get("symbol") or "AAPL").strip().upper()
    symbol = re.sub(r"[^A-Z0-9.\-]", "", symbol)
    return symbol or "AAPL"


def _alpaca_connection_error(exc: Exception, feed: str) -> str:
    text = str(exc).strip()
    lowered = text.lower()
    if ("403" in lowered or "subscription" in lowered or "not entitled" in lowered) and str(feed).lower() == "sip":
        return "Alpaca SIP feed is not enabled for this account. Change Alpaca Feed to iex, or enable a SIP market-data subscription in Alpaca."
    if "401" in lowered or "unauthorized" in lowered or "forbidden" in lowered:
        return "Alpaca rejected the key or secret. Regenerate the Paper Trading API key/secret in Alpaca and paste both values."
    if "429" in lowered:
        return "Alpaca rate limit was hit. Wait a minute and test again."
    return f"Alpaca connection failed: {text or exc.__class__.__name__}"


@app.get("/api/llm/usage")
async def llm_usage(request: Request) -> dict[str, Any]:
    user = require_user(request, settings, db)
    summary = db.llm_usage_summary(100)
    return summary if user.get("role") == "admin" else _public_llm_usage_summary(summary)


@app.post("/api/indstocks/connect")
async def indstocks_connect(payload: dict[str, Any], request: Request) -> dict[str, Any]:
    require_admin(request, settings, db)
    raise HTTPException(status_code=410, detail="INDstocks analytics is disabled. Connect the shared Upstox analytics feed instead.")
    token = normalize_indstocks_access_token(payload.get("access_token") or payload.get("token") or "")
    base_url = str(payload.get("base_url") or settings.indstocks_api_base_url).rstrip("/")
    if not token:
        raise HTTPException(status_code=400, detail="Paste your INDstocks access token first.")
    await _validate_indstocks_access_token(token, base_url)

    current_overrides = db.runtime_settings()
    candidate_overrides = dict(current_overrides)
    candidate_overrides.update(
        {
            "market_data_provider": "indstocks",
            "indstocks_access_token": token,
            "indstocks_api_base_url": base_url,
        }
    )
    candidate_settings = settings_from_overrides(Settings(), candidate_overrides)
    try:
        candidate_stack = build_agent_stack(candidate_settings)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"INDstocks token saved but provider failed to initialize: {exc}") from exc
    result = await _apply_runtime_stack(
        candidate_overrides=candidate_overrides,
        candidate_settings=candidate_settings,
        candidate_stack=candidate_stack,
        changed_keys=["market_data_provider", "indstocks_access_token", "indstocks_api_base_url"],
        component="indstocks",
        event="connected",
        message="INDstocks access token connected and saved",
    )
    db.insert_agent_log(
        "INFO",
        "indstocks",
        "access_token_saved",
        "INDstocks access token saved for market data",
        {"base_url": base_url},
    )
    return {
        "ok": True,
        "message": "INDstocks connected. Token saved and market data provider rebuilt.",
        "provider": result["status"].get("provider"),
        "status": result["status"],
        "config": result["config"],
    }


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
        "message": "Open this URL, login to Upstox, then paste the returned code or full redirect URL into OpenStocks.",
    }


@app.post("/api/upstox/connect")
async def upstox_connect(payload: dict[str, Any], request: Request) -> dict[str, Any]:
    require_admin(request, settings, db)
    direct_token = normalize_upstox_access_token(payload.get("access_token") or payload.get("token") or "")
    base_url = str(payload.get("base_url") or settings.upstox_api_base_url).rstrip("/")
    if direct_token:
        await _validate_upstox_access_token(direct_token, base_url)
        current_overrides = db.runtime_settings()
        candidate_overrides = dict(current_overrides)
        candidate_overrides.update(
            {
                "market_data_provider": "upstox",
                "upstox_access_token": direct_token,
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
            changed_keys=["market_data_provider", "upstox_access_token", "upstox_api_base_url"],
            component="upstox",
            event="connected",
            message="Upstox analytics access token connected and saved",
        )
        db.insert_agent_log(
            "INFO",
            "upstox",
            "access_token_saved",
            "Upstox analytics access token saved for shared market data",
            {"base_url": base_url, "token_type": "direct_access_token"},
        )
        return {
            "ok": True,
            "message": "Upstox connected. Shared analytics feed now uses this token.",
            "provider": result["status"].get("provider"),
            "token_type": "direct_access_token",
            "status": result["status"],
            "config": result["config"],
        }
    api_key = str(payload.get("api_key") or settings.upstox_api_key).strip()
    api_secret = str(payload.get("api_secret") or settings.upstox_api_secret).strip()
    redirect_uri = str(payload.get("redirect_uri") or settings.upstox_redirect_uri).strip()
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
        f"<h1>Upstox authorization code received</h1><p>Paste this code into OpenStocks Upstox Connect.</p>"
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
    candidate_overrides = _runtime_overrides_without_llm(candidate_overrides)

    candidate_settings = _settings_without_llm(settings_from_overrides(Settings(), candidate_overrides))
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
    global settings, market_data, order_router, broker, account, sentiment, macro, institutional_feeds, delivery_service, market_breadth, sector_rotation, macro_calendar, universe_service, options_intelligence, openclaw_notifier, llm, strategy, agent
    candidate_overrides = _runtime_overrides_without_llm(candidate_overrides)
    candidate_settings = _settings_without_llm(candidate_settings)
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
    openclaw_notifier = candidate_stack["openclaw_notifier"]
    llm = candidate_stack["llm"]
    strategy = candidate_stack["strategy"]
    agent = candidate_stack["agent"]
    await universe_service.refresh_if_enabled()
    if settings.us_universe_csv.exists():
        db.seed_universe(settings.us_universe_csv, disable_missing=False)
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
    assigned_provider, assigned_model = _assigned_llm_from_payload(payload)
    signal_execution_mode = _normalize_signal_execution_mode(payload.get("signal_execution_mode"))
    active = bool(payload.get("active", True))
    starting_credits = _positive_float(payload.get("starting_credits", payload.get("credit_balance", 0)), field="starting_credits")
    daily_credit_limit = _positive_float(payload.get("daily_credit_limit", 0), field="daily_credit_limit")
    if db.user_by_username(username):
        raise HTTPException(status_code=409, detail="Username already exists")
    user = db.create_user(
        username,
        hash_password(password),
        role=role,
        active=active,
        assigned_llm_provider=assigned_provider,
        assigned_llm_model=assigned_model,
        signal_execution_mode=signal_execution_mode,
    )
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
            "assigned_llm_provider": assigned_provider,
            "assigned_llm_model": assigned_model,
            "signal_execution_mode": signal_execution_mode,
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
    assigned_provider = assigned_model = None
    if "assigned_llm_provider" in payload or "assigned_llm_model" in payload:
        assigned_provider, assigned_model = _assigned_llm_from_payload(
            {
                "assigned_llm_provider": payload.get("assigned_llm_provider", existing.get("assigned_llm_provider", "")),
                "assigned_llm_model": payload.get("assigned_llm_model", existing.get("assigned_llm_model", "")),
            }
        )
    active = bool(payload["active"]) if "active" in payload else None
    password_hash = hash_password(validate_password(str(payload["password"]))) if payload.get("password") else None
    daily_credit_limit = _positive_float(payload["daily_credit_limit"], field="daily_credit_limit") if "daily_credit_limit" in payload else None
    signal_execution_mode = (
        _normalize_signal_execution_mode(payload.get("signal_execution_mode"))
        if "signal_execution_mode" in payload
        else None
    )
    if existing.get("role") == "admin" and db.active_admin_count() <= 1:
        would_remove_admin = (role is not None and role != "admin") or active is False
        if would_remove_admin:
            raise HTTPException(status_code=400, detail="At least one active admin user is required.")
    user = db.update_user(
        user_id,
        role=role,
        assigned_llm_provider=assigned_provider,
        assigned_llm_model=assigned_model,
        signal_execution_mode=signal_execution_mode,
        active=active,
        password_hash=password_hash,
    )
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
            "llm_assignment_changed": assigned_provider is not None or assigned_model is not None,
            "signal_execution_mode_changed": signal_execution_mode is not None,
            "active_changed": active is not None,
            "password_changed": password_hash is not None,
            "daily_credit_limit_changed": daily_credit_limit is not None,
        },
    )
    return {"ok": True, "user": user, "users": db.list_users()}


@app.get("/api/admin/credits")
async def admin_credit_summary(request: Request) -> dict[str, Any]:
    require_admin(request, settings, db)
    summary = db.admin_credit_usage_summary()
    summary["credit_policy"] = {
        "tokens_per_credit": settings.credit_tokens_per_credit,
        "platform_margin_pct": settings.credit_platform_margin_pct,
        "user_rule": f"{settings.credit_tokens_per_credit} LLM tokens = 1 credit",
    }
    return summary


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


@app.post("/api/users/{user_id}/assign-runtime-indstocks")
async def assign_runtime_indstocks(user_id: int, request: Request) -> dict[str, Any]:
    admin = require_admin(request, settings, db)
    raise HTTPException(status_code=410, detail="INDstocks assignment is disabled. Shared Upstox analytics is routed automatically.")
    existing = db.user_by_id(user_id)
    if not existing:
        raise HTTPException(status_code=404, detail="User not found")
    runtime_settings = db.runtime_settings()
    runtime_indstocks = {
        "indstocks_access_token": (runtime_settings.get("indstocks_access_token") or settings.indstocks_access_token),
        "indstocks_api_base_url": (runtime_settings.get("indstocks_api_base_url") or settings.indstocks_api_base_url),
    }
    if not runtime_indstocks["indstocks_access_token"]:
        raise HTTPException(status_code=400, detail="No runtime INDstocks access token is available to assign.")
    updated_user = db.assign_runtime_indstocks_to_user(user_id, runtime_indstocks)
    db.insert_agent_log(
        "INFO",
        "indstocks",
        "runtime_indstocks_assigned",
        f"Admin assigned runtime INDstocks credentials to {existing.get('username')}",
        {"updated_by": admin.get("username"), "user_id": user_id, "username": existing.get("username")},
    )
    return {"ok": True, "user": updated_user, "users": db.list_users()}


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
        "upstox_token_scope": "shared_analytics",
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


@app.get("/api/tomorrow-plan")
async def tomorrow_plan(request: Request, response: Response) -> dict[str, Any]:
    user = require_user(request, settings, db)
    response.headers["Cache-Control"] = "no-store, max-age=0"
    market_region = normalize_market_region(request.query_params.get("market") or "BOTH", default="BOTH")
    plan = _tomorrow_plan_for_user(user, market_region)
    return {"ok": True, "market": market_region, "tomorrow_plan": plan}


@app.get("/api/rally-plan")
async def rally_plan(request: Request, response: Response) -> dict[str, Any]:
    user = require_user(request, settings, db)
    response.headers["Cache-Control"] = "no-store, max-age=0"
    market_region = normalize_market_region(request.query_params.get("market") or "BOTH", default="BOTH")
    plan = _rally_plan_for_user(user, market_region)
    return {"ok": True, "market": market_region, "rally_plan": _compact_rally_plan(plan)}


@app.get("/api/ideas")
async def ideas(request: Request) -> dict[str, Any]:
    user = require_user(request, settings, db)
    user_id = int(user["id"]) if user.get("role") != "admin" else None
    market_region = normalize_market_region(request.query_params.get("market") or "BOTH", default="BOTH")
    monitor_symbols = db.user_monitor_symbols(user_id) if user_id is not None else []
    tracked_ideas = _followed_signal_ideas_for_user(
        user_id,
        100,
        market_region=market_region,
        monitor_symbols=monitor_symbols,
    ) if user_id is not None else []
    ideas_rows = db.latest_signal_ideas(
        50,
        user_id=user_id,
        market_region=market_region,
        symbols=monitor_symbols or None,
    )
    ideas_by_market = {
        "IN": db.latest_signal_ideas(30, user_id=user_id, market_region="IN", symbols=monitor_symbols or None),
        "US": db.latest_signal_ideas(30, user_id=user_id, market_region="US", symbols=monitor_symbols or None),
    }
    tracked_by_market = {
        "IN": _followed_signal_ideas_for_user(user_id, 100, market_region="IN", monitor_symbols=monitor_symbols) if user_id is not None else [],
        "US": _followed_signal_ideas_for_user(user_id, 100, market_region="US", monitor_symbols=monitor_symbols) if user_id is not None else [],
    }
    return {
        "ok": True,
        "market": market_region,
        "ideas": _compact_signal_ideas(ideas_rows),
        "ideas_by_market": {market: _compact_signal_ideas(rows) for market, rows in ideas_by_market.items()},
        "monitor_watchlist": db.monitor_watchlist_rows(
            monitor_symbols,
            user_id=user_id,
            market_region=market_region,
        ) if user_id is not None else [],
        "monitor_watchlist_by_market": {
            "IN": db.monitor_watchlist_rows(monitor_symbols, user_id=user_id, market_region="IN") if user_id is not None else [],
            "US": db.monitor_watchlist_rows(monitor_symbols, user_id=user_id, market_region="US") if user_id is not None else [],
        },
        "tracked_ideas": [_compact_tracked_idea(row) for row in tracked_ideas],
        "tracked_ideas_by_market": {
            market: [_compact_tracked_idea(row) for row in rows]
            for market, rows in tracked_by_market.items()
        },
        "positions": _user_follow_positions(tracked_ideas),
        "strategy_plans": _filter_strategy_plans_for_symbols(db.strategy_plans(), monitor_symbols),
        "shared_backend": {
            "running": agent.running,
            "last_cycle_at": getattr(agent, "_last_cycle_at", None),
            "admin_controls_engine": True,
        },
    }


@app.post("/api/ideas/{idea_id}/follow")
async def follow_idea(idea_id: int, payload: dict[str, Any], request: Request) -> dict[str, Any]:
    user = _require_signal_user(request)
    mode = str(payload.get("mode") or "TRACK")
    normalized_mode = mode.strip().upper()
    amount = _positive_float(payload.get("amount", 0), field="amount")
    qty = int(_positive_float(payload.get("qty", 0), field="qty"))
    manual_override = bool(payload.get("manual_override") or payload.get("manual_confirmed"))
    idea = _signal_idea_for_user_guard(idea_id, user)
    if normalized_mode == "PAPER" and amount <= 0 and qty <= 0:
        amount = _default_paper_follow_amount(user, idea)
    if normalized_mode == "LIVE":
        _require_user_live_broker(user, normalize_market_region(idea.get("market_region") or "IN", default="IN"))
    try:
        follow = db.follow_signal_idea(
            int(user["id"]),
            idea_id,
            mode=mode,
            amount=amount,
            qty=qty,
            cost_settings=settings,
            manual_override=manual_override,
        )
    except ValueError as exc:
        status_code = 404 if "not found" in str(exc).lower() else 400
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc
    exit_manager = db.manage_user_follow_exits(int(user["id"]), cost_settings=settings)
    db.insert_agent_log(
        "INFO",
        "ideas",
        "idea_followed",
        f"{user.get('username')} followed idea #{idea_id}",
        {"user_id": user.get("id"), "idea_id": idea_id, "mode": mode, "amount": amount, "qty": qty},
    )
    monitor_symbols = db.user_monitor_symbols(int(user["id"]))
    tracked_ideas = _followed_signal_ideas_for_user(int(user["id"]), 100, monitor_symbols=monitor_symbols)
    follow_history = _follow_history_for_user(int(user["id"]), 500, monitor_symbols=monitor_symbols)
    paper_orders = _follow_history_order_events(follow_history)
    paper_cash_by_market = _user_paper_cash_by_market(user)
    realized_pnl_by_market = db.user_follow_realized_pnl_by_market(int(user["id"]))
    user_portfolio = _user_follow_portfolio(
        tracked_ideas,
        db.latest_portfolio() or {},
        paper_cash_by_market=paper_cash_by_market,
        realized_pnl_by_market=realized_pnl_by_market,
    )
    return {
        "ok": True,
        "follow": follow,
        "paper_exit_manager": exit_manager,
        "ideas": _latest_signal_ideas_for_user(int(user["id"]), 50, monitor_symbols=monitor_symbols),
        "tracked_ideas": tracked_ideas,
        "follow_history": follow_history,
        "follow_history_by_market": _rows_by_market(follow_history),
        "orders": [],
        "broker_orders": [],
        "paper_orders": paper_orders,
        "tracked_ideas_by_market": {
            "IN": _followed_signal_ideas_for_user(int(user["id"]), 100, market_region="IN", monitor_symbols=monitor_symbols),
            "US": _followed_signal_ideas_for_user(int(user["id"]), 100, market_region="US", monitor_symbols=monitor_symbols),
        },
        "positions": _user_follow_positions(tracked_ideas),
        "portfolio": user_portfolio,
        "portfolio_by_market": user_portfolio.get("portfolio_by_market", {}),
        "paper": {
            "positions": _user_follow_positions(tracked_ideas),
            "follow_history": follow_history,
            "closed_positions": [row for row in follow_history if str(row.get("state") or "").upper() != "OPEN"],
            "portfolio": user_portfolio,
            "portfolio_by_market": user_portfolio.get("portfolio_by_market", {}),
            "cash_pool_by_market": paper_cash_by_market,
            "realized_pnl_by_market": realized_pnl_by_market,
        },
    }


@app.post("/api/plans/{plan_code}/follow")
async def follow_strategy_plan(plan_code: str, payload: dict[str, Any], request: Request) -> dict[str, Any]:
    user = _require_signal_user(request)
    mode = str(payload.get("mode") or "TRACK").strip().upper()
    if mode not in {"TRACK", "PAPER", "LIVE"}:
        mode = "TRACK"
    amount = _positive_float(payload.get("amount", 0), field="amount")
    market_region = normalize_market_region(payload.get("market") or "BOTH", default="BOTH")
    max_symbols = int(_positive_float(payload.get("max_symbols", 5), field="max_symbols") or 5)
    max_symbols = max(1, min(max_symbols, 10))
    plans = _strategy_plans_for_user(user)
    plan = next((item for item in plans if str(item.get("code") or "") == plan_code), None)
    if plan is None:
        raise HTTPException(status_code=404, detail="Strategy plan not found")
    ideas = [
        idea
        for idea in plan.get("constituents", [])
        if normalize_market_region(idea.get("market_region") or "IN", default="IN") == market_region or market_region == "BOTH"
    ][:max_symbols]
    if not ideas:
        raise HTTPException(status_code=400, detail="No active ideas are available under this plan for the selected market")
    per_idea_amount = float(amount) / len(ideas) if amount > 0 else 0.0
    followed: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for idea in ideas:
        try:
            if mode == "LIVE":
                _require_user_live_broker(user, normalize_market_region(idea.get("market_region") or "IN", default="IN"))
            followed.append(
                db.follow_signal_idea(
                    int(user["id"]),
                    int(idea["id"]),
                    mode=mode,
                    amount=per_idea_amount,
                    cost_settings=settings,
                )
            )
        except ValueError as exc:
            skipped.append({"id": idea.get("id"), "symbol": idea.get("symbol"), "reason": str(exc)})
    db.insert_agent_log(
        "INFO",
        "ideas",
        "plan_followed",
        f"{user.get('username')} followed plan {plan_code}",
        {
            "user_id": user.get("id"),
            "plan_code": plan_code,
            "mode": mode,
            "amount": amount,
            "followed": len(followed),
            "skipped": skipped,
        },
    )
    monitor_symbols = db.user_monitor_symbols(int(user["id"]))
    tracked_ideas = _followed_signal_ideas_for_user(int(user["id"]), 100, monitor_symbols=monitor_symbols)
    follow_history = db.user_follow_history(int(user["id"]), 500)
    paper_cash_by_market = _user_paper_cash_by_market(user)
    realized_pnl_by_market = db.user_follow_realized_pnl_by_market(int(user["id"]))
    user_portfolio = _user_follow_portfolio(
        tracked_ideas,
        db.latest_portfolio() or {},
        paper_cash_by_market=paper_cash_by_market,
        realized_pnl_by_market=realized_pnl_by_market,
    )
    return {
        "ok": True,
        "plan": plan,
        "followed": followed,
        "skipped": skipped,
        "ideas": _latest_signal_ideas_for_user(int(user["id"]), 50, monitor_symbols=monitor_symbols),
        "tracked_ideas": tracked_ideas,
        "follow_history": follow_history,
        "follow_history_by_market": _rows_by_market(follow_history),
        "positions": _user_follow_positions(tracked_ideas),
        "portfolio": user_portfolio,
        "portfolio_by_market": user_portfolio.get("portfolio_by_market", {}),
        "paper": {
            "positions": _user_follow_positions(tracked_ideas),
            "follow_history": follow_history,
            "closed_positions": [row for row in follow_history if str(row.get("state") or "").upper() != "OPEN"],
            "portfolio": user_portfolio,
            "portfolio_by_market": user_portfolio.get("portfolio_by_market", {}),
            "cash_pool_by_market": paper_cash_by_market,
            "realized_pnl_by_market": realized_pnl_by_market,
        },
        "strategy_plans": _strategy_plans_for_user(user),
    }


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


def _payload_market_region(payload: dict[str, Any]) -> str:
    return normalize_market_region(payload.get("market") or payload.get("market_region") or "IN")


def _normalize_symbol(value: str, market_region: str = "IN") -> str:
    symbol = value.strip().upper()
    region = normalize_market_region(market_region)
    for prefix in ("NSE:", "BSE:", "NASDAQ:", "NYSE:", "AMEX:", "US:"):
        symbol = symbol.removeprefix(prefix)
    if region == "IN":
        symbol = symbol.removesuffix(".NS").removesuffix(".BO")
    elif region == "US":
        symbol = symbol.removesuffix(".US").replace(".", "-")
    if not re.fullmatch(r"[A-Z0-9&-]{1,24}", symbol):
        return ""
    return symbol


def _resolve_analysis_symbol(value: str, market_region: str = "IN") -> tuple[str, dict[str, Any], dict[str, Any]]:
    region = normalize_market_region(market_region)
    requested = _normalize_symbol(value, region)
    search_text = str(value or "").strip()
    if requested:
        aliased = COMMON_SYMBOL_ALIASES.get(requested, requested) if region == "IN" else requested
        row = db.universe_row(aliased, market_region=region)
        if row:
            return aliased, row, {"requested_symbol": requested, "resolved_from": "alias" if aliased != requested else "symbol"}
        suggestions = _symbol_suggestions(requested, limit=1, market_region=region)
        if suggestions:
            symbol = str(suggestions[0]["symbol"])
            row = db.universe_row(symbol, market_region=region)
            if row:
                return symbol, row, {"requested_symbol": requested, "resolved_from": "universe_search"}
        return requested, _manual_universe_row(requested, region), {"requested_symbol": requested, "resolved_from": "manual"}

    suggestions = _symbol_suggestions(search_text, limit=1, market_region=region)
    if suggestions:
        symbol = str(suggestions[0]["symbol"])
        row = db.universe_row(symbol, market_region=region)
        if row:
            return symbol, row, {"requested_symbol": search_text, "resolved_from": "company_search"}
    return "", {}, {"requested_symbol": search_text, "resolved_from": "invalid"}


def _symbol_suggestions(value: str, limit: int = 5, market_region: str = "IN") -> list[dict[str, Any]]:
    region = normalize_market_region(market_region)
    raw = str(value or "").strip()
    normalized = _normalize_symbol(raw, region)
    alias = COMMON_SYMBOL_ALIASES.get(normalized or _compact_search_text(raw)) if region == "IN" else None
    candidates: list[dict[str, Any]] = []
    if alias:
        row = db.universe_row(alias, market_region=region)
        if row:
            candidates.append({"symbol": row.get("symbol"), "name": row.get("name"), "sector": row.get("sector"), "exchange": row.get("exchange")})
    compact = _compact_search_text(raw)
    if not compact and not normalized:
        return candidates[:limit]
    like = f"%{raw.upper()}%" if raw else "%"
    compact_like = f"%{compact}%"
    region_clause = ""
    if region == "IN":
        region_clause = "and upper(exchange) in ('NSE','BSE')"
    elif region == "US":
        region_clause = "and upper(exchange) not in ('NSE','BSE')"
    try:
        with db.connect() as conn:
            rows = conn.execute(
                f"""
                select symbol, name, sector, exchange
                from universe
                where enabled = 1
                  {region_clause}
                  and (
                    upper(symbol) like ?
                    or upper(name) like ?
                    or replace(replace(replace(upper(name), ' ', ''), '.', ''), '&', '') like ?
                  )
                order by
                  case
                    when upper(symbol) = ? then 0
                    when replace(replace(replace(upper(name), ' ', ''), '.', ''), '&', '') = ? then 1
                    when upper(symbol) like ? then 2
                    else 3
                  end,
                  symbol
                limit ?
                """,
                (like, like, compact_like, normalized, compact, f"{normalized}%", limit * 2),
            ).fetchall()
    except Exception:
        rows = []
    seen = {str(item.get("symbol")) for item in candidates}
    for row in rows:
        symbol = str(row["symbol"])
        if symbol in seen:
            continue
        candidates.append({"symbol": symbol, "name": row["name"], "sector": row["sector"], "exchange": row["exchange"]})
        seen.add(symbol)
        if len(candidates) >= limit:
            break
    return candidates


def _compact_search_text(value: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "", str(value or "").upper())


def _signal_idea_for_user_guard(idea_id: int, user: dict[str, Any]) -> dict[str, Any]:
    user_id = int(user["id"])
    monitor_symbols = db.user_monitor_symbols(user_id)
    for idea in db.latest_signal_ideas(500, user_id=user_id, symbols=monitor_symbols or None):
        if int(idea.get("id") or 0) == int(idea_id):
            return idea
    if monitor_symbols:
        raise HTTPException(status_code=404, detail="Signal idea is outside this user's monitor list.")
    raise HTTPException(status_code=404, detail="Signal idea not found.")


def _default_paper_follow_amount(user: dict[str, Any], idea: dict[str, Any]) -> float:
    market = normalize_market_region(idea.get("market_region") or "IN", default="IN")
    details = idea.get("details") if isinstance(idea.get("details"), dict) else {}
    quality_gate = fresh_buy_quality_gate(
        {
            "action": details.get("action") or idea.get("signal_type"),
            "signal_type": idea.get("signal_type"),
            "status": idea.get("status"),
            "overall_score_pct": idea.get("overall_score_pct"),
            "overall_grade": idea.get("overall_grade"),
            "confluence": idea.get("confluence"),
            "data_readiness": details.get("data_readiness"),
            "hard_blocked": details.get("hard_blocked"),
            "details": details,
        }
    )
    size_multiplier = quality_size_multiplier(quality_gate) if quality_gate.get("passed") else 1.0
    tracked = db.user_followed_signal_ideas(int(user["id"]), 200)
    portfolio = _user_follow_portfolio(
        tracked,
        db.latest_portfolio() or {},
        paper_cash_by_market=_user_paper_cash_by_market(user),
        realized_pnl_by_market=db.user_follow_realized_pnl_by_market(int(user["id"])),
    )
    market_portfolio = (portfolio.get("portfolio_by_market") or {}).get(market) or {}
    cash = float(market_portfolio.get("cash") or 0.0)
    price = float(idea.get("latest_price") or idea.get("price") or idea.get("entry_price") or 0.0)
    return _auto_follow_amount(cash, price, size_multiplier=size_multiplier, market_region=market)


def _require_user_live_broker(user: dict[str, Any], market_region: str) -> None:
    region = normalize_market_region(market_region or "IN", default="IN")
    gate = live_order_gate(db, settings, market_region=region)
    if not gate.get("passed"):
        reasons = gate.get("blocking_reasons") or ["real-money readiness has not passed"]
        raise HTTPException(
            status_code=400,
            detail=f"Live trading is disabled by readiness gates: {', '.join(str(item) for item in reasons[:4])}. Use Track or Paper.",
        )
    if region == "US":
        raise HTTPException(
            status_code=400,
            detail="Live US trading is not enabled yet. Use Track or Paper for US ideas until a supported US broker is connected.",
        )
    stored = db.user_by_id(int(user["id"])) or {}
    has_upstox = bool(stored.get("upstox_access_token") and stored.get("upstox_token_scope") == "user")
    has_kite = bool(
        stored.get("kite_api_key")
        and stored.get("kite_access_token")
        and stored.get("kite_token_scope") == "user"
    )
    if has_upstox or has_kite:
        return
    raise HTTPException(
        status_code=400,
        detail=(
            "Live trading requires your own broker connection. Save your personal Upstox token or Kite API key/access token "
            "in Account before using Live."
        ),
    )


def _provider_error_is_indstocks_auth(error: str | None) -> bool:
    text = str(error or "").lower()
    return "indstocks" in text and (
        "403" in text
        or "401" in text
        or "access_token" in text
        or "expired" in text
        or "revoked" in text
    )


def _provider_error_is_upstox_auth(error: str | None) -> bool:
    text = str(error or "").lower()
    return "upstox" in text and (
        "403" in text
        or "401" in text
        or "access_token" in text
        or "expired" in text
        or "revoked" in text
        or "unauthorized" in text
    )


def _require_signal_user(request: Request) -> dict[str, Any]:
    user = require_user(request, settings, db)
    if user.get("role") == "admin":
        raise HTTPException(
            status_code=403,
            detail="Admin manages users, credits, and broker connections only. Use a user account for signals and trading.",
        )
    return user


def _validated_monitor_symbols(symbols: list[str]) -> tuple[list[str], list[str]]:
    requested = db.normalize_monitor_symbols(symbols)
    if not requested:
        return [], []
    rows = db.get_universe(enabled_only=True, market_region="BOTH")
    known = {str(row.get("symbol") or "").upper() for row in rows}
    accepted = [symbol for symbol in requested if symbol in known]
    invalid = [symbol for symbol in requested if symbol not in known]
    return accepted, invalid


def _market_data_provider_for_user(user: dict[str, Any], market_region: str = "IN"):
    region = normalize_market_region(market_region)
    if region == "US":
        return build_market_data_provider(replace(settings, market_region="US", market_data_provider="yahoo"))
    stored = db.user_by_id(int(user["id"])) or {}
    if stored.get("upstox_access_token") and stored.get("upstox_token_scope") == "user":
        return build_market_data_provider(
            replace(
                settings,
                market_region=region,
                market_data_provider="upstox",
                upstox_access_token=str(stored.get("upstox_access_token") or ""),
                upstox_api_base_url=str(stored.get("upstox_api_base_url") or settings.upstox_api_base_url).rstrip("/"),
            )
        )
    if stored.get("kite_api_key") and stored.get("kite_access_token") and stored.get("kite_token_scope") == "user":
        return build_market_data_provider(
            replace(
                settings,
                market_region=region,
                market_data_provider="kite_yahoo",
                kite_api_key=str(stored.get("kite_api_key") or ""),
                kite_access_token=str(stored.get("kite_access_token") or ""),
            )
        )
    if settings.upstox_access_token:
        provider = settings.market_data_provider if str(settings.market_data_provider).startswith("upstox") else "upstox"
        return build_market_data_provider(replace(settings, market_region=region, market_data_provider=provider))
    if settings.kite_api_key and settings.kite_access_token:
        provider = settings.market_data_provider if str(settings.market_data_provider).startswith("kite") else "kite_yahoo"
        return build_market_data_provider(replace(settings, market_region=region, market_data_provider=provider))
    if settings.nubra_session_token and settings.nubra_device_id:
        return build_market_data_provider(replace(settings, market_region=region, market_data_provider="nubra"))
    if settings.market_data_provider == "yahoo":
        return build_market_data_provider(replace(settings, market_region=region, market_data_provider="yahoo"))
    raise HTTPException(
        status_code=400,
        detail="No Upstox analytics token is connected. Paste a user Upstox/Kite token in Account, or ask admin to connect the shared Upstox analytics feed.",
    )


def _assigned_llm_from_payload(payload: dict[str, Any]) -> tuple[str, str]:
    return _policy_assigned_llm_from_payload(payload, settings)


def _strategy_for_user_budget(
    user: dict[str, Any],
    credit_summary: dict[str, Any],
    estimated_charge: float,
) -> tuple[StrategyEngine, dict[str, Any]]:
    stored_user = db.user_by_id(int(user["id"])) or user
    active_settings = _llm_settings_for_user(stored_user)
    daily_remaining = float(credit_summary.get("daily_credits_remaining") or 0.0)
    balance = float(credit_summary.get("credit_balance") or 0.0)
    available = min(daily_remaining, balance)
    threshold = max(float(estimated_charge or 0.01) * 3.0, 0.03)
    policy = {
        "mode": "full_context",
        "provider": active_settings.llm_provider,
        "model": _model_name_for_settings(active_settings),
        "daily_credits_remaining": round(daily_remaining, 6),
        "estimated_signal_credit": round(float(estimated_charge or 0.0), 6),
        "tokens_per_credit": settings.credit_tokens_per_credit,
    }
    if active_settings.llm_provider == "deepseek" and active_settings.deepseek_model != "deepseek-v4-flash" and 0 < available <= threshold:
        budget_settings = replace(
            active_settings,
            enable_llm_sentiment=False,
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
        return StrategyEngine(budget_settings, SentimentService(budget_settings, db), LLMBrain(budget_settings, db)), policy
    decision_only_settings = replace(active_settings, enable_llm_sentiment=False)
    return StrategyEngine(decision_only_settings, SentimentService(decision_only_settings, db), LLMBrain(decision_only_settings, db)), policy


def _llm_settings_for_user(user: dict[str, Any]) -> Settings:
    if LLM_HARD_DISABLED:
        return _settings_without_llm(settings)
    provider = str(user.get("assigned_llm_provider") or settings.user_default_llm_provider or settings.llm_provider).strip().lower()
    model = str(user.get("assigned_llm_model") or settings.user_default_llm_model or "").strip()
    if provider == "groq":
        if not settings.groq_api_key:
            return replace(settings, llm_provider="offline", llm_decision_mode="offline")
        return replace(
            settings,
            llm_provider="groq",
            groq_model=model or settings.groq_model,
            llm_max_tokens=min(int(settings.llm_max_tokens or 4096), 2048),
            llm_timeout_seconds=min(max(int(settings.llm_timeout_seconds or 45), 30), 60),
            llm_rolling_context_max_chunks=1 if int(settings.llm_rolling_context_max_chunks or 0) == 0 else min(int(settings.llm_rolling_context_max_chunks), 1),
        )
    if provider == "deepseek":
        if not settings.deepseek_api_key:
            return replace(settings, llm_provider="offline", llm_decision_mode="offline")
        deepseek_model = model if model in {"deepseek-v4-pro", "deepseek-v4-flash"} else settings.deepseek_model
        return replace(settings, llm_provider="deepseek", deepseek_model=deepseek_model)
    return replace(settings, llm_provider="offline", llm_decision_mode="offline")


def _model_name_for_settings(active_settings: Settings) -> str:
    if active_settings.llm_provider == "groq":
        return active_settings.groq_model
    if active_settings.llm_provider == "deepseek":
        return active_settings.deepseek_model
    return "offline"


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


def _auto_follow_buy_ideas_for_user(user: dict[str, Any], decisions: list[Any]) -> dict[str, Any]:
    mode = _normalize_signal_execution_mode(user.get("signal_execution_mode"))
    summary: dict[str, Any] = {
        "mode": mode,
        "enabled": mode in {"AUTO_PAPER", "AUTO_LIVE"},
        "buy_decisions": sum(1 for decision in decisions if getattr(decision, "action", "") == "BUY"),
        "exit_decisions": sum(1 for decision in decisions if getattr(decision, "action", "") == "SELL"),
        "active_buy_ideas": 0,
        "followed": 0,
        "exited": 0,
        "managed_exits": [],
        "managed_exit_skips": [],
        "skipped": [],
        "follows": [],
        "exits": [],
    }

    user_id = int(user["id"])
    monitor_symbols = db.user_monitor_symbols(user_id)
    monitor_allowed = {str(symbol or "").upper() for symbol in monitor_symbols}
    if monitor_allowed:
        summary["monitor_scope"] = "CUSTOM"
        summary["monitor_symbols_count"] = len(monitor_allowed)
        summary["monitor_symbols_sample"] = sorted(monitor_allowed)[:12]
    else:
        summary["monitor_scope"] = "DYNAMIC_OPPORTUNITY"
    idea_mode = "LIVE" if mode == "AUTO_LIVE" else "PAPER"
    paper_cash_by_market = _user_paper_cash_by_market(user)
    realized_pnl_by_market = db.user_follow_realized_pnl_by_market(user_id)
    exit_management = db.manage_user_follow_exits(user_id, cost_settings=settings)
    summary["managed_exits"] = exit_management.get("actions", [])
    summary["managed_exit_skips"] = exit_management.get("skipped", [])
    exit_symbols = {str(getattr(decision, "symbol", "") or "").upper() for decision in decisions if getattr(decision, "action", "") == "SELL"}
    for symbol in sorted(exit_symbols):
        exited = db.exit_user_follow_position(user_id, symbol, reason="auto_exit_signal_sell")
        if exited:
            summary["exited"] += len(exited)
            summary["exits"].extend(
                {
                    "symbol": symbol,
                    "mode": item.get("mode"),
                    "status": item.get("status"),
                    "reason": "auto_exit_signal_sell",
                    "return_pct": item.get("return_pct"),
                }
                for item in exited
            )

    followed_rows = db.user_followed_signal_ideas(user_id, 200)
    lifecycle_exit_statuses = {"EXIT_SIGNAL", "STOP_HIT", "TARGET_3_HIT", "EXPIRED"}
    lifecycle_exit_labels = {"exit_signal", "stopped", "target_3_hit", "expired"}
    already_exited = {item["symbol"] for item in summary["exits"]}
    for followed in followed_rows:
        symbol = str(followed.get("symbol") or "").upper()
        if not symbol or symbol in already_exited:
            continue
        status = str(followed.get("status") or "").upper()
        lifecycle = str(followed.get("lifecycle_status") or "").lower()
        if status not in lifecycle_exit_statuses and lifecycle not in lifecycle_exit_labels:
            continue
        exited = db.exit_user_follow_position(user_id, symbol, reason=f"auto_exit_{lifecycle or status.lower()}")
        if exited:
            already_exited.add(symbol)
            summary["exited"] += len(exited)
            summary["exits"].extend(
                {
                    "symbol": symbol,
                    "mode": item.get("mode"),
                    "status": item.get("status"),
                    "reason": f"auto_exit_{lifecycle or status.lower()}",
                    "return_pct": item.get("return_pct"),
                }
                for item in exited
            )

    realized_pnl_by_market = db.user_follow_realized_pnl_by_market(user_id)
    if mode == "SIGNAL_ONLY":
        return summary

    decision_buy_symbols = {str(getattr(decision, "symbol", "") or "").upper() for decision in decisions if _decision_has_buy_intent(decision)}
    if monitor_allowed:
        blocked = sorted(symbol for symbol in decision_buy_symbols if symbol and symbol not in monitor_allowed)
        if blocked:
            summary["skipped"].append(
                {
                    "reason": "outside_custom_monitor_list",
                    "symbols": blocked[:12],
                    "monitor_symbols_count": len(monitor_allowed),
                }
            )
        decision_buy_symbols = {symbol for symbol in decision_buy_symbols if symbol in monitor_allowed}
        scope_exits = db.exit_active_follows_outside_monitor_scope(user_id, monitor_allowed)
        if scope_exits:
            summary["exited"] += len(scope_exits)
            summary["skipped"].append(
                {
                    "reason": "cleaned_active_follows_outside_custom_monitor_list",
                    "symbols": [str(item.get("symbol") or "").upper() for item in scope_exits[:12]],
                    "monitor_symbols_count": len(monitor_allowed),
                }
            )
    active_buy_ideas = [
        idea
        for idea in db.latest_signal_ideas(200, user_id=user_id, symbols=monitor_symbols or None)
        if str(idea.get("signal_type") or "").upper() == "BUY"
        and str(idea.get("status") or "").upper() == "ACTIVE"
        and str(idea.get("lifecycle_status") or "active").lower() not in {"stopped", "target_3_hit", "expired", "exit_signal"}
    ]
    summary["active_buy_ideas"] = len(active_buy_ideas)
    buy_symbols = decision_buy_symbols | {
        str(idea.get("symbol") or "").upper()
        for idea in active_buy_ideas
        if _auto_follow_idea_fresh_enough(idea, decision_buy_symbols)
    }
    if not buy_symbols:
        return summary
    ideas = [idea for idea in active_buy_ideas if str(idea.get("symbol") or "").upper() in buy_symbols]
    active_follow_symbols = {
        str(item.get("symbol") or "").upper()
        for item in db.user_followed_signal_ideas(user_id, 200)
        if str(item.get("mode") or "").upper() in {"PAPER", "LIVE"}
        and str(item.get("follow_status") or "").upper() in {"ACTIVE", "LIVE_REQUESTED"}
        and int(item.get("qty") or 0) > 0
    }
    seen_symbols: set[str] = set()
    for idea in ideas:
        symbol = str(idea.get("symbol") or "").upper()
        if not symbol or symbol in seen_symbols:
            continue
        seen_symbols.add(symbol)
        quality_gate = auto_follow_quality_gate(idea)
        if not quality_gate.get("passed"):
            summary["skipped"].append({"symbol": symbol, **quality_skip_payload(quality_gate)})
            continue
        reentry_block = db.recent_user_symbol_exit(
            user_id,
            symbol,
            cooldown_hours=max(int(settings.auto_follow_reentry_cooldown_hours or AUTO_FOLLOW_REENTRY_COOLDOWN_HOURS), AUTO_FOLLOW_REENTRY_COOLDOWN_HOURS),
        )
        if reentry_block:
            summary["skipped"].append(
                {
                    "symbol": symbol,
                    "reason": "recent_risk_exit_cooldown",
                    "exit_key": reentry_block.get("exit_key"),
                    "exit_reason": reentry_block.get("exit_reason"),
                    "cooldown_minutes_left": reentry_block.get("cooldown_minutes_left"),
                }
            )
            continue
        if symbol in active_follow_symbols:
            summary["skipped"].append({"symbol": symbol, "reason": "already_followed_symbol"})
            continue
        if not _auto_follow_idea_fresh_enough(idea, decision_buy_symbols):
            summary["skipped"].append(
                {
                    "symbol": symbol,
                    "reason": "active_buy_not_fresh_enough_for_auto_follow",
                    "current_return_pct": round(float(idea.get("current_return_pct") or 0.0), 4),
                    "fresh_action": idea.get("fresh_action"),
                    "setup_bucket": idea.get("setup_bucket"),
                }
            )
            continue
        if idea.get("user_follow") and str((idea.get("user_follow") or {}).get("status") or "").upper() in {"ACTIVE", "LIVE_REQUESTED"}:
            summary["skipped"].append({"symbol": symbol, "reason": "already_followed"})
            continue
        market = normalize_market_region(idea.get("market_region") or "IN", default="IN")
        if mode == "AUTO_LIVE":
            try:
                _require_user_live_broker(user, market)
            except HTTPException as exc:
                summary["skipped"].append({"symbol": symbol, "reason": str(exc.detail)})
                continue
        tracked = db.user_followed_signal_ideas(user_id, 200)
        portfolio = _user_follow_portfolio(
            tracked,
            db.latest_portfolio() or {},
            paper_cash_by_market=paper_cash_by_market,
            realized_pnl_by_market=realized_pnl_by_market,
        )
        market_portfolio = (portfolio.get("portfolio_by_market") or {}).get(market) or {}
        cash = float(market_portfolio.get("cash") or 0.0)
        price = float(idea.get("latest_price") or idea.get("entry_price") or 0.0)
        size_multiplier = quality_size_multiplier(quality_gate)
        idea_details = idea.get("details") if isinstance(idea.get("details"), dict) else {}
        opportunity_scan = idea_details.get("opportunity_scan") if isinstance(idea_details.get("opportunity_scan"), dict) else {}
        liquidity_scan = opportunity_scan.get("liquidity_profile") if isinstance(opportunity_scan.get("liquidity_profile"), dict) else {}
        sizing = _auto_follow_sizing(
            cash,
            price,
            size_multiplier=size_multiplier,
            market_region=market,
            stop_loss=_float_or_none(idea.get("stop_loss") or idea_details.get("stop_loss")),
            confidence=_float_or_none(idea.get("confidence")),
            avg_daily_turnover=_float_or_none(
                opportunity_scan.get("avg20_turnover")
                or opportunity_scan.get("turnover")
                or liquidity_scan.get("avg20_turnover")
            ),
        )
        amount = float(sizing.get("amount") or 0.0)
        if amount <= 0:
            skip_reason = str(sizing.get("reason") or "")
            if skip_reason != "position_size_below_minimum_trade_economics":
                skip_reason = "position_size_cap_below_one_share" if price > 0 and cash >= price else "insufficient_paper_cash_for_position_size"
            summary["skipped"].append(
                {
                    "symbol": symbol,
                    "reason": skip_reason,
                    "cash": round(cash, 4),
                    "price": round(price, 4),
                    "sizing": sizing,
                }
            )
            continue
        try:
            follow = db.follow_signal_idea(user_id, int(idea["id"]), mode=idea_mode, amount=amount, cost_settings=settings)
            summary["followed"] += 1
            summary["follows"].append(
                {
                    "symbol": symbol,
                    "idea_id": int(idea["id"]),
                    "mode": idea_mode,
                    "amount": round(amount, 4),
                    "size_multiplier": size_multiplier,
                    "risk_warnings": quality_gate.get("risk_warnings", []),
                    "qty": follow.get("qty"),
                    "entry_price": follow.get("entry_price"),
                    "market_region": market,
                }
            )
        except ValueError as exc:
            summary["skipped"].append({"symbol": symbol, "reason": str(exc)})
    return summary


def _auto_follow_sizing(
    cash: float,
    price: float,
    *,
    size_multiplier: float = 1.0,
    market_region: str = "IN",
    stop_loss: float | None = None,
    confidence: float | None = None,
    avg_daily_turnover: float | None = None,
) -> dict[str, Any]:
    return auto_follow_sizing(
        cash,
        price,
        max_position_pct=float(settings.max_position_pct or 0.25),
        size_multiplier=size_multiplier,
        market_region=market_region,
        settings=settings,
        stop_loss=stop_loss,
        confidence=confidence,
        avg_daily_turnover=avg_daily_turnover,
    )


def _auto_follow_amount(
    cash: float,
    price: float,
    *,
    size_multiplier: float = 1.0,
    market_region: str = "IN",
) -> float:
    sizing = _auto_follow_sizing(cash, price, size_multiplier=size_multiplier, market_region=market_region)
    return float(sizing.get("amount") or 0.0)


def _auto_follow_idea_fresh_enough(idea: dict[str, Any], fresh_buy_symbols: set[str]) -> bool:
    symbol = str(idea.get("symbol") or "").upper()
    signal_type = str(idea.get("signal_type") or "").upper()
    status = str(idea.get("status") or "").upper()
    if signal_type != "BUY" or status not in {"ACTIVE", "TARGET_1_HIT", "TARGET_2_HIT"}:
        return False
    details = idea.get("details") if isinstance(idea.get("details"), dict) else {}
    if str(idea.get("trade_state") or "").upper() == "RISK_REVIEW" or str(idea.get("setup_bucket") or "").upper() in {"RISK_REVIEW", "AVOID"}:
        return False
    current_return = _float_or_none(idea.get("current_return_pct")) or 0.0
    if current_return < -1.5:
        return False
    score = _float_or_none(idea.get("overall_score_pct") or details.get("overall_score_pct")) or 0.0
    grade = str(idea.get("overall_grade") or details.get("overall_grade") or "").upper()
    if score < 70 or grade not in {"A", "B"}:
        return False
    if str(idea.get("fresh_action") or "").upper() != "BUY_NOW":
        return False
    if symbol in fresh_buy_symbols:
        return True
    if not _idea_seen_recently(idea):
        return False
    return _price_inside_entry_zone(idea, cushion_pct=0.003)


def _decision_has_buy_intent(decision: Decision) -> bool:
    if str(getattr(decision, "action", "") or "").upper() == "BUY":
        return True
    try:
        audit = json.loads(getattr(decision, "details_json", "") or "{}")
    except (TypeError, json.JSONDecodeError):
        return False
    duplicate = audit.get("duplicate_buy_suppression") if isinstance(audit.get("duplicate_buy_suppression"), dict) else {}
    if duplicate.get("suppressed"):
        return False
    risk_gates = audit.get("risk_gates") if isinstance(audit.get("risk_gates"), dict) else {}
    gate_context = risk_gates.get("decision_gate_context") if isinstance(risk_gates.get("decision_gate_context"), dict) else {}
    canonical_gate = gate_context.get("canonical_trade_gate") if isinstance(gate_context.get("canonical_trade_gate"), dict) else {}
    return canonical_gate.get("passed") is True


def _active_monitor_follow_allowed(idea: dict[str, Any]) -> bool:
    follow = idea.get("user_follow") if isinstance(idea.get("user_follow"), dict) else {}
    follow_status = str(follow.get("status") or "").upper()
    if follow_status in {"ACTIVE", "LIVE_REQUESTED", "LIVE_EXIT_REQUESTED"} and int(follow.get("qty") or 0) > 0:
        return False
    details = idea.get("details") if isinstance(idea.get("details"), dict) else {}
    continuity = details.get("signal_continuity") if isinstance(details.get("signal_continuity"), dict) else {}
    if not (continuity.get("duplicate_active_buy") or continuity.get("already_active_buy")):
        return False
    return _idea_seen_recently(idea)


def _idea_seen_recently(idea: dict[str, Any], *, minutes: int = FRESH_BUY_WINDOW_MINUTES) -> bool:
    raw = idea.get("last_seen_at") or idea.get("updated_at") or idea.get("first_seen_at")
    if not raw:
        return False
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    age = datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)
    return age <= timedelta(minutes=max(int(minutes or FRESH_BUY_WINDOW_MINUTES), 1))


def _price_inside_entry_zone(idea: dict[str, Any], cushion_pct: float = 0.0) -> bool:
    price = _float_or_none(idea.get("latest_price") or idea.get("price"))
    zone = idea.get("entry_zone")
    if price is None or price <= 0:
        return False
    if isinstance(zone, list) and len(zone) >= 2:
        low = _float_or_none(zone[0])
        high = _float_or_none(zone[1])
        if low is not None and high is not None:
            lower = min(low, high) * (1 - cushion_pct)
            upper = max(low, high) * (1 + cushion_pct)
            return lower <= price <= upper
    entry = _float_or_none(idea.get("entry_price"))
    if entry and entry > 0:
        return abs(price - entry) / entry <= max(cushion_pct, 0.005)
    return False


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


async def _run_user_signal_cycle(user_id: int) -> dict[str, Any]:
    phase = "prepare"

    def set_phase(name: str, details: dict[str, Any] | None = None) -> None:
        nonlocal phase
        phase = name
        user_signal_sessions.update_phase(user_id, name, details)

    set_phase("prepare")
    user = db.user_by_id(user_id)
    if not user or not user.get("active") or user.get("role") == "admin":
        raise RuntimeError("user signal session requires an active non-admin user")

    full_universe = db.get_universe(enabled_only=True, market_region=settings.market_region)
    session_context = market_session_context(settings.market_region, full_universe)
    db.set_state("market_session_context", session_context)
    if settings.skip_market_data_when_closed:
        open_universe = filter_universe_for_open_markets(full_universe, session_context)
        if not open_universe:
            set_phase(
                "post_market_prep",
                {
                    "market_closed": True,
                    "closed_regions": session_context.get("closed_regions"),
                    "open_regions": session_context.get("open_regions"),
                },
            )
            news_summary: dict[str, Any]
            if settings.post_market_prep_enabled:
                prep_settings = replace(settings, enable_llm_sentiment=False, llm_provider="offline", llm_decision_mode="offline")
                news_summary = await SentimentService(prep_settings, db).refresh_watchlist_news(
                    full_universe,
                    limit=settings.post_market_news_symbols,
                    allow_llm=False,
                    reason=f"user_post_market_prep:{user_id}",
                )
            else:
                news_summary = {
                    "enabled": False,
                    "reason": "post_market_prep_disabled",
                    "symbols_requested": 0,
                    "symbols_refreshed": 0,
                }
            prep_context = {
                "enabled": settings.post_market_prep_enabled,
                "mode": "user_market_closed_tomorrow_prep",
                "prepared_at": utc_now(),
                "user_id": user_id,
                "market_session": session_context,
                "news": news_summary,
                "readiness_note": "Markets are closed, so no quote/candle/OpenStocks View scan or credit charge was made. News prep was refreshed for tomorrow.",
            }
            db.set_state("tomorrow_prep_context", prep_context)
            credit_summary = db.user_credit_summary(user_id, include_ledger=False)
            db.insert_agent_log(
                "INFO",
                "user_session",
                "market_closed_skip_user_signal_cycle",
                f"Skipped market-data and OpenStocks View scan for {user.get('username')} because selected markets are closed.",
                {
                    "user_id": user_id,
                    "username": user.get("username"),
                    "closed_regions": session_context.get("closed_regions"),
                    "news_symbols_requested": news_summary.get("symbols_requested"),
                    "news_symbols_refreshed": news_summary.get("symbols_refreshed"),
                },
            )
            set_phase("sleep", {"market_closed": True, "last_llm_calls": 0, "last_decision_count": 0})
            return {
                "last_cycle_at": utc_now(),
                "last_error": None,
                "market_closed": True,
                "last_credit_charge": 0.0,
                "last_llm_calls": 0,
                "last_llm_tokens": 0,
                "last_llm_activity": {"billable": False, "reason": "market_closed"},
                "auto_trade": {"mode": _normalize_signal_execution_mode(user.get("signal_execution_mode")), "enabled": False, "reason": "market_closed"},
                "last_decision_count": 0,
                "last_action_counts": {},
                "symbols_per_cycle": 0,
                "credit_balance": credit_summary.get("credit_balance"),
                "daily_credits_remaining": credit_summary.get("daily_credits_remaining"),
                "news_prep": news_summary,
            }
        full_universe = open_universe

    estimated_charge = _estimated_signal_credit_charge()
    can_spend, credit_before = db.user_has_credit_for(user_id, estimated_charge)
    if not can_spend:
        raise RuntimeError(f"insufficient credits or daily budget; estimated need {estimated_charge:.4f} credits")

    universe = user_signal_sessions.select_universe(user_id, full_universe, credit_before, estimated_charge)
    if not universe:
        if db.user_monitor_symbols(user_id):
            raise RuntimeError("none of your monitored symbols are available in the currently open market/universe")
        raise RuntimeError("no enabled universe symbols available for user signal session")
    monitor_symbols = db.user_monitor_symbols(user_id)
    set_phase(
        "market_quotes",
        {
            "symbol_count": len(universe),
            "symbols_sample": [row.get("symbol") for row in universe[:10]],
            "monitor_scope": "CUSTOM" if monitor_symbols else "DYNAMIC_OPPORTUNITY",
            "monitor_symbols_count": len(monitor_symbols),
        },
    )

    user_market_data = _market_data_provider_for_user(user, settings.market_region)
    user_strategy, budget_policy = _strategy_for_user_budget(user, credit_before, estimated_charge)
    usage_after_id = db.latest_llm_usage_id()
    usage_scope = f"user_signal_cycle:{user_id}:{uuid4().hex}"
    usage: dict[str, Any] = {"calls": 0, "cost_usd": 0.0, "total_tokens": 0, "input_chars": 0, "output_chars": 0}
    credit_after = credit_before
    decisions: list[Any] = []
    cycle_error: BaseException | None = None
    phase_at_error = phase
    context_token = current_user_id.set(user_id)
    scope_token = current_llm_usage_scope.set(usage_scope)
    try:
        quotes = await user_market_data.get_quotes(universe)
        if not quotes:
            raise MarketDataError(f"{user_market_data.source_name} returned no quotes for user signal session")
        set_phase(
            "candles",
            {
                "symbol_count": len(universe),
                "quote_count": len(quotes),
                "provider": user_market_data.source_name,
            },
        )
        candles_fresh = await user_market_data.get_candles(universe)
        set_phase(
            "persist_market_data",
            {
                "symbol_count": len(universe),
                "quote_count": len(quotes),
                "symbols_with_candles": len(candles_fresh),
                "provider": user_market_data.source_name,
            },
        )
        db.upsert_quotes(quotes)
        db.upsert_candles(candles_fresh)
        candle_sets = db.recent_candle_sets_by_symbol([row["symbol"] for row in universe])
        candles = {
            symbol: sets.get("analysis") or sets.get("daily") or sets.get("intraday") or []
            for symbol, sets in candle_sets.items()
        }
        set_phase(
            "strategy_and_llm",
            {
                "symbol_count": len(universe),
                "provider": user_market_data.source_name,
                "llm_provider": budget_policy.get("provider"),
                "llm_model": budget_policy.get("model"),
            },
        )
        decisions = await asyncio.to_thread(
            lambda: asyncio.run(
                user_strategy.evaluate(
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
            )
        )
    except asyncio.CancelledError:
        raise
    except BaseException as exc:
        phase_at_error = phase
        cycle_error = exc
    finally:
        current_user_id.reset(context_token)
        current_llm_usage_scope.reset(scope_token)

    set_phase(
        "billing",
        {
            "symbol_count": len(universe),
            "decision_count": len(decisions),
            "cycle_error": _exception_message(cycle_error) if cycle_error else None,
        },
    )
    usage = db.llm_usage_cost_for_scope(user_id, usage_scope, usage_after_id)
    llm_activity = _llm_activity_from_decisions(decisions, usage)
    if usage.get("calls") and llm_activity.get("billable"):
        billing = _credit_billing_for_usage(usage)
        credit_after = db.charge_user_credits(
            user_id,
            billing["base_credits"],
            "Autonomous signal cycle",
            {
                "symbols": [row["symbol"] for row in universe],
                "symbol_count": len(universe),
                "llm_usage": usage,
                "llm_activity": llm_activity,
                "provider": user_market_data.source_name,
                "estimated_credit_before": estimated_charge,
                "budget_policy": budget_policy,
                "usage_scope": usage_scope,
                "credit_billing": {
                    "tokens_per_credit": billing["tokens_per_credit"],
                    "total_tokens": billing["total_tokens"],
                    "charged_credits": billing["charged_credits"],
                },
                "admin_billing": {
                    "base_credits": billing["base_credits"],
                    "platform_margin_pct": billing["platform_margin_pct"],
                    "platform_margin_credits": billing["platform_margin_credits"],
                    "api_cost_usd": billing["api_cost_usd"],
                },
            },
            margin_pct=billing["platform_margin_pct"],
            minimum_charge=0.0,
        )
    if cycle_error is not None:
        raise UserSignalCycleError(
            phase_at_error,
            cycle_error,
            {
                "symbol_count": len(universe),
                "provider": user_market_data.source_name,
                "llm_provider": budget_policy.get("provider"),
                "llm_model": budget_policy.get("model"),
            },
        ) from cycle_error

    set_phase("persist_decisions", {"symbol_count": len(universe), "decision_count": len(decisions)})
    tagged_decisions = [
        _attach_user_to_decision(decision, user, credit_after, budget_policy)
        for decision in decisions
    ]
    if tagged_decisions:
        db.insert_decisions(tagged_decisions)
        db.upsert_signal_ideas_from_decisions(tagged_decisions)
    set_phase("auto_execute", {"symbol_count": len(universe), "decision_count": len(tagged_decisions)})
    auto_trade = _auto_follow_buy_ideas_for_user(user, tagged_decisions)

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
            "llm_activity": llm_activity,
            "credit_charge": credit_charge,
            "daily_credits_remaining": credit_after.get("daily_credits_remaining"),
            "budget_policy": budget_policy,
            "auto_trade": auto_trade,
        },
    )
    set_phase(
        "sleep",
        {
            "last_decision_count": len(tagged_decisions),
            "last_llm_calls": usage.get("calls", 0),
            "llm_activity": llm_activity,
            "auto_trade": auto_trade,
        },
    )
    return {
        "last_cycle_at": utc_now(),
        "last_error": None,
        "last_credit_charge": credit_charge,
        "last_llm_calls": usage.get("calls", 0),
        "last_llm_tokens": usage.get("total_tokens", 0),
        "last_llm_activity": llm_activity,
        "last_decision_count": len(tagged_decisions),
        "last_action_counts": action_counts,
        "symbols_per_cycle": len(universe),
        "monitor_scope": "CUSTOM" if monitor_symbols else "DYNAMIC_OPPORTUNITY",
        "monitor_symbols_count": len(monitor_symbols),
        "monitor_symbols_sample": monitor_symbols[:12],
        "credit_balance": credit_after.get("credit_balance"),
        "daily_credits_remaining": credit_after.get("daily_credits_remaining"),
        "budget_policy": budget_policy,
        "auto_trade": auto_trade,
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
    output.pop("admin_billing", None)
    if isinstance(output.get("budget_policy"), dict):
        output["budget_policy"] = _public_budget_policy(output["budget_policy"])
    if isinstance(output.get("llm_usage"), dict):
        output["llm_usage"] = _public_llm_usage(output["llm_usage"])
    return output


def _public_budget_policy(policy: dict[str, Any]) -> dict[str, Any]:
    return {
        "mode": "credit_managed_analysis" if policy.get("mode") != "low_credit_guard" else "low_credit_guard",
        "daily_credits_remaining": policy.get("daily_credits_remaining", 0.0),
        "estimated_signal_credit": policy.get("estimated_signal_credit", 0.0),
        "tokens_per_credit": policy.get("tokens_per_credit", settings.credit_tokens_per_credit),
        "reason": "OpenStocks selected the analysis lane assigned by admin.",
    }


def _public_llm_usage_summary(summary: dict[str, Any]) -> dict[str, Any]:
    output = dict(summary or {})
    output.pop("by_model_today", None)
    output.pop("recent", None)
    return output


def _sanitize_decision_row_for_user(row: dict[str, Any]) -> dict[str, Any]:
    output = dict(row)
    details = _json_object(output.get("details_json"))
    details = _sanitize_private_llm_metadata(details)
    output["details_json"] = json.dumps(details, default=str, separators=(",", ":"))
    return output


def _sanitize_order_row_for_user(row: dict[str, Any]) -> dict[str, Any]:
    output = dict(row)
    details = _json_object(output.get("details_json"))
    details = _sanitize_private_llm_metadata(details)
    output["details_json"] = json.dumps(details, default=str, separators=(",", ":"))
    return output


def _sanitize_decision_payload_for_user(payload: dict[str, Any]) -> dict[str, Any]:
    output = dict(payload)
    if isinstance(output.get("details"), dict):
        output["details"] = _sanitize_private_llm_metadata(output["details"])
    return output


def _sanitize_private_llm_metadata(value: Any) -> Any:
    private_keys = {
        "configured_provider",
        "configured_model",
        "selected_provider",
        "selected_model",
        "model_attempts",
        "_llm_provider",
        "_llm_model",
        "_llm_attempts",
        "llm_provider",
        "llm_model",
    }
    if isinstance(value, dict):
        llm_scoped = bool(
            {"decision_path", "analysis_mode", "llm_output", "json_repaired", "json_retry", "llm_timeout"}
            & set(value.keys())
        ) or ({"provider", "model"} <= set(value.keys()) and bool({"reason", "risk", "strategy", "evidence", "risk_checks"} & set(value.keys())))
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            if key in private_keys or (llm_scoped and key in {"provider", "model"}):
                continue
            if key == "budget_policy" and isinstance(item, dict):
                sanitized[key] = _public_budget_policy(item)
                continue
            sanitized[key] = _sanitize_private_llm_metadata(item)
        return sanitized
    if isinstance(value, list):
        return [_sanitize_private_llm_metadata(item) for item in value]
    return value


def _public_llm_usage(usage: dict[str, Any]) -> dict[str, Any]:
    tokens = int(usage.get("total_tokens") or 0)
    rate = max(float(settings.credit_tokens_per_credit or 10), 1.0)
    return {
        "calls": int(usage.get("calls") or 0),
        "total_tokens": tokens,
        "tokens_per_credit": rate,
        "estimated_credits": round(tokens / rate, 6),
        "input_chars": int(usage.get("input_chars") or 0),
        "output_chars": int(usage.get("output_chars") or 0),
    }


def _llm_activity_from_decisions(decisions: list[Any], usage: dict[str, Any]) -> dict[str, Any]:
    selected = 0
    attempted = 0
    failed = 0
    latest_reason = ""
    for decision in decisions:
        details_value = getattr(decision, "details_json", None)
        if details_value is None and isinstance(decision, dict):
            details_value = decision.get("details_json")
        details = _json_object(details_value)
        decision_path = str(details.get("decision_path") or "")
        risk_gates = details.get("risk_gates") or {}
        context = details.get("context") or {}
        if (
            decision_path.startswith("llm_")
            or risk_gates.get("llm_deep_review_selected")
            or (context.get("llm_primary_selection") or {}).get("selected")
        ):
            selected += 1
        review = context.get("llm_primary_review") or {}
        error = review.get("llm_error")
        attempts = []
        if isinstance(error, dict):
            attempts = error.get("model_attempts") or []
        if attempts or review.get("reviewed") or decision_path.startswith("llm_"):
            attempted += 1
        if error:
            failed += 1
            latest_reason = _public_llm_failure_reason(error) or latest_reason
        fallback = details.get("llm_primary_fallback") or context.get("llm_primary_fallback") or {}
        if fallback and not latest_reason:
            latest_reason = _public_llm_failure_reason({"reason": fallback.get("llm_reason") or fallback.get("reason")})
    calls = int(usage.get("calls") or 0)
    raw_credits = _public_llm_usage(usage)["estimated_credits"]
    billable = bool(calls and not (attempted > 0 and failed >= attempted))
    credits_charged = raw_credits if billable else 0.0
    if calls and billable:
        status = "completed_billable"
        message = f"OpenStocks View completed the review and used {credits_charged:.2f} credits."
    elif calls:
        status = "completed_unusable_not_charged"
        message = "OpenStocks View returned provider output, but it was not usable as a strict trading decision. No credits were charged."
    elif attempted:
        status = "attempted_no_billable_tokens"
        message = "OpenStocks View was selected and attempted analysis, but the provider returned no billable token usage. No credits were charged."
    elif selected:
        status = "selected_not_attempted"
        message = "OpenStocks View was selected, but no provider attempt was completed in this cycle. No credits were charged."
    else:
        status = "not_selected"
        message = "No symbol reached OpenStocks View in this cycle. No review credits were used."
    return {
        "status": status,
        "message": message,
        "selected_symbols": selected,
        "attempted_symbols": attempted,
        "failed_symbols": failed,
        "billable": billable,
        "credits_charged": credits_charged,
        "raw_provider_credits": raw_credits,
        "latest_failure": latest_reason,
    }


def _public_llm_failure_reason(error: dict[str, Any]) -> str:
    text = str(error.get("reason") or error.get("error") or "").strip()
    attempts = error.get("model_attempts") if isinstance(error.get("model_attempts"), list) else []
    attempt_errors = " ".join(str(item.get("error") or "") for item in attempts if isinstance(item, dict))
    combined = f"{text} {attempt_errors}".lower()
    if "413" in combined or "request too large" in combined or "tokens per minute" in combined or "tpm" in combined:
        return "The provider rejected the request because it was too large for the assigned model's token-per-minute limit."
    if "429" in combined or "too many requests" in combined or "rate limit" in combined:
        return "The provider rate-limited the request before returning a billable response."
    if "timeout" in combined:
        return "The provider timed out before returning a usable decision."
    if text:
        return "The provider did not return a usable decision."
    return ""


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


def _manual_universe_row(symbol: str, market_region: str = "IN") -> dict[str, Any]:
    region = normalize_market_region(market_region)
    exchange = "NASDAQ" if region == "US" else "NSE"
    yahoo_symbol = symbol if region == "US" else f"{symbol}.NS"
    return {
        "symbol": symbol,
        "name": symbol,
        "exchange": exchange,
        "yahoo_symbol": yahoo_symbol,
        "kite_symbol": "" if region == "US" else f"NSE:{symbol}",
        "indstocks_scrip_code": "",
        "indstocks_security_id": "",
        "upstox_instrument_key": "",
        "nubra_symbol": "" if region == "US" else symbol,
        "nubra_ref_id": None,
        "sector": "manual",
        "base_price": 100,
        "enabled": 1,
    }


async def _analysis_reference_data(
    row: dict[str, Any],
    quote_payload: dict[str, Any],
    candles: list[Any],
    market_region: str,
) -> dict[str, Any]:
    region = normalize_market_region(market_region)
    yahoo_symbol = str(row.get("yahoo_symbol") or "").strip() or (str(row.get("symbol") or "").upper() if region == "US" else f"{str(row.get('symbol') or '').upper()}.NS")
    fundamentals: dict[str, Any] = {
        "company_name": row.get("name"),
        "source": "reference_feed",
        "market_region": region,
    }
    field_sources: dict[str, str] = {}
    sources_used: list[str] = []
    reference_errors: list[str] = []
    data_gaps: list[str] = []
    unavailable_fields: list[str] = []
    derived_from_candles: list[str] = []
    headers = {"Accept": "application/json", "User-Agent": "OpenStocks/1.0 (+symbol-analysis)"}
    async with httpx.AsyncClient(timeout=8, headers=headers, follow_redirects=True) as client:
        try:
            response = await client.get("https://query1.finance.yahoo.com/v7/finance/quote", params={"symbols": yahoo_symbol})
            response.raise_for_status()
            item = ((response.json().get("quoteResponse") or {}).get("result") or [{}])[0]
            if isinstance(item, dict) and item:
                _merge_yahoo_quote_reference(fundamentals, field_sources, item)
                sources_used.append("yahoo_quote_reference")
        except Exception as exc:
            reference_errors.append(f"yahoo_quote_reference:{exc.__class__.__name__}")

        if _missing_reference_fields(fundamentals, ("pe", "forward_pe", "pb", "market_cap", "beta", "eps_ttm")):
            try:
                response = await client.get(
                    f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/{yahoo_symbol}",
                    params={"modules": "summaryDetail,defaultKeyStatistics,price,financialData"},
                )
                response.raise_for_status()
                summary = ((response.json().get("quoteSummary") or {}).get("result") or [{}])[0]
                if isinstance(summary, dict) and summary:
                    _merge_yahoo_summary_reference(fundamentals, field_sources, summary)
                    sources_used.append("yahoo_quote_summary_reference")
            except Exception as exc:
                reference_errors.append(f"yahoo_quote_summary_reference:{exc.__class__.__name__}")

        if region == "IN" and _missing_reference_fields(fundamentals, ("pe", "market_cap", "week_52_high", "week_52_low")):
            try:
                nse_headers = {
                    **headers,
                    "User-Agent": (
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
                    ),
                    "Referer": f"https://www.nseindia.com/get-quotes/equity?symbol={str(row.get('symbol') or '').upper()}",
                }
                response = await client.get(
                    "https://www.nseindia.com/api/quote-equity",
                    params={"symbol": str(row.get("symbol") or "").upper()},
                    headers=nse_headers,
                )
                response.raise_for_status()
                nse_quote = response.json()
                if isinstance(nse_quote, dict) and nse_quote:
                    _merge_nse_quote_reference(fundamentals, field_sources, nse_quote, quote_payload)
                    sources_used.append("nse_quote_reference")
            except Exception as exc:
                reference_errors.append(f"nse_quote_reference:{exc.__class__.__name__}")

        if _missing_reference_fields(fundamentals, ("week_52_high", "week_52_low")):
            try:
                response = await client.get(
                    f"https://query1.finance.yahoo.com/v8/finance/chart/{yahoo_symbol}",
                    params={"range": "1y", "interval": "1d", "includePrePost": "false"},
                )
                response.raise_for_status()
                result = ((response.json().get("chart") or {}).get("result") or [{}])[0]
                chart_meta = result.get("meta") or {}
                if isinstance(chart_meta, dict) and chart_meta:
                    _merge_yahoo_quote_reference(fundamentals, field_sources, chart_meta, source="yahoo_chart_reference")
                    sources_used.append("yahoo_chart_reference")
            except Exception as exc:
                reference_errors.append(f"yahoo_chart_reference:{exc.__class__.__name__}")

    if sources_used:
        fundamentals["source"] = sources_used[0]
        fundamentals["sources"] = _dedupe(sources_used)
    else:
        fundamentals["source"] = "reference_unavailable"

    if fundamentals.get("week_52_high") is None or fundamentals.get("week_52_low") is None:
        candle_levels = _candle_52w_levels(candles)
        if fundamentals.get("week_52_high") is None and candle_levels.get("week_52_high") is not None:
            fundamentals["week_52_high"] = candle_levels["week_52_high"]
            field_sources["week_52_high"] = "candles"
            derived_from_candles.append("week_52_high")
        if fundamentals.get("week_52_low") is None and candle_levels.get("week_52_low") is not None:
            fundamentals["week_52_low"] = candle_levels["week_52_low"]
            field_sources["week_52_low"] = "candles"
            derived_from_candles.append("week_52_low")

    if _json_number(quote_payload.get("volume")) is not None and _json_number(quote_payload.get("volume")) > 0:
        fundamentals["volume"] = quote_payload.get("volume")
        field_sources["volume"] = str(quote_payload.get("source") or "market_data")
    if quote_payload.get("close") is not None:
        fundamentals["previous_close"] = quote_payload.get("close")

    for required in ("week_52_high", "week_52_low"):
        if fundamentals.get(required) is None:
            data_gaps.append(required)

    for optional in ("pe", "pb", "market_cap", "forward_pe", "beta", "eps_ttm"):
        if fundamentals.get(optional) is None:
            unavailable_fields.append(optional)

    if field_sources:
        fundamentals["field_sources"] = field_sources

    row_fields = {
        "name": fundamentals.get("company_name") or row.get("name"),
        "sector": fundamentals.get("sector") or row.get("sector"),
        "industry": fundamentals.get("industry") or row.get("industry"),
        "pe": fundamentals.get("pe"),
        "forward_pe": fundamentals.get("forward_pe"),
        "pb": fundamentals.get("pb"),
        "market_cap": fundamentals.get("market_cap"),
        "week_52_high": fundamentals.get("week_52_high"),
        "week_52_low": fundamentals.get("week_52_low"),
        "beta": fundamentals.get("beta"),
        "eps_ttm": fundamentals.get("eps_ttm"),
    }
    return {
        "source": fundamentals["source"],
        "fundamentals": fundamentals,
        "row_fields": {key: value for key, value in row_fields.items() if value is not None and value != ""},
        "data_gaps": _dedupe(data_gaps),
        "unavailable_fields": _dedupe(unavailable_fields),
        "derived_from_candles": derived_from_candles,
        "sources": _dedupe(sources_used),
        "field_sources": field_sources,
        "reference_errors": _dedupe(reference_errors),
    }


def _candle_52w_levels(candles: list[Any]) -> dict[str, float | None]:
    recent = candles[-252:] if len(candles) >= 252 else candles
    highs = [_json_number(getattr(candle, "high", None)) for candle in recent]
    lows = [_json_number(getattr(candle, "low", None)) for candle in recent]
    highs = [value for value in highs if value is not None]
    lows = [value for value in lows if value is not None]
    return {
        "week_52_high": max(highs) if highs else None,
        "week_52_low": min(lows) if lows else None,
    }


def _missing_reference_fields(fundamentals: dict[str, Any], fields: tuple[str, ...]) -> bool:
    return any(fundamentals.get(field) is None for field in fields)


def _set_reference_number(
    fundamentals: dict[str, Any],
    field_sources: dict[str, str],
    field: str,
    value: Any,
    source: str,
    *,
    positive_only: bool = True,
) -> bool:
    number = _json_number(value)
    if number is None or (positive_only and number <= 0):
        return False
    fundamentals[field] = number
    field_sources[field] = source
    return True


def _merge_yahoo_quote_reference(
    fundamentals: dict[str, Any],
    field_sources: dict[str, str],
    item: dict[str, Any],
    *,
    source: str = "yahoo_quote_reference",
) -> None:
    field_map = {
        "pe": "trailingPE",
        "forward_pe": "forwardPE",
        "pb": "priceToBook",
        "market_cap": "marketCap",
        "week_52_high": "fiftyTwoWeekHigh",
        "week_52_low": "fiftyTwoWeekLow",
        "volume": "regularMarketVolume",
        "average_volume_10d": "averageDailyVolume10Day",
        "average_volume_3m": "averageDailyVolume3Month",
        "beta": "beta",
        "eps_ttm": "epsTrailingTwelveMonths",
    }
    for target, source_field in field_map.items():
        _set_reference_number(
            fundamentals,
            field_sources,
            target,
            item.get(source_field),
            source,
            positive_only=target not in {"beta", "eps_ttm"},
        )
    fundamentals["company_name"] = item.get("longName") or item.get("shortName") or fundamentals.get("company_name")
    for key in ("currency", "quoteType", "exchange"):
        if item.get(key):
            target = "quote_type" if key == "quoteType" else key
            fundamentals[target] = item.get(key)


def _merge_yahoo_summary_reference(
    fundamentals: dict[str, Any],
    field_sources: dict[str, str],
    summary: dict[str, Any],
) -> None:
    paths_by_field: dict[str, tuple[tuple[str, ...], ...]] = {
        "pe": (("summaryDetail", "trailingPE"), ("defaultKeyStatistics", "trailingPE")),
        "forward_pe": (("summaryDetail", "forwardPE"), ("defaultKeyStatistics", "forwardPE")),
        "pb": (("defaultKeyStatistics", "priceToBook"),),
        "market_cap": (("price", "marketCap"), ("summaryDetail", "marketCap")),
        "week_52_high": (("summaryDetail", "fiftyTwoWeekHigh"),),
        "week_52_low": (("summaryDetail", "fiftyTwoWeekLow"),),
        "volume": (("summaryDetail", "volume"),),
        "average_volume_10d": (("summaryDetail", "averageDailyVolume10Day"),),
        "average_volume_3m": (("summaryDetail", "averageVolume"),),
        "beta": (("summaryDetail", "beta"), ("defaultKeyStatistics", "beta")),
        "eps_ttm": (("defaultKeyStatistics", "trailingEps"),),
    }
    for field, paths in paths_by_field.items():
        for path in paths:
            if _set_reference_number(
                fundamentals,
                field_sources,
                field,
                _nested_value(summary, path),
                "yahoo_quote_summary_reference",
                positive_only=field not in {"beta", "eps_ttm"},
            ):
                break
    price = summary.get("price") if isinstance(summary.get("price"), dict) else {}
    fundamentals["company_name"] = _yahoo_text(price.get("longName")) or _yahoo_text(price.get("shortName")) or fundamentals.get("company_name")
    currency = _yahoo_text(price.get("currency"))
    if currency:
        fundamentals["currency"] = currency
    exchange = _yahoo_text(price.get("exchangeName")) or _yahoo_text(price.get("exchange"))
    if exchange:
        fundamentals["exchange"] = exchange


def _merge_nse_quote_reference(
    fundamentals: dict[str, Any],
    field_sources: dict[str, str],
    nse_quote: dict[str, Any],
    quote_payload: dict[str, Any],
) -> None:
    info = nse_quote.get("info") if isinstance(nse_quote.get("info"), dict) else {}
    metadata = nse_quote.get("metadata") if isinstance(nse_quote.get("metadata"), dict) else {}
    price_info = nse_quote.get("priceInfo") if isinstance(nse_quote.get("priceInfo"), dict) else {}
    security_info = nse_quote.get("securityInfo") if isinstance(nse_quote.get("securityInfo"), dict) else {}
    industry_info = nse_quote.get("industryInfo") if isinstance(nse_quote.get("industryInfo"), dict) else {}

    _set_reference_number(fundamentals, field_sources, "pe", metadata.get("pdSymbolPe"), "nse_quote_reference")
    week_high_low = price_info.get("weekHighLow") if isinstance(price_info.get("weekHighLow"), dict) else {}
    _set_reference_number(fundamentals, field_sources, "week_52_high", week_high_low.get("max"), "nse_quote_reference")
    _set_reference_number(fundamentals, field_sources, "week_52_low", week_high_low.get("min"), "nse_quote_reference")

    issued_size = _json_number(security_info.get("issuedSize"))
    live_price = (
        _json_number(quote_payload.get("price"))
        or _json_number(price_info.get("lastPrice"))
        or _json_number(price_info.get("close"))
        or _json_number(price_info.get("previousClose"))
    )
    if issued_size and live_price:
        _set_reference_number(
            fundamentals,
            field_sources,
            "market_cap",
            issued_size * live_price,
            "nse_quote_reference",
        )

    fundamentals["company_name"] = info.get("companyName") or fundamentals.get("company_name")
    fundamentals["currency"] = fundamentals.get("currency") or "INR"
    fundamentals["exchange"] = fundamentals.get("exchange") or "NSE"
    fundamentals["sector"] = industry_info.get("sector") or metadata.get("pdSectorInd") or fundamentals.get("sector")
    fundamentals["industry"] = (
        industry_info.get("basicIndustry")
        or industry_info.get("industry")
        or metadata.get("industry")
        or info.get("industry")
        or fundamentals.get("industry")
    )


def _nested_value(payload: dict[str, Any], path: tuple[str, ...]) -> Any:
    value: Any = payload
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _yahoo_text(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("fmt") or value.get("longFmt") or value.get("raw")
    return str(value or "").strip()


def _json_number(value: Any) -> float | None:
    if isinstance(value, dict):
        if "raw" in value:
            return _json_number(value.get("raw"))
        if "fmt" in value:
            return _json_number(value.get("fmt"))
        if "longFmt" in value:
            return _json_number(value.get("longFmt"))
    if isinstance(value, str):
        text = value.strip().replace(",", "")
        if text.upper() in {"", "-", "--", "NA", "N/A", "NONE", "NULL"}:
            return None
        value = text
    try:
        number = float(value)
        return number if number == number else None
    except (TypeError, ValueError):
        return None


def _dedupe(values: list[str]) -> list[str]:
    output: list[str] = []
    for value in values:
        if value not in output:
            output.append(value)
    return output


def _json_object(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}") if isinstance(value, str) else value
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        return {}
