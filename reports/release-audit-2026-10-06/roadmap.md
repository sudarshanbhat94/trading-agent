# D, F, G. Delivery phases, implementation tasks and launch evidence

**Current programme state: local safety work advanced; commercial/live release NO-GO.** Preserve the existing ₹10,000 epoch, full history and registered experiment. No production rollout, broker configuration, real order, paid purchase or model promotion is authorized by this implementation. The latest human scope correction is Upstox only; Angel One is excluded from current work. Official public data is supplemented with licensed sources wherever technical coverage/reliability or commercial rights require it.

Engineering-hour ranges overlap and include targeted tests; external approvals and market sessions are separate. The previous seven tasks described repairs now largely implemented locally. The seven below address the current remaining work, not another repeat of the old migrations.

## D. Phased roadmap

| Phase | Milestone / scope | Dependencies | Effort | Exit criteria |
|---|---|---|---:|---|
| 0 — critical safety | Immediate controls from F02–F15: risk/exit identity, personal exits, held feed, fail-closed gates, unsafe HTML, peak/epoch safety; legal/broker classification discovery begins. New catalogue and complete broker-ledger/protection implementation continue in phases 1–2; affected routes remain unpromoted until complete. | Reconciled backups; frozen current book/evidence; known managed ownership | 160–240h | Reproduced defects no longer reproduce; no second entry path; exits survive entry disarm; exact ownership and account invariants hold. No automatic promotion of unvalidated strategies. |
| 1 — instruments, adapters, paper | Versioned instrument/master/calendar/action catalog; canonical plan/order/exit contract; Paper + existing Upstox adapter ports; supported cash/ETF route, explicit refusals; Upstox contract fixtures | Phase 0 identity/risk safety; broker docs and scoped account permissions | 180–280h | NSE/BSE same-ticker/series fixtures independent; daily sync is dated/recoverable; identical domain path before paper/live adapter; no fabricated unsupported trades. |
| 2 — risk/execution/reconciliation | Account-level atomic reservations, durable semantic idempotency, fill/fee ledger, native protection, actual inventory/tradebook reconciliation, incidents and restart recovery | Phase 1 identity/ports; certified broker sandbox/fixtures; Phase 0 exit authority | 180–260h | Zero unresolved test divergences; no duplicate/oversold position through partial/rejected/unknown fills and outages; protected state backed by broker evidence. |
| 3 — derivatives and further venues | Index/stock futures, options and defined-risk multi-leg routing; lot/tick/freeze/margin/greeks, expiry/delivery/roll guards; asset-specific currency/commodity/US adapters and cost/FX/settlement where eligible | Phase 2; official contracts; customer entitlements and margin APIs; validated models | 240–400h for first approved derivatives scope | Actual funded contract quantities and confirmed legs; no naked/physical-settlement surprise; each venue/adapter separately certified. Additional brokers/venues expand this estimate. |
| 4 — product UI / landing / onboarding | Typed account/mode/plan/order/protection lifecycle, desktop/mobile design system, accessible charts, truthful P&L, guided readiness, landing/help, idempotent billing/renewal | Canonical contracts from phases 1–2; truthful capability matrix; initial copy/security fixes run in phase 0 | 160–260h | End-to-end plan → approved paper intent → confirmed fill → protected position → exit → actual P&L; account numbers match ledger; no marketing overclaim. |
| 5 — commercial/security/reliability acceptance | Qualified RA/IA/algo-provider/broker arrangement, data rights/privacy/grievance; session/secret hardening; SBOM/advisories; fenced workers, load/chaos/restore/CI promotion gates | Legal/broker work starts in phase 0; validated data/source retention; phases 2–4 | 120–200h plus external lead time | Zero known Blocker/Critical defects; applicable vulnerability exceptions explicit; restoration and incident rehearsal pass; approved commercial service and hosting scope recorded. |
| 6 — monitored beta / public gate | Independent paper cohort and operational burn-in; small authorised user group; smallest eligible live unit after all gates; limited exposure then scope expansion | All relevant preceding gates; independent strategy evidence; explicit human authorisation for live use | 80–120h plus at least 30 relevant trading sessions | No state divergence through burn-in; actual opportunities/fills/closure samples sufficient for the preregistered quant gate; broker and operator capability verified. Public launch only for certified asset/model/account scopes. |

Approximate initial programme envelope: **1,120–1,760 engineering hours**, subject to re-estimation after phase 0 and commercial/broker decisions. This does not include unlimited adapters, all possible strategies or market-observation time. The scope can be released in certified subsets without pretending the entire asset catalogue is executable.

### Strategy evidence runs alongside delivery

Freeze one liquid individual-stock hypothesis and its exact trade contract, then collect official point-in-time membership/quality/news/price evidence. Run untouched after-cost walk-forward and a genuinely capital-constrained forward portfolio using the same risk/exit/fill contract. Retain negative, untouched and invalidated plans. Do not mine the already inspected losing cohort to choose thresholds and call that validation.

