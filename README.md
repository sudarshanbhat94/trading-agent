# OpenStocks

Autonomous dry-money trading platform for Indian equities. It watches a stock universe, pulls quote/candle data, builds MCP-style analysis context, asks an LLM to decide or review, applies risk controls, paper-fills BUY/SELL orders, and streams the state to a live dashboard.

This defaults to paper trading. Upstox live order routing exists, but it is disabled unless you explicitly enable it with a confirmation phrase.

## What It Does

- Monitors an enabled Indian equity universe from `data/universe.csv`.
- Supports quote adapters:
  - `simulated`: works immediately for local testing.
  - `yahoo`: public best-effort delayed quotes plus candles, not exchange-authorized live data.
  - `kite`: Zerodha Kite Connect quote endpoint for authorized live market data.
  - `upstox`: Upstox REST quote and candle APIs using your access token.
- Builds MCP-style tool context with quotes, candles, candlestick facts, exact math indicators, sentiment, position, and risk limits.
- Scores sentiment from a conservative rotating news RSS scan.
- Adds global market intelligence from major indices, crude, gold, USD/INR, and global news before each stock decision.
- Uses an admin-assigned LLM per user: DeepSeek or Groq Qwen can act as the primary analyst or reviewer.
- Evaluates named strategy presets and tracks their performance in the UI.
- Enforces dry-money risk rules: max positions, max order size, max position size, stop loss, take profit, and daily drawdown limit.
- Can optionally mirror allowed paper orders to Upstox live order placement, disabled by default.
- Stores quotes, decisions, orders, positions, portfolio snapshots, and sentiment events in SQLite.
- Serves a live dashboard at `/`.
- Records a structured audit trail for every decision and order so you can inspect exactly why the agent chose BUY, SELL, or HOLD.
- Ranks the top 5 current BUY/WATCH candidates in a dedicated Suggestions tab with entry, stop, target, institutional bias, and full audit details.
- Shows an exit plan for executed BUY orders and open positions, including hard stop, targets, invalidation, review cadence, and monitoring checklist.

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
- DeepSeek/Groq provider, model, and API keys.
- Risk controls: max positions, order size, stop loss, take profit, and daily loss limit.
- Sentiment scan settings.

Saved settings are stored in SQLite and override `.env` defaults at runtime. Secret values are write-only in the UI: the dashboard shows whether a key is saved but does not display the key. Use **Reset Demo Account** after changing demo cash if you want to clear positions/orders and restart the dummy ledger with the new amount.

### Decision Audit Trail

Every row in **Decisions** and **Orders** is clickable. The drawer shows:

- The final action and plain-English reason.
- The score formula and weighted contributions from technical math, candlesticks, strategy preset, and sentiment.
- The global/institutional score components that affected BUY/SELL/HOLD.
- The planned exit logic: entry zone, hard stop, target ladder, invalidation, and monitoring checklist when a trade plan exists.
- LLM evidence, checklist, confidence gate, risk checks, and invalidators when the LLM is enabled.
- Market context used: quote, position, technical snapshot, candlestick patterns, best strategy, and recent candle tail.
- Global context used: market regime, global risk score, major market moves, and global headlines.
- Broker execution sizing and veto/fill gates for BUY/SELL orders.

This is evidence and audit data, not hidden chain-of-thought. The app asks the model for concise evidence lists and stores the exact structured output used by the agent.

### Admin Access

OpenStocks now starts with a dedicated login screen. The first admin user is migrated from `ADMIN_USERNAME` and `ADMIN_PASSWORD`, then admins can create additional users from **Users** in the dashboard.

Set this before starting the app:

```bash
ADMIN_PASSWORD=choose-a-strong-password
ADMIN_USERNAME=admin
AUTH_SESSION_SECRET=choose-a-long-random-string
```

If `ADMIN_PASSWORD` is empty and no database admin user exists, the dashboard remains locked until an admin password is configured. Admin users can manage settings, broker connections, agent controls, logs, and user creation. Standard users can sign in to view the trading desk and run symbol analysis, while admin-only controls stay locked.

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

## Global Market Intelligence

OpenStocks now adds a macro backdrop to every stock decision. Each cycle checks:

