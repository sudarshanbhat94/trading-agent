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

1. **BTST needs ongoing evaluation** — for each basket, compare entry (prior close) against the next open, net of costs. As of 2026-07-27 the first live basket (KFINTECH, RKFORGE, RRKABEL) was held overnight for a 2026-07-28 open sell.
2. **Catalyst ingestion gap** — at least one filing (CarTrade results) was never *ingested*, which is a separate failure from being ingested and misclassified. Worth finding out why filings are missed.
3. **BTST overlap decision (open)** — the lane currently skips symbols `volume_surge` already traded that day, which excludes some of the best overnight candidates. Decide whether to allow the double exposure.
4. ~~**Overnight sizing cap**~~ — **done 2026-07-27.** `OVERNIGHT_MAX_POS_FRAC` (0.30 of equity) in `v2_live.py`, applied via `cap_overnight_shares()` to every lane that holds through the close; intraday lanes are exempt and already size off a fixed slot. It is a guardrail, not a re-tuning: normal sizing spans 9.2–26.7% of equity, the largest position in the live book's history was 26.6%, and none of the closed trades would have been resized. It only clips the case where `DYN_ALLOC` hands nearly all free cash to the last open slot. **Do not lower it below ~0.27 without a backtest** — that would start re-sizing normal trades.
5. **News feed + performance-analytics tab** — never built.
7. **Technical engine** — `app/indicators.py` now also has ATR (absolute), VWAP, SuperTrend, Ichimoku, pivot points (classic + Fibonacci), Fibonacci retracements and candlestick patterns, plus `advanced_snapshot()` returning all of them. All pure Python, no indicator APIs. **Not yet wired into anything** — `technical_snapshot()`/`TechnicalSnapshot` and the engine's scoring are deliberately untouched, so no trading behaviour changed. Wiring these into the scanner or the AI context is the next step. Still missing from the roadmap list: ADX/Ichimoku are done, but Volume Profile and automatic trendline detection are not.
6. **Mobile polish** — Analyse-tab empty state (recent/suggested symbols); ticker should list held names first.

## Gotchas

- Never call the blocking `_panel()` from a request path — use `_panel_warm()`. The blocking call once made every endpoint hang for ~2 minutes after a restart.
- Restarting the web service restarts the engine with it. (A mid-session restart used to disarm the day's entry window; since 2026-07-27 the window arms from the real session-open time, so this is safe.)
- Any pass that scans the whole universe must be throttled — never run one at the 8-second exit-monitor cadence, or it starves the in-process API.
- All databases are WAL. `VACUUM` only with the market closed and the services stopped; on a large database it can take ~20 minutes.
- Guard every ratio against a zero denominator. A single break-even trade once divided by zero in the profit-factor calculation, 500'd the overview endpoint, and made the whole dashboard render `undefinedNaN`.
- Research and backtest scripts (`scripts/btst_bt.py`, `scripts/exit_stop_bt.py`, …) run on the server against the read-only databases.
- **This repository is public.** Never commit host addresses, SSH logins, tokens or keys. `tests/test_repo_secret_hygiene.py` scans tracked files for credential-shaped strings and public `user@ip` SSH targets, and fails the build if one appears — run the suite before pushing.
- `numpy`, `pandas` and `joblib` were missing from `requirements.txt` while `v2_engine`/`v2_live`/`meta_filter` imported them, and `app/main.py` caught the ImportError and logged a warning — so a clean install produced an app that booted and served pages **with no trading engine at all**. Declared now, and `tests/test_dependency_contract.py` fails if app/ ever imports something undeclared.
- The v2 engine still has **no behavioural test coverage** — the suite only asserts it imports. The 132 skips in the run are deliberate legacy retirements, not the engine. `unittest discover` reporting OK says nothing about whether lane logic is correct.
- **Self-registration** (`POST /api/auth/signup`) exists but is **off unless `SIGNUP_ENABLED=true`**. New accounts are always role `user`, active, with no credits, paper cash or LLM assignment — `signup_user()` takes only a username and password, so there is no payload field that could request a role. Throttled per IP. There is still **no email capability anywhere in the repo** (no SMTP/SES/SendGrid), so email verification and password reset cannot be completed end-to-end until a mail provider is added — that is the blocker for the rest of the auth roadmap.
- **Session auth** (`app/auth.py`): cookies are signed with `AUTH_SESSION_SECRET`; if unset, a random secret is generated once and persisted in `runtime_settings`. It is never derived from `ADMIN_PASSWORD` and there is no hardcoded fallback — the old literal fallback let anyone forge an admin cookie. Login is throttled per (IP, username): 5 failures → 15 min lockout, in-process only, so moving to multiple uvicorn workers requires a shared store.
- **Resolved 2026-07-27:** `joblib` 1.5.3, sklearn 1.9.0 and `meta_model_IN.pkl` are all present on the box — the P(win) meta-filter is genuinely live, not failing open. Box runs numpy 2.4.4 / pandas 3.0.3, both inside the declared ranges, so `pip install -r` will not move them.
- **Previously unverified:** whether `joblib` is installed there. `meta_filter` fails open by design — no joblib means no model, `score()` returns None for every symbol, and the daily engine trades **unfiltered**. Its own docstring says the unfiltered engine is a net loser after costs (−0.27%/trade, PF 0.91), so if the P(win) gate is silently inert that matters. Check before assuming the meta-filter is live.
