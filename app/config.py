from __future__ import annotations

import os
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


load_dotenv()


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return int(raw) if raw not in (None, "") else default


def _float(name: str, default: float) -> float:
    raw = os.getenv(name)
    return float(raw) if raw not in (None, "") else default


def _path(name: str, default: str) -> Path:
    return Path(os.getenv(name, default)).expanduser().resolve()


@dataclass(frozen=True)
class Settings:
    host: str = os.getenv("HOST", "0.0.0.0")
    port: int = _int("PORT", 8000)
    database_path: Path = _path("DATABASE_PATH", "./var/trading_agent.db")
    universe_csv: Path = _path("UNIVERSE_CSV", "./data/universe.csv")
    auto_start_agent: bool = _bool("AUTO_START_AGENT", True)
    agent_interval_seconds: int = _int("AGENT_INTERVAL_SECONDS", 20)
    admin_password: str = os.getenv("ADMIN_PASSWORD", "")
    admin_username: str = os.getenv("ADMIN_USERNAME", "admin")
    auth_session_secret: str = os.getenv("AUTH_SESSION_SECRET", "")
    admin_session_hours: int = _int("ADMIN_SESSION_HOURS", 12)

    initial_cash_inr: float = _float("INITIAL_CASH_INR", 1_000_000)
    max_positions: int = _int("MAX_POSITIONS", 12)
    max_position_pct: float = _float("MAX_POSITION_PCT", 0.12)
    max_order_value_pct: float = _float("MAX_ORDER_VALUE_PCT", 0.04)
    max_trades_per_cycle: int = _int("MAX_TRADES_PER_CYCLE", 4)
    stop_loss_pct: float = _float("STOP_LOSS_PCT", 0.035)
    take_profit_pct: float = _float("TAKE_PROFIT_PCT", 0.08)
    daily_loss_limit_pct: float = _float("DAILY_LOSS_LIMIT_PCT", 0.025)

    market_data_provider: str = os.getenv("MARKET_DATA_PROVIDER", "simulated").strip().lower()
    kite_api_key: str = os.getenv("KITE_API_KEY", "")
    kite_access_token: str = os.getenv("KITE_ACCESS_TOKEN", "")
    upstox_access_token: str = os.getenv("UPSTOX_ACCESS_TOKEN", "")
    upstox_sandbox_access_token: str = os.getenv("UPSTOX_SANDBOX_ACCESS_TOKEN", "")
    upstox_api_base_url: str = os.getenv("UPSTOX_API_BASE_URL", "https://api.upstox.com/v2").rstrip("/")
    upstox_order_base_url: str = os.getenv("UPSTOX_ORDER_BASE_URL", "https://api-hft.upstox.com/v2").rstrip("/")
    upstox_candle_interval: str = os.getenv("UPSTOX_CANDLE_INTERVAL", "30minute")
    upstox_candle_lookback_days: int = _int("UPSTOX_CANDLE_LOOKBACK_DAYS", 3)

    enable_news_sentiment: bool = _bool("ENABLE_NEWS_SENTIMENT", True)
    enable_llm_sentiment: bool = _bool("ENABLE_LLM_SENTIMENT", True)
    news_cache_seconds: int = _int("NEWS_CACHE_SECONDS", 1800)
    news_lookback_days: int = _int("NEWS_LOOKBACK_DAYS", 7)
    news_symbols_per_cycle: int = _int("NEWS_SYMBOLS_PER_CYCLE", 5)

    execution_mode: str = os.getenv("EXECUTION_MODE", "paper").strip().lower()
    live_trading_enabled: bool = _bool("LIVE_TRADING_ENABLED", False)
    live_trading_confirm: str = os.getenv("LIVE_TRADING_CONFIRM", "")
    upstox_order_product: str = os.getenv("UPSTOX_ORDER_PRODUCT", "D")
    upstox_order_validity: str = os.getenv("UPSTOX_ORDER_VALIDITY", "DAY")
    upstox_order_type: str = os.getenv("UPSTOX_ORDER_TYPE", "MARKET")

    llm_provider: str = os.getenv("LLM_PROVIDER", "offline").strip().lower()
    llm_decision_mode: str = os.getenv("LLM_DECISION_MODE", "offline").strip().lower()
    llm_base_url: str = os.getenv("LLM_BASE_URL", "https://api.openai.com").rstrip("/")
    llm_api_key: str = os.getenv("LLM_API_KEY", "")
    llm_model: str = os.getenv("LLM_MODEL", "gpt-4.1-mini")
    llm_temperature: float = _float("LLM_TEMPERATURE", 0.05)
    llm_top_p: float = _float("LLM_TOP_P", 0.7)
    llm_max_tokens: int = _int("LLM_MAX_TOKENS", 900)
    llm_max_symbols_per_cycle: int = _int("LLM_MAX_SYMBOLS_PER_CYCLE", 8)
    llm_primary_min_confidence: float = _float("LLM_PRIMARY_MIN_CONFIDENCE", 0.62)
    llm_reasoning_effort: str = os.getenv("LLM_REASONING_EFFORT", "none").strip().lower()
    llm_thinking_enabled: bool = _bool("LLM_THINKING_ENABLED", False)
    llm_streaming_enabled: bool = _bool("LLM_STREAMING_ENABLED", True)
    llm_timeout_seconds: int = _int("LLM_TIMEOUT_SECONDS", 45)
    nvidia_api_key: str = os.getenv("NVIDIA_API_KEY", "")
    nvidia_base_url: str = os.getenv("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com").rstrip("/")
    nvidia_model: str = os.getenv("NVIDIA_MODEL", "deepseek-ai/deepseek-r1")


