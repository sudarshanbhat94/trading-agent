# OpenStocks — build backlog

Ordered work queue for the autonomous improvement loop. **Read this after
HANDOFF.md.** Each cycle takes the highest item that is not `BLOCKED`, does it
properly, ticks it off here, and commits.

Rules for the loop:

- Do **one** item per cycle. Do not batch.
- Skip anything marked `BLOCKED` — say in your report what would unblock it.
- A live-impacting bug outranks everything here. If you find one, fix that
  instead and note it.
- Never mark an item done without tests that would fail if the feature broke.
- Move finished items to the Done log at the bottom, with the commit hash.
- If an item turns out to be wrong or already built, delete it and say so.
  (This has happened twice: "CarTrade never ingested" and the numpy skip
  theory were both false premises.)

Scope note, kept deliberately: the original brief is a multi-year roadmap for a
team. This backlog is the ordered subset that is real, unblocked and worth
doing. It is not a promise that the brief converges.

---

## P0 — correctness and safety of the live book

- [ ] **Engine behavioural tests.** `v2_live` has no test coverage of lane
      logic — only that it imports. Cover: entry gating per lane, the
      intraday 15:12 square-off, BTST next-open exit, stop/target evaluation,
      and `DISABLED_LANES` actually blocking `gap_momentum`. Use a temp SQLite
      book; do not touch the live DB.
- [ ] **Deploy-safety check script.** `scripts/preflight.py` — verify the
      declared deps import, all three DBs open, the daily candle source is
      current, and services are healthy. Run before any deploy.
- [ ] **Volume Profile + trendline detection** in `app/indicators.py` — the
      last two gaps in the technical-engine section. Pure Python, local, with
      hand-computed test values like the rest of that module.

## P1 — the platform the product needs

- [ ] **Alert engine.** Persist user alert rules (price, indicator cross,
      volume spike, pattern, catalyst) and evaluate them on the engine's
      existing cycle. Deliver through the channels that already exist
      (Telegram, WhatsApp) — do not add new providers here.
- [ ] **Portfolio analytics.** Allocation, sector exposure, concentration,
      drawdown, and per-lane equity curves. Extends `strategy_stats()` in
      `v2_web.py`, which already does per-lane returns.
- [ ] **Watchlist folders and tags.** The watchlist exists; grouping does not.
- [ ] **User preferences.** Risk tolerance, investment style, notification
      preferences, persisted per user and surfaced in the API.
- [ ] **Fundamentals ingestion.** Revenue, profit, EPS, PE/PB, ROE/ROCE, debt,
      promoter and institutional holding. Source from NSE filings already
      being polled where possible. `promoter_holding` currently appears in
      zero files.
- [ ] **Structured AI recommendations.** The stock endpoint returns a verdict,
      entry, stop and target. Add bull case, bear case, risks, catalysts, time
      horizon and evidence links — grounded in stored data only, never
      free-generated. Reuse the existing DeepSeek `llm_brain`.

## P2 — scale and operations

- [ ] **Event-driven orchestrator.** Replace the polling loop: watch for
      changed symbols, queue work, process only what moved, cache
      aggressively, retry with backoff. Large; plan it in one cycle and
      implement across several.
- [ ] **Admin console.** Users, roles, feature flags, job/scheduler status,
      system health, audit log. Build on the existing admin endpoints in
      `main.py` rather than starting fresh.
- [ ] **Database hardening.** Soft delete, audit history, schema versioning
      and migrations. `trade_audit_events` exists as a starting point.
- [ ] **docker-compose + monitoring.** A Dockerfile exists; compose,
      metrics and structured logging do not.
- [ ] **Security sweep.** CSRF on state-changing endpoints, XSS review of the
      SPA templates, prompt-injection hardening on LLM inputs, and a
      systematic look at SQL construction. Session auth, rate limiting and
      secret hygiene are already done.

## BLOCKED — needs something only the user can provide

- [ ] `BLOCKED (email provider)` **Forgot password + email verification.**
      No SMTP/SES/SendGrid anywhere in the repo. Needs a provider and
      credentials. Unblocks three roadmap items at once — highest-value
      unblock available.
- [ ] `BLOCKED (OAuth credentials)` **Google OAuth, Apple OAuth.** Needs a
      client ID and secret; the flow cannot be tested without them.
- [ ] `BLOCKED (provider decision)` **SMS / push / Discord / Slack.** Each
      needs an account and credentials. Telegram and WhatsApp already work.
- [ ] `BLOCKED (data source)` **Full BSE coverage.** A market-data question,
      not a coding one. Decide the source first.
- [ ] `BLOCKED (user decision)` **BTST / volume_surge overlap.** BTST skips
      names volume_surge traded that day, excluding good overnight
      candidates. Allowing the double exposure is a risk call.
- [ ] `BLOCKED (infrastructure)` **Vector DB + semantic news search.** Needs
      an embedding provider and a vector store decision.

---

## Done

- `9a10138` CI on every push — matrix 3.12 (prod) + 3.14 (dev), deps from
  requirements.txt only. Verified by simulating CI locally: clean venv +
  shallow clone, 600 tests pass, and the secret-hygiene guard runs rather
  than silently skipping.
- `e66caa4` Stats tab crash fixed; per-lane performance breakdown
- `94725af` Catalyst ingest backfill window + IST timezone correctness
- `d3b0c14` Candle-ingest deadlock — engine was scoring on stale closes
- `54edc0f` Technicals wired into the stock page with as-of/stale flag
- `f432a15` VWAP, SuperTrend, Ichimoku, pivots, Fibonacci, candlestick patterns
- `59187d1` Overnight per-position size cap
- `68a0dec` Opt-in, privilege-safe self-registration
- `73379d9` Session auth hardening — forgeable cookie, Secure flag, throttle
- `e5b2adb` Declared numpy/pandas/joblib — clean deploy had no engine
- `dda6e88` Redacted production host from the public repo; secret-hygiene tests
