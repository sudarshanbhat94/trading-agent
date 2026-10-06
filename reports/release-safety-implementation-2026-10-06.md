# First implementation milestone — account and exit safety

**TRADING BEHAVIOUR CHANGED. Local implementation; not deployed or live-certified.**

This implements the immediate account/exit controls from the release audit. It does not enable research stock models, change conviction/regime thresholds, change the fee rates, reset any production portfolio or grant commercial/live-release approval. The current paper-capital default remains ₹10,000.

## Implemented changes

| Audit issue | Change | Verification / limit |
|---|---|---|
| F02 | Persisted versioned ExitPolicy, carried from candidates into house and personal positions. An explicit zero holding limit means no time stop. Monthly index positions no longer inherit the accidental five-session exit. | Five-session and configuration-change regressions; monthly regime gate remains in place. Existing known policies are backfilled additively. |
| F03 | Manual paper, subscriber mirrors and managed broker entries use the shared cost-aware RiskManager with their own capital, equity, cash, daily loss and open risk. Check/write is serialized; broker entry rechecks under the journal writer lock. | Halted-account and simultaneous requests tested. Missing held marks or daily baselines refuse new risk. Broker state remains estimated from managed fills, not certified external inventory. |
| F04 | Every active personal paper position is monitored independently of subscription or house membership in the production loop. | Personal-only stop and stale-mark tests. Existing unknown/missing protection still requires explicit remediation; no fabricated fill. |
| F05 | Mirrors reference the originating house position ID. Automatic exits match origin, account and active epoch, rather than closing every account holding that ticker. Broker intents retain origin IDs too; linked exits queue even while a buy fill is unknown. | Manual/other-origin holdings remain open; exact linked mirrors close once. Unknown historical broker origins are not silently adopted. Broker exits retain the confirmed entry's instrument and product when the universe changes. |
| F06 | Separate entry readiness from valid-credential exit readiness. Normal entry disarm retains management of confirmed owned broker holdings. | Mocked broker lifecycle checks; unusable credentials still refuse and retain the exit request. Native broker protection is not implemented in this milestone. |
| F07 | Hot-feed inventory includes personal holdings, pending/unknown orders, managed residual fills and protection obligations. | Isolated database test covers all four. The existing equity feeder is not certification of derivative feeds or a complete instrument master. |
| F11 | Personal entries/sells use serialized transactions. New house/personal entries debit the existing buy-leg charges; cash, equity and the house report include them. Closed net P&L charges the round trip once. Allocator cash also reserves estimated fees/slippage. | Once-only sell, after-cost cash and risk checks. Historical positions retain zero entry-fee backfill; historical realised P&L is not restated. A full multi-asset posting/settlement ledger remains future work. |
| F12 | Book creation no longer silently changes allocated capital or resets an epoch. Durable account peaks survive chart replacement/pruning; broker allocation changes require an explicit approved epoch. | Snapshot replacement and default-change regressions. Previously lost observations are not reconstructed or presented as known. |
| F17 (partial) | House readiness reads the durable peak and uses after-cost open risk, including the separate strategic allocation. Missing held stops refuse new risk in both readiness and the production paper pass. | Durable-peak/pruned-chart and unknown-stop regressions. This does not replace a complete authoritative account/settlement ledger. |
| F13/F14 | Calendar uncertainty refuses manual entries. Legacy start/run/bridge/reset mutation controls return HTTP 410; environment/configuration cannot restart the legacy agent. | Auth guards retained. Historical reads and stop controls remain available. |
| F15 | NSE filing cards use DOM textContent and registered listeners instead of interpolated external HTML/inline handlers. Reset copy describes the actual ₹10,000 epoch and retained historical trades. | Executed JavaScript test uses hostile filing/symbol strings. This does not complete a CSP, penetration or all-page browser review. |
| F40/F42 | Required engine/UI import failure aborts startup; required migration columns are checked explicitly and versioned before the engine thread starts. Unknown critical jobs no longer report healthy; quote-health threshold matches the 120-second execution freshness limit. | Failed migration cannot mark/start an engine. Historical exit reads retain expiry when newer optional columns are absent. Full deployment/restore and worker fencing remain separate release gates. |

Explicit discretionary API buys may supply validated stop/target levels; these remain manual positions, not promoted research-model trades. The old default ±6% manual plan can be refused at ₹10,000 because after-cost risk and minimum-ticket constraints genuinely conflict. A refusal must explain the applicable constraint; thresholds are not loosened to manufacture activity.

## Verification

- Expanded focused run: 305 tests passed, covering release safety, book isolation, broker mocks, journal lifecycle, exits, sleeves, feed integrity, sessions, UI renderers and repository secret hygiene.
- After the full suite exposed old fixtures, a targeted correction run passed 119 tests. Expiry-loader assertions now execute legacy/new schema reads; the separation test counts entry charges rather than assuming an open trade is free.
- Final drawdown-migration boundary checks: 62 tests passed. Known personal chart peaks survive replacement and constrain entries; observations from before the active epoch do not contaminate the book.
- Final full suite: **2,226 tests run in 173.552 seconds; 2,093 passed, 133 skipped, zero failures/errors; exit code 0.** Skipped checks are not counted as passing. The localhost mock-broker wire test was included in the permitted run.
- Repository secret hygiene: all four checks passed after the new files were staged. Source compilation and diff whitespace checks passed.
- Tests use isolated SQLite and mocked broker submission. Background production trading is disabled for the suite. No real order, credential change, portfolio reset or production cycle was run.
- Production browser acceptance remains outstanding; these local DOM tests do not substitute for it.

Run the focused regressions with `OPENSTOCKS_DISABLE_V2=1 .venv/bin/python -m unittest tests.test_release_safety tests.test_user_books tests.test_live_trade -q`. The full suite uses `-m unittest discover -s tests -q`; isolate `BROKER_STATE_DIR` and `BROKER_STATE_PATH` in temporary paths and permit its localhost mock server. No production credentials are required.

## Remaining release work

This is not the completed 50-finding programme. House-to-broker transactional outbox/crash recovery, instrument identity/master migration, actual broker inventory/tradebook/funds reconciliation, native protective orders and compensation, independent approved stock-model evidence, multi-asset adapters, full UI/onboarding/billing, encrypted/revocable credentials, legal/data-rights approval and operational certification remain open. Overnight exposure without a valid daily baseline continues to block entries; a baseline must be supplied by a certified mark, not invented from cost basis. The commercial/live release remains **NO-GO**.

Use [the phased roadmap](/Users/pavithramayya/Documents/Sudarshan/trading-agent/reports/release-audit-2026-10-06/roadmap.md) and [the audit register](/Users/pavithramayya/Documents/Sudarshan/trading-agent/reports/release-audit-2026-10-06/findings.json) for the broader scope. Local test success does not establish positive expectancy or profitability.