SECRET_FIELDS = {
    "kite_api_key",
    "kite_access_token",
    "upstox_access_token",
    "upstox_sandbox_access_token",
    "llm_api_key",
    "nvidia_api_key",
    "live_trading_confirm",
    "admin_password",
    "auth_session_secret",
}


CONFIG_SCHEMA: list[dict[str, Any]] = [
    {"key": "execution_mode", "label": "Execution Mode", "type": "select", "category": "Demo Account", "choices": ["paper", "upstox_sandbox", "upstox_live"]},
    {"key": "initial_cash_inr", "label": "Demo Cash", "type": "number", "category": "Demo Account", "min": 1000, "step": 1000},
    {"key": "auto_start_agent", "label": "Auto Start", "type": "boolean", "category": "Demo Account"},
    {"key": "agent_interval_seconds", "label": "Cycle Seconds", "type": "number", "category": "Demo Account", "min": 5, "step": 1},
    {"key": "admin_password", "label": "Admin Password", "type": "secret", "category": "Access Control"},
    {"key": "admin_username", "label": "Admin Username", "type": "text", "category": "Access Control"},
    {"key": "auth_session_secret", "label": "Session Secret", "type": "secret", "category": "Access Control"},
    {"key": "admin_session_hours", "label": "Session Hours", "type": "number", "category": "Access Control", "min": 1, "step": 1},
    {"key": "market_data_provider", "label": "Market Data", "type": "select", "category": "Market Data", "choices": ["simulated", "yahoo", "kite", "upstox"]},
    {"key": "upstox_access_token", "label": "Upstox Access Token", "type": "secret", "category": "Market Data"},
    {"key": "upstox_sandbox_access_token", "label": "Upstox Sandbox Token", "type": "secret", "category": "Market Data"},
    {"key": "upstox_api_base_url", "label": "Upstox Data URL", "type": "text", "category": "Market Data"},
    {"key": "upstox_order_base_url", "label": "Upstox Order URL", "type": "text", "category": "Market Data"},
    {"key": "upstox_candle_interval", "label": "Candle Interval", "type": "select", "category": "Market Data", "choices": ["1minute", "30minute", "day", "week", "month"]},
    {"key": "upstox_candle_lookback_days", "label": "Candle Lookback Days", "type": "number", "category": "Market Data", "min": 1, "step": 1},
    {"key": "kite_api_key", "label": "Kite API Key", "type": "secret", "category": "Market Data"},
    {"key": "kite_access_token", "label": "Kite Access Token", "type": "secret", "category": "Market Data"},
    {"key": "llm_provider", "label": "LLM Provider", "type": "select", "category": "LLM Brain", "choices": ["offline", "nvidia", "openai_compatible"]},
    {"key": "llm_decision_mode", "label": "Decision Mode", "type": "select", "category": "LLM Brain", "choices": ["offline", "review", "primary"]},
    {"key": "nvidia_api_key", "label": "NVIDIA API Key", "type": "secret", "category": "LLM Brain"},
    {"key": "nvidia_base_url", "label": "NVIDIA Base URL", "type": "text", "category": "LLM Brain"},
    {"key": "nvidia_model", "label": "NVIDIA Model", "type": "text", "category": "LLM Brain"},
    {"key": "llm_base_url", "label": "OpenAI-Compatible URL", "type": "text", "category": "LLM Brain"},
    {"key": "llm_api_key", "label": "OpenAI-Compatible Key", "type": "secret", "category": "LLM Brain"},
    {"key": "llm_model", "label": "OpenAI-Compatible Model", "type": "text", "category": "LLM Brain"},
    {"key": "llm_temperature", "label": "LLM Temperature", "type": "number", "category": "LLM Brain", "min": 0, "max": 2, "step": 0.01},
    {"key": "llm_top_p", "label": "LLM Top P", "type": "number", "category": "LLM Brain", "min": 0, "max": 1, "step": 0.01},
    {"key": "llm_max_tokens", "label": "LLM Max Tokens", "type": "number", "category": "LLM Brain", "min": 24, "step": 128},
    {"key": "llm_max_symbols_per_cycle", "label": "LLM Symbols/Cycle", "type": "number", "category": "LLM Brain", "min": 1, "step": 1},
    {"key": "llm_primary_min_confidence", "label": "Min LLM Confidence", "type": "number", "category": "LLM Brain", "min": 0, "max": 1, "step": 0.01},
    {"key": "llm_reasoning_effort", "label": "Reasoning Effort", "type": "select", "category": "LLM Brain", "choices": ["none", "high", "max"]},
    {"key": "llm_thinking_enabled", "label": "Model Thinking", "type": "boolean", "category": "LLM Brain"},
    {"key": "llm_streaming_enabled", "label": "Stream Responses", "type": "boolean", "category": "LLM Brain"},
    {"key": "llm_timeout_seconds", "label": "LLM Timeout Seconds", "type": "number", "category": "LLM Brain", "min": 5, "step": 5},
    {"key": "max_positions", "label": "Max Positions", "type": "number", "category": "Risk", "min": 1, "step": 1},
    {"key": "max_position_pct", "label": "Max Position %", "type": "number", "category": "Risk", "min": 0.01, "max": 1, "step": 0.01},
    {"key": "max_order_value_pct", "label": "Max Order %", "type": "number", "category": "Risk", "min": 0.01, "max": 1, "step": 0.01},
    {"key": "max_trades_per_cycle", "label": "Max Trades/Cycle", "type": "number", "category": "Risk", "min": 1, "step": 1},
    {"key": "stop_loss_pct", "label": "Stop Loss %", "type": "number", "category": "Risk", "min": 0, "max": 1, "step": 0.005},
    {"key": "take_profit_pct", "label": "Take Profit %", "type": "number", "category": "Risk", "min": 0, "max": 2, "step": 0.005},
    {"key": "daily_loss_limit_pct", "label": "Daily Loss Limit %", "type": "number", "category": "Risk", "min": 0, "max": 1, "step": 0.005},
    {"key": "enable_news_sentiment", "label": "News Sentiment", "type": "boolean", "category": "Sentiment"},
    {"key": "enable_llm_sentiment", "label": "LLM Sentiment", "type": "boolean", "category": "Sentiment"},
    {"key": "news_cache_seconds", "label": "News Cache Seconds", "type": "number", "category": "Sentiment", "min": 60, "step": 60},
    {"key": "news_lookback_days", "label": "News Lookback Days", "type": "number", "category": "Sentiment", "min": 1, "step": 1},
    {"key": "news_symbols_per_cycle", "label": "News Symbols/Cycle", "type": "number", "category": "Sentiment", "min": 0, "step": 1},
    {"key": "live_trading_enabled", "label": "Live Trading Enabled", "type": "boolean", "category": "Live Protection"},
    {"key": "live_trading_confirm", "label": "Live Confirm Phrase", "type": "secret", "category": "Live Protection"},
    {"key": "upstox_order_product", "label": "Order Product", "type": "select", "category": "Live Protection", "choices": ["D", "I"]},
    {"key": "upstox_order_validity", "label": "Order Validity", "type": "select", "category": "Live Protection", "choices": ["DAY", "IOC"]},
    {"key": "upstox_order_type", "label": "Order Type", "type": "select", "category": "Live Protection", "choices": ["MARKET", "LIMIT"]},
]


CONFIG_KEYS = {item["key"] for item in CONFIG_SCHEMA}


def coerce_setting_value(key: str, value: Any, base: Settings) -> Any:
    current = getattr(base, key)
    if isinstance(current, bool):
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(current, int) and not isinstance(current, bool):
        return int(value)
    if isinstance(current, float):
        return float(value)
    if isinstance(current, Path):
        return Path(str(value)).expanduser().resolve()
    return str(value).strip()


def settings_from_overrides(base: Settings, overrides: dict[str, Any]) -> Settings:
    kwargs: dict[str, Any] = {}
    field_names = {field.name for field in fields(Settings)}
    for key, value in overrides.items():
        if key not in field_names or key not in CONFIG_KEYS:
            continue
        if key in SECRET_FIELDS and value == "":
            continue
        kwargs[key] = coerce_setting_value(key, value, base)
    return Settings(**{field.name: getattr(base, field.name) for field in fields(Settings)} | kwargs)


def public_settings(settings: Settings) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for item in CONFIG_SCHEMA:
        key = item["key"]
        value = getattr(settings, key)
        if key in SECRET_FIELDS:
            output[key] = {"saved": bool(value), "value": ""}
        else:
            output[key] = value
    return output
