from __future__ import annotations

from collections import defaultdict, deque
from typing import Any

from .analysis_tools import build_symbol_tool_context, deterministic_score
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
        }

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
            )
            combined = deterministic_score(context)
            if llm_primary and llm_reviews < self.settings.llm_max_symbols_per_cycle:
                decision = await self.llm.decide(context)
                llm_reviews += 1
                decisions.append(decision)
                continue

            action = self._action_from_score(symbol, combined, positions)
            confidence = min(abs(combined), 0.99)
            candle_summary = context["candlestick_analysis"]
            best_strategy = context["best_strategy"]
            reason = (
                f"tools technical={technical.score:.2f} ({technical.trend}), "
                f"candles={candle_summary['score']:.2f} {candle_summary['patterns']}, "
                f"best_strategy={best_strategy['name']}:{best_strategy['score']:.2f}, "
                f"sentiment={sentiment_score:.2f}, combined={combined:.2f}"
            )
            decision = Decision(
                symbol=symbol,
                action=action,
                confidence=round(confidence, 3),
                price=quote.price,
                technical_score=round(technical.score, 3),
                sentiment_score=round(sentiment_score, 3),
                reason=reason,
                asof=utc_now(),
                strategy=best_strategy["name"],
            )

            if (
                self.llm.enabled
                and self.settings.llm_decision_mode == "review"
                and action != "HOLD"
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
