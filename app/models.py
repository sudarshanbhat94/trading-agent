from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Literal


Action = Literal["BUY", "SELL", "HOLD"]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class Quote:
    symbol: str
    price: float
    source: str
    asof: str
    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float | None = None
    volume: float | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Candle:
    symbol: str
    ts: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    source: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class TechnicalSnapshot:
    score: float
    trend: str
    rsi: float | None
    sma_fast: float | None
    sma_slow: float | None
    momentum_pct: float | None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Decision:
    symbol: str
    action: Action
    confidence: float
    price: float
    technical_score: float
    sentiment_score: float
    reason: str
    asof: str
    strategy: str = "llm_primary"
    details_json: str = "{}"

    def to_dict(self) -> dict:
        return asdict(self)
