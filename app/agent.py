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
        delivery_service: Any | None,
        market_breadth: Any | None,
        sector_rotation: Any | None,
        macro_calendar: Any | None,
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
        self.delivery_service = delivery_service
        self.market_breadth = market_breadth
        self.sector_rotation = sector_rotation
        self.macro_calendar = macro_calendar
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
        self._log("INFO", "agent", "start", "Agent loop started", {"interval_seconds": self.interval_seconds})

    async def stop(self) -> None:
        self._running = False
        self._log("INFO", "agent", "stop_requested", "Agent loop stop requested")
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
        self._log("INFO", "cycle", "cycle_start", "Agent cycle started", {"universe_size": len(universe)})
        quotes = await self.market_data.get_quotes(universe)
        if not quotes:
            raise MarketDataError(f"{self.market_data.source_name} returned no quotes for the enabled universe")
        self._log(
            "INFO",
            "market_data",
            "quotes_fetched",
            f"Fetched {len(quotes)} quotes from {self.market_data.source_name}",
            {"provider": self.market_data.source_name, "quote_count": len(quotes)},
        )
        self._cycle_phase = "candles"
        candles = await self.market_data.get_candles(universe)
        candle_counts = {symbol: len(items) for symbol, items in candles.items()}
        candle_sources: dict[str, int] = {}
        for items in candles.values():
            for candle in items[:1]:
                candle_sources[candle.source] = candle_sources.get(candle.source, 0) + 1
        self._log(
            "INFO",
            "market_data",
            "candles_fetched",
            f"Fetched candles for {len(candles)} symbols",
            {
                "provider": self.market_data.source_name,
                "symbols_with_candles": len(candles),
                "total_candles": sum(candle_counts.values()),
                "source_counts": candle_sources,
                "sample_counts": dict(list(candle_counts.items())[:10]),
            },
        )
        self._cycle_phase = "persist_market_data"
        self.db.upsert_quotes(quotes)
        self.db.upsert_candles(candles)
        self.broker.sync_marks(quotes)
        portfolio = self.broker.snapshot()
        positions = self.broker.positions_by_symbol()
        self._log(
            "INFO",
            "portfolio",
            "portfolio_marked",
            "Portfolio marks updated",
            {"cash": portfolio.get("cash"), "equity": portfolio.get("equity"), "open_positions": len(positions)},
        )
        self._cycle_phase = "global_intelligence"
        macro_context = await self.macro.context_for_cycle() if self.macro else {}
        self.db.set_state("macro_context", macro_context)
        self._log(
            "INFO",
            "macro",
            "global_context",
            "Global intelligence refreshed",
            {
                "regime": macro_context.get("regime"),
                "risk_score": macro_context.get("risk_score"),
                "confidence": macro_context.get("confidence"),
            },
        )
        self._cycle_phase = "institutional_feeds"
        institutional_context = (
            await self.institutional_feeds.context_for_cycle(universe)
            if self.institutional_feeds
            else {}
        )
        self.db.set_state("institutional_context", institutional_context)
        self._log(
            "INFO",
            "feeds",
            "institutional_context",
            "Free institutional context refreshed",
            {
                "source_quality": institutional_context.get("source_quality"),
                "market_bias": institutional_context.get("market_bias"),
                "data_gaps": institutional_context.get("data_gaps", [])[:8],
            },
        )
        self._cycle_phase = "delivery_data"
        delivery_status = await self.delivery_service.ensure_data_current() if self.delivery_service else {}
        self.db.set_state("delivery_data_status", delivery_status)
        self._cycle_phase = "market_breadth"
        market_breadth_context = (
            await self.market_breadth.compute_breadth(universe, quotes, candles)
            if self.market_breadth
            else {}
        )
        self.db.set_state("market_breadth_context", market_breadth_context)
        self._cycle_phase = "sector_rotation"
        sector_rotation_context = (
            await self.sector_rotation.compute_sector_scores(universe, quotes, candles)
            if self.sector_rotation
            else {}
        )
        self.db.set_state("sector_rotation_context", sector_rotation_context)
        self._cycle_phase = "macro_calendar"
        macro_calendar_context = (
            await self.macro_calendar.event_context_for_cycle()
            if self.macro_calendar
            else {}
        )
        self.db.set_state("macro_calendar_context", macro_calendar_context)
        self._cycle_phase = "strategy_and_llm"
        decisions = await self.strategy.evaluate(
            universe,
            quotes,
            positions,
            candles,
            macro_context,
            institutional_context,
            self.delivery_service,
            market_breadth_context,
            sector_rotation_context,
            self.macro_calendar,
        )
        action_counts: dict[str, int] = {}
        decision_paths: dict[str, int] = {}
        llm_error_count = 0
        for decision in decisions:
            action_counts[decision.action] = action_counts.get(decision.action, 0) + 1
            audit = _json_object(decision.details_json)
            path = str(audit.get("decision_path") or "unknown")
            decision_paths[path] = decision_paths.get(path, 0) + 1
            if audit.get("llm_error"):
                llm_error_count += 1
        self._log(
            "INFO",
            "strategy",
            "decisions_created",
            f"Created {len(decisions)} decisions",
            {
                "action_counts": action_counts,
                "decision_paths": decision_paths,
                "llm_error_count": llm_error_count,
            },
        )
        self._cycle_phase = "risk_and_execution"
        risk_exits = self.strategy.stop_or_take_profit_exits(quotes, positions, candles)
        decisions = self._merge_risk_exits(decisions, risk_exits)
        self.db.insert_decisions(decisions)
        executed_count = self._execute_top_decisions(decisions, portfolio["equity"])
        portfolio = self.broker.snapshot()
        self._last_cycle_at = utc_now()
        self._last_cycle_duration_seconds = round((datetime.now(timezone.utc) - started).total_seconds(), 3)
        self._cycle_started_at = None
        self._cycle_phase = "idle"
        self._log(
            "INFO",
            "cycle",
            "cycle_complete",
            f"Agent cycle completed in {self._last_cycle_duration_seconds}s",
            {
                "duration_seconds": self._last_cycle_duration_seconds,
                "decisions": len(decisions),
                "risk_exits": len(risk_exits),
                "executed_orders": executed_count,
                "equity": portfolio.get("equity"),
            },
        )
        snapshot = self.snapshot()
        if self.on_update:
            await self.on_update(snapshot)
        return snapshot

    def snapshot(self) -> dict[str, Any]:
        quotes = self.db.latest_quotes()
        decisions = _with_detail_urls(self.db.latest_decision_summaries(80), "decisions")
        suggestion_decisions = self.db.latest_decisions(240)
        orders = _with_detail_urls(self.db.latest_order_summaries(80), "orders")
        order_audit_history = self.db.latest_orders(240)
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
            "market_breadth": self.db.get_state("market_breadth_context", {}),
            "sector_rotation_context": _sector_rotation_summary(self.db.get_state("sector_rotation_context", {})),
            "upcoming_macro_events": (self.db.get_state("macro_calendar_context", {}) or {}).get("next_10", []),
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
                    "id": decision.get("id"),
                    "detail_url": f"/api/decisions/{decision.get('id')}",
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
                self._log(
                    "ERROR",
                    "cycle",
                    "cycle_timeout",
                    self._last_error,
                    {"phase": self._cycle_phase, "timeout_seconds": self.cycle_timeout_seconds},
                )
                self._cycle_started_at = None
                self._cycle_phase = "idle"
                if self.on_update:
                    await self.on_update(self.snapshot())
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._last_error = f"{exc.__class__.__name__}: {exc}"
                self._log(
                    "ERROR",
                    "cycle",
                    "cycle_error",
                    self._last_error,
                    {"phase": self._cycle_phase, "error_type": exc.__class__.__name__},
                )
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

    def _execute_top_decisions(self, decisions: list[Decision], equity: float) -> int:
        candidates = [decision for decision in decisions if decision.action != "HOLD"]
        candidates.sort(key=lambda decision: decision.confidence, reverse=True)
        executed = 0
        for decision in candidates[: self.broker.settings.max_trades_per_cycle]:
            result = self.broker.execute(decision, equity)
            executed += 1 if result else 0
            self._log(
                "INFO",
                "execution",
                "order_attempt",
                f"{decision.action} {decision.symbol} {decision.strategy}: {'executed' if result else 'not executed'}",
                {
                    "symbol": decision.symbol,
                    "action": decision.action,
                    "strategy": decision.strategy,
                    "confidence": decision.confidence,
                    "price": decision.price,
                    "executed": result,
                },
            )
        if not candidates:
            self._log("INFO", "execution", "no_trade_actions", "No BUY/SELL candidates this cycle")
        return executed

    def _log(
        self,
        level: str,
        component: str,
        event: str,
        message: str,
        details: Any | None = None,
    ) -> None:
        try:
            self.db.insert_agent_log(level, component, event, message, details)
        except Exception:
            pass

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


