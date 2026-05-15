from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .market_regions import market_region_for_row
from .models import Candle, Quote, utc_now


@dataclass(frozen=True)
class OpportunityScanResult:
    selected_universe: list[dict[str, Any]]
    candidates: list[dict[str, Any]]
    rejected_counts: dict[str, int]
    summary: dict[str, Any]


class OpportunityScanner:
    """Ranks a broad quote universe before the expensive strategy/LLM pass."""

    def __init__(self, settings: Any) -> None:
        self.settings = settings
        self.candidate_limit = max(1, int(getattr(settings, "dynamic_scan_candidate_limit", 120) or 120))
        self.min_price = max(0.0, float(getattr(settings, "dynamic_scan_min_price", 10.0) or 0.0))
        self.min_turnover = max(0.0, float(getattr(settings, "dynamic_scan_min_turnover_inr", 50_000_000.0) or 0.0))
        self.breakout_distance_pct = max(
            0.1,
            float(getattr(settings, "dynamic_scan_breakout_distance_pct", 3.0) or 3.0),
        )
        self.sentiment_enabled = bool(getattr(settings, "dynamic_scan_sentiment_enabled", True))
        self.sentiment_weight = _clamp(
            float(getattr(settings, "dynamic_scan_sentiment_weight", 0.12) or 0.0),
            0.0,
            0.3,
        )

    def rank(
        self,
        universe: list[dict[str, Any]],
        quotes: dict[str, Quote],
        candle_sets: dict[str, dict[str, list[Candle]]] | None = None,
        positions: dict[str, dict[str, Any]] | None = None,
        sentiment_by_symbol: dict[str, dict[str, Any]] | None = None,
    ) -> OpportunityScanResult:
        candle_sets = candle_sets or {}
        positions = positions or {}
        sentiment_by_symbol = sentiment_by_symbol or {}
        rejected_counts: dict[str, int] = {}
        scored: list[dict[str, Any]] = []
        forced: list[dict[str, Any]] = []

        for row in universe:
            symbol = str(row.get("symbol") or "").upper()
            if not symbol:
                self._count(rejected_counts, "missing_symbol")
                continue
            quote = quotes.get(symbol)
            in_position = symbol in positions
            if not quote:
                if in_position:
                    forced.append(self._forced_item(row, "open_position_without_quote"))
                else:
                    self._count(rejected_counts, "missing_quote")
                continue
            item = self._score_row(
                row,
                quote,
                candle_sets.get(symbol) or {},
                in_position,
                sentiment_by_symbol.get(symbol) or {},
            )
            if item["rejected"] and not in_position:
                self._count(rejected_counts, item["reject_reason"])
                continue
            if in_position and item["rejected"]:
                item["forced_inclusion"] = True
                item["reasons"].append("open position included for exit/risk management")
            scored.append(item)

        scored.sort(key=lambda item: item["score"], reverse=True)
        selected_items = scored[: self.candidate_limit]
        selected_symbols = {item["symbol"] for item in selected_items}
        for item in scored[self.candidate_limit :]:
            if item.get("forced_inclusion") and item["symbol"] not in selected_symbols:
                selected_items.append(item)
                selected_symbols.add(item["symbol"])
        for item in forced:
            if item["symbol"] not in selected_symbols:
                selected_items.append(item)
                selected_symbols.add(item["symbol"])

        row_by_symbol = {str(row.get("symbol") or "").upper(): row for row in universe}
        selected_universe = [row_by_symbol[item["symbol"]] for item in selected_items if item["symbol"] in row_by_symbol]
        candidates = [self._public_item(item) for item in selected_items]
        summary = {
            "enabled": True,
            "mode": "dynamic_opportunity_scan",
            "scanned_at": utc_now(),
            "raw_symbols": len(universe),
            "quoted_symbols": len(quotes),
            "candidate_limit": self.candidate_limit,
            "selected_symbols": len(selected_universe),
            "forced_position_symbols": [item["symbol"] for item in selected_items if item.get("forced_inclusion")],
            "rejected_counts": rejected_counts,
            "top_candidates": candidates[:25],
            "setup_counts": self._counts(item.get("setup") for item in selected_items),
            "bucket_counts": self._counts(item.get("bucket") for item in selected_items),
            "positive_news_candidates": sum(
                1
                for item in selected_items
                if ((item.get("sentiment") or {}).get("positive_catalyst"))
            ),
            "negative_news_filtered": rejected_counts.get("negative_news_catalyst", 0),
            "filters": {
                "min_price": self.min_price,
                "min_turnover_inr": self.min_turnover,
                "breakout_distance_pct": self.breakout_distance_pct,
                "sentiment_enabled": self.sentiment_enabled,
                "sentiment_weight": self.sentiment_weight,
            },
        }
        return OpportunityScanResult(
            selected_universe=selected_universe,
            candidates=candidates,
            rejected_counts=rejected_counts,
            summary=summary,
        )

    def _score_row(
        self,
        row: dict[str, Any],
        quote: Quote,
        candle_set: dict[str, list[Candle]],
        in_position: bool,
        sentiment_detail: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        symbol = str(row.get("symbol") or "").upper()
        price = max(float(quote.price or 0.0), 0.0)
        candles = candle_set.get("analysis") or candle_set.get("daily") or candle_set.get("intraday") or []
        metrics = self._metrics(price, quote, candles)
        sentiment = self._sentiment_metrics(sentiment_detail or {})
        reasons: list[str] = []
        reject_reason = ""

        if price <= 0:
            reject_reason = "invalid_price"
        elif price < self.min_price:
            reject_reason = "below_min_price"
        elif metrics["turnover"] < self.min_turnover and not in_position:
            reject_reason = "below_min_turnover"
        elif sentiment["negative_catalyst"] and not in_position:
            reject_reason = "negative_news_catalyst"

        liquidity = self._liquidity_score(metrics["turnover"])
        trend = self._trend_score(price, metrics)
        breakout = self._breakout_score(price, quote, metrics)
        momentum = self._momentum_score(metrics)
        volume = self._volume_score(metrics)
        risk = self._risk_score(metrics)
        base_score = (
            liquidity * 0.20
            + trend * 0.22
            + breakout * 0.24
            + momentum * 0.16
            + volume * 0.10
            + risk * 0.08
        )
        score = _clamp(base_score + sentiment["boost"], 0.0, 1.0)

        if metrics["distance_to_55d_high_pct"] is not None and metrics["distance_to_55d_high_pct"] <= self.breakout_distance_pct:
            reasons.append(f"near 55D high ({metrics['distance_to_55d_high_pct']:.1f}% away)")
        elif metrics["day_high_distance_pct"] is not None and metrics["day_high_distance_pct"] <= 1.0:
            reasons.append(f"near day high ({metrics['day_high_distance_pct']:.1f}% away)")
        if trend >= 0.65:
            reasons.append("trend filter positive")
        if volume >= 0.65:
            reasons.append("volume expansion")
        if momentum >= 0.65:
            reasons.append("momentum improving")
        if sentiment["positive_catalyst"]:
            reasons.append(
                f"positive news catalyst ({sentiment['score']:+.2f}, {sentiment['headline_count']} headlines)"
            )
        elif sentiment["negative_catalyst"]:
            reasons.append(
                f"negative news risk ({sentiment['score']:+.2f}, {sentiment['headline_count']} headlines)"
            )
        if risk < 0.35:
            reasons.append("risk/reward needs caution")

        setup = self._setup(metrics, trend, breakout, momentum, volume, sentiment)
        bucket = self._bucket(score, risk, reject_reason)
        return {
            "symbol": symbol,
            "name": row.get("name") or symbol,
            "market_region": market_region_for_row(row),
            "score": round(score, 4),
            "bucket": bucket,
            "setup": setup,
            "rejected": bool(reject_reason),
            "reject_reason": reject_reason,
            "forced_inclusion": bool(in_position),
            "reasons": reasons or ["quote/liquidity pass"],
            "metrics": metrics,
            "sentiment": sentiment,
            "components": {
                "liquidity": round(liquidity, 4),
                "trend": round(trend, 4),
                "breakout": round(breakout, 4),
                "momentum": round(momentum, 4),
                "volume": round(volume, 4),
                "risk": round(risk, 4),
                "sentiment": round(sentiment["boost"], 4),
            },
        }

    def _metrics(self, price: float, quote: Quote, candles: list[Candle]) -> dict[str, Any]:
        closes = [float(item.close) for item in candles if item.close is not None and float(item.close) > 0]
        highs = [float(item.high) for item in candles if item.high is not None and float(item.high) > 0]
        lows = [float(item.low) for item in candles if item.low is not None and float(item.low) > 0]
        volumes = [float(item.volume or 0.0) for item in candles]
        last_volume = float(quote.volume or 0.0) or (volumes[-1] if volumes else 0.0)
        avg20_volume = _mean([value for value in volumes[-20:] if value > 0])
        turnover = price * (last_volume or avg20_volume or 0.0)
        if turnover <= 0 and avg20_volume > 0:
            turnover = price * avg20_volume

        high_20 = max(highs[-20:]) if len(highs) >= 20 else None
        high_55 = max(highs[-55:]) if len(highs) >= 55 else high_20
        high_252 = max(highs[-252:]) if len(highs) >= 120 else high_55
        low_20 = min(lows[-20:]) if len(lows) >= 20 else None
        sma_20 = _mean(closes[-20:]) if len(closes) >= 20 else None
        sma_50 = _mean(closes[-50:]) if len(closes) >= 50 else None
        sma_200 = _mean(closes[-200:]) if len(closes) >= 200 else None
        return_5d = _return_pct(closes, 5)
        return_20d = _return_pct(closes, 20)
        return_60d = _return_pct(closes, 60)
        atr_pct = _atr_pct(highs, lows, closes)
        day_high = _float_or_none(quote.high)
        day_low = _float_or_none(quote.low)
        day_range_pos = None
        if day_high and day_low and day_high > day_low:
            day_range_pos = (price - day_low) / (day_high - day_low)
        volume_ratio = (last_volume / avg20_volume) if last_volume and avg20_volume else None
        return {
            "price": round(price, 4),
            "history_candles": len(candles),
            "turnover": round(turnover, 2),
            "last_volume": round(last_volume, 2),
            "avg20_volume": round(avg20_volume, 2),
            "volume_ratio": _round(volume_ratio),
            "sma_20": _round(sma_20),
            "sma_50": _round(sma_50),
            "sma_200": _round(sma_200),
            "return_5d_pct": _round(return_5d),
            "return_20d_pct": _round(return_20d),
            "return_60d_pct": _round(return_60d),
            "atr_pct": _round(atr_pct),
            "day_range_position": _round(day_range_pos),
            "day_high_distance_pct": _round(_distance_pct(price, day_high)),
            "distance_to_20d_high_pct": _round(_distance_pct(price, high_20)),
            "distance_to_55d_high_pct": _round(_distance_pct(price, high_55)),
            "distance_to_252d_high_pct": _round(_distance_pct(price, high_252)),
            "distance_to_sma20_pct": _round(_signed_distance_pct(price, sma_20)),
            "distance_to_sma50_pct": _round(_signed_distance_pct(price, sma_50)),
            "support_20d": _round(low_20),
            "quote_age_seconds": _quote_age_seconds(quote),
        }

    def _liquidity_score(self, turnover: float) -> float:
        if self.min_turnover <= 0:
            return 0.75
        return _clamp(turnover / (self.min_turnover * 2.0), 0.0, 1.0)

    def _trend_score(self, price: float, metrics: dict[str, Any]) -> float:
        checks = 0.0
        total = 0.0
        for key, weight in (("sma_20", 0.30), ("sma_50", 0.35), ("sma_200", 0.20)):
            value = metrics.get(key)
            if value:
                total += weight
                checks += weight if price >= value else 0.0
        sma50 = metrics.get("sma_50")
        sma200 = metrics.get("sma_200")
        if sma50 and sma200:
            total += 0.15
            checks += 0.15 if sma50 >= sma200 else 0.0
        if total <= 0:
            day_pos = metrics.get("day_range_position")
            return _clamp(float(day_pos or 0.45), 0.0, 0.7)
        return _clamp(checks / total, 0.0, 1.0)

    def _breakout_score(self, price: float, quote: Quote, metrics: dict[str, Any]) -> float:
        distances = [
            metrics.get("distance_to_20d_high_pct"),
            metrics.get("distance_to_55d_high_pct"),
            metrics.get("distance_to_252d_high_pct"),
            metrics.get("day_high_distance_pct"),
        ]
        valid = [float(value) for value in distances if value is not None]
        if not valid:
            return 0.35
        best_distance = min(valid)
        proximity = 1.0 - _clamp(best_distance / self.breakout_distance_pct, 0.0, 1.0)
        day_pos = float(metrics.get("day_range_position") or 0.0)
        if day_pos >= 0.75:
            proximity += 0.12
        if price >= float(quote.high or 0.0) and quote.high:
            proximity += 0.08
        return _clamp(proximity, 0.0, 1.0)

    def _momentum_score(self, metrics: dict[str, Any]) -> float:
        values = [
            float(metrics.get("return_5d_pct") or 0.0) / 8.0,
            float(metrics.get("return_20d_pct") or 0.0) / 18.0,
            float(metrics.get("return_60d_pct") or 0.0) / 35.0,
        ]
        positive = sum(_clamp(value, -0.5, 1.0) for value in values) / len(values)
        return _clamp(0.45 + positive * 0.55, 0.0, 1.0)

    def _volume_score(self, metrics: dict[str, Any]) -> float:
        ratio = metrics.get("volume_ratio")
        if ratio is None:
            return 0.45
        return _clamp(float(ratio) / 2.0, 0.0, 1.0)

    def _risk_score(self, metrics: dict[str, Any]) -> float:
        atr_pct = metrics.get("atr_pct")
        if atr_pct is None:
            return 0.50
        atr = float(atr_pct)
        if atr < 1.0:
            return 0.55
        if 1.0 <= atr <= 5.0:
            return 0.85
        if atr <= 8.0:
            return 0.55
        return 0.25

    def _sentiment_metrics(self, detail: dict[str, Any]) -> dict[str, Any]:
        if not self.sentiment_enabled or not detail:
            return {
                "score": 0.0,
                "confidence": 0.0,
                "headline_count": 0,
                "event_count": 0,
                "boost": 0.0,
                "positive_catalyst": False,
                "negative_catalyst": False,
                "headlines": [],
                "asof": None,
            }
        score = _clamp(_float_or_none(detail.get("score")) or 0.0, -1.0, 1.0)
        confidence = _clamp(_float_or_none(detail.get("confidence")) or 0.0, 0.0, 1.0)
        headlines = detail.get("headlines")
        if not isinstance(headlines, list):
            headlines = []
        events = detail.get("events")
        if not isinstance(events, list):
            events = []
        headline_count = int(detail.get("headline_count") or len(headlines) or 0)
        event_count = len(events)
        evidence = _clamp(max(confidence, min((headline_count + event_count) / 6.0, 1.0)), 0.0, 1.0)
        boost = score * evidence * self.sentiment_weight
        return {
            "score": round(score, 4),
            "confidence": round(confidence, 4),
            "headline_count": headline_count,
            "event_count": event_count,
            "boost": round(boost, 4),
            "positive_catalyst": score >= 0.18 and evidence >= 0.30 and headline_count + event_count > 0,
            "negative_catalyst": score <= -0.30 and evidence >= 0.35 and headline_count + event_count > 0,
            "headlines": [str(item)[:180] for item in headlines[:3]],
            "asof": detail.get("ts") or detail.get("asof"),
        }

    def _setup(
        self,
        metrics: dict[str, Any],
        trend: float,
        breakout: float,
        momentum: float,
        volume: float,
        sentiment: dict[str, Any],
    ) -> str:
        if sentiment.get("positive_catalyst") and (volume >= 0.50 or breakout >= 0.55 or momentum >= 0.55):
            return "news_catalyst"
        if breakout >= 0.70 and volume >= 0.60:
            return "breakout_continuation"
        if trend >= 0.65 and momentum >= 0.60:
            return "trend_momentum"
        distance_sma20 = metrics.get("distance_to_sma20_pct")
        if trend >= 0.65 and distance_sma20 is not None and -2.5 <= float(distance_sma20) <= 2.5:
            return "pullback_buy"
        if momentum >= 0.65 and volume >= 0.65:
            return "smallcap_momentum"
        return "watchlist_candidate"

    def _bucket(self, score: float, risk: float, reject_reason: str) -> str:
        if reject_reason:
            return "Avoid"
        if score >= 0.72 and risk >= 0.45:
            return "Actionable"
        if score >= 0.58:
            return "Small Size Only"
        if score >= 0.45:
            return "Watch"
        return "Avoid"

    def _public_item(self, item: dict[str, Any]) -> dict[str, Any]:
        metrics = item.get("metrics") or {}
        sentiment = item.get("sentiment") or {}
        return {
            "symbol": item.get("symbol"),
            "name": item.get("name"),
            "market_region": item.get("market_region"),
            "score": item.get("score"),
            "bucket": item.get("bucket"),
            "setup": item.get("setup"),
            "forced_inclusion": item.get("forced_inclusion", False),
            "reasons": item.get("reasons", [])[:4],
            "components": item.get("components", {}),
            "sentiment": {
                "score": sentiment.get("score"),
                "confidence": sentiment.get("confidence"),
                "headline_count": sentiment.get("headline_count"),
                "event_count": sentiment.get("event_count"),
                "positive_catalyst": sentiment.get("positive_catalyst"),
                "negative_catalyst": sentiment.get("negative_catalyst"),
                "headlines": sentiment.get("headlines", []),
                "asof": sentiment.get("asof"),
            },
            "price": metrics.get("price"),
            "turnover": metrics.get("turnover"),
            "distance_to_55d_high_pct": metrics.get("distance_to_55d_high_pct"),
            "day_high_distance_pct": metrics.get("day_high_distance_pct"),
            "volume_ratio": metrics.get("volume_ratio"),
            "atr_pct": metrics.get("atr_pct"),
            "history_candles": metrics.get("history_candles"),
        }

    def _forced_item(self, row: dict[str, Any], reason: str) -> dict[str, Any]:
        symbol = str(row.get("symbol") or "").upper()
        return {
            "symbol": symbol,
            "name": row.get("name") or symbol,
            "market_region": market_region_for_row(row),
            "score": 0.0,
            "bucket": "Watch",
            "setup": "position_risk_monitor",
            "forced_inclusion": True,
            "reasons": [reason, "open position included for exit/risk management"],
            "metrics": {},
            "components": {},
        }

    @staticmethod
    def _count(counts: dict[str, int], key: str) -> None:
        counts[key] = counts.get(key, 0) + 1

    @staticmethod
    def _counts(values: Any) -> dict[str, int]:
        counts: dict[str, int] = {}
        for value in values:
            key = str(value or "unknown")
            counts[key] = counts.get(key, 0) + 1
        return counts


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _return_pct(closes: list[float], lookback: int) -> float | None:
    if len(closes) <= lookback:
        return None
    previous = closes[-(lookback + 1)]
    current = closes[-1]
    return ((current - previous) / previous) * 100 if previous else None


def _atr_pct(highs: list[float], lows: list[float], closes: list[float], window: int = 14) -> float | None:
    if len(closes) <= window or len(highs) != len(closes) or len(lows) != len(closes):
        return None
    true_ranges = []
    for index in range(1, len(closes)):
        true_ranges.append(
            max(
                highs[index] - lows[index],
                abs(highs[index] - closes[index - 1]),
                abs(lows[index] - closes[index - 1]),
            )
        )
    atr = _mean(true_ranges[-window:]) if len(true_ranges) >= window else None
    return (atr / closes[-1]) * 100 if atr and closes[-1] else None


def _distance_pct(price: float, reference: float | None) -> float | None:
    if not price or not reference:
        return None
    return max(((reference - price) / price) * 100, 0.0)


def _signed_distance_pct(price: float, reference: float | None) -> float | None:
    if not price or not reference:
        return None
    return ((price - reference) / reference) * 100


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _round(value: float | None, digits: int = 4) -> float | None:
    return round(value, digits) if value is not None else None


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def _quote_age_seconds(quote: Quote) -> float | None:
    raw = getattr(quote, "asof", None)
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return round((datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds(), 3)
