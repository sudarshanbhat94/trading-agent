from __future__ import annotations

import asyncio
import ast
import json
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from typing import Any

from .analysis_tools import build_symbol_tool_context, deterministic_score, deterministic_score_breakdown
from .canonical_trade import canonical_trade_readiness_gate
from .config import Settings
from .indicators import technical_snapshot
from .llm_brain import LLMBrain
from .llm_policy import LLM_HARD_DISABLED
from .market_regions import market_region_for_row
from .models import Candle, Decision, Quote, utc_now
from .raw_entry_model import RAW_ENTRY_MODEL_VERSION, evaluate_raw_entry
from .sentiment import SentimentService
from .signal_quality import FRESH_BUY_MIN_SCORE, OPPORTUNITY_PROBE_MIN_SCORE

_WAIT_ONLY_TRADE_WINDOWS = {
    "confirm_before_entry",
    "not_ready",
    "wait_for_pullback",
    "watch_for_ignition",
    "watch_for_pullback",
    "watch_only",
}


def _decision_authority_reset_enabled(settings: Any) -> bool:
    if not hasattr(settings, "decision_authority_mode"):
        return False
    return True


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
    failed_gates = (
        decision_gates.get("blocking_failed_gates")
        if isinstance(decision_gates.get("blocking_failed_gates"), list)
        else decision_gates.get("failed_gates")
        if isinstance(decision_gates.get("failed_gates"), list)
        else []
    )
    if failed_gates:
        return "deterministic_buy_gates_failed_llm_buy"

    return None


def _fresh_market_data_block_reason(context: dict[str, Any]) -> str:
    data_readiness = context.get("data_readiness") if isinstance(context.get("data_readiness"), dict) else {}
    scan = context.get("opportunity_scan") if isinstance(context.get("opportunity_scan"), dict) else {}
    labels: set[str] = set()
    gate = data_readiness.get("fresh_market_data_gate") if isinstance(data_readiness.get("fresh_market_data_gate"), dict) else {}
    if gate and gate.get("passed") is False:
        return str(gate.get("reason") or "stale_market_data")
    if gate and gate.get("passed") is True:
        return ""
    for value in data_readiness.get("missing_data") or []:
        if str(value or "").strip():
            labels.add(str(value).strip().lower())
    for collection_key in ("hard_gaps", "soft_gaps"):
        for gap in data_readiness.get(collection_key) or []:
            if isinstance(gap, dict):
                for key in ("key", "label", "reason"):
                    value = str(gap.get(key) or "").strip().lower()
                    if value:
                        labels.add(value)
            elif str(gap or "").strip():
                labels.add(str(gap).strip().lower())
    data_quality = scan.get("data_quality") if isinstance(scan.get("data_quality"), dict) else {}
    labels.update(str(value or "").strip().lower() for value in data_quality.get("missing") or [] if str(value or "").strip())
    if any(
        token in label
        for label in labels
        for token in ("stale_quote", "stale_intraday", "prior_session", "previous_session", "moneycontrol_prior")
    ):
        return "stale_market_data"
    label = str(scan.get("label") or scan.get("bucket") or "").strip().upper()
    if label == "DATA_STALE_WATCH":
        return "data_stale_watch"
    return ""


def _negative_catalyst_block_reason(context: dict[str, Any]) -> str:
    scan = context.get("opportunity_scan") if isinstance(context.get("opportunity_scan"), dict) else {}
    label = str(scan.get("label") or scan.get("bucket") or "").strip().upper()
    setup = str(scan.get("setup") or "").strip().lower()
    if label == "OVERHANG_REMOVAL_RERATE" or setup == "overhang_removal_rerate":
        return ""
    sentiment = context.get("sentiment") if isinstance(context.get("sentiment"), dict) else {}
    score = _float_or_none(sentiment.get("score")) or _float_or_none(context.get("sentiment_score"))
    confidence = _float_or_none(sentiment.get("confidence"))
    negative = bool(
        sentiment.get("negative_catalyst")
        or scan.get("negative_catalyst")
        or (score is not None and score <= -0.30 and (confidence is None or confidence >= 0.30))
    )
    return "negative_catalyst_no_new_longs" if negative else ""


def _opportunity_scan_wait_reason(scan: dict[str, Any]) -> str:
    if not scan:
        return ""
    label = str(scan.get("label") or scan.get("bucket") or "").strip().upper()
    if label in {"ACTIONABLE_WATCH", "DATA_STALE_WATCH", "LATE_CHASE_AVOID", "LOW_QUALITY_SHORT_COVERING"}:
        return f"opportunity_scan_{label.lower()}"
    setup = str(scan.get("setup") or "").strip().lower()
    if setup in {"circuit_demand_lock", "extended_momentum_watch", "pre_rally_fuel"}:
        return "opportunity_scan_wait_state"
    trade_window = _scan_trade_window(scan)
    normalized = str(trade_window or "").strip().lower()
    if normalized in _WAIT_ONLY_TRADE_WINDOWS:
        return f"opportunity_scan_{normalized}"
    return ""


