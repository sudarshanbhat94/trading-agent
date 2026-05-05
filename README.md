# Indian Equity Dry Trading Agent

Dry-money autonomous trading agent for Indian equities. It watches a stock universe, pulls quote/candle data, builds MCP-style analysis context, asks an LLM to decide or review, applies risk controls, paper-fills BUY/SELL orders, and streams the state to a live dashboard.

This defaults to paper trading. Upstox live order routing exists, but it is disabled unless you explicitly enable it with a confirmation phrase.

## What It Does

- Monitors an enabled Indian equity universe from `data/universe.csv`.
- Supports quote adapters:
  - `simulated`: works immediately for local testing.
  - `yahoo`: public best-effort delayed quote fallback, not exchange-authorized live data.
  - `kite`: Zerodha Kite Connect quote endpoint for authorized live market data.
  - `upstox`: Upstox REST quote and candle APIs using your access token.
- Builds MCP-style tool context with quotes, candles, candlestick facts, exact math indicators, sentiment, position, and risk limits.
- Scores sentiment from a conservative rotating news RSS scan.
- Uses NVIDIA NIM or another OpenAI-compatible LLM as the primary analyst, or as a reviewer.
- Evaluates named strategy presets and tracks their performance in the UI.
- Enforces dry-money risk rules: max positions, max order size, max position size, stop loss, take profit, and daily drawdown limit.
- Can optionally mirror allowed paper orders to Upstox live order placement, disabled by default.
- Stores quotes, decisions, orders, positions, portfolio snapshots, and sentiment events in SQLite.
- Serves a live dashboard at `/`.

## Quick Start

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Open `http://127.0.0.1:8000`.

## Dashboard Control Panel

The dashboard includes a **Settings** panel. Use it to change:

- Demo cash and cycle interval.
- Execution mode: `paper`, `upstox_sandbox`, or `upstox_live`.
- Market-data provider and broker credentials.
- NVIDIA/OpenAI-compatible LLM provider, model, and API key.
- Risk controls: max positions, order size, stop loss, take profit, and daily loss limit.
- Sentiment scan settings.

Saved settings are stored in SQLite and override `.env` defaults at runtime. Secret values are write-only in the UI: the dashboard shows whether a key is saved but does not display the key. Use **Reset Demo Account** after changing demo cash if you want to clear positions/orders and restart the dummy ledger with the new amount.

### Admin Access

Read-only dashboard views are public. Settings, start/stop, run-once, and demo reset require admin login.

Set this before starting the app:

```bash
ADMIN_PASSWORD=choose-a-strong-password
ADMIN_USERNAME=admin
AUTH_SESSION_SECRET=choose-a-long-random-string
```

If `ADMIN_PASSWORD` is empty, the dashboard stays read-only and admin controls remain locked.

## Sentiment Intelligence

The sentiment pipeline now does more than keyword counts:

- Pulls multiple Google News RSS queries per symbol.
- Deduplicates similar headlines.
- Applies source weighting: exchange/reputed financial sources score higher than generic aggregators.
- Applies recency decay so old headlines fade.
- Classifies event type: earnings, guidance, order win, analyst upgrade/downgrade, legal/regulatory, fraud/governance, debt/liquidity, management, corporate action, macro/sector.
- Produces confidence-weighted sentiment scores.
- Optionally asks the configured LLM to refine event labels and score ambiguity.
- Persists the event JSON and headline set for auditability.

For true institutional coverage, plug in licensed feeds for exchange announcements, Reuters/Bloomberg/Dow Jones, filings, transcripts, and broker research through the same adapter pattern.

## Live Indian Market Data

For exact live Indian equity prices, use an exchange-authorized broker/data API. The app includes Upstox and Kite market-data adapters.

### Upstox Market Data

```bash
MARKET_DATA_PROVIDER=upstox
UPSTOX_ACCESS_TOKEN=your_access_token
UPSTOX_SANDBOX_ACCESS_TOKEN=your_sandbox_token
UPSTOX_API_BASE_URL=https://api.upstox.com/v2
UPSTOX_ORDER_BASE_URL=https://api-hft.upstox.com/v2
UPSTOX_CANDLE_INTERVAL=30minute
UPSTOX_CANDLE_LOOKBACK_DAYS=3
```

The universe file includes `upstox_instrument_key` values like `NSE_EQ|INE002A01018`. For all stocks, regenerate `data/universe.csv` from Upstox's instrument master and keep that column accurate.

### Kite Market Data

Set this in `.env`:

```bash
MARKET_DATA_PROVIDER=kite
KITE_API_KEY=your_api_key
KITE_ACCESS_TOKEN=your_daily_access_token
```

Kite access tokens are session-based, so you need a daily login/token refresh workflow before market open. The universe uses `kite_symbol` values like `NSE:INFY`.

The `yahoo` provider is useful only for development. Treat it as delayed/best-effort, not exact live market data.

## LLM Brain

Default mode is deterministic and local:

```bash
LLM_PROVIDER=offline
LLM_DECISION_MODE=offline
```

To make NVIDIA NIM the primary analyst:

```bash
LLM_PROVIDER=nvidia
LLM_DECISION_MODE=primary
NVIDIA_API_KEY=...
NVIDIA_BASE_URL=https://integrate.api.nvidia.com
NVIDIA_MODEL=deepseek-ai/deepseek-r1
LLM_TEMPERATURE=0.05
LLM_TOP_P=0.7
LLM_MAX_TOKENS=900
LLM_MAX_SYMBOLS_PER_CYCLE=8
LLM_PRIMARY_MIN_CONFIDENCE=0.62
LLM_REASONING_EFFORT=none
LLM_THINKING_ENABLED=false
LLM_STREAMING_ENABLED=true
LLM_TIMEOUT_SECONDS=45
```

