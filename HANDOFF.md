# OpenStocks handoff — 2026-09-23

## Release-plan continuation — 7 October 2026

**TRADING BEHAVIOUR CHANGED locally; not deployed.** All entry writers enforce sourced dated canonical NSE cash contracts and regime/risk gates. Manual paper requests freeze immutable owned plans and use the approved pipeline; retry/concurrency/rollback are tested. Actual Upstox trade-ID ledger posts gross cash/assessment/settlement/reversal history; final actual net/margin remain unavailable. Reviewed native policy can cancel partial remainders once and activate new canonical terminal fills only. No passing policy/release record was created.

Also delivered: normalized evidence importer/quarantine, Angel One transport (orchestration/native/live uncertified), immutable PIT facts, atomic manual subscription receipt/access/status (new reproduced Critical F54), owned incident acknowledgement, origin/proxy boundary, versioned atomic critical migrations, authenticated streaming disabled restore, exact dependency locks/SBOM/zero-active-known-advisory scan. Production raw connectors/full net margin ledger/all-adapter convergence/full product/ops/security/additional routes remain engineering work. See `reports/release-plan-implementation-2026-10-06.md` and current A–G `reports/release-audit-2026-10-06/REPORT.md`. Full programme incomplete/NO-GO, no independent profitable stock model.

Final verification is in `verification.json`: 2,375 run / 2,242 pass / 133 skips, 102 research/UI functions, isolated paper rehearsals and selected-handler mobile component proof. Tests use temporary runtime paths and block external HTTP; retained storage fixtures explicitly inject external contract/native evidence, new integration tests use actual synthetic catalogue/risk/ledger code. Read-only deployed snapshot dated 6 October remains build `2b300b4`, user2 ₹10,000 cash/equity, zero positions, unchanged epoch/protocol. No production writes/deployment/real orders/reset/model promotion.


## Current-state audit refresh — 2026-10-06

**TRADING BEHAVIOUR CHANGED locally; not deployed.** Unsupported/malformed broker submissions now refuse at the journal/transport boundary. Personal paper postings are balanced, immutable and atomic with book writes; original risk and actual per-sleeve/regime performance are recorded. Rejected dated research checks are archived and visible in Tracking without changing the registered experiment. Exact quote-age health and a recorded-evidence NO-GO evaluator are added. No production book/protocol/settings/strategy threshold was changed.

The refreshed A–G audit preserves F01–F50 and adds F51–F53: 19 local repairs, 12 partial repairs, 16 capability gaps and six unverified requirements. Source baseline b588ffd plus the recorded working-tree changes; read-only OCI remains 2b300b4, clean user paper ₹10,000. This is a dated observation, not a live assurance. See `reports/release-audit-2026-10-06/REPORT.md` and `reports/release-audit-implementation-2026-10-06.md` for exact acceptance, migration and remaining work.

Final full suite: 2,279 run, 2,146 passed, 133 skipped, zero failures/errors. The final 16 ledger/performance/API/legacy-control checks passed after currency/current-snapshot labels were added; research/UI runner 102 passed. The initial sandbox-blocked localhost test is recorded separately; final permitted run passed. Isolated lifecycle/ledger rehearsal passed. Browser evidence is one synthetic current Tracking component at desktop/390px, not full production authentication/buy/billing certification. Native protection, canonical migration/full live ledger, independent stock-model approval, restore and commercial/broker/data rights remain open; commercial/live release NO-GO.

## Execution/security implementation — 2026-10-06

**TRADING BEHAVIOUR CHANGED; not deployed.** House book writes now enqueue durable account delivery after commit; retries retain intent identity; an expired worker generation cannot write. New broker entries require fresh actual inventory/trades/funds reconciliation. Broker files are account-bound encrypted; logout revokes server sessions. New read-only catalogue/publication/health APIs expose scope honestly. Instrument discovery does not enable derivatives or another broker. Manual requested quantity is honoured or refused; research plans cannot fall back to manual execution.

See `reports/execution-hardening-2026-10-06.md` for commands, evidence, migration/escrow requirements and the open programme. CI runs the research/UI pytest functions in addition to unittest; they were omitted by the previous runner. No production book, broker setting or strategy threshold was changed. Profitability, native stops, automatic production live mirroring, stock-model promotion and all-asset execution remain unproven/incomplete; commercial/live release remains NO-GO.

Final local verification: 2,254 unittest checks run, 2,121 passed, 133 skipped, zero failures/errors; separate research/UI functions 101 passed; isolated paper rehearsal and staged secret scan passed. Remote CI and deployed behaviour require separate verification.

## Local safety implementation — 2026-10-06

