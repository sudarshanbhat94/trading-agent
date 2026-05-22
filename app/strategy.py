from __future__ import annotations

import asyncio
import json
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from typing import Any

from .analysis_tools import build_symbol_tool_context, deterministic_score, deterministic_score_breakdown
from .config import Settings
from .indicators import technical_snapshot
from .llm_brain import LLMBrain
from .market_regions import market_region_for_row
from .models import Candle, Decision, Quote, utc_now
from .sentiment import SentimentService
from .signal_quality import FRESH_BUY_MIN_SCORE
from .trading_rules import evaluate_rules_for_context


def should_call_llm(signal: dict[str, Any]) -> bool:
    return _llm_prefilter_reason(signal) is None


def _llm_prefilter_reason(signal: dict[str, Any]) -> str | None:
    context = signal.get("context") if isinstance(signal.get("context"), dict) else signal
    position = context.get("position") if isinstance(context.get("position"), dict) else {}
    try:
        if float(position.get("qty") or 0.0) > 0:
            return None
    except (TypeError, ValueError):
        pass
    # The shortlist review should compare the best market opportunities even
    # when trade gates will force HOLD. Trade blockers are enforced again after
    # review by _llm_buy_block_reason, so analysis breadth does not weaken safety.
    return None


def _llm_buy_block_reason(context: dict[str, Any]) -> str | None:
    system_audit = context.get("system_gate_audit") if isinstance(context.get("system_gate_audit"), dict) else {}
    if system_audit.get("hard_blocked") is True:
        return "system_rules_hard_blocked_llm_buy"

    data_readiness = context.get("data_readiness") if isinstance(context.get("data_readiness"), dict) else {}
    if data_readiness and data_readiness.get("trade_decision_ready") is not True:
        return "data_readiness_not_trade_ready_llm_buy"

    decision_gates = context.get("decision_gate_context") if isinstance(context.get("decision_gate_context"), dict) else {}
    failed_gates = decision_gates.get("failed_gates") if isinstance(decision_gates.get("failed_gates"), list) else []
    if failed_gates:
        return "deterministic_buy_gates_failed_llm_buy"

    return None


def _signal_confidence(signal: dict[str, Any]) -> float:
    try:
        return max(min(float(signal.get("confidence") or 0.0), 1.0), 0.0)
    except (TypeError, ValueError):
        return 0.0


