from __future__ import annotations

import asyncio
import hashlib
import math
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import quote_plus

import httpx

from .config import Settings
from .models import utc_now


GLOBAL_MARKETS = [
    {"symbol": "^GSPC", "label": "S&P 500", "impact": "risk_asset", "weight": 0.16},
    {"symbol": "^IXIC", "label": "Nasdaq", "impact": "risk_asset", "weight": 0.12},
    {"symbol": "^N225", "label": "Nikkei 225", "impact": "risk_asset", "weight": 0.10},
    {"symbol": "^HSI", "label": "Hang Seng", "impact": "risk_asset", "weight": 0.10},
    {"symbol": "000001.SS", "label": "Shanghai Composite", "impact": "risk_asset", "weight": 0.08},
    {"symbol": "^FTSE", "label": "FTSE 100", "impact": "risk_asset", "weight": 0.06},
    {"symbol": "^NSEI", "label": "Nifty 50", "impact": "india_equity", "weight": 0.16},
    {"symbol": "^BSESN", "label": "Sensex", "impact": "india_equity", "weight": 0.10},
    {"symbol": "CL=F", "label": "Crude Oil", "impact": "india_cost", "weight": 0.06},
    {"symbol": "GC=F", "label": "Gold", "impact": "defensive", "weight": 0.03},
    {"symbol": "INR=X", "label": "USD/INR", "impact": "rupee_pressure", "weight": 0.03},
]

RISK_ON_TERMS = {
    "rally",
    "rebounds",
    "gains",
    "gain",
    "surge",
    "surges",
    "eases",
    "soft landing",
    "rate cut",
    "stimulus",
    "growth",
    "record high",
    "optimism",
}

RISK_OFF_TERMS = {
    "selloff",
    "sell-off",
    "slump",
    "falls",
    "fall",
    "inflation",
    "rate hike",
    "war",
    "sanctions",
    "recession",
    "tariff",
    "crude spike",
    "geopolitical",
    "default",
    "crisis",
    "hawkish",
}


