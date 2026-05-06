from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

from .db import Database
from .institutional_feeds import FreeInstitutionalFeedsService
from .macro import GlobalIntelligenceService
from .market_data import MarketDataError, MarketDataProvider
from .models import Decision, utc_now
from .paper_broker import PaperBroker
from .strategy import StrategyEngine


UpdateCallback = Callable[[dict[str, Any]], Awaitable[None]]


class TradingAgentService:
    def __init__(
        self,
        db: Database,
        market_data: MarketDataProvider,
        broker: PaperBroker,
        strategy: StrategyEngine,
        macro: GlobalIntelligenceService | None,
        institutional_feeds: FreeInstitutionalFeedsService | None,
        interval_seconds: int,
        cycle_timeout_seconds: int,
        on_update: UpdateCallback | None = None,
    ) -> None:
        self.db = db
        self.market_data = market_data
        self.broker = broker
        self.strategy = strategy
        self.macro = macro
        self.institutional_feeds = institutional_feeds
        self.interval_seconds = interval_seconds
        self.cycle_timeout_seconds = max(30, cycle_timeout_seconds)
        self.on_update = on_update
        self._task: asyncio.Task | None = None
        self._running = False
        self._last_error: str | None = None
        self._last_cycle_at: str | None = None
        self._cycle_started_at: str | None = None
        self._cycle_phase = "idle"
        self._last_cycle_duration_seconds: float | None = None

    @property
    def running(self) -> bool:
        return self._running and self._task is not None and not self._task.done()

    def start(self) -> None:
        if self.running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def run_once(self) -> dict[str, Any]:
        return await asyncio.wait_for(self._run_once_inner(), timeout=self.cycle_timeout_seconds)

    async def _run_once_inner(self) -> dict[str, Any]:
        started = datetime.now(timezone.utc)
        self._cycle_started_at = started.isoformat()
        self._cycle_phase = "market_quotes"
        universe = self.db.get_universe(enabled_only=True)
        quotes = await self.market_data.get_quotes(universe)
        if not quotes:
            raise MarketDataError(f"{self.market_data.source_name} returned no quotes for the enabled universe")
        self._cycle_phase = "candles"
        candles = await self.market_data.get_candles(universe)
        self._cycle_phase = "persist_market_data"
        self.db.upsert_quotes(quotes)
        self.db.upsert_candles(candles)
        self.broker.sync_marks(quotes)
        portfolio = self.broker.snapshot()
        positions = self.broker.positions_by_symbol()
        self._cycle_phase = "global_intelligence"
        macro_context = await self.macro.context_for_cycle() if self.macro else {}
        self.db.set_state("macro_context", macro_context)
        self._cycle_phase = "institutional_feeds"
        institutional_context = (
            await self.institutional_feeds.context_for_cycle(universe)
            if self.institutional_feeds
            else {}
        )
        self.db.set_state("institutional_context", institutional_context)
        self._cycle_phase = "strategy_and_llm"
        decisions = await self.strategy.evaluate(
            universe,
            quotes,
            positions,
            candles,
            macro_context,
            institutional_context,
        )
        self._cycle_phase = "risk_and_execution"
        risk_exits = self.strategy.stop_or_take_profit_exits(quotes, positions)
        decisions = self._merge_risk_exits(decisions, risk_exits)
        self.db.insert_decisions(decisions)
        self._execute_top_decisions(decisions, portfolio["equity"])
        portfolio = self.broker.snapshot()
        self._last_cycle_at = utc_now()
        self._last_cycle_duration_seconds = round((datetime.now(timezone.utc) - started).total_seconds(), 3)
        self._cycle_started_at = None
        self._cycle_phase = "idle"
        snapshot = self.snapshot()
        if self.on_update:
            await self.on_update(snapshot)
        return snapshot

    def snapshot(self) -> dict[str, Any]:
        quotes = self.db.latest_quotes()
        decisions = self.db.latest_decisions(80)
        suggestion_decisions = self.db.latest_decisions(1000)
        orders = self.db.latest_orders(80)
        order_audit_history = self.db.latest_orders(1000)
        positions = self._positions_with_exit_plans(self.db.positions(), order_audit_history)
        suggestions = self._suggestions(suggestion_decisions)
        return {
            "running": self.running,
            "provider": self.market_data.source_name,
            "last_error": self._last_error,
            "last_cycle_at": self._last_cycle_at,
            "cycle": {
                "phase": self._cycle_phase,
                "started_at": self._cycle_started_at,
                "timeout_seconds": self.cycle_timeout_seconds,
                "last_duration_seconds": self._last_cycle_duration_seconds,
            },
            "portfolio": self.db.latest_portfolio()
            or {
                "cash": self.broker.cash,
                "invested": 0,
                "market_value": 0,
                "equity": self.broker.cash,
                "realized_pnl": 0,
                "unrealized_pnl": 0,
            },
            "positions": positions,
            "quotes": quotes,
            "decisions": decisions,
            "suggestions": suggestions,
            "orders": orders,
            "equity_curve": self.db.recent_equity(120),
            "strategy_metrics": self.db.strategy_metrics(),
            "sentiment": self.db.latest_sentiment(40),
            "universe_size": len(self.db.get_universe(enabled_only=True)),
            "market_health": self._market_health(quotes),
            "macro_context": self.db.get_state("macro_context", {}),
            "institutional_context": self.db.get_state("institutional_context", {}),
        }

    def _suggestions(self, decisions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        latest_by_symbol: dict[str, dict[str, Any]] = {}
        for decision in decisions:
            latest_by_symbol.setdefault(decision["symbol"], decision)

        suggestions: list[dict[str, Any]] = []
        for decision in latest_by_symbol.values():
            audit = _json_object(decision.get("details_json"))
            context = audit.get("context") or {}
            full = context.get("full_spectrum_analysis") or {}
            confluence = full.get("confluence_score") or {}
            trade_plan = full.get("trade_plan") or {}
            signal_plan = full.get("signal_plan") or {}
            institutional = full.get("institutional_flow") or {}
            risk = full.get("risk_overrides") or {}
            combined = float((audit.get("score_breakdown") or {}).get("combined", 0.0) or 0.0)
            confluence_total = int(confluence.get("total", 0) or 0)
            suggestion = (
                "BUY"
                if decision.get("action") == "BUY"
                else "WATCH"
                if confluence_total >= 10 and combined >= 0
                else "NO_TRADE"
            )
            suggestions.append(
                {
                    "symbol": decision["symbol"],
                    "suggestion": suggestion,
                    "action": decision.get("action"),
                    "price": decision.get("price"),
                    "strategy": decision.get("strategy"),
                    "confidence": decision.get("confidence"),
                    "combined_score": round(combined, 4),
                    "confluence": confluence_total,
                    "tier": confluence.get("tier", "NO_SIGNAL"),
                    "decision_readiness": signal_plan.get("decision_readiness", "monitor_only"),
                    "entry_zone": trade_plan.get("entry_zone"),
                    "stop_loss": trade_plan.get("stop_loss"),
                    "targets": trade_plan.get("targets", []),
                    "exit_plan": _exit_plan_from_trade_plan(trade_plan, full.get("monitoring_checklist", [])),
                    "institutional_flags": institutional.get("symbol_flags", {}),
                    "institutional_bias": (institutional.get("market_bias") or {}).get("score"),
                    "risk_flags": risk.get("flags", []),
                    "reason": audit.get("action_reason") or decision.get("reason"),
                    "details_json": decision.get("details_json"),
                }
            )
        suggestions.sort(
            key=lambda row: (
                {"BUY": 3, "WATCH": 2, "NO_TRADE": 1}.get(row["suggestion"], 0),
                float(row.get("combined_score") or 0),
                int(row.get("confluence") or 0),
                float(row.get("institutional_bias") or 0),
                float(row.get("confidence") or 0),
            ),
            reverse=True,
        )
        return suggestions[:5]

    def _positions_with_exit_plans(
        self,
        positions: list[dict[str, Any]],
        orders: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        exit_plans: dict[str, dict[str, Any]] = {}
        for order in orders:
            if order.get("side") != "BUY" or order.get("status") != "FILLED":
                continue
            audit = _json_object(order.get("details_json"))
            decision = audit.get("decision") or {}
            details = decision.get("details") or {}
            full = ((details.get("context") or {}).get("full_spectrum_analysis") or {})
            trade_plan = full.get("trade_plan") or {}
            if trade_plan:
                exit_plans.setdefault(
                    order["symbol"],
                    _exit_plan_from_trade_plan(trade_plan, full.get("monitoring_checklist", [])),
                )
        return [{**position, "exit_plan": exit_plans.get(position["symbol"], {})} for position in positions]

    async def _loop(self) -> None:
        while self._running:
            try:
                await self.run_once()
                self._last_error = None
            except asyncio.TimeoutError:
                self._last_error = f"Cycle timed out after {self.cycle_timeout_seconds}s during {self._cycle_phase}"
                self._cycle_started_at = None
                self._cycle_phase = "idle"
                if self.on_update:
                    await self.on_update(self.snapshot())
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._last_error = f"{exc.__class__.__name__}: {exc}"
                self._cycle_started_at = None
                self._cycle_phase = "idle"
                if self.on_update:
                    await self.on_update(self.snapshot())
            await asyncio.sleep(self.interval_seconds)

    def _merge_risk_exits(self, decisions: list[Decision], exits: list[Decision]) -> list[Decision]:
        by_symbol = {decision.symbol: decision for decision in decisions}
        for decision in exits:
            by_symbol[decision.symbol] = decision
        return list(by_symbol.values())

    def _execute_top_decisions(self, decisions: list[Decision], equity: float) -> None:
        candidates = [decision for decision in decisions if decision.action != "HOLD"]
        candidates.sort(key=lambda decision: decision.confidence, reverse=True)
        for decision in candidates[: self.broker.settings.max_trades_per_cycle]:
            self.broker.execute(decision, equity)

    def _market_health(self, quotes: list[dict[str, Any]]) -> dict[str, Any]:
        latest_age: float | None = None
        latest_ts: str | None = None
        for quote in quotes:
            ts = quote.get("ts")
            if not ts:
                continue
            try:
                parsed = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            except ValueError:
                continue
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            age = max((datetime.now(timezone.utc) - parsed).total_seconds(), 0)
            if latest_age is None or age < latest_age:
                latest_age = age
                latest_ts = str(ts)
        provider = self.market_data.source_name
        if "live" in provider:
            mode = "live"
        elif "delayed" in provider:
            mode = "delayed"
        elif "simulated" in provider:
            mode = "simulated"
        else:
            mode = "external"
        return {
            "provider": provider,
            "mode": mode,
            "quote_count": len(quotes),
            "latest_quote_at": latest_ts,
            "latest_quote_age_seconds": round(latest_age, 1) if latest_age is not None else None,
        }


def _json_object(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}") if isinstance(value, str) else value
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        return {}


def _exit_plan_from_trade_plan(
    trade_plan: dict[str, Any],
    monitoring_checklist: list[str] | None = None,
) -> dict[str, Any]:
    targets = trade_plan.get("targets") or []
    t1 = targets[0] if targets else {}
    t2 = targets[1] if len(targets) > 1 else {}
    t3 = targets[2] if len(targets) > 2 else {}
    return {
        "horizon": trade_plan.get("horizon", "swing_3_to_7_days"),
        "entry_zone": trade_plan.get("entry_zone"),
        "stop_loss": trade_plan.get("stop_loss"),
        "target_1": t1,
        "target_2": t2,
        "target_3": t3,
        "invalidation": trade_plan.get("invalidation", {}),
        "monitoring_checklist": monitoring_checklist or [],
        "plan": (
            "Exit immediately on hard stop/invalidation. Take partial profit or tighten stop near T1, "
            "trail after T1, and reassess at T2/T3 or on negative news/global risk shift."
        ),
    }
