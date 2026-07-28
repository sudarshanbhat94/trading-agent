# OpenStocks — Handoff

_Last updated: 2026-07-27_

Working state of the project, written so a new session (or a new person) can pick it up cold.

> **Server access, host addresses, SSH key and absolute deploy paths are deliberately not in this file** — this repository is public. They live in the operator's local notes (`~/.claude/projects/-Users-pavithramayya-Documents-Sudarshan/memory/openstocks-infra-access.md`).

## What it is

Autonomous **paper-trading** platform for Indian NSE equities (US support removed). A deterministic multi-factor engine plus a FastAPI web app. All positions are paper money — the system never places a real broker order.

## Shape of the deployment

- One VM behind nginx, serving a login-gated web app.
- Three systemd units: the **web app + engine** (the engine runs as a background thread *inside* the web service), the **quote feed**, and the **NSE announcements/catalyst feed**.
- Three SQLite databases, all in WAL mode: the paper book, candles/quotes/sentiment, and NSE filings.
- Deploy is: copy the file up, `py_compile` it, restart the web service, then confirm HTTP 200 and a clean journal.

## Engine lanes

| Lane | Status | What it does |
|---|---|---|
| `swing_meanrev` | active | Daily mean-reversion, 2×ATR stop, 8-day hold. Net positive (+₹795 live, PF 1.25). |
| `volume_surge` | active | Intraday catalyst + volume day-mover; squares off same day at 15:12. |
| `intraday_news` | active (capped) | Small news-momentum sleeve. |
| `mom_breakout` | active | 52-week-high breakout, strong-trend only. |
| `btst` | active — **unproven live trial** | Buy near the close on strong-closing catalyst names, sell at the next open. |
| `gap_momentum` | **quarantined** | Proven net loser (−0.51%/trade, PF 0.81 over 44k trades). Listed in `DISABLED_LANES` in `app/v2_live.py`. |

### On `btst`

Shipped 2026-07-27. Backtested at 64% win / +0.20% per trade / PF ~1.6, but on only ~149 trades over 2 months. Treat it as an experiment under observation: give it weeks before trusting it, widen it if it holds up, kill it if it doesn't.

The edge is the **overnight gap only** — holding into the next day gives it back, so the exit must be at the open.

## Settled questions — please don't re-litigate

Each of these was tested and closed:

- ✅ **BTST on catalyst names** — works. Shipped.
- ❌ **Buying the next open and holding to close** — loses, −0.21% per trade.
- ❌ **Reading results quality to predict the move** — no signal at all (correlation +0.016). Karur posted +45% profit and Himadri a record Q1; both fell the next day.
- ❌ **Tightening the swing stop** — actively *hurts* the edge (+0.63% → +0.14% per trade at a −4% cap, PF 1.24 → 1.06) and does not fix overnight gaps. The wide 2×ATR stop is what earns the edge.

**The key structural insight:** a poll-based paper stop *cannot* defend an overnight gap. It fills at the gapped open, exactly as a real broker stop would. The stop shown in the UI is honest, but it is not a guarantee on anything held overnight. The residual tail risk there is a **sizing** problem, not a stop-level one.

## Open work

1. **BTST first live basket: LOST, 0 for 3 (2026-07-28).** All three names gapped down through the −2% stop at the open and exited on `stop`, not on the `btst` next-open rule — exactly the precedence the exit tests pin, and exactly the documented limitation that a poll-based stop fills at the gapped price rather than the stop level.
   | | entry | exit | | |
   |---|---|---|---|---|
   | KFINTECH | 950.40 | 931.39 | −2.00% | −₹190.08 |
   | RKFORGE | 624.00 | 611.52 | −2.00% | −₹199.68 |
   | RRKABEL | 2571.40 | 2519.97 | −2.00% | −₹154.28 |

   BTST all-time: **−₹544.04 over 3 trades.** One basket proves nothing either way against a 149-trade backtest, but the failure mode is the one the backtest could not price: a correlated overnight gap hitting every name at once. Worth watching whether losses cluster on the same morning again.
   **Same day, `volume_surge` gave back its gains**: AEROFLEX −528.05 and CGCL −1648.00 (both stops) against COFORGE +908.80 (target). Lane all-time is now **−₹302.40 over 8 trades**, having been +₹964.85. Book all-time **−₹846.44**.
   **The engine then stopped buying, by design.** `_risk_halt`'s StoplossGuard pauses new entries after `RISK["stopguard_n"]` = 4 stop/trail exits in a day; there were 5. It resets next session and never touches open positions. If the book looks frozen mid-session, check this first.