The current ₹10,000 paper epoch and publications stay intact. Additional paper test accounts are isolated and are not presented as resets that erase losses. Operational correctness and statistically credible positive expectancy are **different gates**. Thirty idle sessions do not satisfy the opportunity/fill/exit validation requirement.

**Immediate human priority:** data correctness, correct Ideas population, engine call eligibility, order placement and outcome tracking. Finish this path before unrelated broker/product expansion.

## F. Seven pasteable implementation tasks

### 1. Complete native protection and actual broker accounting — 48–80h

**Paste:** “Extend the existing actual trade-ID ledger and reviewed native terminal-fill policy. Source/allocate actual final broker fees, margin and cash/holding settlement; reconcile net/FX without estimates. Resolve completed GTT child outcomes, quantity amendments, session/RMS/corporate cancellation and credential/outage handoff. Keep stable cancel/transmission claims, append-only actual events and worker fencing; never repeat an unknown outcome. Do not activate policies or place orders.”

**Acceptance:** partial/timeout/cancel/trigger/takeover/restart tests cannot duplicate or oversell; exact owned net/cash/settlement matches broker evidence; failure never abandons protection. Actual sandbox certification is separate. F08/F09/F11/F20.

### 2. Production official evidence connectors and canonical migration — 48–80h

**Paste:** “Build actual official master/actions/calendar/restriction and rights-reviewed market/fundamentals/news connectors producing the already implemented normalized bundle and PIT fact contracts. Preserve source artifacts/hash/observed/effective/publication dates. Import via the existing atomic catalogue receipt/quarantine path; do not infer old series/ownership from a current ticker. Add reliable daily source coverage/outage reporting and reviewed corporate-action adjustment evidence without resetting portfolios or synthesizing history.”

**Acceptance:** real source fixtures and independent dated acceptance; collisions, action revisions, stale missing rules and ambiguous legacy ownership refuse; startup/import failures preserve the existing epoch. Source availability and commercial rights are separately recorded. F10/F25/F26/F27/F28.

### 3. One approved execution orchestration for paper and Upstox — 56–96h

**Paste:** “Retain the approved/manual cash-paper pipeline, immutable plan bindings and unified risk. Converge house and broker approved-plan attribution/reservations with the journal and actual ledger. Converge only the existing Upstox adapter; certify its native protection with explicit broker evidence and failure handling. Preserve exact account/product/contract/model/protection semantics and unsupported route refusals. Do not create passing live authorization.”

**Acceptance:** one approved fixture traverses paper and mocked supported broker entry/fill/protection/exit/reconciliation/accounting with identical risk decisions; retry/partial/cancel/unknown/external inventory and parallel account cases remain safe. No real order. F01/F18/F19/F21.

### 4. Independent liquid-stock research validation — 40–72h plus forward observations

**Paste:** “Populate existing PIT membership/fundamental/adjusted-price/delivery/news/benchmark stores from reviewed sources. Freeze a liquid individual-stock hypothesis and protocol before evaluating untouched after-cost data with the unchanged ₹10,000 portfolio constraints. Retain losing/untouched/superseded/rejected publications and original registered experiment. Compare only matching-time benchmark intervals. Report non-estimable metrics and coverage limits; propose exact new model version without promoting it.”

**Acceptance:** reproducible decision-time evidence, no future corrections/survivorship/overlap leakage, actual affordable tickets and cost/risk allocation; independent positive evidence and market forward cohort required before approval. F01/F22/F28/F29/F30/F49.

### 5. Complete production product/billing/accessibility journeys — 64–104h

**Paste:** “Reuse current account/report/frozen review/receipt/incident components. Finish real ideas/watchlist/review/approved buy/position/order/reconciliation flows, guided broker/risk setup, landing/help, signed gateway webhooks, renewal/refund/tax reconciliation. Keep atomic manual receipt fallback and owner/mode asynchronous guards. Research, approvals, paper fills and real fills stay visibly different. Test actual full auth journeys at 360/390/768/desktop and keyboard/screen-reader states; do not make unapproved research executable.”

**Acceptance:** rendered balances match actual ledger; no cross-owner/mode stale response, clipping or missing protection; replayed payment callbacks cannot double grant, refunds/expiry remain exact. Full deployed browser acceptance is separate. F17/F37/F45/F46/F47/F48/F49.

### 6. Complete operational/security/legal/data-rights package — 56–96h plus external lead time

**Paste:** “Use existing v2 streaming restore, versioned critical migrations, exact locks/SBOM/advisory scan and owner incident inbox. Finish supervised complete-source quiescence, encrypted scheduled/off-host retention, escrow/key rotation and actual restore/rollback/load/outage/SLO drills. Complete auxiliary schema migration coverage, independent SAST/license/privacy/host/proxy review and consented on-call escalation. Record qualified commercial/exchange/broker classification and source-specific rights as unresolved until verified. No purchase, deployment or credential changes.”

**Acceptance:** complete restored deployment stays disabled, preserves exact protocol/uncertainty and cannot replay orders; actual independent security/licensing/backup evidence and explicit unresolved dependencies. Passing fixture or declared role alone is insufficient. F16/F36/F37/F38/F39/F40/F41/F42/F43/F44/F50/F54.