def _with_detail_urls(rows: list[dict[str, Any]], collection: str) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["detail_url"] = f"/api/{collection}/{item.get('id')}"
        output.append(item)
    return output


def _sector_rotation_summary(context: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(context, dict):
        return {}
    leaderboard = context.get("leaderboard") or {}
    return {
        "enabled": context.get("enabled"),
        "updated_at": context.get("updated_at"),
        "nifty_proxy_return_20d": context.get("nifty_proxy_return_20d"),
        "leaderboard": {
            "top": (leaderboard.get("top") or [])[:3],
            "bottom": (leaderboard.get("bottom") or [])[:3],
        },
    }


def _exit_plan_from_trade_plan(
    trade_plan: dict[str, Any],
    monitoring_checklist: list[str] | None = None,
) -> dict[str, Any]:
    targets = _monotonic_targets(trade_plan.get("targets") or [])
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


def _monotonic_targets(targets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized = [dict(target) for target in targets if isinstance(target, dict)]
    if len(normalized) < 3:
        return normalized
    t1_price = _float_or_none(normalized[0].get("price"))
    t2_price = _float_or_none(normalized[1].get("price"))
    t3_price = _float_or_none(normalized[2].get("price"))
    if t2_price is None or t3_price is None or t3_price > t2_price:
        return normalized
    risk_step = (t2_price - t1_price) if t1_price is not None and t2_price > t1_price else max(t2_price * 0.01, 0.01)
    original = dict(normalized[2])
    normalized[2] = {
        **original,
        "price": round(t2_price + risk_step, 3),
        "rr": original.get("rr") if original.get("rr") != "structure" else "3.5_or_structure",
        "structure_reference": original.get("structure_reference", t3_price),
        "note": "normalized so target ladder stays above T2; original structure target is retained as reference",
    }
    return normalized


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