1. **BTST needs ongoing evaluation** — for each basket, compare entry (prior close) against the next open, net of costs. As of 2026-07-27 the first live basket (KFINTECH, RKFORGE, RRKABEL) was held overnight for a 2026-07-28 open sell.
2. ~~**Catalyst ingestion gap**~~ — **investigated 2026-07-27, not a bug.** The premise was wrong. `opentrade-nse-ann.service` started **2026-07-23 17:51 UTC** with 0 restarts, and the table's history begins exactly there — CarTrade's results filing simply predates the feed. The one CarTrade row present is an **ESOP allotment** correctly classified as `noise`. Feed is healthy: ~660–750 filings on weekdays, ~120–160 material.
   Two real weaknesses were found and fixed while looking: (a) `fetch()` queried **only today**, so any downtime lost that day's filings permanently — there was no backfill, and the 7-day prune hides the hole a week later. It now re-queries a 3-day window (`NSE_ANN_BACKFILL_DAYS`), which is idempotent via `INSERT OR IGNORE`. (b) `_epoch()` parsed a naive datetime and compensated with a hardcoded −5.5h, so it was only correct while the host runs UTC; switching the box to IST would have shifted every catalyst by 5.5h and corrupted the freshness window the gates use. Now parsed as IST explicitly — verified byte-identical against 200 stored rows.
   Also: `opentrade-nse-ann.service` was running on the box with **no unit file in `deploy/`**; it is now committed.
3. **BTST overlap decision (open)** — the lane currently skips symbols `volume_surge` already traded that day, which excludes some of the best overnight candidates. Decide whether to allow the double exposure.
4. ~~**Overnight sizing cap**~~ — **done 2026-07-27.** `OVERNIGHT_MAX_POS_FRAC` (0.30 of equity) in `v2_live.py`, applied via `cap_overnight_shares()` to every lane that holds through the close; intraday lanes are exempt and already size off a fixed slot. It is a guardrail, not a re-tuning: normal sizing spans 9.2–26.7% of equity, the largest position in the live book's history was 26.6%, and none of the closed trades would have been resized. It only clips the case where `DYN_ALLOC` hands nearly all free cash to the last open slot. **Do not lower it below ~0.27 without a backtest** — that would start re-sizing normal trades.
5. **News feed** — never built. **Performance analytics: partly done 2026-07-27.** The Stats tab was **broken**: `/v2/api/stats` returns one row per market with no `strategy` field, but both `loadStats()` templates called `s.strategy.indexOf('gap')` — a TypeError that aborted the whole `.map()`, so the tab rendered nothing. Fixed, and `/v2/api/stats` now carries `by_strategy`: per-lane trades, win%, PF, avg/best/worst return, P&L and average holding days, each under its real lane name with an `overnight` flag. The old view collapsed every non-gap lane to "swing", which made btst/volume_surge/intraday_news/mom_breakout impossible to tell apart. This is what open item 1 (evaluating the BTST trial) needs. Still missing: an equity-curve-per-lane view and any drawdown metric.
8. **Daily candle ingestion deadlock — root-caused and fixed in the repo 2026-07-27, NOT yet deployed.** `_fresh_symbols()` in `scripts/candle_ingest.py` took its freshness target from `MAX(ts)` of the candle table *itself*. Once the table sat at day D every symbol matched D, `todo` was empty, and the run fetched nothing — which kept it at D. Self-referential deadlock. It only ever broke when the held-symbol force pass happened to pull a newer bar, so ingestion advanced only when a position was open. Evidence: both of 2026-07-23's runs fetched nothing, and Friday 07-24's bar did not land until **Monday 16:00 IST**, meaning the engine scored Monday's session on **Thursday's** closes. History itself is complete (~2640 symbols every session) — nothing was lost, it just lagged. Fixed by targeting the calendar's last closed session (`expected_session()`). Verified against the live DB: the next run would have fetched 22 symbols under the old logic and 2,662 under the new one. **Deploying this changes the DATA the engine sees (fresher, correct) though no strategy logic — expect signals to differ from the stale-data baseline.** Holidays are not modelled, so a holiday run does one wasted full pass (~200s) that ingests nothing and logs a warning.
7. **Technical engine** — `app/indicators.py` now also has ATR (absolute), VWAP, SuperTrend, Ichimoku, pivot points (classic + Fibonacci), Fibonacci retracements and candlestick patterns, plus `advanced_snapshot()` returning all of them. All pure Python, no indicator APIs. **Not yet wired into anything** — `technical_snapshot()`/`TechnicalSnapshot` and the engine's scoring are deliberately untouched, so no trading behaviour changed. Wiring these into the scanner or the AI context is the next step. Still missing from the roadmap list: ADX/Ichimoku are done, but Volume Profile and automatic trendline detection are not.
6. **Mobile polish** — Analyse-tab empty state (recent/suggested symbols); ticker should list held names first.

