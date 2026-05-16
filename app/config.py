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


def _market_data_provider(default: str = "upstox") -> str:
    provider = os.getenv("MARKET_DATA_PROVIDER", default).strip().lower()
    if provider == "indstocks":
        provider = "upstox"
    elif provider == "indstocks_yahoo":
        provider = "upstox_yahoo"
    choices = {"simulated", "upstox", "upstox_yahoo", "kite", "kite_yahoo", "nubra", "yahoo"}
    return provider if provider in choices else default


@dataclass(frozen=True)
class Settings:
    host: str = os.getenv("HOST", "0.0.0.0")
    port: int = _int("PORT", 8000)
    database_path: Path = _path("DATABASE_PATH", "./var/trading_agent.db")
    universe_csv: Path = _path("UNIVERSE_CSV", "./data/universe.csv")
    us_universe_csv: Path = _path("US_UNIVERSE_CSV", "./data/us_universe.csv")
    universe_source: str = os.getenv("UNIVERSE_SOURCE", "csv").strip().lower()
    market_region: str = os.getenv("MARKET_REGION", "IN").strip().upper()
    nse_equity_list_url: str = os.getenv(
        "NSE_EQUITY_LIST_URL",
        "https://archives.nseindia.com/content/equities/EQUITY_L.csv",
    ).strip()
    nse_universe_refresh_on_start: bool = _bool("NSE_UNIVERSE_REFRESH_ON_START", False)
    nse_universe_series: str = os.getenv("NSE_UNIVERSE_SERIES", "EQ").strip().upper()
    universe_symbols_per_cycle: int = _int("UNIVERSE_SYMBOLS_PER_CYCLE", 0)
    dynamic_opportunity_scan_enabled: bool = _bool("DYNAMIC_OPPORTUNITY_SCAN_ENABLED", True)
    dynamic_scan_raw_limit: int = _int("DYNAMIC_SCAN_RAW_LIMIT", 500)
    dynamic_scan_candidate_limit: int = _int("DYNAMIC_SCAN_CANDIDATE_LIMIT", 60)
    dynamic_scan_min_score: float = _float("DYNAMIC_SCAN_MIN_SCORE", 0.58)
    dynamic_scan_require_active_setup: bool = _bool("DYNAMIC_SCAN_REQUIRE_ACTIVE_SETUP", True)
    dynamic_scan_min_price: float = _float("DYNAMIC_SCAN_MIN_PRICE", 10.0)
    dynamic_scan_min_turnover_inr: float = _float("DYNAMIC_SCAN_MIN_TURNOVER_INR", 50_000_000.0)
    dynamic_scan_breakout_distance_pct: float = _float("DYNAMIC_SCAN_BREAKOUT_DISTANCE_PCT", 3.0)
    dynamic_scan_sentiment_enabled: bool = _bool("DYNAMIC_SCAN_SENTIMENT_ENABLED", True)
    dynamic_scan_news_probe_limit: int = _int("DYNAMIC_SCAN_NEWS_PROBE_LIMIT", 16)
    dynamic_scan_sentiment_weight: float = _float("DYNAMIC_SCAN_SENTIMENT_WEIGHT", 0.12)
    auto_start_agent: bool = _bool("AUTO_START_AGENT", True)
    agent_interval_seconds: int = _int("AGENT_INTERVAL_SECONDS", 180)
    cycle_timeout_seconds: int = _int("CYCLE_TIMEOUT_SECONDS", 120)
    skip_market_data_when_closed: bool = _bool("SKIP_MARKET_DATA_WHEN_CLOSED", True)
    post_market_prep_enabled: bool = _bool("POST_MARKET_PREP_ENABLED", True)
    post_market_news_symbols: int = _int("POST_MARKET_NEWS_SYMBOLS", 20)
    admin_password: str = os.getenv("ADMIN_PASSWORD", "")
    admin_username: str = os.getenv("ADMIN_USERNAME", "admin")
    auth_session_secret: str = os.getenv("AUTH_SESSION_SECRET", "")
    admin_session_hours: int = _int("ADMIN_SESSION_HOURS", 12)
    credit_tokens_per_credit: int = _int("CREDIT_TOKENS_PER_CREDIT", 10)
    credit_platform_margin_pct: float = _float("CREDIT_PLATFORM_MARGIN_PCT", 0.20)
    openclaw_bridge_enabled: bool = _bool("OPENCLAW_BRIDGE_ENABLED", True)
    openclaw_bridge_token: str = os.getenv("OPENCLAW_BRIDGE_TOKEN", "")
    openclaw_default_username: str = os.getenv("OPENCLAW_DEFAULT_USERNAME", "sudarshan")
    openclaw_webhook_url: str = os.getenv("OPENCLAW_WEBHOOK_URL", "")
    openclaw_webhook_secret: str = os.getenv("OPENCLAW_WEBHOOK_SECRET", "")
    openclaw_cli_path: str = os.getenv("OPENCLAW_CLI_PATH", "openclaw")
    openclaw_notify_channel: str = os.getenv("OPENCLAW_NOTIFY_CHANNEL", "whatsapp")
    openclaw_notify_target: str = os.getenv("OPENCLAW_NOTIFY_TARGET", "")
    openclaw_notify_ideas: bool = _bool("OPENCLAW_NOTIFY_IDEAS", True)
    openclaw_notify_orders: bool = _bool("OPENCLAW_NOTIFY_ORDERS", True)

    initial_cash_inr: float = _float("INITIAL_CASH_INR", 10_000)
    max_positions: int = _int("MAX_POSITIONS", 5)
    max_position_pct: float = _float("MAX_POSITION_PCT", 0.15)
    max_order_value_pct: float = _float("MAX_ORDER_VALUE_PCT", 0.1)
    max_trades_per_cycle: int = _int("MAX_TRADES_PER_CYCLE", 2)
    stop_loss_pct: float = _float("STOP_LOSS_PCT", 0.035)
    take_profit_pct: float = _float("TAKE_PROFIT_PCT", 0.08)
    daily_loss_limit_pct: float = _float("DAILY_LOSS_LIMIT_PCT", 0.025)

    market_data_provider: str = _market_data_provider("upstox")
    indstocks_access_token: str = os.getenv("INDSTOCKS_ACCESS_TOKEN", "")
    indstocks_api_base_url: str = os.getenv("INDSTOCKS_API_BASE_URL", "https://api.indstocks.com").rstrip("/")
    indstocks_candle_interval: str = os.getenv("INDSTOCKS_CANDLE_INTERVAL", "1day")
    indstocks_candle_lookback_days: int = _int("INDSTOCKS_CANDLE_LOOKBACK_DAYS", 365)
    indstocks_candle_concurrency: int = _int("INDSTOCKS_CANDLE_CONCURRENCY", 2)
    indstocks_candle_request_spacing_ms: int = _int("INDSTOCKS_CANDLE_REQUEST_SPACING_MS", 450)
    indstocks_candle_retry_attempts: int = _int("INDSTOCKS_CANDLE_RETRY_ATTEMPTS", 4)
    indstocks_candle_retry_backoff_seconds: float = _float("INDSTOCKS_CANDLE_RETRY_BACKOFF_SECONDS", 1.0)
    indstocks_fetch_timeout_seconds: int = _int("INDSTOCKS_FETCH_TIMEOUT_SECONDS", 35)
    kite_api_key: str = os.getenv("KITE_API_KEY", "")
    kite_access_token: str = os.getenv("KITE_ACCESS_TOKEN", "")
    upstox_api_key: str = os.getenv("UPSTOX_API_KEY", "")
    upstox_api_secret: str = os.getenv("UPSTOX_API_SECRET", "")
    upstox_redirect_uri: str = os.getenv("UPSTOX_REDIRECT_URI", "")
    upstox_access_token: str = os.getenv("UPSTOX_ACCESS_TOKEN", "")
    upstox_sandbox_access_token: str = os.getenv("UPSTOX_SANDBOX_ACCESS_TOKEN", "")
    upstox_api_base_url: str = os.getenv("UPSTOX_API_BASE_URL", "https://api.upstox.com/v2").rstrip("/")
    upstox_order_base_url: str = os.getenv("UPSTOX_ORDER_BASE_URL", "https://api-hft.upstox.com/v2").rstrip("/")
    upstox_candle_interval: str = os.getenv("UPSTOX_CANDLE_INTERVAL", "30minute")
    upstox_candle_lookback_days: int = _int("UPSTOX_CANDLE_LOOKBACK_DAYS", 3)
    enable_upstox_multi_timeframe_candles: bool = _bool("ENABLE_UPSTOX_MULTI_TIMEFRAME_CANDLES", True)
    upstox_daily_candle_lookback_days: int = _int("UPSTOX_DAILY_CANDLE_LOOKBACK_DAYS", 420)
    upstox_weekly_candle_lookback_days: int = _int("UPSTOX_WEEKLY_CANDLE_LOOKBACK_DAYS", 1100)
    upstox_candle_concurrency: int = _int("UPSTOX_CANDLE_CONCURRENCY", 10)
    upstox_candle_fetch_timeout_seconds: int = _int("UPSTOX_CANDLE_FETCH_TIMEOUT_SECONDS", 35)
    yahoo_candle_interval: str = os.getenv("YAHOO_CANDLE_INTERVAL", "1d")
    yahoo_candle_range: str = os.getenv("YAHOO_CANDLE_RANGE", "1y")
    enable_yahoo_candle_fallback: bool = _bool("ENABLE_YAHOO_CANDLE_FALLBACK", False)
    nubra_api_base_url: str = os.getenv("NUBRA_API_BASE_URL", "https://uatapi.nubra.io").rstrip("/")
    nubra_phone: str = os.getenv("NUBRA_PHONE", "")
    nubra_mpin: str = os.getenv("NUBRA_MPIN", "")
    nubra_session_token: str = os.getenv("NUBRA_SESSION_TOKEN", "")
    nubra_device_id: str = os.getenv("NUBRA_DEVICE_ID", "")
    nubra_price_scale: float = _float("NUBRA_PRICE_SCALE", 100)
    nubra_candle_interval: str = os.getenv("NUBRA_CANDLE_INTERVAL", "15m")
    nubra_candle_lookback_days: int = _int("NUBRA_CANDLE_LOOKBACK_DAYS", 3)
    nubra_candle_symbols_per_cycle: int = _int("NUBRA_CANDLE_SYMBOLS_PER_CYCLE", 30)
    nubra_option_chain_endpoint: str = os.getenv("NUBRA_OPTION_CHAIN_ENDPOINT", "")
    nubra_market_depth_endpoint: str = os.getenv("NUBRA_MARKET_DEPTH_ENDPOINT", "")
    nubra_delivery_endpoint: str = os.getenv("NUBRA_DELIVERY_ENDPOINT", "")
    nubra_oi_endpoint: str = os.getenv("NUBRA_OI_ENDPOINT", "")

    enable_news_sentiment: bool = _bool("ENABLE_NEWS_SENTIMENT", True)
    enable_llm_sentiment: bool = _bool("ENABLE_LLM_SENTIMENT", False)
    news_cache_seconds: int = _int("NEWS_CACHE_SECONDS", 1800)
    news_lookback_days: int = _int("NEWS_LOOKBACK_DAYS", 7)
    news_symbols_per_cycle: int = _int("NEWS_SYMBOLS_PER_CYCLE", 10)
    enable_global_intelligence: bool = _bool("ENABLE_GLOBAL_INTELLIGENCE", True)
    global_cache_seconds: int = _int("GLOBAL_CACHE_SECONDS", 900)
    global_news_lookback_days: int = _int("GLOBAL_NEWS_LOOKBACK_DAYS", 2)
    global_risk_weight: float = _float("GLOBAL_RISK_WEIGHT", 0.1)
    enable_free_institutional_feeds: bool = _bool("ENABLE_FREE_INSTITUTIONAL_FEEDS", True)
    free_feed_cache_seconds: int = _int("FREE_FEED_CACHE_SECONDS", 1800)
    free_feed_timeout_seconds: int = _int("FREE_FEED_TIMEOUT_SECONDS", 10)
    free_feed_option_chain_symbols: str = os.getenv("FREE_FEED_OPTION_CHAIN_SYMBOLS", "NIFTY,BANKNIFTY")
    free_feed_corporate_lookback_days: int = _int("FREE_FEED_CORPORATE_LOOKBACK_DAYS", 2)
    institutional_risk_weight: float = _float("INSTITUTIONAL_RISK_WEIGHT", 0.12)
    enable_delivery_data: bool = _bool("ENABLE_DELIVERY_DATA", True)
    delivery_cache_seconds: int = _int("DELIVERY_CACHE_SECONDS", 86400)
    delivery_fetch_days: int = _int("DELIVERY_FETCH_DAYS", 20)
    enable_market_breadth: bool = _bool("ENABLE_MARKET_BREADTH", True)
    market_breadth_cache_seconds: int = _int("MARKET_BREADTH_CACHE_SECONDS", 60)
    enable_sector_rotation: bool = _bool("ENABLE_SECTOR_ROTATION", True)
    sector_rotation_cache_seconds: int = _int("SECTOR_ROTATION_CACHE_SECONDS", 300)
    enable_macro_calendar: bool = _bool("ENABLE_MACRO_CALENDAR", True)
    macro_calendar_cache_seconds: int = _int("MACRO_CALENDAR_CACHE_SECONDS", 3600)
    enable_options_intelligence: bool = _bool("ENABLE_OPTIONS_INTELLIGENCE", True)
    options_cache_seconds: int = _int("OPTIONS_CACHE_SECONDS", 300)
    options_symbols_per_cycle: int = _int("OPTIONS_SYMBOLS_PER_CYCLE", 12)
    options_index_symbols: str = os.getenv("OPTIONS_INDEX_SYMBOLS", "NIFTY,BANKNIFTY")
    options_max_pain_buy_suppress_pct: float = _float("OPTIONS_MAX_PAIN_BUY_SUPPRESS_PCT", -8.0)

    execution_mode: str = os.getenv("EXECUTION_MODE", "paper").strip().lower()
    live_trading_enabled: bool = _bool("LIVE_TRADING_ENABLED", False)
    live_trading_confirm: str = os.getenv("LIVE_TRADING_CONFIRM", "")
    indstocks_order_product: str = os.getenv("INDSTOCKS_ORDER_PRODUCT", "CNC")
    indstocks_order_validity: str = os.getenv("INDSTOCKS_ORDER_VALIDITY", "DAY")
    indstocks_order_type: str = os.getenv("INDSTOCKS_ORDER_TYPE", "MARKET")
    indstocks_algo_id: str = os.getenv("INDSTOCKS_ALGO_ID", "99999")
    upstox_order_product: str = os.getenv("UPSTOX_ORDER_PRODUCT", "D")
    upstox_order_validity: str = os.getenv("UPSTOX_ORDER_VALIDITY", "DAY")
    upstox_order_type: str = os.getenv("UPSTOX_ORDER_TYPE", "MARKET")
    brokerage_bps: float = _float("BROKERAGE_BPS", 0.0)
    slippage_bps: float = _float("SLIPPAGE_BPS", 5.0)
    taxes_bps: float = _float("TAXES_BPS", 1.0)
    stt_bps: float = _float("STT_BPS", 10.0)

    llm_provider: str = os.getenv("LLM_PROVIDER", "deepseek").strip().lower()
    llm_decision_mode: str = os.getenv("LLM_DECISION_MODE", "primary").strip().lower()
    deepseek_api_key: str = os.getenv("DEEPSEEK_API_KEY", "")
    deepseek_base_url: str = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
    deepseek_model: str = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-pro")
    groq_api_key: str = os.getenv("GROQ_API_KEY", "")
    groq_base_url: str = os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1").rstrip("/")
    groq_model: str = os.getenv("GROQ_MODEL", "qwen/qwen3-32b")
    user_default_llm_provider: str = os.getenv("USER_DEFAULT_LLM_PROVIDER", "groq").strip().lower()
    user_default_llm_model: str = os.getenv("USER_DEFAULT_LLM_MODEL", "qwen/qwen3-32b")
    llm_temperature: float = _float("LLM_TEMPERATURE", 0.05)
    llm_top_p: float = _float("LLM_TOP_P", 0.7)
    llm_max_tokens: int = _int("LLM_MAX_TOKENS", 4096)
    llm_max_symbols_per_cycle: int = _int("LLM_MAX_SYMBOLS_PER_CYCLE", 3)
    llm_primary_min_confidence: float = _float("LLM_PRIMARY_MIN_CONFIDENCE", 0.62)
    llm_reasoning_effort: str = os.getenv("LLM_REASONING_EFFORT", "high").strip().lower()
    llm_thinking_enabled: bool = _bool("LLM_THINKING_ENABLED", True)
    llm_streaming_enabled: bool = _bool("LLM_STREAMING_ENABLED", False)
    llm_timeout_seconds: int = _int("LLM_TIMEOUT_SECONDS", 120)
    llm_rolling_context_enabled: bool = _bool("LLM_ROLLING_CONTEXT_ENABLED", True)
    llm_rolling_context_threshold_chars: int = _int("LLM_ROLLING_CONTEXT_THRESHOLD_CHARS", 16000)
    llm_rolling_context_chunk_chars: int = _int("LLM_ROLLING_CONTEXT_CHUNK_CHARS", 7000)
    llm_rolling_context_max_chunks: int = _int("LLM_ROLLING_CONTEXT_MAX_CHUNKS", 0)

    enable_db_maintenance: bool = _bool("ENABLE_DB_MAINTENANCE", True)
    db_maintenance_interval_hours: int = _int("DB_MAINTENANCE_INTERVAL_HOURS", 168)
    db_retention_full_audit_keep_latest: int = _int("DB_RETENTION_FULL_AUDIT_KEEP_LATEST", 500)
    db_retention_hold_decision_days: int = _int("DB_RETENTION_HOLD_DECISION_DAYS", 7)
    db_retention_full_audit_days: int = _int("DB_RETENTION_FULL_AUDIT_DAYS", 30)
    db_retention_market_tick_days: int = _int("DB_RETENTION_MARKET_TICK_DAYS", 7)
    db_retention_sentiment_days: int = _int("DB_RETENTION_SENTIMENT_DAYS", 30)
    db_retention_llm_usage_days: int = _int("DB_RETENTION_LLM_USAGE_DAYS", 180)
    db_retention_delivery_days: int = _int("DB_RETENTION_DELIVERY_DAYS", 90)
    db_retention_candle_rows_per_symbol_source: int = _int("DB_RETENTION_CANDLE_ROWS_PER_SYMBOL_SOURCE", 320)
    db_retention_vacuum: bool = _bool("DB_RETENTION_VACUUM", True)


