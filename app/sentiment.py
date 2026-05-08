from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import quote_plus, urlparse

import httpx

from .config import Settings
from .db import Database
from .llm_usage import build_llm_usage_event
from .models import utc_now


SOURCE_WEIGHTS = {
    "nseindia.com": 1.0,
    "bseindia.com": 1.0,
    "reuters.com": 0.9,
    "bloomberg.com": 0.9,
    "moneycontrol.com": 0.78,
    "economictimes.indiatimes.com": 0.76,
    "livemint.com": 0.74,
    "business-standard.com": 0.72,
    "financialexpress.com": 0.68,
    "cnbctv18.com": 0.68,
}

EVENT_PRIORS = {
    "earnings": 0.18,
    "guidance": 0.18,
    "order_win": 0.2,
    "analyst_upgrade": 0.16,
    "analyst_downgrade": -0.16,
    "legal_regulatory": -0.24,
    "fraud_governance": -0.35,
    "debt_liquidity": -0.2,
    "management": 0.0,
    "corporate_action": 0.02,
    "macro_sector": 0.0,
    "neutral": 0.0,
}

POSITIVE_TERMS = {
    "beats",
    "beat",
    "profit",
    "surge",
    "surges",
    "upgrade",
    "record",
    "growth",
    "strong",
    "rally",
    "rises",
    "wins",
    "expands",
    "order",
    "contract",
    "approval",
    "dividend",
}

NEGATIVE_TERMS = {
    "miss",
    "falls",
    "fall",
    "loss",
    "weak",
    "downgrade",
    "probe",
    "penalty",
    "slump",
    "cuts",
    "debt",
    "fraud",
    "concern",
    "default",
    "resigns",
}


@dataclass
class NewsEvent:
    title: str
    source: str
    url: str
    published_at: str | None
    event_type: str
    score: float
    confidence: float
    source_weight: float
    recency_weight: float
    weighted_score: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SentimentResult:
    score: float
    confidence: float
    headlines: list[str]
    events: list[NewsEvent]


