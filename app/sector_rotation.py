from __future__ import annotations

import time
from typing import Any

from .config import Settings
from .db import Database
from .models import Candle, Quote, utc_now


SECTOR_FALLBACKS = {
    "CEIGALL": "Infrastructure & Construction",
    "HAPPYFORGE": "Industrials & Auto Ancillary",
    "KERNEX": "Rail & Transport Technology",
    "KRISHANA": "Chemicals & Fertilizers",
    "NATCOPHARM": "Pharmaceuticals",
    "RADICO": "Consumer Alcohol",
    "RICOAUTO": "Auto Ancillary",
    "RISHABH": "Electrical Equipment",
    "RPEL": "Industrial Materials",
    "SENORES": "Pharmaceuticals",
    "SPARC": "Pharmaceuticals",
    "UNIPARTS": "Auto Ancillary",
}


def _mean(values: list[float] | tuple[float, ...]) -> float:
    return sum(values) / len(values) if values else 0.0


class SectorRotationService:
    def __init__(self, settings: Settings, db: Database) -> None:
        self.settings = settings
        self.db = db
        self._cache: dict[str, tuple[float, dict[str, Any]]] = {}

    async def compute_sector_scores(
        self,
        universe: list[dict[str, Any]],
        quotes: dict[str, Quote],
        candles_by_symbol: dict[str, list[Candle]],
        market_region: str = "BOTH",
    ) -> dict[str, Any]:
        if not self.settings.enable_sector_rotation:
            return _neutral("disabled")
        cache_key = str(market_region or "BOTH").upper()
        if cache_key in self._cache and time.monotonic() - self._cache[cache_key][0] < self.settings.sector_rotation_cache_seconds:
            return self._cache[cache_key][1]
        try:
            context = self._compute(universe, quotes, candles_by_symbol, market_region=cache_key)
            self._cache[cache_key] = (time.monotonic(), context)
            self._log("INFO", "sector_rotation_computed", "Sector rotation computed", context.get("leaderboard"))
            return context
        except Exception as exc:
            context = _neutral(f"{exc.__class__.__name__}: {str(exc)[:200]}")
            self._log("WARN", "sector_rotation_error", "Sector rotation failed safely", context)
            return context

    def get_symbol_sector_context(self, symbol: str, sector: str | None, context: dict[str, Any] | None = None) -> dict[str, Any]:
        cached = self._cache.get("BOTH") or self._cache.get("IN") or self._cache.get("US")
        source = context or (cached[1] if cached else self.db.get_state("sector_rotation_context", {}))
        symbols = source.get("symbols") or {}
        sectors = source.get("sectors") or {}
        symbol_context = symbols.get(str(symbol).upper())
        if symbol_context:
            return symbol_context
        sector_text = str(sector or "").strip()
        normalized_sector = (
            sector_text
            if sector_text and sector_text.lower() not in {"unclassified", "unknown", "na", "n/a", "-"}
            else SECTOR_FALLBACKS.get(str(symbol).upper())
        )
        sector_context = sectors.get(normalized_sector or "")
        if not sector_context:
            return {
                "available": False,
                "sector": normalized_sector or sector or "unknown",
                "sector_tailwind": False,
                "sector_headwind": False,
                "sector_rotation_score": 0.0,
                "data_gap": "sector_rotation_unavailable",
            }
        return _symbol_context(str(symbol).upper(), sector_context)

    def _compute(
        self,
        universe: list[dict[str, Any]],
        quotes: dict[str, Quote],
        candles_by_symbol: dict[str, list[Candle]],
        market_region: str = "BOTH",
    ) -> dict[str, Any]:
        grouped: dict[str, list[dict[str, Any]]] = {}
        universe_returns_20: list[float] = []
        for row in universe:
            symbol = row["symbol"]
            sector = _sector_for_row(row)
            candles = candles_by_symbol.get(symbol) or []
            quote = quotes.get(symbol)
            item = _symbol_returns(symbol, candles, float(quote.price) if quote else None)
            grouped.setdefault(sector, []).append(item)
            if item["return_20d"] is not None:
                universe_returns_20.append(item["return_20d"])
        nifty_proxy = _mean(universe_returns_20) if universe_returns_20 else 0.0
        previous = self.db.get_state("sector_rotation_history", [])
        if not isinstance(previous, list):
            previous = []
        previous_by_sector = {
            sector: [snapshot.get("sectors", {}).get(sector, {}).get("sector_vs_nifty_rs") for snapshot in previous[-3:]]
            for sector in grouped
        }
        sectors: dict[str, dict[str, Any]] = {}
        for sector, items in grouped.items():
            returns_5 = [item["return_5d"] for item in items if item["return_5d"] is not None]
            returns_20 = [item["return_20d"] for item in items if item["return_20d"] is not None]
            adv = sum(1 for item in items if item.get("advanced"))
            dec = sum(1 for item in items if item.get("declined"))
            sector_return_5d = _mean(returns_5) if returns_5 else 0.0
            sector_return_20d = _mean(returns_20) if returns_20 else 0.0
            rs = sector_return_20d - nifty_proxy
            prior_rs = [float(value) for value in previous_by_sector.get(sector, []) if value is not None]
            rising = len(prior_rs) >= 2 and rs > prior_rs[-1] > prior_rs[0]
            falling = len(prior_rs) >= 2 and rs < prior_rs[-1] < prior_rs[0]
            acceleration = sector_return_5d > sector_return_20d / 4
            if rs > 3 and acceleration and rising:
                stage = "markup"
            elif rs > 0 and rising:
                stage = "accumulation"
            elif rs < 0 and falling:
                stage = "distribution"
            else:
                stage = "neutral"
            sectors[sector] = {
                "sector": sector,
                "symbols": len(items),
                "sector_return_5d": round(sector_return_5d, 4),
                "sector_return_20d": round(sector_return_20d, 4),
                "sector_vs_nifty_rs": round(rs, 4),
                "sector_momentum": acceleration,
                "sector_adv_dec": round(adv / max(dec, 1), 4),
                "advance_count": adv,
                "decline_count": dec,
                "sector_stage": stage,
            }
        ranked = sorted(sectors.values(), key=lambda item: float(item["sector_vs_nifty_rs"]), reverse=True)
        total = len(ranked)
        for index, item in enumerate(ranked, start=1):
            tier = _tier(index, total)
            item["sector_rank"] = index
            item["sector_tier"] = tier
            item["sector_rotation_score"] = _rotation_score(tier, item["sector_stage"])
        sectors = {item["sector"]: item for item in ranked}
        symbols: dict[str, dict[str, Any]] = {}
        for row in universe:
            symbol = str(row["symbol"]).upper()
            symbols[symbol] = _symbol_context(symbol, sectors.get(_sector_for_row(row), {}))
        context = {
            "enabled": True,
            "updated_at": utc_now(),
            "market_region": market_region,
            "nifty_proxy_return_20d": round(nifty_proxy, 4),
            "sectors": sectors,
            "symbols": symbols,
            "leaderboard": {
                "top": ranked[:3],
                "bottom": list(reversed(ranked[-3:])),
            },
        }
        history_item = {"updated_at": context["updated_at"], "sectors": sectors}
        history = previous + [history_item]
        self.db.set_state("sector_rotation_history", history[-20:])
        return context

    def _log(self, level: str, event: str, message: str, details: Any | None = None) -> None:
        try:
            self.db.insert_agent_log(level, "sector_rotation", event, message, details)
        except Exception:
            pass