class StrategyEngine:
    def __init__(self, settings: Settings, sentiment: SentimentService, llm: LLMBrain) -> None:
        self.settings = settings
        self.sentiment = sentiment
        self.llm = llm
        self._history: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=80))

    async def evaluate(
        self,
        universe: list[dict[str, Any]],
        quotes: dict[str, Quote],
        positions: dict[str, dict[str, Any]],
        candles_by_symbol: dict[str, list[Candle]] | None = None,
        global_context: dict[str, Any] | None = None,
        institutional_context: dict[str, Any] | None = None,
        options_context: dict[str, Any] | None = None,
        delivery_service: Any | None = None,
        market_breadth: dict[str, Any] | None = None,
        sector_rotation_context: dict[str, Any] | None = None,
        macro_calendar: Any | None = None,
        timeframe_candles_by_symbol: dict[str, dict[str, list[Candle]]] | None = None,
        portfolio_equity: float | None = None,
    ) -> list[Decision]:
        sentiment_scores = await self.sentiment.scores_for_cycle(universe)
        decisions: list[Decision] = []
        llm_reviews = 0
        llm_primary_required = self.settings.llm_decision_mode == "primary" and self.settings.llm_provider != "offline"
        llm_primary = llm_primary_required and self.llm.enabled
        candles_by_symbol = candles_by_symbol or {}
        timeframe_candles_by_symbol = timeframe_candles_by_symbol or {}
        risk_limits = {
            "max_positions": self.settings.max_positions,
            "max_position_pct": min(float(self.settings.max_position_pct), 0.15),
            "max_order_value_pct": min(float(self.settings.max_order_value_pct), 0.15),
            "stop_loss_pct": self.settings.stop_loss_pct,
            "take_profit_pct": self.settings.take_profit_pct,
            "daily_loss_limit_pct": self.settings.daily_loss_limit_pct,
            "min_llm_confidence": self.settings.llm_primary_min_confidence,
            "global_risk_weight": self.settings.global_risk_weight,
            "institutional_risk_weight": self.settings.institutional_risk_weight,
            "llm_candidate_limit": self.settings.llm_max_symbols_per_cycle,
            "portfolio_equity": portfolio_equity or 0.0,
            "execution_cost_bps": _execution_cost_bps(self.settings),
        }
        try:
            performance_feedback = self.sentiment.db.strategy_performance_feedback()
        except Exception:
            performance_feedback = {}

        scan_items: list[dict[str, Any]] = []
        breadth_regime = (market_breadth or {}).get("breadth_regime")
        if breadth_regime == "bear_confirmed":
            self._log_pre_filter("market_breadth_bear_regime_blocked_buys", {"breadth_regime": breadth_regime})
        for row in universe:
            symbol = row["symbol"]
            market_region = market_region_for_row(row)
            quote = quotes.get(symbol)
            if not quote:
                continue
            self._history[symbol].append(quote.price)
            sentiment_score = sentiment_scores.get(symbol, 0.0)
            timeframe_candles = timeframe_candles_by_symbol.get(symbol, {})
            candles = (
                timeframe_candles.get("analysis")
                or timeframe_candles.get("daily")
                or candles_by_symbol.get(symbol, [])
            )
            if candles:
                history = [candle.close for candle in candles]
            else:
                history = list(self._history[symbol])
            technical = technical_snapshot(history)
            macro_event_context = (
                macro_calendar.event_context_for_date(symbol=symbol, market_region=market_region)
                if macro_calendar is not None
                else {}
            )
            symbol_breadth = _market_specific_context(market_breadth or {}, market_region)
            symbol_sector_rotation = _market_specific_context(sector_rotation_context or {}, market_region)
            delivery_data = (
                {
                    "available": False,
                    "market_region": "US",
                    "source": "not_applicable_to_us_market",
                    "data_gap": "NSE delivery bhavcopy is not applicable to US equities.",
                    "delivery_score": 0.0,
                    "net_bias": "neutral",
                }
                if market_region == "US"
                else self._delivery_context(symbol, delivery_service)
            )
            options_data = ((options_context or {}).get("symbols") or {}).get(symbol, {})
            sector_context = ((symbol_sector_rotation or {}).get("symbols") or {}).get(symbol, {})
            pattern_state = self._pattern_state(symbol)
            pre_filter = self._pre_filter_context(
                symbol=symbol,
                row=row,
                quote=quote,
                candles=candles,
                positions=positions,
                delivery_data=delivery_data,
                market_breadth=symbol_breadth or {},
                sector_context=sector_context,
                macro_event_context=macro_event_context,
            )
            context = build_symbol_tool_context(
                row=row,
                quote=quote,
                candles=candles,
                position=positions.get(symbol),
                sentiment_score=sentiment_score,
                risk_limits=risk_limits,
                global_context=global_context,
                institutional_context=institutional_context,
                sentiment_detail=self.sentiment.latest_for_symbol(symbol),
                delivery_data=delivery_data,
                options_data=options_data,
                sector_context=sector_context,
                market_breadth=symbol_breadth,
                macro_event_context=macro_event_context,
                timeframe_candles=timeframe_candles,
                pattern_state=pattern_state,
                performance_feedback=performance_feedback,
                execution_mode=self.settings.execution_mode,
            )
            self._persist_pattern_state_updates(symbol, context)
            context["pre_filter"] = pre_filter
            combined = deterministic_score(context)
            score_breakdown = deterministic_score_breakdown(context)
            action = self._action_from_context(symbol, combined, positions, context, candles_by_symbol)
            confidence = self._confidence_for_action(action, combined, macro_event_context, symbol_breadth)
            scan_items.append(
                {
                    "row": row,
                    "symbol": symbol,
                    "quote": quote,
                    "technical": technical,
                    "sentiment_score": sentiment_score,
                    "sentiment_detail": self.sentiment.latest_for_symbol(symbol),
                    "candles": candles,
                    "timeframe_candles": timeframe_candles,
                    "delivery_data": delivery_data,
                    "options_data": options_data,
                    "sector_context": sector_context,
                    "macro_event_context": macro_event_context,
                    "context": context,
                    "combined": combined,
                    "score_breakdown": score_breakdown,
                    "action": action,
                    "confidence": confidence,
                }
            )
            if len(scan_items) % 5 == 0:
                await asyncio.sleep(0)

        await self._refresh_candidate_sentiment(
            scan_items,
            positions,
            candles_by_symbol,
            risk_limits,
            global_context,
            institutional_context,
            market_breadth,
            performance_feedback,
        )
        self._apply_universe_relative_strength(scan_items, positions, candles_by_symbol)
        ranked = sorted(scan_items, key=self._scan_priority, reverse=True)
        for rank, item in enumerate(ranked, start=1):
            context = item["context"]
            context["universe_scan"] = {
                "symbols_scanned": len(scan_items),
                "rank": rank,
                "llm_candidate_limit": self.settings.llm_max_symbols_per_cycle,
                "priority_score": round(self._scan_priority_score(item), 4),
                "selection_basis": (
                    "LLM primary reviews open positions first for exit risk, then non-HOLD candidates, "
                    "then highest-ranked symbols by combined score, universe relative strength, full-spectrum layers, strategy confidence, technical score, and sentiment"
                ),
            }

        llm_candidate_symbols: set[str] = set()
        llm_selection_details: dict[str, dict[str, Any]] = {}
        if llm_primary:
            llm_prefilter_skips = {
                item["symbol"]: _llm_prefilter_reason(item)
                for item in ranked
                if _llm_prefilter_reason(item) is not None
            }
            llm_skip_reasons: dict[str, int] = defaultdict(int)
            for reason in llm_prefilter_skips.values():
                llm_skip_reasons[str(reason).split(":", 1)[0]] += 1
            llm_candidate_symbols = self._llm_candidate_symbols(ranked)
            llm_selection_details = getattr(self, "_last_llm_selection_details", {}) or {}
            self._log_pre_filter(
                "llm_candidates_selected",
                {
                    "enabled": True,
                    "mode": self.settings.llm_decision_mode,
                    "limit": self.settings.llm_max_symbols_per_cycle,
                    "symbols": sorted(llm_candidate_symbols),
                    "symbols_scanned": len(scan_items),
                    "prefilter_skipped": len(llm_prefilter_skips),
                    "prefilter_skip_reasons": dict(llm_skip_reasons),
                    "event_triggered": bool(getattr(self.settings, "llm_event_triggered_cycles", True)),
                    "selection_details": {
                        symbol: llm_selection_details.get(symbol, {})
                        for symbol in sorted(llm_candidate_symbols)
                    },
                    "provider": self.settings.llm_provider,
                    "model": self.llm.model,
                },
            )
        elif llm_primary_required:
            self._log_pre_filter(
                "llm_primary_unavailable",
                {
                    "enabled": False,
                    "mode": self.settings.llm_decision_mode,
                    "provider": self.settings.llm_provider,
                    "symbols_scanned": len(scan_items),
                },
            )

        for item in scan_items:
            context = item["context"]
            selection_detail = llm_selection_details.get(item["symbol"], {})
            context["llm_primary_selection"] = {
                "required": llm_primary_required,
                "enabled": llm_primary,
                "selected": item["symbol"] in llm_candidate_symbols,
                "candidate_limit": self.settings.llm_max_symbols_per_cycle,
                "prefilter_passed": should_call_llm(item),
                "prefilter_reason": _llm_prefilter_reason(item),
                "event_triggered": bool(getattr(self.settings, "llm_event_triggered_cycles", True)),
                "event_review": selection_detail or None,
            }
            blocked_by_llm_primary = False
            llm_block_reason: str | None = None
            if llm_primary and item["symbol"] in llm_candidate_symbols:
                context["llm_prompt_profile"] = str(
                    getattr(self.settings, "llm_cycle_prompt_profile", "compact") or "compact"
                ).strip().lower()
                llm_prefilter_reason = _llm_prefilter_reason(item)
                if llm_prefilter_reason is not None:
                    blocked_by_llm_primary = item["action"] != "HOLD"
                    llm_block_reason = f"llm_budget_prefilter_rule_hold:{llm_prefilter_reason}"
                    context["llm_primary_gate"] = {
                        "required": True,
                        "reviewed": False,
                        "original_action": item["action"],
                        "final_action": "HOLD",
                        "reason": llm_block_reason,
                        "effect": "forced_hold_no_trade",
                    }
                else:
                    llm_decision = await self.llm.decide(context)
                    llm_reviews += 1
                    llm_audit = _json_object(llm_decision.details_json)
                    llm_failed = bool(llm_audit.get("llm_error")) or llm_audit.get("decision_path") == "safe_hold"
                    context["llm_primary_review"] = {
                        "reviewed": True,
                        "llm_action": llm_decision.action,
                        "llm_reason": llm_decision.reason,
                        "llm_error": llm_audit.get("llm_error"),
                        "json_synthetic": bool(llm_audit.get("json_synthetic")),
                        "deterministic_action_preserved": False,
                        "deterministic_action_blocked": llm_failed and item["action"] != "HOLD",
                    }
                    llm_buy_block_reason = _llm_buy_block_reason(context) if llm_decision.action == "BUY" else None
                    if llm_buy_block_reason:
                        context["llm_primary_rule_blocked"] = {
                            "llm_action": llm_decision.action,
                            "reason": llm_buy_block_reason,
                            "hard_blocks": (context.get("system_gate_audit") or {}).get("hard_blocks", []),
                            "active_flags": (context.get("system_gate_audit") or {}).get("active_flags", []),
                            "failed_gates": (context.get("decision_gate_context") or {}).get("failed_gates", []),
                        }
                        llm_failed = True
                        llm_block_reason = "llm_buy_blocked_by_trade_gates"
                    if not llm_failed:
                        decisions.append(llm_decision)
                        continue
                    blocked_by_llm_primary = item["action"] != "HOLD"
                    llm_block_reason = llm_block_reason or "llm_primary_failed_safe_hold"
                    context["llm_primary_fallback"] = {
                        "blocked_deterministic_action": item["action"],
                        "llm_action": llm_decision.action,
                        "llm_reason": llm_decision.reason,
                        "reason": llm_block_reason,
                        "effect": "forced_hold_no_trade",
                    }
            elif llm_primary_required and item["action"] != "HOLD":
                blocked_by_llm_primary = True
                prefilter_reason = _llm_prefilter_reason(item)
                if llm_primary and prefilter_reason is not None:
                    llm_block_reason = f"llm_budget_prefilter_rule_hold:{prefilter_reason}"
                else:
                    llm_block_reason = (
                        "llm_primary_unavailable_no_trade"
                        if not self.llm.enabled
                        else "llm_primary_required_no_unreviewed_trade"
                    )
                context["llm_primary_gate"] = {
                    "required": True,
                    "reviewed": False,
                    "original_action": item["action"],
                    "final_action": "HOLD",
                    "reason": llm_block_reason,
                    "effect": "forced_hold_no_trade",
                }
            if blocked_by_llm_primary:
                item["action"] = "HOLD"
                item["confidence"] = self._confidence_for_action(
                    "HOLD",
                    item["combined"],
                    item.get("macro_event_context") or {},
                    context.get("market_breadth_context") or {},
                )

            candle_summary = context["candlestick_analysis"]
            best_strategy = context["best_strategy"]
            global_risk = context.get("global_market_context", {})
            institutional = context.get("institutional_context", {})
            confluence = context.get("full_spectrum_analysis", {}).get("confluence_score", {})
            liquidity = context.get("full_spectrum_analysis", {}).get("liquidity_profile", {})
            conflicts = context.get("full_spectrum_analysis", {}).get("signal_conflicts", {})
            institutional_bias = (institutional.get("market_bias") or {}).get("score", 0.0)
            reason = (
                f"tools technical={item['technical'].score:.2f} ({item['technical'].trend}), "
                f"candles={candle_summary['score']:.2f} {candle_summary['patterns']}, "
                f"best_strategy={best_strategy['name']}:{best_strategy['score']:.2f}, "
                f"sentiment={item['sentiment_score']:.2f}, "
                f"global={float(global_risk.get('risk_score', 0.0) or 0.0):.2f} ({global_risk.get('regime', 'unknown')}), "
                f"free_inst={float(institutional_bias or 0.0):.2f} ({institutional.get('source_quality', 'unknown')}), "
                f"confluence={confluence.get('total', 0)}/26 {confluence.get('tier', 'NO_SIGNAL')}, "
                f"liquidity={liquidity.get('liquidity_tier', 'unknown')}, conflicts={conflicts.get('severity', 'none')}, "
                f"combined={item['combined']:.2f}, universe_rank={context['universe_scan']['rank']}/{len(scan_items)}"
            )
            failed_gate_names = [
                str(gate.get("gate"))
                for gate in (context.get("decision_gate_context") or {}).get("failed_gates", [])
            ]
            if failed_gate_names:
                reason = f"{reason}, failed_gates={failed_gate_names}"
            if context.get("llm_primary_fallback"):
                reason = f"{reason}, {context['llm_primary_fallback'].get('reason', 'llm_primary_failed_safe_hold')}"
            if context.get("llm_primary_gate", {}).get("effect") == "forced_hold_no_trade":
                reason = f"{reason}, {context['llm_primary_gate'].get('reason', 'llm_primary_required_no_unreviewed_trade')}"
            action = item["action"]
            confidence = item["confidence"]
            decision_path = "deterministic_after_full_universe_scan"
            if context.get("llm_primary_fallback"):
                decision_path = "llm_primary_failed_safe_hold"
            elif context.get("llm_primary_gate", {}).get("effect") == "forced_hold_no_trade":
                gate_reason = str(context.get("llm_primary_gate", {}).get("reason") or "")
                decision_path = (
                    "llm_budget_prefilter_rule_hold"
                    if gate_reason.startswith("llm_budget_prefilter_rule_hold")
                    else "llm_primary_required_safe_hold"
                )
            decision = Decision(
                symbol=item["symbol"],
                action=action,
                confidence=round(confidence, 3),
                price=item["quote"].price,
                technical_score=round(item["technical"].score, 3),
                sentiment_score=round(item["sentiment_score"], 3),
                reason=reason,
                asof=utc_now(),
                strategy=best_strategy["name"],
                details_json=self._decision_details_json(
                    context=context,
                    action=action,
                    decision_path=decision_path,
                    score_breakdown=item["score_breakdown"],
                    action_reason=reason,
                    positions=positions,
                    llm_selected=item["symbol"] in llm_candidate_symbols,
                ),
            )
            if (
                self.llm.enabled
                and self.settings.llm_decision_mode == "review"
                and item["action"] != "HOLD"
                and llm_reviews < self.settings.llm_max_symbols_per_cycle
            ):
                decision = await self.llm.review(decision, context)
                llm_reviews += 1
            decisions.append(decision)
            if len(decisions) % 5 == 0:
                await asyncio.sleep(0)
        return decisions

    def stop_or_take_profit_exits(
        self,
        quotes: dict[str, Quote],
        positions: dict[str, dict[str, Any]],
        candles_by_symbol: dict[str, list[Candle]] | None = None,
    ) -> list[Decision]:
        decisions: list[Decision] = []
        candles_by_symbol = candles_by_symbol or {}
        for symbol, position in positions.items():
            quote = quotes.get(symbol)
            if not quote or position["qty"] <= 0:
                continue
            avg_price = float(position["avg_price"])
            details = _json_object(position.get("details_json"))
            candles = candles_by_symbol.get(symbol, [])
            atr = _atr(candles, 14)
            if atr:
                risk_unit = max(1.5 * atr, avg_price * 0.01)
                stop = avg_price - risk_unit
                target1 = avg_price + (risk_unit * 1.5)
                target2 = avg_price + (risk_unit * 2.5)
            else:
                stop = avg_price * (1 - self.settings.stop_loss_pct)
                target1 = avg_price * (1 + self.settings.take_profit_pct)
                target2 = avg_price * (1 + self.settings.take_profit_pct * 1.6)
            if details.get("tier1_hit"):
                stop = max(stop, avg_price)
            held_periods = _held_periods_from_position(position, candles)
            partial_pct = None
            if quote.price <= stop:
                reason = f"risk exit: price {quote.price:.2f} <= stop {stop:.2f}"
            elif quote.price >= target2 and not details.get("tier2_hit"):
                reason = f"profit tier2: price {quote.price:.2f} >= target2 {target2:.2f}"
                partial_pct = 0.33
            elif quote.price >= target1 and not details.get("tier1_hit"):
                reason = f"profit tier1: price {quote.price:.2f} >= target1 {target1:.2f}; tighten stop to break-even"
                partial_pct = 0.33
            elif held_periods > 15 and abs(quote.price - avg_price) / avg_price <= 0.01:
                reason = "time_stop_no_progress_15_sessions"
            else:
                continue
            decisions.append(
                Decision(
                    symbol=symbol,
                    action="SELL",
                    confidence=0.99,
                    price=quote.price,
                    technical_score=0.0,
                    sentiment_score=0.0,
                    reason=reason,
                    asof=utc_now(),
                    strategy="risk_exit",
                    details_json=self._risk_exit_details_json(
                        symbol=symbol,
                        quote=quote,
                        position=position,
                        stop=stop,
                        target=target1,
                        reason=reason,
                        atr=atr,
                        target2=target2,
                        partial_sell_pct=partial_pct,
                        held_periods=held_periods,
                    ),
                )
            )
        return decisions

    def _action_from_context(
        self,
        symbol: str,
        combined: float,
        positions: dict[str, dict[str, Any]],
        context: dict[str, Any],
        candles_by_symbol: dict[str, list[Candle]] | None = None,
    ) -> str:
        has_position = symbol in positions and positions[symbol]["qty"] > 0
        full_spectrum = context.get("full_spectrum_analysis", {})
        confluence = full_spectrum.get("confluence_score", {})
        risk_overrides = full_spectrum.get("risk_overrides", {})
        scorecard = full_spectrum.get("institutional_scorecard", {})
        stage = full_spectrum.get("stage_analysis") or {}
        entry = full_spectrum.get("entry_quality") or {}
        breakout = full_spectrum.get("breakout_quality") or {}
        strategy_logic = full_spectrum.get("strategy_logic_filters") if isinstance(full_spectrum.get("strategy_logic_filters"), dict) else {}
        divergence = full_spectrum.get("price_volume_divergence") or {}
        alignment = ((full_spectrum.get("trend_context") or {}).get("timeframe_alignment") or {})
        options_oi = full_spectrum.get("options_oi") or {}
        sector = full_spectrum.get("sector_rotation") or {}
        market_breadth = context.get("market_breadth_context") or {}
        pre_filter = context.get("pre_filter") or {}
        rule_audit = evaluate_rules_for_context(
            context,
            positions,
            context.get("risk_limits", {}).get("portfolio_equity", 0.0),
        )
        context["system_gate_audit"] = rule_audit
        confluence_total = float(confluence.get("total", 0) or 0.0)
        scorecard_total = float(scorecard.get("total_score") or scorecard.get("score") or 0.0)
        alignment_grade = alignment.get("alignment_grade")
        entry_grade = entry.get("entry_grade")
        effective_entry_grade = (rule_audit.get("entry") or {}).get("effective_entry_grade") or entry_grade
        delivery = full_spectrum.get("delivery_accumulation") or context.get("delivery_data") or {}
        delivery_bias = str(
            delivery.get("net_bias")
            or delivery.get("trend_direction")
            or delivery.get("bias")
            or ""
        ).lower()
        delivery_is_distribution = delivery_bias in {"distribution", "volume_distribution_proxy"}
        exceptional_setup = (
            confluence_total >= 22
            and scorecard_total >= 85
            and alignment_grade == "A"
            and effective_entry_grade in {"A", "B"}
            and not breakout.get("two_day_rule_failed")
            and not divergence.get("climax_volume_top")
        )
        threshold = float(pre_filter.get("buy_threshold") or 0.35)
        if market_breadth.get("breadth_regime") == "bear_warning":
            threshold = max(threshold, 0.45)
        if market_breadth.get("breadth_regime") == "bull_confirmed":
            threshold = min(threshold, 0.30)
        failed_gates: list[dict[str, Any]] = []

        def fail(gate: str, value: Any, reason: str) -> None:
            failed_gates.append({"gate": gate, "value": value, "reason": reason})

        pre_filter_block_reason = str(pre_filter.get("elimination_reason") or "")
        phase3_event_thesis = strategy_logic.get("event_driven_thesis") if isinstance(strategy_logic.get("event_driven_thesis"), dict) else {}
        phase3_hard_flags = {str(block.get("flag") or "") for block in strategy_logic.get("hard_blocks") or []}
        event_driven_earnings_override = (
            pre_filter.get("block_gate") == "macro_calendar_gate"
            and pre_filter_block_reason == "earnings_lockout"
            and phase3_event_thesis.get("supported")
            and "EARNINGS_LOCKOUT_NOT_EVENT_DRIVEN" not in phase3_hard_flags
        )
        if pre_filter.get("buy_blocked") and not has_position and not event_driven_earnings_override:
            fail(pre_filter.get("block_gate", "pre_filter"), pre_filter.get("block_value"), pre_filter.get("elimination_reason", "pre_filter_block"))
        for block in rule_audit.get("hard_blocks") or []:
            fail(
                f"system_rule_{block.get('flag', 'hard_block')}",
                block.get("value"),
                block.get("reason") or str(block.get("flag") or "hard_block"),
            )
        data_ready = context.get("data_readiness") if isinstance(context.get("data_readiness"), dict) else {}
        has_data_readiness_block = any(str(block.get("flag") or "") == "DATA_READINESS_BLOCK" for block in rule_audit.get("hard_blocks") or [])
        if not has_position and not has_data_readiness_block and data_ready.get("trade_decision_ready") is not True:
            fail("phase2_data_readiness", data_ready or None, "phase2_data_not_trade_ready")
        for block in strategy_logic.get("hard_blocks") or []:
            if not has_position:
                fail(
                    f"phase3_{block.get('flag', 'strategy_logic')}",
                    block.get("value"),
                    block.get("reason") or "phase3_strategy_logic_block",
                )
        if entry_grade == "D":
            fail("entry_grade_gate", entry_grade, "extended_entry_no_new_longs")
        if effective_entry_grade == "WATCH":
            fail(
                "entry_grade_gate",
                {
                    "entry_grade": entry_grade,
                    "effective_entry_grade": effective_entry_grade,
                    "confluence": confluence_total,
                    "scorecard": scorecard_total,
                    "alignment": alignment_grade,
                    "sentiment": rule_audit.get("sentiment"),
                },
                "watch_entry_needs_exceptional_confirmation",
            )
        if breakout.get("two_day_rule_failed"):
            fail("breakout_quality_gate", True, "false_breakout_two_day_rule_failed")
        breakout_volume = strategy_logic.get("breakout_volume") if isinstance(strategy_logic.get("breakout_volume"), dict) else {}
        if breakout_volume.get("suspect_without_volume"):
            fail("breakout_volume_gate", breakout_volume, "suspect_breakout_without_volume")
        if stage and not stage.get("buy_permitted", True):
            fail("stage_buy_permitted", stage.get("stage"), "stage_analysis_not_stage2_markup")
        if divergence.get("climax_volume_top"):
            fail("climax_volume_gate", True, "climax_top_detected_no_new_longs")
        if alignment_grade == "D":
            fail("timeframe_alignment_gate", "D", "timeframe_alignment_conflict")
        if alignment_grade == "C":
            context["mtf_c_speculative_size_only"] = True
        overall_score_pct = float(rule_audit.get("overall_score_pct") or 0.0)
        if not has_position and overall_score_pct < FRESH_BUY_MIN_SCORE:
            fail(
                "overall_quality_gate",
                {"overall_score_pct": overall_score_pct, "overall_grade": rule_audit.get("overall_grade")},
                "overall_score_below_70_no_new_longs",
            )
        performance_block = _performance_feedback_block(context.get("performance_feedback") or {})
        if performance_block and not has_position:
            fail("performance_feedback_gate", performance_block, performance_block["reason"])
        if delivery_is_distribution and not exceptional_setup:
            fail(
                "delivery_distribution_gate",
                {
                    "bias": delivery_bias,
                    "delivery_score": delivery.get("delivery_score"),
                    "source": delivery.get("source"),
                    "accumulation_days": delivery.get("accumulation_days"),
                    "distribution_days": delivery.get("distribution_days"),
                },
                "delivery_distribution_no_new_longs",
            )
        if breakout.get("breakout_quality") == "suspect":
            context["suspect_breakout_size_reduction"] = True
        if options_oi.get("buy_suppressed"):
            fail(
                "options_max_pain_gate",
                {
                    "source": options_oi.get("audit_label") or options_oi.get("source"),
                    "max_pain": options_oi.get("max_pain"),
                    "max_pain_distance_pct": options_oi.get("max_pain_distance_pct"),
                },
                "options_max_pain_8pct_below_no_new_longs",
            )
        if sector.get("sector_tier") == "bottom_quartile" and sector.get("sector_stage") == "distribution" and confluence_total <= 20:
            fail("sector_rotation_gate", sector, "bottom_quartile_distribution")
        fundamental = full_spectrum.get("fundamental_quality") or {}
        sentiment = context.get("sentiment") or {}
        price_volume_confirmed = (
            confluence_total >= 18
            and delivery_bias != "distribution"
            and alignment_grade in {"A", "B"}
            and (
                entry.get("volume_confirmation")
                or breakout.get("volume_confirmation")
                or breakout.get("volume_expansion")
                or breakout_volume.get("volume_confirmed")
                or breakout_volume.get("confirmed")
                or delivery_bias in {"accumulation", "volume_accumulation_proxy"}
                or scorecard_total >= 75
            )
        )
        if (
            fundamental.get("quality_bucket") == "unknown"
            and not delivery.get("institutional_fingerprint")
            and float(sentiment.get("confidence") or 0.0) <= 0.05
            and alignment_grade != "A"
            and not exceptional_setup
            and not price_volume_confirmed
        ):
            fail(
                "fundamental_confirmation_gate",
                {
                    "fundamental_quality": "unknown",
                    "sentiment_confidence": sentiment.get("confidence"),
                    "delivery_fingerprint": delivery.get("institutional_fingerprint"),
                    "alignment": alignment_grade,
                },
                "fundamentals_unknown_needs_news_or_delivery_confirmation",
            )
        correlation_gate = self._portfolio_correlation_gate(symbol, positions, candles_by_symbol or {})
        if correlation_gate.get("block_buy"):
            fail("portfolio_correlation_gate", correlation_gate, "portfolio_concentration_correlation_too_high")
        context["portfolio_correlation_gate"] = correlation_gate
        distribution_sessions = _delivery_distribution_sessions(delivery)
        distribution_exit_pressure = has_position and delivery_is_distribution and distribution_sessions >= 3
        if has_position and delivery_is_distribution:
            context["delivery_exit_pressure"] = {
                "reason": "delivery_distribution_exit_review" if distribution_sessions < 3 else "delivery_distribution_persisted_3_sessions_exit",
                "delivery": delivery,
                "distribution_sessions": distribution_sessions,
                "recommended_action": "TRAIL STOP" if distribution_sessions < 3 else "EXIT",
            }
        exit_pressure = (
            scorecard.get("hard_veto", {}).get("failed")
            or "sentiment_not_bearish" in (scorecard.get("must_pass_failed") or [])
            or "hard_veto_clear" in (scorecard.get("must_pass_failed") or [])
            or distribution_exit_pressure
        )
        if risk_overrides.get("no_new_longs") and not has_position:
            fail("risk_overrides", risk_overrides.get("flags", []), "risk_override_no_new_longs")
        sizing_grade = self._position_sizing_grade(context, context.get("risk_limits", {}).get("portfolio_equity", 0), positions)
        if correlation_gate.get("warning"):
            sizing_grade["modifier_details"].append(correlation_gate.get("warning"))
            sizing_grade["final_multiplier"] = round(max(float(sizing_grade["final_multiplier"]) * 0.5, 0.0), 4)
            max_position_pct = min(float(self.settings.max_position_pct), 0.15)
            sizing_grade["recommended_max_position_pct"] = min(
                max_position_pct,
                max_position_pct * sizing_grade["final_multiplier"],
            )
        context["sizing_grade"] = sizing_grade
        evaluated_gates = [
            *list(pre_filter.get("gates") or []),
            {"gate": "system_rule_gates", "passed": not rule_audit.get("hard_blocked"), "value": rule_audit.get("hard_blocks")},
            {"gate": "phase2_data_readiness", "passed": bool((context.get("data_readiness") or {}).get("trade_decision_ready", False)), "value": context.get("data_readiness")},
            {"gate": "phase3_strategy_logic", "passed": bool(strategy_logic.get("passed", True)), "value": strategy_logic},
            {"gate": "entry_grade_gate", "passed": effective_entry_grade in {"A", "B", "C"}, "value": {"entry_grade": entry_grade, "effective_entry_grade": effective_entry_grade}},
            {"gate": "breakout_gate", "passed": not breakout.get("two_day_rule_failed") and not breakout_volume.get("suspect_without_volume"), "value": breakout},
            {"gate": "divergence_gate", "passed": not divergence.get("climax_volume_top"), "value": divergence},
            {"gate": "alignment_gate", "passed": alignment_grade != "D", "value": alignment_grade},
            {"gate": "overall_quality_gate", "passed": has_position or overall_score_pct >= FRESH_BUY_MIN_SCORE, "value": {"overall_score_pct": overall_score_pct, "overall_grade": rule_audit.get("overall_grade")}},
            {"gate": "performance_feedback_gate", "passed": not performance_block, "value": performance_block},
            {"gate": "delivery_distribution_gate", "passed": not delivery_is_distribution or exceptional_setup, "value": delivery},
            {"gate": "options_max_pain_gate", "passed": not options_oi.get("buy_suppressed"), "value": options_oi},
        ]
        context["decision_gate_context"] = {
            "buy_threshold": threshold,
            "failed_gates": failed_gates,
            "evaluated_gates": evaluated_gates,
            "pre_filter": pre_filter,
            "breadth_regime": market_breadth.get("breadth_regime"),
            "breadth_thrust": market_breadth.get("breadth_thrust"),
            "system_gate_audit": rule_audit,
        }
        if failed_gates and not has_position:
            return "HOLD"
        if combined >= threshold and confluence_total >= 16 and scorecard.get("buy_ready") and not has_position:
            return "BUY"
        if has_position and combined <= -0.38:
            context["score_weakness_exit_review"] = {
                "combined": round(combined, 4),
                "review_threshold": -0.38,
                "policy": "no_direct_sell_from_composite_score",
                "recommended_action": "risk_review_or_trail_stop",
            }
        if exit_pressure and has_position:
            return "SELL"
        return "HOLD"

    def _delivery_context(self, symbol: str, delivery_service: Any | None) -> dict[str, Any]:
        if delivery_service is None:
            return {"available": False, "data_gap": "delivery_service_unavailable"}
        trend = delivery_service.rolling_delivery_trend(symbol)
        score_payload = (
            delivery_service.delivery_score_payload(symbol)
            if hasattr(delivery_service, "delivery_score_payload")
            else {"score": delivery_service.delivery_score(symbol)}
        )
        return {
            **trend,
            "score_payload": score_payload,
            "delivery_score": score_payload.get("score", 0.0),
            "institutional_fingerprint": score_payload.get("fingerprint", False),
        }

    def _pre_filter_context(
        self,
        symbol: str,
        row: dict[str, Any],
        quote: Quote,
        candles: list[Candle],
        positions: dict[str, dict[str, Any]],
        delivery_data: dict[str, Any],
        market_breadth: dict[str, Any],
        sector_context: dict[str, Any],
        macro_event_context: dict[str, Any],
    ) -> dict[str, Any]:
        has_position = symbol in positions and positions[symbol].get("qty", 0) > 0
        gates: list[dict[str, Any]] = []
        buy_threshold = 0.35
        buy_blocked = False
        block_gate = None
        block_value: Any = None
        elimination_reason = None
        closes = [c.close for c in candles]
        if len(closes) >= 35:
            sma = sum(closes[-30:]) / 30
            prior = sum(closes[-35:-5]) / 30
            failed = quote.price < sma and sma < prior
            gates.append({"gate": "stage_gate", "passed": not failed, "value": {"price": quote.price, "sma30": round(sma, 3), "slope": round(sma - prior, 3)}})
            if failed:
                buy_threshold = max(buy_threshold, 0.50)
        else:
            gates.append({"gate": "stage_gate", "passed": True, "value": "skipped_insufficient_candles"})
        delivery_score = float(delivery_data.get("delivery_score") or 0.0)
        delivery_bias = str(
            delivery_data.get("net_bias")
            or delivery_data.get("trend_direction")
            or delivery_data.get("bias")
            or ""
        ).lower()
        official_distribution = bool(delivery_data.get("available")) and delivery_bias == "distribution"
        delivery_failed = (delivery_score < -0.4 or official_distribution) and not has_position
        gates.append(
            {
                "gate": "delivery_gate",
                "passed": not delivery_failed,
                "value": {
                    "delivery_score": delivery_score,
                    "bias": delivery_bias or "neutral",
                    "source": delivery_data.get("source"),
                    "official_distribution": official_distribution,
                },
            }
        )
        if delivery_failed:
            buy_blocked = True
            block_gate = "delivery_gate"
            block_value = {"delivery_score": delivery_score, "bias": delivery_bias, "source": delivery_data.get("source")}
            elimination_reason = "pre_filter_stage2_distribution"
        breadth_regime = market_breadth.get("breadth_regime")
        breadth_failed = breadth_regime == "bear_confirmed" and not has_position
        gates.append({"gate": "breadth_gate", "passed": not breadth_failed, "value": breadth_regime})
        if breadth_regime == "bear_warning":
            buy_threshold = max(buy_threshold, 0.50)
        if breadth_failed:
            buy_blocked = True
            block_gate = block_gate or "breadth_gate"
            block_value = block_value or breadth_regime
            elimination_reason = elimination_reason or "market_breadth_bear_confirmed_no_new_longs"
        earnings_trading_days = macro_event_context.get("earnings_trading_days_away")
        earnings_days = macro_event_context.get("earnings_days_away")
        event_thesis = _macro_event_driven_thesis(macro_event_context)
        earnings_trading_value = _float_or_none(earnings_trading_days)
        earnings_days_value = _float_or_none(earnings_days)
        if earnings_trading_value is not None:
            earnings_window = 0 <= earnings_trading_value <= 10
        else:
            earnings_window = earnings_days_value is not None and 0 <= earnings_days_value <= 14
        earnings_block = earnings_window and not event_thesis.get("supported") and not has_position
        monthly_expiry_block = bool(
            macro_event_context.get("is_monthly_expiry_day")
            or macro_event_context.get("is_monthly_expiry_eve")
            or (macro_event_context.get("is_expiry_day") and macro_event_context.get("expiry_type") == "monthly")
        ) and not has_position
        macro_failed = earnings_block or monthly_expiry_block
        macro_reason = (
            "earnings_lockout"
            if earnings_block
            else "monthly_expiry_no_new_longs"
            if macro_event_context.get("is_monthly_expiry_day") or macro_event_context.get("is_expiry_day")
            else "monthly_expiry_eve_no_new_longs"
            if monthly_expiry_block
            else None
        )
        gates.append({"gate": "earnings_gate", "passed": not macro_failed, "value": {"macro_event_context": macro_event_context, "event_thesis": event_thesis}})
        if macro_failed:
            buy_blocked = True
            block_gate = block_gate or "macro_calendar_gate"
            block_value = block_value or macro_event_context
            elimination_reason = elimination_reason or macro_reason
        sector_failed = sector_context.get("sector_tier") == "bottom_quartile" and sector_context.get("sector_stage") == "distribution"
        gates.append({"gate": "sector_gate", "passed": not sector_failed, "value": sector_context})
        if sector_failed:
            buy_threshold = max(buy_threshold, 0.55)
        return {
            "pre_filter_stage": "completed",
            "gates": gates,
            "buy_threshold": buy_threshold,
            "buy_blocked": buy_blocked,
            "block_gate": block_gate,
            "block_value": block_value,
            "elimination_reason": elimination_reason,
        }

    async def _refresh_candidate_sentiment(
        self,
        scan_items: list[dict[str, Any]],
        positions: dict[str, dict[str, Any]],
        candles_by_symbol: dict[str, list[Candle]],
        risk_limits: dict[str, Any],
        global_context: dict[str, Any] | None,
        institutional_context: dict[str, Any] | None,
        market_breadth: dict[str, Any] | None,
        performance_feedback: dict[str, Any] | None,
    ) -> None:
        if not self.settings.enable_news_sentiment or not scan_items:
            return
        limit = min(max(int(self.settings.news_symbols_per_cycle or 0), 2), 5)
        candidates = sorted(
            [
                item
                for item in scan_items
                if item.get("action") == "BUY"
                or float(item.get("combined") or 0.0) >= 0.25
                or float(((item.get("context", {}).get("full_spectrum_analysis") or {}).get("confluence_score") or {}).get("total") or 0.0) >= 16
            ],
            key=self._scan_priority,
            reverse=True,
        )
        selected: list[dict[str, Any]] = []
        for item in candidates:
            sentiment = (item.get("context") or {}).get("sentiment") or {}
            if sentiment.get("headline_count") or float(sentiment.get("confidence") or 0.0) > 0.05:
                continue
            selected.append(item)
            if len(selected) >= limit:
                break
        if not selected:
            return
        results = await asyncio.gather(
            *(self.sentiment.analyze_symbol_news(item["row"]) for item in selected),
            return_exceptions=True,
        )
        for item, result in zip(selected, results):
            if isinstance(result, Exception) or not isinstance(result, dict):
                continue
            try:
                sentiment_score = float(result.get("score") or 0.0)
            except (TypeError, ValueError):
                sentiment_score = 0.0
            context = build_symbol_tool_context(
                row=item["row"],
                quote=item["quote"],
                candles=item.get("candles") or [],
                position=positions.get(item["symbol"]),
                sentiment_score=sentiment_score,
                risk_limits=risk_limits,
                global_context=global_context,
                institutional_context=institutional_context,
                sentiment_detail=result,
                delivery_data=item.get("delivery_data") or {},
                options_data=item.get("options_data") or {},
                sector_context=item.get("sector_context") or {},
                market_breadth=market_breadth,
                macro_event_context=item.get("macro_event_context") or {},
                timeframe_candles=item.get("timeframe_candles") or {},
                pattern_state=self._pattern_state(item["symbol"]),
                performance_feedback=performance_feedback,
                execution_mode=self.settings.execution_mode,
            )
            self._persist_pattern_state_updates(item["symbol"], context)
            context["pre_filter"] = (item.get("context") or {}).get("pre_filter") or {}
            combined = deterministic_score(context)
            item["sentiment_score"] = sentiment_score
            item["sentiment_detail"] = result
            item["context"] = context
            item["combined"] = combined
            item["score_breakdown"] = deterministic_score_breakdown(context)
            item["action"] = self._action_from_context(item["symbol"], combined, positions, context, candles_by_symbol)
            item["confidence"] = self._confidence_for_action(item["action"], combined, item.get("macro_event_context") or {}, market_breadth)

    def _scan_priority(self, item: dict[str, Any]) -> tuple[float, float, float, float, float, float, float]:
        opportunity_rank = self._opportunity_rank_score(item)
        opportunity_score = self._opportunity_priority_score(item)
        return (
            1.0 if item["action"] != "HOLD" else 0.0,
            opportunity_rank,
            opportunity_score,
            abs(float(item["combined"])),
            abs(float(item["context"].get("best_strategy", {}).get("score", 0.0) or 0.0)),
            abs(float(item["technical"].score)),
            abs(float(item["sentiment_score"])),
        )

    def _scan_priority_score(self, item: dict[str, Any]) -> float:
        action_boost, opportunity_rank, opportunity, combined, strategy, technical, sentiment = self._scan_priority(item)
        rs_percentile = float(((item.get("context") or {}).get("universe_relative_strength") or {}).get("percentile_63") or 50.0)
        rs_score = (rs_percentile - 50.0) / 50.0
        return (
            (action_boost * 0.35)
            + (opportunity_rank * 0.20)
            + (opportunity * 0.16)
            + (combined * 0.16)
            + (rs_score * 0.08)
            + (strategy * 0.08)
            + (technical * 0.04)
            + (sentiment * 0.02)
        )

    def _opportunity_rank_score(self, item: dict[str, Any]) -> float:
        row = item.get("row") or {}
        scan = row.get("_opportunity_scan") if isinstance(row, dict) else None
        if not isinstance(scan, dict):
            return 0.0
        rank = _float_or_none(row.get("_opportunity_rank") or scan.get("rank"))
        if rank is None or rank <= 0:
            return 0.0
        limit = max(float(getattr(self.settings, "dynamic_scan_candidate_limit", 60) or 60), rank)
        return max(min((limit - rank + 1.0) / limit, 1.0), 0.0)

    def _opportunity_priority_score(self, item: dict[str, Any]) -> float:
        scan = (item.get("row") or {}).get("_opportunity_scan")
        if not isinstance(scan, dict):
            return 0.0
        try:
            score = max(min(float(scan.get("score") or 0.0), 1.0), 0.0)
        except (TypeError, ValueError):
            score = 0.0
        bucket = str(scan.get("bucket") or "").lower()
        setup = str(scan.get("setup") or "").lower()
        data_quality = scan.get("data_quality") if isinstance(scan.get("data_quality"), dict) else {}
        if bucket == "actionable":
            score += 0.06
        if setup in {"news_catalyst", "breakout_continuation", "near_breakout"}:
            score += 0.03
        if data_quality.get("actionable_data_ready"):
            score += 0.03
        return max(min(score, 1.0), 0.0)

    def _pattern_state(self, symbol: str) -> dict[str, Any]:
        db = getattr(self.sentiment, "db", None)
        if db is None or not hasattr(db, "get_pattern_state"):
            return {}
        state = db.get_pattern_state(symbol, "darvas_box", {}) or {}
        return {"darvas_box": state if isinstance(state, dict) else {}}

    def _persist_pattern_state_updates(self, symbol: str, context: dict[str, Any]) -> None:
        db = getattr(self.sentiment, "db", None)
        if db is None or not hasattr(db, "upsert_pattern_state"):
            return
        for signal in context.get("strategy_signals") or []:
            if signal.get("name") != "darvas_box_breakout":
                continue
            update = ((signal.get("metadata") or {}).get("state_update") or {})
            if isinstance(update, dict) and update:
                db.upsert_pattern_state(symbol, "darvas_box", update)
            break

    def _apply_universe_relative_strength(
        self,
        scan_items: list[dict[str, Any]],
        positions: dict[str, dict[str, Any]],
        candles_by_symbol: dict[str, list[Candle]],
    ) -> None:
        benchmark_returns = self._benchmark_returns(candles_by_symbol)
        returns_by_market: dict[str, list[float]] = defaultdict(list)
        for item in scan_items:
            ret = _return_pct_from_candles(item.get("candles") or [], 63)
            if ret is None:
                continue
            returns_by_market[market_region_for_row(item["row"])].append(ret)
            item["_rs_63_return"] = ret

        sorted_by_market = {
            market: sorted(values)
            for market, values in returns_by_market.items()
            if len(values) >= 5
        }
        if not sorted_by_market:
            return

        for item in scan_items:
            market = market_region_for_row(item["row"])
            universe_returns = sorted_by_market.get(market)
            ret = item.get("_rs_63_return")
            if not universe_returns or ret is None:
                continue
            percentile = _percentile_rank(float(ret), universe_returns)
            benchmark = benchmark_returns.get(market)
            relative_vs_benchmark = float(ret) - float(benchmark["return_pct"]) if benchmark else None
            bucket = "top_decile" if percentile >= 90 else "leadership" if percentile >= 80 else "lagging" if percentile < 40 else "neutral"
            profile = {
                "available": True,
                "window": "63_candles",
                "return_pct": round(float(ret), 4),
                "percentile_63": round(percentile, 2),
                "bucket": bucket,
                "universe_size": len(universe_returns),
                "benchmark_symbol": benchmark.get("symbol") if benchmark else None,
                "benchmark_return_63_pct": round(float(benchmark["return_pct"]), 4) if benchmark else None,
                "relative_vs_benchmark_pct": round(relative_vs_benchmark, 4) if relative_vs_benchmark is not None else None,
                "official_benchmark_available": bool(benchmark),
                "note": (
                    "ranked against scanned peers and the configured market benchmark"
                    if benchmark
                    else "ranked against scanned peers; configured benchmark candles unavailable"
                ),
            }
            context = item.get("context") or {}
            context["universe_relative_strength"] = profile
            full = context.get("full_spectrum_analysis") or {}
            relative_strength = full.setdefault("relative_strength", {})
            relative_strength.update(
                {
                    "universe_return_63_pct": profile["return_pct"],
                    "universe_percentile_63": profile["percentile_63"],
                    "universe_bucket": bucket,
                    "benchmark_symbol": profile["benchmark_symbol"],
                    "benchmark_return_63_pct": profile["benchmark_return_63_pct"],
                    "relative_vs_benchmark_pct": profile["relative_vs_benchmark_pct"],
                    "universe_note": profile["note"],
                }
            )
            if relative_vs_benchmark is not None and relative_vs_benchmark >= 2.0 and percentile >= 60:
                relative_strength["bias"] = "outperforming"
            elif relative_vs_benchmark is not None and relative_vs_benchmark <= -2.0:
                relative_strength["bias"] = "underperforming"
            elif percentile >= 80:
                relative_strength["bias"] = "outperforming"
            elif percentile < 40:
                relative_strength["bias"] = "underperforming"
            else:
                relative_strength["bias"] = "neutral"
            item["score_breakdown"] = deterministic_score_breakdown(context)
            item["combined"] = item["score_breakdown"]["combined"]
            item["action"] = self._action_from_context(item["symbol"], item["combined"], positions, context, candles_by_symbol)
            item["confidence"] = self._confidence_for_action(
                item["action"],
                item["combined"],
                item.get("macro_event_context") or {},
                context.get("market_breadth_context") or {},
            )

    def _benchmark_returns(self, candles_by_symbol: dict[str, list[Candle]]) -> dict[str, dict[str, Any]]:
        output: dict[str, dict[str, Any]] = {}
        candidates = {
            "IN": _csv_symbols(getattr(self.settings, "rs_benchmark_symbols_in", "")),
            "US": _csv_symbols(getattr(self.settings, "rs_benchmark_symbols_us", "")),
        }
        for market, symbols in candidates.items():
            for symbol in symbols:
                ret = _return_pct_from_candles(candles_by_symbol.get(symbol) or [], 63)
                if ret is None:
                    continue
                output[market] = {"symbol": symbol, "return_pct": ret}
                break
        return output

    def _confidence_for_action(
        self,
        action: str,
        combined: float,
        macro_event_context: dict[str, Any],
        market_breadth: dict[str, Any] | None,
    ) -> float:
        confidence = min(abs(combined), 0.99)
        if action != "BUY":
            return confidence
        breadth = market_breadth or {}
        if breadth.get("breadth_regime") == "bear_warning":
            confidence = max(confidence - 0.25, 0.0)
        if breadth.get("breadth_regime") == "bull_confirmed":
            confidence = min(confidence + 0.10, 0.99)
        if breadth.get("breadth_thrust"):
            confidence = min(confidence + 0.15, 0.99)
        if float(macro_event_context.get("event_risk_score") or 0.0) > 0.6:
            confidence = max(confidence - 0.20, 0.0)
        return confidence

    def _llm_candidate_symbols(self, ranked: list[dict[str, Any]]) -> set[str]:
        limit = max(int(self.settings.llm_max_symbols_per_cycle or 1), 1)
        selected: list[str] = []
        eligible = [item for item in ranked if should_call_llm(item)]
        self._last_llm_selection_details = {}
        if bool(getattr(self.settings, "llm_event_triggered_cycles", True)):
            return self._event_triggered_llm_candidate_symbols(eligible, limit)

        sector_counts: dict[str, int] = defaultdict(int)
        strategy_counts: dict[str, int] = defaultdict(int)
        sector_cap = max(2, limit // 4)
        strategy_cap = max(2, limit // 3)

        def add(items: list[dict[str, Any]], *, diversify: bool = False) -> None:
            for item in items:
                symbol = item["symbol"]
                if symbol in selected:
                    continue
                sector = str((item.get("row") or {}).get("sector") or (item.get("context") or {}).get("sector") or "unknown")
                strategy = str(((item.get("context") or {}).get("best_strategy") or {}).get("name") or item.get("strategy") or "unknown")
                if diversify and (
                    sector_counts[sector] >= sector_cap
                    or strategy_counts[strategy] >= strategy_cap
                ):
                    continue
                selected.append(symbol)
                sector_counts[sector] += 1
                strategy_counts[strategy] += 1
                if len(selected) >= limit:
                    return

        open_positions = sorted(
            [item for item in eligible if self._has_open_position(item)],
            key=self._exit_review_priority,
            reverse=True,
        )
        action_candidates = [item for item in eligible if item["action"] != "HOLD" and not self._has_open_position(item)]
        add(open_positions)
        if len(selected) < limit:
            add(action_candidates, diversify=True)
        if len(selected) < limit:
            add(action_candidates, diversify=False)
        if len(selected) < limit:
            add(eligible, diversify=True)
        if len(selected) < limit:
            add(eligible, diversify=False)
        return set(selected)

    def _event_triggered_llm_candidate_symbols(self, eligible: list[dict[str, Any]], limit: int) -> set[str]:
        now = datetime.now(timezone.utc)
        state = self._llm_review_state()
        symbols_state = state.setdefault("symbols", {})
        daily_budget = self._llm_daily_budget(state, now)
        daily_limit = max(int(getattr(self.settings, "llm_max_reviews_per_market_day", 40) or 0), 0)
        remaining = max(daily_limit - int(daily_budget.get("reviews") or 0), 0) if daily_limit else limit
        if remaining <= 0:
            self._last_llm_selection_details = {
                item["symbol"]: {
                    "triggered": False,
                    "selected": False,
                    "reason": "daily_llm_review_budget_exhausted",
                    "daily_review_budget": {
                        "date": daily_budget.get("date"),
                        "used": int(daily_budget.get("reviews") or 0),
                        "limit": daily_limit,
                    },
                }
                for item in eligible[:limit]
            }
            return set()

        selected: list[str] = []
        details: dict[str, dict[str, Any]] = {}
        sector_counts: dict[str, int] = defaultdict(int)
        strategy_counts: dict[str, int] = defaultdict(int)
        sector_cap = max(2, limit // 4)
        strategy_cap = max(2, limit // 3)

        evaluated: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for item in eligible:
            symbol = str(item.get("symbol") or "").upper()
            trigger = self._llm_event_trigger(item, symbols_state.get(symbol) or {}, now)
            details[symbol] = trigger
            if trigger.get("triggered"):
                evaluated.append((item, trigger))

        buckets = [
            sorted(
                [(item, trigger) for item, trigger in evaluated if self._has_open_position(item)],
                key=lambda pair: self._exit_review_priority(pair[0]),
                reverse=True,
            ),
            [(item, trigger) for item, trigger in evaluated if item.get("action") != "HOLD" and not self._has_open_position(item)],
            [(item, trigger) for item, trigger in evaluated if item.get("action") == "HOLD" and not self._has_open_position(item)],
        ]

        def add(items: list[tuple[dict[str, Any], dict[str, Any]]], *, diversify: bool) -> None:
            for item, trigger in items:
                symbol = str(item.get("symbol") or "").upper()
                if not symbol or symbol in selected:
                    continue
                sector = str((item.get("row") or {}).get("sector") or (item.get("context") or {}).get("sector") or "unknown")
                strategy = str(((item.get("context") or {}).get("best_strategy") or {}).get("name") or item.get("strategy") or "unknown")
                if diversify and (
                    sector_counts[sector] >= sector_cap
                    or strategy_counts[strategy] >= strategy_cap
                ):
                    continue
                selected.append(symbol)
                sector_counts[sector] += 1
                strategy_counts[strategy] += 1
                trigger["selected"] = True
                trigger["daily_review_budget"] = {
                    "date": daily_budget.get("date"),
                    "used_before": int(daily_budget.get("reviews") or 0),
                    "limit": daily_limit,
                }
                if len(selected) >= min(limit, remaining):
                    return

        for bucket in buckets:
            if len(selected) >= min(limit, remaining):
                break
            add(bucket, diversify=True)
            if len(selected) >= min(limit, remaining):
                break
            add(bucket, diversify=False)

        if selected:
            for symbol in selected:
                item_detail = details.get(symbol) or {}
                symbols_state[symbol] = {
                    **(symbols_state.get(symbol) or {}),
                    "last_reviewed_at": now.isoformat(),
                    "last_score": item_detail.get("current_score"),
                    "last_action": item_detail.get("current_action"),
                    "last_strategy": item_detail.get("strategy"),
                    "last_triggers": item_detail.get("triggers", []),
                    "last_position_signature": item_detail.get("position_signature"),
                    "market_region": item_detail.get("market_region"),
                }
            daily_budget["reviews"] = int(daily_budget.get("reviews") or 0) + len(selected)
            state["daily_budget"] = daily_budget
            self._save_llm_review_state(state)

        self._last_llm_selection_details = details
        return set(selected)

    def _llm_event_trigger(
        self,
        item: dict[str, Any],
        previous: dict[str, Any],
        now: datetime,
    ) -> dict[str, Any]:
        context = item.get("context") if isinstance(item.get("context"), dict) else {}
        row = item.get("row") if isinstance(item.get("row"), dict) else {}
        full = context.get("full_spectrum_analysis") if isinstance(context.get("full_spectrum_analysis"), dict) else {}
        rule_audit = context.get("system_gate_audit") if isinstance(context.get("system_gate_audit"), dict) else {}
        confluence = full.get("confluence_score") if isinstance(full.get("confluence_score"), dict) else {}
        entry = full.get("entry_quality") if isinstance(full.get("entry_quality"), dict) else {}
        breakout = full.get("breakout_quality") if isinstance(full.get("breakout_quality"), dict) else {}
        strategy_logic = full.get("strategy_logic_filters") if isinstance(full.get("strategy_logic_filters"), dict) else {}
        current_score = _float_or_none(item.get("combined")) or 0.0
        previous_score = _float_or_none(previous.get("last_score"))
        score_delta = abs(current_score - previous_score) if previous_score is not None else None
        material_delta = score_delta is not None and score_delta >= float(getattr(self.settings, "llm_material_score_delta", 0.08) or 0.08)
        action = str(item.get("action") or "HOLD").upper()
        strategy = str((context.get("best_strategy") or {}).get("name") or item.get("strategy") or "unknown")
        overall_score_pct = _float_or_none(rule_audit.get("overall_score_pct")) or 0.0
        overall_grade = str(rule_audit.get("overall_grade") or "").upper()
        confluence_total = _float_or_none(confluence.get("total")) or 0.0
        triggers: list[str] = []
        market_region = market_region_for_row(row) if row else str(context.get("market_region") or "").upper()
        position_signature = self._llm_position_signature(item)

        if self._has_open_position(item):
            last_position_review = _parse_time(previous.get("last_reviewed_at"))
            interval_minutes = max(int(getattr(self.settings, "llm_open_position_review_interval_minutes", 15) or 15), 1)
            due = last_position_review is None or (now - last_position_review).total_seconds() >= interval_minutes * 60
            changed = previous.get("last_position_signature") != position_signature
            if due:
                triggers.append("open_position_review_interval_due")
            if changed:
                triggers.append("open_position_state_changed")
            return {
                "triggered": bool(triggers),
                "selected": False,
                "reason": "open_position_needs_review" if triggers else "open_position_review_interval_not_due",
                "triggers": triggers,
                "current_score": round(current_score, 6),
                "previous_score": previous_score,
                "score_delta": round(score_delta, 6) if score_delta is not None else None,
                "current_action": action,
                "strategy": strategy,
                "position_signature": position_signature,
                "market_region": market_region,
            }

        if action != "HOLD":
            triggers.append(f"deterministic_{action.lower()}")
        if overall_score_pct >= float(getattr(self.settings, "llm_min_trigger_score_pct", 70.0) or 70.0) and confluence_total >= float(
            getattr(self.settings, "llm_min_trigger_confluence", 16.0) or 16.0
        ):
            triggers.append("quality_score_confluence_threshold")
        opportunity_trigger = self._llm_opportunity_trigger(row)
        if opportunity_trigger:
            triggers.append(opportunity_trigger)
        news_trigger = self._llm_news_trigger(context, item)
        if news_trigger:
            triggers.append(news_trigger)
        if material_delta:
            triggers.append("material_score_change")
        if previous.get("last_action") and str(previous.get("last_action")).upper() != action:
            triggers.append("action_changed")

        triggers = list(dict.fromkeys(triggers))
        if not triggers:
            return {
                "triggered": False,
                "selected": False,
                "reason": "no_material_llm_event",
                "triggers": [],
                "current_score": round(current_score, 6),
                "previous_score": previous_score,
                "score_delta": round(score_delta, 6) if score_delta is not None else None,
                "overall_score_pct": overall_score_pct,
                "overall_grade": overall_grade,
                "confluence": confluence_total,
                "current_action": action,
                "strategy": strategy,
                "position_signature": position_signature,
                "market_region": market_region,
            }

        cooldown_minutes = max(int(getattr(self.settings, "llm_symbol_cooldown_minutes", 240) or 240), 1)
        last_review = _parse_time(previous.get("last_reviewed_at"))
        cooldown_until = last_review + timedelta(minutes=cooldown_minutes) if last_review else None
        cooldown_active = bool(cooldown_until and now < cooldown_until)
        if cooldown_active and not material_delta and "action_changed" not in triggers:
            return {
                "triggered": False,
                "selected": False,
                "reason": "symbol_llm_cooldown_active",
                "triggers": triggers,
                "cooldown": {
                    "active": True,
                    "until": cooldown_until.isoformat() if cooldown_until else None,
                    "minutes": cooldown_minutes,
                },
                "current_score": round(current_score, 6),
                "previous_score": previous_score,
                "score_delta": round(score_delta, 6) if score_delta is not None else None,
                "current_action": action,
                "strategy": strategy,
                "position_signature": position_signature,
                "market_region": market_region,
            }

        entry_block_reason = self._llm_entry_review_block_reason(
            context=context,
            item=item,
            overall_score_pct=overall_score_pct,
            overall_grade=overall_grade,
            confluence_total=confluence_total,
        )
        if entry_block_reason:
            return {
                "triggered": False,
                "selected": False,
                "reason": entry_block_reason,
                "triggers": triggers,
                "current_score": round(current_score, 6),
                "previous_score": previous_score,
                "score_delta": round(score_delta, 6) if score_delta is not None else None,
                "overall_score_pct": overall_score_pct,
                "overall_grade": overall_grade,
                "confluence": confluence_total,
                "entry_grade": entry.get("entry_grade") or entry.get("grade"),
                "breakout_quality": breakout.get("breakout_quality"),
                "breakout_volume": strategy_logic.get("breakout_volume") if isinstance(strategy_logic.get("breakout_volume"), dict) else None,
                "current_action": action,
                "strategy": strategy,
                "position_signature": position_signature,
                "market_region": market_region,
            }

        return {
            "triggered": bool(triggers),
            "selected": False,
            "reason": "event_triggered" if triggers else "no_material_llm_event",
            "triggers": triggers,
            "cooldown": {
                "active": False,
                "until": cooldown_until.isoformat() if cooldown_until else None,
                "minutes": cooldown_minutes,
            },
            "current_score": round(current_score, 6),
            "previous_score": previous_score,
            "score_delta": round(score_delta, 6) if score_delta is not None else None,
            "overall_score_pct": overall_score_pct,
            "overall_grade": overall_grade,
            "confluence": confluence_total,
            "entry_grade": entry.get("entry_grade"),
            "breakout_quality": breakout.get("breakout_quality"),
            "breakout_volume": strategy_logic.get("breakout_volume") if isinstance(strategy_logic.get("breakout_volume"), dict) else None,
            "current_action": action,
            "strategy": strategy,
            "position_signature": position_signature,
            "market_region": market_region,
        }

    def _llm_opportunity_trigger(self, row: dict[str, Any]) -> str | None:
        scan = row.get("_opportunity_scan") if isinstance(row, dict) else None
        if not isinstance(scan, dict):
            return None
        bucket = str(scan.get("bucket") or "").strip().lower()
        setup = str(scan.get("setup") or "").strip().lower()
        data_quality = scan.get("data_quality") if isinstance(scan.get("data_quality"), dict) else {}
        if bucket == "actionable" and setup in {"breakout_continuation", "near_breakout", "news_catalyst"}:
            if data_quality and data_quality.get("actionable_data_ready") is False:
                return None
            return f"opportunity_scan_{setup}"
        return None

    def _llm_news_trigger(self, context: dict[str, Any], item: dict[str, Any]) -> str | None:
        sentiment = context.get("sentiment") if isinstance(context.get("sentiment"), dict) else {}
        if not sentiment:
            sentiment = item.get("sentiment_detail") if isinstance(item.get("sentiment_detail"), dict) else {}
        confidence = _float_or_none(sentiment.get("confidence")) or 0.0
        score = _float_or_none(sentiment.get("score")) or _float_or_none(item.get("sentiment_score")) or 0.0
        events = sentiment.get("events") if isinstance(sentiment.get("events"), list) else []
        verified_positive_events = [
            event
            for event in events
            if isinstance(event, dict)
            and (_float_or_none(event.get("confidence")) or confidence) >= 0.45
            and (_float_or_none(event.get("score")) or _float_or_none(event.get("weighted_score")) or score) >= 0.25
        ]
        if verified_positive_events:
            return "verified_positive_news_event"
        if score >= 0.30 and confidence >= 0.55 and int(sentiment.get("headline_count") or 0) > 0:
            return "high_confidence_positive_news"
        return None

    def _llm_entry_review_block_reason(
        self,
        *,
        context: dict[str, Any],
        item: dict[str, Any],
        overall_score_pct: float,
        overall_grade: str,
        confluence_total: float,
    ) -> str | None:
        data_readiness = context.get("data_readiness") if isinstance(context.get("data_readiness"), dict) else {}
        if not data_readiness:
            return "entry_data_readiness_missing"
        if data_readiness.get("trade_decision_ready") is not True:
            return "entry_not_trade_ready"

        rule_audit = context.get("system_gate_audit") if isinstance(context.get("system_gate_audit"), dict) else {}
        if rule_audit.get("hard_blocked") is True:
            return "entry_hard_blocked"

        decision_gates = context.get("decision_gate_context") if isinstance(context.get("decision_gate_context"), dict) else {}
        failed_gates = decision_gates.get("failed_gates") if isinstance(decision_gates.get("failed_gates"), list) else []
        if failed_gates:
            return "entry_failed_trade_gates"

        min_score = float(getattr(self.settings, "llm_min_trigger_score_pct", 70.0) or 70.0)
        min_confluence = float(getattr(self.settings, "llm_min_trigger_confluence", 16.0) or 16.0)
        if overall_score_pct < min_score or confluence_total < min_confluence:
            return "entry_below_actionable_quality_floor"
        if str(overall_grade or "").upper() not in {"A", "B"}:
            return "entry_overall_grade_not_a_or_b"

        full = context.get("full_spectrum_analysis") if isinstance(context.get("full_spectrum_analysis"), dict) else {}
        entry = full.get("entry_quality") if isinstance(full.get("entry_quality"), dict) else {}
        rule_entry = rule_audit.get("entry") if isinstance(rule_audit.get("entry"), dict) else {}
        entry_grade = str(
            rule_entry.get("effective_entry_grade")
            or entry.get("entry_grade")
            or entry.get("grade")
            or ""
        ).upper()
        if entry_grade and entry_grade not in {"A", "B"}:
            return "entry_grade_not_a_or_b"

        breakout = full.get("breakout_quality") if isinstance(full.get("breakout_quality"), dict) else {}
        strategy_logic = full.get("strategy_logic_filters") if isinstance(full.get("strategy_logic_filters"), dict) else {}
        breakout_volume = strategy_logic.get("breakout_volume") if isinstance(strategy_logic.get("breakout_volume"), dict) else {}
        suspect_breakout = (
            str(breakout.get("breakout_quality") or "").lower() == "suspect"
            or bool(breakout_volume.get("suspect_without_volume"))
        )
        volume_confirmed = bool(
            breakout.get("volume_confirmation")
            or breakout.get("volume_expansion")
            or breakout_volume.get("volume_confirmed")
            or breakout_volume.get("confirmed")
        )
        if suspect_breakout and not volume_confirmed:
            return "entry_suspect_breakout_without_volume_confirmation"

        action = str(item.get("action") or "HOLD").upper()
        scan = (item.get("row") or {}).get("_opportunity_scan")
        actionable_scan = isinstance(scan, dict) and str(scan.get("bucket") or "").lower() == "actionable"
        if action == "HOLD" and not actionable_scan and not self._llm_news_trigger(context, item):
            return "entry_not_actionable"
        return None

    def _llm_position_signature(self, item: dict[str, Any]) -> str:
        context = item.get("context") if isinstance(item.get("context"), dict) else {}
        position = context.get("position") if isinstance(context.get("position"), dict) else {}
        quote = item.get("quote")
        avg_price = _float_or_none(position.get("avg_price")) or 0.0
        current_price = _float_or_none(getattr(quote, "price", None)) or _float_or_none((context.get("quote") or {}).get("price")) or 0.0
        pnl_pct = ((current_price - avg_price) / avg_price) if avg_price > 0 else 0.0
        scorecard = ((context.get("full_spectrum_analysis") or {}).get("institutional_scorecard") or {})
        failed_gates = [
            str(gate.get("reason") or gate.get("gate") or "")
            for gate in (context.get("decision_gate_context") or {}).get("failed_gates", [])
            if isinstance(gate, dict)
        ]
        payload = {
            "action": str(item.get("action") or "HOLD").upper(),
            "pnl_bucket_pct": round(pnl_pct * 20) / 20,
            "delivery_exit_pressure": bool(context.get("delivery_exit_pressure")),
            "scorecard_hard_veto": bool((scorecard.get("hard_veto") or {}).get("failed")),
            "failed_gates": sorted(failed_gates)[:6],
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))

    def _llm_review_state(self) -> dict[str, Any]:
        db = getattr(getattr(self, "sentiment", None), "db", None)
        if db is None or not hasattr(db, "get_state"):
            return {"symbols": {}, "daily_budget": {}}
        try:
            state = db.get_state("llm_symbol_review_state", {}) or {}
        except Exception:
            return {"symbols": {}, "daily_budget": {}}
        return state if isinstance(state, dict) else {"symbols": {}, "daily_budget": {}}

    def _save_llm_review_state(self, state: dict[str, Any]) -> None:
        db = getattr(getattr(self, "sentiment", None), "db", None)
        if db is None or not hasattr(db, "set_state"):
            return
        try:
            db.set_state("llm_symbol_review_state", state)
        except Exception:
            pass

    def _llm_daily_budget(self, state: dict[str, Any], now: datetime) -> dict[str, Any]:
        today = now.date().isoformat()
        daily_budget = state.get("daily_budget") if isinstance(state.get("daily_budget"), dict) else {}
        if daily_budget.get("date") != today:
            daily_budget = {"date": today, "reviews": 0}
        state["daily_budget"] = daily_budget
        return daily_budget

    def _has_open_position(self, item: dict[str, Any]) -> bool:
        return float((item.get("context", {}).get("position") or {}).get("qty") or 0) > 0

    def _exit_review_priority(self, item: dict[str, Any]) -> tuple[float, float, float]:
        position = item.get("context", {}).get("position") or {}
        quote = item.get("quote")
        avg_price = float(position.get("avg_price") or 0)
        current_price = float(getattr(quote, "price", 0.0) or 0.0)
        pnl_pct = ((current_price - avg_price) / avg_price) if avg_price > 0 else 0.0
        negative_score = max(-float(item.get("combined") or 0.0), 0.0)
        stress = max(-pnl_pct, 0.0) + max(pnl_pct - self.settings.take_profit_pct * 0.7, 0.0)
        return (negative_score + stress, abs(pnl_pct), abs(float(item.get("combined") or 0.0)))

    def _portfolio_correlation_gate(
        self,
        symbol: str,
        positions: dict[str, dict[str, Any]],
        candles_by_symbol: dict[str, list[Candle]],
    ) -> dict[str, Any]:
        candidate_returns = _return_series(candles_by_symbol.get(symbol, []), 20)
        if len(candidate_returns) < 5:
            return {"available": False, "warning": None, "block_buy": False, "data_gap": "candidate_returns_unavailable"}
        correlated: list[dict[str, Any]] = []
        for pos_symbol, position in positions.items():
            if pos_symbol == symbol or position.get("qty", 0) <= 0:
                continue
            corr = _pearson(candidate_returns, _return_series(candles_by_symbol.get(pos_symbol, []), 20))
            if corr is not None and corr > 0.75:
                correlated.append({"symbol": pos_symbol, "correlation": round(corr, 4)})
        return {
            "available": True,
            "warning": "high_correlation_with_existing_position" if correlated else None,
            "block_buy": len(correlated) >= 2,
            "correlated_positions": correlated,
        }

    def _position_sizing_grade(
        self,
        context: dict[str, Any],
        portfolio_equity: float,
        positions: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        full = context.get("full_spectrum_analysis") or {}
        confluence = full.get("confluence_score") or {}
        entry = full.get("entry_quality") or {}
        indicators = full.get("indicator_suite") or {}
        sector = full.get("sector_rotation") or {}
        breadth = context.get("market_breadth_context") or {}
        rule_audit = context.get("system_gate_audit") or {}
        classification = rule_audit.get("classification") or {}
        strategy_logic = full.get("strategy_logic_filters") if isinstance(full.get("strategy_logic_filters"), dict) else {}
        tier = confluence.get("tier", "NO_SIGNAL")
        base = {
            "MAXIMUM_CONVICTION": 1.5,
            "HIGH_CONVICTION": 1.0,
            "TRADE_SIGNAL": 0.75,
            "WATCHLIST": 0.5,
            "NO_SIGNAL": 0.0,
        }.get(tier, 0.0)
        multiplier = base
        modifiers: list[str] = [f"base_from_confluence={tier}:{base}"]
        effective_entry_grade = (rule_audit.get("entry") or {}).get("effective_entry_grade") or entry.get("entry_grade")
        entry_mod = {"A": 1.0, "B": 0.85, "C": 0.65, "WATCH": 0.0}.get(effective_entry_grade, 0.0)
        multiplier *= entry_mod
        modifiers.append(f"effective_entry_grade={effective_entry_grade} x{entry_mod}")
        allocation_cap = float(rule_audit.get("allocation_cap_multiplier") if rule_audit else 1.0)
        phase3_size_cap = _float_or_none((strategy_logic.get("sizing") or {}).get("max_multiplier")) if strategy_logic else None
        if phase3_size_cap is not None:
            allocation_cap = min(allocation_cap, phase3_size_cap)
        if allocation_cap < 1.0:
            multiplier = min(multiplier, allocation_cap)
            modifiers.append(f"classification_gate={classification.get('classification', 'unknown')} cap {allocation_cap}")
        if classification.get("classification") == "SPECULATIVE":
            multiplier = min(multiplier, 0.15)
            modifiers.append("speculative_tiny_size_cap=0.15")
        atr_pct = indicators.get("atr_pct")
        if atr_pct is not None and float(atr_pct) > 4:
            multiplier *= 0.6
            modifiers.append("atr_pct_above_4 x0.6")
        if sector.get("sector_tailwind"):
            multiplier += 0.15
            modifiers.append("sector_tailwind +0.15")
        if sector.get("sector_headwind"):
            multiplier *= 0.6
            modifiers.append("sector_headwind x0.6")
        if breadth.get("breadth_regime") == "bull_confirmed":
            multiplier += 0.1
            modifiers.append("breadth_bull_confirmed +0.1")
        if breadth.get("breadth_regime") == "bear_warning":
            multiplier *= 0.5
            modifiers.append("breadth_bear_warning x0.5")
        multiplier = max(min(multiplier, 2.0), 0.0)
        max_position_pct = min(float(self.settings.max_position_pct), 0.15)
        recommended = min(max_position_pct, max_position_pct * multiplier)
        return {
            "final_multiplier": round(multiplier, 4),
            "base_multiplier": base,
            "modifier_details": modifiers,
            "recommended_max_position_pct": round(recommended, 6),
            "portfolio_equity": portfolio_equity,
            "open_positions": len([row for row in positions.values() if row.get("qty", 0) > 0]),
            "classification": classification,
            "rule_allocation_cap_multiplier": allocation_cap,
        }

    def _log_pre_filter(self, event: str, details: dict[str, Any]) -> None:
        try:
            self.sentiment.db.insert_agent_log("INFO", "strategy", event, event, details)
        except Exception:
            pass

    def _decision_details_json(
        self,
        context: dict[str, Any],
        action: str,
        decision_path: str,
        score_breakdown: dict[str, Any],
        action_reason: str,
        positions: dict[str, dict[str, Any]],
        llm_selected: bool = False,
    ) -> str:
        has_position = bool(context.get("position", {}).get("qty", 0))
        risk_limits = context.get("risk_limits", {})
        full_spectrum = context.get("full_spectrum_analysis") or {}
        scorecard = full_spectrum.get("institutional_scorecard") or {}
        gates = {
            "has_existing_position": has_position,
            "current_open_positions": len([row for row in positions.values() if row.get("qty", 0) > 0]),
            "max_positions": risk_limits.get("max_positions"),
            "buy_combined_threshold": (context.get("decision_gate_context") or {}).get("buy_threshold", 0.35),
            "buy_confluence_threshold": 16,
            "buy_requires_accumulation_proxy_ready": True,
            "accumulation_proxy_scorecard": {
                "buy_ready": scorecard.get("buy_ready"),
                "score": scorecard.get("total_score"),
                "grade": scorecard.get("grade"),
                "failed": scorecard.get("must_pass_failed", []),
                "hard_veto": (scorecard.get("hard_veto") or {}).get("failed", []),
            },
            "score_weakness_review_threshold": -0.38,
            "buy_requires_no_existing_position": True,
            "buy_requires_no_new_longs_clear": True,
            "sell_requires_existing_position": True,
            "broker_checks_after_decision": [
                "daily_loss_limit",
                "max_positions",
                "max_position_pct",
                "max_order_value_pct",
                "available_cash",
            ],
            "llm_deep_review_selected": llm_selected,
                "llm_candidate_limit": risk_limits.get("llm_candidate_limit"),
            "pre_filter": context.get("pre_filter"),
            "decision_gate_context": context.get("decision_gate_context"),
            "portfolio_correlation_gate": context.get("portfolio_correlation_gate"),
            "sizing_grade": context.get("sizing_grade"),
            "system_gate_audit": context.get("system_gate_audit"),
            "data_readiness": context.get("data_readiness"),
            "llm_primary_fallback": context.get("llm_primary_fallback"),
            "llm_primary_rule_blocked": context.get("llm_primary_rule_blocked"),
        }
        if has_position:
            position = context.get("position") or {}
            avg_price = float(position.get("avg_price") or 0)
            current_price = float((context.get("quote") or {}).get("price") or 0)
            pnl_pct = ((current_price - avg_price) / avg_price) if avg_price > 0 else 0.0
            gates["position_exit_management"] = {
                "avg_price": round(avg_price, 4),
                "current_price": round(current_price, 4),
                "unrealized_pnl_pct": round(pnl_pct, 4),
                "hard_stop_price": round(avg_price * (1 - self.settings.stop_loss_pct), 4) if avg_price else None,
                "take_profit_price": round(avg_price * (1 + self.settings.take_profit_pct), 4) if avg_price else None,
                "deterministic_exit": "SELL on hard stop, take-profit, persistent distribution, or explicit invalidation; composite weakness only triggers risk review",
                "scorecard_exit": "SELL if a hard veto, severe negative sentiment, or high-conflict condition appears",
                "llm_exit_review": "primary mode reviews open positions before new entries when within LLM Symbols/Cycle limit",
            }
        return _json_dumps(
            {
                "audit_version": 1,
                "decision_path": decision_path,
                "final_action": action,
                "action_reason": action_reason,
                "action_policy": {
                    "BUY": "combined score >= 0.35, confluence >= 16/26, scorecard ready=true, overall quality >=70 with grade A/B before publication/follow, no existing long position, and no no-new-longs override",
                    "SELL": "existing long position plus LLM exit call, hard stop, take-profit, persistent distribution, or explicit invalidation trigger",
                    "HOLD": "score/action gates did not permit a trade",
                },
                "score_breakdown": score_breakdown,
                "overall_score_pct": (context.get("system_gate_audit") or {}).get("overall_score_pct"),
                "overall_grade": (context.get("system_gate_audit") or {}).get("overall_grade"),
                "pre_filter": context.get("pre_filter"),
                "system_gate_audit": context.get("system_gate_audit"),
                "data_readiness": context.get("data_readiness"),
                "sizing_grade": context.get("sizing_grade"),
                "llm_primary_fallback": context.get("llm_primary_fallback"),
                "risk_gates": gates,
                "context": _compact_context(context),
            }
        )

    def _risk_exit_details_json(
        self,
        symbol: str,
        quote: Quote,
        position: dict[str, Any],
        stop: float,
        target: float,
        reason: str,
        atr: float | None = None,
        target2: float | None = None,
        partial_sell_pct: float | None = None,
        held_periods: int | None = None,
    ) -> str:
        avg_price = float(position["avg_price"])
        return _json_dumps(
            {
                "audit_version": 1,
                "decision_path": "risk_exit",
                "final_action": "SELL",
                "action_reason": reason,
                "risk_gates": {
                    "entry_price": avg_price,
                    "current_price": quote.price,
                    "stop_loss_pct": self.settings.stop_loss_pct,
                    "take_profit_pct": self.settings.take_profit_pct,
                    "atr": _round(atr),
                    "atr_aware": atr is not None,
                    "stop_price": round(stop, 4),
                    "target_1_price": round(target, 4),
                    "target_2_price": round(target2, 4) if target2 else None,
                    "stop_triggered": quote.price <= stop,
                    "take_profit_triggered": quote.price >= target,
                    "partial_sell_pct": partial_sell_pct,
                    "held_periods": held_periods,
                },
                "context": {
                    "symbol": symbol,
                    "quote": quote.to_dict(),
                    "position": position,
                },
            }
        )


def _compact_context(context: dict[str, Any]) -> dict[str, Any]:
    recent_candles = context.get("recent_candles", [])
    return {
        "symbol": context.get("symbol"),
        "company": context.get("company"),
        "market_region": context.get("market_region"),
        "currency": context.get("currency"),
        "sector": context.get("sector"),
        "exchange": context.get("exchange"),
        "quote": context.get("quote"),
        "position": context.get("position"),
        "technical_math": context.get("technical_math"),
        "candlestick_analysis": context.get("candlestick_analysis"),
        "best_strategy": context.get("best_strategy"),
        "strategy_signals": context.get("strategy_signals"),
        "sentiment": context.get("sentiment"),
        "global_market_context": context.get("global_market_context"),
        "institutional_context": context.get("institutional_context"),
        "market_breadth_context": context.get("market_breadth_context"),
        "macro_event_context": context.get("macro_event_context"),
        "timeframe_data": context.get("timeframe_data"),
        "sector_rotation": context.get("sector_rotation"),
        "delivery_data": context.get("delivery_data"),
        "data_readiness": context.get("data_readiness"),
        "performance_feedback": context.get("performance_feedback"),
        "system_gate_audit": context.get("system_gate_audit"),
        "pre_filter": context.get("pre_filter"),
        "decision_gate_context": context.get("decision_gate_context"),
        "sizing_grade": context.get("sizing_grade"),
        "llm_primary_selection": context.get("llm_primary_selection"),
        "llm_primary_fallback": context.get("llm_primary_fallback"),
        "llm_primary_rule_blocked": context.get("llm_primary_rule_blocked"),
        "llm_primary_gate": context.get("llm_primary_gate"),
        "llm_primary_review": context.get("llm_primary_review"),
        "full_spectrum_analysis": context.get("full_spectrum_analysis"),
        "universe_scan": context.get("universe_scan"),
        "risk_limits": context.get("risk_limits"),
        "recent_candle_count": len(recent_candles),
        "recent_candles_tail": recent_candles[-60:],
    }


def _json_dumps(value: dict[str, Any]) -> str:
    return json.dumps(value, default=str, separators=(",", ":"))


def _json_object(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}") if isinstance(value, str) else value
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        return {}


def _round(value: Any, digits: int = 4) -> float | None:
    try:
        return round(float(value), digits)
    except (TypeError, ValueError):
        return None


def _return_pct_from_candles(candles: list[Candle], window: int) -> float | None:
    closes = [float(candle.close) for candle in candles if candle.close]
    if len(closes) <= window:
        return None
    base = closes[-window - 1]
    if base <= 0:
        return None
    return ((closes[-1] - base) / base) * 100


def _percentile_rank(value: float, sorted_values: list[float]) -> float:
    if not sorted_values:
        return 50.0
    below_or_equal = sum(1 for item in sorted_values if item <= value)
    return max(min((below_or_equal / len(sorted_values)) * 100.0, 100.0), 0.0)


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


def _execution_cost_bps(settings: Settings) -> float:
    return round(
        max(float(settings.brokerage_bps or 0.0), 0.0)
        + max(float(settings.slippage_bps or 0.0), 0.0)
        + max(float(settings.taxes_bps or 0.0), 0.0)
        + max(float(settings.stt_bps or 0.0), 0.0),
        4,
    )


def _market_specific_context(context: dict[str, Any], market_region: str) -> dict[str, Any]:
    if not isinstance(context, dict):
        return {}
    by_market = context.get("by_market")
    if isinstance(by_market, dict):
        selected = by_market.get(str(market_region or "").upper())
        if isinstance(selected, dict):
            return selected
    return context


def _atr(candles: list[Candle], period: int = 14) -> float | None:
    if len(candles) < period + 1:
        return None
    ranges = []
    for previous, candle in zip(candles[-period - 1 : -1], candles[-period:]):
        ranges.append(max(candle.high - candle.low, abs(candle.high - previous.close), abs(candle.low - previous.close)))
    return sum(ranges) / len(ranges) if ranges else None


def _held_periods_from_position(position: dict[str, Any], candles: list[Candle]) -> int:
    opened_at = _parse_time(position.get("updated_at"))
    if opened_at is None or not candles:
        return 0
    return sum(1 for candle in candles if (_parse_time(candle.ts) or datetime.min.replace(tzinfo=timezone.utc)) > opened_at)


def _delivery_distribution_sessions(delivery: dict[str, Any]) -> int:
    payload = delivery.get("score_payload") if isinstance(delivery.get("score_payload"), dict) else {}
    nested_trend = payload.get("trend") if isinstance(payload.get("trend"), dict) else {}
    candidates = [
        payload.get("distribution_streak"),
        delivery.get("distribution_streak"),
        delivery.get("distribution_days"),
        nested_trend.get("distribution_days"),
    ]
    for value in candidates:
        try:
            return max(int(value), 0)
        except (TypeError, ValueError):
            continue
    return 1 if str(delivery.get("bias") or "").lower() == "distribution" else 0


def _macro_event_driven_thesis(macro_event_context: dict[str, Any]) -> dict[str, Any]:
    evidence = []
    for key in ("event_driven", "explicit_event_driven", "earnings_event_driven", "allow_earnings_trade", "catalyst_trade"):
        if macro_event_context.get(key) is True:
            evidence.append(f"macro_context.{key}=true")
    text = " ".join(
        str(macro_event_context.get(key) or "").lower()
        for key in ("strategy", "strategy_type", "thesis", "event_thesis", "catalyst")
    )
    if "event-driven" in text or "event driven" in text or "catalyst" in text or "earnings trade" in text:
        evidence.append("macro_context contains explicit event/catalyst thesis")
    return {
        "supported": bool(evidence),
        "evidence": list(dict.fromkeys(evidence)),
    }


def _performance_feedback_block(feedback: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(feedback, dict) or feedback.get("available") is False:
        return None
    candidates = [
        ("strategy_market", feedback.get("selected_strategy_market")),
        ("strategy", feedback.get("selected_strategy")),
    ]
    for scope, item in candidates:
        if not isinstance(item, dict) or not item:
            continue
        closed = int(item.get("closed_trades") or 0)
        if closed < 20:
            continue
        expectancy = _float_or_none(item.get("expectancy_pct")) or 0.0
        stop_rate = _float_or_none(item.get("stop_hit_rate")) or 0.0
        win_rate = _float_or_none(item.get("win_rate")) or 0.0
        strong_negative_expectancy = expectancy <= -0.75
        severe_stop_profile = stop_rate >= 0.65 and expectancy < 0
        persistent_low_win_rate = win_rate < 0.30 and expectancy < -0.25
        if strong_negative_expectancy or severe_stop_profile or persistent_low_win_rate:
            return {
                "scope": scope,
                "key": item.get("key"),
                "closed_trades": closed,
                "expectancy_pct": expectancy,
                "stop_hit_rate": stop_rate,
                "win_rate": win_rate,
                "reason": "historical strategy feedback is strongly negative; require manual review before fresh BUY",
            }
    return None


def _float_or_none(value: Any) -> float | None:
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def _return_series(candles: list[Candle], periods: int) -> list[float]:
    closes = [float(candle.close) for candle in candles[-(periods + 1) :]]
    if len(closes) < 2:
        return []
    returns: list[float] = []
    for previous, current in zip(closes, closes[1:]):
        if previous:
            returns.append((current - previous) / previous)
    return returns


def _pearson(left: list[float], right: list[float]) -> float | None:
    n = min(len(left), len(right))
    if n < 5:
        return None
    x = left[-n:]
    y = right[-n:]
    mean_x = sum(x) / n
    mean_y = sum(y) / n
    num = sum((a - mean_x) * (b - mean_y) for a, b in zip(x, y))
    den_x = sum((a - mean_x) ** 2 for a in x)
    den_y = sum((b - mean_y) ** 2 for b in y)
    denom = (den_x * den_y) ** 0.5
    return num / denom if denom else None