To enable another OpenAI-compatible endpoint:

```bash
LLM_PROVIDER=openai_compatible
LLM_DECISION_MODE=primary
LLM_BASE_URL=https://api.openai.com
LLM_API_KEY=...
LLM_MODEL=gpt-4.1-mini
```

`LLM_DECISION_MODE=review` keeps the deterministic strategy as the proposer and asks the LLM to review non-HOLD candidates. `LLM_DECISION_MODE=primary` asks the LLM to produce the BUY/SELL/HOLD decision from tool context. In both modes, the paper broker risk layer can still veto the trade. NVIDIA models use streaming by default when `LLM_STREAMING_ENABLED=true`. For NVIDIA DeepSeek V4 and Kimi models, the app sends `chat_template_kwargs.thinking=false` by default. Turn `LLM_THINKING_ENABLED` on only when you can tolerate slower responses; then `LLM_REASONING_EFFORT=high` or `max` can be used for supported DeepSeek V4 models. If a large model is slow to respond, increase `LLM_TIMEOUT_SECONDS` or switch to a faster NVIDIA model.

For NVIDIA Kimi K2.6, set:

```bash
NVIDIA_MODEL=moonshotai/kimi-k2.6
LLM_STREAMING_ENABLED=true
LLM_THINKING_ENABLED=true
```

## Strategy Presets

The app computes named strategy signals before asking the LLM. The LLM sees those outputs and must choose a `strategy` in its decision JSON.

Included presets:

- `minervini_trend_template`: price above major moving averages, moving-average alignment, rising 200 SMA, and proximity to period high.
- `vcp_breakout`: volatility contraction, volume dry-up, and pivot breakout.
- `darvas_box_breakout`: compact range box plus breakout and volume confirmation.
- `ema_pullback_continuation`: trend continuation after a pullback toward the 21 EMA.
- `bollinger_squeeze_breakout`: low-volatility compression followed by upper-band breakout.
- `rsi_mean_reversion`: oversold rebound setup with trend filter.

The dashboard shows strategy-level open positions, exposure, unrealized P&L, and filled orders. For Minervini-style analysis, use enough daily history to make the 150/200 SMA checks meaningful.

## Demo And Live Order Routing

The default demo mode is:

```bash
EXECUTION_MODE=paper
MARKET_DATA_PROVIDER=upstox
UPSTOX_ACCESS_TOKEN=your_access_token
```

That uses live Upstox quote/candle data but executes only in the app's internal dummy-money ledger.

To also hit Upstox sandbox order APIs:

```bash
EXECUTION_MODE=upstox_sandbox
UPSTOX_SANDBOX_ACCESS_TOKEN=your_sandbox_token
```

Live order routing is off unless all of these are set:

```bash
EXECUTION_MODE=upstox_live
LIVE_TRADING_ENABLED=true
LIVE_TRADING_CONFIRM=I_UNDERSTAND_THIS_PLACES_REAL_ORDERS
UPSTOX_ACCESS_TOKEN=your_access_token
UPSTOX_ORDER_PRODUCT=D
UPSTOX_ORDER_VALIDITY=DAY
UPSTOX_ORDER_TYPE=MARKET
```

The app still runs its paper broker and risk checks first. If a paper BUY/SELL is filled and live routing is enabled, it submits a matching Upstox order and logs `LIVE_SUBMITTED` or `LIVE_FAILED` in the dashboard.

For production-grade real trading, add broker position reconciliation, order status polling, cancel/modify controls, manual kill switch, fill-aware P&L, and compliance/audit export before using meaningful capital.

## MCP Notes

The app builds an MCP-style JSON tool context internally for every LLM decision. Upstox also offers an MCP integration for read-only analysis use cases; trading/order placement should use the normal Upstox REST APIs, not MCP.

## Scaling To All Indian Stocks

The bundled `data/universe.csv` starts with a Nifty-style sample list. For all NSE equities, replace or regenerate that file from your broker's instrument master and keep these columns:

```csv
symbol,name,exchange,yahoo_symbol,kite_symbol,upstox_instrument_key,sector,base_price,enabled
```

For a free-tier VM, do not scan news and LLM-review every listed stock every few seconds. A practical setup is:

- Quotes: broad universe, frequent.
- Sentiment: rotate a small batch per cycle.
- LLM: only review high-conviction candidates.
- Orders: strict caps per cycle and per position.

## OCI Free Tier Deployment

On an Oracle Linux or Ubuntu VM:

```bash
sudo dnf install -y python3.12 python3.12-pip git
sudo mkdir -p /opt/trading-agent
sudo chown opc:opc /opt/trading-agent
git clone <your-repo-url> /opt/trading-agent
cd /opt/trading-agent
python3.12 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Edit `/opt/trading-agent/.env`, then install the service:

```bash
sudo cp deploy/trading-agent.service /etc/systemd/system/trading-agent.service
sudo systemctl daemon-reload
sudo systemctl enable --now trading-agent
sudo systemctl status trading-agent
```

Open port `8000` in the OCI security list and the VM firewall, or put Nginx/Caddy in front with HTTPS.

## Safety Notes

- This app is not investment advice.
- Do not connect it to meaningful real order placement without additional compliance, broker approval, audit logging, kill switches, and manual override controls.
- The phrase "complete internet sentiment" is not technically realistic. This implementation uses configurable sources and leaves a clear adapter point for adding paid news, social, filings, or exchange feeds.
- Backtest and paper trade for a long period before trusting any strategy.
