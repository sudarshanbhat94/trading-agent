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
            "llm_candidate_limit": self.settings.llm_max_symbols_per_cycle,
        }

        scan_items: list[dict[str, Any]] = []
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
            context = build_symbol_tool_context(
                row=row,
                quote=quote,
                candles=candles,
                position=positions.get(symbol),
                sentiment_score=sentiment_score,
                risk_limits=risk_limits,
                global_context=global_context,
            )
            combined = deterministic_score(context)
            score_breakdown = deterministic_score_breakdown(context)
            action = self._action_from_score(symbol, combined, positions)
            confidence = min(abs(combined), 0.99)
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
                    "non-HOLD candidates ranked first, then absolute combined score, strategy confidence, "
                    "technical score, and sentiment"
                ),
            }

        llm_candidate_symbols: set[str] = set()
        if llm_primary:
            llm_pool = [item for item in ranked if item["action"] != "HOLD"] or ranked
            llm_candidate_symbols = {
                item["symbol"] for item in llm_pool[: self.settings.llm_max_symbols_per_cycle]
            }

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
            reason = (
                f"tools technical={item['technical'].score:.2f} ({item['technical'].trend}), "
                f"candles={candle_summary['score']:.2f} {candle_summary['patterns']}, "
                f"best_strategy={best_strategy['name']}:{best_strategy['score']:.2f}, "
                f"sentiment={item['sentiment_score']:.2f}, "
                f"global={float(global_risk.get('risk_score', 0.0) or 0.0):.2f} ({global_risk.get('regime', 'unknown')}), "
                f"combined={item['combined']:.2f}, universe_rank={context['universe_scan']['rank']}/{len(scan_items)}"
            )
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
    ) -> list[Decision]:
        decisions: list[Decision] = []
        for symbol, position in positions.items():
            quote = quotes.get(symbol)
            if not quote or position["qty"] <= 0:
                continue
            avg_price = float(position["avg_price"])
            stop = avg_price * (1 - self.settings.stop_loss_pct)
            target = avg_price * (1 + self.settings.take_profit_pct)
            if quote.price <= stop:
                reason = f"risk exit: price {quote.price:.2f} <= stop {stop:.2f}"
            elif quote.price >= target:
                reason = f"profit exit: price {quote.price:.2f} >= target {target:.2f}"
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
                        target=target,
                        reason=reason,
                    ),
                )
            )
        return decisions

    def _action_from_score(
        self,
        symbol: str,
        combined: float,
        positions: dict[str, dict[str, Any]],
    ) -> str:
        has_position = symbol in positions and positions[symbol]["qty"] > 0
        if combined >= 0.45 and not has_position:
            return "BUY"
        if combined <= -0.38 and has_position:
            return "SELL"
        return "HOLD"

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
        gates = {
            "has_existing_position": has_position,
            "current_open_positions": len([row for row in positions.values() if row.get("qty", 0) > 0]),
            "max_positions": risk_limits.get("max_positions"),
            "buy_threshold": 0.45,
            "sell_threshold": -0.38,
            "buy_requires_no_existing_position": True,
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
        }
        return _json_dumps(
            {
                "audit_version": 1,
                "decision_path": decision_path,
                "final_action": action,
                "action_reason": action_reason,
                "action_policy": {
                    "BUY": "combined score >= 0.45 and no existing long position",
                    "SELL": "combined score <= -0.38 and an existing long position is open",
                    "HOLD": "score/action gates did not permit a trade",
                },
                "score_breakdown": score_breakdown,
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
                    "stop_price": round(stop, 4),
                    "target_price": round(target, 4),
                    "stop_triggered": quote.price <= stop,
                    "take_profit_triggered": quote.price >= target,
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
        "universe_scan": context.get("universe_scan"),
        "risk_limits": context.get("risk_limits"),
        "recent_candle_count": len(recent_candles),
        "recent_candles_tail": recent_candles[-5:],
    }


def _json_dumps(value: dict[str, Any]) -> str:
    return json.dumps(value, default=str, separators=(",", ":"))
