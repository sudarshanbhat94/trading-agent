from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timezone
from typing import Any
from zoneinfo import ZoneInfo

from .india_top_gainers import build_playbook_dashboard, evaluate_indian_top_gainer_playbook
from .market_regions import market_region_for_row
from .models import Candle, Quote, utc_now
from .us_top_movers import build_us_playbook_dashboard, evaluate_us_top_mover_playbook


@dataclass(frozen=True)
class OpportunityScanResult:
    selected_universe: list[dict[str, Any]]
    candidates: list[dict[str, Any]]
    rejected_counts: dict[str, int]
    summary: dict[str, Any]


_ACTIVE_OPPORTUNITY_SETUPS = {
    "52_week_high_volume_breakout",
    "broker_re_rating_breakout",
    "circuit_demand_lock",
    "earnings_beat_gap_and_go",
    "market_action_momentum",
    "news_catalyst",
    "breakout_continuation",
    "btst_buy_candidate",
    "extended_momentum_watch",
    "opening_ignition",
    "price_shocker_reversal_breakout",
    "pre_rally_fuel",
    "top_gainer_momentum",
    "intraday_momentum",
    "intraday_momentum_probe",
    "near_breakout",
    "trend_momentum",
    "pullback_buy",
    "smallcap_momentum",
}

_LIVE_RALLY_SETUPS = {
    "opening_ignition",
    "intraday_momentum",
    "extended_momentum_watch",
}

_MARKET_ACTION_SETUPS = {
    "52_week_high_volume_breakout",
    "broker_re_rating_breakout",
    "circuit_demand_lock",
    "earnings_beat_gap_and_go",
    "market_action_momentum",
    "price_shocker_reversal_breakout",
    "top_gainer_momentum",
}

ACTIONABLE_WATCH = "ACTIONABLE_WATCH"
DATA_STALE_WATCH = "DATA_STALE_WATCH"
LATE_CHASE_AVOID = "LATE_CHASE_AVOID"