**TRADING BEHAVIOUR CHANGED; local only, not deployed.** Personal and managed broker entries now share account-specific, cost-aware risk checks; position exit policies/origin links are persisted; personal exits run independently; entry disarm retains managed exits. Legacy entry controls are retired. Capital defaults and strategy thresholds are unchanged. See `reports/release-safety-implementation-2026-10-06.md` for the exact changes and remaining release blockers.

Final full suite: 2,226 run, 2,093 passed, 133 skipped, zero failures/errors. This is the first safety milestone, not the completed release programme or proof of profitability. Broker inventory reconciliation/native protection, transactional crash recovery, instrument master and stock-model validation remain open; commercial/live release is NO-GO.

This is a dated orientation, not a live portfolio or broker-status report. Verify changing facts from the production book, API, and code before reporting them.

## Current trading path

- `app/v2_live.py:loop()` calls `sleeve_pass()` for paper entries. `app/sleeves/config.py` lists `index_directional` as the only production sleeve; `quality_momentum` is observation-only. Legacy lanes remain disabled. `app/sleeves/risk.py` applies unified limits.
- Subscriber paper-book mirrors cap their quantity at the house-approved shares and their own available cash. They do not independently validate strategy profitability.
- The default paper capital is ₹10,000. The current regime gate may intentionally hold cash. Do not equate an idle cycle with a broken feed, or a displayed research watch with a funded trade.
- `app/live_trade.py:MIRRORED_LANES` does not include `index_directional`; the production sleeve therefore cannot automatically mirror its entries to a broker. Do not claim automatic live trading works without verifying this path and broker readiness.
- The corrected stock replay in `reports/stock_replay_2026-09-23.md` applies the production allocator. It yielded just one holdout trade and did **not** establish an after-cost equity edge. Its retrospective sample has survivorship and coverage limits. There is no verified consistently profitable engine.

## Next product objective

The user wants liquid, high-quality individual NSE equities, with index exposure as one choice. Build a dated, adjusted, point-in-time universe and quality dataset; test stock rules after fees and slippage on an untouched holdout; then forward-test successful candidates in the ₹10,000 paper book. Keep broker execution gated until paper and broker reconciliation are verified. Do not enable a stock sleeve merely to produce trades.

## Non-negotiables

The repo is public: never commit credentials, host addresses, keys, or account tokens. Paper-book correctness outranks features. State **TRADING BEHAVIOUR CHANGED** prominently in both commit and report when applicable. Never promise profit, enter the user's API credentials, or place real trades on the user's behalf.

## Idea observations — 2026-10-01

`app/screening/tracking.py` archives each subscriber's first delivered conditional stock plan in a separate `var/idea_tracking.db`; changed levels/date/quantity create separate versions. `scripts/idea_tracker.py` and `deploy/opentrade-idea-tracker.timer` observe the existing quote feed every 30 seconds without touching paper books or strategy rules. Ideas → Tracking and the private `/v2/api/idea-tracking` endpoint expose frozen plans and forward observations. Entry-zone scenarios are hypothetical, not approved entries or actual paper P&L. See `docs/idea-tracking.md` for methodology and the read-only report command. Initial capture began October 1 at 09:27:07 IST; earlier history is not backfilled.

## Resumed release-plan engineering — 2026-10-06

**TRADING BEHAVIOUR CHANGED locally; not deployed.** Immutable approved NSE cash-paper plans now traverse exact dated contract/session checks, shared account risk, fill, owned exit and balanced P&L. New broker risk requires private reviewed exact-build/account/route/model authorization; a connected/armed account is insufficient. Explicitly activated native stop obligations handle unknown/timeout/partial/cancel/trigger/re-entry cases; late acknowledgements/cancellations/status writes are fenced across takeover. Normalized journal observations are append-only, not an actual fee/settlement ledger. Encrypted declared-deployment restore creates a private execution-disabled copy; existing backup no longer deletes live equity history. Current SPA adds actual epoch/day sleeve/regime performance, approved paper review and protection warnings.

Final frozen-source unittest 2,321 run / 2,188 passed / 133 skipped / zero failures/errors; research/UI runner 102 passed; isolated real-handler paper and component browser checks passed desktop/390px. Read-only deployed build remains 2b300b4, clean ₹10,000 cash/equity, zero positions; unchanged epoch. No deployment, real trade, reset, credential change or model promotion. See `reports/release-plan-implementation-2026-10-06.md` and `docs/recovery-and-approved-paper.md` for limitations and commands.

The full commercial release plan remains **incomplete/NO-GO**. Remaining engineering includes official contract ingestion/canonical migration; all-adapter actual fill/fee/margin/settlement accounting; automatic certified native coverage and real broker recovery; Angel One/additional assets; full production product/billing/accessibility; operational/security/restore certification. Independent stock validation, real relevant paper sessions and legal/broker/data permissions remain external acceptance. Never equate fixtures, declared backup roles, a review-reference string or the recorded-evidence evaluator with independent proof.