## Gotchas

- Never call the blocking `_panel()` from a request path — use `_panel_warm()`. The blocking call once made every endpoint hang for ~2 minutes after a restart.
- Restarting the web service restarts the engine with it. (A mid-session restart used to disarm the day's entry window; since 2026-07-27 the window arms from the real session-open time, so this is safe.)
- Any pass that scans the whole universe must be throttled — never run one at the 8-second exit-monitor cadence, or it starves the in-process API.
- All databases are WAL. `VACUUM` only with the market closed and the services stopped; on a large database it can take ~20 minutes.
- Guard every ratio against a zero denominator. A single break-even trade once divided by zero in the profit-factor calculation, 500'd the overview endpoint, and made the whole dashboard render `undefinedNaN`.
- **Alerts fire server-side.** `_check_alerts()` used to be called ONLY from `_stream_payload()`, the SSE builder, so an alert was evaluated only while a browser had the dashboard open — set one, close the tab, and it never triggered. The engine loop now evaluates them every `ALERT_INTERVAL` (20s) via a **lazy** `v2_web` import (module-level would be circular, since `v2_web` imports `v2_live`). Price kinds only: above / below / pct-move, delivered by Telegram. `alert_hit()` fails **closed** on bad input — a spurious fire is a false signal to the user's phone.
- The portfolio tab renders an **allocation & risk** card (deployed, largest position, top-3, position count, max drawdown, distance below high-water, plus a per-position bar). Largest-position colour turns amber at 20% of equity and red at 30%, which lines up with `OVERNIGHT_MAX_POS_FRAC` = 0.30.
- **Portfolio analytics** at `/v2/api/portfolio` (`app/portfolio.py`): allocation, concentration (largest, top-3, Herfindahl over EQUITY so idle cash counts as diversification), peak-to-trough drawdown, and per-lane cumulative realised P&L. Two caveats: **drawdown needs daily equity rows and the book currently has one**, so it reads 0% until more sessions close; and **sector exposure is deliberately null** because `universe.sector` is a catch-all covering 2,594 names.
- **SPA escaping is partial.** `esc()` in `v2_web.py` escapes the recommendation card and news headlines, which render external text (news titles, NSE filing subjects) via `innerHTML`. The rest of the SPA still interpolates unescaped — treat any new `innerHTML` interpolation of external text as an XSS bug and wrap it. `tests/test_stock_ui_render.py` executes the template in node and will fail if the escaping regresses.
- **No LLM is actually running.** The box sets `LLM_PROVIDER=deepseek` and `LLM_DECISION_MODE=primary`, but `DEEPSEEK_API_KEY` is **empty**, so `LLMBrain.enabled` is False and every LLM path silently falls back to deterministic logic. The config reads as if a model decides; it does not. Supply a key before believing anything labelled AI-driven.
- **Narrative prose is guarded** (`app/narrative.py`). `narrate()` accepts any writer callable and passes its output through `verify_narrative()`, which discards sentences containing numbers not traceable to the evidence `value` fields — deliberately not the `claim` prose, since harvesting free text would let injected content whitelist its own figure. One unsupported figure discards the whole note and the deterministic prose is served instead.
- **Analyst panel** (`app/analysts.py`, served as `recommendation.panel`): four independent analysts — technical, catalyst, risk, position — each with its own stance, confidence and evidence, reconciled by a CIO. Two deliberate properties: an analyst with no data **abstains** and is excluded rather than counted as a neutral vote, and the risk analyst's stance is capped at zero so it can veto enthusiasm but never manufacture it. The CIO reports **dissent** explicitly and lowers its confidence when analysts genuinely conflict — the thing a single blended score cannot express. Display-only.
- **Recommendations are deterministic, not LLM-written** (`app/recommendation.py`). A 7-level call (Strong Sell → Strong Buy) built from weighted signals — engine conviction, MA structure, SuperTrend, Ichimoku, relative strength, news — each carrying an `evidence` entry naming its metric, value and source. Missing facts drop their signal and lower `confidence` rather than being filled with prose; that is how "never hallucinate" is satisfied. Thin or contradictory evidence is pulled back from the extremes, and stale indicators cut confidence by 30%. **Display-only** — `verdict` and the lane logic are untouched, so it does not affect what is traded.
- **Exit rules live in `evaluate_exit()`** (`v2_live.py`), extracted from `exit_monitor` so they are testable; `exit_monitor` keeps every side effect. Precedence is deliberate and pinned by tests: **stop is checked before the BTST next-open exit**, so an overnight gap *through* the stop books as a stop loss, not a gap capture. A poll-based stop fills at the gapped price, not the stop level — honest, but not a resting order.
- **CI runs on every push and PR** (`.github/workflows/tests.yml`): the full suite on Python 3.12 (matches the deploy box) and 3.14 (matches local dev). Dependencies install from `requirements.txt` **only**, so an undeclared import fails the build — that gap once shipped a box that booted with no trading engine. Note the runner has no network fixtures or prod paths; keep new tests that way or CI will go red.
- Research and backtest scripts (`scripts/btst_bt.py`, `scripts/exit_stop_bt.py`, …) run on the server against the read-only databases.
- **This repository is public.** Never commit host addresses, SSH logins, tokens or keys. `tests/test_repo_secret_hygiene.py` scans tracked files for credential-shaped strings and public `user@ip` SSH targets, and fails the build if one appears — run the suite before pushing.
- `numpy`, `pandas` and `joblib` were missing from `requirements.txt` while `v2_engine`/`v2_live`/`meta_filter` imported them, and `app/main.py` caught the ImportError and logged a warning — so a clean install produced an app that booted and served pages **with no trading engine at all**. Declared now, and `tests/test_dependency_contract.py` fails if app/ ever imports something undeclared.
- The v2 engine still has **no behavioural test coverage** — the suite only asserts it imports. The 132 skips in the run are deliberate legacy retirements, not the engine. `unittest discover` reporting OK says nothing about whether lane logic is correct.
- **Self-registration** (`POST /api/auth/signup`) exists but is **off unless `SIGNUP_ENABLED=true`**. New accounts are always role `user`, active, with no credits, paper cash or LLM assignment — `signup_user()` takes only a username and password, so there is no payload field that could request a role. Throttled per IP. There is still **no email capability anywhere in the repo** (no SMTP/SES/SendGrid), so email verification and password reset cannot be completed end-to-end until a mail provider is added — that is the blocker for the rest of the auth roadmap.
- **Session auth** (`app/auth.py`): cookies are signed with `AUTH_SESSION_SECRET`; if unset, a random secret is generated once and persisted in `runtime_settings`. It is never derived from `ADMIN_PASSWORD` and there is no hardcoded fallback — the old literal fallback let anyone forge an admin cookie. Login is throttled per (IP, username): 5 failures → 15 min lockout, in-process only, so moving to multiple uvicorn workers requires a shared store.
- **Resolved 2026-07-27:** `joblib` 1.5.3, sklearn 1.9.0 and `meta_model_IN.pkl` are all present on the box — the P(win) meta-filter is genuinely live, not failing open. Box runs numpy 2.4.4 / pandas 3.0.3, both inside the declared ranges, so `pip install -r` will not move them.
- **Previously unverified:** whether `joblib` is installed there. `meta_filter` fails open by design — no joblib means no model, `score()` returns None for every symbol, and the daily engine trades **unfiltered**. Its own docstring says the unfiltered engine is a net loser after costs (−0.27%/trade, PF 0.91), so if the P(win) gate is silently inert that matters. Check before assuming the meta-filter is live.