@dataclass(frozen=True)
class MacroNewsItem:
    title: str
    source: str
    published_at: str | None
    score: float
    confidence: float
    recency_weight: float
    weighted_score: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class GlobalIntelligenceService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._cache: tuple[float, dict[str, Any]] | None = None
        self._headers = {
            "Accept": "application/json",
            "User-Agent": "OpenStocks/1.0 (+global-risk-context)",
        }

    async def context_for_cycle(self) -> dict[str, Any]:
        if not self.settings.enable_global_intelligence:
            return {
                "enabled": False,
                "risk_score": 0.0,
                "confidence": 0.0,
                "regime": "disabled",
                "rationale": ["global intelligence disabled"],
            }

        now = time.monotonic()
        if self._cache and now - self._cache[0] < self.settings.global_cache_seconds:
            return self._cache[1]

        markets, news = await asyncio.gather(self._market_context(), self._news_context(), return_exceptions=True)
        if isinstance(markets, Exception):
            markets = {"score": 0.0, "confidence": 0.0, "items": [], "rationale": ["global market fetch failed"]}
        if isinstance(news, Exception):
            news = {"score": 0.0, "confidence": 0.0, "items": [], "headlines": []}

        market_weight = 0.72 if markets["items"] else 0.0
        news_weight = 0.28 if news["items"] else 0.0
        denominator = market_weight + news_weight or 1.0
        risk_score = ((markets["score"] * market_weight) + (news["score"] * news_weight)) / denominator
        confidence = min(((markets["confidence"] * market_weight) + (news["confidence"] * news_weight)) / denominator, 1.0)
        context = {
            "enabled": True,
            "updated_at": utc_now(),
            "risk_score": round(max(min(risk_score, 1.0), -1.0), 3),
            "confidence": round(confidence, 3),
            "regime": self._regime(risk_score),
            "market_score": markets["score"],
            "news_score": news["score"],
            "markets": markets["items"],
            "news": news,
            "rationale": markets["rationale"][:8],
            "source": "yahoo-delayed + google-news-rss",
        }
        self._cache = (now, context)
        return context

    async def _market_context(self) -> dict[str, Any]:
        symbols = [item["symbol"] for item in GLOBAL_MARKETS]
        by_symbol = {item["symbol"]: item for item in GLOBAL_MARKETS}
        items: list[dict[str, Any]] = []
        async with httpx.AsyncClient(timeout=10, headers=self._headers, follow_redirects=True) as client:
            response = await client.get(
                "https://query1.finance.yahoo.com/v7/finance/quote",
                params={"symbols": ",".join(symbols)},
            )
            response.raise_for_status()
            for raw in response.json().get("quoteResponse", {}).get("result", []):
                symbol = raw.get("symbol")
                spec = by_symbol.get(symbol)
                if not spec:
                    continue
                change_pct = raw.get("regularMarketChangePercent")
                price = raw.get("regularMarketPrice")
                if change_pct is None or price is None:
                    continue
                signal = self._market_signal(float(change_pct), spec["impact"])
                items.append(
                    {
                        "symbol": symbol,
                        "label": spec["label"],
                        "impact": spec["impact"],
                        "price": round(float(price), 4),
                        "change_pct": round(float(change_pct), 3),
                        "signal": round(signal, 3),
                        "weight": spec["weight"],
                        "asof": self._epoch_to_iso(raw.get("regularMarketTime")),
                    }
                )
        weight_sum = sum(item["weight"] for item in items) or 1.0
        score = sum(item["signal"] * item["weight"] for item in items) / weight_sum
        confidence = min(len(items) / len(GLOBAL_MARKETS), 1.0)
        rationale = [
            f"{item['label']} {item['change_pct']:+.2f}% -> {item['signal']:+.2f}"
            for item in sorted(items, key=lambda item: abs(item["signal"] * item["weight"]), reverse=True)
        ]
        return {
            "score": round(max(min(score, 1.0), -1.0), 3),
            "confidence": round(confidence, 3),
            "items": items,
            "rationale": rationale,
        }

    async def _news_context(self) -> dict[str, Any]:
        queries = [
            f'global stock markets Fed inflation crude oil rupee when:{self.settings.global_news_lookback_days}d',
            f'Asia markets India Nifty Sensex global cues when:{self.settings.global_news_lookback_days}d',
            f'geopolitical risk crude oil dollar emerging markets when:{self.settings.global_news_lookback_days}d',
        ]
        raw_items: list[dict[str, Any]] = []
        async with httpx.AsyncClient(timeout=8, follow_redirects=True) as client:
            responses = await asyncio.gather(
                *[
                    client.get(
                        f"https://news.google.com/rss/search?q={quote_plus(query)}&hl=en-IN&gl=IN&ceid=IN:en"
                    )
                    for query in queries
                ],
                return_exceptions=True,
            )
        for response in responses:
            if isinstance(response, Exception):
                continue
            response.raise_for_status()
            root = ET.fromstring(response.text)
            for item in root.findall(".//item")[:12]:
                title = item.findtext("title", default="").strip()
                if not title:
                    continue
                published = _parse_pubdate(item.findtext("pubDate"))
                if not self._within_lookback(published):
                    continue
                raw_items.append(
                    {
                        "title": title,
                        "source": item.findtext("source", default="").strip() or "unknown",
                        "published_at": published,
                    }
                )
        events = self._dedupe_news([self._classify_news(item) for item in raw_items])
        denominator = sum(event.confidence * event.recency_weight for event in events) or 1.0
        score = sum(event.weighted_score for event in events) / denominator
        confidence = min(sum(event.confidence * event.recency_weight for event in events) / max(len(events), 1), 1.0)
        return {
            "score": round(max(min(score, 1.0), -1.0), 3),
            "confidence": round(confidence, 3),
            "headlines": [event.title for event in events[:10]],
            "items": [event.to_dict() for event in events[:12]],
        }

    def _classify_news(self, item: dict[str, Any]) -> MacroNewsItem:
        title = item["title"]
        published_at = item.get("published_at")
        lower = title.lower()
        positive = sum(1 for term in RISK_ON_TERMS if term in lower)
        negative = sum(1 for term in RISK_OFF_TERMS if term in lower)
        score = max(min((positive - negative) / 3, 1.0), -1.0)
        confidence = min(0.35 + abs(score), 0.9)
        recency_weight = self._recency_weight(published_at)
        return MacroNewsItem(
            title=title,
            source=item.get("source", "unknown"),
            published_at=published_at.isoformat() if published_at else None,
            score=round(score, 3),
            confidence=round(confidence, 3),
            recency_weight=round(recency_weight, 3),
            weighted_score=round(score * confidence * recency_weight, 3),
        )

    def _dedupe_news(self, events: list[MacroNewsItem]) -> list[MacroNewsItem]:
        seen: set[str] = set()
        unique: list[MacroNewsItem] = []
        for event in sorted(events, key=lambda item: abs(item.weighted_score), reverse=True):
            key = _fingerprint(event.title)
            if key in seen:
                continue
            seen.add(key)
            unique.append(event)
            if len(unique) >= 14:
                break
        return unique

    def _market_signal(self, change_pct: float, impact: str) -> float:
        normalized = max(min(change_pct / 2.5, 1.0), -1.0)
        if impact in {"risk_asset", "india_equity"}:
            return normalized
        if impact in {"india_cost", "defensive", "rupee_pressure"}:
            return -normalized
        return 0.0

    def _regime(self, score: float) -> str:
        if score >= 0.28:
            return "risk-on"
        if score <= -0.28:
            return "risk-off"
        return "neutral"

    def _within_lookback(self, published_at: datetime | None) -> bool:
        if not published_at:
            return False
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.settings.global_news_lookback_days)
        return published_at >= cutoff

    def _recency_weight(self, published_at: datetime | None) -> float:
        if not published_at:
            return 0.5
        age_hours = max((datetime.now(timezone.utc) - published_at).total_seconds() / 3600, 0)
        return max(math.exp(-age_hours / 36), 0.15)

    def _epoch_to_iso(self, value: Any) -> str:
        if not value:
            return utc_now()
        try:
            return datetime.fromtimestamp(float(value), timezone.utc).isoformat()
        except Exception:
            return utc_now()


def _parse_pubdate(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except Exception:
        return None


def _fingerprint(title: str) -> str:
    normalized = " ".join(token for token in title.lower().split() if len(token) > 2)
    return hashlib.sha1(normalized[:180].encode("utf-8")).hexdigest()