class OpportunityScanner:
    """Ranks a broad quote universe before the expensive strategy/LLM pass."""

    def __init__(self, settings: Any) -> None:
        self.settings = settings
        self.candidate_limit = max(1, int(getattr(settings, "dynamic_scan_candidate_limit", 60) or 60))
        self.min_score = _clamp(
            float(getattr(settings, "dynamic_scan_min_score", 0.58) or 0.0),
            0.0,
            1.0,
        )
        self.require_active_setup = bool(getattr(settings, "dynamic_scan_require_active_setup", True))
        self.min_price = max(0.0, float(getattr(settings, "dynamic_scan_min_price", 10.0) or 0.0))
        self.min_turnover_inr = max(0.0, float(getattr(settings, "dynamic_scan_min_turnover_inr", 50_000_000.0) or 0.0))
        self.min_turnover_usd = max(0.0, float(getattr(settings, "dynamic_scan_min_turnover_usd", 2_000_000.0) or 0.0))
        self.min_turnover = self.min_turnover_inr
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
        top_gainers_playbook_records: list[dict[str, Any]] = []
        rs_context_by_symbol = self._relative_strength_context(candle_sets)

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
                rs_context_by_symbol.get(symbol) or {},
            )
            playbook = item.get("top_gainers_playbook") if isinstance(item.get("top_gainers_playbook"), dict) else {}
            if playbook.get("available"):
                top_gainers_playbook_records.append(playbook)
            if item["rejected"] and not in_position:
                self._count(rejected_counts, item["reject_reason"])
                continue
            if in_position and item["rejected"]:
                item["forced_inclusion"] = True
                item["reasons"].append("open position included for exit/risk management")
            if not in_position:
                quality_reject_reason = self._quality_reject_reason(item)
                if quality_reject_reason:
                    self._count(rejected_counts, quality_reject_reason)
                    continue
            scored.append(item)

        scored.sort(key=lambda item: item["score"], reverse=True)
        selected_items = self._select_rally_radar_then_diverse(scored, self.candidate_limit)
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
        candidates: list[dict[str, Any]] = []
        selected_universe: list[dict[str, Any]] = []
        for rank, item in enumerate(selected_items, start=1):
            public_item = self._public_item(item)
            public_item["rank"] = rank
            candidates.append(public_item)
            row = row_by_symbol.get(item["symbol"])
            if row:
                selected_universe.append(
                    {
                        **row,
                        "_opportunity_rank": rank,
                        "_opportunity_score": public_item.get("score"),
                        "_opportunity_scan": public_item,
                    }
                )
        news_covered_candidates = sum(
            1
            for item in selected_items
            if int((item.get("sentiment") or {}).get("headline_count") or 0) > 0
            or int((item.get("sentiment") or {}).get("event_count") or 0) > 0
        )
        verified_catalyst_candidates = sum(
            1
            for item in selected_items
            if ((item.get("sentiment") or {}).get("positive_catalyst"))
        )
        summary = {
            "enabled": True,
            "mode": "dynamic_opportunity_scan",
            "scanned_at": utc_now(),
            "raw_symbols": len(universe),
            "quoted_symbols": len(quotes),
            "candidate_limit": self.candidate_limit,
            "selected_symbols": len(selected_universe),
            "tradeable_screening_symbols": sum(
                1 for item in scored if (item.get("data_quality") or {}).get("tradeable_screening")
            ),
            "forced_position_symbols": [item["symbol"] for item in selected_items if item.get("forced_inclusion")],
            "rejected_counts": rejected_counts,
            "top_candidates": candidates,
            "top_fast_movers": [
                item
                for item in candidates
                if item.get("setup") == "intraday_momentum"
                or item.get("setup") == "opening_ignition"
                or item.get("setup") == "extended_momentum_watch"
                or float(((item.get("components") or {}).get("live_momentum")) or 0.0) >= 0.70
            ][:12],
            "top_rally_radar": [
                item
                for item in candidates
                if item.get("rally_phase") and item.get("rally_phase") != "none"
            ][:25],
            "top_market_action": [
                item
                for item in candidates
                if (item.get("market_action") or {}).get("available")
            ][:25],
            "btst_buy_candidates": [
                item
                for item in candidates
                if item.get("setup") == "btst_buy_candidate" and (item.get("btst") or {}).get("detected")
            ][:20],
            "setup_counts": self._counts(item.get("setup") for item in selected_items),
            "bucket_counts": self._counts(item.get("bucket") for item in selected_items),
            "news_covered_candidates": news_covered_candidates,
            "verified_catalyst_candidates": verified_catalyst_candidates,
            "positive_news_candidates": verified_catalyst_candidates,
            "negative_news_filtered": rejected_counts.get("negative_news_catalyst", 0),
            "filters": {
                "min_price": self.min_price,
                "min_turnover_inr": self.min_turnover_inr,
                "min_turnover_usd": self.min_turnover_usd,
                "min_score": self.min_score,
                "require_active_setup": self.require_active_setup,
                "active_setups": sorted(_ACTIVE_OPPORTUNITY_SETUPS),
                "breakout_distance_pct": self.breakout_distance_pct,
                "sentiment_enabled": self.sentiment_enabled,
                "sentiment_weight": self.sentiment_weight,
            },
            "top_gainers_playbook": _combined_playbook_dashboard(top_gainers_playbook_records),
            "top_gainers_playbook_by_market": {
                "IN": build_playbook_dashboard(
                    [item for item in top_gainers_playbook_records if str(item.get("market_region") or "IN").upper() == "IN"]
                ),
                "US": build_us_playbook_dashboard(
                    [item for item in top_gainers_playbook_records if str(item.get("market_region") or "").upper() == "US"]
                ),
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
        rs_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        symbol = str(row.get("symbol") or "").upper()
        market_region = market_region_for_row(row)
        min_turnover = self._min_turnover_for_market(market_region)
        price = max(float(quote.price or 0.0), 0.0)
        candles = candle_set.get("analysis") or candle_set.get("daily") or candle_set.get("intraday") or []
        metrics = self._metrics(price, quote, candles, market_region)
        metrics["candle_freshness"] = self._candle_freshness(candle_set, quote, market_region)
        sentiment = self._sentiment_metrics(sentiment_detail or {})
        liquidity_profile = self._liquidity_profile(metrics, min_turnover)
        market_action = row.get("_market_action") if isinstance(row.get("_market_action"), dict) else {}
        reasons: list[str] = []
        reject_reason = ""

        if price <= 0:
            reject_reason = "invalid_price"
        elif price < self.min_price:
            reject_reason = "below_min_price"
        elif not liquidity_profile["screening_pass"] and not in_position:
            reject_reason = liquidity_profile["reject_reason"]
        elif sentiment["negative_catalyst"] and not in_position:
            reject_reason = "negative_news_catalyst"

        liquidity = float(liquidity_profile["score"])
        trend = self._trend_score(price, metrics)
        breakout = self._breakout_score(price, quote, metrics)
        momentum = self._momentum_score(metrics)
        live_momentum = self._live_momentum_score(metrics, min_turnover)
        volume = self._volume_score(metrics)
        risk = self._risk_score(metrics)
        rally = self._rally_radar(metrics, trend, breakout, momentum, live_momentum, volume, sentiment, min_turnover)
        market_action_review = self._market_action_review(market_action, metrics, sentiment, min_turnover)
        btst = self._btst_review(
            row,
            quote,
            candles,
            metrics,
            trend,
            breakout,
            momentum,
            volume,
            sentiment,
            rs_context or {},
            min_turnover,
        )
        top_gainers_playbook = (
            evaluate_us_top_mover_playbook(
                row=row,
                quote=quote,
                candles=candles,
                market_action=market_action,
                sentiment=sentiment,
                rs_context=rs_context or {},
            )
            if market_region == "US"
            else evaluate_indian_top_gainer_playbook(
                row=row,
                quote=quote,
                candles=candles,
                market_action=market_action,
                sentiment=sentiment,
                rs_context=rs_context or {},
            )
        )
        data_quality = self._data_quality(row, quote, metrics, sentiment)
        if data_quality["reject_reason"] and not in_position and not reject_reason:
            if self._allow_live_rally_probe(data_quality, rally, metrics, min_turnover):
                data_quality = {
                    **data_quality,
                    "reject_reason": "",
                    "tradeable_screening": False,
                    "actionable_data_ready": False,
                    "probe_only": True,
                }
                reasons.append("live rally radar selected for history prefetch")
            else:
                reject_reason = data_quality["reject_reason"]
        base_score = (
            liquidity * 0.14
            + trend * 0.16
            + breakout * 0.18
            + momentum * 0.11
            + live_momentum * 0.17
            + volume * 0.10
            + risk * 0.06
            + data_quality["score"] * 0.08
        )
        catalyst_boost = 0.06 if sentiment["positive_catalyst"] else 0.0
        score = _clamp(
            base_score
            + sentiment["boost"]
            + catalyst_boost
            + rally["score_boost"]
            + market_action_review["score_boost"]
            + btst["score_boost"]
            + self._live_momentum_boost(metrics, live_momentum, min_turnover),
            0.0,
            1.0,
        )
        rally_floor = self._live_rally_score_floor(rally, liquidity_profile, metrics)
        if rally_floor > score:
            score = rally_floor
        market_action_floor = self._market_action_score_floor(market_action_review, liquidity_profile, metrics)
        if market_action_floor > score:
            score = market_action_floor
        if btst.get("detected"):
            score = max(score, float(btst.get("score") or 0.0))

        reasons.extend(rally.get("reasons", []))
        reasons.extend(market_action_review.get("reasons", []))
        reasons.extend(btst.get("reasons", []))
        if top_gainers_playbook.get("available"):
            reasons.append(
                f"top gainers playbook: {top_gainers_playbook.get('final_signal')} "
                f"with quant {top_gainers_playbook.get('quant_score')}/100"
            )
            signal = str(top_gainers_playbook.get("final_signal") or "")
            quant_score = float(top_gainers_playbook.get("quant_score") or 0.0) / 100.0
            if signal == "STRONG BUY":
                score = max(score, min(0.82, max(quant_score, 0.72)))
            elif signal == "MODERATE BUY":
                score = max(score, min(0.74, max(quant_score, 0.64)))
            elif signal == "WATCH":
                score = max(score, min(0.62, max(quant_score, 0.52)))
        if metrics["distance_to_55d_high_pct"] is not None and metrics["distance_to_55d_high_pct"] <= self.breakout_distance_pct:
            reasons.append(f"near 55D high ({metrics['distance_to_55d_high_pct']:.1f}% away)")
        elif metrics["day_high_distance_pct"] is not None and metrics["day_high_distance_pct"] <= 1.0:
            reasons.append(f"near day high ({metrics['day_high_distance_pct']:.1f}% away)")
        if live_momentum >= 0.70:
            reasons.append(f"live fast mover ({float(metrics.get('day_gain_pct') or 0.0):+.1f}% from open)")
        if liquidity_profile.get("reason"):
            reasons.append(str(liquidity_profile["reason"]))
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
        if int(metrics.get("history_candles") or 0) < 20:
            reasons.append("daily history not loaded yet; intraday probe only")
        if data_quality["missing"]:
            reasons.append(f"data gaps: {', '.join(data_quality['missing'][:3])}")
        if risk < 0.35:
            reasons.append("risk/reward needs caution")

        setup = self._setup(metrics, trend, breakout, momentum, volume, sentiment, rally, min_turnover, market_action_review)
        playbook_strategy = top_gainers_playbook.get("strategy_match") if isinstance(top_gainers_playbook.get("strategy_match"), dict) else {}
        playbook_signal = str(top_gainers_playbook.get("final_signal") or "")
        if playbook_signal in {"STRONG BUY", "MODERATE BUY"} and playbook_strategy.get("code"):
            setup = str(playbook_strategy["code"])
        elif btst.get("detected"):
            setup = "btst_buy_candidate"
        late_chase = self._late_chase(metrics, market_action_review)
        bucket = self._bucket(score, risk, reject_reason, metrics)
        if not reject_reason and btst.get("detected") and float(btst.get("score") or 0.0) >= 0.72 and risk >= 0.45:
            bucket = "Actionable"
        if late_chase:
            bucket = LATE_CHASE_AVOID
            if setup != "circuit_demand_lock":
                setup = "extended_momentum_watch"
            reasons.append("late chase avoid: move is already too extended for a fresh entry")
        elif not reject_reason and playbook_signal == "STRONG BUY":
            bucket = "Actionable"
        elif not reject_reason and playbook_signal == "MODERATE BUY":
            bucket = "Small Size Only"
        elif not reject_reason and playbook_signal == "WATCH" and bucket == "Avoid":
            bucket = "Watch"
        if bucket == "Avoid" and not reject_reason and self._good_mover_watchable(market_action_review, rally, metrics):
            bucket = ACTIONABLE_WATCH
            reasons.append("good mover visible as actionable watch; entry gates still need confirmation")
        return {
            "symbol": symbol,
            "name": row.get("name") or symbol,
            "market_region": market_region,
            "sector": row.get("sector") or "",
            "industry": row.get("industry") or "",
            "exchange": row.get("exchange") or "",
            "score": round(score, 4),
            "bucket": bucket,
            "setup": setup,
            "rejected": bool(reject_reason),
            "reject_reason": reject_reason,
            "forced_inclusion": bool(in_position),
            "reasons": reasons or ["quote/liquidity pass"],
            "metrics": metrics,
            "sentiment": sentiment,
            "rally_radar": rally,
            "market_action": market_action_review,
            "btst": btst,
            "top_gainers_playbook": top_gainers_playbook,
            "data_quality": data_quality,
            "liquidity_profile": liquidity_profile,
            "late_chase": late_chase,
            "actionable_watch": bucket == ACTIONABLE_WATCH,
            "components": {
                "liquidity": round(liquidity, 4),
                "trend": round(trend, 4),
                "breakout": round(breakout, 4),
                "momentum": round(momentum, 4),
                "live_momentum": round(live_momentum, 4),
                "rally_radar": round(rally["score"], 4),
                "btst": round(float(btst.get("score") or 0.0), 4),
                "volume": round(volume, 4),
                "risk": round(risk, 4),
                "data_quality": round(data_quality["score"], 4),
                "sentiment": round(sentiment["boost"], 4),
            },
        }

    def _min_turnover_for_market(self, market_region: str) -> float:
        return self.min_turnover_usd if str(market_region or "").upper() == "US" else self.min_turnover_inr

    def _metrics(self, price: float, quote: Quote, candles: list[Candle], market_region: str) -> dict[str, Any]:
        closes = [float(item.close) for item in candles if item.close is not None and float(item.close) > 0]
        highs = [float(item.high) for item in candles if item.high is not None and float(item.high) > 0]
        lows = [float(item.low) for item in candles if item.low is not None and float(item.low) > 0]
        volumes = [float(item.volume or 0.0) for item in candles]
        last_volume = float(quote.volume or 0.0) or (volumes[-1] if volumes else 0.0)
        avg20_volume = _mean([value for value in volumes[-20:] if value > 0])
        avg20_turnover = _mean(
            [
                float(item.close) * float(item.volume or 0.0)
                for item in candles[-20:]
                if item.close is not None and float(item.close) > 0 and float(item.volume or 0.0) > 0
            ]
        )
        turnover = price * (last_volume or avg20_volume or 0.0)
        if turnover <= 0 and avg20_volume > 0:
            turnover = price * avg20_volume
        session_progress = _session_progress_pct(quote, market_region)
        projection_divisor = max(session_progress or 1.0, 0.08)

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
        projected_volume_ratio = (volume_ratio / projection_divisor) if volume_ratio is not None and session_progress is not None else volume_ratio
        projected_turnover = (turnover / projection_divisor) if session_progress is not None else turnover
        open_price = _float_or_none(quote.open)
        day_gain_pct = _signed_distance_pct(price, open_price)
        day_range_pct = ((day_high - day_low) / open_price) * 100 if day_high and day_low and open_price else None
        return {
            "price": round(price, 4),
            "open": _round(open_price),
            "day_gain_pct": _round(day_gain_pct),
            "day_range_pct": _round(day_range_pct),
            "history_candles": len(candles),
            "turnover": round(turnover, 2),
            "avg20_turnover": round(avg20_turnover, 2),
            "projected_turnover": round(projected_turnover, 2),
            "last_volume": round(last_volume, 2),
            "avg20_volume": round(avg20_volume, 2),
            "volume_ratio": _round(volume_ratio),
            "projected_volume_ratio": _round(projected_volume_ratio),
            "session_progress_pct": _round(session_progress),
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

    def _liquidity_score(self, turnover: float, min_turnover: float) -> float:
        if min_turnover <= 0:
            return 0.75
        return _clamp(turnover / (min_turnover * 2.0), 0.0, 1.0)

    def _liquidity_profile(self, metrics: dict[str, Any], min_turnover: float) -> dict[str, Any]:
        turnover = _float_or_none(metrics.get("turnover")) or 0.0
        projected_turnover = _float_or_none(metrics.get("projected_turnover")) or turnover
        volume_ratio = _float_or_none(metrics.get("volume_ratio")) or 0.0
        projected_volume_ratio = _float_or_none(metrics.get("projected_volume_ratio")) or volume_ratio
        day_gain = _float_or_none(metrics.get("day_gain_pct")) or 0.0
        range_position = _float_or_none(metrics.get("day_range_position")) or 0.0
        high_distance = _float_or_none(metrics.get("day_high_distance_pct"))
        participation_ratio = max(volume_ratio, projected_volume_ratio * 0.75)
        absolute_ratio = turnover / min_turnover if min_turnover > 0 else 1.0
        projected_ratio = projected_turnover / min_turnover if min_turnover > 0 else 1.0
        near_high = high_distance is None or high_distance <= 2.0

        absolute_pass = absolute_ratio >= 1.0
        adaptive_pass = (
            projected_ratio >= 0.85
            and participation_ratio >= 0.90
        ) or (
            projected_ratio >= 0.45
            and participation_ratio >= 1.35
            and day_gain >= 1.5
            and range_position >= 0.60
        ) or (
            absolute_ratio >= 0.30
            and participation_ratio >= 1.75
            and day_gain >= 2.0
            and range_position >= 0.68
            and near_high
        )
        screening_pass = absolute_pass or adaptive_pass
        score = _clamp(
            max(
                self._liquidity_score(turnover, min_turnover),
                projected_ratio / 2.5,
            )
            * 0.65
            + _clamp(participation_ratio / 3.0, 0.0, 1.0) * 0.35,
            0.0,
            1.0,
        )
        reason = ""
        if absolute_pass:
            reason = "liquidity ok by traded value"
        elif adaptive_pass:
            reason = "adaptive liquidity ok: relative participation is building"
        return {
            "screening_pass": screening_pass,
            "reject_reason": "" if screening_pass else "below_adaptive_liquidity",
            "score": round(score, 4),
            "absolute_turnover_ratio": round(absolute_ratio, 4),
            "projected_turnover_ratio": round(projected_ratio, 4),
            "participation_ratio": round(participation_ratio, 4),
            "absolute_pass": absolute_pass,
            "adaptive_pass": adaptive_pass,
            "min_turnover": min_turnover,
            "reason": reason,
        }

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
        ]
        valid = [float(value) for value in distances if value is not None]
        if not valid:
            day_pos = float(metrics.get("day_range_position") or 0.0)
            return _clamp(0.25 + min(day_pos, 1.0) * 0.15, 0.0, 0.40)
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

    def _live_momentum_score(self, metrics: dict[str, Any], min_turnover: float) -> float:
        day_gain = _float_or_none(metrics.get("day_gain_pct"))
        if day_gain is None:
            return 0.0
        range_position = _clamp(float(metrics.get("day_range_position") or 0.0), 0.0, 1.0)
        high_distance = _float_or_none(metrics.get("day_high_distance_pct"))
        volume_ratio = _float_or_none(metrics.get("volume_ratio"))
        projected_volume_ratio = _float_or_none(metrics.get("projected_volume_ratio"))
        turnover = float(metrics.get("turnover") or 0.0)
        projected_turnover = float(metrics.get("projected_turnover") or turnover)
        gain_score = _clamp((day_gain - 2.0) / 6.0, 0.0, 1.0)
        range_score = _clamp((range_position - 0.55) / 0.35, 0.0, 1.0)
        high_score = 1.0 - _clamp(float(high_distance if high_distance is not None else 10.0) / 2.0, 0.0, 1.0)
        participation = max(volume_ratio or 0.0, (projected_volume_ratio or 0.0) * 0.75)
        volume_score = _clamp((participation - 1.0) / 2.5, 0.0, 1.0)
        turnover_score = max(
            _clamp(turnover / max(min_turnover * 6.0, 1.0), 0.0, 1.0),
            _clamp(projected_turnover / max(min_turnover * 10.0, 1.0), 0.0, 1.0),
        )
        return _clamp(
            gain_score * 0.35
            + range_score * 0.25
            + high_score * 0.15
            + max(volume_score, turnover_score * 0.75) * 0.25,
            0.0,
            1.0,
        )

    def _live_momentum_boost(self, metrics: dict[str, Any], live_momentum: float, min_turnover: float) -> float:
        day_gain = _float_or_none(metrics.get("day_gain_pct")) or 0.0
        volume_ratio = _float_or_none(metrics.get("volume_ratio")) or 0.0
        projected_volume_ratio = _float_or_none(metrics.get("projected_volume_ratio")) or volume_ratio
        range_position = _float_or_none(metrics.get("day_range_position")) or 0.0
        projected_turnover = _float_or_none(metrics.get("projected_turnover")) or _float_or_none(metrics.get("turnover")) or 0.0
        participation = max(volume_ratio, projected_volume_ratio * 0.75)
        if live_momentum >= 0.82 and day_gain >= 5.0 and range_position >= 0.70:
            return 0.10
        if live_momentum >= 0.70 and day_gain >= 4.0 and (participation >= 1.2 or projected_turnover >= min_turnover * 4.0):
            return 0.06
        return 0.0

    def _live_rally_score_floor(
        self,
        rally: dict[str, Any],
        liquidity_profile: dict[str, Any],
        metrics: dict[str, Any],
    ) -> float:
        if rally.get("phase") not in _LIVE_RALLY_SETUPS:
            return 0.0
        if not liquidity_profile.get("screening_pass"):
            return 0.0
        rally_score = _float_or_none(rally.get("score")) or 0.0
        if rally_score < 0.68:
            return 0.0
        day_gain = _float_or_none(metrics.get("day_gain_pct")) or 0.0
        range_position = _float_or_none(metrics.get("day_range_position")) or 0.0
        high_distance = _float_or_none(metrics.get("day_high_distance_pct"))
        if day_gain < 1.5 or range_position < 0.68:
            return 0.0
        if high_distance is not None and high_distance > 2.0 and day_gain < 5.0:
            return 0.0
        return _clamp(max(self.min_score + 0.02, rally_score * 0.86), 0.0, 0.78)

    def _market_action_review(
        self,
        market_action: dict[str, Any],
        metrics: dict[str, Any],
        sentiment: dict[str, Any],
        min_turnover: float,
    ) -> dict[str, Any]:
        if not market_action:
            return {
                "available": False,
                "setup": "",
                "score": 0.0,
                "score_boost": 0.0,
                "trade_window": "",
                "reasons": [],
                "event_types": [],
            }
        event_types = [str(item).upper() for item in market_action.get("event_types", []) if str(item or "").strip()]
        strategy = str(market_action.get("strategy") or "").strip().lower()
        day_gain = _float_or_none(metrics.get("day_gain_pct"))
        if day_gain is None:
            day_gain = _float_or_none(market_action.get("pct_change")) or 0.0
        high_distance = _float_or_none(metrics.get("day_high_distance_pct"))
        range_position = _float_or_none(metrics.get("day_range_position")) or 0.0
        dist55 = _float_or_none(metrics.get("distance_to_55d_high_pct"))
        dist252 = _float_or_none(metrics.get("distance_to_252d_high_pct"))
        volume_ratio = _float_or_none(metrics.get("volume_ratio")) or _float_or_none(market_action.get("volume_multiplier")) or 0.0
        projected_volume_ratio = _float_or_none(metrics.get("projected_volume_ratio")) or volume_ratio
        turnover = _float_or_none(metrics.get("turnover")) or 0.0
        projected_turnover = _float_or_none(metrics.get("projected_turnover")) or turnover
        dist_sma20 = _float_or_none(metrics.get("distance_to_sma20_pct"))
        participation = max(volume_ratio, projected_volume_ratio * 0.75)
        near_high = high_distance is None or high_distance <= 2.0
        near_breakout = (
            dist55 is not None and dist55 <= 3.0
        ) or (
            dist252 is not None and dist252 <= 3.0
        ) or "52_WEEK_HIGH" in event_types or "ALL_TIME_HIGH" in event_types
        volume_confirmed = participation >= 1.25 or projected_turnover >= min_turnover * 1.5 or "VOLUME_SHOCKER" in event_types
        extended = day_gain >= 7.0 or (dist_sma20 is not None and dist_sma20 > 12.0)
        setup = strategy if strategy in _MARKET_ACTION_SETUPS else "market_action_momentum"
        trade_window = str(market_action.get("trade_window") or "confirm_before_entry")
        news_event_types = {
            str(event.get("event_type") or "").lower()
            for event in sentiment.get("events", [])
            if isinstance(event, dict)
        }
        if "ONLY_BUYERS" in event_types or setup == "circuit_demand_lock":
            setup = "circuit_demand_lock"
            trade_window = "watch_for_pullback"
        elif sentiment.get("positive_catalyst") and "earnings" in news_event_types and volume_confirmed:
            setup = "earnings_beat_gap_and_go"
            trade_window = "actionable_if_vwap_holds"
        elif sentiment.get("positive_catalyst") and news_event_types & {"analyst_upgrade", "order_win", "guidance"} and volume_confirmed:
            setup = "broker_re_rating_breakout"
            trade_window = "actionable_if_vwap_holds"
        elif near_breakout and volume_confirmed:
            setup = "52_week_high_volume_breakout"
            trade_window = "actionable_if_vwap_holds" if not extended else "wait_for_pullback"
        elif extended:
            setup = "extended_momentum_watch"
            trade_window = "wait_for_pullback"
        score = _clamp(float(market_action.get("market_action_score") or 0.0) / 100.0, 0.0, 1.0)
        if near_high:
            score += 0.04
        if volume_confirmed:
            score += 0.06
        if sentiment.get("positive_catalyst"):
            score += 0.04
        score = _clamp(score, 0.0, 1.0)
        score_boost = _clamp(0.04 + score * 0.08, 0.0, 0.12)
        if setup == "circuit_demand_lock":
            score_boost = min(score_boost, 0.07)
        reasons = [
            f"market action radar: {market_action.get('reason') or ', '.join(event_types[:3])}",
        ]
        if setup == "circuit_demand_lock":
            reasons.append("demand locked; wait for tradable pullback")
        elif volume_confirmed and near_breakout:
            reasons.append("external breakout/volume event confirmed by price action")
        return {
            "available": True,
            "setup": setup,
            "score": round(score, 4),
            "score_boost": score_boost,
            "trade_window": trade_window,
            "reasons": reasons,
            "event_types": event_types,
            "raw": market_action,
            "evidence": {
                "near_breakout": near_breakout,
                "volume_confirmed": volume_confirmed,
                "extended": extended,
                "day_gain_pct": _round(day_gain),
                "volume_ratio": _round(volume_ratio),
                "projected_volume_ratio": _round(projected_volume_ratio),
                "turnover": round(turnover, 2),
                "projected_turnover": round(projected_turnover, 2),
                "range_position": _round(range_position),
                "day_high_distance_pct": _round(high_distance),
                "distance_to_55d_high_pct": _round(dist55),
                "distance_to_252d_high_pct": _round(dist252),
            },
        }

    def _market_action_score_floor(
        self,
        market_action_review: dict[str, Any],
        liquidity_profile: dict[str, Any],
        metrics: dict[str, Any],
    ) -> float:
        if not market_action_review.get("available") or not liquidity_profile.get("screening_pass"):
            return 0.0
        setup = str(market_action_review.get("setup") or "")
        score = _float_or_none(market_action_review.get("score")) or 0.0
        if score < 0.55:
            return 0.0
        if setup == "circuit_demand_lock":
            return max(self.min_score + 0.01, min(0.66, score * 0.76))
        if setup in {"52_week_high_volume_breakout", "earnings_beat_gap_and_go", "broker_re_rating_breakout", "market_action_momentum", "top_gainer_momentum"}:
            day_gain = _float_or_none(metrics.get("day_gain_pct")) or 0.0
            high_distance = _float_or_none(metrics.get("day_high_distance_pct"))
            if day_gain >= 1.0 and (high_distance is None or high_distance <= 3.0):
                return max(self.min_score + 0.03, min(0.78, score * 0.84))
        return 0.0

    def _btst_review(
        self,
        row: dict[str, Any],
        quote: Quote,
        candles: list[Candle],
        metrics: dict[str, Any],
        trend: float,
        breakout: float,
        momentum: float,
        volume: float,
        sentiment: dict[str, Any],
        rs_context: dict[str, Any],
        min_turnover: float,
    ) -> dict[str, Any]:
        price = _float_or_none(metrics.get("price")) or float(quote.price or 0.0)
        atr_pct = _float_or_none(metrics.get("atr_pct")) or 3.0
        day_gain = _float_or_none(metrics.get("day_gain_pct"))
        range_position = _float_or_none(metrics.get("day_range_position")) or 0.0
        high_distance = _float_or_none(metrics.get("day_high_distance_pct"))
        volume_ratio = _float_or_none(metrics.get("volume_ratio")) or 0.0
        projected_volume_ratio = _float_or_none(metrics.get("projected_volume_ratio")) or volume_ratio
        turnover = _float_or_none(metrics.get("turnover")) or 0.0
        projected_turnover = _float_or_none(metrics.get("projected_turnover")) or turnover
        dist_sma20 = _float_or_none(metrics.get("distance_to_sma20_pct"))
        dist_sma50 = _float_or_none(metrics.get("distance_to_sma50_pct"))
        dist_high = min(
            [
                value
                for value in (
                    _float_or_none(metrics.get("distance_to_20d_high_pct")),
                    _float_or_none(metrics.get("distance_to_55d_high_pct")),
                )
                if value is not None
            ],
            default=None,
        )
        return_5d = _float_or_none(metrics.get("return_5d_pct")) or 0.0
        history = int(metrics.get("history_candles") or 0)
        rs_rank = _float_or_none(rs_context.get("rs_rank") or rs_context.get("percentile_63"))
        rs_improving = bool(rs_context.get("improving")) or (rs_rank is not None and rs_rank >= 70.0)
        participation = max(volume_ratio, projected_volume_ratio * 0.75)
        liquidity_ok = turnover >= min_turnover or projected_turnover >= min_turnover * 1.5
        trend_ok = trend >= 0.66 and momentum >= 0.55
        range_ok = range_position >= 0.72 and (high_distance is None or high_distance <= 1.20)
        day_move_ok = day_gain is not None and 0.60 <= day_gain <= 4.80
        not_extended = (dist_sma20 is None or -1.5 <= dist_sma20 <= 8.5) and return_5d <= 12.0
        breakout_zone = breakout >= 0.58 or (dist_high is not None and dist_high <= 4.0)
        volume_ok = participation >= 1.10 or volume >= 0.60
        overnight_risk_ok = atr_pct <= 6.5 and not (day_gain is not None and day_gain >= 5.0 and participation >= 4.5)
        sentiment_ok = not sentiment.get("negative_catalyst")
        close_strength = _close_strength_from_candles(candles, price)
        latest_close_position = _float_or_none(close_strength.get("latest_close_position"))
        prior_day_follow_through = latest_close_position is not None and latest_close_position >= 0.60

        checks = {
            "history_ok": history >= 55,
            "liquidity_ok": liquidity_ok,
            "trend_ok": trend_ok,
            "range_ok": range_ok,
            "day_move_ok": day_move_ok,
            "not_extended": not_extended,
            "breakout_zone": breakout_zone,
            "volume_ok": volume_ok,
            "overnight_risk_ok": overnight_risk_ok,
            "sentiment_ok": sentiment_ok,
            "relative_strength_ok": rs_improving or rs_rank is None,
            "prior_close_strength_ok": prior_day_follow_through or history < 80,
        }
        score = (
            (1.0 if checks["history_ok"] else 0.0) * 0.07
            + (1.0 if liquidity_ok else 0.0) * 0.10
            + trend * 0.14
            + breakout * 0.13
            + momentum * 0.11
            + _clamp((range_position - 0.55) / 0.35, 0.0, 1.0) * 0.12
            + _clamp((participation - 0.8) / 2.2, 0.0, 1.0) * 0.12
            + (0.08 if not_extended else 0.0)
            + (0.06 if overnight_risk_ok else 0.0)
            + (0.05 if rs_improving else 0.0)
            + (0.05 if sentiment.get("positive_catalyst") else 0.0)
        )
        score = _clamp(score, 0.0, 1.0)
        detected = bool(score >= 0.70 and all(checks[key] for key in (
            "history_ok",
            "liquidity_ok",
            "trend_ok",
            "range_ok",
            "day_move_ok",
            "not_extended",
            "breakout_zone",
            "volume_ok",
            "overnight_risk_ok",
            "sentiment_ok",
        )))
        stop_risk_pct = _clamp(max(2.2, min(5.5, atr_pct * 0.85)), 2.0, 6.0)
        target_pct = _clamp(max(2.4, min(6.0, atr_pct * 1.05)), 2.0, 7.0)
        max_entry_pct = 1.2 if day_gain is not None and day_gain <= 3.5 else 0.8
        entry_low = price * 0.995 if price else None
        entry_high = price * (1 + max_entry_pct / 100.0) if price else None
        stop = price * (1 - stop_risk_pct / 100.0) if price else None
        target1 = price * (1 + target_pct / 100.0) if price else None
        reasons: list[str] = []
        if detected:
            reasons.append("BTST buy candidate: closing near high with controlled overnight risk")
        if range_ok:
            reasons.append("holds upper part of the day range")
        if volume_ok:
            reasons.append("volume participation supports next-day follow-through")
        if trend_ok:
            reasons.append("trend and momentum are aligned")
        if rs_improving:
            reasons.append("relative strength is improving")
        if sentiment.get("positive_catalyst"):
            reasons.append("positive news/catalyst improves next-day odds")
        if not sentiment_ok:
            reasons.append("negative news blocks BTST buy")
        if not overnight_risk_ok:
            reasons.append("overnight gap risk too high for BTST")
        return {
            "detected": detected,
            "score": round(score, 4),
            "confidence": round(_clamp(0.45 + score * 0.45, 0.0, 0.92), 4),
            "score_boost": 0.08 if detected else 0.0,
            "setup": "btst_buy_candidate" if detected else "",
            "action_bias": "BUY" if detected else "WAIT",
            "next_day_bias": "positive_follow_through" if detected else "not_ready",
            "holding_period": "BTST",
            "entry_zone": {"low": _round(entry_low), "high": _round(entry_high)},
            "max_entry": _round(entry_high),
            "stop_loss": _round(stop),
            "target1": _round(target1),
            "exit_plan": "Sell tomorrow into first strength or trail below first 15-minute low; do not average down overnight.",
            "reasons": reasons,
            "checks": checks,
            "evidence": {
                "day_gain_pct": _round(day_gain),
                "day_range_position": _round(range_position),
                "day_high_distance_pct": _round(high_distance),
                "volume_ratio": _round(volume_ratio),
                "projected_volume_ratio": _round(projected_volume_ratio),
                "turnover": round(turnover, 2),
                "projected_turnover": round(projected_turnover, 2),
                "distance_to_near_high_pct": _round(dist_high),
                "distance_to_sma20_pct": _round(dist_sma20),
                "distance_to_sma50_pct": _round(dist_sma50),
                "atr_pct": _round(atr_pct),
                "return_5d_pct": _round(return_5d),
                "rs_rank": _round(rs_rank),
                "latest_close_position": _round(close_strength.get("latest_close_position")),
                "sector": row.get("sector") or "",
            },
        }

    def _rally_radar(
        self,
        metrics: dict[str, Any],
        trend: float,
        breakout: float,
        momentum: float,
        live_momentum: float,
        volume: float,
        sentiment: dict[str, Any],
        min_turnover: float,
    ) -> dict[str, Any]:
        day_gain = _float_or_none(metrics.get("day_gain_pct"))
        range_position = _float_or_none(metrics.get("day_range_position"))
        high_distance = _float_or_none(metrics.get("day_high_distance_pct"))
        volume_ratio = _float_or_none(metrics.get("volume_ratio")) or 0.0
        projected_volume_ratio = _float_or_none(metrics.get("projected_volume_ratio")) or volume_ratio
        turnover = float(metrics.get("turnover") or 0.0)
        projected_turnover = float(metrics.get("projected_turnover") or turnover)
        dist20 = _float_or_none(metrics.get("distance_to_20d_high_pct"))
        dist55 = _float_or_none(metrics.get("distance_to_55d_high_pct"))
        dist_high = min([value for value in (dist20, dist55) if value is not None], default=None)
        dist_sma20 = _float_or_none(metrics.get("distance_to_sma20_pct"))
        return_5d = _float_or_none(metrics.get("return_5d_pct")) or 0.0
        return_20d = _float_or_none(metrics.get("return_20d_pct")) or 0.0
        reasons: list[str] = []
        phase = "none"
        trade_window = "not_ready"
        score = 0.0
        score_boost = 0.0

        near_high = high_distance is not None and high_distance <= 1.5
        near_pivot = dist_high is not None and dist_high <= 7.0
        constructive_trend = trend >= 0.65 or momentum >= 0.62
        participation = max(volume_ratio, projected_volume_ratio * 0.75)
        volume_support = participation >= 1.15 or volume >= 0.65 or turnover >= min_turnover * 3.0 or projected_turnover >= min_turnover * 3.0
        not_too_extended = dist_sma20 is None or dist_sma20 <= 10.0

        if day_gain is not None:
            if (
                day_gain >= 1.5
                and day_gain < 4.0
                and (range_position or 0.0) >= 0.68
                and near_high
                and volume_support
                and (turnover >= min_turnover * 0.75 or projected_turnover >= min_turnover * 1.5)
            ):
                phase = "opening_ignition"
                trade_window = "early_actionable"
                score = _clamp(0.68 + live_momentum * 0.22 + min(max(day_gain - 1.5, 0.0), 2.5) * 0.03, 0.0, 1.0)
                score_boost = 0.12
                reasons.append(f"opening ignition ({day_gain:+.1f}% with volume)")
            elif (
                day_gain >= 4.0
                and (range_position or 0.0) >= (0.60 if day_gain >= 5.0 and volume_ratio >= 2.0 else 0.70)
                and high_distance is not None
                and high_distance <= (4.0 if day_gain >= 5.0 and volume_ratio >= 2.0 else 2.0)
                and (turnover >= min_turnover * 0.75 or projected_turnover >= min_turnover * 1.5)
                and volume_support
            ):
                late_chase = day_gain >= 7.0 or (dist_sma20 is not None and dist_sma20 > 12.0)
                phase = "extended_momentum_watch" if late_chase else "intraday_momentum"
                trade_window = "wait_for_pullback" if late_chase else "actionable_momentum"
                score = _clamp(0.70 + live_momentum * 0.20 + min(max(day_gain - 4.0, 0.0), 5.0) * 0.015, 0.0, 1.0)
                score_boost = 0.07 if late_chase else 0.10
                label = "extended live rally" if late_chase else "live rally ignition"
                reasons.append(f"{label} ({day_gain:+.1f}% from open)")

        if phase == "none" and (
            constructive_trend
            and near_pivot
            and not_too_extended
            and (turnover >= min_turnover * 0.50 or projected_turnover >= min_turnover)
            and (
                volume_ratio >= 1.10
                or return_5d >= 2.0
                or return_20d >= 5.0
                or sentiment.get("positive_catalyst")
            )
        ):
            phase = "pre_rally_fuel"
            trade_window = "watch_for_ignition"
            score = _clamp(
                0.58
                + (0.12 if trend >= 0.75 else 0.0)
                + (0.10 if volume_ratio >= 1.3 else 0.0)
                + (0.08 if dist_high is not None and dist_high <= 3.0 else 0.0)
                + (0.08 if sentiment.get("positive_catalyst") else 0.0),
                0.0,
                1.0,
            )
            score_boost = 0.05
            reasons.append("pre-rally fuel near breakout zone")

        return {
            "phase": phase,
            "setup": "" if phase == "none" else phase,
            "score": round(score, 4),
            "score_boost": score_boost,
            "trade_window": trade_window,
            "reasons": reasons,
            "evidence": {
                "day_gain_pct": _round(day_gain),
                "day_range_position": _round(range_position),
                "day_high_distance_pct": _round(high_distance),
                "volume_ratio": _round(volume_ratio),
                "projected_volume_ratio": _round(projected_volume_ratio),
                "turnover": round(turnover, 2),
                "projected_turnover": round(projected_turnover, 2),
                "distance_to_near_high_pct": _round(dist_high),
                "distance_to_sma20_pct": _round(dist_sma20),
                "near_pivot": near_pivot,
                "volume_support": volume_support,
            },
        }

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

    def _data_quality(
        self,
        row: dict[str, Any],
        quote: Quote,
        metrics: dict[str, Any],
        sentiment: dict[str, Any],
    ) -> dict[str, Any]:
        region = market_region_for_row(row)
        missing: list[str] = []
        source = str(quote.source or "").lower()
        history = int(metrics.get("history_candles") or 0)
        quote_age = _float_or_none(metrics.get("quote_age_seconds"))
        max_quote_age_seconds = 96 * 3600
        if quote_age is None:
            missing.append("quote_timestamp")
        elif quote_age > max_quote_age_seconds:
            missing.append("stale_quote")
        if history < 20:
            missing.append("daily_history")
        if metrics.get("volume_ratio") is None:
            missing.append("volume_baseline")
        freshness = metrics.get("candle_freshness") if isinstance(metrics.get("candle_freshness"), dict) else {}
        live_session = metrics.get("session_progress_pct") is not None
        intraday_count = int(freshness.get("intraday_count") or 0)
        fresh_intraday_required = bool(
            live_session
            and history >= 20
            and (
                ("upstox" in source or "kite" in source or "nubra" in source or "indstocks" in source)
                if region != "US"
                else ("alpaca" in source or "polygon" in source or "yahoo" in source)
            )
        )
        if fresh_intraday_required and intraday_count <= 0:
            missing.append("fresh_intraday_candles")
        elif fresh_intraday_required and not freshness.get("intraday_matches_quote_session"):
            missing.append("stale_intraday_candles")
        if region == "US":
            if "yahoo" not in source and not any(token in source for token in ("alpaca", "polygon")):
                missing.append("us_screening_quote_source")
            if "yahoo" in source and not any(token in source for token in ("alpaca", "polygon", "iex", "sip")):
                missing.append("us_realtime_intraday_for_actionable_trade")
        else:
            if not any(token in source for token in ("upstox", "kite", "nubra", "indstocks", "yahoo")):
                missing.append("in_screening_quote_source")
        if self.sentiment_enabled and not sentiment.get("headline_count") and not sentiment.get("event_count"):
            missing.append("news_sentiment")
        score = 1.0
        if history < 20:
            score -= 0.45
        elif history < 55:
            score -= 0.18
        if metrics.get("volume_ratio") is None:
            score -= 0.18
        if "stale_quote" in missing:
            score -= 0.45
        if "quote_timestamp" in missing:
            score -= 0.10
        if "us_realtime_intraday_for_actionable_trade" in missing:
            score -= 0.12
        if "fresh_intraday_candles" in missing or "stale_intraday_candles" in missing:
            score -= 0.10
        if self.sentiment_enabled and "news_sentiment" in missing:
            score -= 0.08
        reject_reason = ""
        if "stale_quote" in missing:
            reject_reason = "stale_quote"
        elif history < 20 and not sentiment.get("positive_catalyst"):
            reject_reason = "insufficient_screening_data"
        return {
            "score": _clamp(score, 0.0, 1.0),
            "missing": missing,
            "reject_reason": reject_reason,
            "history_candles": history,
            "quote_source": quote.source,
            "quote_age_seconds": quote_age,
            "candle_freshness": freshness,
            "fresh_intraday_required": fresh_intraday_required,
            "tradeable_screening": not reject_reason and history >= 55 and "stale_quote" not in missing,
            "actionable_data_ready": not reject_reason
            and "us_realtime_intraday_for_actionable_trade" not in missing
            and "fresh_intraday_candles" not in missing
            and "stale_intraday_candles" not in missing,
            "probe_only": False,
        }

    def _candle_freshness(
        self,
        candle_set: dict[str, list[Candle]],
        quote: Quote,
        market_region: str,
    ) -> dict[str, Any]:
        intraday = candle_set.get("intraday") or []
        daily = candle_set.get("daily") or []
        analysis = candle_set.get("analysis") or []
        quote_dt = _parse_datetime(getattr(quote, "asof", None))
        market_tz = ZoneInfo("America/New_York") if str(market_region or "").upper() == "US" else ZoneInfo("Asia/Kolkata")
        quote_market_date = quote_dt.astimezone(market_tz).date().isoformat() if quote_dt else None
        intraday_dt = _latest_candle_datetime(intraday)
        daily_dt = _latest_candle_datetime(daily)
        analysis_dt = _latest_candle_datetime(analysis)
        intraday_market_date = intraday_dt.astimezone(market_tz).date().isoformat() if intraday_dt else None
        daily_market_date = daily_dt.astimezone(market_tz).date().isoformat() if daily_dt else None
        analysis_market_date = analysis_dt.astimezone(market_tz).date().isoformat() if analysis_dt else None
        return {
            "quote_market_date": quote_market_date,
            "latest_intraday_ts": intraday_dt.isoformat() if intraday_dt else None,
            "latest_daily_ts": daily_dt.isoformat() if daily_dt else None,
            "latest_analysis_ts": analysis_dt.isoformat() if analysis_dt else None,
            "latest_intraday_market_date": intraday_market_date,
            "latest_daily_market_date": daily_market_date,
            "latest_analysis_market_date": analysis_market_date,
            "intraday_count": len(intraday),
            "daily_count": len(daily),
            "analysis_count": len(analysis),
            "intraday_matches_quote_session": bool(quote_market_date and intraday_market_date == quote_market_date),
        }

    def _allow_live_rally_probe(
        self,
        data_quality: dict[str, Any],
        rally: dict[str, Any],
        metrics: dict[str, Any],
        min_turnover: float,
    ) -> bool:
        """Let obvious live rally evidence reach candle prefetch before final trade gates."""

        if data_quality.get("reject_reason") != "insufficient_screening_data":
            return False
        if rally.get("phase") not in _LIVE_RALLY_SETUPS:
            return False
        if "stale_quote" in (data_quality.get("missing") or []):
            return False
        day_gain = _float_or_none(metrics.get("day_gain_pct")) or 0.0
        range_position = _float_or_none(metrics.get("day_range_position")) or 0.0
        high_distance = _float_or_none(metrics.get("day_high_distance_pct"))
        turnover = _float_or_none(metrics.get("turnover")) or 0.0
        projected_turnover = _float_or_none(metrics.get("projected_turnover")) or turnover
        participation = max(
            _float_or_none(metrics.get("volume_ratio")) or 0.0,
            (_float_or_none(metrics.get("projected_volume_ratio")) or 0.0) * 0.75,
        )
        return (
            day_gain >= 1.5
            and range_position >= 0.68
            and (high_distance is None or high_distance <= 2.0 or day_gain >= 5.0)
            and (
                turnover >= min_turnover * 2.0
                or ((turnover >= min_turnover * 0.5 or projected_turnover >= min_turnover) and participation >= 1.0)
            )
        )

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
        high_quality_event_count = _high_quality_event_count(events)
        evidence = _clamp(confidence, 0.0, 1.0)
        boost = score * evidence * self.sentiment_weight
        return {
            "score": round(score, 4),
            "confidence": round(confidence, 4),
            "headline_count": headline_count,
            "event_count": event_count,
            "high_quality_event_count": high_quality_event_count,
            "boost": round(boost, 4),
            "positive_catalyst": score >= 0.22 and evidence >= 0.35 and high_quality_event_count > 0,
            "negative_catalyst": score <= -0.30 and evidence >= 0.30 and high_quality_event_count > 0,
            "headlines": [str(item)[:180] for item in headlines[:3]],
            "events": [event for event in events[:5] if isinstance(event, dict)],
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
        rally: dict[str, Any] | None = None,
        min_turnover: float | None = None,
        market_action_review: dict[str, Any] | None = None,
    ) -> str:
        rally = rally or {}
        market_action_review = market_action_review or {}
        min_turnover = self.min_turnover if min_turnover is None else min_turnover
        market_action_setup = str(market_action_review.get("setup") or "")
        if market_action_setup in {"circuit_demand_lock", "52_week_high_volume_breakout", "earnings_beat_gap_and_go", "broker_re_rating_breakout"}:
            return market_action_setup
        if rally.get("setup"):
            return str(rally["setup"])
        if market_action_setup in _MARKET_ACTION_SETUPS:
            return market_action_setup
        history_candles = int(metrics.get("history_candles") or 0)
        has_structural_history = history_candles >= 20
        has_breakout_history = (
            metrics.get("distance_to_20d_high_pct") is not None
            or metrics.get("distance_to_55d_high_pct") is not None
        )
        day_range_position = float(metrics.get("day_range_position") or 0.0)
        turnover = float(metrics.get("turnover") or 0.0)
        projected_turnover = float(metrics.get("projected_turnover") or turnover)
        day_gain = _float_or_none(metrics.get("day_gain_pct")) or 0.0
        volume_ratio = _float_or_none(metrics.get("volume_ratio")) or 0.0
        projected_volume_ratio = _float_or_none(metrics.get("projected_volume_ratio")) or volume_ratio
        participation = max(volume_ratio, projected_volume_ratio * 0.75)
        day_high_distance = _float_or_none(metrics.get("day_high_distance_pct"))
        near_day_high = (
            day_high_distance is not None
            and day_high_distance <= (4.0 if day_gain >= 5.0 and volume_ratio >= 2.0 else 2.0)
        )
        if (
            has_structural_history
            and day_gain >= 4.0
            and day_range_position >= (0.60 if day_gain >= 5.0 and volume_ratio >= 2.0 else 0.70)
            and near_day_high
            and (turnover >= min_turnover * 0.75 or projected_turnover >= min_turnover * 1.5)
            and (participation >= 1.15 or projected_turnover >= min_turnover * 3.0)
        ):
            return "intraday_momentum"
        if sentiment.get("positive_catalyst") and (
            volume >= 0.50
            or breakout >= 0.55
            or momentum >= 0.55
            or ((turnover >= min_turnover * 0.75 or projected_turnover >= min_turnover * 1.5) and day_range_position >= 0.70)
        ):
            return "news_catalyst"
        if has_breakout_history and breakout >= 0.70 and volume >= 0.60:
            return "breakout_continuation"
        if has_breakout_history and breakout >= 0.70 and (trend >= 0.55 or momentum >= 0.55 or sentiment.get("positive_catalyst")):
            return "near_breakout"
        if has_structural_history and trend >= 0.65 and momentum >= 0.60:
            return "trend_momentum"
        distance_sma20 = metrics.get("distance_to_sma20_pct")
        if has_structural_history and trend >= 0.65 and distance_sma20 is not None and -2.5 <= float(distance_sma20) <= 2.5:
            return "pullback_buy"
        if has_structural_history and momentum >= 0.65 and volume >= 0.65:
            return "smallcap_momentum"
        if not has_structural_history and (turnover >= min_turnover * 0.75 or projected_turnover >= min_turnover * 1.5) and day_range_position >= 0.70:
            return "intraday_momentum_probe"
        return "watchlist_candidate"

    def _bucket(self, score: float, risk: float, reject_reason: str, metrics: dict[str, Any]) -> str:
        if reject_reason:
            return DATA_STALE_WATCH if reject_reason == "stale_quote" else "Avoid"
        day_gain = _float_or_none(metrics.get("day_gain_pct")) or 0.0
        if day_gain >= 8.0:
            return LATE_CHASE_AVOID
        if int(metrics.get("history_candles") or 0) < 20 and score >= 0.58:
            return "Small Size Only"
        if score >= 0.72 and risk >= 0.45:
            return "Actionable"
        if score >= 0.58:
            return "Small Size Only"
        if score >= 0.45:
            return "Watch"
        return "Avoid"

    def _quality_reject_reason(self, item: dict[str, Any]) -> str:
        if item.get("bucket") == DATA_STALE_WATCH:
            return "stale_quote"
        if item.get("bucket") == LATE_CHASE_AVOID:
            return ""
        if item.get("bucket") == "Avoid":
            return "avoid_bucket_quality_gate"
        data_quality = item.get("data_quality") if isinstance(item.get("data_quality"), dict) else {}
        if data_quality.get("reject_reason"):
            return str(data_quality["reject_reason"])
        score = _float_or_none(item.get("score")) or 0.0
        if score < self.min_score:
            return "below_opportunity_score"
        if self.require_active_setup and item.get("setup") not in _ACTIVE_OPPORTUNITY_SETUPS:
            if self._good_mover_watchable(item.get("market_action") or {}, item.get("rally_radar") or {}, item.get("metrics") or {}):
                item["bucket"] = ACTIONABLE_WATCH
                item["actionable_watch"] = True
                return ""
            return "no_active_opportunity_setup"
        return ""

    def _late_chase(self, metrics: dict[str, Any], market_action_review: dict[str, Any]) -> bool:
        day_gain = _float_or_none(metrics.get("day_gain_pct")) or 0.0
        dist_sma20 = _float_or_none(metrics.get("distance_to_sma20_pct"))
        market_evidence = market_action_review.get("evidence") if isinstance(market_action_review.get("evidence"), dict) else {}
        market_extended = bool(market_evidence.get("extended")) and day_gain >= 8.0
        return bool(day_gain >= 8.0 or (dist_sma20 is not None and dist_sma20 > 14.0) or market_extended)

    def _good_mover_watchable(
        self,
        market_action_review: dict[str, Any],
        rally: dict[str, Any],
        metrics: dict[str, Any],
    ) -> bool:
        if market_action_review.get("available"):
            return True
        phase = str(rally.get("phase") or "")
        if phase in {*_LIVE_RALLY_SETUPS, "pre_rally_fuel"}:
            return True
        day_gain = _float_or_none(metrics.get("day_gain_pct")) or 0.0
        volume_ratio = _float_or_none(metrics.get("volume_ratio")) or _float_or_none(metrics.get("projected_volume_ratio")) or 0.0
        high_distance = _float_or_none(metrics.get("day_high_distance_pct"))
        return bool(day_gain >= 3.0 and volume_ratio >= 1.2 and (high_distance is None or high_distance <= 3.0))

    def _public_item(self, item: dict[str, Any]) -> dict[str, Any]:
        metrics = item.get("metrics") or {}
        sentiment = item.get("sentiment") or {}
        data_quality = item.get("data_quality") or {}
        liquidity_profile = item.get("liquidity_profile") if isinstance(item.get("liquidity_profile"), dict) else {}
        rally = item.get("rally_radar") if isinstance(item.get("rally_radar"), dict) else {}
        market_action = item.get("market_action") if isinstance(item.get("market_action"), dict) else {}
        btst = item.get("btst") if isinstance(item.get("btst"), dict) else {}
        top_gainers_playbook = item.get("top_gainers_playbook") if isinstance(item.get("top_gainers_playbook"), dict) else {}
        trade_window = market_action.get("trade_window") or rally.get("trade_window")
        if btst.get("detected"):
            trade_window = "buy_before_close_sell_tomorrow"
        return {
            "symbol": item.get("symbol"),
            "name": item.get("name"),
            "market_region": item.get("market_region"),
            "score": item.get("score"),
            "bucket": item.get("bucket"),
            "setup": item.get("setup"),
            "label": LATE_CHASE_AVOID if item.get("bucket") == LATE_CHASE_AVOID else DATA_STALE_WATCH if item.get("bucket") == DATA_STALE_WATCH else ACTIONABLE_WATCH if item.get("bucket") == ACTIONABLE_WATCH else item.get("setup"),
            "rally_phase": rally.get("phase"),
            "rally_score": rally.get("score"),
            "trade_window": trade_window,
            "rally_evidence": rally.get("evidence", {}),
            "market_action": market_action,
            "btst": btst,
            "top_gainers_playbook": top_gainers_playbook,
            "actionable_watch": bool(item.get("actionable_watch")),
            "late_chase": bool(item.get("late_chase")),
            "forced_inclusion": item.get("forced_inclusion", False),
            "reasons": item.get("reasons", [])[:4],
            "components": item.get("components", {}),
            "data_quality": {
                "score": data_quality.get("score"),
                "missing": data_quality.get("missing", []),
                "quote_source": data_quality.get("quote_source"),
                "quote_age_seconds": data_quality.get("quote_age_seconds"),
                "candle_freshness": data_quality.get("candle_freshness", {}),
                "fresh_intraday_required": data_quality.get("fresh_intraday_required"),
                "tradeable_screening": data_quality.get("tradeable_screening"),
                "actionable_data_ready": data_quality.get("actionable_data_ready"),
                "probe_only": data_quality.get("probe_only"),
            },
            "liquidity_profile": liquidity_profile,
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
            "open": metrics.get("open"),
            "day_gain_pct": metrics.get("day_gain_pct"),
            "day_range_pct": metrics.get("day_range_pct"),
            "day_range_position": metrics.get("day_range_position"),
            "turnover": metrics.get("turnover"),
            "projected_turnover": metrics.get("projected_turnover"),
            "projected_volume_ratio": metrics.get("projected_volume_ratio"),
            "session_progress_pct": metrics.get("session_progress_pct"),
            "distance_to_55d_high_pct": metrics.get("distance_to_55d_high_pct"),
            "day_high_distance_pct": metrics.get("day_high_distance_pct"),
            "volume_ratio": metrics.get("volume_ratio"),
            "atr_pct": metrics.get("atr_pct"),
            "history_candles": metrics.get("history_candles"),
        }

    def _relative_strength_context(self, candle_sets: dict[str, dict[str, list[Candle]]]) -> dict[str, dict[str, Any]]:
        returns_252: dict[str, float] = {}
        returns_126: dict[str, float] = {}
        returns_84: dict[str, float] = {}
        returns_63: dict[str, float] = {}
        for symbol, sets in candle_sets.items():
            candles = sets.get("analysis") or sets.get("daily") or []
            closes = [float(item.close) for item in candles if item.close is not None and float(item.close) > 0]
            ret_252 = _return_pct(closes, 252)
            ret_126 = _return_pct(closes, 126)
            ret_84 = _return_pct(closes, 84)
            ret_63 = _return_pct(closes, 63)
            if ret_252 is not None:
                returns_252[str(symbol).upper()] = ret_252
            if ret_126 is not None:
                returns_126[str(symbol).upper()] = ret_126
            if ret_84 is not None:
                returns_84[str(symbol).upper()] = ret_84
            if ret_63 is not None:
                returns_63[str(symbol).upper()] = ret_63
        pct_252 = _percentile_ranks(returns_252)
        pct_126 = _percentile_ranks(returns_126)
        pct_63 = _percentile_ranks(returns_63)
        output: dict[str, dict[str, Any]] = {}
        for symbol in set(returns_252) | set(returns_126) | set(returns_63):
            rs_rank = pct_252.get(symbol, pct_126.get(symbol, pct_63.get(symbol)))
            output[symbol] = {
                "rs_rank": rs_rank,
                "percentile_252": pct_252.get(symbol),
                "percentile_126": pct_126.get(symbol),
                "percentile_63": pct_63.get(symbol),
                "return_252d_pct": returns_252.get(symbol),
                "return_126d_pct": returns_126.get(symbol),
                "return_63d_pct": returns_63.get(symbol),
                "improving": bool(
                    returns_63.get(symbol) is not None
                    and returns_84.get(symbol) is not None
                    and returns_63[symbol] >= returns_84[symbol] * 0.75
                ),
                "source": "scan_universe_return_percentile",
            }
        return output

    def _select_diverse(self, scored: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        selected: list[dict[str, Any]] = []
        selected_symbols: set[str] = set()
        sector_counts: dict[str, int] = {}
        setup_counts: dict[str, int] = {}
        sector_cap = max(3, limit // 4)
        setup_cap = max(5, limit // 3)

        def try_add(item: dict[str, Any], enforce_caps: bool) -> None:
            if len(selected) >= limit:
                return
            symbol = str(item.get("symbol") or "")
            if not symbol or symbol in selected_symbols:
                return
            sector = str(item.get("sector") or item.get("market_region") or "unknown")
            setup = str(item.get("setup") or "unknown")
            if enforce_caps and (
                sector_counts.get(sector, 0) >= sector_cap
                or setup_counts.get(setup, 0) >= setup_cap
            ):
                return
            selected.append(item)
            selected_symbols.add(symbol)
            sector_counts[sector] = sector_counts.get(sector, 0) + 1
            setup_counts[setup] = setup_counts.get(setup, 0) + 1

        for item in scored:
            try_add(item, enforce_caps=True)
        if len(selected) < limit:
            for item in scored:
                try_add(item, enforce_caps=False)
        return selected

    def _select_rally_radar_then_diverse(self, scored: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        selected: list[dict[str, Any]] = []
        selected_symbols: set[str] = set()
        reserve = min(limit, max(45, int(limit * 0.80)))
        playbook_buys = [
            item
            for item in scored
            if self._top_gainers_playbook_buy_priority(item) > 0
        ]
        playbook_buys.sort(
            key=lambda item: (
                self._top_gainers_playbook_buy_priority(item),
                self._rally_discovery_score(item),
                float((item.get("metrics") or {}).get("turnover") or 0.0),
            ),
            reverse=True,
        )
        for item in playbook_buys[: min(limit, 12)]:
            symbol = str(item.get("symbol") or "")
            if symbol and symbol not in selected_symbols:
                selected.append(item)
                selected_symbols.add(symbol)

        rally_items = [
            item
            for item in scored
            if str(item.get("setup") or "")
            in {*_LIVE_RALLY_SETUPS, "pre_rally_fuel", "btst_buy_candidate", *_MARKET_ACTION_SETUPS}
            or float(((item.get("components") or {}).get("live_momentum")) or 0.0) >= 0.70
            or float(((item.get("components") or {}).get("rally_radar")) or 0.0) >= 0.60
            or float(((item.get("components") or {}).get("btst")) or 0.0) >= 0.70
            or bool((item.get("market_action") or {}).get("available"))
        ]
        rally_items.sort(
            key=lambda item: (
                self._rally_discovery_score(item),
                float((item.get("metrics") or {}).get("day_gain_pct") or 0.0),
                float((item.get("metrics") or {}).get("turnover") or 0.0),
            ),
            reverse=True,
        )
        for item in rally_items[:reserve]:
            symbol = str(item.get("symbol") or "")
            if symbol and symbol not in selected_symbols:
                selected.append(item)
                selected_symbols.add(symbol)

        pre_fuel_min = min(12, max(4, limit // 5))
        pre_fuel = [
            item
            for item in rally_items
            if item.get("setup") == "pre_rally_fuel" and str(item.get("symbol") or "") not in selected_symbols
        ]
        pre_fuel.sort(key=self._rally_discovery_score, reverse=True)
        for item in pre_fuel[:pre_fuel_min]:
            if len(selected) >= limit:
                break
            symbol = str(item.get("symbol") or "")
            if symbol and symbol not in selected_symbols:
                selected.append(item)
                selected_symbols.add(symbol)

        if len(selected) >= limit:
            return selected[:limit]

        remainder = [item for item in scored if str(item.get("symbol") or "") not in selected_symbols]
        selected.extend(self._select_diverse(remainder, limit - len(selected)))
        return selected[:limit]

    def _top_gainers_playbook_buy_priority(self, item: dict[str, Any]) -> float:
        playbook = item.get("top_gainers_playbook") if isinstance(item.get("top_gainers_playbook"), dict) else {}
        signal = str(playbook.get("final_signal") or "").upper()
        if signal not in {"STRONG BUY", "MODERATE BUY"}:
            return 0.0
        if playbook.get("hard_excluded") or playbook.get("hard_excludes"):
            return 0.0
        anti_codes = {
            str(flag.get("code") or "").upper()
            for flag in playbook.get("anti_patterns") or []
            if isinstance(flag, dict)
        }
        if anti_codes & {"CHASING", "OPERATOR_RISK", "SHORT_COVER", "STAGE_TRAP", "ILLIQUID_BREAKOUT"}:
            return 0.0
        quant = _clamp(float(playbook.get("quant_score") or 0.0) / 100.0, 0.0, 1.0)
        gain = _clamp((float(playbook.get("gain_pct") or 0.0) - 3.0) / 7.0, 0.0, 1.0)
        signal_boost = 0.30 if signal == "STRONG BUY" else 0.18
        return _clamp(signal_boost + quant * 0.55 + gain * 0.15, 0.0, 1.0)

    def _rally_discovery_score(self, item: dict[str, Any]) -> float:
        metrics = item.get("metrics") if isinstance(item.get("metrics"), dict) else {}
        components = item.get("components") if isinstance(item.get("components"), dict) else {}
        setup = str(item.get("setup") or "")
        playbook_boost = self._top_gainers_playbook_buy_priority(item) * 0.25
        phase_boost = {
            "52_week_high_volume_breakout": 0.13,
            "earnings_beat_gap_and_go": 0.12,
            "broker_re_rating_breakout": 0.10,
            "circuit_demand_lock": 0.04,
            "opening_ignition": 0.12,
            "intraday_momentum": 0.10,
            "market_action_momentum": 0.08,
            "pre_rally_fuel": 0.06,
            "btst_buy_candidate": 0.11,
            "extended_momentum_watch": 0.02,
            "top_gainer_momentum": 0.05,
        }.get(setup, 0.0)
        live = float(components.get("live_momentum") or 0.0)
        rally = float(components.get("rally_radar") or 0.0)
        btst = float(components.get("btst") or 0.0)
        gain = _clamp((float(metrics.get("day_gain_pct") or 0.0) - 3.0) / 7.0, 0.0, 1.0)
        volume = _clamp((float(metrics.get("volume_ratio") or 0.0) - 1.0) / 4.0, 0.0, 1.0)
        min_turnover = self._min_turnover_for_market(str(item.get("market_region") or "IN"))
        turnover = _clamp(float(metrics.get("turnover") or 0.0) / max(min_turnover * 10.0, 1.0), 0.0, 1.0)
        market_action = item.get("market_action") if isinstance(item.get("market_action"), dict) else {}
        market_action_score = float(market_action.get("score") or 0.0)
        return _clamp(
            playbook_boost
            + phase_boost
            + rally * 0.30
            + btst * 0.16
            + live * 0.22
            + gain * 0.18
            + volume * 0.10
            + turnover * 0.07
            + market_action_score * 0.13,
            0.0,
            1.0,
        )

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


def _combined_playbook_dashboard(records: list[dict[str, Any]]) -> dict[str, Any]:
    records = [item for item in records if isinstance(item, dict) and item.get("available")]
    records.sort(
        key=lambda item: (
            _playbook_signal_rank(item.get("final_signal")),
            float(item.get("quant_score") or 0.0),
            float(item.get("gain_pct") or 0.0),
        ),
        reverse=True,
    )
    excluded = [item for item in records if item.get("hard_excluded") or item.get("tier") == "HARD EXCLUDE"]
    tier3 = [item for item in records if item.get("tier") == "TIER 3 - WATCH ONLY"]
    buys = [item for item in records if item.get("final_signal") in {"STRONG BUY", "MODERATE BUY"}]
    watch = [item for item in records if item.get("final_signal") == "WATCH" or "WATCH" in str(item.get("tier") or "")]
    avoid = [item for item in records if item.get("final_signal") == "AVOID"]
    return {
        "enabled": True,
        "source": "multi_market_top_movers_playbook",
        "label": "Top Movers Playbook",
        "market_region": "BOTH",
        "generated_at": utc_now(),
        "total_gainers_evaluated": len(records),
        "tier_summary": {
            "tier1": sum(1 for item in records if item.get("tier") == "TIER 1"),
            "tier2": sum(1 for item in records if item.get("tier") == "TIER 2"),
            "tier3_watch": len(tier3),
            "excluded": len(excluded),
        },
        "signal_summary": {
            "strong_buy": sum(1 for item in records if item.get("final_signal") == "STRONG BUY"),
            "moderate_buy": sum(1 for item in records if item.get("final_signal") == "MODERATE BUY"),
            "watch": len(watch),
            "avoid": len(avoid),
        },
        "catalyst_distribution": _count_values((item.get("catalyst_review") or {}).get("catalyst_type") for item in records),
        "top_gainer_pct": max([float(item.get("gain_pct") or 0.0) for item in records], default=0.0),
        "buy_signals": buys[:12],
        "tomorrow_watchlist": [
            item
            for item in records
            if item.get("final_signal") in {"WATCH", "QUANT HOLD"} and not item.get("hard_excluded")
        ][:20],
        "do_not_chase": [
            {
                "symbol": item.get("symbol"),
                "name": item.get("name"),
                "reason": _playbook_avoid_reason(item),
                "anti_patterns": item.get("anti_patterns") or [],
            }
            for item in records
            if item.get("final_signal") == "AVOID" or item.get("hard_excluded")
        ][:30],
        "records": records[:80],
    }


def _playbook_signal_rank(value: Any) -> int:
    return {
        "STRONG BUY": 5,
        "MODERATE BUY": 4,
        "WATCH": 3,
        "QUANT HOLD": 2,
        "AVOID": 1,
    }.get(str(value or ""), 0)


def _count_values(values: Any) -> dict[str, int]:
    output: dict[str, int] = {}
    for value in values:
        key = str(value or "UNKNOWN")
        output[key] = output.get(key, 0) + 1
    return output


def _playbook_avoid_reason(item: dict[str, Any]) -> str:
    if item.get("hard_excludes"):
        return f"Do not buy {item.get('symbol')} - hard exclusion: {', '.join(item.get('hard_excludes') or [])}."
    anti = item.get("anti_patterns") or []
    if anti:
        return f"Do not buy {item.get('symbol')} - {anti[0].get('reason') or anti[0].get('label')}."
    return f"Do not buy {item.get('symbol')} - final signal is {item.get('final_signal')}."


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _return_pct(closes: list[float], lookback: int) -> float | None:
    if len(closes) <= lookback:
        return None
    previous = closes[-(lookback + 1)]
    current = closes[-1]
    return ((current - previous) / previous) * 100 if previous else None


def _close_strength_from_candles(candles: list[Candle], fallback_price: float | None = None) -> dict[str, Any]:
    if not candles:
        return {"latest_close_position": None}
    latest = candles[-1]
    high = _float_or_none(latest.high)
    low = _float_or_none(latest.low)
    close = _float_or_none(fallback_price) or _float_or_none(latest.close)
    if high is None or low is None or close is None or high <= low:
        return {"latest_close_position": None}
    return {"latest_close_position": _clamp((close - low) / (high - low), 0.0, 1.0)}


def _percentile_ranks(values: dict[str, float]) -> dict[str, float]:
    if not values:
        return {}
    ordered = sorted(values.items(), key=lambda item: item[1])
    if len(ordered) == 1:
        return {ordered[0][0]: 100.0}
    denominator = len(ordered) - 1
    return {
        symbol: round((rank / denominator) * 100.0, 2)
        for rank, (symbol, _value) in enumerate(ordered)
    }


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


def _high_quality_event_count(events: list[Any]) -> int:
    count = 0
    for event in events:
        if not isinstance(event, dict):
            continue
        event_type = str(event.get("event_type") or event.get("label") or "").lower()
        if event_type in {"", "neutral", "macro_sector"}:
            continue
        confidence = _float_or_none(event.get("confidence")) or 0.0
        source_weight = _float_or_none(event.get("source_weight")) or 0.0
        if confidence >= 0.30 and source_weight >= 0.55:
            count += 1
    return count


def _round(value: float | None, digits: int = 4) -> float | None:
    return round(value, digits) if value is not None else None


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def _session_progress_pct(quote: Quote, market_region: str) -> float | None:
    raw = getattr(quote, "asof", None)
    parsed = _parse_datetime(raw) or datetime.now(timezone.utc)
    market = str(market_region or "").upper()
    if market == "US":
        local = parsed.astimezone(ZoneInfo("America/New_York"))
        start = time(9, 30)
        end = time(16, 0)
    else:
        local = parsed.astimezone(ZoneInfo("Asia/Kolkata"))
        start = time(9, 15)
        end = time(15, 30)
    if local.weekday() >= 5:
        return None
    day_start = local.replace(hour=start.hour, minute=start.minute, second=0, microsecond=0)
    day_end = local.replace(hour=end.hour, minute=end.minute, second=0, microsecond=0)
    if local < day_start:
        return None
    if local >= day_end:
        return 1.0
    total = (day_end - day_start).total_seconds()
    elapsed = (local - day_start).total_seconds()
    return _clamp(elapsed / total, 0.02, 1.0) if total > 0 else None


def _quote_age_seconds(quote: Quote) -> float | None:
    raw = getattr(quote, "asof", None)
    if not raw:
        return None
    parsed = _parse_datetime(raw)
    if parsed is None:
        return None
    return round((datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds(), 3)


def _parse_datetime(raw: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _latest_candle_datetime(candles: list[Candle]) -> datetime | None:
    if not candles:
        return None
    return _parse_datetime(getattr(candles[-1], "ts", None))