SECRET_FIELDS = {
    "kite_api_key",
    "kite_access_token",
    "indstocks_access_token",
    "upstox_api_key",
    "upstox_api_secret",
    "upstox_access_token",
    "upstox_sandbox_access_token",
    "nubra_session_token",
    "nubra_phone",
    "nubra_mpin",
    "deepseek_api_key",
    "groq_api_key",
    "live_trading_confirm",
    "admin_password",
    "auth_session_secret",
    "openclaw_bridge_token",
    "openclaw_webhook_secret",
}


CONFIG_SCHEMA: list[dict[str, Any]] = [
    {"key": "execution_mode", "label": "Execution Mode", "type": "select", "category": "Runtime", "choices": ["paper", "upstox_sandbox", "upstox_live"]},
    {"key": "initial_cash_inr", "label": "Paper Capital", "type": "number", "category": "Runtime", "min": 1000, "step": 1000},
    {"key": "auto_start_agent", "label": "Auto Start", "type": "boolean", "category": "Agent Cycle"},
    {"key": "agent_interval_seconds", "label": "Cycle Seconds", "type": "number", "category": "Agent Cycle", "min": 5, "step": 1},
    {"key": "cycle_timeout_seconds", "label": "Cycle Timeout Seconds", "type": "number", "category": "Agent Cycle", "min": 30, "step": 15},
    {"key": "skip_market_data_when_closed", "label": "Skip Closed Markets", "type": "boolean", "category": "Agent Cycle"},
    {"key": "post_market_prep_enabled", "label": "Post-Market Prep", "type": "boolean", "category": "Agent Cycle"},
    {"key": "post_market_news_symbols", "label": "Post-Market News Symbols", "type": "number", "category": "Agent Cycle", "min": 0, "step": 1},
    {"key": "enable_db_maintenance", "label": "DB Maintenance", "type": "boolean", "category": "Maintenance"},
    {"key": "db_maintenance_interval_hours", "label": "Maintenance Interval Hours", "type": "number", "category": "Maintenance", "min": 1, "step": 1},
    {"key": "db_retention_full_audit_keep_latest", "label": "Full Audits To Keep", "type": "number", "category": "Maintenance", "min": 0, "step": 100},
    {"key": "db_retention_hold_decision_days", "label": "HOLD Decision Days", "type": "number", "category": "Maintenance", "min": 1, "step": 1},
    {"key": "db_retention_full_audit_days", "label": "Full Audit Days", "type": "number", "category": "Maintenance", "min": 1, "step": 1},
    {"key": "db_retention_market_tick_days", "label": "Market Tick Days", "type": "number", "category": "Maintenance", "min": 1, "step": 1},
    {"key": "db_retention_sentiment_days", "label": "Sentiment Days", "type": "number", "category": "Maintenance", "min": 1, "step": 1},
    {"key": "db_retention_llm_usage_days", "label": "LLM Usage Days", "type": "number", "category": "Maintenance", "min": 1, "step": 1},
    {"key": "db_retention_delivery_days", "label": "Delivery Data Days", "type": "number", "category": "Maintenance", "min": 20, "step": 1},
    {"key": "db_retention_candle_rows_per_symbol_source", "label": "Candles Per Symbol/Source", "type": "number", "category": "Maintenance", "min": 30, "step": 10},
    {"key": "db_retention_vacuum", "label": "Vacuum After Purge", "type": "boolean", "category": "Maintenance"},
    {"key": "admin_password", "label": "Admin Password", "type": "secret", "category": "Access Control"},
    {"key": "admin_username", "label": "Admin Username", "type": "text", "category": "Access Control"},
    {"key": "auth_session_secret", "label": "Session Secret", "type": "secret", "category": "Access Control"},
    {"key": "admin_session_hours", "label": "Session Hours", "type": "number", "category": "Access Control", "min": 1, "step": 1},
    {"key": "credit_tokens_per_credit", "label": "Tokens Per Credit", "type": "number", "category": "User Credits", "min": 1, "step": 1},
    {"key": "credit_platform_margin_pct", "label": "Platform Margin %", "type": "number", "category": "User Credits", "min": 0, "max": 1, "step": 0.01},
    {"key": "openclaw_bridge_enabled", "label": "OpenClaw Bridge", "type": "boolean", "category": "OpenClaw"},
    {"key": "openclaw_bridge_token", "label": "OpenClaw API Token", "type": "secret", "category": "OpenClaw"},
    {"key": "openclaw_default_username", "label": "OpenClaw User", "type": "text", "category": "OpenClaw"},
    {"key": "openclaw_webhook_url", "label": "OpenClaw Webhook URL", "type": "text", "category": "OpenClaw"},
    {"key": "openclaw_webhook_secret", "label": "OpenClaw Webhook Secret", "type": "secret", "category": "OpenClaw"},
    {"key": "openclaw_cli_path", "label": "OpenClaw CLI Path", "type": "text", "category": "OpenClaw"},
    {"key": "openclaw_notify_channel", "label": "Notify Channel", "type": "text", "category": "OpenClaw"},
    {"key": "openclaw_notify_target", "label": "Notify Target", "type": "text", "category": "OpenClaw"},
    {"key": "openclaw_notify_ideas", "label": "Notify Ideas", "type": "boolean", "category": "OpenClaw"},
    {"key": "openclaw_notify_orders", "label": "Notify Orders", "type": "boolean", "category": "OpenClaw"},
    {"key": "market_region", "label": "Market Region", "type": "select", "category": "Market Data", "choices": ["IN", "US", "BOTH"]},
    {"key": "market_data_provider", "label": "Market Data", "type": "select", "category": "Market Data", "choices": ["simulated", "upstox", "upstox_yahoo", "kite", "kite_yahoo", "nubra", "yahoo"]},
    {"key": "universe_source", "label": "Universe Source", "type": "select", "category": "Market Data", "choices": ["csv", "nse_equity"]},
    {"key": "us_universe_csv", "label": "US Universe CSV", "type": "text", "category": "Market Data"},
    {"key": "nse_universe_refresh_on_start", "label": "Refresh NSE Universe", "type": "boolean", "category": "Market Data"},
    {"key": "nse_equity_list_url", "label": "NSE Equity List URL", "type": "text", "category": "Market Data"},
    {"key": "nse_universe_series", "label": "NSE Series", "type": "text", "category": "Market Data"},
    {"key": "universe_symbols_per_cycle", "label": "Symbols/Cycle (0=All)", "type": "number", "category": "Market Data", "min": 0, "step": 50},
    {"key": "dynamic_opportunity_scan_enabled", "label": "Dynamic Opportunity Scan", "type": "boolean", "category": "Market Data"},
    {"key": "dynamic_scan_raw_limit", "label": "Dynamic Raw Symbols/Cycle", "type": "number", "category": "Market Data", "min": 0, "step": 50},
    {"key": "dynamic_scan_candidate_limit", "label": "Dynamic Candidates/Cycle", "type": "number", "category": "Market Data", "min": 1, "step": 10},
    {"key": "dynamic_scan_min_score", "label": "Dynamic Min Opportunity Score", "type": "number", "category": "Market Data", "min": 0, "max": 1, "step": 0.01},
    {"key": "dynamic_scan_require_active_setup", "label": "Require Active Setup", "type": "boolean", "category": "Market Data"},
    {"key": "dynamic_scan_min_price", "label": "Dynamic Min Price", "type": "number", "category": "Market Data", "min": 0, "step": 1},
    {"key": "dynamic_scan_min_turnover_inr", "label": "Dynamic Min Turnover INR", "type": "number", "category": "Market Data", "min": 0, "step": 1000000},
    {"key": "dynamic_scan_breakout_distance_pct", "label": "Dynamic Breakout Distance %", "type": "number", "category": "Market Data", "min": 0.1, "step": 0.1},
    {"key": "dynamic_scan_sentiment_enabled", "label": "Dynamic Sentiment Scan", "type": "boolean", "category": "Market Data"},
    {"key": "dynamic_scan_news_probe_limit", "label": "Dynamic News Probe/Cycle", "type": "number", "category": "Market Data", "min": 0, "step": 1},
    {"key": "dynamic_scan_sentiment_weight", "label": "Dynamic Sentiment Weight", "type": "number", "category": "Market Data", "min": 0, "max": 0.3, "step": 0.01},
    {"key": "upstox_access_token", "label": "Upstox Analytics Token", "type": "secret", "category": "Market Data"},
    {"key": "upstox_api_base_url", "label": "Upstox API URL", "type": "text", "category": "Market Data"},
    {"key": "upstox_candle_interval", "label": "Upstox Candle Interval", "type": "select", "category": "Market Data", "choices": ["1minute", "30minute", "day", "week", "month"]},
    {"key": "upstox_candle_lookback_days", "label": "Upstox Intraday Lookback Days", "type": "number", "category": "Market Data", "min": 1, "max": 30, "step": 1},
    {"key": "enable_upstox_multi_timeframe_candles", "label": "Upstox Daily/Weekly Candles", "type": "boolean", "category": "Market Data"},
    {"key": "upstox_daily_candle_lookback_days", "label": "Upstox Daily Lookback Days", "type": "number", "category": "Market Data", "min": 30, "step": 30},
    {"key": "upstox_weekly_candle_lookback_days", "label": "Upstox Weekly Lookback Days", "type": "number", "category": "Market Data", "min": 90, "step": 30},
    {"key": "upstox_candle_concurrency", "label": "Upstox Candle Concurrency", "type": "number", "category": "Market Data", "min": 1, "step": 1},
    {"key": "upstox_candle_fetch_timeout_seconds", "label": "Upstox Candle Timeout", "type": "number", "category": "Market Data", "min": 5, "step": 5},
    {"key": "yahoo_candle_interval", "label": "Yahoo Candle Interval", "type": "select", "category": "Market Data", "choices": ["5m", "15m", "30m", "60m", "1d", "1wk"]},
    {"key": "yahoo_candle_range", "label": "Yahoo Candle Range", "type": "select", "category": "Market Data", "choices": ["5d", "1mo", "3mo", "6mo", "1y", "2y", "5y"]},
    {"key": "llm_provider", "label": "LLM Provider", "type": "select", "category": "LLM Brain", "choices": ["deepseek", "groq", "offline"]},
    {"key": "llm_decision_mode", "label": "Decision Mode", "type": "select", "category": "LLM Brain", "choices": ["offline", "review", "primary"]},
    {"key": "deepseek_api_key", "label": "DeepSeek API Key", "type": "secret", "category": "LLM Brain"},
    {"key": "deepseek_base_url", "label": "DeepSeek Base URL", "type": "text", "category": "LLM Brain"},
    {"key": "deepseek_model", "label": "DeepSeek Model", "type": "select", "category": "LLM Brain", "choices": ["deepseek-v4-pro", "deepseek-v4-flash"]},
    {"key": "groq_api_key", "label": "Groq API Key", "type": "secret", "category": "LLM Brain"},
    {"key": "groq_base_url", "label": "Groq Base URL", "type": "text", "category": "LLM Brain"},
    {"key": "groq_model", "label": "Groq Model", "type": "text", "category": "LLM Brain"},
    {"key": "user_default_llm_provider", "label": "Default User LLM", "type": "select", "category": "User Credits", "choices": ["groq", "deepseek", "offline"]},
    {"key": "user_default_llm_model", "label": "Default User Model", "type": "text", "category": "User Credits"},
    {"key": "llm_rolling_context_enabled", "label": "Rolling Context", "type": "boolean", "category": "LLM Brain"},
    {"key": "llm_rolling_context_threshold_chars", "label": "Rolling Threshold Chars", "type": "number", "category": "LLM Brain", "min": 2000, "step": 1000},
    {"key": "llm_rolling_context_chunk_chars", "label": "Rolling Chunk Chars", "type": "number", "category": "LLM Brain", "min": 1000, "step": 500},
    {"key": "llm_rolling_context_max_chunks", "label": "Rolling Max Chunks (0=All)", "type": "number", "category": "LLM Brain", "min": 0, "step": 1},
    {"key": "llm_temperature", "label": "LLM Temperature", "type": "number", "category": "LLM Brain", "min": 0, "max": 2, "step": 0.01},
    {"key": "llm_top_p", "label": "LLM Top P", "type": "number", "category": "LLM Brain", "min": 0, "max": 1, "step": 0.01},
    {"key": "llm_max_tokens", "label": "LLM Max Tokens", "type": "number", "category": "LLM Brain", "min": 24, "step": 128},
    {"key": "llm_max_symbols_per_cycle", "label": "LLM Symbols/Cycle", "type": "number", "category": "LLM Brain", "min": 1, "step": 1},
    {"key": "llm_primary_min_confidence", "label": "Min LLM Confidence", "type": "number", "category": "LLM Brain", "min": 0, "max": 1, "step": 0.01},
    {"key": "llm_reasoning_effort", "label": "Reasoning Effort", "type": "select", "category": "LLM Brain", "choices": ["none", "high", "max"]},
    {"key": "llm_thinking_enabled", "label": "DeepSeek Thinking", "type": "boolean", "category": "LLM Brain"},
    {"key": "llm_timeout_seconds", "label": "LLM Timeout Seconds", "type": "number", "category": "LLM Brain", "min": 5, "step": 5},
    {"key": "max_positions", "label": "Max Positions", "type": "number", "category": "Risk", "min": 1, "step": 1},
    {"key": "max_position_pct", "label": "Max Position %", "type": "number", "category": "Risk", "min": 0.01, "max": 0.15, "step": 0.01},
    {"key": "max_order_value_pct", "label": "Max Order %", "type": "number", "category": "Risk", "min": 0.01, "max": 0.15, "step": 0.01},
    {"key": "max_trades_per_cycle", "label": "Max Trades/Cycle", "type": "number", "category": "Risk", "min": 1, "step": 1},
    {"key": "stop_loss_pct", "label": "Stop Loss %", "type": "number", "category": "Risk", "min": 0, "max": 1, "step": 0.005},
    {"key": "take_profit_pct", "label": "Take Profit %", "type": "number", "category": "Risk", "min": 0, "max": 2, "step": 0.005},
    {"key": "daily_loss_limit_pct", "label": "Daily Loss Limit %", "type": "number", "category": "Risk", "min": 0, "max": 1, "step": 0.005},
    {"key": "brokerage_bps", "label": "Brokerage Bps", "type": "number", "category": "Risk", "min": 0, "step": 0.1},
    {"key": "slippage_bps", "label": "Slippage Bps", "type": "number", "category": "Risk", "min": 0, "step": 0.1},
    {"key": "taxes_bps", "label": "Taxes/Fees Bps", "type": "number", "category": "Risk", "min": 0, "step": 0.1},
    {"key": "stt_bps", "label": "STT Bps", "type": "number", "category": "Risk", "min": 0, "step": 0.1},
    {"key": "enable_news_sentiment", "label": "News Sentiment", "type": "boolean", "category": "Sentiment"},
    {"key": "enable_llm_sentiment", "label": "LLM Sentiment", "type": "boolean", "category": "Sentiment"},
    {"key": "news_cache_seconds", "label": "News Cache Seconds", "type": "number", "category": "Sentiment", "min": 60, "step": 60},
    {"key": "news_lookback_days", "label": "News Lookback Days", "type": "number", "category": "Sentiment", "min": 1, "step": 1},
    {"key": "news_symbols_per_cycle", "label": "News Symbols/Cycle", "type": "number", "category": "Sentiment", "min": 0, "step": 1},
    {"key": "enable_global_intelligence", "label": "Global Intelligence", "type": "boolean", "category": "Global Intelligence"},
    {"key": "global_cache_seconds", "label": "Global Cache Seconds", "type": "number", "category": "Global Intelligence", "min": 60, "step": 60},
    {"key": "global_news_lookback_days", "label": "Global News Days", "type": "number", "category": "Global Intelligence", "min": 1, "step": 1},
    {"key": "global_risk_weight", "label": "Global Risk Weight", "type": "number", "category": "Global Intelligence", "min": 0, "max": 0.3, "step": 0.01},
    {"key": "enable_free_institutional_feeds", "label": "Free Institutional Feeds", "type": "boolean", "category": "Institutional Feeds"},
    {"key": "free_feed_cache_seconds", "label": "Free Feed Cache Seconds", "type": "number", "category": "Institutional Feeds", "min": 300, "step": 300},
    {"key": "free_feed_timeout_seconds", "label": "Free Feed Timeout Seconds", "type": "number", "category": "Institutional Feeds", "min": 3, "step": 1},
    {"key": "free_feed_option_chain_symbols", "label": "Free Option Chain Symbols", "type": "text", "category": "Institutional Feeds"},
    {"key": "free_feed_corporate_lookback_days", "label": "Corporate Lookback Days", "type": "number", "category": "Institutional Feeds", "min": 1, "step": 1},
    {"key": "institutional_risk_weight", "label": "Institutional Weight", "type": "number", "category": "Institutional Feeds", "min": 0, "max": 0.3, "step": 0.01},
    {"key": "enable_delivery_data", "label": "NSE Delivery Data", "type": "boolean", "category": "Institutional Feeds"},
    {"key": "delivery_cache_seconds", "label": "Delivery Cache Seconds", "type": "number", "category": "Institutional Feeds", "min": 3600, "step": 3600},
    {"key": "delivery_fetch_days", "label": "Delivery Fetch Days", "type": "number", "category": "Institutional Feeds", "min": 5, "step": 1},
    {"key": "enable_market_breadth", "label": "Market Breadth", "type": "boolean", "category": "Institutional Feeds"},
    {"key": "market_breadth_cache_seconds", "label": "Breadth Cache Seconds", "type": "number", "category": "Institutional Feeds", "min": 30, "step": 30},
    {"key": "enable_sector_rotation", "label": "Sector Rotation", "type": "boolean", "category": "Institutional Feeds"},
    {"key": "sector_rotation_cache_seconds", "label": "Sector Cache Seconds", "type": "number", "category": "Institutional Feeds", "min": 60, "step": 60},
    {"key": "enable_macro_calendar", "label": "Macro Calendar", "type": "boolean", "category": "Global Intelligence"},
    {"key": "macro_calendar_cache_seconds", "label": "Macro Calendar Cache Seconds", "type": "number", "category": "Global Intelligence", "min": 300, "step": 300},
    {"key": "enable_options_intelligence", "label": "Options Intelligence", "type": "boolean", "category": "Institutional Feeds"},
    {"key": "options_cache_seconds", "label": "Options Cache Seconds", "type": "number", "category": "Institutional Feeds", "min": 60, "step": 60},
    {"key": "options_symbols_per_cycle", "label": "Stock Option Symbols/Cycle", "type": "number", "category": "Institutional Feeds", "min": 0, "step": 1},
    {"key": "options_index_symbols", "label": "Index Option Symbols", "type": "text", "category": "Institutional Feeds"},
    {"key": "options_max_pain_buy_suppress_pct", "label": "Max Pain BUY Suppress %", "type": "number", "category": "Institutional Feeds", "min": -50, "max": 0, "step": 0.5},
    {"key": "live_trading_enabled", "label": "Live Trading Enabled", "type": "boolean", "category": "Live Protection"},
    {"key": "live_trading_confirm", "label": "Live Confirm Phrase", "type": "secret", "category": "Live Protection"},
    {"key": "upstox_sandbox_access_token", "label": "Upstox Sandbox Token", "type": "secret", "category": "Live Protection"},
    {"key": "upstox_order_base_url", "label": "Upstox Order URL", "type": "text", "category": "Live Protection"},
    {"key": "upstox_order_product", "label": "Upstox Product", "type": "select", "category": "Live Protection", "choices": ["D", "I"]},
    {"key": "upstox_order_validity", "label": "Upstox Validity", "type": "select", "category": "Live Protection", "choices": ["DAY", "IOC"]},
    {"key": "upstox_order_type", "label": "Upstox Order Type", "type": "select", "category": "Live Protection", "choices": ["MARKET", "LIMIT"]},
]