- US, Europe, Asia, and Indian index moves.
- Crude oil, gold, and USD/INR pressure.
- Global market headlines around rates, inflation, crude, rupee, Asia, and geopolitical risk.

The resulting `global_market_context.risk_score` is included in the decision score and sent to the LLM. The dashboard shows it under **Global Risk**, and every decision drawer includes the exact macro inputs used.

Tune the macro influence:

```bash
ENABLE_GLOBAL_INTELLIGENCE=true
GLOBAL_CACHE_SECONDS=900
GLOBAL_NEWS_LOOKBACK_DAYS=2
GLOBAL_RISK_WEIGHT=0.10
```

`GLOBAL_RISK_WEIGHT` is capped at `0.30`; keep it modest so macro conditions influence stock selection without drowning out price action.

## Free Institutional Feeds

OpenStocks can now enrich every decision with free/public institutional context. These feeds are best-effort and often EOD, so the audit labels them separately from live broker/vendor data.

```bash
ENABLE_FREE_INSTITUTIONAL_FEEDS=true
FREE_FEED_CACHE_SECONDS=1800
FREE_FEED_TIMEOUT_SECONDS=10
FREE_FEED_OPTION_CHAIN_SYMBOLS=NIFTY,BANKNIFTY
FREE_FEED_CORPORATE_LOOKBACK_DAYS=2
INSTITUTIONAL_RISK_WEIGHT=0.12
```

Currently included:

- NSE FII/DII market activity.
- NSE India VIX / index context through public index data.
- NSE option-chain PCR/OI for configured index symbols when the public endpoint responds.
- NSE ASM/GSM surveillance lists.
- NSE corporate announcements for recent official filings.
- Best-effort bulk-deal adapter.

These feeds are not just displayed. They are used in the decision engine through:

- The weighted `free_institutional_context` score component.
- The full-spectrum confluence score.
- Hard no-new-long gates for ASM/GSM/F&O-ban flags when those feeds are available.
- The Suggestions tab and decision drawer, which show the institutional bias and symbol-level flags used.

Still intentionally marked as gaps until a stable adapter/feed is connected: F&O ban, delivery percentage bhavcopy, stock-level FII/DII flow, GIFT Nifty, FedWatch/yield curve detail, social sentiment, analyst consensus, promoter pledge, and tick-level volume profile.

## Live Indian Market Data

For exact live Indian equity prices, use an exchange-authorized broker/data API. The app includes Upstox, Kite, and Nubra market-data adapters.

### Upstox Market Data

```bash
MARKET_DATA_PROVIDER=upstox
UPSTOX_API_KEY=your_api_key
UPSTOX_API_SECRET=your_api_secret
UPSTOX_REDIRECT_URI=http://127.0.0.1:8000/upstox/callback
UPSTOX_ACCESS_TOKEN=your_access_token
UPSTOX_SANDBOX_ACCESS_TOKEN=your_sandbox_token
UPSTOX_API_BASE_URL=https://api.upstox.com/v2
UPSTOX_ORDER_BASE_URL=https://api-hft.upstox.com/v2
UPSTOX_CANDLE_INTERVAL=30minute
UPSTOX_CANDLE_LOOKBACK_DAYS=3
YAHOO_CANDLE_INTERVAL=15m
YAHOO_CANDLE_RANGE=5d
```

If you only have the Upstox API key/secret, open **Settings → Upstox Connect** in the dashboard. Save the API key, API secret, and redirect URI, click **Open Login**, complete the Upstox login, then paste the returned `code` or full redirect URL into **Connect Upstox**. OpenStocks exchanges it for an access token, saves it, switches `MARKET_DATA_PROVIDER` to `upstox`, and rebuilds the running provider.

The universe file includes `upstox_instrument_key` values like `NSE_EQ|INE002A01018`. For all stocks, regenerate `data/universe.csv` from Upstox's instrument master and keep that column accurate.

### Nubra Market Data

Nubra works well for testing market watch because its REST API exposes current price and historical time-series endpoints. The dashboard Settings page has a **Nubra Connect** panel that sends the phone OTP, verifies OTP + MPIN, saves the returned session token, switches `MARKET_DATA_PROVIDER` to `nubra`, and rebuilds the running provider automatically. You can still seed these values from env:

```bash
MARKET_DATA_PROVIDER=nubra
NUBRA_API_BASE_URL=https://uatapi.nubra.io
NUBRA_PHONE=your_phone
NUBRA_MPIN=your_mpin
NUBRA_SESSION_TOKEN=your_session_token
NUBRA_DEVICE_ID=your_device_id
NUBRA_PRICE_SCALE=100
NUBRA_CANDLE_INTERVAL=15m
NUBRA_CANDLE_LOOKBACK_DAYS=5
NUBRA_CANDLE_SYMBOLS_PER_CYCLE=100
NUBRA_OPTION_CHAIN_ENDPOINT=
NUBRA_MARKET_DEPTH_ENDPOINT=
NUBRA_DELIVERY_ENDPOINT=
NUBRA_OI_ENDPOINT=
```

To quickly test market watch before starting OpenStocks:

```bash
export NUBRA_SESSION_TOKEN=...
export NUBRA_DEVICE_ID=...
python scripts/test_nubra_market_watch.py RELIANCE TCS INFY
```

For production market data, change `NUBRA_API_BASE_URL` from `https://uatapi.nubra.io` to your approved Nubra production base URL. Keep `NUBRA_CANDLE_SYMBOLS_PER_CYCLE` conservative for large universes because historical candles are heavier than LTP quotes.

The `NUBRA_*_ENDPOINT` fields are placeholders for your account-specific Nubra option-chain, market-depth, delivery, and OI APIs. Once you get those endpoint paths from Nubra, add them in Settings and wire the adapter without changing the decision engine.

### Kite Market Data

Set this in `.env`:

```bash
MARKET_DATA_PROVIDER=kite
KITE_API_KEY=your_api_key
KITE_ACCESS_TOKEN=your_daily_access_token
```

Kite access tokens are session-based, so you need a daily login/token refresh workflow before market open. The universe uses `kite_symbol` values like `NSE:INFY`.

The `yahoo` provider is useful for development and paper testing. It now pulls delayed quotes and recent candles for technical analysis, but treat it as best-effort and not exact live market data.

## LLM Brain

OpenStocks supports admin-assigned LLM lanes per user. Users spend the same token-based credits regardless of the assigned model, and normal users do not see the underlying provider/model. Admins can assign Groq Qwen, DeepSeek Pro, DeepSeek Flash, or offline mode from the Users panel.

```bash
LLM_PROVIDER=deepseek
LLM_DECISION_MODE=primary
DEEPSEEK_API_KEY=...
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-v4-pro
LLM_ROLLING_CONTEXT_ENABLED=true
LLM_ROLLING_CONTEXT_THRESHOLD_CHARS=16000
LLM_ROLLING_CONTEXT_CHUNK_CHARS=7000
LLM_ROLLING_CONTEXT_MAX_CHUNKS=0
LLM_TEMPERATURE=0.05
LLM_TOP_P=0.7
LLM_MAX_TOKENS=4096
LLM_MAX_SYMBOLS_PER_CYCLE=1
LLM_PRIMARY_MIN_CONFIDENCE=0.62
LLM_REASONING_EFFORT=high
LLM_THINKING_ENABLED=true
LLM_STREAMING_ENABLED=false
LLM_TIMEOUT_SECONDS=120

GROQ_API_KEY=...
GROQ_BASE_URL=https://api.groq.com/openai/v1
GROQ_MODEL=qwen/qwen3-32b
USER_DEFAULT_LLM_PROVIDER=groq
USER_DEFAULT_LLM_MODEL=qwen/qwen3-32b
```

To temporarily turn the brain off, set:

```bash
LLM_PROVIDER=offline
LLM_DECISION_MODE=offline
```

`LLM_DECISION_MODE=review` keeps the deterministic strategy as the proposer and asks the assigned LLM to review non-HOLD candidates. `LLM_DECISION_MODE=primary` asks the assigned LLM to produce the BUY/SELL/HOLD decision from tool context. In both modes, the paper broker risk layer can still veto the trade. Admin audit records the selected provider/model, API attempts, and whether rolling context was used; standard user audit hides model routing.

