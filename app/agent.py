from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import is_dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any
from types import SimpleNamespace
from uuid import uuid4

from .decision_contract import normalize_trade_targets
from .decision_diagnostics import build_cycle_decision_diagnostics
from .db import Database
from .institutional_feeds import FreeInstitutionalFeedsService
from .llm_policy import LLM_HARD_DISABLED
from .llm_usage import credit_breakdown_for_usage
from .macro import GlobalIntelligenceService
from .market_action_radar import MarketActionRadar
from .market_day_regime import compute_market_day_regimes
from .market_data import MarketDataError, MarketDataProvider
from .market_regions import filter_universe_for_open_markets, market_region_for_row, market_session_context, normalize_market_region
from .models import Decision, Quote, utc_now
from .opportunity_scanner import OpportunityScanner
from .paper_broker import PaperBroker
from .pre_catalyst_engine import build_pre_catalyst_watchlist
from .rally_plan import build_rally_plan, build_rally_plan_by_market, extract_rally_plan_promotions
from .request_context import current_llm_usage_scope, current_user_id
from .signal_quality import (
    AUTO_FOLLOW_REENTRY_COOLDOWN_HOURS,
    FRESH_BUY_WINDOW_MINUTES,
    auto_follow_quality_gate,
    quality_size_multiplier,
    quality_skip_payload,
)
from .strategy import StrategyEngine
from .tomorrow_plan import build_tomorrow_plan
from .trade_economics import auto_follow_sizing
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
        openclaw_notifier: Any | None = None,
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
        self.openclaw_notifier = openclaw_notifier
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
        self._last_shared_auto_trade: dict[str, Any] = {}
        self._universe_cursor = 0
        self._news_probe_cursor = 0
        self._candle_backfill_cursor = 0
        self._last_candle_fetch_at: dict[str, datetime] = {}
        self.opportunity_scanner = OpportunityScanner(strategy.settings)
        self.market_action_radar = MarketActionRadar(strategy.settings)

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
        try:
            return await asyncio.wait_for(self._run_once_inner(), timeout=self.cycle_timeout_seconds)
        except asyncio.TimeoutError:
            return await self._handle_cycle_timeout()

    async def _handle_cycle_timeout(self) -> dict[str, Any]:
        if self._cycle_phase == "idle" and self._last_cycle_at:
            self._log(
                "WARN",
                "cycle",
                "post_cycle_timeout_after_complete",
                "Cycle work completed, but post-cycle callbacks exceeded the run_once timeout.",
                {"last_cycle_at": self._last_cycle_at, "timeout_seconds": self.cycle_timeout_seconds},
            )
            self._cycle_started_at = None
            return self.snapshot()
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
        snapshot = self.snapshot()
        if self.on_update:
            await self.on_update(snapshot)
        return snapshot

    async def _run_once_inner(self) -> dict[str, Any]:
        started = datetime.now(timezone.utc)
        self._cycle_started_at = started.isoformat()
        self._cycle_phase = "market_quotes"
        full_universe = self.db.get_universe(enabled_only=True, market_region=self.market_region)
        pre_positions = self.broker.positions_by_symbol()
        session_context = market_session_context(self.market_region, full_universe)
        self.db.set_state("market_session_context", session_context)
        scan_universe = full_universe
        if getattr(self.strategy.settings, "skip_market_data_when_closed", True):
            scan_universe = filter_universe_for_open_markets(full_universe, session_context)
            if not scan_universe:
                return await self._run_market_closed_prep(started, full_universe, pre_positions, session_context)
        dynamic_scan_enabled = bool(getattr(self.strategy.settings, "dynamic_opportunity_scan_enabled", True))
        raw_scan_limit = (
            max(0, int(getattr(self.strategy.settings, "dynamic_scan_raw_limit", 500) or 0))
            if dynamic_scan_enabled
            else None
        )
        sentiment_by_symbol: dict[str, dict[str, Any]] = {}
        raw_universe, raw_scan_policy = self._raw_scan_universe_for_cycle(
            scan_universe,
            pre_positions,
            dynamic_scan_enabled=dynamic_scan_enabled,
            raw_scan_limit=raw_scan_limit,
        )
        market_action_summary: dict[str, Any] = {"enabled": False, "reason": "dynamic_scan_disabled", "events": [], "events_by_symbol": {}}
        if dynamic_scan_enabled:
            self._cycle_phase = "market_action_radar"
            try:
                market_action_summary = await asyncio.wait_for(
                    self.market_action_radar.scan(scan_universe),
                    timeout=self._market_action_timeout_seconds(),
                )
            except asyncio.TimeoutError:
                timeout_error = f"market_action_radar_timeout_after_{self._market_action_timeout_seconds():g}s"
                cached_market_action = self._recent_market_action_summary()
                if cached_market_action:
                    market_action_summary = {
                        **cached_market_action,
                        "enabled": True,
                        "source": cached_market_action.get("source") or "market_action_radar",
                        "reused_due_to_timeout": True,
                        "errors": [*(cached_market_action.get("errors") or []), timeout_error],
                    }
                    timeout_event = "market_action_radar_timeout_reused_cache"
                    timeout_message = "Market-action radar timed out; reusing recent non-empty radar snapshot."
                else:
                    market_action_summary = {
                        "enabled": True,
                        "source": "market_action_radar",
                        "events": [],
                        "events_by_symbol": {},
                        "errors": [timeout_error],
                    }
                    timeout_event = "market_action_radar_timeout"
                    timeout_message = "Market-action radar timed out; continuing with configured raw scan universe."
                self._log(
                    "WARN",
                    "scanner",
                    timeout_event,
                    timeout_message,
                    {
                        "timeout_seconds": self._market_action_timeout_seconds(),
                        "phase": self._cycle_phase,
                        "reused_cached_events": int(len((market_action_summary.get("events_by_symbol") or {}))),
                        "cache_age_seconds": market_action_summary.get("cache_age_seconds"),
                    },
                )
            except Exception as exc:
                market_action_summary = {
                    "enabled": True,
                    "source": "market_action_radar",
                    "events": [],
                    "events_by_symbol": {},
                    "errors": [f"{exc.__class__.__name__}: {str(exc)[:220]}"],
                }
            self.db.set_state("market_action_radar", market_action_summary)
            raw_universe, market_action_policy = self._merge_market_action_universe(
                raw_universe,
                scan_universe,
                market_action_summary,
            )
            raw_scan_policy = {**raw_scan_policy, "market_action": market_action_policy}
        raw_universe, tomorrow_plan_quote_policy = self._merge_tomorrow_plan_universe(raw_universe, scan_universe)
        raw_scan_policy = {**raw_scan_policy, "tomorrow_plan_quote_force": tomorrow_plan_quote_policy}
        universe = raw_universe
        self._log(
            "INFO",
            "cycle",
            "cycle_start",
            "Agent cycle started",
            {
                "enabled_universe_size": len(full_universe),
                "open_scan_universe_size": len(scan_universe),
                "market_region": self.market_region,
                "open_regions": session_context.get("open_regions"),
                "closed_regions": session_context.get("closed_regions"),
                "market_data_policy": session_context.get("data_policy"),
                "raw_scan_universe_size": len(raw_universe),
                "symbols_per_cycle": self.universe_symbols_per_cycle,
                "dynamic_opportunity_scan_enabled": dynamic_scan_enabled,
                "dynamic_scan_raw_limit": raw_scan_limit,
                "raw_scan_policy": raw_scan_policy,
                "market_action_symbols": market_action_summary.get("symbols", []),
            },
        )
        quotes = await self.market_data.get_quotes(raw_universe)
        if not quotes:
            raise MarketDataError(f"{self.market_data.source_name} returned no quotes for the enabled universe")
        resolved_instruments = [row for row in raw_universe if row.get("upstox_instrument_key")]
        if resolved_instruments:
            self.db.upsert_universe_rows(resolved_instruments, disable_missing=False)
        self.db.upsert_quotes(quotes)
        self._log(
            "INFO",
            "market_data",
            "quotes_fetched",
            f"Fetched {len(quotes)} quotes from {self.market_data.source_name}",
            {
                "provider": self.market_data.source_name,
                "requested_symbols": len(raw_universe),
                "quote_count": len(quotes),
                "source_counts": _source_counts(quotes),
                "provider_diagnostics": _market_data_diagnostics(self.market_data),
            },
        )
        if dynamic_scan_enabled:
            self._cycle_phase = "opportunity_scan"
            raw_cached_sets = self.db.recent_candle_sets_by_symbol([row["symbol"] for row in raw_universe])
            news_probe_summary: dict[str, Any] = {"enabled": False, "reason": "sentiment_scan_disabled"}
            if bool(getattr(self.strategy.settings, "enable_news_sentiment", True)) and bool(
                getattr(self.strategy.settings, "dynamic_scan_sentiment_enabled", True)
            ):
                news_probe_rows = self._news_probe_universe(raw_universe, quotes, pre_positions)
                news_probe_rows = self._prepend_market_action_news_rows(
                    news_probe_rows,
                    raw_universe,
                    quotes,
                    market_action_summary,
                )
                if news_probe_rows:
                    try:
                        news_probe_summary = await asyncio.wait_for(
                            self.strategy.sentiment.refresh_watchlist_news(
                                news_probe_rows,
                                limit=len(news_probe_rows),
                                allow_llm=False,
                                reason="dynamic_opportunity_scan",
                            ),
                            timeout=self._news_probe_timeout_seconds(),
                        )
                    except asyncio.TimeoutError:
                        news_probe_summary = {
                            "enabled": True,
                            "reason": "dynamic_opportunity_scan_timeout",
                            "symbols_requested": len(news_probe_rows),
                            "symbols_refreshed": 0,
                            "events_found": 0,
                            "headlines_found": 0,
                            "timeout_seconds": self._news_probe_timeout_seconds(),
                        }
                        self._log(
                            "WARN",
                            "sentiment",
                            "news_probe_timeout",
                            "Dynamic scan news probe timed out; continuing with cached sentiment.",
                            {"timeout_seconds": self._news_probe_timeout_seconds(), "symbols_requested": len(news_probe_rows)},
                        )
                sentiment_by_symbol = self.db.latest_sentiment_by_symbol(
                    [row["symbol"] for row in raw_universe],
                    max_age_days=max(1, int(getattr(self.strategy.settings, "news_lookback_days", 7) or 7)),
                )
            scan_ready_universe = self._annotate_universe_with_cached_surveillance(raw_universe)
            scan_result = self.opportunity_scanner.rank(
                scan_ready_universe,
                quotes,
                raw_cached_sets,
                pre_positions,
                sentiment_by_symbol,
            )
            universe, tomorrow_plan_selected_policy = self._merge_tomorrow_plan_universe(
                scan_result.selected_universe,
                scan_universe,
            )
            eligible_prefetch_rows = [
                row
                for row in universe
                if _analysis_history_count(raw_cached_sets.get(str(row.get("symbol") or "").upper()) or {}) < 20
            ]
            prefetch_limit = self._opportunity_history_prefetch_limit()
            prefetch_rows = eligible_prefetch_rows[:prefetch_limit] if prefetch_limit > 0 else []
            history_prefetch_summary: dict[str, Any] = {
                "eligible_symbols": len(eligible_prefetch_rows),
                "requested_symbols": 0,
                "symbols_with_candles": 0,
                "reranked": False,
                "limit": prefetch_limit,
                "truncated": len(eligible_prefetch_rows) > len(prefetch_rows),
            }
            if prefetch_rows:
                self._cycle_phase = "opportunity_history_prefetch"
                prefetch_error = None
                prefetch_timeout = self._opportunity_history_prefetch_timeout_seconds()
                try:
                    prefetch_candles = await asyncio.wait_for(
                        self.market_data.get_candles(prefetch_rows),
                        timeout=prefetch_timeout,
                    )
                except asyncio.TimeoutError:
                    prefetch_candles = {}
                    prefetch_error = f"TimeoutError: history prefetch exceeded {prefetch_timeout:g}s"
                except Exception as exc:
                    prefetch_candles = {}
                    prefetch_error = f"{exc.__class__.__name__}: {str(exc)[:220]}"
                if prefetch_candles:
                    self.db.upsert_candles(prefetch_candles)
                    raw_cached_sets = self.db.recent_candle_sets_by_symbol([row["symbol"] for row in raw_universe])
                    scan_ready_universe = self._annotate_universe_with_cached_surveillance(raw_universe)
                    scan_result = self.opportunity_scanner.rank(
                        scan_ready_universe,
                        quotes,
                        raw_cached_sets,
                        pre_positions,
                        sentiment_by_symbol,
                    )
                    universe, tomorrow_plan_selected_policy = self._merge_tomorrow_plan_universe(
                        scan_result.selected_universe,
                        scan_universe,
                    )
                history_prefetch_summary = {
                    "eligible_symbols": len(eligible_prefetch_rows),
                    "requested_symbols": len(prefetch_rows),
                    "symbols_with_candles": len(prefetch_candles),
                    "reranked": bool(prefetch_candles),
                    "limit": prefetch_limit,
                    "truncated": len(eligible_prefetch_rows) > len(prefetch_rows),
                    "timeout_seconds": prefetch_timeout,
                    "sample_symbols": [row.get("symbol") for row in prefetch_rows[:12]],
                    "error": prefetch_error,
                }
            scan_summary = scan_result.summary
            scan_summary["market_action_radar"] = market_action_summary
            scan_summary["tomorrow_plan"] = tomorrow_plan_selected_policy
            scan_summary["news_probe"] = news_probe_summary
            scan_summary["history_prefetch"] = history_prefetch_summary
            scan_summary["enabled_universe_symbols"] = len(full_universe)
            scan_summary["open_universe_symbols"] = len(scan_universe)
            scan_summary["scanned_symbols_this_cycle"] = len(raw_universe)
            scan_summary["raw_scan_limit"] = raw_scan_limit
            scan_summary["scan_rotation_enabled"] = bool(raw_scan_policy.get("rotation_enabled"))
            scan_summary["raw_scan_policy"] = raw_scan_policy
            scan_summary["news_screened_symbols"] = int(news_probe_summary.get("symbols_requested") or 0)
            scan_summary["news_events_found"] = int(news_probe_summary.get("events_found") or 0)
            scan_summary["news_headlines_found"] = int(news_probe_summary.get("headlines_found") or 0)
            self._annotate_thin_history_diagnostics(universe, pre_positions, raw_cached_sets, scan_summary)
            if not universe:
                fallback_limit = min(
                    12,
                    max(1, int(getattr(self.strategy.settings, "dynamic_scan_candidate_limit", 60) or 60)),
                )
                universe = self._fallback_quoted_universe(raw_universe, quotes, pre_positions, fallback_limit)
                scan_summary = {
                    **scan_summary,
                    "fallback_reason": "no_symbols_passed_opportunity_quality_gate",
                    "selected_symbols": len(universe),
                    "top_candidates": [
                        {"symbol": row.get("symbol"), "bucket": "Watch", "setup": "fallback_quote_momentum_probe"}
                        for row in universe[:25]
                    ],
                }
            universe, trim_policy = self._trim_universe_to_decision_target(universe, pre_positions, scan_summary)
            if trim_policy.get("trimmed"):
                scan_summary["selected_symbols"] = len(universe)
                scan_summary["decision_target_trim"] = trim_policy
                kept_symbols = {str(row.get("symbol") or "").upper() for row in universe}
                scan_summary["top_candidates"] = [
                    item
                    for item in scan_summary.get("top_candidates", [])
                    if str(item.get("symbol") or "").upper() in kept_symbols
                ]
            scan_summary["by_market"] = _opportunity_scan_by_market(
                scan_summary,
                full_universe,
                scan_universe,
                raw_universe,
                universe,
                news_probe_summary,
            )
            self.db.set_state("opportunity_scan", scan_summary)
            self._log(
                "INFO",
                "scanner",
                "dynamic_opportunity_scan_completed",
                f"Opportunity scan selected {len(universe)} of {len(raw_universe)} raw symbols",
                {
                    "raw_symbols": len(raw_universe),
                    "quoted_symbols": len(quotes),
                    "selected_symbols": len(universe),
                    "candidate_limit": scan_summary.get("candidate_limit"),
                    "rejected_counts": scan_summary.get("rejected_counts"),
                    "setup_counts": scan_summary.get("setup_counts"),
                    "news_probe": scan_summary.get("news_probe"),
                    "top_symbols": [item.get("symbol") for item in scan_summary.get("top_candidates", [])[:12]],
                    "fallback_reason": scan_summary.get("fallback_reason"),
                },
            )
        else:
            static_summary = {
                "enabled": False,
                "mode": "static_cycle_universe",
                "scanned_at": utc_now(),
                "raw_symbols": len(raw_universe),
                "enabled_universe_symbols": len(full_universe),
                "open_universe_symbols": len(scan_universe),
                "scanned_symbols_this_cycle": len(raw_universe),
                "raw_scan_limit": raw_scan_limit,
                "scan_rotation_enabled": bool(
                    raw_scan_limit and raw_scan_limit > 0 and raw_scan_limit < len(scan_universe)
                ),
                "selected_symbols": len(universe),
            }
            static_summary["by_market"] = _opportunity_scan_by_market(
                static_summary,
                full_universe,
                scan_universe,
                raw_universe,
                universe,
                {},
            )
            self.db.set_state(
                "opportunity_scan",
                static_summary,
            )
            scan_summary = static_summary
        self._cycle_phase = "candles"
        benchmark_rows = self._relative_strength_benchmark_rows()
        benchmark_symbols = [row["symbol"] for row in benchmark_rows]
        cached_sets_before = self.db.recent_candle_sets_by_symbol([row["symbol"] for row in universe] + benchmark_symbols)
        candle_fetch_universe, candle_fetch_plan = self._candle_fetch_universe(universe, cached_sets_before)
        known_fetch_symbols = {row["symbol"] for row in candle_fetch_universe}
        benchmark_fetch_universe, benchmark_fetch_plan = self._candle_fetch_universe(benchmark_rows, cached_sets_before)
        for row in benchmark_fetch_universe:
            if row["symbol"] not in known_fetch_symbols:
                candle_fetch_universe.append(row)
                known_fetch_symbols.add(row["symbol"])
        backfill_universe, backfill_plan = self._candle_backfill_universe(scan_universe, known_fetch_symbols)
        for row in backfill_universe:
            symbol = row["symbol"]
            if symbol not in known_fetch_symbols:
                candle_fetch_universe.append(row)
                known_fetch_symbols.add(symbol)
        candle_fetch_plan["relative_strength_benchmark_symbols"] = benchmark_symbols
        candle_fetch_plan["relative_strength_benchmark_fetch"] = benchmark_fetch_plan
        candle_fetch_plan["coverage_backfill"] = backfill_plan
        self.db.set_state("candle_backfill_plan", backfill_plan)
        planned_candle_fetch_count = len(candle_fetch_universe)
        if not self._pre_strategy_candle_fetch_enabled():
            candle_fetch_plan["deferred"] = True
            candle_fetch_plan["planned_fetch_symbols"] = planned_candle_fetch_count
            candle_fetch_plan["defer_reason"] = "preserve_cycle_budget_for_full_decision_pass"
            candle_fetch_universe = []
        else:
            candle_fetch_plan["deferred"] = False
            candle_fetch_plan["planned_fetch_symbols"] = planned_candle_fetch_count
        fresh_candles: dict[str, list[Any]] = {}
        candle_fetch_error = None
        if candle_fetch_universe:
            try:
                fresh_candles = await asyncio.wait_for(
                    self.market_data.get_candles(candle_fetch_universe),
                    timeout=self._candle_fetch_timeout_seconds(),
                )
            except asyncio.TimeoutError:
                candle_fetch_error = f"TimeoutError: candle fetch exceeded {self._candle_fetch_timeout_seconds():g}s"
            except Exception as exc:
                candle_fetch_error = f"{exc.__class__.__name__}: {str(exc)[:220]}"
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
                "planned_fetch_symbols": planned_candle_fetch_count,
                "cached_symbols_reused": candle_fetch_plan.get("cache_ready", 0) + candle_fetch_plan.get("recently_attempted", 0),
                "symbols_with_candles": len(fresh_candles),
                "total_candles": sum(candle_counts.values()),
                "source_counts": candle_sources,
                "sample_counts": dict(list(candle_counts.items())[:10]),
                "provider_diagnostics": _market_data_diagnostics(self.market_data),
                "error": candle_fetch_error,
            },
        )
        self._cycle_phase = "persist_market_data"
        self.db.upsert_candles(fresh_candles)
        candle_sets = self.db.recent_candle_sets_by_symbol([row["symbol"] for row in universe] + benchmark_symbols)
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
        macro_context = (
            await self._run_optional_phase(
                component="macro",
                event="global_context",
                description="Global intelligence",
                awaitable=self.macro.context_for_cycle(),
                default={},
            )
            if self.macro
            else {}
        )
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
            await self._run_optional_phase(
                component="feeds",
                event="institutional_context",
                description="Free institutional context",
                awaitable=self.institutional_feeds.context_for_cycle(universe),
                default={},
            )
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
            await self._run_optional_phase(
                component="options",
                event="options_intelligence",
                description="Options intelligence",
                awaitable=self.options_intelligence.context_for_cycle(universe, quotes),
                default={},
            )
            if self.options_intelligence
            else {}
        )
        self.db.set_state("options_intelligence_context", options_context)
        self._cycle_phase = "delivery_data"
        delivery_status = (
            await self._run_optional_phase(
                component="delivery",
                event="delivery_data",
                description="Delivery data refresh",
                awaitable=self.delivery_service.ensure_data_current(),
                default={},
            )
            if self.delivery_service
            else {}
        )
        self.db.set_state("delivery_data_status", delivery_status)
        self._cycle_phase = "market_breadth"
        market_breadth_context = (
            await self._run_optional_phase(
                component="market_breadth",
                event="market_breadth_context",
                description="Market breadth",
                awaitable=self._market_breadth_by_region(universe, quotes, candles),
                default={},
            )
            if self.market_breadth
            else {}
        )
        self.db.set_state("market_breadth_context", market_breadth_context)
        self._cycle_phase = "sector_rotation"
        sector_rotation_context = (
            await self._run_optional_phase(
                component="sector_rotation",
                event="sector_rotation_context",
                description="Sector rotation",
                awaitable=self._sector_rotation_by_region(universe, quotes, candles),
                default={},
            )
            if self.sector_rotation
            else {}
        )
        self.db.set_state("sector_rotation_context", sector_rotation_context)
        self._cycle_phase = "macro_calendar"
        macro_calendar_context = (
            await self._run_optional_phase(
                component="macro_calendar",
                event="macro_calendar",
                description="Macro calendar",
                awaitable=self.macro_calendar.event_context_for_cycle(),
                default={},
            )
            if self.macro_calendar
            else {}
        )
        self.db.set_state("macro_calendar_context", macro_calendar_context)
        self._cycle_phase = "pre_catalyst_discovery"
        discovery_symbols = [row["symbol"] for row in universe]
        discovery_candle_sets = self.db.recent_candle_sets_by_symbol(discovery_symbols)
        if not sentiment_by_symbol:
            sentiment_by_symbol = self.db.latest_sentiment_by_symbol(
                discovery_symbols,
                max_age_days=max(1, int(getattr(self.strategy.settings, "news_lookback_days", 7) or 7)),
            )
        pre_catalyst_summary = self._build_pre_catalyst_discovery(
            universe,
            quotes,
            discovery_candle_sets,
            sentiment_by_symbol,
            macro_calendar_context,
            sector_rotation_context,
            macro_context,
            market_action_summary,
        )
        self._store_pre_catalyst_discovery(pre_catalyst_summary)
        pre_decision_market_day_regime = compute_market_day_regimes(
            universe,
            quotes,
            candle_sets,
            market_breadth_context,
            market_region=self.market_region,
        )
        self.db.set_state("market_day_regime", pre_decision_market_day_regime)
        pre_decision_rally_plan = self._build_and_store_rally_plan(
            market_day_regime_context=pre_decision_market_day_regime,
            pre_catalyst_summary=pre_catalyst_summary,
            scan_summary=scan_summary,
            market_action_summary=market_action_summary,
        )
        rally_promotion_summary = self._annotate_rally_plan_promotions(
            universe,
            scan_summary,
            pre_decision_rally_plan,
        )
        scan_summary["rally_plan_promotion"] = rally_promotion_summary
        self.db.set_state("opportunity_scan", scan_summary)
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
        shared_usage_after_id = self.db.latest_llm_usage_id()
        shared_usage_scope = f"shared_agent_cycle:{uuid4().hex}"
        funding_precheck = self._shared_llm_cycle_funding_status()
        original_strategy_settings = getattr(self.strategy, "settings", None)
        original_llm_settings = getattr(getattr(self.strategy, "llm", None), "settings", None)
        if funding_precheck.get("skip_llm"):
            disabled_settings = _settings_with_llm_api_disabled(original_strategy_settings)
            self.strategy.settings = disabled_settings
            if getattr(self.strategy, "llm", None) is not None:
                self.strategy.llm.settings = disabled_settings
            self._log(
                "WARNING",
                "credits",
                "shared_llm_cycle_unfunded_skipped",
                "Shared AI cycle skipped LLM review because no active user could fund the estimated token spend",
                funding_precheck,
            )
        shared_scope_token = current_llm_usage_scope.set(shared_usage_scope)
        shared_user_token = current_user_id.set(None)
        try:
            decisions = await self._run_strategy_evaluation(
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
        finally:
            current_user_id.reset(shared_user_token)
            current_llm_usage_scope.reset(shared_scope_token)
            if funding_precheck.get("skip_llm"):
                self.strategy.settings = original_strategy_settings
                if getattr(self.strategy, "llm", None) is not None:
                    self.strategy.llm.settings = original_llm_settings
        shared_llm_usage = self.db.llm_usage_cost_for_system_scope(shared_usage_scope, shared_usage_after_id)
        decisions = self.db.suppress_repeated_buy_decisions(decisions)
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
        market_day_regime_context = compute_market_day_regimes(
            universe,
            quotes,
            candle_sets,
            market_breadth_context,
            market_region=self.market_region,
        )
        self.db.set_state("market_day_regime", market_day_regime_context)
        self._build_and_store_rally_plan(
            market_day_regime_context=market_day_regime_context,
            pre_catalyst_summary=pre_catalyst_summary,
            scan_summary=scan_summary,
            market_action_summary=market_action_summary,
        )
        unsafe_follow_exits: list[dict[str, Any]] = []
        legacy_economics_exits = self.db.exit_subfloor_paper_follows(
            reason="legacy_position_below_minimum_trade_economics",
            cost_settings=self.strategy.settings,
        )
        downgraded_buy_ideas: list[dict[str, Any]] = []
        shared_auto_trade = self._auto_follow_buy_ideas_for_signal_users(decisions)
        shared_auto_trade["credit_billing"] = self._charge_shared_ai_cycle_to_users(
            shared_llm_usage,
            decisions,
            universe,
            shared_usage_scope,
        )
        shared_auto_trade["credit_billing"]["funding_precheck"] = funding_precheck
        if legacy_economics_exits:
            shared_auto_trade["legacy_economics_exited"] = [
                {
                    "user_id": item.get("user_id"),
                    "symbol": item.get("symbol"),
                    "mode": item.get("mode"),
                    "market_region": item.get("market_region"),
                    "qty": item.get("qty"),
                    "invested_amount": item.get("invested_amount"),
                    "reason": "legacy_position_below_minimum_trade_economics",
                }
                for item in legacy_economics_exits[:20]
            ]
        if unsafe_follow_exits or legacy_economics_exits:
            shared_auto_trade["safety_exited"] = [
                {
                    "user_id": item.get("user_id"),
                    "symbol": item.get("symbol"),
                    "mode": item.get("mode"),
                    "quality_reason": (item.get("quality_gate") or {}).get("reason"),
                }
                for item in (unsafe_follow_exits + legacy_economics_exits)[:20]
            ]
        if downgraded_buy_ideas:
            shared_auto_trade["downgraded_buy_ideas"] = [
                {
                    "symbol": item.get("symbol"),
                    "quality_reason": (item.get("quality_gate") or {}).get("reason"),
                }
                for item in downgraded_buy_ideas[:20]
            ]
        self._last_shared_auto_trade = shared_auto_trade
        self._store_shared_auto_trade_summary(shared_auto_trade)
        executed_count = self._execute_top_decisions(decisions, portfolio["equity"]) if self.execute_trades else 0
        if not self.execute_trades:
            self._log(
                "INFO",
                "execution",
                "admin_execution_disabled",
                "Global admin cycle is analysis-only; users own signal execution.",
                {"decisions": len(decisions)},
            )
        decision_diagnostics = build_cycle_decision_diagnostics(
            scan_summary,
            decisions,
            shared_auto_trade=shared_auto_trade,
            executed_orders=executed_count,
            market_region=self.market_region,
            generated_at=utc_now(),
            cycle_duration_seconds=round((datetime.now(timezone.utc) - started).total_seconds(), 3),
            missed_move_review_row_id=int(pre_catalyst_summary.get("missed_move_review_row_id") or 0),
        )
        self.db.set_state("decision_diagnostics", decision_diagnostics)
        self._log(
            "INFO",
            "diagnostics",
            "decision_funnel_built",
            decision_diagnostics.get("summary") or "Decision funnel diagnostics built.",
            {
                "funnel": decision_diagnostics.get("funnel"),
                "health_flags": decision_diagnostics.get("health_flags"),
                "top_blockers": decision_diagnostics.get("top_blockers", [])[:5],
                "slot_fill_counts": decision_diagnostics.get("slot_fill_counts"),
                "slot_shortfalls": decision_diagnostics.get("slot_shortfalls"),
                "slot_fill_counts_by_market": decision_diagnostics.get("slot_fill_counts_by_market"),
                "missed_move_review_row_id": decision_diagnostics.get("missed_move_review_row_id"),
                "live_quote_stale_intraday": decision_diagnostics.get("live_quote_stale_intraday"),
                "auto_follow": decision_diagnostics.get("auto_follow"),
                "scanner_rejections": decision_diagnostics.get("scanner_rejections"),
                "scanner_setups": decision_diagnostics.get("scanner_setups"),
            },
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
                "shared_auto_trade": shared_auto_trade,
                "equity": portfolio.get("equity"),
            },
        )
        snapshot = self.snapshot()
        await self._run_post_cycle_callbacks(snapshot)
        return snapshot

    async def _run_post_cycle_callbacks(self, snapshot: dict[str, Any]) -> None:
        callbacks: list[tuple[str, Awaitable[Any]]] = []
        if self.openclaw_notifier:
            callbacks.append(("openclaw_notifications", self.openclaw_notifier.notify_cycle_events()))
        if self.on_update:
            callbacks.append(("dashboard_update", self.on_update(snapshot)))
        if not callbacks:
            return

        previous_phase = self._cycle_phase
        self._cycle_phase = "post_cycle_callbacks"
        try:
            for name, awaitable in callbacks:
                remaining = self._remaining_cycle_seconds()
                if remaining is not None and remaining <= 0.5:
                    self._log(
                        "WARN",
                        "cycle",
                        f"{name}_skipped_no_cycle_budget",
                        "Post-cycle callback skipped because the cycle budget was already exhausted.",
                        {"remaining_cycle_seconds": remaining},
                    )
                    if hasattr(awaitable, "close"):
                        awaitable.close()
                    continue
                timeout = 1.0 if remaining is None else max(0.1, min(1.0, remaining - 0.25))
                try:
                    result = await asyncio.wait_for(awaitable, timeout=timeout)
                except asyncio.TimeoutError:
                    self._log(
                        "WARN",
                        "cycle",
                        f"{name}_timeout",
                        "Post-cycle callback timed out after the trading work had already completed.",
                        {"timeout_seconds": timeout, "remaining_cycle_seconds": self._remaining_cycle_seconds()},
                    )
                    continue
                if name == "openclaw_notifications" and isinstance(result, dict) and result.get("enabled"):
                    self._log(
                        "INFO",
                        "openclaw",
                        "notifications_checked",
                        "OpenClaw notification bridge checked cycle events",
                        result,
                    )
        finally:
            self._cycle_phase = previous_phase

    def _build_and_store_rally_plan(
        self,
        *,
        market_day_regime_context: dict[str, Any],
        pre_catalyst_summary: dict[str, Any],
        scan_summary: dict[str, Any],
        market_action_summary: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            generated_at = utc_now()
            signal_ideas = self.db.latest_signal_ideas(300, market_region=self.market_region)
            kwargs = {
                "market_day_regime": market_day_regime_context,
                "pre_catalyst": pre_catalyst_summary,
                "tomorrow_plan": self.db.latest_tomorrow_plan(self.market_region),
                "opportunity_scan": scan_summary,
                "market_action_radar": market_action_summary,
                "signal_ideas": signal_ideas,
                "generated_at": generated_at,
            }
            region = normalize_market_region(self.market_region or "BOTH", default="BOTH")
            plan = build_rally_plan_by_market(**kwargs) if region == "BOTH" else build_rally_plan(market_region=region, **kwargs)
            self.db.set_state("rally_plan", plan)
            by_market = plan.get("by_market") if isinstance(plan.get("by_market"), dict) else {}
            item_counts = (
                {
                    market: len(raw.get("items") or [])
                    for market, raw in by_market.items()
                    if isinstance(raw, dict)
                }
                if by_market
                else {region: len(plan.get("items") or [])}
            )
            self._log(
                "INFO",
                "rally_plan",
                "rally_plan_built",
                "Rally Plan refreshed from the latest shared market cycle.",
                {
                    "market_region": region,
                    "generated_at": generated_at,
                    "item_counts": item_counts,
                    "regime": plan.get("regime"),
                },
            )
            return plan
        except Exception as exc:
            self._log(
                "WARN",
                "rally_plan",
                "rally_plan_build_failed",
                "Rally Plan could not be refreshed during the shared market cycle.",
                {"error": f"{exc.__class__.__name__}: {str(exc)[:220]}"},
            )
            return {}

    def _annotate_rally_plan_promotions(
        self,
        universe: list[dict[str, Any]],
        scan_summary: dict[str, Any],
        rally_plan: dict[str, Any],
    ) -> dict[str, Any]:
        if not bool(getattr(self.strategy.settings, "rally_plan_promotion_enabled", True)):
            return {"enabled": False, "reason": "rally_plan_promotion_disabled", "annotated_symbols": []}
        max_per_market = max(0, int(getattr(self.strategy.settings, "rally_plan_max_promotions_per_market", 8) or 0))
        promotions = extract_rally_plan_promotions(rally_plan, max_per_market=max_per_market)
        by_symbol: dict[str, dict[str, Any]] = {}
        by_market = promotions.get("by_market") if isinstance(promotions.get("by_market"), dict) else {}
        for market, rows in by_market.items():
            for row in rows or []:
                if not isinstance(row, dict):
                    continue
                symbol = str(row.get("symbol") or "").upper()
                if not symbol:
                    continue
                by_symbol[symbol] = {**row, "market_region": str(row.get("market_region") or market or "").upper() or row.get("market_region")}
        annotated: list[str] = []
        for row in universe:
            symbol = str(row.get("symbol") or "").upper()
            promotion = by_symbol.get(symbol)
            scan = row.get("_opportunity_scan") if isinstance(row.get("_opportunity_scan"), dict) else {}
            if promotion:
                scan["rally_plan_promotion"] = promotion
                row["_opportunity_scan"] = scan
                annotated.append(symbol)
        for item in scan_summary.get("top_candidates") or []:
            if isinstance(item, dict):
                promotion = by_symbol.get(str(item.get("symbol") or "").upper())
                if promotion:
                    item["rally_plan_promotion"] = promotion
        by_market_summary = scan_summary.get("by_market") if isinstance(scan_summary.get("by_market"), dict) else {}
        for market_summary in by_market_summary.values():
            if not isinstance(market_summary, dict):
                continue
            for item in market_summary.get("top_candidates") or []:
                if isinstance(item, dict):
                    promotion = by_symbol.get(str(item.get("symbol") or "").upper())
                    if promotion:
                        item["rally_plan_promotion"] = promotion
        summary = {
            "enabled": True,
            "max_per_market": max_per_market,
            "available_total": int(promotions.get("total") or 0),
            "annotated_count": len(annotated),
            "annotated_symbols": sorted(annotated)[:25],
            "by_market_counts": {
                str(market): len(rows or [])
                for market, rows in by_market.items()
            },
            "blocked_counts": promotions.get("blocked_counts") if isinstance(promotions.get("blocked_counts"), dict) else {},
        }
        self._log(
            "INFO",
            "rally_plan",
            "rally_plan_promotions_annotated",
            "Actionable Rally Plan rows attached to the strategy decision context.",
            summary,
        )
        return summary

    async def _run_optional_phase(
        self,
        *,
        component: str,
        event: str,
        description: str,
        awaitable: Awaitable[dict[str, Any]],
        default: dict[str, Any],
    ) -> dict[str, Any]:
        timeout = self._optional_phase_timeout_seconds()
        try:
            result = await asyncio.wait_for(awaitable, timeout=timeout)
            return result if isinstance(result, dict) else dict(default)
        except asyncio.TimeoutError:
            self._log(
                "WARN",
                component,
                f"{event}_timeout",
                f"{description} timed out after {timeout:g}s; continuing with empty context.",
                {"phase": self._cycle_phase, "timeout_seconds": timeout},
            )
            return {**default, "status": "timeout", "timeout_seconds": timeout}
        except Exception as exc:
            self._log(
                "WARN",
                component,
                f"{event}_failed",
                f"{description} failed; continuing with empty context.",
                {"phase": self._cycle_phase, "error": f"{exc.__class__.__name__}: {str(exc)[:220]}"},
            )
            return {**default, "status": "error", "error": f"{exc.__class__.__name__}: {str(exc)[:220]}"}

    async def _run_strategy_evaluation(
        self,
        universe: list[dict[str, Any]],
        quotes: dict[str, Quote],
        positions: dict[str, dict[str, Any]],
        candles: dict[str, list[Any]],
        macro_context: dict[str, Any],
        institutional_context: dict[str, Any],
        options_context: dict[str, Any],
        delivery_service: Any | None,
        market_breadth_context: dict[str, Any],
        sector_rotation_context: dict[str, Any],
        macro_calendar: Any | None,
        candle_sets: dict[str, dict[str, list[Any]]],
        portfolio_equity: float | None,
    ) -> list[Decision]:
        timeout = self._strategy_eval_timeout_seconds()
        try:
            return await asyncio.wait_for(
                self.strategy.evaluate(
                    universe,
                    quotes,
                    positions,
                    candles,
                    macro_context,
                    institutional_context,
                    options_context,
                    delivery_service,
                    market_breadth_context,
                    sector_rotation_context,
                    macro_calendar,
                    candle_sets,
                    portfolio_equity,
                ),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            self._log(
                "ERROR",
                "strategy",
                "strategy_eval_timeout",
                f"Strategy evaluation exceeded {timeout:g}s; writing safe HOLD decisions for this cycle.",
                {
                    "timeout_seconds": timeout,
                    "symbols": len(universe),
                    "quoted_symbols": len(quotes),
                    "remaining_cycle_seconds": self._remaining_cycle_seconds(),
                },
            )
            return self._strategy_timeout_fallback_decisions(universe, quotes, positions, reason="strategy_eval_timeout", timeout=timeout)
        except Exception as exc:
            error = f"{exc.__class__.__name__}: {str(exc)[:220]}"
            self._log(
                "ERROR",
                "strategy",
                "strategy_eval_failed",
                "Strategy evaluation failed; writing safe HOLD decisions for this cycle.",
                {"symbols": len(universe), "quoted_symbols": len(quotes), "error": error},
            )
            return self._strategy_timeout_fallback_decisions(universe, quotes, positions, reason="strategy_eval_failed", error=error)

    def _strategy_timeout_fallback_decisions(
        self,
        universe: list[dict[str, Any]],
        quotes: dict[str, Quote],
        positions: dict[str, dict[str, Any]],
        *,
        reason: str,
        timeout: float | None = None,
        error: str | None = None,
    ) -> list[Decision]:
        decisions: list[Decision] = []
        asof = utc_now()
        for row in universe:
            symbol = str(row.get("symbol") or "").upper()
            quote = quotes.get(symbol)
            if not symbol or quote is None:
                continue
            market_region = market_region_for_row(row)
            primary_blocker = {
                "gate": reason,
                "reason": reason,
                "value": {
                    "timeout_seconds": timeout,
                    "error": error,
                    "selected_for_decision": True,
                    "has_position": bool(positions.get(symbol, {}).get("qty", 0) > 0),
                },
            }
            details = {
                "decision_path": "strategy_budget_safe_hold",
                "market_region": market_region,
                "currency": "USD" if market_region == "US" else "INR",
                "opportunity_scan": row.get("_opportunity_scan") or {},
                "data_readiness": {
                    "trade_decision_ready": False,
                    "primary_blocker": reason,
                    "secondary_diagnostics": ["cycle_budget_protected"],
                },
                "decision_gate_context": {
                    "primary_blocker": primary_blocker,
                    "secondary_blockers": [],
                    "blocking_failed_gates": [primary_blocker],
                    "failed_gates": [primary_blocker],
                },
                "system_gate_audit": {
                    "hard_blocked": True,
                    "hard_blocks": [
                        {
                            "flag": reason.upper(),
                            "reason": reason,
                            "value": primary_blocker["value"],
                        }
                    ],
                },
            }
            decisions.append(
                Decision(
                    symbol=symbol,
                    action="HOLD",
                    confidence=0.0,
                    price=quote.price,
                    technical_score=0.0,
                    sentiment_score=0.0,
                    reason=f"safe hold: {reason}; selected for cycle but rich strategy evaluation did not finish within budget",
                    asof=asof,
                    strategy="strategy_budget_safe_hold",
                    details_json=json.dumps(details, default=str),
                )
            )
        return decisions

    def _optional_phase_timeout_seconds(self) -> float:
        raw = getattr(self.strategy.settings, "optional_phase_timeout_seconds", 3.0)
        try:
            value = float(raw or 3.0)
        except (TypeError, ValueError):
            value = 3.0
        return max(1.0, min(value, max(float(self.cycle_timeout_seconds) - 1.0, 1.0)))

    def _strategy_eval_timeout_seconds(self) -> float:
        configured = self._bounded_positive_seconds(
            getattr(self.strategy.settings, "strategy_eval_timeout_seconds", 75.0),
            default=75.0,
        )
        remaining = self._remaining_cycle_seconds()
        if remaining is None:
            return configured
        reserve = min(12.0, max(5.0, remaining * 0.12))
        return max(1.0, min(configured, max(remaining - reserve, 1.0)))

    def _remaining_cycle_seconds(self) -> float | None:
        started_raw = getattr(self, "_cycle_started_at", None)
        if not started_raw:
            return None
        try:
            started = datetime.fromisoformat(str(started_raw))
        except ValueError:
            return None
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        elapsed = (datetime.now(timezone.utc) - started).total_seconds()
        return max(float(self.cycle_timeout_seconds) - elapsed, 0.0)

    def _pre_strategy_candle_fetch_enabled(self) -> bool:
        return bool(getattr(self.strategy.settings, "pre_strategy_candle_fetch_enabled", False))

    def _opportunity_history_prefetch_limit(self) -> int:
        try:
            return max(0, int(getattr(self.strategy.settings, "opportunity_history_prefetch_symbols", 20) or 0))
        except (TypeError, ValueError):
            return 20

    def _opportunity_history_prefetch_timeout_seconds(self) -> float:
        return min(self._candle_fetch_timeout_seconds(), self._optional_phase_timeout_seconds())

    def _market_action_timeout_seconds(self) -> float:
        raw = getattr(self.strategy.settings, "market_action_radar_timeout_seconds", self._optional_phase_timeout_seconds())
        return self._bounded_positive_seconds(raw, default=self._optional_phase_timeout_seconds())

    def _recent_market_action_summary(self, max_age_seconds: float = 900.0) -> dict[str, Any]:
        cached = self.db.get_state("market_action_radar", {})
        if not isinstance(cached, dict):
            return {}
        events_by_symbol = cached.get("events_by_symbol") if isinstance(cached.get("events_by_symbol"), dict) else {}
        if not events_by_symbol:
            return {}
        scanned_at = cached.get("scanned_at")
        cache_age_seconds: float | None = None
        if scanned_at:
            try:
                parsed = datetime.fromisoformat(str(scanned_at).replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                cache_age_seconds = max((datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds(), 0.0)
            except ValueError:
                cache_age_seconds = None
        if cache_age_seconds is not None and cache_age_seconds > max_age_seconds:
            return {}
        return {**cached, "cache_age_seconds": round(cache_age_seconds, 3) if cache_age_seconds is not None else None}

    def _news_probe_timeout_seconds(self) -> float:
        raw = getattr(self.strategy.settings, "dynamic_scan_news_timeout_seconds", 3.0)
        return self._bounded_positive_seconds(raw, default=3.0)

    def _candle_fetch_timeout_seconds(self) -> float:
        raw = getattr(self.strategy.settings, "candle_fetch_timeout_seconds", 10.0)
        return self._bounded_positive_seconds(raw, default=10.0)

    def _bounded_positive_seconds(self, raw: Any, *, default: float) -> float:
        try:
            value = float(raw or default)
        except (TypeError, ValueError):
            value = default
        return max(1.0, min(value, max(float(self.cycle_timeout_seconds) - 1.0, 1.0)))

    def _write_market_closed_cycle_state(
        self,
        full_universe: list[dict[str, Any]],
        session_context: dict[str, Any],
    ) -> None:
        now = utc_now()
        scan_summary = {
            "enabled": True,
            "mode": "market_closed",
            "scan_paused": True,
            "scanned_at": now,
            "market_region": self.market_region,
            "open_regions": session_context.get("open_regions"),
            "closed_regions": session_context.get("closed_regions"),
            "market_data_policy": session_context.get("data_policy"),
            "enabled_universe_symbols": len(full_universe),
            "open_universe_symbols": 0,
            "raw_symbols": 0,
            "quoted_symbols": 0,
            "selected_symbols": 0,
            "target_decision_symbols": 0,
            "target_decision_symbols_by_market": {},
            "slot_fill_counts": {},
            "slot_fill_counts_by_market": {},
            "primary_blocker_counts": {},
            "skip_reason": "all_selected_markets_closed",
            "top_candidates": [],
        }
        scan_summary["by_market"] = _opportunity_scan_by_market(
            scan_summary,
            full_universe,
            [],
            [],
            [],
            {},
        )
        self.db.set_state("opportunity_scan", scan_summary)
        self.db.set_state(
            "decision_diagnostics",
            {
                "generated_at": now,
                "mode": "market_closed",
                "scan_paused": True,
                "market_region": self.market_region,
                "raw_nse_count": 0,
                "scanner_shortlist_count": 0,
                "full_decision_count": 0,
                "target_decision_count": 0,
                "slot_fill_counts": {},
                "primary_blocker_counts": {},
                "paper_follow_conversion_count": 0,
                "missed_move_review_row_id": 0,
                "health_flags": [],
                "summary": "Markets are closed; live scanner and execution checks are paused.",
            },
        )

    async def _run_market_closed_prep(
        self,
        started: datetime,
        full_universe: list[dict[str, Any]],
        positions: dict[str, dict[str, Any]],
        session_context: dict[str, Any],
    ) -> dict[str, Any]:
        self._cycle_phase = "post_market_prep"
        self._log(
            "INFO",
            "cycle",
            "market_closed_skip_market_data",
            "All selected markets are closed; skipped live quote, breadth, sector, strategy and LLM scans.",
            {
                "market_region": self.market_region,
                "open_regions": session_context.get("open_regions"),
                "closed_regions": session_context.get("closed_regions"),
                "enabled_universe_size": len(full_universe),
                "post_market_prep_enabled": getattr(self.strategy.settings, "post_market_prep_enabled", True),
            },
        )
        self._write_market_closed_cycle_state(full_universe, session_context)
        now_dt = datetime.now(timezone.utc)
        previous_prep = self.db.get_state("tomorrow_prep_context", {})
        previous_prepared_at = (
            _parse_iso_datetime(previous_prep.get("prepared_at"))
            if isinstance(previous_prep, dict)
            else None
        )
        throttle_minutes = max(int(getattr(self.strategy.settings, "post_market_prep_min_interval_minutes", 30) or 30), 10)
        if (
            getattr(self.strategy.settings, "post_market_prep_enabled", True)
            and previous_prepared_at
            and (now_dt - previous_prepared_at.astimezone(timezone.utc)) < timedelta(minutes=throttle_minutes)
        ):
            self._last_cycle_at = utc_now()
            self._last_cycle_duration_seconds = round((datetime.now(timezone.utc) - started).total_seconds(), 3)
            self._cycle_started_at = None
            self._cycle_phase = "idle"
            self._log(
                "INFO",
                "cycle",
                "post_market_prep_throttled",
                "Closed-market prep skipped because a recent tomorrow plan already exists.",
                {
                    "last_prepared_at": previous_prepared_at.isoformat(),
                    "min_interval_minutes": throttle_minutes,
                    "duration_seconds": self._last_cycle_duration_seconds,
                },
            )
            snapshot = self.snapshot()
            if self.on_update:
                await self.on_update(snapshot)
            return snapshot
        macro_context = self.db.get_state("macro_context", {})
        macro_calendar_context = self.db.get_state("macro_calendar_context", {})
        delivery_status = self.db.get_state("delivery_data_status", {})
        news_summary: dict[str, Any] = {
            "enabled": False,
            "reason": "post_market_prep_disabled",
            "symbols_requested": 0,
            "symbols_refreshed": 0,
        }
        prep_candle_summary: dict[str, Any] = {
            "enabled": False,
            "reason": "post_market_prep_disabled",
            "requested_symbols": 0,
            "symbols_with_candles": 0,
        }
        if getattr(self.strategy.settings, "post_market_prep_enabled", True):
            self._cycle_phase = "post_market_news"
            prep_rows = self._post_market_news_rows(full_universe, positions)
            news_summary = await self.strategy.sentiment.refresh_watchlist_news(
                prep_rows,
                limit=getattr(self.strategy.settings, "post_market_news_symbols", 20),
                allow_llm=False,
                reason="post_market_tomorrow_prep",
            )
            self._cycle_phase = "macro_calendar"
            macro_context = (
                await self._run_optional_phase(
                    component="macro",
                    event="global_context",
                    description="Global intelligence",
                    awaitable=self.macro.context_for_cycle(),
                    default=macro_context if isinstance(macro_context, dict) else {},
                )
                if self.macro
                else macro_context
            )
            self.db.set_state("macro_context", macro_context)
            macro_calendar_context = (
                await self._run_optional_phase(
                    component="macro_calendar",
                    event="macro_calendar",
                    description="Macro calendar",
                    awaitable=self.macro_calendar.event_context_for_cycle(),
                    default=macro_calendar_context if isinstance(macro_calendar_context, dict) else {},
                )
                if self.macro_calendar
                else macro_calendar_context
            )
            self.db.set_state("macro_calendar_context", macro_calendar_context)
            self._cycle_phase = "delivery_data"
            delivery_status = (
                await self._run_optional_phase(
                    component="delivery",
                    event="delivery_data",
                    description="Delivery data refresh",
                    awaitable=self.delivery_service.ensure_data_current(),
                    default=delivery_status if isinstance(delivery_status, dict) else {},
                )
                if self.delivery_service
                else delivery_status
            )
            self.db.set_state("delivery_data_status", delivery_status)
            self._cycle_phase = "post_market_candle_backfill"
            backfill_rows, backfill_plan = self._candle_backfill_universe(full_universe, set())
            backfill_candles: dict[str, list[Any]] = {}
            backfill_error = None
            if backfill_rows:
                try:
                    backfill_candles = await self.market_data.get_candles(backfill_rows)
                except Exception as exc:
                    backfill_error = f"{exc.__class__.__name__}: {str(exc)[:220]}"
                if backfill_candles:
                    self.db.upsert_candles(backfill_candles)
            prep_candle_summary = {
                "enabled": True,
                "mode": "closed_market_historical_backfill",
                "requested_symbols": len(backfill_rows),
                "symbols_with_candles": len(backfill_candles),
                "total_candles": sum(len(items) for items in backfill_candles.values()),
                "plan": backfill_plan,
                "sample_symbols": [row.get("symbol") for row in backfill_rows[:20]],
                "error": backfill_error,
            }
            self.db.set_state("post_market_candle_backfill", prep_candle_summary)

        self._cycle_phase = "pre_catalyst_discovery"
        opportunity_state = self.db.get_state("opportunity_scan", {})
        cached_market_action = (
            opportunity_state.get("market_action_radar", {})
            if isinstance(opportunity_state, dict)
            else {}
        ) or self.db.get_state("market_action_radar", {})
        pre_catalyst_summary = self._build_cached_pre_catalyst_discovery(
            full_universe,
            macro_context if isinstance(macro_context, dict) else {},
            macro_calendar_context if isinstance(macro_calendar_context, dict) else {},
            self.db.get_state("sector_rotation_context", {}),
            cached_market_action,
        )
        self._store_pre_catalyst_discovery(pre_catalyst_summary)

        quote_rows = self.db.latest_quotes()
        portfolio = self.broker.snapshot()
        market_health = self._market_health(quote_rows)
        market_health["portfolio_equity"] = portfolio.get("equity")
        self_audit = build_self_audit(list(positions.values()), quote_rows, portfolio, market_health, macro_calendar_context)
        self.db.set_state("self_audit", self_audit)
        tomorrow_plan_summary: dict[str, Any] = {"enabled": False, "reason": "not_built"}
        try:
            configured_region = str(self.market_region or "IN").upper()
            if configured_region in {"IN", "US"}:
                plan_regions = [configured_region]
            else:
                available_regions = {market_region_for_row(row) for row in full_universe if row.get("symbol")}
                plan_regions = [region for region in ("IN", "US") if region in available_regions] or ["IN"]
            signal_ideas = self.db.latest_signal_ideas(300)
            position_rows = list(positions.values())
            built_plans = []
            prepared_at = utc_now()
            for region in plan_regions:
                plan = build_tomorrow_plan(
                    market_region=region,
                    signal_ideas=signal_ideas,
                    positions=position_rows,
                    pre_catalyst=pre_catalyst_summary,
                    opportunity_scan=opportunity_state if isinstance(opportunity_state, dict) else {},
                    macro_context=macro_context if isinstance(macro_context, dict) else {},
                    market_session=session_context,
                    prepared_at=prepared_at,
                )
                self.db.upsert_tomorrow_plan(plan)
                built_plans.append(plan)
            tomorrow_plan_summary = {
                "enabled": True,
                "markets": {plan["market_region"]: plan.get("summary", {}) for plan in built_plans},
                "total_items": sum(len(plan.get("items") or []) for plan in built_plans),
                "ready_at_open": sum(int((plan.get("summary") or {}).get("ready_at_open") or 0) for plan in built_plans),
                "near_breakout": sum(int((plan.get("summary") or {}).get("near_breakout") or 0) for plan in built_plans),
                "news_watch": sum(int((plan.get("summary") or {}).get("news_watch") or 0) for plan in built_plans),
                "position_actions": sum(int((plan.get("summary") or {}).get("position_actions") or 0) for plan in built_plans),
            }
        except Exception as exc:
            tomorrow_plan_summary = {
                "enabled": False,
                "error": f"{exc.__class__.__name__}: {str(exc)[:220]}",
            }
            self._log(
                "WARN",
                "cycle",
                "tomorrow_plan_failed",
                "Tomorrow plan could not be built during closed-market prep.",
                tomorrow_plan_summary,
            )
        prep_context = {
            "enabled": getattr(self.strategy.settings, "post_market_prep_enabled", True),
            "mode": "market_closed_tomorrow_prep",
            "prepared_at": utc_now(),
            "market_session": session_context,
            "news": news_summary,
            "candle_backfill": prep_candle_summary,
            "macro": {
                "regime": macro_context.get("regime") if isinstance(macro_context, dict) else None,
                "risk_score": macro_context.get("risk_score") if isinstance(macro_context, dict) else None,
                "confidence": macro_context.get("confidence") if isinstance(macro_context, dict) else None,
            },
            "upcoming_macro_events": (macro_calendar_context or {}).get("next_10", []),
            "delivery_data_status": delivery_status,
            "pre_catalyst_discovery": {
                "enabled": pre_catalyst_summary.get("enabled"),
                "candidates": len(pre_catalyst_summary.get("candidates") or []),
                "live_confirmations": len(pre_catalyst_summary.get("live_confirmations") or []),
                "label_counts": pre_catalyst_summary.get("label_counts"),
            },
            "tomorrow_plan": tomorrow_plan_summary,
            "skipped_phases": [
                "market_quotes",
                "candles",
                "market_breadth",
                "sector_rotation",
                "strategy_and_llm",
                "risk_and_execution",
            ],
            "readiness_note": "Closed-market prep only. Next live scan will run when the selected market session opens.",
        }
        self.db.set_state("tomorrow_prep_context", prep_context)
        previous_scan = self.db.get_state("opportunity_scan", {})
        last_open_scan: dict[str, Any] = {}
        if isinstance(previous_scan, dict) and previous_scan.get("scan_paused") and isinstance(previous_scan.get("last_open_scan"), dict):
            last_open_scan = dict(previous_scan.get("last_open_scan") or {})
        elif isinstance(previous_scan, dict) and previous_scan.get("mode") == "dynamic_opportunity_scan":
            last_open_scan = {
                key: previous_scan.get(key)
                for key in (
                    "scanned_at",
                    "raw_symbols",
                    "scanned_symbols_this_cycle",
                    "selected_symbols",
                    "tradeable_screening_symbols",
                    "open_universe_symbols",
                    "enabled_universe_symbols",
                )
                if key in previous_scan
            }
            if isinstance(previous_scan.get("by_market"), dict):
                last_open_scan["by_market"] = previous_scan.get("by_market")
            last_open_scan["top_candidates"] = [
                {
                    "symbol": item.get("symbol"),
                    "score": item.get("score"),
                    "setup": item.get("setup"),
                    "bucket": item.get("bucket"),
                }
                for item in (previous_scan.get("top_candidates") or [])[:5]
                if isinstance(item, dict)
            ]
        closed_scan_summary = {
            "enabled": True,
            "mode": "market_closed_tomorrow_prep",
            "scan_paused": True,
            "scanned_at": utc_now(),
            "raw_symbols": 0,
            "quoted_symbols": 0,
            "candidate_limit": getattr(self.strategy.settings, "dynamic_scan_candidate_limit", 60),
            "selected_symbols": 0,
            "tradeable_screening_symbols": 0,
            "enabled_universe_symbols": len(full_universe),
            "open_universe_symbols": 0,
            "scanned_symbols_this_cycle": 0,
            "raw_scan_limit": getattr(self.strategy.settings, "dynamic_scan_raw_limit", 500),
            "scan_rotation_enabled": False,
            "news_screened_symbols": int(news_summary.get("symbols_requested") or 0),
            "news_events_found": int(news_summary.get("events_found") or 0),
            "news_headlines_found": int(news_summary.get("headlines_found") or 0),
            "post_market_candle_backfill": prep_candle_summary,
            "news_covered_candidates": 0,
            "verified_catalyst_candidates": 0,
            "positive_news_candidates": 0,
            "market_session": session_context,
            "last_open_scan": last_open_scan,
            "readiness_note": "Live opportunity scan is paused while selected markets are closed.",
        }
        closed_scan_summary["by_market"] = _opportunity_scan_by_market(
            closed_scan_summary,
            full_universe,
            [],
            [],
            [],
            news_summary,
        )
        self.db.set_state("opportunity_scan", closed_scan_summary)
        self._last_cycle_at = utc_now()
        self._last_cycle_duration_seconds = round((datetime.now(timezone.utc) - started).total_seconds(), 3)
        self._cycle_started_at = None
        self._cycle_phase = "idle"
        self._log(
            "INFO",
            "cycle",
            "post_market_prep_completed",
            "Closed-market prep completed for tomorrow.",
            {
                "duration_seconds": self._last_cycle_duration_seconds,
                "news_symbols_requested": news_summary.get("symbols_requested"),
                "news_symbols_refreshed": news_summary.get("symbols_refreshed"),
                "events_found": news_summary.get("events_found"),
                "headlines_found": news_summary.get("headlines_found"),
                "open_regions": session_context.get("open_regions"),
                "closed_regions": session_context.get("closed_regions"),
                "tomorrow_plan": tomorrow_plan_summary,
            },
        )
        snapshot = self.snapshot()
        if self.on_update:
            await self.on_update(snapshot)
        return snapshot

    def _build_cached_pre_catalyst_discovery(
        self,
        full_universe: list[dict[str, Any]],
        macro_context: dict[str, Any],
        macro_calendar_context: dict[str, Any],
        sector_rotation_context: dict[str, Any],
        market_action_summary: dict[str, Any],
    ) -> dict[str, Any]:
        quotes = _quote_models_from_rows(self.db.latest_quotes())
        symbols = [row["symbol"] for row in full_universe if row.get("symbol")]
        candle_sets = self.db.recent_candle_sets_by_symbol(symbols)
        eligible_rows = [
            row
            for row in full_universe
            if row.get("symbol")
            and (
                str(row.get("symbol") or "").upper() in quotes
                or _analysis_history_count(candle_sets.get(str(row.get("symbol") or "").upper()) or {}) >= 30
            )
        ]
        if not eligible_rows:
            return {
                "enabled": bool(getattr(self.strategy.settings, "pre_catalyst_engine_enabled", True)),
                "source": "pre_catalyst_engine",
                "mode": "cached_closed_market_prep",
                "reason": "no_cached_quotes_or_daily_history_available",
                "candidates": [],
                "live_confirmations": [],
            }
        symbols = [row["symbol"] for row in eligible_rows]
        sentiment_by_symbol = self.db.latest_sentiment_by_symbol(
            symbols,
            max_age_days=max(1, int(getattr(self.strategy.settings, "news_lookback_days", 7) or 7)),
        )
        return self._build_pre_catalyst_discovery(
            eligible_rows,
            quotes,
            candle_sets,
            sentiment_by_symbol,
            macro_calendar_context,
            sector_rotation_context,
            macro_context,
            market_action_summary,
        )

    def _build_pre_catalyst_discovery(
        self,
        universe: list[dict[str, Any]],
        quotes: dict[str, Any],
        candle_sets: dict[str, dict[str, list[Any]]],
        sentiment_by_symbol: dict[str, dict[str, Any]],
        macro_calendar_context: dict[str, Any],
        sector_rotation_context: dict[str, Any],
        macro_context: dict[str, Any],
        market_action_summary: dict[str, Any],
    ) -> dict[str, Any]:
        previous_state = self.db.get_state("pre_catalyst_discovery", {})
        try:
            return build_pre_catalyst_watchlist(
                universe,
                quotes,
                candle_sets,
                sentiment_by_symbol=sentiment_by_symbol,
                macro_calendar_context=macro_calendar_context,
                sector_rotation_context=sector_rotation_context,
                macro_context=macro_context,
                market_action_summary=market_action_summary,
                previous_state=previous_state if isinstance(previous_state, dict) else {},
                settings=self.strategy.settings,
            )
        except Exception as exc:
            return {
                "enabled": True,
                "source": "pre_catalyst_engine",
                "generated_at": utc_now(),
                "error": f"{exc.__class__.__name__}: {str(exc)[:220]}",
                "candidates": [],
                "live_confirmations": [],
            }

    def _store_pre_catalyst_discovery(self, summary: dict[str, Any]) -> None:
        self._persist_missed_move_review(summary)
        self.db.set_state("pre_catalyst_discovery", summary)
        if isinstance(summary.get("calendar_enrichment"), dict):
            self.db.set_state("pre_catalyst_calendar_enrichment", summary["calendar_enrichment"])
        self._log(
            "INFO",
            "pre_catalyst",
            "pre_catalyst_discovery_completed",
            f"Pre-catalyst discovery found {len(summary.get('candidates') or [])} candidates and {len(summary.get('live_confirmations') or [])} live confirmations",
            {
                "enabled": summary.get("enabled"),
                "label_counts": summary.get("label_counts"),
                "data_gaps": summary.get("data_gaps"),
                "calendar_status": (summary.get("calendar_enrichment") or {}).get("status")
                if isinstance(summary.get("calendar_enrichment"), dict)
                else None,
                "events": (summary.get("log_events") or [])[-20:],
            },
        )

    def _store_shared_auto_trade_summary(self, summary: dict[str, Any]) -> None:
        if not isinstance(summary, dict):
            summary = {}
        self.db.set_state("shared_auto_trade", {**summary, "stored_at": utc_now()})

    def _persist_missed_move_review(self, summary: dict[str, Any]) -> None:
        settings = self.strategy.settings
        if not bool(getattr(settings, "missed_move_review_enabled", True)):
            return
        review = summary.get("missed_move_review") if isinstance(summary, dict) else {}
        if not isinstance(review, dict) or not review.get("enabled"):
            return
        configured_market = normalize_market_region(getattr(settings, "missed_move_review_market", "BOTH") or "BOTH", default="BOTH")
        market_region = self._missed_move_review_market_region(summary)
        if configured_market not in {"BOTH", market_region}:
            return
        generated_at = str(review.get("generated_at") or summary.get("generated_at") or utc_now())
        review_date = generated_at[:10]
        details = {
            "source": "pre_catalyst_missed_move_review",
            "market_region": market_region,
            "review_date": review_date,
            "review": review,
            "candidate_count": len(summary.get("candidates") or []),
            "candidate_pool_count": int(summary.get("candidate_pool_count") or 0),
            "live_confirmations": len(summary.get("live_confirmations") or []),
            "label_counts": summary.get("label_counts") if isinstance(summary.get("label_counts"), dict) else {},
        }
        row_id = self.db.insert_missed_move_review(
            market_region=market_region,
            review_date=review_date,
            details=details,
            ts=generated_at,
        )
        summary["missed_move_review_row_id"] = row_id

    def _missed_move_review_market_region(self, summary: dict[str, Any]) -> str:
        configured_region = normalize_market_region(self.market_region or "BOTH", default="BOTH")
        if configured_region != "BOTH":
            return configured_region
        markets: set[str] = set()
        for key in ("candidates", "candidate_pool", "live_confirmations"):
            values = summary.get(key) if isinstance(summary, dict) else []
            if not isinstance(values, list):
                continue
            for item in values:
                if not isinstance(item, dict):
                    continue
                raw_market = item.get("market_region")
                if raw_market:
                    markets.add(normalize_market_region(raw_market, default="BOTH"))
                    continue
                symbol = str(item.get("symbol") or "")
                if symbol:
                    markets.add(market_region_for_row(item))
        markets.discard("BOTH")
        if len(markets) == 1:
            return next(iter(markets))
        return "BOTH"

    def _post_market_news_rows(
        self,
        full_universe: list[dict[str, Any]],
        positions: dict[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        rows_by_symbol = {str(row.get("symbol")): row for row in full_universe}
        ordered_symbols: list[str] = []
        for symbol in positions:
            ordered_symbols.append(symbol)
        for idea in self.db.latest_signal_ideas(80, market_region=self.market_region):
            symbol = str(idea.get("symbol") or "")
            if symbol:
                ordered_symbols.append(symbol)
        for decision in self.db.latest_decision_summaries(120, market_region=self.market_region):
            symbol = str(decision.get("symbol") or "")
            if symbol:
                ordered_symbols.append(symbol)
        for row in full_universe:
            ordered_symbols.append(str(row.get("symbol") or ""))

        selected: list[dict[str, Any]] = []
        seen: set[str] = set()
        for symbol in ordered_symbols:
            if not symbol or symbol in seen:
                continue
            row = rows_by_symbol.get(symbol)
            if not row:
                continue
            seen.add(symbol)
            selected.append(row)
        return selected

    def _annotate_universe_with_cached_surveillance(self, universe: list[dict[str, Any]]) -> list[dict[str, Any]]:
        institutional = self.db.get_state("institutional_context", {}) or {}
        feeds = institutional.get("feeds") if isinstance(institutional, dict) else {}
        symbol_flags = institutional.get("symbol_flags") if isinstance(institutional, dict) else {}
        asm_symbols: set[str] = set()
        gsm_symbols: set[str] = set()
        if isinstance(feeds, dict):
            asm_symbols = {str(item or "").upper() for item in ((feeds.get("asm") or {}).get("symbols") or [])}
            gsm_symbols = {str(item or "").upper() for item in ((feeds.get("gsm") or {}).get("symbols") or [])}
        output: list[dict[str, Any]] = []
        for row in universe:
            symbol = str(row.get("symbol") or "").upper()
            flags = symbol_flags.get(symbol) if isinstance(symbol_flags, dict) else {}
            asm = bool(symbol in asm_symbols or (isinstance(flags, dict) and flags.get("asm")))
            gsm = bool(symbol in gsm_symbols or (isinstance(flags, dict) and flags.get("gsm")))
            if not asm and not gsm:
                output.append(row)
                continue
            output.append(
                {
                    **row,
                    "_asm_surveillance": asm,
                    "_gsm_surveillance": gsm,
                    "_surveillance_stage": "GSM" if gsm else "ASM",
                }
            )
        return output

    def _auto_follow_buy_ideas_for_signal_users(self, decisions: list[Decision]) -> dict[str, Any]:
        buy_symbols = {
            str(decision.symbol or "").upper()
            for decision in decisions
            if _decision_has_buy_intent(decision)
        }
        exit_symbols = {
            str(decision.symbol or "").upper()
            for decision in decisions
            if str(decision.action or "").upper() == "SELL"
        }
        summary: dict[str, Any] = {
            "buy_symbols": sorted(buy_symbols),
            "exit_symbols": sorted(exit_symbols),
            "users_checked": 0,
            "followed": 0,
            "exited": 0,
            "managed_exit_actions": 0,
            "managed_exit_skips": 0,
            "managed_exits": [],
            "managed_exit_skip_samples": [],
            "active_buy_ideas_checked": 0,
            "skipped": [],
        }
        users = [
            user
            for user in self.db.list_users()
            if user.get("role") != "admin"
            and user.get("active")
            and _normalize_signal_execution_mode(user.get("signal_execution_mode")) in {"AUTO_PAPER", "AUTO_LIVE"}
        ]
        summary["users_checked"] = len(users)
        for user in users:
            user_id = int(user["id"])
            mode = _normalize_signal_execution_mode(user.get("signal_execution_mode"))
            monitor_symbols = self.db.user_monitor_symbols(user_id)
            monitor_allowed = {str(symbol or "").upper() for symbol in monitor_symbols}
            scoped_buy_symbols = buy_symbols
            if monitor_allowed:
                blocked = sorted(symbol for symbol in buy_symbols if symbol and symbol not in monitor_allowed)
                if blocked:
                    summary["skipped"].append(
                        {
                            "user_id": user.get("id"),
                            "username": user.get("username"),
                            "reason": "outside_custom_monitor_list",
                            "symbols": blocked[:12],
                            "monitor_symbols_count": len(monitor_allowed),
                        }
                    )
                scoped_buy_symbols = {symbol for symbol in buy_symbols if symbol in monitor_allowed}
                scope_exits = self.db.exit_active_follows_outside_monitor_scope(user_id, monitor_allowed)
                if scope_exits:
                    summary["exited"] += len(scope_exits)
                    summary["skipped"].append(
                        {
                            "user_id": user.get("id"),
                            "username": user.get("username"),
                            "reason": "cleaned_active_follows_outside_custom_monitor_list",
                            "symbols": [str(item.get("symbol") or "").upper() for item in scope_exits[:12]],
                            "monitor_symbols_count": len(monitor_allowed),
                        }
                    )
            exited_symbols = self._auto_exit_followed_signal_ideas_for_user(user_id, user, exit_symbols)
            summary["exited"] += len(exited_symbols)
            managed_exits = self.db.manage_user_follow_exits(user_id, cost_settings=self.strategy.settings)
            managed_actions = managed_exits.get("actions", []) if isinstance(managed_exits, dict) else []
            managed_skips = managed_exits.get("skipped", []) if isinstance(managed_exits, dict) else []
            summary["managed_exit_actions"] += len(managed_actions)
            summary["managed_exit_skips"] += len(managed_skips)
            summary["exited"] += sum(1 for action in managed_actions if str(action.get("action") or "").upper() == "EXIT_FULL")
            for action in managed_actions:
                payload = {
                    "user_id": user.get("id"),
                    "username": user.get("username"),
                    **action,
                }
                if len(summary["managed_exits"]) < 50:
                    summary["managed_exits"].append(payload)
                self._log(
                    "INFO",
                    "user_session",
                    "shared_user_follow_exit_managed",
                    f"Managed paper follow exit for {payload.get('symbol')} ({payload.get('action')})",
                    payload,
                )
            for skip in managed_skips:
                if len(summary["managed_exit_skip_samples"]) < 30:
                    summary["managed_exit_skip_samples"].append(
                        {
                            "user_id": user.get("id"),
                            "username": user.get("username"),
                            **skip,
                        }
                    )
            active_buy_ideas = [
                idea
                for idea in self.db.latest_signal_ideas(200, user_id=user_id, symbols=monitor_symbols or None)
                if str(idea.get("signal_type") or "").upper() == "BUY"
                and str(idea.get("status") or "").upper() == "ACTIVE"
                and str(idea.get("lifecycle_status") or "active").lower() not in {"stopped", "target_3_hit", "expired", "exit_signal"}
            ]
            summary["active_buy_ideas_checked"] += len(active_buy_ideas)
            active_follow_symbols = {
                str(item.get("symbol") or "").upper()
                for item in self.db.user_followed_signal_ideas(user_id, 200)
                if str(item.get("mode") or "").upper() in {"PAPER", "LIVE"}
                and str(item.get("follow_status") or "").upper() in {"ACTIVE", "LIVE_REQUESTED"}
                and int(item.get("qty") or 0) > 0
            }
            candidate_buy_symbols = scoped_buy_symbols | {
                str(idea.get("symbol") or "").upper()
                for idea in active_buy_ideas
                if _auto_follow_idea_fresh_enough(idea, scoped_buy_symbols)
            }
            for idea in active_buy_ideas:
                symbol = str(idea.get("symbol") or "").upper()
                if not symbol or symbol in candidate_buy_symbols:
                    continue
                if symbol in active_follow_symbols:
                    summary["skipped"].append({"user_id": user.get("id"), "symbol": symbol, "reason": "already_followed_symbol"})
                    continue
                summary["skipped"].append(
                    {
                        "user_id": user.get("id"),
                        "symbol": symbol,
                        "reason": "active_buy_not_fresh_enough_for_auto_follow",
                        "current_return_pct": round(float(idea.get("current_return_pct") or 0.0), 4),
                        "fresh_action": idea.get("fresh_action"),
                        "setup_bucket": idea.get("setup_bucket"),
                    }
                )
            if mode == "AUTO_LIVE":
                if candidate_buy_symbols:
                    summary["skipped"].append(
                        {
                            "user_id": user.get("id"),
                            "username": user.get("username"),
                            "reason": "live_unavailable_shared_engine_needs_user_broker_session",
                        }
                    )
                continue
            if not candidate_buy_symbols:
                continue
            ideas = [
                idea
                for idea in active_buy_ideas
                if str(idea.get("symbol") or "").upper() in candidate_buy_symbols
            ]
            seen_symbols: set[str] = set()
            for idea in ideas:
                symbol = str(idea.get("symbol") or "").upper()
                if not symbol or symbol in seen_symbols:
                    continue
                seen_symbols.add(symbol)
                quality_gate = auto_follow_quality_gate(idea)
                if not quality_gate.get("passed"):
                    summary["skipped"].append({"user_id": user.get("id"), "symbol": symbol, **quality_skip_payload(quality_gate)})
                    continue
                reentry_block = self.db.recent_user_symbol_exit(
                    user_id,
                    symbol,
                    cooldown_hours=max(
                        int(
                            getattr(
                                self.strategy.settings,
                                "auto_follow_reentry_cooldown_hours",
                                AUTO_FOLLOW_REENTRY_COOLDOWN_HOURS,
                            )
                            or AUTO_FOLLOW_REENTRY_COOLDOWN_HOURS
                        ),
                        AUTO_FOLLOW_REENTRY_COOLDOWN_HOURS,
                    ),
                )
                if reentry_block:
                    summary["skipped"].append(
                        {
                            "user_id": user.get("id"),
                            "symbol": symbol,
                            "reason": "recent_risk_exit_cooldown",
                            "exit_key": reentry_block.get("exit_key"),
                            "exit_reason": reentry_block.get("exit_reason"),
                            "cooldown_minutes_left": reentry_block.get("cooldown_minutes_left"),
                        }
                    )
                    continue
                if symbol in active_follow_symbols:
                    summary["skipped"].append({"user_id": user.get("id"), "symbol": symbol, "reason": "already_followed_symbol"})
                    continue
                if not _auto_follow_idea_fresh_enough(idea, scoped_buy_symbols):
                    summary["skipped"].append(
                        {
                            "user_id": user.get("id"),
                            "symbol": symbol,
                            "reason": "active_buy_not_fresh_enough_for_auto_follow",
                            "current_return_pct": round(float(idea.get("current_return_pct") or 0.0), 4),
                            "fresh_action": idea.get("fresh_action"),
                            "setup_bucket": idea.get("setup_bucket"),
                        }
                    )
                    continue
                follow = idea.get("user_follow") or {}
                if follow and str(follow.get("status") or "").upper() in {"ACTIVE", "LIVE_REQUESTED"}:
                    summary["skipped"].append({"user_id": user.get("id"), "symbol": symbol, "reason": "already_followed"})
                    continue
                market = str(idea.get("market_region") or "IN").upper()
                cash = self._auto_follow_cash_for_user(user_id, user, market)
                price = _float_or_none(idea.get("latest_price") or idea.get("entry_price")) or 0.0
                size_multiplier = quality_size_multiplier(quality_gate)
                idea_details = idea.get("details") if isinstance(idea.get("details"), dict) else {}
                opportunity_scan = idea_details.get("opportunity_scan") if isinstance(idea_details.get("opportunity_scan"), dict) else {}
                liquidity_scan = opportunity_scan.get("liquidity_profile") if isinstance(opportunity_scan.get("liquidity_profile"), dict) else {}
                sizing = self._auto_follow_sizing(
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
                            "user_id": user.get("id"),
                            "symbol": symbol,
                            "reason": skip_reason,
                            "cash": round(cash, 4),
                            "price": round(price, 4),
                            "sizing": sizing,
                        }
                    )
                    continue
                try:
                    created = self.db.follow_signal_idea(
                        user_id,
                        int(idea["id"]),
                        mode="PAPER",
                        amount=amount,
                        cost_settings=self.strategy.settings,
                    )
                    summary["followed"] += 1
                    self._log(
                        "INFO",
                        "user_session",
                        "shared_buy_auto_paper_followed",
                        f"Auto-paper followed {symbol} for {user.get('username')}",
                        {
                            "user_id": user.get("id"),
                            "username": user.get("username"),
                            "symbol": symbol,
                            "idea_id": idea.get("id"),
                            "amount": round(amount, 4),
                            "size_multiplier": size_multiplier,
                            "risk_warnings": quality_gate.get("risk_warnings", []),
                            "qty": created.get("qty"),
                            "entry_price": created.get("entry_price"),
                        },
                    )
                except ValueError as exc:
                    summary["skipped"].append({"user_id": user.get("id"), "symbol": symbol, "reason": str(exc)})
        return summary

    def _charge_shared_ai_cycle_to_users(
        self,
        usage: dict[str, Any],
        decisions: list[Decision],
        universe: list[dict[str, Any]],
        usage_scope: str,
    ) -> dict[str, Any]:
        activity = _shared_llm_activity_from_decisions(decisions, usage)
        billing = credit_breakdown_for_usage(
            usage,
            tokens_per_credit=getattr(self.strategy.settings, "credit_tokens_per_credit", 10),
            margin_pct=getattr(self.strategy.settings, "credit_platform_margin_pct", 0.20),
        )
        summary: dict[str, Any] = {
            "enabled": True,
            "scope_id": usage_scope,
            "calls": int(usage.get("calls") or 0),
            "total_tokens": int(usage.get("total_tokens") or 0),
            "total_credits": billing.get("charged_credits", 0.0),
            "billable": bool(activity.get("billable")),
            "participants": 0,
            "charged_users": 0,
            "skipped_users": [],
            "per_user_credits": 0.0,
            "activity": activity,
        }
        if not usage.get("calls") or not activity.get("billable") or float(billing.get("charged_credits") or 0.0) <= 0:
            return summary

        users = [
            user
            for user in self.db.list_users()
            if user.get("role") != "admin" and user.get("active")
        ]
        summary["participants"] = len(users)
        if not users:
            summary["billable"] = False
            summary["reason"] = "no_active_users_to_bill"
            return summary

        participant_count = max(len(users), 1)
        per_user_base = float(billing.get("base_credits") or 0.0) / participant_count
        per_user_credits = float(billing.get("charged_credits") or 0.0) / participant_count
        summary["per_user_credits"] = round(per_user_credits, 6)
        symbols = [str(row.get("symbol") or "") for row in universe if row.get("symbol")]
        top_symbols = [
            str(decision.symbol or "").upper()
            for decision in decisions[:12]
            if str(decision.symbol or "").strip()
        ]
        for user in users:
            user_id = int(user["id"])
            can_spend, credit_before = self.db.user_has_credit_for(user_id, per_user_credits)
            if not can_spend:
                summary["skipped_users"].append(
                    {
                        "user_id": user_id,
                        "username": user.get("username"),
                        "reason": "insufficient_credits_or_daily_budget",
                        "required_credits": round(per_user_credits, 6),
                        "balance": credit_before.get("credit_balance"),
                        "daily_remaining": credit_before.get("daily_credits_remaining"),
                    }
                )
                continue
            try:
                self.db.charge_user_credits(
                    user_id,
                    per_user_base,
                    "Shared AI opportunity cycle",
                    {
                        "usage_scope": usage_scope,
                        "shared_cycle": True,
                        "billing_model": "split_across_active_users",
                        "participant_count": participant_count,
                        "symbol_count": len(symbols),
                        "symbols_sample": symbols[:25],
                        "decision_count": len(decisions),
                        "top_symbols": top_symbols,
                        "llm_usage_total": usage,
                        "llm_activity": activity,
                        "credit_billing_total": billing,
                        "credit_billing_user": {
                            "base_credits": round(per_user_base, 6),
                            "charged_credits": round(per_user_credits, 6),
                            "platform_margin_pct": billing.get("platform_margin_pct"),
                        },
                    },
                    margin_pct=float(billing.get("platform_margin_pct") or 0.0),
                    minimum_charge=0.0,
                )
                summary["charged_users"] += 1
            except ValueError as exc:
                summary["skipped_users"].append(
                    {
                        "user_id": user_id,
                        "username": user.get("username"),
                        "reason": str(exc),
                        "required_credits": round(per_user_credits, 6),
                    }
                )
        self._log(
            "INFO",
            "credits",
            "shared_ai_cycle_billed",
            f"Billed shared AI cycle to {summary['charged_users']} of {summary['participants']} active users",
            summary,
        )
        return summary

    def _shared_llm_cycle_funding_status(self) -> dict[str, Any]:
        settings = getattr(self.strategy, "settings", None)
        if LLM_HARD_DISABLED:
            return {"required": False, "funded": True, "skip_llm": False, "reason": "llm_hard_disabled"}
        if not bool(getattr(settings, "llm_require_funded_shared_cycle", True)):
            return {"required": False, "funded": True, "skip_llm": False}
        if str(getattr(settings, "llm_provider", "offline") or "offline").lower() == "offline":
            return {"required": True, "funded": True, "skip_llm": False, "reason": "llm_provider_offline"}
        if str(getattr(settings, "llm_decision_mode", "offline") or "offline").lower() == "offline":
            return {"required": True, "funded": True, "skip_llm": False, "reason": "llm_decision_mode_offline"}

        estimated_reviews = max(int(getattr(settings, "llm_max_symbols_per_cycle", 8) or 8), 1)
        estimated_tokens_per_review = max(int(getattr(settings, "llm_event_review_estimated_tokens", 12000) or 12000), 1000)
        estimated_usage = {
            "calls": estimated_reviews,
            "total_tokens": estimated_reviews * estimated_tokens_per_review,
            "cost_usd": 0.0,
        }
        billing = credit_breakdown_for_usage(
            estimated_usage,
            tokens_per_credit=getattr(settings, "credit_tokens_per_credit", 10),
            margin_pct=getattr(settings, "credit_platform_margin_pct", 0.20),
        )
        users = [
            user
            for user in self.db.list_users()
            if user.get("role") != "admin" and user.get("active")
        ]
        if not users:
            return {
                "required": True,
                "funded": False,
                "skip_llm": True,
                "reason": "no_active_users_to_fund_shared_llm_cycle",
                "estimated_reviews": estimated_reviews,
                "estimated_tokens": estimated_usage["total_tokens"],
                "estimated_credits": billing.get("charged_credits", 0.0),
            }

        per_user_credits = float(billing.get("charged_credits") or 0.0) / max(len(users), 1)
        funded_users = []
        skipped_users = []
        for user in users:
            user_id = int(user["id"])
            can_spend, credit_summary = self.db.user_has_credit_for(user_id, per_user_credits)
            if can_spend:
                funded_users.append(user_id)
            else:
                skipped_users.append(
                    {
                        "user_id": user_id,
                        "username": user.get("username"),
                        "reason": "insufficient_credits_or_daily_budget",
                        "required_credits": round(per_user_credits, 6),
                        "balance": credit_summary.get("credit_balance"),
                        "daily_remaining": credit_summary.get("daily_credits_remaining"),
                    }
                )
        funded = bool(funded_users)
        return {
            "required": True,
            "funded": funded,
            "skip_llm": not funded,
            "reason": None if funded else "no_active_user_can_fund_estimated_shared_llm_cycle",
            "participant_count": len(users),
            "funded_user_count": len(funded_users),
            "skipped_users": skipped_users[:12],
            "estimated_reviews": estimated_reviews,
            "estimated_tokens": estimated_usage["total_tokens"],
            "estimated_credits": billing.get("charged_credits", 0.0),
            "estimated_per_user_credits": round(per_user_credits, 6),
        }

    def _auto_exit_followed_signal_ideas_for_user(
        self,
        user_id: int,
        user: dict[str, Any],
        exit_symbols: set[str],
    ) -> list[str]:
        exited: list[str] = []
        followed = self.db.user_followed_signal_ideas(user_id, 200)
        lifecycle_exit_statuses = {"EXIT_SIGNAL", "STOP_HIT", "TARGET_3_HIT", "EXPIRED"}
        lifecycle_exit_labels = {"exit_signal", "stopped", "target_3_hit", "expired"}
        for item in followed:
            symbol = str(item.get("symbol") or "").upper()
            if not symbol or symbol in exited:
                continue
            status = str(item.get("status") or "").upper()
            lifecycle = str(item.get("lifecycle_status") or "").lower()
            should_exit = (
                symbol in exit_symbols
                or status in lifecycle_exit_statuses
                or lifecycle in lifecycle_exit_labels
            )
            if not should_exit:
                continue
            rows = self.db.exit_user_follow_position(user_id, symbol, reason=f"shared_auto_exit_{lifecycle or status.lower()}")
            if rows:
                exited.append(symbol)
                self._log(
                    "INFO",
                    "user_session",
                    "shared_signal_auto_exit",
                    f"Auto-exit requested for {symbol} for {user.get('username')}",
                    {
                        "user_id": user_id,
                        "username": user.get("username"),
                        "symbol": symbol,
                        "mode": item.get("mode"),
                        "status": status,
                        "lifecycle_status": lifecycle,
                        "exited_rows": len(rows),
                    },
                )
        return exited

    def _auto_follow_cash_for_user(self, user_id: int, user: dict[str, Any], market: str) -> float:
        market = "US" if str(market or "").upper() == "US" else "IN"
        cash_by_market = user.get("paper_cash_by_market") if isinstance(user.get("paper_cash_by_market"), dict) else {}
        base_cash = _float_or_none(cash_by_market.get(market)) if cash_by_market else None
        if base_cash is None:
            base_cash = float(self.strategy.settings.initial_cash_inr or 0.0)
        tracked = self.db.user_followed_signal_ideas(user_id, 200, market_region=market)
        invested = sum(
            float(item.get("invested_amount") or 0.0)
            for item in tracked
            if str(item.get("mode") or "").upper() in {"PAPER", "LIVE"} and int(item.get("qty") or 0) > 0
        )
        return max(float(base_cash or 0.0) - invested, 0.0)

    def _auto_follow_sizing(
        self,
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
            max_position_pct=float(self.strategy.settings.max_position_pct or 0.25),
            size_multiplier=size_multiplier,
            market_region=market_region,
            settings=self.strategy.settings,
            stop_loss=stop_loss,
            confidence=confidence,
            avg_daily_turnover=avg_daily_turnover,
        )

    def _auto_follow_amount(
        self,
        cash: float,
        price: float,
        *,
        size_multiplier: float = 1.0,
        market_region: str = "IN",
    ) -> float:
        sizing = self._auto_follow_sizing(cash, price, size_multiplier=size_multiplier, market_region=market_region)
        return float(sizing.get("amount") or 0.0)

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
        fetch_limit = max(0, int(getattr(self.strategy.settings, "candle_fetch_symbols_per_cycle", 80) or 0))
        stats["fetch_symbols_before_limit"] = len(selected)
        stats["fetch_symbol_limit"] = fetch_limit
        if fetch_limit > 0 and len(selected) > fetch_limit:
            selected = selected[:fetch_limit]
            stats["fetch_symbols_truncated"] = True
        else:
            stats["fetch_symbols_truncated"] = False
        stats["fetch_symbols"] = len(selected)
        return selected, stats

    def _candle_backfill_universe(
        self,
        universe: list[dict[str, Any]],
        excluded_symbols: set[str] | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        settings = self.strategy.settings
        enabled = bool(getattr(settings, "candle_backfill_enabled", True))
        limit = max(0, int(getattr(settings, "candle_backfill_symbols_per_cycle", 40) or 0))
        excluded_symbols = {str(symbol or "").upper() for symbol in (excluded_symbols or set())}
        stats: dict[str, Any] = {
            "enabled": enabled,
            "limit": limit,
            "universe_symbols": len(universe),
            "excluded_active_fetch_symbols": len(excluded_symbols),
            "selected_symbols": 0,
            "missing_or_short_history": 0,
            "stale_history": 0,
            "recently_attempted": 0,
            "cache_ready": 0,
            "cursor_before": self._candle_backfill_cursor,
            "cursor_after": self._candle_backfill_cursor,
            "min_daily_candles": max(1, int(getattr(settings, "candle_backfill_min_daily_candles", 55) or 55)),
            "min_intraday_candles": max(0, int(getattr(settings, "candle_backfill_min_intraday_candles", 20) or 20)),
            "min_weekly_candles": max(0, int(getattr(settings, "candle_backfill_min_weekly_candles", 20) or 20)),
        }
        if not enabled or limit <= 0 or not universe:
            return [], stats

        symbols = [str(row.get("symbol") or "").upper() for row in universe if row.get("symbol")]
        coverage_by_symbol = self.db.candle_coverage_by_symbol(symbols)
        selected: list[dict[str, Any]] = []
        now = datetime.now(timezone.utc)
        retry_cooldown = timedelta(hours=max(1, int(getattr(settings, "candle_backfill_retry_hours", 6) or 6)))
        stale_after = timedelta(hours=20)
        stored_cursor = _int_or_none(self.db.get_state("candle_backfill_cursor", self._candle_backfill_cursor))
        cursor = (stored_cursor if stored_cursor is not None else self._candle_backfill_cursor) % len(universe)
        stats["cursor_before"] = cursor
        next_cursor = cursor

        for offset in range(len(universe)):
            row = universe[(cursor + offset) % len(universe)]
            symbol = str(row.get("symbol") or "").upper()
            if not symbol or symbol in excluded_symbols:
                continue
            next_cursor = (cursor + offset + 1) % len(universe)
            last_attempt = self._last_candle_fetch_at.get(symbol)
            if last_attempt and now - last_attempt < retry_cooldown:
                stats["recently_attempted"] += 1
                continue
            coverage = coverage_by_symbol.get(symbol) or {}
            gaps = self._candle_coverage_gaps(row, coverage, now, stale_after)
            if not gaps:
                stats["cache_ready"] += 1
                continue
            if "stale_history" in gaps and len(gaps) == 1:
                stats["stale_history"] += 1
            else:
                stats["missing_or_short_history"] += 1
            selected.append(row)
            if len(selected) >= limit:
                break

        self._candle_backfill_cursor = next_cursor
        self.db.set_state("candle_backfill_cursor", next_cursor)
        stats["cursor_after"] = next_cursor
        stats["selected_symbols"] = len(selected)
        stats["sample_symbols"] = [row.get("symbol") for row in selected[:20]]
        stats["provider_sources"] = sorted(_provider_source_names(self.market_data))
        return selected, stats

    def _candle_coverage_gaps(
        self,
        row: dict[str, Any],
        coverage: dict[str, Any],
        now: datetime,
        stale_after: timedelta,
    ) -> list[str]:
        settings = self.strategy.settings
        min_daily = max(1, int(getattr(settings, "candle_backfill_min_daily_candles", 55) or 55))
        min_intraday = max(0, int(getattr(settings, "candle_backfill_min_intraday_candles", 20) or 20))
        min_weekly = max(0, int(getattr(settings, "candle_backfill_min_weekly_candles", 20) or 20))
        daily = coverage.get("daily") if isinstance(coverage.get("daily"), dict) else {}
        intraday = coverage.get("intraday") if isinstance(coverage.get("intraday"), dict) else {}
        weekly = coverage.get("weekly") if isinstance(coverage.get("weekly"), dict) else {}
        analysis = coverage.get("analysis") if isinstance(coverage.get("analysis"), dict) else {}
        daily_count = int(daily.get("count") or 0)
        intraday_count = int(intraday.get("count") or 0)
        weekly_count = int(weekly.get("count") or 0)
        gaps: list[str] = []
        market = market_region_for_row(row)
        if market == "IN":
            if self._provider_supports_live_minutes_for_market("IN"):
                daily_count = _coverage_source_count(coverage, "daily", ("upstox", "kite", "nubra", "indstocks-live"))
                intraday_count = _coverage_source_count(coverage, "intraday", ("upstox", "kite", "nubra", "indstocks-live"))
                weekly_count = _coverage_source_count(coverage, "weekly", ("upstox", "kite", "nubra", "indstocks-live"))
            if daily_count < min_daily:
                gaps.append("daily_history")
            if min_intraday and self._provider_supports_live_minutes_for_market("IN") and intraday_count < min_intraday:
                gaps.append("intraday_history")
            if min_weekly and weekly_count < min_weekly:
                gaps.append("weekly_history")
        elif market == "US":
            if self._provider_supports_live_minutes_for_market("US"):
                daily_count = _coverage_source_count(coverage, "daily", ("alpaca", "polygon"))
                intraday_count = _coverage_source_count(coverage, "intraday", ("alpaca", "polygon"))
            if daily_count < min_daily:
                gaps.append("daily_history")
            if min_intraday and self._provider_supports_live_minutes_for_market("US") and intraday_count < min_intraday:
                gaps.append("intraday_history")
        elif daily_count < min_daily:
            gaps.append("daily_history")
        latest_ts = analysis.get("latest_ts") or daily.get("latest_ts") or intraday.get("latest_ts")
        latest = _parse_iso_datetime(latest_ts)
        if latest is None:
            if not gaps:
                gaps.append("missing_timestamp")
        else:
            if latest.tzinfo is None:
                latest = latest.replace(tzinfo=timezone.utc)
            if now - latest.astimezone(timezone.utc) > stale_after and not gaps:
                gaps.append("stale_history")
        return gaps

    def _provider_supports_live_minutes_for_market(self, market: str) -> bool:
        names = _provider_source_names(self.market_data)
        normalized = {name.lower() for name in names}
        if market == "US":
            return any(("alpaca" in name or "polygon" in name) and "not-connected" not in name for name in normalized)
        return any(
            any(token in name for token in ("upstox", "kite", "nubra", "indstocks-live"))
            and "not-connected" not in name
            for name in normalized
        )

    def _relative_strength_benchmark_rows(self) -> list[dict[str, Any]]:
        settings = self.strategy.settings
        rows: list[dict[str, Any]] = []
        key_map = _parse_symbol_map(getattr(settings, "rs_benchmark_instrument_keys_in", ""))
        if self.market_region in {"IN", "BOTH"}:
            for symbol in _csv_symbols(getattr(settings, "rs_benchmark_symbols_in", "")):
                rows.append(
                    {
                        "symbol": symbol,
                        "name": "Nifty 500" if symbol == "NIFTY500" else "Nifty 50" if symbol in {"NIFTY50", "NIFTY"} else symbol,
                        "exchange": "NSE",
                        "yahoo_symbol": "^CRSLDX" if symbol == "NIFTY500" else "^NSEI" if symbol in {"NIFTY50", "NIFTY"} else "",
                        "upstox_instrument_key": key_map.get(symbol, ""),
                        "sector": "Benchmark",
                        "industry": "Market Index",
                        "enabled": 0,
                    }
                )
        if self.market_region in {"US", "BOTH"}:
            for symbol in _csv_symbols(getattr(settings, "rs_benchmark_symbols_us", "")):
                rows.append(
                    {
                        "symbol": symbol,
                        "name": symbol,
                        "exchange": "NYSEARCA" if symbol in {"SPY", "QQQ"} else "NASDAQ",
                        "yahoo_symbol": symbol,
                        "sector": "Benchmark",
                        "industry": "Market ETF",
                        "enabled": 0,
                    }
                )
        seen: set[str] = set()
        unique: list[dict[str, Any]] = []
        for row in rows:
            symbol = row["symbol"]
            if symbol and symbol not in seen:
                unique.append(row)
                seen.add(symbol)
        return unique

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

    def _raw_scan_universe_for_cycle(
        self,
        scan_universe: list[dict[str, Any]],
        positions: dict[str, dict[str, Any]],
        *,
        dynamic_scan_enabled: bool,
        raw_scan_limit: int | None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        configured_limit = max(0, int(raw_scan_limit or 0)) if raw_scan_limit is not None else None
        if (
            dynamic_scan_enabled
            and configured_limit
            and scan_universe
            and configured_limit < len(scan_universe)
        ):
            return list(scan_universe), {
                "configured_raw_limit": configured_limit,
                "full_live_quote_sweep": True,
                "rotation_enabled": False,
                "quote_sweep_symbols": len(scan_universe),
                "reason": "live_rally_radar_requires_all_open_symbols",
            }
        if dynamic_scan_enabled and not configured_limit and scan_universe:
            selected, cap_policy = self._market_capped_scan_universe(scan_universe, positions)
            if cap_policy.get("cap_applied"):
                return selected, {
                    "configured_raw_limit": configured_limit,
                    "full_live_quote_sweep": False,
                    "rotation_enabled": True,
                    "quote_sweep_symbols": len(selected),
                    "reason": "market_open_symbol_cap",
                    **cap_policy,
                }

        selected = self._cycle_universe(scan_universe, positions, limit_override=configured_limit)
        return selected, {
            "configured_raw_limit": configured_limit,
            "full_live_quote_sweep": bool(dynamic_scan_enabled and (not configured_limit or configured_limit >= len(scan_universe))),
            "rotation_enabled": bool(
                configured_limit
                and selected
                and configured_limit < len(scan_universe)
            ),
            "quote_sweep_symbols": len(selected),
            "reason": "configured_all_symbols" if dynamic_scan_enabled else "static_cycle_universe",
        }

    def _market_capped_scan_universe(
        self,
        scan_universe: list[dict[str, Any]],
        positions: dict[str, dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        by_market: dict[str, list[dict[str, Any]]] = {}
        for row in scan_universe:
            by_market.setdefault(market_region_for_row(row), []).append(row)
        selected: list[dict[str, Any]] = []
        selected_symbols: set[str] = set()
        market_caps: dict[str, int] = {}
        market_counts: dict[str, int] = {}
        market_selected: dict[str, int] = {}
        cap_applied = False
        for market in sorted(by_market):
            rows = by_market[market]
            market_counts[market] = len(rows)
            cap = self._market_open_symbol_cap(market)
            market_caps[market] = cap
            if cap > 0 and cap < len(rows):
                cap_applied = True
                chosen = self._cycle_rows(rows, cap)
            else:
                chosen = rows
            for row in chosen:
                symbol = str(row.get("symbol") or "").upper()
                if not symbol or symbol in selected_symbols:
                    continue
                selected.append(row)
                selected_symbols.add(symbol)
            market_selected[market] = len(chosen)
        if positions:
            rows_by_symbol = {str(row.get("symbol") or "").upper(): row for row in scan_universe}
            for symbol in positions:
                normalized = str(symbol or "").upper()
                if normalized in selected_symbols:
                    continue
                row = rows_by_symbol.get(normalized)
                if not row:
                    continue
                selected.append(row)
                selected_symbols.add(normalized)
                market = market_region_for_row(row)
            market_selected[market] = market_selected.get(market, 0) + 1
        return selected, {
            "cap_applied": cap_applied,
            "market_caps": market_caps,
            "market_open_symbols": market_counts,
            "market_quote_sweep_symbols": market_selected,
        }

    def _annotate_thin_history_diagnostics(
        self,
        universe: list[dict[str, Any]],
        positions: dict[str, dict[str, Any]],
        raw_cached_sets: dict[str, dict[str, Any]],
        scan_summary: dict[str, Any],
    ) -> None:
        thin_history_symbols = {
            str(row.get("symbol") or "").upper()
            for row in universe
            if str(row.get("symbol") or "").upper() not in positions
            and _analysis_history_count(raw_cached_sets.get(str(row.get("symbol") or "").upper()) or {}) < 20
        }
        if not thin_history_symbols:
            scan_summary["thin_history_after_prefetch_count"] = 0
            return
        secondary = dict(scan_summary.get("secondary_diagnostic_counts") or {})
        secondary["thin_history_after_prefetch"] = (
            secondary.get("thin_history_after_prefetch", 0) + len(thin_history_symbols)
        )
        scan_summary["secondary_diagnostic_counts"] = secondary
        scan_summary["thin_history_after_prefetch_count"] = len(thin_history_symbols)
        scan_summary["history_diagnostic_symbols"] = sorted(thin_history_symbols)[:25]
        scan_summary["history_diagnostic_policy"] = (
            "diagnostic_only_full_decision_preserved; stale or missing candles may block candle-dependent setups later"
        )

    def _trim_universe_to_decision_target(
        self,
        universe: list[dict[str, Any]],
        positions: dict[str, dict[str, Any]],
        scan_summary: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        target = _int_or_none(scan_summary.get("target_decision_symbols") or scan_summary.get("candidate_limit"))
        if not target or target <= 0 or len(universe) <= target:
            return universe, {"trimmed": False, "target": target or 0, "before": len(universe), "after": len(universe)}
        position_symbols = {str(symbol or "").upper() for symbol in positions}
        sorted_rows = sorted(
            universe,
            key=lambda row: (
                0 if str(row.get("symbol") or "").upper() in position_symbols else 1,
                int(row.get("_opportunity_rank") or 999999),
                -float(row.get("_opportunity_score") or 0.0),
                str(row.get("symbol") or ""),
            ),
        )
        trimmed = sorted_rows[:target]
        trimmed_symbols = {str(row.get("symbol") or "").upper() for row in trimmed}
        for row in sorted_rows[target:]:
            symbol = str(row.get("symbol") or "").upper()
            if symbol in position_symbols and symbol not in trimmed_symbols:
                trimmed.append(row)
                trimmed_symbols.add(symbol)
        return trimmed, {
            "trimmed": True,
            "target": target,
            "before": len(universe),
            "after": len(trimmed),
            "kept_position_symbols": sorted(position_symbols & trimmed_symbols),
        }

    def _market_open_symbol_cap(self, market: str) -> int:
        settings = self.strategy.settings
        if str(market or "").upper() == "US":
            return max(0, int(getattr(settings, "dynamic_scan_max_open_symbols_us", 1000) or 0))
        return max(0, int(getattr(settings, "dynamic_scan_max_open_symbols_in", 0) or 0))

    def _cycle_rows(self, rows: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
        limit = max(0, int(limit or 0))
        if limit <= 0 or limit >= len(rows):
            return list(rows)
        start = self._universe_cursor % len(rows)
        selected = [rows[(start + index) % len(rows)] for index in range(limit)]
        self._universe_cursor = (start + limit) % len(rows)
        return selected

    def _cycle_universe(
        self,
        full_universe: list[dict[str, Any]],
        positions: dict[str, dict[str, Any]],
        limit_override: int | None = None,
    ) -> list[dict[str, Any]]:
        limit = self.universe_symbols_per_cycle if limit_override is None else max(0, int(limit_override or 0))
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

    def _merge_market_action_universe(
        self,
        raw_universe: list[dict[str, Any]],
        scan_universe: list[dict[str, Any]],
        market_action_summary: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        events_by_symbol = market_action_summary.get("events_by_symbol") if isinstance(market_action_summary, dict) else {}
        if not isinstance(events_by_symbol, dict) or not events_by_symbol:
            return raw_universe, {"enabled": bool(market_action_summary.get("enabled")), "forced_symbols": [], "added_symbols": []}

        events_by_symbol = {str(symbol).upper(): event for symbol, event in events_by_symbol.items() if isinstance(event, dict)}
        rows_by_symbol = {str(row.get("symbol") or "").upper(): row for row in scan_universe}
        selected: list[dict[str, Any]] = []
        selected_symbols: set[str] = set()
        added_symbols: list[str] = []

        for row in raw_universe:
            symbol = str(row.get("symbol") or "").upper()
            if not symbol:
                continue
            selected_symbols.add(symbol)
            event = events_by_symbol.get(symbol)
            selected.append({**row, "_market_action": event} if event else row)

        for symbol, event in events_by_symbol.items():
            if symbol in selected_symbols:
                continue
            row = rows_by_symbol.get(symbol)
            if not row:
                continue
            selected.append({**row, "_market_action": event})
            selected_symbols.add(symbol)
            added_symbols.append(symbol)

        return selected, {
            "enabled": True,
            "forced_symbols": [symbol for symbol in events_by_symbol if symbol in selected_symbols],
            "added_symbols": added_symbols,
            "events_found": len(events_by_symbol),
            "source": market_action_summary.get("source"),
        }

    def _merge_tomorrow_plan_universe(
        self,
        selected_universe: list[dict[str, Any]],
        scan_universe: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        plan = self.db.latest_tomorrow_plan(self.market_region)
        items: list[dict[str, Any]] = []
        if isinstance(plan, dict) and isinstance(plan.get("by_market"), dict):
            for market_plan in plan.get("by_market", {}).values():
                if isinstance(market_plan, dict) and isinstance(market_plan.get("items"), list):
                    items.extend([item for item in market_plan.get("items") if isinstance(item, dict)])
        elif isinstance(plan, dict) and isinstance(plan.get("items"), list):
            items = [item for item in plan.get("items") if isinstance(item, dict)]
        if not isinstance(items, list) or not items:
            return selected_universe, {"enabled": False, "forced_symbols": [], "added_symbols": []}
        allowed_sections = {"ready_at_open", "near_breakout", "news_watch", "position_actions"}
        planned_symbols: list[str] = []
        seen_plan_symbols: set[str] = set()
        planned_items_by_symbol: dict[str, dict[str, Any]] = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            section = str(item.get("section") or "").lower()
            action = str(item.get("action") or "").upper()
            symbol = str(item.get("symbol") or "").upper()
            if symbol and symbol not in seen_plan_symbols and section in allowed_sections and action != "AVOID":
                planned_symbols.append(symbol)
                seen_plan_symbols.add(symbol)
                planned_items_by_symbol[symbol] = item
        if not planned_symbols:
            return selected_universe, {"enabled": True, "forced_symbols": [], "added_symbols": [], "reason": "no_tradeable_plan_symbols"}

        rows_by_symbol = {str(row.get("symbol") or "").upper(): row for row in scan_universe}
        selected: list[dict[str, Any]] = []
        selected_symbols: set[str] = set()
        for row in selected_universe:
            symbol = str(row.get("symbol") or "").upper()
            if not symbol or symbol in selected_symbols:
                continue
            selected_symbols.add(symbol)
            if symbol in planned_symbols:
                selected.append(self._tomorrow_plan_row(row, planned_items_by_symbol.get(symbol)))
            else:
                selected.append(row)

        added_symbols: list[str] = []
        missing_symbols: list[str] = []
        for symbol in planned_symbols[:60]:
            if symbol in selected_symbols:
                continue
            row = rows_by_symbol.get(symbol)
            if not row:
                missing_symbols.append(symbol)
                continue
            selected.append(self._tomorrow_plan_row(row, planned_items_by_symbol.get(symbol)))
            selected_symbols.add(symbol)
            added_symbols.append(symbol)
        return selected, {
            "enabled": True,
            "plan_date": plan.get("plan_date"),
            "forced_symbols": [symbol for symbol in planned_symbols if symbol in selected_symbols],
            "added_symbols": added_symbols,
            "missing_symbols": missing_symbols[:20],
            "source": "tomorrow_plan",
        }

    @staticmethod
    def _tomorrow_plan_row(row: dict[str, Any], item: dict[str, Any] | None) -> dict[str, Any]:
        item = item if isinstance(item, dict) else {}
        compact_item = {
            key: item.get(key)
            for key in (
                "plan_date",
                "market_region",
                "section",
                "action",
                "trigger_price",
                "max_entry",
                "stop_loss",
                "target1",
                "score",
                "confidence",
                "strategy",
                "rationale",
                "validation",
            )
            if item.get(key) not in (None, "")
        }
        return {**row, "_tomorrow_plan": True, "_tomorrow_plan_item": compact_item}

    def _fallback_quoted_universe(
        self,
        full_universe: list[dict[str, Any]],
        quotes: dict[str, Any],
        positions: dict[str, dict[str, Any]],
        limit: int,
    ) -> list[dict[str, Any]]:
        selected: list[dict[str, Any]] = []
        selected_symbols: set[str] = set()
        for row in full_universe:
            symbol = row["symbol"]
            if symbol in positions:
                selected.append(row)
                selected_symbols.add(symbol)
        ranked = sorted(
            [row for row in full_universe if row["symbol"] in quotes],
            key=lambda row: self._news_probe_priority(row, quotes[row["symbol"]]),
            reverse=True,
        )
        for row in ranked:
            symbol = row["symbol"]
            if symbol in selected_symbols:
                continue
            selected.append(row)
            selected_symbols.add(symbol)
            if len(selected) >= limit:
                break
        return selected

    def _news_probe_universe(
        self,
        full_universe: list[dict[str, Any]],
        quotes: dict[str, Any],
        positions: dict[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        limit = max(0, int(getattr(self.strategy.settings, "dynamic_scan_news_probe_limit", 16) or 0))
        if limit <= 0 or not full_universe:
            return []
        selected: list[dict[str, Any]] = []
        selected_symbols: set[str] = set()

        def add(row: dict[str, Any]) -> None:
            symbol = row["symbol"]
            if symbol in selected_symbols:
                return
            selected.append(row)
            selected_symbols.add(symbol)

        for row in full_universe:
            if row["symbol"] in positions:
                add(row)
                if len(selected) >= limit:
                    return selected

        ranked = sorted(
            [row for row in full_universe if row["symbol"] in quotes],
            key=lambda row: self._news_probe_priority(row, quotes[row["symbol"]]),
            reverse=True,
        )
        for row in ranked[: max(1, limit // 2)]:
            add(row)
            if len(selected) >= limit:
                return selected

        start = self._news_probe_cursor % len(full_universe)
        for index in range(len(full_universe)):
            row = full_universe[(start + index) % len(full_universe)]
            if row["symbol"] not in quotes:
                continue
            add(row)
            if len(selected) >= limit:
                self._news_probe_cursor = (start + index + 1) % len(full_universe)
                return selected
        self._news_probe_cursor = (start + len(full_universe)) % len(full_universe)
        return selected

    def _prepend_market_action_news_rows(
        self,
        news_probe_rows: list[dict[str, Any]],
        raw_universe: list[dict[str, Any]],
        quotes: dict[str, Any],
        market_action_summary: dict[str, Any],
    ) -> list[dict[str, Any]]:
        events_by_symbol = market_action_summary.get("events_by_symbol") if isinstance(market_action_summary, dict) else {}
        if not isinstance(events_by_symbol, dict) or not events_by_symbol:
            return news_probe_rows
        limit = max(0, int(getattr(self.strategy.settings, "market_action_priority_news_limit", 40) or 0))
        if limit <= 0:
            return news_probe_rows
        rows_by_symbol = {str(row.get("symbol") or "").upper(): row for row in raw_universe}
        priority_rows: list[dict[str, Any]] = []
        for symbol in list(events_by_symbol)[:limit]:
            normalized = str(symbol or "").upper()
            row = rows_by_symbol.get(normalized)
            if not row or normalized not in quotes:
                continue
            event = events_by_symbol.get(symbol)
            priority_rows.append({**row, "_market_action": event} if isinstance(event, dict) else row)
        selected: list[dict[str, Any]] = []
        selected_symbols: set[str] = set()
        for row in [*priority_rows, *news_probe_rows]:
            symbol = str(row.get("symbol") or "").upper()
            if not symbol or symbol in selected_symbols:
                continue
            selected.append(row)
            selected_symbols.add(symbol)
        return selected

    def _news_probe_priority(self, row: dict[str, Any], quote: Any) -> float:
        price = _float_or_none(getattr(quote, "price", 0.0)) or 0.0
        volume = _float_or_none(getattr(quote, "volume", 0.0)) or 0.0
        high = _float_or_none(getattr(quote, "high", None))
        low = _float_or_none(getattr(quote, "low", None))
        open_price = _float_or_none(getattr(quote, "open", None))
        turnover = price * volume
        range_position = 0.0
        if high and low and high > low:
            range_position = (price - low) / (high - low)
        day_change_abs = abs((price - open_price) / open_price) if open_price else 0.0
        return turnover + (turnover * range_position * 0.25) + (turnover * min(day_change_abs, 0.08))

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

    def snapshot(self, *, lightweight: bool = False) -> dict[str, Any]:
        quotes = self._dashboard_quotes()
        market_health = self._market_health(quotes)
        macro_calendar_context = self.db.get_state("macro_calendar_context", {})
        universe_summary = self.db.universe_summary()
        options_context = self.db.get_state("options_intelligence_context", {})
        opportunity_scan = self.db.get_state("opportunity_scan", {})
        base_payload: dict[str, Any] = {
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
            "quotes": quotes,
            "decisions": [],
            "decisions_by_market": {"IN": [], "US": []},
            "suggestions": [],
            "signal_ideas": [],
            "suggestions_by_market": {"IN": [], "US": []},
            "strategy_plans": [],
            "orders": [],
            "equity_curve": [],
            "strategy_metrics": self.db.strategy_metrics(),
            "sentiment": self.db.latest_sentiment(40),
            "universe_size": universe_summary.get("enabled"),
            "market_health": market_health,
            "market_session": self.db.get_state("market_session_context", {}),
            "candle_backfill": self.db.get_state("candle_backfill_plan", {}),
            "tomorrow_prep": self.db.get_state("tomorrow_prep_context", {}),
            "tomorrow_plan": self.db.latest_tomorrow_plan(self.market_region),
            "market_data_health": _market_data_diagnostics(self.market_data),
            "macro_context": self.db.get_state("macro_context", {}),
            "institutional_context": self.db.get_state("institutional_context", {}),
            "market_breadth": self.db.get_state("market_breadth_context", {}),
            "sector_rotation_context": _sector_rotation_summary(self.db.get_state("sector_rotation_context", {})),
            "options_intelligence": _options_intelligence_summary(options_context),
            "market_action_radar": self.db.get_state("market_action_radar", {}),
            "opportunity_scan": opportunity_scan,
            "decision_diagnostics": self.db.get_state("decision_diagnostics", {}),
            "pre_catalyst_discovery": self.db.get_state("pre_catalyst_discovery", {}),
            "upcoming_macro_events": (macro_calendar_context or {}).get("next_10", []),
            "self_audit": self.db.get_state("self_audit", {}),
            "shared_auto_trade": self._last_shared_auto_trade,
            "llm_usage": self.db.llm_usage_summary(),
        }
        if lightweight:
            return base_payload

        decisions = _with_detail_urls(self.db.latest_decision_summaries(80), "decisions")
        decisions_by_market = {
            "IN": _with_detail_urls(self.db.latest_decision_summaries(80, market_region="IN"), "decisions"),
            "US": _with_detail_urls(self.db.latest_decision_summaries(80, market_region="US"), "decisions"),
        }
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
        market_health["portfolio_equity"] = portfolio.get("equity")
        positions = self._positions_with_exit_plans(raw_positions, order_audit_history, quotes, market_health, macro_calendar_context)
        suggestions = self.db.latest_signal_ideas(40)
        suggestions_by_market = {
            "IN": self.db.latest_signal_ideas(25, market_region="IN"),
            "US": self.db.latest_signal_ideas(25, market_region="US"),
        }
        self_audit = base_payload.get("self_audit")
        if not self_audit or "overall_score_pct" not in self_audit:
            self_audit = build_self_audit(
                raw_positions,
                quotes,
                portfolio,
                market_health,
                macro_calendar_context,
            )
        base_payload.update(
            {
                "portfolio": portfolio,
                "portfolio_by_market": portfolio_by_market,
                "positions": positions,
                "decisions": decisions,
                "decisions_by_market": decisions_by_market,
                "suggestions": suggestions,
                "signal_ideas": suggestions,
                "suggestions_by_market": suggestions_by_market,
                "strategy_plans": self.db.strategy_plans(),
                "orders": orders,
                "equity_curve": self.db.recent_equity(120),
                "performance": self.db.performance_summary(),
                "universe_size": len(self.db.get_universe(enabled_only=True, market_region=self.market_region)),
                "self_audit": self_audit,
            }
        )
        return base_payload

    def _dashboard_quotes(self) -> list[dict[str, Any]]:
        region = normalize_market_region(self.market_region or "BOTH", default="BOTH")
        if region == "BOTH":
            quotes = self.db.latest_quotes(180, market_region="IN")
            quotes.extend(self.db.latest_quotes(180, market_region="US"))
            return quotes
        return self.db.latest_quotes(300, market_region=region)

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
        session_context = self.db.get_state("market_session_context", {}) or market_session_context(self.market_region)
        market_open = bool((session_context or {}).get("is_any_market_open"))
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
            "market_session": session_context,
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


def _normalize_signal_execution_mode(value: Any) -> str:
    mode = str(value or "SIGNAL_ONLY").strip().upper()
    if mode in {"PAPER", "AUTO_PAPER"}:
        return "AUTO_PAPER"
    if mode in {"LIVE", "AUTO_LIVE"}:
        return "AUTO_LIVE"
    return "SIGNAL_ONLY"


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


def _provider_source_names(provider: Any) -> set[str]:
    names: set[str] = set()

    def visit(item: Any) -> None:
        if item is None:
            return
        source_name = str(getattr(item, "source_name", "") or "")
        if source_name:
            names.add(source_name)
        for attr in ("primary", "fallback", "india_provider", "us_provider"):
            child = getattr(item, attr, None)
            if child is not None and child is not item:
                visit(child)

    visit(provider)
    return names


def _coverage_source_count(coverage: dict[str, Any], timeframe: str, tokens: tuple[str, ...]) -> int:
    sources = coverage.get("sources") if isinstance(coverage.get("sources"), dict) else {}
    count = 0
    for source, payload in sources.items():
        normalized = str(source or "").lower()
        if not normalized or not any(token in normalized for token in tokens):
            continue
        if not _source_matches_timeframe(normalized, timeframe):
            continue
        if isinstance(payload, dict):
            count += int(payload.get("count") or 0)
    return count


def _source_matches_timeframe(source: str, timeframe: str) -> bool:
    normalized = str(source or "").lower()
    if timeframe == "daily":
        return normalized == "yahoo-delayed" or ":day" in normalized or ":1day" in normalized
    if timeframe == "weekly":
        return ":week" in normalized or ":1week" in normalized
    if timeframe == "intraday":
        return (
            "minute" in normalized
            or normalized.endswith(":5m")
            or normalized.endswith(":15m")
            or normalized.endswith(":30m")
            or normalized.endswith(":60m")
            or normalized in {"upstox-live", "indstocks-live", "kite-live"}
            or normalized.startswith("nubra")
        )
    return False


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
    normalized = normalize_trade_targets(targets)
    for target in normalized:
        if target.get("rr") == "structure":
            target["rr"] = "3.5_or_structure"
    return normalized


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
    if str(idea.get("fresh_action") or "").upper() != "BUY_NOW":
        return False
    if symbol in fresh_buy_symbols:
        return True
    if not _idea_seen_recently(idea):
        return False
    return _price_inside_entry_zone(idea, cushion_pct=0.003)


def _decision_has_buy_intent(decision: Decision) -> bool:
    if str(decision.action or "").upper() == "BUY":
        return True
    try:
        audit = json.loads(decision.details_json or "{}")
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
    parsed = _parse_iso_datetime(idea.get("last_seen_at") or idea.get("updated_at") or idea.get("first_seen_at"))
    if not parsed:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    age = datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)
    return age <= timedelta(minutes=max(int(minutes or FRESH_BUY_WINDOW_MINUTES), 1))


def _analysis_history_count(candle_sets: dict[str, list[Any]]) -> int:
    candles = candle_sets.get("analysis") or candle_sets.get("daily") or candle_sets.get("intraday") or []
    return len(candles)


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


def _opportunity_scan_by_market(
    summary: dict[str, Any],
    full_universe: list[dict[str, Any]],
    open_universe: list[dict[str, Any]],
    raw_universe: list[dict[str, Any]],
    selected_universe: list[dict[str, Any]],
    news_summary: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    news_summary = news_summary or {}
    rows_by_symbol = {
        str(row.get("symbol") or "").upper(): row
        for row in [*full_universe, *open_universe, *raw_universe, *selected_universe]
        if row.get("symbol")
    }
    full_counts = _market_counts_for_rows(full_universe)
    open_counts = _market_counts_for_rows(open_universe)
    raw_counts = _market_counts_for_rows(raw_universe)
    selected_counts = _market_counts_for_rows(selected_universe)
    top_candidates = summary.get("top_candidates") if isinstance(summary.get("top_candidates"), list) else []
    news_rows = news_summary.get("symbols") if isinstance(news_summary.get("symbols"), list) else []
    last_open_by_market = {}
    last_open_scan = summary.get("last_open_scan") if isinstance(summary.get("last_open_scan"), dict) else {}
    if isinstance(last_open_scan.get("by_market"), dict):
        last_open_by_market = last_open_scan.get("by_market") or {}
    playbook_by_market = summary.get("top_gainers_playbook_by_market") if isinstance(summary.get("top_gainers_playbook_by_market"), dict) else {}

    by_market: dict[str, dict[str, Any]] = {}
    for region in ("IN", "US"):
        region_top = [
            _slim_candidate(item)
            for item in top_candidates
            if isinstance(item, dict) and _candidate_market_region(item, rows_by_symbol) == region
        ]
        region_news = [
            item
            for item in news_rows
            if isinstance(item, dict)
            and _symbol_market_region(str(item.get("symbol") or "").upper(), rows_by_symbol) == region
        ]
        news_events = sum(int(item.get("event_count") or 0) for item in region_news)
        news_headlines = sum(int(item.get("headline_count") or 0) for item in region_news)
        by_market[region] = {
            "enabled": summary.get("enabled", True),
            "mode": summary.get("mode"),
            "market_region": region,
            "scan_paused": bool(summary.get("scan_paused")),
            "scanned_at": summary.get("scanned_at"),
            "raw_symbols": raw_counts.get(region, 0),
            "quoted_symbols": raw_counts.get(region, 0) if summary.get("scan_paused") else None,
            "candidate_limit": summary.get("candidate_limit"),
            "selected_symbols": selected_counts.get(region, 0),
            "tradeable_screening_symbols": sum(
                1 for item in region_top if (item.get("data_quality") or {}).get("tradeable_screening")
            ),
            "enabled_universe_symbols": full_counts.get(region, 0),
            "open_universe_symbols": open_counts.get(region, 0),
            "scanned_symbols_this_cycle": raw_counts.get(region, 0),
            "raw_scan_limit": summary.get("raw_scan_limit"),
            "scan_rotation_enabled": bool(
                summary.get("raw_scan_limit")
                and raw_counts.get(region, 0) > 0
                and raw_counts.get(region, 0) < open_counts.get(region, 0)
            ),
            "news_screened_symbols": len(region_news),
            "news_events_found": news_events,
            "news_headlines_found": news_headlines,
            "news_covered_candidates": sum(
                1
                for item in region_top
                if int((item.get("sentiment") or {}).get("headline_count") or 0) > 0
                or int((item.get("sentiment") or {}).get("event_count") or 0) > 0
            ),
            "verified_catalyst_candidates": sum(
                1 for item in region_top if (item.get("sentiment") or {}).get("positive_catalyst")
            ),
            "positive_news_candidates": sum(
                1 for item in region_top if (item.get("sentiment") or {}).get("positive_catalyst")
            ),
            "top_candidates": region_top[:25],
            "top_gainers_playbook": playbook_by_market.get(region, {}),
            "last_open_scan": last_open_by_market.get(region, {}),
            "readiness_note": summary.get("readiness_note"),
        }
    return by_market


def _market_counts_for_rows(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts = {"IN": 0, "US": 0}
    for row in rows:
        region = market_region_for_row(row)
        if region in counts:
            counts[region] += 1
    return counts


def _candidate_market_region(item: dict[str, Any], rows_by_symbol: dict[str, dict[str, Any]]) -> str:
    explicit = str(item.get("market_region") or "").upper()
    if explicit in {"IN", "US"}:
        return explicit
    return _symbol_market_region(str(item.get("symbol") or "").upper(), rows_by_symbol)


def _symbol_market_region(symbol: str, rows_by_symbol: dict[str, dict[str, Any]]) -> str:
    row = rows_by_symbol.get(str(symbol or "").upper())
    return market_region_for_row(row or {})


def _slim_candidate(item: dict[str, Any]) -> dict[str, Any]:
    return {
        key: item.get(key)
        for key in (
            "symbol",
            "name",
            "market_region",
            "score",
            "bucket",
            "setup",
            "trade_window",
            "btst",
            "data_quality",
            "sentiment",
            "price",
            "turnover",
            "volume_ratio",
            "history_candles",
        )
        if key in item
    }


def _shared_llm_activity_from_decisions(decisions: list[Decision], usage: dict[str, Any]) -> dict[str, Any]:
    selected = 0
    attempted = 0
    failed = 0
    latest_reason = ""
    for decision in decisions:
        details = _json_object(getattr(decision, "details_json", None))
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
        error = review.get("llm_error") or details.get("llm_error")
        attempts = error.get("model_attempts") if isinstance(error, dict) else []
        if attempts or review.get("reviewed") or decision_path.startswith("llm_"):
            attempted += 1
        if error:
            failed += 1
            latest_reason = str((error or {}).get("reason") or (error or {}).get("error") or latest_reason)[:180]
    calls = int(usage.get("calls") or 0)
    billable = bool(calls and not (attempted > 0 and failed >= attempted))
    return {
        "status": "completed_billable" if billable else ("completed_unusable_not_charged" if calls else "not_selected"),
        "selected_symbols": selected,
        "attempted_symbols": attempted,
        "failed_symbols": failed,
        "billable": billable,
        "latest_failure": latest_reason,
    }


def _settings_with_llm_api_disabled(settings: Any) -> Any:
    overrides = {"deepseek_api_key": "", "groq_api_key": ""}
    if settings is None:
        return SimpleNamespace(**overrides)
    if is_dataclass(settings) and not isinstance(settings, type):
        valid_overrides = {key: value for key, value in overrides.items() if hasattr(settings, key)}
        return replace(settings, **valid_overrides)
    values = dict(getattr(settings, "__dict__", {}) or {})
    values.update(overrides)
    if "llm_provider" not in values:
        values["llm_provider"] = getattr(settings, "llm_provider", "offline")
    if "llm_decision_mode" not in values:
        values["llm_decision_mode"] = getattr(settings, "llm_decision_mode", "offline")
    return SimpleNamespace(**values)


def _quote_models_from_rows(rows: list[dict[str, Any]]) -> dict[str, Quote]:
    quotes: dict[str, Quote] = {}
    for row in rows:
        symbol = str(row.get("symbol") or "").upper()
        price = _float_or_none(row.get("price"))
        if not symbol or price is None:
            continue
        quotes[symbol] = Quote(
            symbol=symbol,
            price=price,
            source=str(row.get("source") or "cached"),
            asof=str(row.get("ts") or row.get("asof") or utc_now()),
            open=_float_or_none(row.get("open")),
            high=_float_or_none(row.get("high")),
            low=_float_or_none(row.get("low")),
            close=_float_or_none(row.get("close")),
            volume=_float_or_none(row.get("volume")),
        )
    return quotes


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _csv_symbols(value: Any) -> list[str]:
    symbols: list[str] = []
    seen: set[str] = set()
    for raw in str(value or "").replace(";", ",").split(","):
        symbol = raw.strip().upper()
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        symbols.append(symbol)
    return symbols


def _parse_symbol_map(value: Any) -> dict[str, str]:
    output: dict[str, str] = {}
    for raw in str(value or "").split(","):
        if ":" not in raw:
            continue
        symbol, mapped = raw.split(":", 1)
        symbol = symbol.strip().upper()
        mapped = mapped.strip()
        if symbol and mapped:
            output[symbol] = mapped
    return output


def _parse_iso_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