class SentimentService:
    def __init__(self, settings: Settings, db: Database) -> None:
        self.settings = settings
        self.db = db
        self._cache: dict[str, tuple[float, SentimentResult]] = {}
        self._cursor = 0

    async def scores_for_cycle(self, universe: list[dict[str, Any]]) -> dict[str, float]:
        if not self.settings.enable_news_sentiment:
            return {row["symbol"]: 0.0 for row in universe}

        symbols_to_refresh = self._next_symbols(universe)
        await asyncio.gather(*(self._refresh(row) for row in symbols_to_refresh), return_exceptions=True)
        return {
            row["symbol"]: self._cache.get(row["symbol"], (0, SentimentResult(0.0, 0.0, [], [])))[1].score
            for row in universe
        }

    def latest_for_symbol(self, symbol: str) -> dict[str, Any]:
        cached = self._cache.get(symbol)
        if not cached:
            return {"score": 0.0, "confidence": 0.0, "headlines": [], "events": []}
        return self._result_payload(cached[1])

    async def analyze_symbol_news(self, row: dict[str, Any]) -> dict[str, Any]:
        if not self.settings.enable_news_sentiment:
            result = SentimentResult(score=0.0, confidence=0.0, headlines=[], events=[])
        else:
            try:
                raw_items = await self._fetch_news_items(row)
                events = self._dedupe_events([self._classify_item(item) for item in raw_items])
                if self._llm_sentiment_enabled() and events:
                    events = await self._llm_refine_events(row, events)
                result = self._aggregate(events)
            except Exception as exc:
                self.db.insert_agent_log(
                    "WARN",
                    "sentiment",
                    "manual_news_fetch_failed",
                    f"News sentiment fetch failed for {row['symbol']}; using neutral sentiment",
                    {"symbol": row["symbol"], "error": f"{exc.__class__.__name__}: {str(exc)[:240]}"},
                )
                result = SentimentResult(score=0.0, confidence=0.0, headlines=[], events=[])
        self._cache[row["symbol"]] = (time.monotonic(), result)
        self._persist(row["symbol"], result)
        return self._result_payload(result)

    def _next_symbols(self, universe: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not universe:
            return []
        limit = min(self.settings.news_symbols_per_cycle, len(universe))
        selected = []
        now = time.monotonic()
        for _ in range(len(universe)):
            row = universe[self._cursor % len(universe)]
            self._cursor += 1
            cached = self._cache.get(row["symbol"])
            if cached and now - cached[0] < self.settings.news_cache_seconds:
                continue
            selected.append(row)
            if len(selected) >= limit:
                break
        return selected

    def _result_payload(self, result: SentimentResult) -> dict[str, Any]:
        return {
            "score": result.score,
            "confidence": result.confidence,
            "headlines": result.headlines[:12],
            "events": [event.to_dict() for event in result.events[:12]],
            "asof": utc_now(),
        }

    async def _refresh(self, row: dict[str, Any]) -> None:
        symbol = row["symbol"]
        try:
            raw_items = await self._fetch_news_items(row)
            events = self._dedupe_events([self._classify_item(item) for item in raw_items])
            if self._llm_sentiment_enabled() and events:
                events = await self._llm_refine_events(row, events)
            result = self._aggregate(events)
        except Exception:
            result = SentimentResult(score=0.0, confidence=0.0, headlines=[], events=[])
        self._cache[symbol] = (time.monotonic(), result)
        self._persist(symbol, result)

    async def _fetch_news_items(self, row: dict[str, Any]) -> list[dict[str, Any]]:
        company = row.get("name") or row["symbol"]
        symbol = row["symbol"]
        queries = [
            f'"{company}" {symbol} NSE stock when:{self.settings.news_lookback_days}d',
            f'"{company}" results earnings guidance India when:{self.settings.news_lookback_days}d',
            f'"{company}" order win contract rating downgrade upgrade when:{self.settings.news_lookback_days}d',
            f'"{company}" fraud probe penalty resignation debt when:{self.settings.news_lookback_days}d',
        ]
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
        items: list[dict[str, Any]] = []
        for response in responses:
            if isinstance(response, Exception):
                continue
            try:
                response.raise_for_status()
            except Exception:
                continue
            root = ET.fromstring(response.text)
            for item in root.findall(".//item")[:15]:
                title = item.findtext("title", default="").strip()
                if not title:
                    continue
                if not self._is_relevant(row, title):
                    continue
                source = item.findtext("source", default="").strip()
                url = item.findtext("link", default="")
                published = item.findtext("pubDate")
                parsed_published = _parse_pubdate(published)
                if not self._within_lookback(parsed_published):
                    continue
                items.append({"title": title, "source": source, "url": url, "published": published})
        return items

    def _is_relevant(self, row: dict[str, Any], title: str) -> bool:
        text = title.lower()
        symbol = str(row["symbol"]).lower().replace("&", "")
        compact = re.sub(r"[^a-z0-9]+", "", text)
        if symbol and symbol in compact:
            return True
        company = str(row.get("name") or "").lower()
        stop_words = {
            "bank",
            "india",
            "indian",
            "industries",
            "corporation",
            "company",
            "limited",
            "ltd",
            "and",
            "the",
            "of",
        }
        tokens = [
            token
            for token in re.sub(r"[^a-z0-9 ]+", " ", company).split()
            if len(token) >= 4 and token not in stop_words
        ]
        return any(token in text for token in tokens[:3])

    def _within_lookback(self, published_at: datetime | None) -> bool:
        if not published_at:
            return False
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.settings.news_lookback_days)
        return published_at >= cutoff

    def _classify_item(self, item: dict[str, Any]) -> NewsEvent:
        title = item["title"]
        source = item.get("source") or _domain(item.get("url", ""))
        published_at = _parse_pubdate(item.get("published"))
        source_weight = self._source_weight(source, item.get("url", ""))
        recency_weight = self._recency_weight(published_at)
        event_type = self._event_type(title)
        lexical_score = self._lexical_score(title)
        score = max(min(lexical_score + EVENT_PRIORS.get(event_type, 0.0), 1.0), -1.0)
        confidence = min(0.35 + abs(score) + (source_weight * 0.25), 0.95)
        weighted_score = score * confidence * source_weight * recency_weight
        return NewsEvent(
            title=title,
            source=source or "unknown",
            url=item.get("url", ""),
            published_at=published_at.isoformat() if published_at else None,
            event_type=event_type,
            score=round(score, 3),
            confidence=round(confidence, 3),
            source_weight=round(source_weight, 3),
            recency_weight=round(recency_weight, 3),
            weighted_score=round(weighted_score, 3),
        )

    def _dedupe_events(self, events: list[NewsEvent]) -> list[NewsEvent]:
        seen: set[str] = set()
        unique: list[NewsEvent] = []
        for event in sorted(events, key=lambda item: abs(item.weighted_score), reverse=True):
            key = _fingerprint(event.title)
            if key in seen:
                continue
            seen.add(key)
            unique.append(event)
            if len(unique) >= 18:
                break
        return unique

    async def _llm_refine_events(self, row: dict[str, Any], events: list[NewsEvent]) -> list[NewsEvent]:
        attempts: list[dict[str, Any]] = []
        for model in self._llm_model_candidates():
            payload = {
                "model": model,
                "temperature": min(self.settings.llm_temperature, 0.2),
                "top_p": min(self.settings.llm_top_p, 0.7),
                "max_tokens": max(256, min(self.settings.llm_max_tokens, 900)),
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "Classify Indian equity news for trading sentiment. Return JSON only with key events. "
                            "Each event must include index, event_type, score -1..1, confidence 0..1. "
                            "Prefer HOLD/neutral sentiment when headlines are ambiguous."
                        ),
                    },
                    {
                        "role": "user",
                        "content": json.dumps(
                            {
                                "symbol": row["symbol"],
                                "company": row.get("name"),
                                "events": [
                                    {"index": index, "title": event.title, "source": event.source}
                                    for index, event in enumerate(events)
                                ],
                            },
                            separators=(",", ":"),
                        ),
                    },
                ],
            }
            self._apply_llm_model_options(payload, model=model)
            started = time.monotonic()
            try:
                async with httpx.AsyncClient(
                    timeout=self.settings.llm_timeout_seconds,
                    headers={"Authorization": f"Bearer {self._llm_key()}"},
                ) as client:
                    response = await asyncio.wait_for(
                        client.post(self._llm_chat_completions_url(), json=payload),
                        timeout=min(max(self.settings.llm_timeout_seconds, 8), 20),
                    )
                    response.raise_for_status()
                data = response.json()
                content = data["choices"][0]["message"]["content"]
                self._record_llm_usage(
                    payload=payload,
                    response_data=data,
                    output_text=content,
                    model=model,
                    latency_ms=round((time.monotonic() - started) * 1000),
                    symbol=row["symbol"],
                )
                parsed = json.loads(_extract_json(content))
                by_index = {int(item["index"]): item for item in parsed.get("events", []) if "index" in item}
                refined: list[NewsEvent] = []
                for index, event in enumerate(events):
                    update = by_index.get(index)
                    if not update:
                        refined.append(event)
                        continue
                    score = max(min(float(update.get("score", event.score)), 1.0), -1.0)
                    confidence = max(min(float(update.get("confidence", event.confidence)), 1.0), 0.0)
                    event_type = str(update.get("event_type", event.event_type))[:40]
                    weighted = score * confidence * event.source_weight * event.recency_weight
                    refined.append(
                        NewsEvent(
                            title=event.title,
                            source=event.source,
                            url=event.url,
                            published_at=event.published_at,
                            event_type=event_type,
                            score=round(score, 3),
                            confidence=round(confidence, 3),
                            source_weight=event.source_weight,
                            recency_weight=event.recency_weight,
                            weighted_score=round(weighted, 3),
                        )
                    )
                attempts.append({"model": model, "status": "ok", "latency_ms": round((time.monotonic() - started) * 1000)})
                return refined
            except Exception as exc:
                attempts.append(
                    {
                        "model": model,
                        "status": "failed",
                        "latency_ms": round((time.monotonic() - started) * 1000),
                        "error": f"{exc.__class__.__name__}: {str(exc)[:180]}",
                    }
                )
                continue
        self.db.insert_agent_log(
            "WARN",
            "sentiment",
            "llm_sentiment_refine_failed",
            f"LLM sentiment refinement failed for {row['symbol']}; using lexical sentiment",
            {"symbol": row["symbol"], "attempts": attempts[:8]},
        )
        return events

    def _aggregate(self, events: list[NewsEvent]) -> SentimentResult:
        if not events:
            return SentimentResult(score=0.0, confidence=0.0, headlines=[], events=[])
        denominator = sum(abs(event.confidence * event.source_weight * event.recency_weight) for event in events) or 1.0
        score = sum(event.weighted_score for event in events) / denominator
        confidence = min(sum(event.confidence * event.source_weight * event.recency_weight for event in events) / len(events), 1.0)
        return SentimentResult(
            score=round(max(min(score, 1.0), -1.0), 3),
            confidence=round(confidence, 3),
            headlines=[event.title for event in events[:12]],
            events=events,
        )

    def _lexical_score(self, headline: str) -> float:
        words = {word.strip(".,:;!?()[]'\"").lower() for word in headline.split()}
        total = len(words & POSITIVE_TERMS) - len(words & NEGATIVE_TERMS)
        return max(min(total / 3, 1.0), -1.0)

    def _event_type(self, headline: str) -> str:
        text = headline.lower()
        patterns = [
            ("fraud_governance", r"fraud|forensic|governance|whistleblower"),
            ("legal_regulatory", r"probe|penalty|sebi|ed |cbi|tax notice|regulator|lawsuit"),
            ("debt_liquidity", r"debt|default|liquidity|pledge|downgrade"),
            ("analyst_upgrade", r"upgrade|raises target|buy rating"),
            ("analyst_downgrade", r"downgrade|cuts target|sell rating"),
            ("order_win", r"order win|wins order|contract|deal|approval"),
            ("earnings", r"profit|revenue|ebitda|quarter|q[1-4]|results|earnings"),
            ("guidance", r"guidance|outlook|forecast"),
            ("management", r"ceo|cfo|resign|appoint|management"),
            ("corporate_action", r"dividend|buyback|split|bonus|merger|demerger"),
            ("macro_sector", r"rbi|inflation|crude|rupee|tariff|policy"),
        ]
        for label, pattern in patterns:
            if re.search(pattern, text):
                return label
        return "neutral"

    def _source_weight(self, source: str, url: str) -> float:
        domain = _domain(url) or source.lower().replace("www.", "")
        for known, weight in SOURCE_WEIGHTS.items():
            if known in domain:
                return weight
        if source and any(word in source.lower() for word in ["exchange", "nse", "bse"]):
            return 0.95
        return 0.55

    def _recency_weight(self, published_at: datetime | None) -> float:
        if not published_at:
            return 0.55
        age_hours = max((datetime.now(timezone.utc) - published_at).total_seconds() / 3600, 0)
        return max(math.exp(-age_hours / 72), 0.15)

    def _llm_sentiment_enabled(self) -> bool:
        if not self.settings.enable_llm_sentiment:
            return False
        return self.settings.llm_provider == "deepseek" and bool(self.settings.deepseek_api_key)

    def _llm_base_url(self) -> str:
        return self.settings.deepseek_base_url

    def _llm_chat_completions_url(self) -> str:
        base_url = self._llm_base_url().rstrip("/")
        return f"{base_url}/chat/completions"

    def _llm_model(self) -> str:
        return self.settings.deepseek_model

    def _llm_model_candidates(self) -> list[str]:
        return [self.settings.deepseek_model]

    def _llm_key(self) -> str:
        return self.settings.deepseek_api_key

    def _apply_llm_model_options(self, payload: dict[str, Any], model: str | None = None) -> None:
        payload["response_format"] = {"type": "json_object"}

    def _record_llm_usage(
        self,
        *,
        payload: dict[str, Any],
        response_data: dict[str, Any],
        output_text: str,
        model: str,
        latency_ms: int,
        symbol: str,
    ) -> None:
        try:
            event = build_llm_usage_event(
                component="sentiment",
                purpose="sentiment_refine",
                provider="deepseek",
                model=model,
                payload=payload,
                response_data=response_data,
                output_text=output_text,
                latency_ms=latency_ms,
                details={
                    "symbol": symbol,
                    "api_usage_present": bool(response_data.get("usage")),
                    "response_id": response_data.get("id"),
                },
            )
            self.db.insert_llm_usage(event)
        except Exception:
            return

    def _persist(self, symbol: str, result: SentimentResult) -> None:
        with self.db.connect() as conn:
            conn.execute(
                """
                insert into sentiment_events
                    (ts, symbol, score, headline_count, headlines_json, confidence, events_json)
                values (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    utc_now(),
                    symbol,
                    result.score,
                    len(result.headlines),
                    json.dumps(result.headlines[:12]),
                    result.confidence,
                    json.dumps([event.to_dict() for event in result.events[:18]]),
                ),
            )


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


def _domain(url: str) -> str:
    try:
        return urlparse(url).netloc.lower().replace("www.", "")
    except Exception:
        return ""


def _fingerprint(title: str) -> str:
    normalized = re.sub(r"[^a-z0-9 ]+", " ", title.lower())
    tokens = [token for token in normalized.split() if len(token) > 2]
    return hashlib.sha1(" ".join(tokens[:16]).encode("utf-8")).hexdigest()


def _extract_json(content: str) -> str:
    text = content.strip()
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end >= start:
        return text[start : end + 1]
    return text
