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

**Reprioritised 2026-07-28 on the user's instruction.** The first version of
this file put correctness and test coverage first, and the loop faithfully
followed it for several cycles — CI, engine tests, exit-rule tests. That was my
ordering, not the user's. The brief asked for an AI investment platform, so
**product features now come first**. Correctness work is still here, demoted,
not deleted.

Scope note, kept deliberately: the original brief is a multi-year roadmap for a
team. This backlog is the ordered subset that is real, unblocked and worth
doing. It is not a promise that the brief converges.

---

## P0 — the product that was asked for

- [ ] `BLOCKED (DeepSeek API key)` **Connect the model writer to the
      narrative.** The prose layer and its hallucination guard are built
      (`app/narrative.py`); `narrate(rec, writer=...)` takes any callable and
      verifies its output. What is missing is the writer itself: the box has
      `LLM_PROVIDER=deepseek` and `LLM_DECISION_MODE=primary` but
      **`DEEPSEEK_API_KEY` is empty**, so `LLMBrain.enabled` is False and no
      model runs. Needs a key, plus a sync-safe call path (the brain's methods
      are async and `api_stock` is sync). Until then the deterministic
      narrative serves, which is what production would show today anyway.
- [ ] `BLOCKED (all four remaining agents)` **More analysts.** Five exist
      (technical, catalyst, risk, macro, position). Every remaining agent in
      the brief is blocked or unwise: **fundamental** and **sector** on data
      quality (see below); **broker-integration** has nothing to integrate —
      the box has zero broker tokens and `EXECUTION_MODE=paper`; and
      **prediction** needs a defined, backtestable target first. Do not add an
      agent that guesses.
- [ ] `BLOCKED (sector data)` **Sector exposure.** `universe.sector` is a
      catch-all — "NSE Listed Equity" covers 2,594 Indian names — so a
      breakdown built on it would be a single 100% bar. Needs a real
      symbol→sector source (NSE industry classification) before this is worth
      building. The analytics payload returns `sector_exposure: null` with a
      note rather than a fake chart.
- [ ] `BLOCKED (no volume in latest_quotes)` **Volume-spike alerts.**
      `latest_quotes` stores symbol/price/open/high/low/close and **no
      volume**, so relative volume cannot be evaluated in the alert loop.
      Needs either a volume column on the quote feed or a candle-based
      fallback — a data decision, not a coding one.
- [ ] **Watchlist folders and tags.** The watchlist exists; grouping does not.
- [ ] **Fundamentals ingestion.** Revenue, profit, EPS, PE/PB, ROE/ROCE, debt,
      promoter and institutional holding. `promoter_holding` is in zero files.
- [ ] **User preferences.** Risk tolerance, investment style, notification
      preferences, persisted per user and surfaced in the API.
- [ ] **Admin console.** Users, roles, feature flags, scheduler/job status,
      system health, audit log. Build on the admin endpoints in `main.py`.

## P1 — correctness and safety of the live book

- **Engine behavioural tests** — split; `v2_live` is 1730 lines and its two
  biggest functions are DB-coupled, so this is several cycles.
  - [x] Decision logic: catalyst window, hold clock, volume curve, lane
        configuration invariants. Done, `a389407`.
  - [x] Exit **decision rules**: stop, trailing stop, breakeven arming, target,
        BTST next-open, intraday square-off, time exit, and the precedence
        between them. Done, see Done log, via an `evaluate_exit()` extraction
        proven identical by differential test.
  - [ ] **Exit side effects against a temp SQLite book.** The decision is now
        covered; the write path is not. Assert that an exit inserts one
        `v2_trades` row with the right pnl/return_pct/reason, deletes the
        position, updates `peak` when holding, and that the equity snapshot is
        throttled to 60s. Build a throwaway DB fixture — never touch the live
        one.
  - [ ] **Entry gating per lane.** That each lane's `*_pass` refuses to open a
        position when its own gate fails (regime, catalyst freshness, rvol,
        near-high, earnings block, risk halt), and that `DISABLED_LANES` is
        honoured in the candidate loop rather than only as a constant.
- [ ] **Deploy-safety check script.** `scripts/preflight.py` — verify the
      declared deps import, all three DBs open, the daily candle source is
      current, and services are healthy. Run before any deploy.
- [ ] **Volume Profile + trendline detection** in `app/indicators.py` — the
      last two gaps in the technical-engine section. Pure Python, local, with
      hand-computed test values like the rest of that module.

## P2 — scale and operations

- [ ] **Event-driven orchestrator.** Replace the polling loop: watch for
      changed symbols, queue work, process only what moved, cache
      aggressively, retry with backoff. Large; plan it in one cycle and
      implement across several.
- [ ] **Database hardening.** Soft delete, audit history, schema versioning
      and migrations. `trade_audit_events` exists as a starting point.
- [ ] **docker-compose + monitoring.** A Dockerfile exists; compose,
      metrics and structured logging do not.
- [ ] **Security sweep.** CSRF on state-changing endpoints, XSS review of the
      SPA templates (an `esc()` helper now exists and covers the recommendation
      card and news headlines — the rest of the SPA is still unescaped), prompt-injection hardening on LLM inputs, and a
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

- `9c061ee` Pattern-alert filtering — 32% -> 13% of names on a real session
- `efb0776` Pattern alerts with a bar-freshness rule
- `ee9f03e` SMA cross alerts (cross_up / cross_down) with a cached candle loader
- `eb05ae1` Catalyst alerts — fire on the next material NSE filing
- `4a8035f` Macro analyst — expiry, policy weeks and earnings proximity
- `714ed26` Analyst panel card — dissent highlighted, abstainers shown as
  abstained rather than neutral
- `5341675` Multi-agent analysts + CIO reconciliation with explicit dissent
- `fe634d8` Alerts evaluated server-side — they previously fired ONLY while
  a browser had the dashboard open (2 live alerts, 0 ever triggered)
- `c1fe36e` Allocation & risk card in the portfolio tab
- `4de242c` Portfolio analytics: allocation, concentration, drawdown,
  per-lane realised-P&L curves (`/v2/api/portfolio`)
- `b9ecc7d` Recommendation card in the SPA + HTML escaping (first XSS fix)
- `7154717` Narrative layer + hallucination guard (evidence-constrained,
  deterministic fallback)
- `5ede852` Structured evidence-grounded recommendations (7-level call,
  confidence, bull/bear, risks, catalysts, levels, targets, horizon)
- `8f57348` CI on every push — matrix 3.12 (prod) + 3.14 (dev), deps from
  requirements.txt only. Verified by simulating CI locally: clean venv +
  shallow clone, 600 tests pass, and the secret-hygiene guard runs rather
  than silently skipping.
- `74d8200` Exit rules extracted + tested (differential-proven identical)
- `a389407` Engine decision-logic tests + hold-clock extraction
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