### 7. Additional-route certification, burn-in and release proposal — 48–96h plus relevant sessions

**Paste:** “Implement each required instrument route separately: funded futures/defined-risk options with margin/freeze slicing/multi-leg compensation/expiry/physical settlement; BSE/US/currency/commodity with actual calendars/FX/settlement. Upstox first, Angel second. Certify exact broker/asset/model/account/product scopes; unsupported and underfunded routes stay disabled. Collect at least 30 independent relevant paper sessions with reviewed dated entry/exit/protection evidence. Produce exact clean-commit canary/rollback release manifest and concrete separate deployment/promotion/live request. No real orders.”

**Acceptance:** failed hedge never silently creates naked exposure; unknown quantity/settlement rules refuse; no unexplained divergence, duplicates, oversells or abandoned protection. Idle days/aggregate invented counts never satisfy relevant sessions. Operational, profitability and commercial gates remain separate. F01/F08/F16/F21/F22/F24/F44.

## G. Launch checklist and external dependencies

**Current decision: NO-GO.** These boxes are evidence requirements, not actions already completed. Local passing tests and repaired IDs cannot supply deployed, strategy or commercial evidence.

- [ ] Zero known Blocker/Critical defects for the released scope, with regressions and explicit unresolved dependencies.
- [ ] One canonical orchestration, account reservations, immutable plan/intent/fill/fee events and exact ownership across Paper and each released broker adapter.
- [ ] No duplicates, oversells, unexplained cash/inventory/fee divergence or abandoned protection through partial fills, unknown outcomes, races, takeover/restart and outages.
- [ ] Native protection and cancellation/failure/credential recovery certified for required exposure; GTT execution is not assumed guaranteed.
- [ ] Canonical effective-dated instruments, complete calendars/actions/restrictions/settlement/margin, fresh data and actual affordable quantity verified for each route.
- [ ] Actual personal/house/broker performance and browser-visible balances reconcile with the ledger; average R excludes unknown original risk, no invented benchmark/Sharpe/expectancy.
- [ ] Independent frozen after-cost strategy evidence passes its own reviewed gate, with realistic ₹10,000 capital, all outcomes retained and no legacy/synthetic cohort counted as independent.
- [ ] At least 30 relevant paper sessions and observed entry/exit/protection/recovery events, with zero unexplained divergence. Idle sessions alone do not qualify.
- [ ] Cross-user/session/CSRF/XSS/proxy/admin security, dependency advisory review, encrypted-secret restoration and repository secret hygiene pass.
- [ ] Full desktop/mobile watchlist/review/buy/portfolio/order/protection/reconciliation/subscription flows and accessibility pass in a permitted browser.
- [ ] Complete backup/restore/rollback, incident acknowledgement/escalation, load/outage and measured SLO evidence recorded.
- [ ] Qualified legal/commercial classification, exchange/broker provider/hosting/API requirements, data licences, privacy/grievance/retention and user terms recorded.
- [ ] Exact release commit, clean source, explicit certified scopes and reviewed evidence manifest; deployment, promotion and live beta independently authorized.

**Unresolved external dependencies:** qualified commercial entity/service classification and any registration/empanelment; Upstox vendor/hosting/API/protection permissions and supported sandbox evidence; point-in-time fundamentals/prices/news coverage and commercial redistribution licence; account-specific margin, sell authorization and derivative eligibility; secret escrow, on-call/incident recipient and complete recovery environment; genuine forward sessions and approved independent strategy results. Public access, an API token or transparent scoring does not satisfy these dependencies.

**Decisions already resolved:** Upstox only (latest human correction); official public sources plus licensed data as necessary; existing ₹10,000 epoch and forward protocol preserved; other routes are individually certified rather than assumed. No further broker-priority or capital question is needed.

## Evidence evaluator limitations

`scripts/check_release.py` checks the completeness/build identity of externally reviewed recorded evidence. It is initially scoped to NSE paper-session dates. A reviewer name and path are not cryptographic proof or a legal permission; authenticated evidence/attestations and each other market calendar require further work. The current `release-evidence.json` deliberately fails. Never populate it with synthetic claims to manufacture GO.

## Current delivered versus remaining scope

[Exact implementation ledger](../release-plan-implementation-2026-10-06.md) records all delivered code and tests. All seven tasks above concern remaining work; storage, canonical gates, manual approval binding, actual fill events, native partial recovery, receipt atomicity, incident acknowledgement, streaming disabled restore and exact dependency locks are implemented locally. Production raw connectors, final actual net/margin settlement, broker/house convergence, complete product/security/ops and additional-route implementation remain substantial engineering. Strategy proof, permissions and relevant observed market sessions remain separate acceptance. The whole commercial programme is not complete.

Native completed-child capture, claim-once reduction, oversized-child cancellation and final-fee-coverage net allocation are now implemented locally. Task 1 still requires automatic fee/settlement sourcing, margin/FX/all-adapter accounting and actual scoped broker/outage acceptance; it is not marked complete.