Large context is handled with rolling analysis instead of blunt trimming. When the rich context exceeds `LLM_ROLLING_CONTEXT_THRESHOLD_CHARS`, OpenStocks summarizes each chunk with DeepSeek, then sends a compact core packet plus the chunk evidence summaries for the final decision. `LLM_ROLLING_CONTEXT_MAX_CHUNKS=0` means cover all chunks; set a positive number only when you intentionally want to cap cost/latency on a small VM.

DeepSeek calls are non-streamed JSON-mode calls. When `LLM_THINKING_ENABLED=true`, OpenStocks sends `thinking={"type":"enabled"}` with `reasoning_effort=high`, matching the direct DeepSeek API style.

## Strategy Presets

The app computes named strategy signals before asking the LLM. The LLM sees those outputs and must choose a `strategy` in its decision JSON.

Included presets:

- `minervini_trend_template`: price above major moving averages, moving-average alignment, rising 200 SMA, and proximity to period high.
- `vcp_breakout`: volatility contraction, volume dry-up, and pivot breakout.
- `darvas_box_breakout`: compact range box plus breakout and volume confirmation.
- `ema_pullback_continuation`: trend continuation after a pullback toward the 21 EMA.
- `bollinger_squeeze_breakout`: low-volatility compression followed by upper-band breakout.
- `rsi_mean_reversion`: oversold rebound setup with trend filter.
- `donchian_momentum_breakout`: channel breakout with trend and volume confirmation.
- `volume_price_accumulation`: accumulation pressure, demand candle, and EMA alignment.
- `failed_breakdown_reversal`: false breakdown reclaim with reversal volume.

The dashboard shows strategy-level open positions, exposure, unrealized P&L, and filled orders. For Minervini-style analysis, use enough daily history to make the 150/200 SMA checks meaningful.

## Full-Spectrum v2 Analysis

Every decision now includes an `openstocks-full-spectrum-v2` audit block inspired by the institutional prompt. The app computes and stores:

- Primary universe filters and data gaps.
- Multi-timeframe trend context from available candles.
- Key levels, gap zones, VWAP proxy, Fibonacci levels, ATR, ADX, MACD, Bollinger, OBV, and CMF.
- Expanded candlestick recognition and classical chart-pattern proxies.
- SMC/Wyckoff approximations: liquidity sweep, BOS/range state, order-block proxy, FVG, and premium/discount zone.
- Confluence score out of 26 with tiers: `NO_SIGNAL`, `WATCHLIST`, `TRADE_SIGNAL`, `HIGH_CONVICTION`, and `MAXIMUM_CONVICTION`.
- Signal plan, trade plan, entry zone, hard stop, targets, invalidation, position-sizing note, and monitoring checklist.
- Code-level action gates: BUY requires full-spectrum confluence `>= 14/26` plus the normal score and risk gates; LLM-primary mode cannot bypass those gates.

Unavailable institutional inputs are not guessed. The audit explicitly records data gaps such as stock-level FII/DII flow, delivery percentage, earnings calendar, and macro event calendar until those feeds are connected. Free/EOD feeds are labelled as `free_public_eod_best_effort` in the audit.

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
symbol,name,exchange,yahoo_symbol,kite_symbol,upstox_instrument_key,nubra_symbol,nubra_ref_id,sector,base_price,enabled
```

To generate an Upstox-backed universe:

```bash
python scripts/update_universe_from_upstox.py --exchange NSE --output data/universe.csv
```

For NSE plus BSE:

```bash
python scripts/update_universe_from_upstox.py --exchange both --output data/universe.csv
```

After regenerating the file, restart the app so it reseeds SQLite. For a very large universe on OCI Free Tier, increase `AGENT_INTERVAL_SECONDS`, keep `NEWS_SYMBOLS_PER_CYCLE` conservative, and keep `LLM_MAX_SYMBOLS_PER_CYCLE` to a realistic shortlist. The agent still scans every enabled symbol deterministically, ranks the full universe, and sends only the strongest candidates to the LLM unless you raise the limit.

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
