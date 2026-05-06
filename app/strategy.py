from __future__ import annotations

import json
from collections import defaultdict, deque
from typing import Any

from .analysis_tools import build_symbol_tool_context, deterministic_score, deterministic_score_breakdown
from .config import Settings
from .indicators import technical_snapshot
from .llm_brain import LLMBrain
from .models import Candle, Decision, Quote, utc_now
from .sentiment import SentimentService


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
        delivery_service: Any | None = None,
        market_breadth: dict[str, Any] | None = None,
        sector_rotation_context: dict[str, Any] | None = None,
        macro_calendar: Any | None = None,
    ) -> list[Decision]:
        sentiment_scores = await self.sentiment.scores_for_cycle(universe)
        decisions: list[Decision] = []
        llm_reviews = 0
        llm_primary = self.settings.llm_decision_mode == "primary" and self.llm.enabled
        candles_by_symbol = candles_by_symbol or {}
        risk_limits = {
            "max_positions": self.settings.max_positions,
            "max_position_pct": self.settings.max_position_pct,
            "max_order_value_pct": self.settings.max_order_value_pct,
            "stop_loss_pct": self.settings.stop_loss_pct,
            "take_profit_pct": self.settings.take_profit_pct,
            "daily_loss_limit_pct": self.settings.daily_loss_limit_pct,
            "min_llm_confidence": self.settings.llm_primary_min_confidence,
            "global_risk_weight": self.settings.global_risk_weight,
            "institutional_risk_weight": self.settings.institutional_risk_weight,
            "llm_candidate_limit": self.settings.llm_max_symbols_per_cycle,
        }

        scan_items: list[dict[str, Any]] = []
        breadth_regime = (market_breadth or {}).get("breadth_regime")
        if breadth_regime == "bear_confirmed":
            self._log_pre_filter("market_breadth_bear_regime_blocked_buys", {"breadth_regime": breadth_regime})
        for row in universe:
            symbol = row["symbol"]
            quote = quotes.get(symbol)
            if not quote:
                continue
            self._history[symbol].append(quote.price)
            sentiment_score = sentiment_scores.get(symbol, 0.0)
            candles = candles_by_symbol.get(symbol, [])
            if candles:
                history = [candle.close for candle in candles]
            else:
                history = list(self._history[symbol])
            technical = technical_snapshot(history)
            macro_event_context = (
                macro_calendar.event_context_for_date(symbol=symbol)
                if macro_calendar is not None
                else {}
            )
            delivery_data = self._delivery_context(symbol, delivery_service)
            sector_context = ((sector_rotation_context or {}).get("symbols") or {}).get(symbol, {})
            pre_filter = self._pre_filter_context(
                symbol=symbol,
                row=row,
                quote=quote,
                candles=candles,
                positions=positions,
                delivery_data=delivery_data,
                market_breadth=market_breadth or {},
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
                sector_context=sector_context,
                market_breadth=market_breadth,
                macro_event_context=macro_event_context,
            )
            context["pre_filter"] = pre_filter
            combined = deterministic_score(context)
            score_breakdown = deterministic_score_breakdown(context)
            action = self._action_from_context(symbol, combined, positions, context, candles_by_symbol)
            confidence = min(abs(combined), 0.99)
            if action == "BUY":
                if market_breadth and market_breadth.get("breadth_regime") == "bear_warning":
                    confidence = max(confidence - 0.25, 0.0)
                if market_breadth and market_breadth.get("breadth_regime") == "bull_confirmed":
                    confidence = min(confidence + 0.10, 0.99)
                if market_breadth and market_breadth.get("breadth_thrust"):
                    confidence = min(confidence + 0.15, 0.99)
                if float(macro_event_context.get("event_risk_score") or 0.0) > 0.6:
                    confidence = max(confidence - 0.20, 0.0)
            scan_items.append(
                {
                    "row": row,
                    "symbol": symbol,
                    "quote": quote,
                    "technical": technical,
                    "sentiment_score": sentiment_score,
                    "context": context,
                    "combined": combined,
                    "score_breakdown": score_breakdown,
                    "action": action,
                    "confidence": confidence,
                }
            )

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
                    "then highest-ranked symbols by combined score, full-spectrum layers, strategy confidence, technical score, and sentiment"
                ),
            }

        llm_candidate_symbols: set[str] = set()
        if llm_primary:
            llm_candidate_symbols = self._llm_candidate_symbols(ranked)

        for item in scan_items:
            context = item["context"]
            if llm_primary and item["symbol"] in llm_candidate_symbols:
                decision = await self.llm.decide(context)
                llm_reviews += 1
                decisions.append(decision)
                continue

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
            decision_path = "deterministic_after_full_universe_scan"
            decision = Decision(
                symbol=item["symbol"],
                action=item["action"],
                confidence=round(item["confidence"], 3),
                price=item["quote"].price,
                technical_score=round(item["technical"].score, 3),
                sentiment_score=round(item["sentiment_score"], 3),
                reason=reason,
                asof=utc_now(),
                strategy=best_strategy["name"],
                details_json=self._decision_details_json(
                    context=context,
                    action=item["action"],
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
                risk_unit = 1.5 * atr
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
        divergence = full_spectrum.get("price_volume_divergence") or {}
        alignment = ((full_spectrum.get("trend_context") or {}).get("timeframe_alignment") or {})
        sector = full_spectrum.get("sector_rotation") or {}
        market_breadth = context.get("market_breadth_context") or {}
        pre_filter = context.get("pre_filter") or {}
        confluence_total = int(confluence.get("total", 0) or 0)
        threshold = float(pre_filter.get("buy_threshold") or 0.35)
        if market_breadth.get("breadth_regime") == "bear_warning":
            threshold = max(threshold, 0.45)
        if market_breadth.get("breadth_regime") == "bull_confirmed":
            threshold = min(threshold, 0.30)
        failed_gates: list[dict[str, Any]] = []

        def fail(gate: str, value: Any, reason: str) -> None:
            failed_gates.append({"gate": gate, "value": value, "reason": reason})

        if pre_filter.get("buy_blocked") and not has_position:
            fail(pre_filter.get("block_gate", "pre_filter"), pre_filter.get("block_value"), pre_filter.get("elimination_reason", "pre_filter_block"))
        if entry.get("entry_grade") == "D":
            fail("entry_grade_gate", entry.get("entry_grade"), "extended_entry_no_new_longs")
        if breakout.get("two_day_rule_failed"):
            fail("breakout_quality_gate", True, "false_breakout_two_day_rule_failed")
        if stage and not stage.get("buy_permitted", True):
            fail("stage_buy_permitted", stage.get("stage"), "stage_analysis_not_stage2_markup")
        if divergence.get("climax_volume_top"):
            fail("climax_volume_gate", True, "climax_top_detected_no_new_longs")
        if alignment.get("alignment_grade") == "D":
            fail("timeframe_alignment_gate", "D", "timeframe_alignment_conflict")
        if sector.get("sector_tier") == "bottom_quartile" and sector.get("sector_stage") == "distribution" and confluence_total <= 20:
            fail("sector_rotation_gate", sector, "bottom_quartile_distribution")
        correlation_gate = self._portfolio_correlation_gate(symbol, positions, candles_by_symbol or {})
        if correlation_gate.get("block_buy"):
            fail("portfolio_correlation_gate", correlation_gate, "portfolio_concentration_correlation_too_high")
        context["portfolio_correlation_gate"] = correlation_gate
        exit_pressure = (
            scorecard.get("hard_veto", {}).get("failed")
            or "sentiment_not_bearish" in (scorecard.get("must_pass_failed") or [])
            or "hard_veto_clear" in (scorecard.get("must_pass_failed") or [])
        )
        if risk_overrides.get("no_new_longs") and not has_position:
            fail("risk_overrides", risk_overrides.get("flags", []), "risk_override_no_new_longs")
        sizing_grade = self._position_sizing_grade(context, context.get("risk_limits", {}).get("portfolio_equity", 0), positions)
        if correlation_gate.get("warning"):
            sizing_grade["modifier_details"].append(correlation_gate.get("warning"))
            sizing_grade["final_multiplier"] = round(max(float(sizing_grade["final_multiplier"]) * 0.5, 0.0), 4)
            sizing_grade["recommended_max_position_pct"] = min(
                self.settings.max_position_pct,
                self.settings.max_position_pct * sizing_grade["final_multiplier"],
            )
        context["sizing_grade"] = sizing_grade
        evaluated_gates = [
            *list(pre_filter.get("gates") or []),
            {"gate": "entry_grade_gate", "passed": entry.get("entry_grade") != "D", "value": entry.get("entry_grade")},
            {"gate": "breakout_gate", "passed": not breakout.get("two_day_rule_failed"), "value": breakout},
            {"gate": "divergence_gate", "passed": not divergence.get("climax_volume_top"), "value": divergence},
            {"gate": "alignment_gate", "passed": alignment.get("alignment_grade") != "D", "value": alignment.get("alignment_grade")},
        ]
        context["decision_gate_context"] = {
            "buy_threshold": threshold,
            "failed_gates": failed_gates,
            "evaluated_gates": evaluated_gates,
            "pre_filter": pre_filter,
            "breadth_regime": market_breadth.get("breadth_regime"),
            "breadth_thrust": market_breadth.get("breadth_thrust"),
        }
        if failed_gates and not has_position:
            return "HOLD"
        if combined >= threshold and confluence_total >= 16 and scorecard.get("buy_ready") and not has_position:
            return "BUY"
        if (combined <= -0.38 or exit_pressure) and has_position:
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
        delivery_failed = delivery_score < -0.4 and not has_position
        gates.append({"gate": "delivery_gate", "passed": not delivery_failed, "value": delivery_score})
        if delivery_failed:
            buy_blocked = True
            block_gate = "delivery_gate"
            block_value = delivery_score
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
        earnings_days = macro_event_context.get("earnings_days_away")
        macro_failed = (earnings_days is not None and earnings_days <= 5 and not has_position) or macro_event_context.get("is_expiry_day")
        macro_reason = "earnings_lockout" if earnings_days is not None and earnings_days <= 5 else "expiry_day_no_new_longs" if macro_event_context.get("is_expiry_day") else None
        gates.append({"gate": "earnings_gate", "passed": not macro_failed, "value": macro_event_context})
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

    def _scan_priority(self, item: dict[str, Any]) -> tuple[float, float, float, float, float]:
        return (
            1.0 if item["action"] != "HOLD" else 0.0,
            abs(float(item["combined"])),
            abs(float(item["context"].get("best_strategy", {}).get("score", 0.0) or 0.0)),
            abs(float(item["technical"].score)),
            abs(float(item["sentiment_score"])),
        )

    def _scan_priority_score(self, item: dict[str, Any]) -> float:
        action_boost, combined, strategy, technical, sentiment = self._scan_priority(item)
        return (action_boost * 0.5) + (combined * 0.28) + (strategy * 0.12) + (technical * 0.06) + (sentiment * 0.04)

    def _llm_candidate_symbols(self, ranked: list[dict[str, Any]]) -> set[str]:
        limit = max(int(self.settings.llm_max_symbols_per_cycle or 1), 1)
        selected: list[str] = []

        def add(items: list[dict[str, Any]]) -> None:
            for item in items:
                symbol = item["symbol"]
                if symbol in selected:
                    continue
                selected.append(symbol)
                if len(selected) >= limit:
                    return

        open_positions = sorted(
            [item for item in ranked if self._has_open_position(item)],
            key=self._exit_review_priority,
            reverse=True,
        )
        action_candidates = [item for item in ranked if item["action"] != "HOLD"]
        add(open_positions)
        if len(selected) < limit:
            add(action_candidates)
        if len(selected) < limit:
            add(ranked)
        return set(selected)

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
        entry_mod = {"A": 1.0, "B": 0.85, "C": 0.65}.get(entry.get("entry_grade"), 1.0)
        multiplier *= entry_mod
        modifiers.append(f"entry_grade={entry.get('entry_grade')} x{entry_mod}")
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
        recommended = min(self.settings.max_position_pct, self.settings.max_position_pct * multiplier)
        return {
            "final_multiplier": round(multiplier, 4),
            "base_multiplier": base,
            "modifier_details": modifiers,
            "recommended_max_position_pct": round(recommended, 6),
            "portfolio_equity": portfolio_equity,
            "open_positions": len([row for row in positions.values() if row.get("qty", 0) > 0]),
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
            "buy_requires_institutional_scorecard_ready": True,
            "institutional_scorecard": {
                "buy_ready": scorecard.get("buy_ready"),
                "score": scorecard.get("total_score"),
                "grade": scorecard.get("grade"),
                "failed": scorecard.get("must_pass_failed", []),
                "hard_veto": (scorecard.get("hard_veto") or {}).get("failed", []),
            },
            "sell_threshold": -0.38,
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
                "deterministic_exit": "SELL if combined <= -0.38, hard stop hit, or take-profit hit",
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
                    "BUY": "combined score >= 0.35, confluence >= 16/26, institutional scorecard buy_ready=true, no existing long position, and no no-new-longs override",
                    "SELL": "existing long position plus combined score <= -0.38, LLM exit call, hard stop, take-profit, or invalidation trigger",
                    "HOLD": "score/action gates did not permit a trade",
                },
                "score_breakdown": score_breakdown,
                "pre_filter": context.get("pre_filter"),
                "sizing_grade": context.get("sizing_grade"),
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
        "sector_rotation": context.get("sector_rotation"),
        "delivery_data": context.get("delivery_data"),
        "pre_filter": context.get("pre_filter"),
        "decision_gate_context": context.get("decision_gate_context"),
        "sizing_grade": context.get("sizing_grade"),
        "full_spectrum_analysis": context.get("full_spectrum_analysis"),
        "universe_scan": context.get("universe_scan"),
        "risk_limits": context.get("risk_limits"),
        "recent_candle_count": len(recent_candles),
        "recent_candles_tail": recent_candles[-5:],
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


def _atr(candles: list[Candle], period: int = 14) -> float | None:
    if len(candles) < period + 1:
        return None
    ranges = []
    for previous, candle in zip(candles[-period - 1 : -1], candles[-period:]):
        ranges.append(max(candle.high - candle.low, abs(candle.high - previous.close), abs(candle.low - previous.close)))
    return sum(ranges) / len(ranges) if ranges else None


def _held_periods_from_position(position: dict[str, Any], candles: list[Candle]) -> int:
    updated = str(position.get("updated_at") or "")
    if not updated or not candles:
        return 0
    return min(len(candles), 16)


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