def _scan_trade_window(scan: dict[str, Any]) -> str:
    values = [scan.get("trade_window")]
    market_action = scan.get("market_action") if isinstance(scan.get("market_action"), dict) else {}
    rally = scan.get("rally_radar") if isinstance(scan.get("rally_radar"), dict) else {}
    values.extend([market_action.get("trade_window"), rally.get("trade_window")])
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _primary_decision_gate(failed_gates: list[dict[str, Any]]) -> dict[str, Any]:
    if not failed_gates:
        return {}
    priority = {
        "invalid_price": 0,
        "phase2_data_readiness": 10,
        "fresh_market_data_gate": 20,
        "technical_score_gate": 30,
        "actionable_strategy_gate": 40,
        "opportunity_scan_entry_window": 50,
        "overall_quality_gate": 60,
        "entry_grade_gate": 70,
    }
    indexed = [
        (priority.get(str(gate.get("gate") or ""), 500), index, gate)
        for index, gate in enumerate(failed_gates)
        if isinstance(gate, dict)
    ]
    return min(indexed, key=lambda item: (item[0], item[1]))[2] if indexed else {}


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
        llm_primary_required = (not LLM_HARD_DISABLED) and self.settings.llm_decision_mode == "primary" and self.settings.llm_provider != "offline"
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
            "decision_symbols": len(universe),
            "skip_symbol_backtest": len(universe) >= 100,
            "backtest_skip_reason": "broad_open_market_decision_cycle" if len(universe) >= 100 else "",
        }
        try:
            performance_feedback = self.sentiment.db.strategy_performance_feedback()
        except Exception:
            performance_feedback = {}
        pattern_states = self._pattern_states_for_cycle([str(row.get("symbol") or "") for row in universe])

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
            pattern_state = pattern_states.get(symbol) or self._pattern_state(symbol)
            sentiment_detail = self.sentiment.latest_for_symbol(symbol)
            context = build_symbol_tool_context(
                row=row,
                quote=quote,
                candles=candles,
                position=positions.get(symbol),
                sentiment_score=sentiment_score,
                risk_limits=risk_limits,
                global_context=global_context,
                institutional_context=institutional_context,
                sentiment_detail=sentiment_detail,
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
                    "sentiment_detail": sentiment_detail,
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
                    "LLM primary reviews open positions first for exit risk, then raw-entry BUY candidates, "
                    "then highest-ranked symbols by scanner rank, raw score, combined score, technical score, and sentiment"
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
            raw_entry = context.get("raw_entry_model") if isinstance(context.get("raw_entry_model"), dict) else {}
            entry_strategy = str(raw_entry.get("setup") or RAW_ENTRY_MODEL_VERSION)
            global_risk = context.get("global_market_context", {})
            institutional = context.get("institutional_context", {})
            confluence = context.get("full_spectrum_analysis", {}).get("confluence_score", {})
            liquidity = context.get("full_spectrum_analysis", {}).get("liquidity_profile", {})
            conflicts = context.get("full_spectrum_analysis", {}).get("signal_conflicts", {})
            institutional_bias = (institutional.get("market_bias") or {}).get("score", 0.0)
            reason = (
                f"tools technical={item['technical'].score:.2f} ({item['technical'].trend}), "
                f"candles={candle_summary['score']:.2f} {candle_summary['patterns']}, "
                f"entry_model={RAW_ENTRY_MODEL_VERSION}:{float(raw_entry.get('raw_score') or 0.0):.2f}, "
                f"entry_reason={raw_entry.get('reason')}, "
                f"sentiment={item['sentiment_score']:.2f}, "
                f"global={float(global_risk.get('risk_score', 0.0) or 0.0):.2f} ({global_risk.get('regime', 'unknown')}), "
                f"free_inst={float(institutional_bias or 0.0):.2f} ({institutional.get('source_quality', 'unknown')}), "
                f"confluence={confluence.get('total', 0)}/26 {confluence.get('tier', 'NO_SIGNAL')}, "
                f"liquidity={liquidity.get('liquidity_tier', 'unknown')}, conflicts={conflicts.get('severity', 'none')}, "
                f"combined={item['combined']:.2f}, universe_rank={context['universe_scan']['rank']}/{len(scan_items)}"
            )
            tomorrow_plan_decision = context.get("tomorrow_plan_decision") if isinstance(context.get("tomorrow_plan_decision"), dict) else {}
            if tomorrow_plan_decision.get("active"):
                plan_bits = [str(tomorrow_plan_decision.get("section") or "planned")]
                if tomorrow_plan_decision.get("eligible_for_entry_boost"):
                    plan_bits.append(
                        f"live_confirmed:{'+'.join(tomorrow_plan_decision.get('live_confirmation') or [])}"
                    )
                    plan_bits.append(f"threshold_boost={float(tomorrow_plan_decision.get('threshold_boost') or 0.0):.2f}")
                else:
                    plan_bits.append(str(tomorrow_plan_decision.get("reason") or "waiting"))
                reason = f"{reason}, tomorrow_plan={'/'.join(plan_bits)}"
            if context.get("llm_primary_fallback"):
                reason = f"{reason}, {context['llm_primary_fallback'].get('reason', 'llm_primary_failed_safe_hold')}"
            if context.get("llm_primary_gate", {}).get("effect") == "forced_hold_no_trade":
                reason = f"{reason}, {context['llm_primary_gate'].get('reason', 'llm_primary_required_no_unreviewed_trade')}"
            action = item["action"]
            confidence = item["confidence"]
            decision_path = RAW_ENTRY_MODEL_VERSION
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
                strategy=entry_strategy,
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
        return self._raw_entry_action_from_context(symbol, positions, context, has_position=has_position)
        reset_authority = _decision_authority_reset_enabled(self.settings)
        if reset_authority:
            return self._fresh_only_action_from_context(symbol, positions, context)
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
        opportunity_probe = self._opportunity_probe_profile(context)
        rule_audit = evaluate_rules_for_context(
            context,
            positions,
            context.get("risk_limits", {}).get("portfolio_equity", 0.0),
        )
        if not reset_authority and opportunity_probe.get("source") == "top_gainers_playbook":
            rule_audit = self._rule_audit_with_playbook_overrides(rule_audit, opportunity_probe)
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
        best_strategy_name = str((context.get("best_strategy") or {}).get("name") or "")
        session_momentum = full_spectrum.get("session_momentum") if isinstance(full_spectrum.get("session_momentum"), dict) else {}
        live_momentum_review = (
            full_spectrum.get("live_momentum_review") if isinstance(full_spectrum.get("live_momentum_review"), dict) else {}
        )
        data_ready = context.get("data_readiness") if isinstance(context.get("data_readiness"), dict) else {}
        data_sources = data_ready.get("sources") if isinstance(data_ready.get("sources"), dict) else {}
        us_yahoo_reference_signal = (
            str(data_ready.get("market_region") or "").upper() == "US"
            and data_ready.get("trade_decision_ready") is True
            and "yahoo" in str(data_sources.get("quote") or "").lower()
        )
        if us_yahoo_reference_signal:
            threshold = max(threshold, 0.45)
        tomorrow_plan_decision = self._tomorrow_plan_decision_context(context, opportunity_probe)
        if tomorrow_plan_decision.get("eligible_for_entry_boost"):
            boost = float(tomorrow_plan_decision.get("threshold_boost") or 0.0)
            threshold = max(threshold - boost, 0.20)
            tomorrow_plan_decision["adjusted_buy_threshold"] = round(threshold, 4)
        context["tomorrow_plan_decision"] = tomorrow_plan_decision
        failed_gates: list[dict[str, Any]] = []

        def fail(gate: str, value: Any, reason: str) -> None:
            failed_gates.append({"gate": gate, "value": value, "reason": reason})

        if not has_position and best_strategy_name == "no_actionable_strategy" and not reset_authority:
            fail("actionable_strategy_gate", best_strategy_name, "no_actionable_strategy")
        technical_score = _float_or_none((context.get("technical_math") or {}).get("score")) if isinstance(context.get("technical_math"), dict) else None
        if not has_position and technical_score is not None and technical_score < 0.50:
            fail("technical_score_gate", technical_score, "technical_score_below_0_50")
        stale_data_reason = _fresh_market_data_block_reason(context)
        if not has_position and stale_data_reason:
            fail("fresh_market_data_gate", data_ready or context.get("opportunity_scan"), stale_data_reason)
        negative_catalyst_reason = _negative_catalyst_block_reason(context)
        if not has_position and negative_catalyst_reason:
            fail("catalyst_quality_gate", context.get("sentiment"), negative_catalyst_reason)
        opportunity_wait_reason = _opportunity_scan_wait_reason(
            context.get("opportunity_scan") if isinstance(context.get("opportunity_scan"), dict) else {}
        )
        if not has_position and opportunity_wait_reason:
            fail("opportunity_scan_entry_window", context.get("opportunity_scan"), opportunity_wait_reason)
        if (
            not has_position
            and us_yahoo_reference_signal
            and not bool(session_momentum.get("confirmed"))
            and not bool(live_momentum_review.get("strategy_ready"))
        ):
            fail(
                "session_momentum_gate",
                {"source": data_sources.get("quote"), "session_momentum": session_momentum, "live_momentum_review": live_momentum_review},
                "us_yahoo_reference_needs_live_confirmation",
            )

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
        broad_momentum_strategy = best_strategy_name in {
            "time_series_momentum_trend",
            "normalized_momentum_factor",
            "aggressive_relative_strength_breakout",
            "fifty_two_week_high_momentum",
            "minervini_trend_template",
        }
        live_momentum_strategy = best_strategy_name == "live_intraday_momentum"
        if (
            not has_position
            and broad_momentum_strategy
            and not live_momentum_strategy
            and not bool(session_momentum.get("confirmed"))
        ):
            fail(
                "session_momentum_gate",
                session_momentum,
                "broad_momentum_entry_needs_current_session_confirmation",
            )
        if not has_position and bool(live_momentum_review.get("late_chase")):
            fail(
                "session_momentum_gate",
                live_momentum_review,
                "late_intraday_momentum_wait_for_pullback",
            )
        if not has_position and live_momentum_strategy and not bool(live_momentum_review.get("strategy_ready")):
            fail("session_momentum_gate", live_momentum_review or session_momentum, "live_momentum_not_trade_ready")
        if stage and not stage.get("buy_permitted", True):
            fail("stage_buy_permitted", stage.get("stage"), "stage_analysis_not_stage2_markup")
        if divergence.get("climax_volume_top"):
            fail("climax_volume_gate", True, "climax_top_detected_no_new_longs")
        if alignment_grade == "D":
            fail("timeframe_alignment_gate", "D", "timeframe_alignment_conflict")
        if alignment_grade == "C":
            context["mtf_c_speculative_size_only"] = True
        overall_score_pct = float(rule_audit.get("overall_score_pct") or 0.0)
        if (
            not has_position
            and overall_score_pct < FRESH_BUY_MIN_SCORE
            and not (
                not reset_authority
                and opportunity_probe.get("ready")
                and overall_score_pct >= OPPORTUNITY_PROBE_MIN_SCORE
            )
        ):
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
            {
                "gate": "session_momentum_gate",
                "passed": (
                    has_position
                    or not (broad_momentum_strategy or live_momentum_strategy)
                    or bool(session_momentum.get("confirmed"))
                    or bool(live_momentum_review.get("strategy_ready"))
                ),
                "value": live_momentum_review or session_momentum,
            },
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
            "opportunity_probe": opportunity_probe,
            "blocking_failed_gates": failed_gates,
        }
        blocking_failed_gates = failed_gates
        if not reset_authority and opportunity_probe.get("ready") and failed_gates:
            opportunity_probe["data_readiness_block_absorbable"] = any(
                (
                    str(gate.get("gate") or "") in {"system_rule_DATA_READINESS_BLOCK", "phase2_data_readiness"}
                    and self._opportunity_probe_can_absorb_data_readiness_block(gate.get("value"), opportunity_probe)
                )
                for gate in failed_gates
            )
            blocking_failed_gates = [
                gate
                for gate in failed_gates
                if not self._opportunity_probe_can_absorb_gate(gate, opportunity_probe)
            ]
            context["decision_gate_context"]["opportunity_probe"]["absorbed_gates"] = [
                gate
                for gate in failed_gates
                if self._opportunity_probe_can_absorb_gate(gate, opportunity_probe)
            ]
            context["decision_gate_context"]["opportunity_probe"]["blocking_gates"] = blocking_failed_gates
            context["decision_gate_context"]["blocking_failed_gates"] = blocking_failed_gates
            if not blocking_failed_gates:
                context["system_gate_audit"] = self._opportunity_probe_publishable_rule_audit(
                    rule_audit,
                    opportunity_probe,
                    context["decision_gate_context"]["opportunity_probe"]["absorbed_gates"],
                )
        reset_gate: dict[str, Any] = {}
        if reset_authority and not has_position:
            reset_gate = self._reset_trade_authority_gate(
                context=context,
                rule_audit=rule_audit,
                failed_gates=failed_gates,
                combined=combined,
                threshold=threshold,
                confluence_total=confluence_total,
                effective_entry_grade=effective_entry_grade,
            )
            context["decision_gate_context"]["reset_trade_authority_gate"] = reset_gate
            if not reset_gate.get("passed"):
                reset_block = {
                    "gate": "reset_trade_authority_gate",
                    "value": reset_gate,
                    "reason": reset_gate.get("primary_blocker")
                    or reset_gate.get("reason")
                    or "reset_trade_authority_not_ready",
                }
                failed_gates.append(reset_block)
                blocking_failed_gates = [*blocking_failed_gates, reset_block]
                context["decision_gate_context"]["blocking_failed_gates"] = blocking_failed_gates
        primary_blocker = _primary_decision_gate(blocking_failed_gates)
        context["decision_gate_context"]["primary_blocker"] = primary_blocker
        context["decision_gate_context"]["secondary_blockers"] = [
            gate
            for gate in blocking_failed_gates
            if not primary_blocker or gate is not primary_blocker
        ]
        if failed_gates and not has_position:
            if blocking_failed_gates:
                return "HOLD"
        buy_ready = bool(reset_gate.get("passed")) if reset_authority else bool(scorecard.get("buy_ready")) or bool(opportunity_probe.get("ready"))
        buy_threshold_met = bool(reset_gate.get("passed")) if reset_authority else combined >= threshold or (
            bool(opportunity_probe.get("ready")) and combined >= float(opportunity_probe.get("combined_floor") or 0.20)
        )
        probe_min_confluence = _float_or_none(opportunity_probe.get("min_confluence"))
        buy_confluence_floor = (
            18.0
            if reset_authority
            else probe_min_confluence
            if opportunity_probe.get("ready") and probe_min_confluence is not None
            else 16.0
        )
        if buy_threshold_met and confluence_total >= buy_confluence_floor and buy_ready and not has_position:
            canonical_rule_audit = (
                context.get("system_gate_audit") if isinstance(context.get("system_gate_audit"), dict) else rule_audit
            )
            canonical_details = {
                "action": "BUY",
                "overall_score_pct": canonical_rule_audit.get("overall_score_pct"),
                "overall_grade": canonical_rule_audit.get("overall_grade"),
                "confluence": confluence_total,
                "hard_blocked": bool(canonical_rule_audit.get("hard_blocked")),
                "hard_blocks": canonical_rule_audit.get("hard_blocks") if isinstance(canonical_rule_audit.get("hard_blocks"), list) else [],
                "active_flags": canonical_rule_audit.get("active_flags") if isinstance(canonical_rule_audit.get("active_flags"), list) else [],
                "risk_flags": risk_overrides.get("flags") if isinstance(risk_overrides.get("flags"), list) else [],
                "failed_gates": context["decision_gate_context"].get("failed_gates") or [],
                "data_readiness": data_ready,
                "quote": context.get("quote") if isinstance(context.get("quote"), dict) else {},
                "entry_quality": entry,
                "breakout_quality": breakout,
                "strategy_logic_filters": strategy_logic,
                "opportunity_scan": context.get("opportunity_scan") if isinstance(context.get("opportunity_scan"), dict) else {},
                "live_momentum_review": live_momentum_review,
                "risk_gates": {"decision_gate_context": context["decision_gate_context"]},
                "entry_zone": (full_spectrum.get("trade_plan") or {}).get("entry_zone") if isinstance(full_spectrum.get("trade_plan"), dict) else None,
                "stop_loss": (full_spectrum.get("trade_plan") or {}).get("stop_loss") if isinstance(full_spectrum.get("trade_plan"), dict) else None,
                "targets": (full_spectrum.get("trade_plan") or {}).get("targets") if isinstance(full_spectrum.get("trade_plan"), dict) else [],
                "market_region": context.get("market_region"),
            }
            canonical_gate = canonical_trade_readiness_gate(
                {
                    "symbol": symbol,
                    "action": "BUY",
                    "signal_type": "BUY",
                    "status": "ACTIVE",
                    "overall_score_pct": canonical_rule_audit.get("overall_score_pct"),
                    "overall_grade": canonical_rule_audit.get("overall_grade"),
                    "confluence": confluence_total,
                    "hard_blocked": bool(canonical_rule_audit.get("hard_blocked")),
                    "data_readiness": data_ready,
                    "quote": context.get("quote") if isinstance(context.get("quote"), dict) else {},
                    "market_region": context.get("market_region"),
                    "details": canonical_details,
                }
            )
            context["canonical_trade_gate"] = canonical_gate
            context["decision_gate_context"]["canonical_trade_gate"] = canonical_gate
            if canonical_gate.get("passed"):
                return "BUY"
            canonical_block = {
                "gate": "canonical_trade_contract",
                "value": canonical_gate,
                "reason": canonical_gate.get("primary_blocker") or canonical_gate.get("reason") or "canonical_trade_not_ready",
            }
            context["decision_gate_context"]["blocking_failed_gates"] = [canonical_block]
            context["decision_gate_context"]["primary_blocker"] = canonical_block
            context["decision_gate_context"]["secondary_blockers"] = canonical_gate.get("secondary_blockers") or []
            return "HOLD"
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

    def _raw_entry_action_from_context(
        self,
        symbol: str,
        positions: dict[str, dict[str, Any]],
        context: dict[str, Any],
        *,
        has_position: bool,
    ) -> str:
        if has_position:
            raw = {
                "version": RAW_ENTRY_MODEL_VERSION,
                "passed": False,
                "action": "HOLD",
                "reason": "existing_position_managed_by_position_rules",
                "raw_score": None,
                "grade": None,
                "truth_blocks": [],
                "trade_plan": {},
                "legacy_decision_logic_removed": True,
            }
        else:
            raw = evaluate_raw_entry(context, self.settings)

        truth_blocks = raw.get("truth_blocks") if isinstance(raw.get("truth_blocks"), list) else []
        compatibility_blocks = [
            {"gate": "truth_check", "reason": item.get("reason"), "value": item.get("value")}
            for item in truth_blocks
            if isinstance(item, dict)
        ]
        context["raw_entry_model"] = raw
        context["fresh_trade_authority"] = raw
        context["decision_gate_context"] = {
            "decision_authority": RAW_ENTRY_MODEL_VERSION,
            "raw_entry_model": raw,
            "fresh_trade_authority": raw,
            "failed_gates": compatibility_blocks,
            "blocking_failed_gates": compatibility_blocks,
            "primary_blocker": compatibility_blocks[0] if compatibility_blocks else {},
            "secondary_blockers": compatibility_blocks[1:],
            "evaluated_gates": [],
            "legacy_logic_deleted": True,
            "legacy_logic_removed_components": [
                "pre_filter_context",
                "btst_strategy_mutation",
                "live_momentum_strategy_mutation",
                "opportunity_probe_absorption",
                "phase2_phase3_entry_gates",
                "canonical_trade_entry_contract",
                "institutional_scorecard_entry_veto",
            ],
            "policy": "Entry authority is raw quote/scan scoring plus invalid/untradeable truth checks only.",
        }
        context["system_gate_audit"] = {
            "hard_blocked": bool(truth_blocks),
            "hard_blocks": [
                {"flag": str(item.get("reason") or "").upper(), "reason": item.get("reason"), "value": item.get("value")}
                for item in truth_blocks
                if isinstance(item, dict)
            ],
            "soft_flags": [],
            "active_flags": [str(item.get("reason") or "").upper() for item in truth_blocks if isinstance(item, dict)],
            "overall_score_pct": raw.get("raw_score"),
            "overall_grade": raw.get("grade"),
            "classification": "RAW_ENTRY_BUY" if raw.get("passed") else "RAW_ENTRY_HOLD",
            "allocation_cap_multiplier": 1.0,
            "legacy_decision_logic_removed": True,
        }

        full = context.get("full_spectrum_analysis") if isinstance(context.get("full_spectrum_analysis"), dict) else {}
        trade_plan = raw.get("trade_plan") if isinstance(raw.get("trade_plan"), dict) else {}
        if trade_plan:
            full["trade_plan"] = trade_plan
        entry = full.get("entry_quality") if isinstance(full.get("entry_quality"), dict) else {}
        entry["entry_grade"] = raw.get("grade")
        entry["setup_type"] = raw.get("setup")
        entry["quality_score"] = raw.get("raw_score")
        entry["source"] = RAW_ENTRY_MODEL_VERSION
        full["entry_quality"] = entry
        confluence = full.get("confluence_score") if isinstance(full.get("confluence_score"), dict) else {}
        confluence["total"] = round(max(float(raw.get("raw_score") or 0.0) / 4.0, 0.0), 4)
        confluence["tier"] = "RAW_ENTRY_MODEL"
        full["confluence_score"] = confluence
        context["full_spectrum_analysis"] = full
        context["best_strategy"] = {
            "name": str(raw.get("setup") or RAW_ENTRY_MODEL_VERSION),
            "score": round(float(raw.get("raw_score") or 0.0) / 100.0, 4),
            "direction": raw.get("action"),
            "confidence": raw.get("confidence"),
            "notes": [raw.get("reason")],
            "metadata": {"source": RAW_ENTRY_MODEL_VERSION},
        }
        context["strategy_signals"] = [context["best_strategy"]]
        context["tomorrow_plan_decision"] = {"active": False, "reason": "legacy_tomorrow_plan_entry_boost_removed"}
        return "BUY" if raw.get("passed") else "HOLD"

    def _fresh_only_action_from_context(
        self,
        symbol: str,
        positions: dict[str, dict[str, Any]],
        context: dict[str, Any],
    ) -> str:
        has_position = symbol in positions and positions[symbol].get("qty", 0) > 0
        if has_position:
            gate = {
                "passed": False,
                "mode": "fresh_authority_v1",
                "reason": "existing_position_managed_by_risk_exit_only",
                "primary_blocker": "existing_position",
                "blockers": [{"reason": "existing_position", "value": positions.get(symbol)}],
                "checks": {},
            }
            context["decision_gate_context"] = {
                "decision_authority": "fresh_authority_v1",
                "fresh_trade_authority": gate,
                "failed_gates": [],
                "blocking_failed_gates": [],
                "primary_blocker": {},
                "secondary_blockers": [],
                "evaluated_gates": [],
                "legacy_logic_deleted": True,
                "legacy_logic_policy": "No legacy strategy score, old soft gate, opportunity probe, scorecard, or canonical legacy gate can approve or veto reset-mode entries.",
            }
            context["system_gate_audit"] = {
                "hard_blocked": False,
                "hard_blocks": [],
                "soft_flags": [],
                "active_flags": [],
                "overall_score_pct": None,
                "overall_grade": None,
                "classification": "POSITION_MONITOR",
            }
            return "HOLD"

        gate = self._fresh_trade_authority_gate(context)
        blockers = [
            {"gate": "fresh_trade_authority", "value": item.get("value"), "reason": item.get("reason")}
            for item in gate.get("blockers", [])
        ]
        primary = blockers[0] if blockers else {}
        context["decision_gate_context"] = {
            "decision_authority": "fresh_authority_v1",
            "fresh_trade_authority": gate,
            "failed_gates": blockers,
            "blocking_failed_gates": blockers,
            "primary_blocker": primary,
            "secondary_blockers": blockers[1:],
            "evaluated_gates": gate.get("evaluated_gates", []),
            "legacy_logic_deleted": True,
            "legacy_logic_policy": "No legacy strategy score, old soft gate, opportunity probe, scorecard, or canonical legacy gate can approve or veto reset-mode entries.",
        }
        audit = gate.get("system_gate_audit") if isinstance(gate.get("system_gate_audit"), dict) else {}
        context["system_gate_audit"] = audit
        context["fresh_trade_authority"] = gate
        full = context.get("full_spectrum_analysis") if isinstance(context.get("full_spectrum_analysis"), dict) else {}
        if gate.get("passed"):
            plan = gate.get("trade_plan") if isinstance(gate.get("trade_plan"), dict) else {}
            if plan:
                full["trade_plan"] = plan
            entry = full.get("entry_quality") if isinstance(full.get("entry_quality"), dict) else {}
            entry["entry_grade"] = gate.get("fresh_grade")
            entry["setup_type"] = gate.get("setup")
            entry["quality_score"] = gate.get("fresh_score")
            entry["volume_confirmation"] = True
            full["entry_quality"] = entry
            breakout = full.get("breakout_quality") if isinstance(full.get("breakout_quality"), dict) else {}
            breakout["volume_confirmation"] = True
            full["breakout_quality"] = breakout
            confluence = full.get("confluence_score") if isinstance(full.get("confluence_score"), dict) else {}
            confluence["total"] = max(float(confluence.get("total") or 0.0), float(gate.get("fresh_confluence") or 0.0))
            confluence["tier"] = "FRESH_AUTHORITY"
            full["confluence_score"] = confluence
            context["full_spectrum_analysis"] = full
            return "BUY"
        return "HOLD"

    def _fresh_trade_authority_gate(self, context: dict[str, Any]) -> dict[str, Any]:
        full = context.get("full_spectrum_analysis") if isinstance(context.get("full_spectrum_analysis"), dict) else {}
        scan = context.get("opportunity_scan") if isinstance(context.get("opportunity_scan"), dict) else {}
        data_ready = context.get("data_readiness") if isinstance(context.get("data_readiness"), dict) else {}
        quote = context.get("quote") if isinstance(context.get("quote"), dict) else {}
        risk = full.get("risk_overrides") if isinstance(full.get("risk_overrides"), dict) else {}
        blockers: list[dict[str, Any]] = []
        evaluated: list[dict[str, Any]] = []

        def block(reason: str, value: Any = None) -> None:
            blockers.append({"reason": reason, "value": value})

        def evaluated_gate(name: str, passed: bool, value: Any = None) -> None:
            evaluated.append({"gate": name, "passed": passed, "value": value})

        price = _float_or_none(quote.get("price"))
        quote_source = str(quote.get("source") or "").lower()
        market_region = str(context.get("market_region") or data_ready.get("market_region") or scan.get("market_region") or "").upper()
        broker_quote = any(token in quote_source for token in ("upstox", "kite", "nubra", "alpaca", "polygon"))
        quote_has_ohlcv = all((_float_or_none(quote.get(key)) or 0.0) > 0 for key in ("price", "open", "high", "low", "volume"))
        live_quote_ok = bool(price and price > 0 and broker_quote and quote_has_ohlcv)
        if not price or price <= 0:
            block("invalid_quote_price", quote)
        evaluated_gate("live_quote", live_quote_ok, {"source": quote_source, "has_ohlcv": quote_has_ohlcv})

        freshness = data_ready.get("fresh_market_data_gate") if isinstance(data_ready.get("fresh_market_data_gate"), dict) else {}
        freshness_reason = str(freshness.get("reason") or "").lower()
        freshness_failed = freshness and freshness.get("passed") is False
        if freshness_failed and any(token in freshness_reason for token in ("stale_quote", "prior_session", "previous_session", "quote")):
            block("stale_or_delayed_quote", freshness)
        if data_ready.get("trade_decision_ready") is not True and not live_quote_ok:
            block("fresh_trade_data_missing", data_ready or None)
        evaluated_gate(
            "freshness",
            not any(item["reason"] in {"stale_or_delayed_quote", "fresh_trade_data_missing"} for item in blockers),
            {"data_readiness": data_ready, "live_quote_ok": live_quote_ok},
        )

        data_quality = scan.get("data_quality") if isinstance(scan.get("data_quality"), dict) else {}
        missing = {
            str(item or "").strip().lower()
            for item in data_quality.get("missing") or []
            if str(item or "").strip()
        }
        if any(token in label for label in missing for token in ("stale_quote", "prior_session", "previous_session")):
            block("opportunity_scan_quote_stale", sorted(missing))

        setup = str(scan.get("setup") or "").strip().lower()
        allowed_setups = {
            "opening_ignition",
            "intraday_momentum",
            "breakout_continuation",
            "near_breakout",
            "52_week_high_volume_breakout",
            "broker_re_rating_breakout",
            "earnings_beat_gap_and_go",
            "market_action_momentum",
            "top_gainer_momentum",
            "price_shocker_reversal_breakout",
        }
        wait_reason = _opportunity_scan_wait_reason(scan)
        if setup not in allowed_setups:
            block("setup_not_fresh_authority_allowed", setup or None)
        if wait_reason:
            block("setup_wait_only", wait_reason)
        evaluated_gate("setup", setup in allowed_setups and not wait_reason, {"setup": setup, "wait_reason": wait_reason})

        components = scan.get("components") if isinstance(scan.get("components"), dict) else {}
        scan_score = _float_or_none(scan.get("score")) or 0.0
        live_score = _float_or_none(components.get("live_momentum")) or scan_score
        day_gain = _float_or_none(scan.get("day_gain_pct")) or 0.0
        range_position = _float_or_none(scan.get("day_range_position")) or 0.0
        high_distance = _float_or_none(scan.get("day_high_distance_pct"))
        volume_ratio = _float_or_none(scan.get("volume_ratio")) or 0.0
        projected_volume_ratio = _float_or_none(scan.get("projected_volume_ratio")) or volume_ratio
        turnover = _float_or_none(scan.get("turnover")) or 0.0
        projected_turnover = _float_or_none(scan.get("projected_turnover")) or turnover
        min_gain, max_gain = (1.0, 7.5) if market_region == "US" else (1.2, 6.8)
        if day_gain < min_gain or day_gain >= max_gain:
            block("day_gain_outside_fresh_range", {"day_gain_pct": day_gain, "min": min_gain, "max_exclusive": max_gain})
        if range_position < 0.62:
            block("not_holding_upper_day_range", range_position)
        if high_distance is not None and high_distance > 3.0:
            block("too_far_from_day_high", high_distance)
        min_turnover = max(
            float(
                getattr(
                    self.settings,
                    "dynamic_scan_min_turnover_usd" if market_region == "US" else "dynamic_scan_min_turnover_inr",
                    2_000_000 if market_region == "US" else 50_000_000,
                )
                or (2_000_000 if market_region == "US" else 50_000_000)
            ),
            1.0,
        )
        turnover_floor = max(min_turnover * 3.0, 8_000_000.0) if market_region == "US" else max(min_turnover * 2.0, 100_000_000.0)
        volume_confirmed = (
            volume_ratio >= 1.15
            or projected_volume_ratio >= 1.8
            or turnover >= turnover_floor
            or projected_turnover >= turnover_floor * 1.2
        )
        if not volume_confirmed:
            block(
                "volume_or_turnover_not_confirmed",
                {
                    "volume_ratio": volume_ratio,
                    "projected_volume_ratio": projected_volume_ratio,
                    "turnover": turnover,
                    "projected_turnover": projected_turnover,
                    "turnover_floor": turnover_floor,
                },
            )
        evaluated_gate(
            "live_price_volume",
            day_gain >= min_gain
            and day_gain < max_gain
            and range_position >= 0.62
            and (high_distance is None or high_distance <= 3.0)
            and volume_confirmed,
            {
                "day_gain_pct": day_gain,
                "day_range_position": range_position,
                "day_high_distance_pct": high_distance,
                "volume_ratio": volume_ratio,
                "projected_volume_ratio": projected_volume_ratio,
                "turnover": turnover,
            },
        )

        severe_risk_flags = self._fresh_authority_severe_risk_flags(risk.get("flags") if isinstance(risk.get("flags"), list) else [])
        if severe_risk_flags:
            block("severe_risk_flags", severe_risk_flags)
        evaluated_gate("severe_risk_flags", not severe_risk_flags, severe_risk_flags)

        if price and price > 0:
            quote_low = _float_or_none(quote.get("low"))
            raw_stop = price * 0.965
            if quote_low and quote_low > 0 and quote_low < price:
                raw_stop = max(min(quote_low * 0.995, price * 0.985), price * 0.94)
            risk_per_share = price - raw_stop
            target = price + risk_per_share * 1.8
            trade_plan = {
                "entry_zone": [round(price * 0.995, 4), round(price * 1.005, 4)],
                "stop_loss": round(raw_stop, 4),
                "targets": [{"label": "FRESH-T1", "price": round(target, 4), "distance_pct": round(((target - price) / price) * 100.0, 4)}],
                "holding_period": "fresh_intraday_to_swing",
                "source": "fresh_authority_v1",
            }
        else:
            trade_plan = {}
        if not trade_plan:
            block("trade_plan_missing", None)

        fresh_score = min(
            99.0,
            60.0
            + max(min(scan_score, 1.0), 0.0) * 20.0
            + max(min(live_score, 1.0), 0.0) * 8.0
            + min(max(volume_ratio, projected_volume_ratio), 3.0) * 2.0
            + max(min(range_position, 1.0), 0.0) * 5.0
            + (3.0 if high_distance is None or high_distance <= 1.5 else 1.0),
        )
        if fresh_score < 84.0:
            block("fresh_score_below_84", round(fresh_score, 4))
        fresh_grade = "A" if fresh_score >= 90.0 else "B" if fresh_score >= 84.0 else "WATCH"
        fresh_confluence = min(26.0, 12.0 + max(min(scan_score, 1.0), 0.0) * 4.0 + max(min(live_score, 1.0), 0.0) * 4.0 + (3.0 if volume_confirmed else 0.0) + max(min(range_position, 1.0), 0.0))
        if fresh_confluence < 18.0:
            block("fresh_confluence_below_18", round(fresh_confluence, 4))
        evaluated_gate("fresh_score", fresh_score >= 84.0 and fresh_confluence >= 18.0, {"fresh_score": fresh_score, "fresh_confluence": fresh_confluence, "fresh_grade": fresh_grade})

        system_gate_audit = {
            "hard_blocked": bool(severe_risk_flags or any(item["reason"] in {"invalid_quote_price", "stale_or_delayed_quote", "fresh_trade_data_missing", "opportunity_scan_quote_stale"} for item in blockers)),
            "hard_blocks": [
                {"flag": str(item["reason"]).upper(), "reason": item["reason"], "value": item.get("value")}
                for item in blockers
                if item["reason"] in {"invalid_quote_price", "stale_or_delayed_quote", "fresh_trade_data_missing", "opportunity_scan_quote_stale", "severe_risk_flags"}
            ],
            "soft_flags": [],
            "active_flags": [str(item["reason"]).upper() for item in blockers],
            "overall_score_pct": round(fresh_score, 4),
            "overall_grade": fresh_grade,
            "classification": "FRESH_AUTHORITY_BUY" if not blockers else "FRESH_AUTHORITY_HOLD",
            "allocation_cap_multiplier": 1.0,
        }
        return {
            "passed": not blockers,
            "mode": "fresh_authority_v1",
            "reason": "fresh_authority_ready" if not blockers else blockers[0]["reason"],
            "primary_blocker": blockers[0]["reason"] if blockers else None,
            "secondary_blockers": blockers[1:],
            "setup": setup,
            "market_region": market_region or None,
            "fresh_score": round(fresh_score, 4),
            "fresh_grade": fresh_grade,
            "fresh_confluence": round(fresh_confluence, 4),
            "risk_flags": severe_risk_flags,
            "trade_plan": trade_plan,
            "system_gate_audit": system_gate_audit,
            "evaluated_gates": evaluated,
            "checks": {
                "source": "fresh_authority_v1",
                "setup": setup,
                "scan_score": round(scan_score, 4),
                "live_momentum_score": round(live_score, 4),
                "day_gain_pct": round(day_gain, 4),
                "day_range_position": round(range_position, 4),
                "day_high_distance_pct": round(high_distance, 4) if high_distance is not None else None,
                "volume_ratio": round(volume_ratio, 4),
                "projected_volume_ratio": round(projected_volume_ratio, 4),
                "turnover": round(turnover, 2),
                "projected_turnover": round(projected_turnover, 2),
                "live_quote_ok": live_quote_ok,
                "trade_decision_ready": data_ready.get("trade_decision_ready"),
            },
            "blockers": blockers,
        }

    def _fresh_authority_severe_risk_flags(self, flags: list[Any]) -> list[str]:
        severe_tokens = (
            "asm",
            "gsm",
            "fno_ban",
            "illiquid",
            "operator",
            "surveillance",
            "price_mismatch",
            "negative_catalyst",
            "corporate_event_risk",
            "climax_top",
            "delivery_distribution",
            "untradeable",
        )
        severe: list[str] = []
        for flag in flags:
            normalized = str(flag or "").strip().lower()
            if normalized and any(token in normalized for token in severe_tokens):
                severe.append(normalized)
        return list(dict.fromkeys(severe))

    def _opportunity_probe_profile(self, context: dict[str, Any]) -> dict[str, Any]:
        if _decision_authority_reset_enabled(self.settings):
            return {
                "ready": False,
                "reason": "decision_authority_reset_disables_opportunity_probe",
                "source": None,
            }
        scan = context.get("opportunity_scan") if isinstance(context.get("opportunity_scan"), dict) else {}
        full = context.get("full_spectrum_analysis") if isinstance(context.get("full_spectrum_analysis"), dict) else {}
        review = full.get("live_momentum_review") if isinstance(full.get("live_momentum_review"), dict) else {}
        setup = str(scan.get("setup") or review.get("setup") or "").strip().lower()
        data_quality = scan.get("data_quality") if isinstance(scan.get("data_quality"), dict) else {}
        wait_reason = _opportunity_scan_wait_reason(scan)
        if wait_reason:
            return {"ready": False, "reason": wait_reason, "setup": setup}
        if setup in {"extended_momentum_watch", "pre_rally_fuel", "circuit_demand_lock"}:
            return {"ready": False, "reason": "opportunity_scan_wait_state", "setup": setup}
        if setup == "btst_buy_candidate":
            btst = scan.get("btst") if isinstance(scan.get("btst"), dict) else {}
            data_quality = scan.get("data_quality") if isinstance(scan.get("data_quality"), dict) else {}
            scan_score = _float_or_none(scan.get("score")) or 0.0
            btst_score = _float_or_none(btst.get("score")) or scan_score
            data_quality_override = self._btst_reference_data_ready(context, data_quality)
            ready = (
                bool(btst.get("detected"))
                and str(scan.get("bucket") or "").strip().lower() == "actionable"
                and btst_score >= 0.70
                and (data_quality.get("actionable_data_ready") is not False or data_quality_override)
            )
            return {
                "ready": ready,
                "reason": "btst_buy_candidate_ready" if ready else "btst_buy_candidate_not_ready",
                "setup": setup,
                "source": "btst_buy_candidate" if ready else None,
                "scan_score": round(scan_score, 4),
                "btst_score": round(btst_score, 4),
                "combined_floor": 0.18,
                "min_quality_score": 70.0,
                "min_confluence": 16.0,
                "size_policy": "btst_guarded_buy",
                "data_quality_override": "phase2_fresh_reference_data" if data_quality_override else None,
                "data_quality_missing": [
                    str(item or "").strip().lower()
                    for item in data_quality.get("missing") or []
                    if str(item or "").strip()
                ],
                "btst": btst,
            }
        playbook_profile = self._top_gainers_playbook_profile(context, scan)
        if playbook_profile.get("ready"):
            return playbook_profile
        if playbook_profile.get("reason") != "no_top_gainers_playbook_buy":
            return playbook_profile

        review_ready = bool(
            review.get("strategy_ready")
            or review.get("early_ignition_ready")
            or review.get("live_momentum_ready")
            or review.get("market_action_breakout_ready")
        )
        bucket = str(scan.get("bucket") or "").strip().lower()
        scan_score = _float_or_none(scan.get("score")) or 0.0
        allowed_scan_setup = setup in {
            "opening_ignition",
            "intraday_momentum",
            "btst_buy_candidate",
            "breakout_continuation",
            "near_breakout",
            "news_catalyst",
            "52_week_high_volume_breakout",
            "broker_re_rating_breakout",
            "earnings_beat_gap_and_go",
            "market_action_momentum",
            "top_gainer_momentum",
            "price_shocker_reversal_breakout",
        }
        live_quote_probe_ok = self._live_quote_probe_data_ok(context, scan, setup)
        data_ready = context.get("data_readiness") if isinstance(context.get("data_readiness"), dict) else {}
        live_review_probe_ok = review_ready and data_ready.get("trade_decision_ready") is True
        if (
            data_quality.get("actionable_data_ready") is False
            and not data_quality.get("probe_only")
            and not live_quote_probe_ok
            and not live_review_probe_ok
        ):
            return {"ready": False, "reason": "opportunity_scan_data_not_actionable", "setup": setup}
        scan_ready = allowed_scan_setup and bucket == "actionable" and (
            scan_score >= 0.60 or bool(data_quality.get("actionable_data_ready")) or live_quote_probe_ok
        )
        ready = review_ready or scan_ready
        source = (
            "live_momentum_review"
            if review_ready
            else "live_quote_opportunity_scan"
            if scan_ready and live_quote_probe_ok
            else "opportunity_scan"
            if scan_ready
            else None
        )
        return {
            "ready": ready,
            "reason": "opportunity_price_volume_probe_ready" if ready else "no_opportunity_probe",
            "setup": setup,
            "source": source,
            "scan_score": round(scan_score, 4),
            "combined_floor": 0.20,
            "min_quality_score": OPPORTUNITY_PROBE_MIN_SCORE,
            "min_confluence": self._opportunity_probe_min_confluence(source, setup, scan_score),
            "size_policy": "probe_size_only",
            "data_quality_override": (
                "live_momentum_review_with_trade_ready_data"
                if live_review_probe_ok and data_quality.get("actionable_data_ready") is False
                else "live_quote_ohlcv_used_for_probe"
                if scan_ready and live_quote_probe_ok
                else None
            ),
            "data_quality_missing": [
                str(item or "").strip().lower()
                for item in data_quality.get("missing") or []
                if str(item or "").strip()
            ],
        }

    def _top_gainers_playbook_profile(self, context: dict[str, Any], scan: dict[str, Any]) -> dict[str, Any]:
        playbook = scan.get("top_gainers_playbook") if isinstance(scan.get("top_gainers_playbook"), dict) else {}
        signal = str(playbook.get("final_signal") or "").upper()
        if signal not in {"STRONG BUY", "MODERATE BUY"}:
            return {"ready": False, "reason": "no_top_gainers_playbook_buy"}
        market_region = str(playbook.get("market_region") or context.get("market_region") or "").upper()
        if playbook.get("hard_excluded") or playbook.get("hard_excludes") or str(playbook.get("tier") or "").upper() == "HARD EXCLUDE":
            return {"ready": False, "reason": "top_gainers_playbook_hard_excluded"}
        anti_codes = {
            str(flag.get("code") or "").upper()
            for flag in playbook.get("anti_patterns") or []
            if isinstance(flag, dict)
        }
        hard_anti = {"CHASING", "OPERATOR_RISK", "SHORT_COVER", "STAGE_TRAP", "ILLIQUID_BREAKOUT", "FAILED_BREAKOUT_RISK"}
        if anti_codes & hard_anti:
            return {"ready": False, "reason": "top_gainers_playbook_antipattern_block", "anti_patterns": sorted(anti_codes)}
        catalyst = playbook.get("catalyst_review") if isinstance(playbook.get("catalyst_review"), dict) else {}
        if catalyst.get("catalyst_confirmed") is not True:
            return {"ready": False, "reason": "top_gainers_playbook_catalyst_unconfirmed"}
        weinstein = playbook.get("weinstein") if isinstance(playbook.get("weinstein"), dict) else {}
        playbook_stage = str(weinstein.get("stage") or "").strip()
        if playbook_stage in {"Stage 3", "Stage 4"}:
            return {"ready": False, "reason": "top_gainers_playbook_stage_trap", "stage": playbook_stage}
        levels = playbook.get("levels") if isinstance(playbook.get("levels"), dict) else {}
        entry = _float_or_none(levels.get("entry"))
        max_entry = _float_or_none(levels.get("max_entry"))
        stop = _float_or_none(levels.get("stop"))
        price = _float_or_none((context.get("quote") or {}).get("price")) or _float_or_none(playbook.get("cmp")) or entry
        if entry is None or max_entry is None or stop is None or price is None:
            return {"ready": False, "reason": "top_gainers_playbook_missing_trade_levels"}
        if price > max_entry * 1.0005:
            return {
                "ready": False,
                "reason": "top_gainers_playbook_price_above_max_entry",
                "price": round(price, 4),
                "max_entry": round(max_entry, 4),
            }
        stop_risk_pct = ((price - stop) / price) * 100.0 if stop < price else 100.0
        if stop >= price or stop_risk_pct > 9.0:
            return {
                "ready": False,
                "reason": "top_gainers_playbook_stop_risk_too_wide",
                "price": round(price, 4),
                "stop": round(stop, 4),
                "stop_risk_pct": round(stop_risk_pct, 4),
            }
        quant_score = _float_or_none(playbook.get("quant_score")) or 0.0
        minimum = 70.0
        if quant_score < minimum:
            return {"ready": False, "reason": "top_gainers_playbook_quant_below_signal_floor", "quant_score": quant_score}
        quote = context.get("quote") if isinstance(context.get("quote"), dict) else {}
        data_ready = context.get("data_readiness") if isinstance(context.get("data_readiness"), dict) else {}
        sources = data_ready.get("sources") if isinstance(data_ready.get("sources"), dict) else {}
        quote_source = str(quote.get("source") or sources.get("quote") or "").lower()
        broker_or_live_source = any(token in quote_source for token in ("upstox", "kite", "nubra", "polygon", "alpaca", "ibkr"))
        us_yahoo_reference = market_region == "US" and "yahoo" in quote_source
        data_quality_override = None
        if data_ready.get("trade_decision_ready") is not True and us_yahoo_reference:
            if not self._top_gainers_playbook_us_reference_data_ok(playbook, data_ready):
                return {"ready": False, "reason": "top_gainers_playbook_us_reference_data_missing"}
            data_quality_override = "us_yahoo_reference_reduced_size"
        elif data_ready.get("trade_decision_ready") is not True and not broker_or_live_source:
            return {"ready": False, "reason": "top_gainers_playbook_needs_live_quote_or_trade_ready_data"}
        return {
            "ready": True,
            "reason": "top_gainers_playbook_buy_ready",
            "market_region": market_region,
            "setup": str(scan.get("setup") or "").strip().lower(),
            "source": "top_gainers_playbook",
            "final_signal": signal,
            "scan_score": round(quant_score / 100.0, 4),
            "playbook_quant_score": round(quant_score, 4),
            "combined_floor": -1.0,
            "min_quality_score": minimum,
            "min_confluence": 0.0,
            "size_policy": "probe_size_only" if signal == "MODERATE BUY" else "reduced_size_until_follow_through",
            "playbook_entry_zone_valid": True,
            "entry": round(entry, 4),
            "max_entry": round(max_entry, 4),
            "stop": round(stop, 4),
            "stop_risk_pct": round(stop_risk_pct, 4),
            "playbook_stage": playbook_stage,
            "playbook_stage_buy_allowed": self._top_gainers_playbook_stage_allows_entry(playbook_stage),
            "data_quality_override": data_quality_override,
            "data_quality_missing": [
                str(item or "").strip().lower()
                for item in (playbook.get("data_gaps") or [])
                if str(item or "").strip()
            ],
        }

    def _top_gainers_playbook_stage_allows_entry(self, stage: str) -> bool:
        normalized = str(stage or "").strip()
        return normalized in {"Stage 1", "Stage 2", ""}

    def _top_gainers_playbook_stage_can_override_alignment(self, profile: dict[str, Any]) -> bool:
        return (
            profile.get("source") == "top_gainers_playbook"
            and profile.get("playbook_entry_zone_valid")
            and str(profile.get("playbook_stage") or "") == "Stage 2"
        )

    def _top_gainers_playbook_stage_can_override_legacy_stage_gate(self, profile: dict[str, Any]) -> bool:
        return (
            profile.get("source") == "top_gainers_playbook"
            and profile.get("playbook_entry_zone_valid")
            and bool(profile.get("playbook_stage_buy_allowed"))
        )

    def _top_gainers_playbook_us_reference_data_ok(self, playbook: dict[str, Any], data_ready: dict[str, Any]) -> bool:
        hard_gap_keys = {
            str(gap.get("key") or "").strip().lower()
            for gap in data_ready.get("hard_gaps") or []
            if isinstance(gap, dict) and str(gap.get("key") or "").strip()
        }
        allowed_reference_gaps = {"us_realtime_quote", "us_minute_bars", "us_sec_filings"}
        if hard_gap_keys and not hard_gap_keys <= allowed_reference_gaps:
            return False
        required_values = (
            playbook.get("cmp"),
            playbook.get("volume"),
            playbook.get("volume_ratio"),
            playbook.get("quant_score"),
        )
        return all(_float_or_none(value) is not None for value in required_values)

    def _rule_audit_with_playbook_overrides(self, rule_audit: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
        if not profile.get("playbook_entry_zone_valid"):
            return rule_audit
        output = dict(rule_audit or {})
        hard_blocks = [
            block
            for block in output.get("hard_blocks") or []
            if not (isinstance(block, dict) and str(block.get("flag") or "").upper() == "PRICE_EXTENDED_FROM_PIVOT")
        ]
        active_flags = [
            flag
            for flag in output.get("active_flags") or []
            if str(flag or "").upper() != "PRICE_EXTENDED_FROM_PIVOT"
        ]
        output["hard_blocks"] = hard_blocks
        output["active_flags"] = active_flags
        output["hard_blocked"] = bool(hard_blocks)
        playbook_score = _float_or_none(profile.get("playbook_quant_score"))
        current_score = _float_or_none(output.get("overall_score_pct"))
        if playbook_score is not None and playbook_score > float(current_score or 0.0):
            output["overall_score_pct"] = playbook_score
            output["overall_grade"] = _score_grade(playbook_score)
        output["playbook_override"] = {
            "source": "top_gainers_playbook",
            "final_signal": profile.get("final_signal"),
            "entry_zone_valid": True,
            "reason": "Moneycontrol top-gainers playbook validated price within max entry and 7% stop risk.",
        }
        return output

    def _opportunity_probe_publishable_rule_audit(
        self,
        rule_audit: dict[str, Any],
        profile: dict[str, Any],
        absorbed_gates: list[dict[str, Any]],
    ) -> dict[str, Any]:
        output = dict(rule_audit or {})
        absorbed_flags = self._absorbed_system_rule_flags(absorbed_gates)
        if absorbed_flags:
            absorbed_blocks: list[dict[str, Any]] = []
            hard_blocks: list[dict[str, Any]] = []
            for block in output.get("hard_blocks") or []:
                flag = str(block.get("flag") or "").strip().upper() if isinstance(block, dict) else ""
                if flag in absorbed_flags:
                    absorbed_blocks.append(block)
                else:
                    hard_blocks.append(block)
            output["hard_blocks"] = hard_blocks
            output["hard_blocked"] = bool(hard_blocks)
            output["active_flags"] = [
                flag
                for flag in output.get("active_flags") or []
                if str(flag or "").strip().upper() not in absorbed_flags
            ]
            output["opportunity_probe_absorbed_hard_blocks"] = absorbed_blocks

        scan_score_pct = self._opportunity_probe_score_pct(profile)
        minimum = _float_or_none(profile.get("min_quality_score")) or OPPORTUNITY_PROBE_MIN_SCORE
        if scan_score_pct is not None and scan_score_pct >= minimum:
            current_score = _float_or_none(output.get("overall_score_pct")) or 0.0
            if scan_score_pct > current_score:
                output["overall_score_pct"] = round(scan_score_pct, 4)
                output["overall_grade"] = _score_grade(scan_score_pct)
            entry = dict(output.get("entry") or {})
            if str(entry.get("effective_entry_grade") or "").upper() == "WATCH":
                entry["effective_entry_grade"] = "C"
                output["entry"] = entry
            allocation_cap = _float_or_none(output.get("allocation_cap_multiplier"))
            if allocation_cap is None or allocation_cap <= 0.0:
                output["allocation_cap_multiplier"] = 0.35

        output["opportunity_probe_override"] = {
            "source": profile.get("source"),
            "setup": profile.get("setup"),
            "scan_score": profile.get("scan_score"),
            "absorbed_gates": [gate.get("gate") for gate in absorbed_gates],
            "policy": "Scanner-qualified opportunity; absorbed legacy institutional gates only after live quote/data and hard-risk checks passed.",
        }
        return output

    def _absorbed_system_rule_flags(self, absorbed_gates: list[dict[str, Any]]) -> set[str]:
        flags: set[str] = set()
        prefix = "system_rule_"
        for gate in absorbed_gates:
            name = str(gate.get("gate") or "").strip()
            if name.startswith(prefix):
                flags.add(name[len(prefix) :].upper())
        return flags

    def _opportunity_probe_score_pct(self, profile: dict[str, Any]) -> float | None:
        score = _float_or_none(profile.get("playbook_quant_score"))
        if score is not None:
            return score
        score = _float_or_none(profile.get("scan_score"))
        if score is None:
            return None
        return score * 100.0 if score <= 1.0 else score

    def _opportunity_probe_min_confluence(self, source: Any, setup: str, scan_score: float) -> float:
        normalized_source = str(source or "").strip()
        normalized_setup = str(setup or "").strip().lower()
        score = float(scan_score or 0.0)
        if normalized_source in {"live_momentum_review", "live_quote_opportunity_scan"}:
            live_setups = {
                "opening_ignition",
                "intraday_momentum",
                "top_gainer_momentum",
                "market_action_momentum",
                "price_shocker_reversal_breakout",
            }
            breakout_setups = {
                "52_week_high_volume_breakout",
                "breakout_continuation",
                "near_breakout",
                "broker_re_rating_breakout",
                "earnings_beat_gap_and_go",
            }
            if normalized_setup in live_setups:
                return 6.0 if score >= 0.85 else 10.0
            if normalized_setup in breakout_setups:
                return 10.0 if score >= 0.82 else 12.0
            return 12.0
        if normalized_source == "opportunity_scan":
            return 12.0
        return 16.0

    def _opportunity_probe_can_absorb_gate(self, gate: dict[str, Any], profile: dict[str, Any]) -> bool:
        if not profile.get("ready"):
            return False
        gate_name = str(gate.get("gate") or "").strip()
        reason = str(gate.get("reason") or "").strip()
        value = gate.get("value")
        if (
            profile.get("source") == "top_gainers_playbook"
            and profile.get("playbook_entry_zone_valid")
            and "PRICE_EXTENDED_FROM_PIVOT" in gate_name.upper()
        ):
            return True
        if gate_name == "overall_quality_gate":
            score = self._gate_overall_score(value)
            minimum = max(_float_or_none(profile.get("min_quality_score")) or OPPORTUNITY_PROBE_MIN_SCORE, FRESH_BUY_MIN_SCORE)
            playbook_score = _float_or_none(profile.get("playbook_quant_score"))
            if playbook_score is not None and playbook_score >= minimum:
                return True
            scan_score_pct = self._opportunity_probe_score_pct(profile)
            if scan_score_pct is not None and scan_score_pct >= max(minimum, 80.0):
                return True
            if score is not None and score >= minimum:
                return True
            return False
        if gate_name == "entry_grade_gate":
            return self._opportunity_probe_can_absorb_entry_grade(gate.get("value"), reason, profile)
        if gate_name == "system_rule_GRADE_VIOLATION":
            return self._opportunity_probe_can_absorb_entry_grade(gate.get("value"), reason, profile)
        if gate_name == "fresh_market_data_gate":
            return self._opportunity_probe_can_absorb_fresh_market_data_gate(gate.get("value"), reason, profile)
        if gate_name in {"system_rule_MTF_HARD_BLOCK", "timeframe_alignment_gate"}:
            return self._top_gainers_playbook_stage_can_override_alignment(profile)
        if gate_name == "session_momentum_gate":
            if reason == "late_intraday_momentum_wait_for_pullback" or self._gate_value_flag(value, "late_chase"):
                if profile.get("source") == "top_gainers_playbook" and profile.get("playbook_entry_zone_valid"):
                    return True
                return False
            return True
        absorbable = {
            "fundamental_confirmation_gate",
        }
        if gate_name in absorbable:
            return True
        if gate_name == "system_rule_DATA_READINESS_BLOCK":
            return self._opportunity_probe_can_absorb_data_readiness_block(gate.get("value"), profile)
        if gate_name == "phase2_data_readiness":
            return self._opportunity_probe_can_absorb_data_readiness_block(gate.get("value"), profile)
        if gate_name == "stage_buy_permitted":
            if profile.get("source") == "top_gainers_playbook":
                return self._top_gainers_playbook_stage_can_override_legacy_stage_gate(profile)
            return profile.get("source") in {
                "live_momentum_review",
                "opportunity_scan",
                "live_quote_opportunity_scan",
            }
        if gate_name == "risk_overrides":
            return self._opportunity_probe_can_absorb_risk_flags(gate.get("value"), profile)
        if reason == "stage_analysis_not_stage2_markup" and profile.get("source") == "top_gainers_playbook":
            return self._top_gainers_playbook_stage_can_override_legacy_stage_gate(profile)
        if reason in {
            "overall_score_below_70_no_new_longs",
            "fundamentals_unknown_needs_news_or_delivery_confirmation",
            "broad_momentum_entry_needs_current_session_confirmation",
        }:
            return True
        return False

    def _opportunity_probe_can_absorb_entry_grade(self, value: Any, reason: str, profile: dict[str, Any]) -> bool:
        if profile.get("source") == "top_gainers_playbook" and profile.get("playbook_entry_zone_valid"):
            return True
        if reason == "extended_entry_no_new_longs":
            return False
        if self._gate_value_flag(value, "late_chase"):
            return False
        if isinstance(value, str) and value.strip().upper() == "D":
            return False
        if isinstance(value, dict):
            entry_grade = str(value.get("entry_grade") or "").strip().upper()
            effective = str(value.get("effective_entry_grade") or "").strip().upper()
            if "D" in {entry_grade, effective}:
                return False
        scan_score_pct = self._opportunity_probe_score_pct(profile)
        return bool(scan_score_pct is not None and scan_score_pct >= 80.0)

    def _gate_overall_score(self, value: Any) -> float | None:
        if isinstance(value, dict):
            return _float_or_none(value.get("overall_score_pct"))
        if isinstance(value, str):
            try:
                parsed = ast.literal_eval(value)
            except (SyntaxError, ValueError):
                parsed = None
            if isinstance(parsed, dict):
                return _float_or_none(parsed.get("overall_score_pct"))
        return None

    def _gate_value_flag(self, value: Any, key: str) -> bool:
        if isinstance(value, dict):
            return bool(value.get(key))
        if isinstance(value, str):
            try:
                parsed = ast.literal_eval(value)
            except (SyntaxError, ValueError):
                parsed = None
            if isinstance(parsed, dict):
                return bool(parsed.get(key))
        return False

    def _opportunity_probe_can_absorb_fresh_market_data_gate(
        self, value: Any, reason: str, profile: dict[str, Any]
    ) -> bool:
        override = str(profile.get("data_quality_override") or "")
        if override not in {"live_quote_ohlcv_used_for_probe", "live_momentum_review_with_trade_ready_data"}:
            return False
        if reason not in {"stale_market_data", "data_stale_watch"}:
            return False
        missing = {
            str(item or "").strip().lower()
            for item in profile.get("data_quality_missing") or []
            if str(item or "").strip()
        }
        if missing and missing - {"stale_intraday_candles"}:
            return False
        labels = self._fresh_market_gate_labels(value)
        if labels and not self._labels_only_stale_intraday_candles(labels):
            return False
        if isinstance(value, dict):
            gate = value.get("fresh_market_data_gate") if isinstance(value.get("fresh_market_data_gate"), dict) else {}
            if gate.get("passed") is False and not labels:
                return False
        return True

    def _fresh_market_gate_labels(self, value: Any) -> set[str]:
        labels: set[str] = set()
        if not isinstance(value, dict):
            return labels
        for field in ("key", "label", "reason"):
            label = str(value.get(field) or "").strip().lower()
            if label:
                labels.add(label)
        for field in ("missing_data", "hard_gaps", "soft_gaps"):
            for item in value.get(field) or []:
                if isinstance(item, dict):
                    for nested_field in ("key", "label", "reason"):
                        label = str(item.get(nested_field) or "").strip().lower()
                        if label:
                            labels.add(label)
                else:
                    label = str(item or "").strip().lower()
                    if label:
                        labels.add(label)
        data_quality = value.get("data_quality") if isinstance(value.get("data_quality"), dict) else {}
        labels.update(str(item or "").strip().lower() for item in data_quality.get("missing") or [] if str(item or "").strip())
        return labels

    def _labels_only_stale_intraday_candles(self, labels: set[str]) -> bool:
        stale_quote_tokens = ("stale_quote", "quote_stale", "prior_session", "previous_session", "moneycontrol_prior")
        if any(token in label for label in labels for token in stale_quote_tokens):
            return False
        for label in labels:
            if "intraday" in label and ("candle" in label or "ohlcv" in label):
                continue
            if label in {"in_intraday_candles", "stale_intraday", "stale_intraday_candles"}:
                continue
            return False
        return True

    def _live_quote_probe_data_ok(self, context: dict[str, Any], scan: dict[str, Any], setup: str) -> bool:
        data_quality = scan.get("data_quality") if isinstance(scan.get("data_quality"), dict) else {}
        missing = {str(item or "").strip().lower() for item in data_quality.get("missing") or [] if str(item or "").strip()}
        if missing and missing - {"stale_intraday_candles"}:
            return False
        if setup not in {
            "opening_ignition",
            "intraday_momentum",
            "breakout_continuation",
            "near_breakout",
            "news_catalyst",
            "52_week_high_volume_breakout",
            "broker_re_rating_breakout",
            "earnings_beat_gap_and_go",
            "market_action_momentum",
            "top_gainer_momentum",
            "price_shocker_reversal_breakout",
        }:
            return False
        quote = context.get("quote") if isinstance(context.get("quote"), dict) else {}
        source = str(quote.get("source") or data_quality.get("quote_source") or "").lower()
        if not any(token in source for token in ("upstox", "kite", "nubra")):
            return False
        has_live_ohlcv = all((_float_or_none(quote.get(key)) or 0.0) > 0 for key in ("price", "open", "high", "low", "volume"))
        turnover = _float_or_none(scan.get("turnover")) or 0.0
        projected_turnover = _float_or_none(scan.get("projected_turnover")) or 0.0
        return has_live_ohlcv and (turnover >= 50_000_000 or projected_turnover >= 150_000_000)

    def _opportunity_probe_can_absorb_risk_flags(self, value: Any, profile: dict[str, Any]) -> bool:
        flags = _risk_flag_list(value)
        if not flags:
            return False
        hard_tokens = (
            "asm_surveillance",
            "delivery_conflict",
            "distribution",
            "mtf",
            "timeframe_conflict",
            "stop_hit",
            "climax",
            "earnings_lockout",
            "corporate_event_risk",
            "circuit",
            "price_extended_from_pivot",
            "hard_block",
        )
        for flag in flags:
            normalized = str(flag or "").strip().lower()
            if (
                profile.get("source") == "top_gainers_playbook"
                and profile.get("playbook_entry_zone_valid")
                and "price_extended_from_pivot" in normalized
            ):
                continue
            if (
                profile.get("source") == "top_gainers_playbook"
                and profile.get("playbook_entry_zone_valid")
                and profile.get("market_region") == "US"
                and (
                    "possible_circuit" in normalized
                    or "extreme_atr_volatility" in normalized
                )
            ):
                continue
            if any(token in normalized for token in hard_tokens):
                return False
        return True

    def _opportunity_probe_can_absorb_data_readiness_block(self, value: Any, profile: dict[str, Any]) -> bool:
        if profile.get("data_quality_override") == "us_yahoo_reference_reduced_size":
            hard_gap_keys: set[str] = set()
            if isinstance(value, dict) and "hard_gaps" in value:
                hard_gap_keys = {
                    str(gap.get("key") or "").strip().lower()
                    for gap in value.get("hard_gaps") or []
                    if isinstance(gap, dict) and str(gap.get("key") or "").strip()
                }
            elif isinstance(value, dict):
                key = str(value.get("key") or "").strip().lower()
                if key:
                    hard_gap_keys = {key}
            allowed_reference_gaps = {"us_realtime_quote", "us_minute_bars", "us_sec_filings"}
            return bool(hard_gap_keys) and hard_gap_keys <= allowed_reference_gaps
        if profile.get("data_quality_override") != "live_quote_ohlcv_used_for_probe":
            return False
        missing = {
            str(item or "").strip().lower()
            for item in profile.get("data_quality_missing") or []
            if str(item or "").strip()
        }
        if missing and missing - {"stale_intraday_candles"}:
            return False
        if isinstance(value, dict) and "hard_gaps" in value:
            hard_gaps = value.get("hard_gaps") or []
            keys = {
                str(gap.get("key") or "").strip().lower()
                for gap in hard_gaps
                if isinstance(gap, dict) and str(gap.get("key") or "").strip()
            }
            return bool(keys) and keys <= {"in_intraday_candles"}
        if isinstance(value, dict):
            key = str(value.get("key") or "").strip().lower()
            return key == "in_intraday_candles"
        return False

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
        monthly_expiry_day = bool(
            macro_event_context.get("is_monthly_expiry_day")
            or (macro_event_context.get("is_expiry_day") and macro_event_context.get("expiry_type") == "monthly")
        )
        monthly_expiry_eve = bool(macro_event_context.get("is_monthly_expiry_eve")) and not monthly_expiry_day
        monthly_expiry_block = monthly_expiry_day and not has_position
        if monthly_expiry_eve and not has_position:
            buy_threshold = max(buy_threshold, 0.40)
            macro_event_context["expiry_risk_policy"] = "probe_size_only"
            macro_event_context["expiry_size_multiplier"] = min(
                _float_or_none(macro_event_context.get("expiry_size_multiplier")) or 0.35,
                0.35,
            )
            macro_event_context["expiry_risk_reason"] = "monthly_expiry_eve_reduce_size"
        macro_failed = earnings_block or monthly_expiry_block
        macro_reason = (
            "earnings_lockout"
            if earnings_block
            else "monthly_expiry_no_new_longs"
            if monthly_expiry_block
            else None
        )
        gates.append(
            {
                "gate": "earnings_gate",
                "passed": not macro_failed,
                "value": {
                    "macro_event_context": macro_event_context,
                    "event_thesis": event_thesis,
                    "expiry_eve_policy": macro_event_context.get("expiry_risk_policy"),
                },
            }
        )
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
            combined = deterministic_score(context)
            item["sentiment_score"] = sentiment_score
            item["sentiment_detail"] = result
            item["context"] = context
            item["combined"] = combined
            item["score_breakdown"] = deterministic_score_breakdown(context)
            item["action"] = self._action_from_context(item["symbol"], combined, positions, context, candles_by_symbol)
            item["confidence"] = self._confidence_for_action(item["action"], combined, item.get("macro_event_context") or {}, market_breadth)

    def _scan_priority(self, item: dict[str, Any]) -> tuple[float, float, float, float, float, float, float, float]:
        opportunity_rank = self._opportunity_rank_score(item)
        opportunity_score = self._opportunity_priority_score(item)
        tomorrow_plan = (item.get("context") or {}).get("tomorrow_plan_decision")
        if not isinstance(tomorrow_plan, dict):
            tomorrow_plan = {}
        tomorrow_priority = float(tomorrow_plan.get("priority_boost") or 0.0)
        return (
            1.0 if item["action"] != "HOLD" else 0.0,
            opportunity_rank,
            tomorrow_priority,
            opportunity_score,
            abs(float(item["combined"])),
            abs(float(item["context"].get("best_strategy", {}).get("score", 0.0) or 0.0)),
            abs(float(item["technical"].score)),
            abs(float(item["sentiment_score"])),
        )

    def _scan_priority_score(self, item: dict[str, Any]) -> float:
        action_boost, opportunity_rank, tomorrow_priority, opportunity, combined, strategy, technical, sentiment = self._scan_priority(item)
        rs_percentile = float(((item.get("context") or {}).get("universe_relative_strength") or {}).get("percentile_63") or 50.0)
        rs_score = (rs_percentile - 50.0) / 50.0
        return (
            (action_boost * 0.35)
            + (opportunity_rank * 0.20)
            + (tomorrow_priority * 0.08)
            + (opportunity * 0.14)
            + (combined * 0.14)
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
        playbook = scan.get("top_gainers_playbook") if isinstance(scan.get("top_gainers_playbook"), dict) else {}
        if bucket == "actionable":
            score += 0.06
        if setup in {"news_catalyst", "breakout_continuation", "near_breakout"}:
            score += 0.03
        if data_quality.get("actionable_data_ready"):
            score += 0.03
        if playbook.get("final_signal") == "STRONG BUY":
            score += 0.08
        elif playbook.get("final_signal") == "MODERATE BUY":
            score += 0.05
        return max(min(score, 1.0), 0.0)

    def _tomorrow_plan_decision_context(
        self,
        context: dict[str, Any],
        opportunity_probe: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        plan = context.get("tomorrow_plan_context") if isinstance(context.get("tomorrow_plan_context"), dict) else {}
        if not plan or not plan.get("active"):
            return {"active": False}
        section = str(plan.get("section") or "").strip().lower()
        action = str(plan.get("action") or "").strip().upper()
        allowed_sections = {"ready_at_open", "near_breakout", "news_watch"}
        if section not in allowed_sections or action == "AVOID":
            return {
                "active": True,
                "section": section or None,
                "action": action or None,
                "eligible_for_entry_boost": False,
                "reason": "tomorrow_plan_section_not_entry_eligible",
            }

        full = context.get("full_spectrum_analysis") if isinstance(context.get("full_spectrum_analysis"), dict) else {}
        session_momentum = full.get("session_momentum") if isinstance(full.get("session_momentum"), dict) else {}
        live_momentum_review = full.get("live_momentum_review") if isinstance(full.get("live_momentum_review"), dict) else {}
        reasons: list[str] = []
        if opportunity_probe and opportunity_probe.get("ready"):
            reasons.append(str(opportunity_probe.get("source") or "opportunity_probe"))
        if session_momentum.get("confirmed"):
            reasons.append("session_momentum_confirmed")
        for key in ("strategy_ready", "early_ignition_ready", "live_momentum_ready", "market_action_breakout_ready"):
            if live_momentum_review.get(key):
                reasons.append(key)

        threshold_boost_by_section = {
            "ready_at_open": 0.05,
            "near_breakout": 0.04,
            "news_watch": 0.03,
        }
        priority_boost_by_section = {
            "ready_at_open": 0.10,
            "near_breakout": 0.08,
            "news_watch": 0.06,
        }
        eligible = bool(reasons)
        return {
            "active": True,
            "plan_date": plan.get("plan_date"),
            "section": section,
            "action": action or None,
            "strategy": plan.get("strategy"),
            "trigger_price": plan.get("trigger_price"),
            "max_entry": plan.get("max_entry"),
            "score": plan.get("score"),
            "confidence": plan.get("confidence"),
            "eligible_for_entry_boost": eligible,
            "live_confirmation": reasons,
            "threshold_boost": threshold_boost_by_section.get(section, 0.0) if eligible else 0.0,
            "priority_boost": priority_boost_by_section.get(section, 0.0),
            "reason": "live_confirmation_present" if eligible else "waiting_for_live_confirmation",
        }

    def _pattern_state(self, symbol: str) -> dict[str, Any]:
        db = getattr(self.sentiment, "db", None)
        if db is None or not hasattr(db, "get_pattern_state"):
            return {}
        state = db.get_pattern_state(symbol, "darvas_box", {}) or {}
        return {"darvas_box": state if isinstance(state, dict) else {}}

    def _pattern_states_for_cycle(self, symbols: list[str]) -> dict[str, dict[str, Any]]:
        db = getattr(self.sentiment, "db", None)
        if db is None or not hasattr(db, "pattern_states_for_symbols"):
            return {}
        try:
            states = db.pattern_states_for_symbols(symbols, "darvas_box")
        except Exception:
            return {}
        if not isinstance(states, dict):
            return {}
        result: dict[str, dict[str, Any]] = {}
        for symbol, state in states.items():
            normalized = str(symbol or "").upper()
            result[normalized] = {"darvas_box": state if isinstance(state, dict) else {}}
        return result

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

    def _apply_btst_strategy(self, context: dict[str, Any]) -> None:
        scan = context.get("opportunity_scan") if isinstance(context.get("opportunity_scan"), dict) else {}
        if str(scan.get("setup") or "").strip().lower() != "btst_buy_candidate":
            return
        btst = scan.get("btst") if isinstance(scan.get("btst"), dict) else {}
        if not btst.get("detected"):
            return
        full = context.get("full_spectrum_analysis") if isinstance(context.get("full_spectrum_analysis"), dict) else {}
        full["btst_review"] = btst
        if _decision_authority_reset_enabled(self.settings):
            reset_notes = full.setdefault("decision_authority_reset", {})
            reset_notes["btst_buy_candidate"] = {
                "status": "diagnostic_only",
                "reason": "reset_v2 disables BTST strategy mutation as BUY authority",
            }
            return
        btst_score = _float_or_none(btst.get("score")) or _float_or_none(scan.get("score")) or 0.0
        strategy = {
            "name": "btst_buy_candidate",
            "score": round(max(0.74, min(0.92, btst_score)), 3),
            "direction": "BUY",
            "confidence": round(min(0.88, 0.58 + btst_score * 0.30), 3),
            "notes": [
                "BTST candidate",
                "closing strength supports next-day follow-through",
                "overnight risk checks passed",
            ],
            "metadata": btst,
        }
        current = context.get("best_strategy") if isinstance(context.get("best_strategy"), dict) else {}
        if float(current.get("score") or 0.0) < strategy["score"] or str(current.get("name") or "") in {"", "no_actionable_strategy"}:
            context["best_strategy"] = strategy
            signals = context.get("strategy_signals")
            if isinstance(signals, list):
                signals.append(strategy)
        entry = full.get("entry_quality") if isinstance(full.get("entry_quality"), dict) else {}
        if entry:
            current_grade = str(entry.get("entry_grade") or "WATCH").upper()
            if current_grade in {"", "WATCH", "C"}:
                entry["entry_grade"] = "B"
                entry["setup_type"] = "btst_buy_candidate"
                entry["quality_score"] = max(float(entry.get("quality_score") or 0.0), 74.0)
            entry["volume_confirmation"] = True
            entry["btst_confirmation"] = btst
        trade_plan = full.get("trade_plan") if isinstance(full.get("trade_plan"), dict) else {}
        entry_zone = btst.get("entry_zone") if isinstance(btst.get("entry_zone"), dict) else {}
        low = _float_or_none(entry_zone.get("low"))
        high = _float_or_none(entry_zone.get("high") or btst.get("max_entry"))
        if low and high:
            trade_plan["entry_zone"] = [round(low, 2), round(high, 2)]
        stop = _float_or_none(btst.get("stop_loss"))
        target1 = _float_or_none(btst.get("target1"))
        if stop:
            trade_plan["stop_loss"] = round(stop, 2)
        if target1:
            trade_plan["targets"] = [{"label": "BTST-T1", "price": round(target1, 2)}]
        trade_plan["holding_period"] = "BTST"
        full["trade_plan"] = trade_plan

    def _btst_reference_data_ready(self, context: dict[str, Any], data_quality: dict[str, Any]) -> bool:
        data_ready = context.get("data_readiness") if isinstance(context.get("data_readiness"), dict) else {}
        market_region = str(data_ready.get("market_region") or context.get("market_region") or "").upper()
        if market_region != "US" or data_ready.get("trade_decision_ready") is not True:
            return False
        freshness = data_ready.get("fresh_market_data_gate") if isinstance(data_ready.get("fresh_market_data_gate"), dict) else {}
        if freshness.get("passed") is not True:
            return False
        missing = {
            str(item or "").strip().lower()
            for item in data_quality.get("missing") or []
            if str(item or "").strip()
        }
        return bool(missing) and missing <= {"us_realtime_intraday_for_actionable_trade"}

    def _apply_live_momentum_strategy(self, context: dict[str, Any]) -> None:
        full = context.get("full_spectrum_analysis") if isinstance(context.get("full_spectrum_analysis"), dict) else {}
        scan = context.get("opportunity_scan") if isinstance(context.get("opportunity_scan"), dict) else {}
        session = full.get("session_momentum") if isinstance(full.get("session_momentum"), dict) else {}
        components = scan.get("components") if isinstance(scan.get("components"), dict) else {}
        setup = str(scan.get("setup") or "")
        wait_reason = _opportunity_scan_wait_reason(scan)
        live_score = _float_or_none(components.get("live_momentum")) or 0.0
        day_gain = _float_or_none(scan.get("day_gain_pct") or session.get("day_gain_pct")) or 0.0
        range_position = _float_or_none(scan.get("day_range_position") or session.get("day_range_position")) or 0.0
        high_distance = _float_or_none(scan.get("day_high_distance_pct") or session.get("day_high_distance_pct"))
        volume_ratio = _float_or_none(scan.get("volume_ratio")) or 0.0
        projected_volume_ratio = _float_or_none(scan.get("projected_volume_ratio")) or volume_ratio
        turnover = _float_or_none(scan.get("turnover")) or 0.0
        projected_turnover = _float_or_none(scan.get("projected_turnover")) or turnover
        market_region = str(context.get("market_region") or scan.get("market_region") or "").upper()
        min_turnover = max(
            float(
                getattr(
                    self.settings,
                    "dynamic_scan_min_turnover_usd" if market_region == "US" else "dynamic_scan_min_turnover_inr",
                    2_000_000 if market_region == "US" else 50_000_000,
                )
                or (2_000_000 if market_region == "US" else 50_000_000)
            ),
            1.0,
        )
        turnover_floor = max(min_turnover * 5.0, 10_000_000.0) if market_region == "US" else max(min_turnover * 3.0, 150_000_000.0)
        ignition_setup = setup == "opening_ignition"
        momentum_setup = setup == "intraday_momentum"
        extended_setup = setup == "extended_momentum_watch"
        pre_rally_setup = setup == "pre_rally_fuel"
        market_action_breakout_setup = setup in {
            "52_week_high_volume_breakout",
            "broker_re_rating_breakout",
            "earnings_beat_gap_and_go",
            "market_action_momentum",
            "top_gainer_momentum",
            "price_shocker_reversal_breakout",
        }
        circuit_setup = setup == "circuit_demand_lock"
        fast_mover = (
            ignition_setup
            or momentum_setup
            or extended_setup
            or market_action_breakout_setup
            or circuit_setup
            or bool(session.get("fast_mover"))
            or live_score >= 0.70
        )
        participation = max(volume_ratio, projected_volume_ratio * 0.75)
        volume_confirmed = participation >= 1.15 or turnover >= turnover_floor or projected_turnover >= turnover_floor * 1.2
        near_high = high_distance is None or high_distance <= (1.5 if ignition_setup else 2.0)
        late_chase = extended_setup or day_gain >= 7.0 or wait_reason in {
            "opportunity_scan_wait_for_pullback",
            "opportunity_scan_watch_for_pullback",
        }
        early_ignition_ready = (
            ignition_setup
            and day_gain >= 1.5
            and day_gain < 4.0
            and range_position >= 0.68
            and near_high
            and volume_confirmed
        )
        live_momentum_ready = (
            momentum_setup
            and day_gain >= 4.0
            and day_gain < 7.0
            and range_position >= 0.70
            and near_high
            and volume_confirmed
        )
        market_action_breakout_ready = (
            market_action_breakout_setup
            and day_gain >= 2.0
            and day_gain < 7.0
            and range_position >= 0.65
            and near_high
            and volume_confirmed
        )
        confirmed = bool(session.get("confirmed", True)) and (
            early_ignition_ready or live_momentum_ready or market_action_breakout_ready
        )
        if wait_reason:
            confirmed = False
        if pre_rally_setup:
            confirmed = False
        if circuit_setup:
            confirmed = False
        reason = (
            "opening ignition confirmed"
            if early_ignition_ready
            else "live fast mover confirmed"
            if live_momentum_ready
            else "market-action breakout confirmed"
            if market_action_breakout_ready
            else "demand locked; wait for circuit unlock or VWAP pullback"
            if circuit_setup
            else "late chase blocked; wait for pullback"
            if late_chase
            else "pre-rally fuel; wait for opening ignition"
            if pre_rally_setup
            else "live fast mover needs more confirmation"
        )
        if wait_reason and not (circuit_setup or extended_setup or pre_rally_setup):
            reason = "opportunity scan entry window is wait-only; wait for pullback or fresh confirmation"
        review = {
            "setup": setup,
            "fast_mover": fast_mover,
            "strategy_ready": confirmed,
            "early_ignition_ready": early_ignition_ready,
            "live_momentum_ready": live_momentum_ready,
            "market_action_breakout_ready": market_action_breakout_ready,
            "circuit_demand_lock": circuit_setup,
            "late_chase": late_chase,
            "live_momentum_score": round(live_score, 4),
            "day_gain_pct": round(day_gain, 3),
            "day_range_position": round(range_position, 3),
            "day_high_distance_pct": round(high_distance, 3) if high_distance is not None else None,
            "volume_ratio": round(volume_ratio, 3),
            "projected_volume_ratio": round(projected_volume_ratio, 3),
            "turnover": round(turnover, 2),
            "projected_turnover": round(projected_turnover, 2),
            "turnover_floor": round(turnover_floor, 2),
            "participation_ratio": round(participation, 3),
            "volume_confirmed": volume_confirmed,
            "near_day_high": near_high,
            "reason": reason,
        }
        full["live_momentum_review"] = review
        if _decision_authority_reset_enabled(self.settings):
            reset_notes = full.setdefault("decision_authority_reset", {})
            reset_notes["live_momentum"] = {
                "status": "diagnostic_only",
                "reason": "reset_v2 uses reset_trade_authority_gate instead of strategy mutation",
                "strategy_ready": confirmed,
                "setup": setup,
            }
            return
        if not confirmed:
            return

        score = max(0.76, min(0.93, 0.72 + live_score * 0.18 + min(max(day_gain - 4.0, 0.0), 4.0) * 0.01))
        strategy = {
            "name": setup if market_action_breakout_setup else "live_intraday_momentum",
            "score": round(score, 3),
            "direction": "BUY",
            "confidence": round(min(0.88, 0.62 + live_score * 0.22), 3),
            "notes": [
                f"live gain {day_gain:+.1f}% from open",
                "holding upper part of day range",
                "volume/turnover confirms participation",
            ],
            "metadata": review,
        }
        current = context.get("best_strategy") if isinstance(context.get("best_strategy"), dict) else {}
        if float(current.get("score") or 0.0) < strategy["score"]:
            context["best_strategy"] = strategy
            signals = context.get("strategy_signals")
            if isinstance(signals, list):
                signals.append(strategy)
        entry = full.get("entry_quality") if isinstance(full.get("entry_quality"), dict) else {}
        if entry:
            current_grade = str(entry.get("entry_grade") or "WATCH").upper()
            if current_grade in {"", "WATCH", "C"}:
                entry["entry_grade"] = "B" if day_gain < 7.0 else "A"
                entry["setup_type"] = "live_intraday_momentum"
                entry["quality_score"] = max(float(entry.get("quality_score") or 0.0), 72.0 if day_gain < 7.0 else 86.0)
            entry["volume_confirmation"] = True
            entry["session_confirmation"] = review

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
        if setup in {"opening_ignition", "intraday_momentum", "extended_momentum_watch", "pre_rally_fuel"}:
            if data_quality and data_quality.get("actionable_data_ready") is False and not data_quality.get("probe_only"):
                return None
            return f"opportunity_scan_{setup}"
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
        failed_gates = (
            decision_gates.get("blocking_failed_gates")
            if isinstance(decision_gates.get("blocking_failed_gates"), list)
            else decision_gates.get("failed_gates")
            if isinstance(decision_gates.get("failed_gates"), list)
            else []
        )
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
        macro_event = context.get("macro_event_context") if isinstance(context.get("macro_event_context"), dict) else {}
        has_position = float((context.get("position") or {}).get("qty") or 0.0) > 0
        if macro_event.get("is_monthly_expiry_eve") and not has_position:
            expiry_cap = min(_float_or_none(macro_event.get("expiry_size_multiplier")) or 0.35, 0.35)
            multiplier = min(multiplier, expiry_cap)
            allocation_cap = min(allocation_cap, expiry_cap)
            modifiers.append(f"monthly_expiry_eve_probe_size_cap={expiry_cap}")
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
            "legacy_entry_gates_removed": True,
            "entry_model": context.get("raw_entry_model"),
            "has_existing_position": has_position,
            "current_open_positions": len([row for row in positions.values() if row.get("qty", 0) > 0]),
            "max_positions": risk_limits.get("max_positions"),
            "buy_entry_line": (context.get("raw_entry_model") or {}).get("entry_line"),
            "buy_requires_no_existing_position": True,
            "truth_checks": (context.get("raw_entry_model") or {}).get("truth_blocks", []),
            "broker_checks_after_decision": [
                "daily_loss_limit",
                "max_positions",
                "max_position_pct",
                "max_order_value_pct",
                "available_cash",
            ],
            "llm_deep_review_selected": llm_selected,
                "llm_candidate_limit": risk_limits.get("llm_candidate_limit"),
            "decision_gate_context": context.get("decision_gate_context"),
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
                "deterministic_exit": "SELL on hard stop, take-profit, or time stop from position rules only",
                "llm_exit_review": "primary mode reviews open positions before new entries when within LLM Symbols/Cycle limit",
            }
        return _json_dumps(
            {
                "audit_version": 1,
                "decision_path": decision_path,
                "final_action": action,
                "action_reason": action_reason,
                "action_policy": {
                    "BUY": "raw_entry_model_v1 score meets the entry line, with only invalid quote and explicitly untradeable truth checks able to stop entry",
                    "SELL": "existing long position plus hard stop, take-profit, or time stop from position rules",
                    "HOLD": "raw entry score is below the entry line, an existing position is already open, or a truth check failed",
                },
                "score_breakdown": score_breakdown,
                "overall_score_pct": (context.get("system_gate_audit") or {}).get("overall_score_pct"),
                "overall_grade": (context.get("system_gate_audit") or {}).get("overall_grade"),
                "raw_entry_model": context.get("raw_entry_model"),
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
        "raw_entry_model": context.get("raw_entry_model"),
        "sentiment": context.get("sentiment"),
        "global_market_context": context.get("global_market_context"),
        "institutional_context": context.get("institutional_context"),
        "market_breadth_context": context.get("market_breadth_context"),
        "macro_event_context": context.get("macro_event_context"),
        "opportunity_scan": context.get("opportunity_scan"),
        "timeframe_data": context.get("timeframe_data"),
        "sector_rotation": context.get("sector_rotation"),
        "delivery_data": context.get("delivery_data"),
        "data_readiness": context.get("data_readiness"),
        "performance_feedback": context.get("performance_feedback"),
        "system_gate_audit": context.get("system_gate_audit"),
        "decision_gate_context": context.get("decision_gate_context"),
        "fresh_trade_authority": context.get("fresh_trade_authority"),
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


def _risk_flag_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item or "").strip() for item in value if str(item or "").strip()]
    if isinstance(value, str):
        try:
            parsed = ast.literal_eval(value)
        except (SyntaxError, ValueError):
            parsed = None
        if isinstance(parsed, list):
            return [str(item or "").strip() for item in parsed if str(item or "").strip()]
        return [part.strip() for part in value.replace("[", "").replace("]", "").replace("'", "").split(",") if part.strip()]
    return []


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


def _score_grade(score: float | None) -> str:
    value = _float_or_none(score)
    if value is None:
        return ""
    if value >= 80:
        return "A"
    if value >= 70:
        return "B"
    if value >= 55:
        return "C"
    return "D"


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