CONFIG_KEYS = {item["key"] for item in CONFIG_SCHEMA}


def coerce_setting_value(key: str, value: Any, base: Settings) -> Any:
    if key == "market_region":
        region = str(value).strip().upper()
        return region if region in {"IN", "US", "BOTH"} else "IN"
    if key == "market_data_provider":
        provider = str(value).strip().lower()
        if provider == "indstocks":
            provider = "upstox"
        elif provider == "indstocks_yahoo":
            provider = "upstox_yahoo"
        choices = {"simulated", "upstox", "upstox_yahoo", "kite", "kite_yahoo", "nubra", "yahoo"}
        return provider if provider in choices else "upstox"
    if key == "upstox_api_base_url":
        return str(value).strip().rstrip("/") or "https://api.upstox.com/v2"
    if key == "upstox_order_base_url":
        return str(value).strip().rstrip("/") or "https://api-hft.upstox.com/v2"
    if key == "upstox_candle_interval":
        interval = str(value).strip()
        choices = {"1minute", "30minute", "day", "week", "month"}
        return interval if interval in choices else "30minute"
    if key == "indstocks_api_base_url":
        return str(value).strip().rstrip("/") or "https://api.indstocks.com"
    if key == "indstocks_candle_interval":
        interval = str(value).strip()
        choices = {"1minute", "5minute", "15minute", "30minute", "60minute", "1day", "1week", "1month"}
        return interval if interval in choices else "1day"
    if key == "execution_mode":
        mode = str(value).strip().lower()
        return mode if mode in {"paper", "upstox_sandbox", "upstox_live"} else "paper"
    if key == "yahoo_candle_interval":
        interval = str(value).strip()
        return interval if interval in {"5m", "15m", "30m", "60m", "1d", "1wk"} else "1d"
    if key == "yahoo_candle_range":
        candle_range = str(value).strip()
        return candle_range if candle_range in {"5d", "1mo", "3mo", "6mo", "1y", "2y", "5y"} else "1y"
    if key == "llm_provider":
        provider = str(value).strip().lower()
        return provider if provider in {"deepseek", "groq", "offline"} else "deepseek"
    if key == "user_default_llm_provider":
        provider = str(value).strip().lower()
        return provider if provider in {"groq", "deepseek", "offline"} else "groq"
    if key == "llm_decision_mode":
        mode = str(value).strip().lower()
        return mode if mode in {"offline", "review", "primary"} else "primary"
    if key == "deepseek_model":
        model = str(value).strip()
        return model if model in {"deepseek-v4-pro", "deepseek-v4-flash"} else "deepseek-v4-pro"
    if key == "deepseek_base_url":
        return str(value).strip().rstrip("/") or "https://api.deepseek.com"
    if key == "groq_base_url":
        return str(value).strip().rstrip("/") or "https://api.groq.com/openai/v1"
    if key in {"groq_model", "user_default_llm_model"}:
        return str(value).strip() or "qwen/qwen3-32b"
    if key == "llm_reasoning_effort":
        effort = str(value).strip().lower()
        return effort if effort in {"none", "high", "max"} else "high"
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