def _symbol_returns(symbol: str, candles: list[Candle], quote_price: float | None) -> dict[str, Any]:
    closes = [float(candle.close) for candle in candles]
    price = quote_price or (closes[-1] if closes else None)
    ret_5 = _return(closes, price, 5)
    ret_20 = _return(closes, price, 20)
    advanced = len(closes) >= 2 and closes[-1] > closes[-2]
    declined = len(closes) >= 2 and closes[-1] < closes[-2]
    return {"symbol": symbol, "return_5d": ret_5, "return_20d": ret_20, "advanced": advanced, "declined": declined}


def _sector_for_row(row: dict[str, Any]) -> str:
    symbol = str(row.get("symbol") or "").strip().upper()
    sector = str(row.get("sector") or "").strip()
    if sector and sector.lower() not in {"unclassified", "unknown", "na", "n/a", "-"}:
        return sector
    industry = str(row.get("industry") or row.get("macro") or "").lower()
    if "pharma" in industry:
        return "Pharmaceuticals"
    if "auto" in industry or "component" in industry:
        return "Auto Ancillary"
    if "construction" in industry or "civil" in industry or "infra" in industry:
        return "Infrastructure & Construction"
    if "fertil" in industry or "chemical" in industry:
        return "Chemicals & Fertilizers"
    if "electrical" in industry or "equipment" in industry:
        return "Electrical Equipment"
    if "brew" in industry or "distiller" in industry or "alcohol" in industry:
        return "Consumer Alcohol"
    return SECTOR_FALLBACKS.get(symbol, "Unclassified")


def _return(closes: list[float], price: float | None, periods: int) -> float | None:
    if price is None or len(closes) <= periods or closes[-periods - 1] == 0:
        return None
    return ((price - closes[-periods - 1]) / closes[-periods - 1]) * 100


def _tier(rank: int, total: int) -> str:
    if total <= 0:
        return "neutral"
    pct = rank / total
    if pct <= 0.25:
        return "top_quartile"
    if pct <= 0.5:
        return "upper_mid"
    if pct <= 0.75:
        return "lower_mid"
    return "bottom_quartile"


def _rotation_score(tier: str, stage: str) -> float:
    score = {"top_quartile": 0.45, "upper_mid": 0.2, "lower_mid": -0.1, "bottom_quartile": -0.35}.get(tier, 0.0)
    score += {"markup": 0.35, "accumulation": 0.2, "distribution": -0.35}.get(stage, 0.0)
    return round(max(min(score, 1.0), -1.0), 4)


def _symbol_context(symbol: str, sector_context: dict[str, Any]) -> dict[str, Any]:
    stage = sector_context.get("sector_stage", "neutral")
    tier = sector_context.get("sector_tier", "neutral")
    return {
        "available": bool(sector_context),
        "symbol": symbol,
        "sector": sector_context.get("sector", "unknown"),
        "sector_rank": sector_context.get("sector_rank"),
        "sector_stage": stage,
        "sector_rs_score": sector_context.get("sector_vs_nifty_rs"),
        "sector_tier": tier,
        "sector_tailwind": stage in {"accumulation", "markup"} and tier in {"top_quartile", "upper_mid"},
        "sector_headwind": stage == "distribution" and tier == "bottom_quartile",
        "sector_rotation_score": _rotation_score(tier, stage),
    }


def _neutral(reason: str) -> dict[str, Any]:
    return {
        "enabled": False,
        "updated_at": utc_now(),
        "sectors": {},
        "symbols": {},
        "leaderboard": {"top": [], "bottom": []},
        "data_gap": reason,
    }
