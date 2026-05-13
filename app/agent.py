from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from .db import Database
from .institutional_feeds import FreeInstitutionalFeedsService
from .macro import GlobalIntelligenceService
from .market_data import MarketDataError, MarketDataProvider
from .market_regions import market_region_for_row
from .models import Decision, utc_now
from .paper_broker import PaperBroker
from .strategy import StrategyEngine
from .trading_rules import build_position_summary, build_self_audit


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
        options_intelligence: Any | None,
        interval_seconds: int,
        cycle_timeout_seconds: int,
        market_region: str = "IN",
        universe_symbols_per_cycle: int = 0,
        execute_trades: bool = True,
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
        self.options_intelligence = options_intelligence
        self.interval_seconds = interval_seconds
        self.cycle_timeout_seconds = max(30, cycle_timeout_seconds)
        self.market_region = market_region
        self.universe_symbols_per_cycle = max(0, int(universe_symbols_per_cycle or 0))
        self.execute_trades = execute_trades
        self.on_update = on_update
        self._task: asyncio.Task | None = None
        self._running = False
        self._last_error: str | None = None
        self._last_cycle_at: str | None = None
        self._cycle_started_at: str | None = None
        self._cycle_phase = "idle"
        self._last_cycle_duration_seconds: float | None = None
        self._universe_cursor = 0
        self._last_candle_fetch_at: dict[str, datetime] = {}

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
        return await self._run_once_inner()

    async def _run_once_inner(self) -> dict[str, Any]:
        started = datetime.now(timezone.utc)
        self._cycle_started_at = started.isoformat()
        self._cycle_phase = "market_quotes"
        full_universe = self.db.get_universe(enabled_only=True, market_region=self.market_region)
        pre_positions = self.broker.positions_by_symbol()
        universe = self._cycle_universe(full_universe, pre_positions)
        self._log(
            "INFO",
            "cycle",
            "cycle_start",
            "Agent cycle started",
            {
                "enabled_universe_size": len(full_universe),
                "market_region": self.market_region,
                "cycle_universe_size": len(universe),
                "symbols_per_cycle": self.universe_symbols_per_cycle,
            },
        )
        quotes = await self.market_data.get_quotes(universe)
        if not quotes:
            raise MarketDataError(f"{self.market_data.source_name} returned no quotes for the enabled universe")
        self._log(
            "INFO",
            "market_data",
            "quotes_fetched",
            f"Fetched {len(quotes)} quotes from {self.market_data.source_name}",
            {
                "provider": self.market_data.source_name,
                "quote_count": len(quotes),
                "source_counts": _source_counts(quotes),
                "provider_diagnostics": _market_data_diagnostics(self.market_data),
            },
        )
        self._cycle_phase = "candles"
        cached_sets_before = self.db.recent_candle_sets_by_symbol([row["symbol"] for row in universe])
        candle_fetch_universe, candle_fetch_plan = self._candle_fetch_universe(universe, cached_sets_before)
        fresh_candles = await self.market_data.get_candles(candle_fetch_universe) if candle_fetch_universe else {}
        fetched_at = datetime.now(timezone.utc)
        for row in candle_fetch_universe:
            self._last_candle_fetch_at[row["symbol"]] = fetched_at
        candle_counts = {symbol: len(items) for symbol, items in fresh_candles.items()}
        candle_sources: dict[str, int] = {}
        for items in fresh_candles.values():
            for candle in items:
                candle_sources[candle.source] = candle_sources.get(candle.source, 0) + 1
        self._log(
            "INFO",
            "market_data",
            "candles_fetched",
            f"Fetched candles for {len(fresh_candles)} symbols",
            {
                "provider": self.market_data.source_name,
                "fetch_plan": candle_fetch_plan,
                "requested_fetch_symbols": len(candle_fetch_universe),
                "cached_symbols_reused": candle_fetch_plan.get("cache_ready", 0) + candle_fetch_plan.get("recently_attempted", 0),
                "symbols_with_candles": len(fresh_candles),
                "total_candles": sum(candle_counts.values()),
                "source_counts": candle_sources,
                "sample_counts": dict(list(candle_counts.items())[:10]),
                "provider_diagnostics": _market_data_diagnostics(self.market_data),
            },
        )
        self._cycle_phase = "persist_market_data"
        self.db.upsert_quotes(quotes)
        self.db.upsert_candles(fresh_candles)
        candle_sets = self.db.recent_candle_sets_by_symbol([row["symbol"] for row in universe])
        candles = {
            symbol: sets.get("analysis") or sets.get("daily") or sets.get("intraday") or []
            for symbol, sets in candle_sets.items()
        }
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
        self._cycle_phase = "options_intelligence"
        options_context = (
            await self.options_intelligence.context_for_cycle(universe, quotes)
            if self.options_intelligence
            else {}
        )
        self.db.set_state("options_intelligence_context", options_context)
        self._cycle_phase = "delivery_data"
        delivery_status = await self.delivery_service.ensure_data_current() if self.delivery_service else {}
        self.db.set_state("delivery_data_status", delivery_status)
        self._cycle_phase = "market_breadth"
        market_breadth_context = (
            await self._market_breadth_by_region(universe, quotes, candles)
            if self.market_breadth
            else {}
        )
        self.db.set_state("market_breadth_context", market_breadth_context)
        self._cycle_phase = "sector_rotation"
        sector_rotation_context = (
            await self._sector_rotation_by_region(universe, quotes, candles)
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
        self._cycle_phase = "self_audit"
        quote_rows = [quote.to_dict() for quote in quotes.values()]
        market_health = self._market_health(quote_rows)
        market_health["portfolio_equity"] = portfolio.get("equity")
        self_audit = build_self_audit(list(positions.values()), quote_rows, portfolio, market_health, macro_calendar_context)
        self.db.set_state("self_audit", self_audit)
        self._log(
            "INFO",
            "rules",
            "self_audit_completed",
            "System rule self-audit completed before strategy evaluation",
            {
                "overall_score_pct": self_audit.get("overall_score_pct"),
                "overall_grade": self_audit.get("overall_grade"),
                "grade_violation_count": self_audit.get("grade_violation_count"),
                "delivery_conflict_count": self_audit.get("delivery_conflict_count"),
                "price_mismatch_count": self_audit.get("price_mismatch_count"),
                "earnings_calendar_last_updated": self_audit.get("earnings_calendar_last_updated"),
                "speculative_pct_of_open_positions": self_audit.get("speculative_pct_of_open_positions"),
                "capital_pool_within_position_count_rule": self_audit.get("capital_pool_within_position_count_rule"),
            },
        )
        self._cycle_phase = "strategy_and_llm"
        decisions = await asyncio.to_thread(
            lambda: asyncio.run(
                self.strategy.evaluate(
                    universe,
                    quotes,
                    positions,
                    candles,
                    macro_context,
                    institutional_context,
                    options_context,
                    self.delivery_service,
                    market_breadth_context,
                    sector_rotation_context,
                    self.macro_calendar,
                    candle_sets,
                    portfolio.get("equity"),
                )
            )
        )
        action_counts: dict[str, int] = {}
        decision_paths: dict[str, int] = {}
        llm_error_count = 0
        for decision in decisions:
            action_counts[decision.action] = action_counts.get(decision.action, 0) + 1
            audit = _json_object(decision.details_json)
            path = str(audit.get("decision_path") or "unknown")
            decision_paths[path] = decision_paths.get(path, 0) + 1
            if audit.get("llm_error") or audit.get("json_synthetic") or audit.get("llm_timeout"):
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
        self.db.upsert_signal_ideas_from_decisions(decisions)
        executed_count = self._execute_top_decisions(decisions, portfolio["equity"]) if self.execute_trades else 0
        if not self.execute_trades:
            self._log(
                "INFO",
                "execution",
                "admin_execution_disabled",
                "Global admin cycle is analysis-only; users own signal execution.",
                {"decisions": len(decisions)},
            )
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

    def _candle_fetch_universe(
        self,
        universe: list[dict[str, Any]],
        cached_sets: dict[str, dict[str, list[Any]]],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        now = datetime.now(timezone.utc)
        minimum_ready_candles = 60
        retry_cooldown = timedelta(hours=6)
        stale_after = timedelta(hours=20)
        selected: list[dict[str, Any]] = []
        stats = {
            "minimum_ready_candles": minimum_ready_candles,
            "missing_or_short_history": 0,
            "stale_history": 0,
            "cache_ready": 0,
            "recently_attempted": 0,
        }
        for row in universe:
            symbol = row["symbol"]
            sets = cached_sets.get(symbol) or {}
            candles = sets.get("analysis") or sets.get("daily") or sets.get("intraday") or []
            last_attempt = self._last_candle_fetch_at.get(symbol)
            if last_attempt and now - last_attempt < retry_cooldown and len(candles) >= 30:
                stats["recently_attempted"] += 1
                continue
            if len(candles) < minimum_ready_candles:
                stats["missing_or_short_history"] += 1
                selected.append(row)
                continue
            if self._candles_are_stale(candles, now, stale_after):
                stats["stale_history"] += 1
                selected.append(row)
                continue
            stats["cache_ready"] += 1
        stats["fetch_symbols"] = len(selected)
        return selected, stats

    def _candles_are_stale(self, candles: list[Any], now: datetime, stale_after: timedelta) -> bool:
        if not candles:
            return True
        ts = getattr(candles[-1], "ts", None)
        parsed = _parse_iso_datetime(ts)
        if parsed is None:
            return True
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return now - parsed.astimezone(timezone.utc) > stale_after

    def _cycle_universe(
        self,
        full_universe: list[dict[str, Any]],
        positions: dict[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        limit = self.universe_symbols_per_cycle
        if limit <= 0 or limit >= len(full_universe):
            return full_universe
        if not full_universe:
            return []
        start = self._universe_cursor % len(full_universe)
        selected = [full_universe[(start + index) % len(full_universe)] for index in range(limit)]
        self._universe_cursor = (start + limit) % len(full_universe)
        selected_symbols = {row["symbol"] for row in selected}
        if positions:
            for row in full_universe:
                if row["symbol"] in positions and row["symbol"] not in selected_symbols:
                    selected.append(row)
                    selected_symbols.add(row["symbol"])
        return selected

    async def _market_breadth_by_region(
        self,
        universe: list[dict[str, Any]],
        quotes: dict[str, Any],
        candles: dict[str, Any],
    ) -> dict[str, Any]:
        if self.market_region != "BOTH":
            return await self.market_breadth.compute_breadth(universe, quotes, candles, market_region=self.market_region)
        by_market: dict[str, Any] = {}
        for region in ("IN", "US"):
            rows = [row for row in universe if market_region_for_row(row) == region]
            by_market[region] = await self.market_breadth.compute_breadth(rows, quotes, candles, market_region=region)
        return {
            "enabled": True,
            "market_region": "BOTH",
            "by_market": by_market,
            "breadth_regime": by_market.get("IN", {}).get("breadth_regime", "neutral"),
            "data_note": "Use by_market.IN or by_market.US for symbol-level decisions.",
        }

    async def _sector_rotation_by_region(
        self,
        universe: list[dict[str, Any]],
        quotes: dict[str, Any],
        candles: dict[str, Any],
    ) -> dict[str, Any]:
        if self.market_region != "BOTH":
            return await self.sector_rotation.compute_sector_scores(universe, quotes, candles, market_region=self.market_region)
        by_market: dict[str, Any] = {}
        for region in ("IN", "US"):
            rows = [row for row in universe if market_region_for_row(row) == region]
            by_market[region] = await self.sector_rotation.compute_sector_scores(rows, quotes, candles, market_region=region)
        return {
            "enabled": True,
            "market_region": "BOTH",
            "by_market": by_market,
            "leaderboard": by_market.get("IN", {}).get("leaderboard", {"top": [], "bottom": []}),
            "data_note": "Use by_market.IN or by_market.US for symbol-level sector rotation.",
        }

    def snapshot(self) -> dict[str, Any]:
        quotes = self.db.latest_quotes()
        decisions = _with_detail_urls(self.db.latest_decision_summaries(80), "decisions")
        decisions_by_market = {
            "IN": _with_detail_urls(self.db.latest_decision_summaries(80, market_region="IN"), "decisions"),
            "US": _with_detail_urls(self.db.latest_decision_summaries(80, market_region="US"), "decisions"),
        }
        suggestion_decisions = self.db.latest_decisions(240)
        orders = _with_detail_urls(self.db.latest_order_summaries(80), "orders")
        order_audit_history = self.db.latest_orders(240)
        raw_positions = self.db.positions()
        portfolio = self.db.latest_portfolio() or {
            "cash": self.broker.cash,
            "invested": 0,
            "market_value": 0,
            "equity": self.broker.cash,
            "realized_pnl": 0,
            "unrealized_pnl": 0,
        }
        portfolio_by_market = self.broker.portfolio_by_market(raw_positions)
        market_health = self._market_health(quotes)
        market_health["portfolio_equity"] = portfolio.get("equity")
        macro_calendar_context = self.db.get_state("macro_calendar_context", {})
        positions = self._positions_with_exit_plans(raw_positions, order_audit_history, quotes, market_health, macro_calendar_context)
        suggestions = self.db.latest_signal_ideas(40)
        suggestions_by_market = {
            "IN": self.db.latest_signal_ideas(25, market_region="IN"),
            "US": self.db.latest_signal_ideas(25, market_region="US"),
        }
        universe_summary = self.db.universe_summary()
        options_context = self.db.get_state("options_intelligence_context", {})
        self_audit = self.db.get_state("self_audit")
        if not self_audit or "overall_score_pct" not in self_audit:
            self_audit = build_self_audit(
                raw_positions,
                quotes,
                portfolio,
                market_health,
                macro_calendar_context,
            )
        return {
            "running": self.running,
            "provider": self.market_data.source_name,
            "last_error": self._last_error,
            "last_cycle_at": self._last_cycle_at,
            "universe": {
                "enabled": universe_summary.get("enabled"),
                "total": universe_summary.get("total"),
                "market_region": self.market_region,
                "india_enabled": universe_summary.get("india_enabled"),
                "us_enabled": universe_summary.get("us_enabled"),
                "low_price_enabled": universe_summary.get("low_price_enabled"),
                "symbols_per_cycle": self.universe_symbols_per_cycle,
            },
            "cycle": {
                "phase": self._cycle_phase,
                "started_at": self._cycle_started_at,
                "timeout_seconds": self.cycle_timeout_seconds,
                "last_duration_seconds": self._last_cycle_duration_seconds,
            },
            "portfolio": portfolio,
            "portfolio_by_market": portfolio_by_market,
            "positions": positions,
            "quotes": quotes,
            "decisions": decisions,
            "decisions_by_market": decisions_by_market,
            "suggestions": suggestions,
            "signal_ideas": suggestions,
            "suggestions_by_market": suggestions_by_market,
            "strategy_plans": self.db.strategy_plans(),
            "orders": orders,
            "equity_curve": self.db.recent_equity(120),
            "strategy_metrics": self.db.strategy_metrics(),
            "performance": self.db.performance_summary(),
            "sentiment": self.db.latest_sentiment(40),
            "universe_size": len(self.db.get_universe(enabled_only=True, market_region=self.market_region)),
            "market_health": market_health,
            "market_data_health": _market_data_diagnostics(self.market_data),
            "macro_context": self.db.get_state("macro_context", {}),
            "institutional_context": self.db.get_state("institutional_context", {}),
            "market_breadth": self.db.get_state("market_breadth_context", {}),
            "sector_rotation_context": _sector_rotation_summary(self.db.get_state("sector_rotation_context", {})),
            "options_intelligence": _options_intelligence_summary(options_context),
            "upcoming_macro_events": (macro_calendar_context or {}).get("next_10", []),
            "self_audit": self_audit,
            "llm_usage": self.db.llm_usage_summary(),
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
            system_audit = audit.get("system_gate_audit") or context.get("system_gate_audit") or {}
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
                    "overall_score_pct": system_audit.get("overall_score_pct"),
                    "overall_grade": system_audit.get("overall_grade"),
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
        quotes: list[dict[str, Any]] | None = None,
        market_health: dict[str, Any] | None = None,
        macro_calendar_context: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        quote_map = {str(row.get("symbol")): row for row in (quotes or [])}
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
        return [
            {
                **position,
                "exit_plan": exit_plans.get(position["symbol"], {}),
                "position_summary": build_position_summary(
                    position,
                    quote_map.get(str(position.get("symbol"))),
                    market_health,
                    macro_calendar_context,
                ),
            }
            for position in positions
        ]

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
            ts = quote.get("ts") or quote.get("asof")
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
        quote_sources = _source_counts({str(index): quote for index, quote in enumerate(quotes)})
        market_open = _is_nse_regular_session_now()
        if "upstox" in provider and not market_open:
            mode = "last_traded"
        elif latest_age is not None and latest_age > 900 and "live" in provider:
            mode = "stale"
        elif "live" in provider:
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
            "display_label": "Last traded" if mode == "last_traded" else "Stale quote" if mode == "stale" else mode,
            "is_market_open": market_open,
            "quote_count": len(quotes),
            "quote_sources": quote_sources,
            "latest_quote_at": latest_ts,
            "latest_quote_age_seconds": round(latest_age, 1) if latest_age is not None else None,
        }


def _json_object(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}") if isinstance(value, str) else value
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        return {}


def _source_counts(items: dict[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items.values():
        if isinstance(item, dict):
            source = str(item.get("source") or "unknown")
        else:
            source = str(getattr(item, "source", None) or "unknown")
        counts[source] = counts.get(source, 0) + 1
    return counts


def _market_data_diagnostics(provider: MarketDataProvider) -> dict[str, Any]:
    diagnostics: dict[str, Any] = {"provider": getattr(provider, "source_name", "unknown")}
    primary = getattr(provider, "primary", None)
    fallback = getattr(provider, "fallback", None)
    if primary is not None:
        diagnostics["primary"] = {
            "provider": getattr(primary, "source_name", "unknown"),
            "last_quote_diagnostics": getattr(primary, "last_quote_diagnostics", None),
            "last_candle_diagnostics": getattr(primary, "last_candle_diagnostics", None),
        }
    if fallback is not None:
        diagnostics["fallback"] = {"provider": getattr(fallback, "source_name", "unknown")}
    own = getattr(provider, "last_quote_diagnostics", None)
    if own:
        diagnostics["last_quote_diagnostics"] = own
    candle_own = getattr(provider, "last_candle_diagnostics", None)
    if candle_own:
        diagnostics["last_candle_diagnostics"] = candle_own
    return diagnostics


def _is_nse_regular_session_now() -> bool:
    now_ist = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
    if now_ist.weekday() >= 5:
        return False
    current_minutes = now_ist.hour * 60 + now_ist.minute
    return (9 * 60 + 15) <= current_minutes <= (15 * 60 + 30)


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
    by_market = context.get("by_market") or {}
    if isinstance(by_market, dict) and by_market:
        return {
            "enabled": context.get("enabled"),
            "updated_at": context.get("updated_at"),
            "market_region": context.get("market_region"),
            "by_market": {
                region: _sector_rotation_summary(value)
                for region, value in by_market.items()
                if isinstance(value, dict)
            },
        }
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


def _options_intelligence_summary(context: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(context, dict):
        return {}
    symbols = context.get("symbols") or {}
    indices = context.get("indices") or {}
    suppressed = [
        {
            "symbol": symbol,
            "max_pain": item.get("max_pain"),
            "max_pain_distance_pct": item.get("max_pain_distance_pct"),
        }
        for symbol, item in symbols.items()
        if isinstance(item, dict) and item.get("buy_suppressed")
    ]
    return {
        "enabled": context.get("enabled"),
        "source": context.get("source"),
        "index_source": context.get("index_source"),
        "updated_at": context.get("updated_at"),
        "stock_symbols_checked": len(symbols),
        "stock_symbols_ok": sum(1 for item in symbols.values() if isinstance(item, dict) and item.get("status") == "ok"),
        "index_symbols_ok": sum(1 for item in indices.values() if isinstance(item, dict) and item.get("status") == "ok"),
        "buy_suppressed": suppressed[:10],
        "indices": indices,
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


def _parse_iso_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
