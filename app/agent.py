from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

from .db import Database
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
        interval_seconds: int,
        on_update: UpdateCallback | None = None,
    ) -> None:
        self.db = db
        self.market_data = market_data
        self.broker = broker
        self.strategy = strategy
        self.macro = macro
        self.interval_seconds = interval_seconds
        self.on_update = on_update
        self._task: asyncio.Task | None = None
        self._running = False
        self._last_error: str | None = None
        self._last_cycle_at: str | None = None

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
        universe = self.db.get_universe(enabled_only=True)
        quotes = await self.market_data.get_quotes(universe)
        if not quotes:
            raise MarketDataError(f"{self.market_data.source_name} returned no quotes for the enabled universe")
        candles = await self.market_data.get_candles(universe)
        self.db.upsert_quotes(quotes)
        self.db.upsert_candles(candles)
        self.broker.sync_marks(quotes)
        portfolio = self.broker.snapshot()
        positions = self.broker.positions_by_symbol()
        macro_context = await self.macro.context_for_cycle() if self.macro else {}
        self.db.set_state("macro_context", macro_context)
        decisions = await self.strategy.evaluate(universe, quotes, positions, candles, macro_context)
        risk_exits = self.strategy.stop_or_take_profit_exits(quotes, positions)
        decisions = self._merge_risk_exits(decisions, risk_exits)
        self.db.insert_decisions(decisions)
        self._execute_top_decisions(decisions, portfolio["equity"])
        portfolio = self.broker.snapshot()
        self._last_cycle_at = utc_now()
        snapshot = self.snapshot()
        if self.on_update:
            await self.on_update(snapshot)
        return snapshot

    def snapshot(self) -> dict[str, Any]:
        quotes = self.db.latest_quotes()
        positions = self.db.positions()
        decisions = self.db.latest_decisions(80)
        orders = self.db.latest_orders(80)
        return {
            "running": self.running,
            "provider": self.market_data.source_name,
            "last_error": self._last_error,
            "last_cycle_at": self._last_cycle_at,
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
            "orders": orders,
            "equity_curve": self.db.recent_equity(120),
            "strategy_metrics": self.db.strategy_metrics(),
            "sentiment": self.db.latest_sentiment(40),
            "universe_size": len(self.db.get_universe(enabled_only=True)),
            "market_health": self._market_health(quotes),
            "macro_context": self.db.get_state("macro_context", {}),
        }

    async def _loop(self) -> None:
        while self._running:
            try:
                await self.run_once()
                self._last_error = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._last_error = f"{exc.__class__.__name__}: {exc}"
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
