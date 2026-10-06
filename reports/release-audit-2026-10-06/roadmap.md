# D, F, G. Delivery phases, implementation tasks and launch evidence

**Current programme state: local safety work advanced; commercial/live release NO-GO.** Preserve the existing ₹10,000 epoch, full history and registered experiment. No production rollout, broker configuration, real order, paid purchase or model promotion is authorized by this implementation. Upstox is first; Angel One second. Official public data is supplemented with licensed sources wherever technical coverage/reliability or commercial rights require it.

Engineering-hour ranges overlap and include targeted tests; external approvals and market sessions are separate. The previous seven tasks described repairs now largely implemented locally. The seven below address the current remaining work, not another repeat of the old migrations.

## D. Phased roadmap

| Phase | Milestone / scope | Dependencies | Effort | Exit criteria |
|---|---|---|---:|---|
| 0 — critical safety | Immediate controls from F02–F15: risk/exit identity, personal exits, held feed, fail-closed gates, unsafe HTML, peak/epoch safety; legal/broker classification discovery begins. New catalogue and complete broker-ledger/protection implementation continue in phases 1–2; affected routes remain unpromoted until complete. | Reconciled backups; frozen current book/evidence; known managed ownership | 160–240h | Reproduced defects no longer reproduce; no second entry path; exits survive entry disarm; exact ownership and account invariants hold. No automatic promotion of unvalidated strategies. |
| 1 — instruments, adapters, paper | Versioned instrument/master/calendar/action catalog; canonical plan/order/exit contract; Paper + existing Upstox adapter ports; supported cash/ETF route, explicit refusals; SmartAPI fixtures | Phase 0 identity/risk safety; broker docs and scoped account permissions | 180–280h | NSE/BSE same-ticker/series fixtures independent; daily sync is dated/recoverable; identical domain path before paper/live adapter; no fabricated unsupported trades. |
| 2 — risk/execution/reconciliation | Account-level atomic reservations, durable semantic idempotency, fill/fee ledger, native protection, actual inventory/tradebook reconciliation, incidents and restart recovery | Phase 1 identity/ports; certified broker sandbox/fixtures; Phase 0 exit authority | 180–260h | Zero unresolved test divergences; no duplicate/oversold position through partial/rejected/unknown fills and outages; protected state backed by broker evidence. |
| 3 — derivatives and further venues | Index/stock futures, options and defined-risk multi-leg routing; lot/tick/freeze/margin/greeks, expiry/delivery/roll guards; asset-specific currency/commodity/US adapters and cost/FX/settlement where eligible | Phase 2; official contracts; customer entitlements and margin APIs; validated models | 240–400h for first approved derivatives scope | Actual funded contract quantities and confirmed legs; no naked/physical-settlement surprise; each venue/adapter separately certified. Additional brokers/venues expand this estimate. |
| 4 — product UI / landing / onboarding | Typed account/mode/plan/order/protection lifecycle, desktop/mobile design system, accessible charts, truthful P&L, guided readiness, landing/help, idempotent billing/renewal | Canonical contracts from phases 1–2; truthful capability matrix; initial copy/security fixes run in phase 0 | 160–260h | End-to-end plan → approved paper intent → confirmed fill → protected position → exit → actual P&L; account numbers match ledger; no marketing overclaim. |
| 5 — commercial/security/reliability acceptance | Qualified RA/IA/algo-provider/broker arrangement, data rights/privacy/grievance; session/secret hardening; SBOM/advisories; fenced workers, load/chaos/restore/CI promotion gates | Legal/broker work starts in phase 0; validated data/source retention; phases 2–4 | 120–200h plus external lead time | Zero known Blocker/Critical defects; applicable vulnerability exceptions explicit; restoration and incident rehearsal pass; approved commercial service and hosting scope recorded. |
| 6 — monitored beta / public gate | Independent paper cohort and operational burn-in; small authorised user group; smallest eligible live unit after all gates; limited exposure then scope expansion | All relevant preceding gates; independent strategy evidence; explicit human authorisation for live use | 80–120h plus at least 30 relevant trading sessions | No state divergence through burn-in; actual opportunities/fills/closure samples sufficient for the preregistered quant gate; broker and operator capability verified. Public launch only for certified asset/model/account scopes. |

Approximate initial programme envelope: **1,120–1,760 engineering hours**, subject to re-estimation after phase 0 and commercial/broker decisions. This does not include unlimited adapters, all possible strategies or market-observation time. The scope can be released in certified subsets without pretending the entire asset catalogue is executable.

### Strategy evidence runs alongside delivery

Freeze one liquid individual-stock hypothesis and its exact trade contract, then collect official point-in-time membership/quality/news/price evidence. Run untouched after-cost walk-forward and a genuinely capital-constrained forward portfolio using the same risk/exit/fill contract. Retain negative, untouched and invalidated plans. Do not mine the already inspected losing cohort to choose thresholds and call that validation.

The current ₹10,000 paper epoch and publications stay intact. Additional paper test accounts are isolated and are not presented as resets that erase losses. Operational correctness and statistically credible positive expectancy are **different gates**. Thirty idle sessions do not satisfy the opportunity/fill/exit validation requirement.

## F. Seven pasteable implementation tasks

### 1. Native protection and managed-exit recovery — 24–48h

**Paste:** “Implement Upstox cash-position protection obligations behind an explicitly disabled-by-default, separately certified adapter capability. Persist desired/confirmed quantity, stop, broker IDs and unknown/rejected/cancelled status before network operations. Track fill/cancel races, entry disarm and expired credentials without abandoning the owned position. Model GTT as best-effort triggering, not guaranteed execution; reconcile it after RMS/corporate-action/session cancellation. Use official fixtures or a supported sandbox, never real orders or broker-setting changes.”

**Acceptance:** partial entry creates protection for confirmed quantity only; stop/target race cannot oversell; rejected/lost protection raises a durable entry-blocking incident; crash/restart retains obligations. No UI “protected” state without broker evidence. A concrete certification record is required before activation. F08/F20/F21.

### 2. Canonical instrument and historical ownership migration — 32–56h

**Paste:** “Complete instrument/version/broker-binding IDs throughout house, personal, intent, fill and position records. Add availability/effective rules, venue/segment/series calendars, official actions/restrictions/bans, tick/lot/freeze/settlement/circuit fields and a dated daily ingestion job. Produce a copied-database migration/ambiguity report, preserving legacy rows and quarantining unknown ownership. New risk requires a valid instrument/rule/binding; exits retain an explicitly reviewed legacy obligation until mapped. Do not migrate production or enable new routes.”

**Acceptance:** NSE/BSE identical aliases cannot collide; symbol/lot/expiry changes preserve correct ownership/effective versions; missing rules and ambiguity refuse before submission; all existing epochs/history/publications are preserved and test balances reconcile. F10/F24/F26/F27/F40.

### 3. One paper/broker reservation, intent and fill ledger — 40–72h

**Paste:** “Converge current book/outbox/journal orchestration before Paper versus Upstox adapter selection. Persist immutable approved plan, original risk, account/epoch/instrument IDs, stable idempotency, all pending cash/risk/margin commitments and monotonic confirmed fills. Extend the personal integer posting foundation to actual fees, settlement, owned inventory and correction events. Implement submit/modify/cancel/status/fills/inventory/funds/margin capabilities with explicit unknown outcomes and takeover fencing. Do not rewrite legacy fills or call real order APIs.”

**Acceptance:** duplicate callbacks/submits, partial fills, timeout ambiguity, cancel/fill races, account concurrency and restart conserve cash/quantity; no blind resend/oversell/adopted external inventory; actual reconciliation divergence blocks new risk and remains visible. Approved synthetic individual-stock fixture traverses the same orchestration through paper entry, simulated fill/protection/exit/P&L. F09/F11/F19/F21/F49.

### 4. Independent liquid-equity evidence and promotion package — 32–64h plus observation time

**Paste:** “Freeze a new liquid NSE stock hypothesis and its exact capital/risk/exit/cost/fill contract without changing production. Obtain point-in-time membership, corporate-action-aware prices and decision-time quality/news/participation evidence from official or appropriately licensed sources. Use untouched after-cost walk-forward and a separate forward funded ₹10,000 portfolio; preserve all rejected, untouched, losing and superseded versions. Compare confirmed-rebound and baseline only over matching registered intervals. Produce evidence, limitations and a versioned promotion proposal; do not overwrite the existing protocol, buy data or promote automatically.”

**Acceptance:** no survivorship/lookahead or overlapping-version independence claim; allocation is realistically fundable; metrics are reported only when estimable. Existing negative scenarios are not reused as an untouched holdout. Approval remains separate. F01/F22/F25/F28/F29/F30/F31.

### 5. Account-aware product journeys and actual performance — 32–56h

**Paste:** “Use the current API contracts to build reusable opportunity/research/order/protection/performance components in the existing SPA. Distinguish research, approved opportunity, actual personal paper and broker fills. Surface scoped daily/epoch equity/cash/open positions, sleeve/regime P&L, win rate and original-R coverage; unknown marks remain unavailable. Finish loading/error/empty/stale/rejected states, watchlist/review flows, risk/onboarding/help and truthful subscription capabilities. Test account/mode/epoch race handling and desktop/mobile accessibility; do not make an unapproved stock Buy executable.”

**Acceptance:** full browser journeys reconcile with actual ledger; unsupported/no-approval reasons are clear; no unsafe external HTML or clipped primary money/quantity/protection controls at 360/390/768/desktop. Billing receipts/refunds/renewal/expiry remain truthful and separately tested. F18/F37/F45/F46/F47/F48/F49.

### 6. Operational, security, legal and data-rights release package — 40–72h plus external lead time

**Paste:** “Build complete encrypted backup/restore and schema rollback rehearsals including all ledgers/epochs/outbox/protocol/config/permissions, with secret key escrow separate from encrypted state. Add dependency lock/SBOM/advisory review, revocation/CSRF/proxy/cross-tenant tests, incident delivery/acknowledgement, measured SLOs and load/chaos drills. Record qualified SEBI/exchange/broker commercial classification and source-specific commercial/redistribution rights. Keep unverifiable facts as explicit release blockers; do not purchase services, expose credentials or deploy.”

**Acceptance:** restore preserves uncertainty and cannot replay fills/orders; secrets are recoverable without disclosure; supported routes have real-reviewed permissions and audit records; no unresolved Blocker/Critical or unexplained reconciliation divergence. F16/F36/F37/F38/F39/F40/F41/F42/F43/F44/F50.

### 7. Route certification, burn-in and authorized release proposal — 24–48h plus 30 relevant sessions

**Paste:** “Certify each proposed broker/asset/model/account/product scope separately, Upstox cash first and Angel One second. Futures/options/multi-leg, BSE, currency/commodity and US routes remain disabled until their own margin/settlement/calendar/protection contracts pass. Run independent paper lifecycle burn-in with at least 30 relevant sessions, observed entries/exits/protection/recovery and no unexplained divergence. Produce a commit-specific release manifest and rollback plan. Request separate concrete authorization for deployment, model promotion, paid data and any limited live beta; place no real orders during this task.”

**Acceptance:** idle days do not substitute for observed lifecycle; one unfunded futures/options lot is refused; hedge failure cannot create naked exposure; all certifications refer to the exact build and supported scope. Recorded evidence checker is NO-GO until every required record is reviewed. No blanket all-assets or profit claim. F01/F08/F16/F21/F22/F24/F44.

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

**Unresolved external dependencies:** qualified commercial entity/service classification and any registration/empanelment; Upstox vendor/hosting/API/protection permissions and supported sandbox evidence; Angel One access/contract certification; point-in-time fundamentals/prices/news coverage and commercial redistribution licence; account-specific margin, sell authorization and derivative eligibility; secret escrow, on-call/incident recipient and complete recovery environment; genuine forward sessions and approved independent strategy results. Public access, an API token or transparent scoring does not satisfy these dependencies.

**Decisions already resolved:** Upstox first, Angel One second; official public sources plus licensed data as necessary; existing ₹10,000 epoch and forward protocol preserved; other routes are individually certified rather than assumed. No further broker-priority or capital question is needed.

## Evidence evaluator limitations

`scripts/check_release.py` checks the completeness/build identity of externally reviewed recorded evidence. It is initially scoped to NSE paper-session dates. A reviewer name and path are not cryptographic proof or a legal permission; authenticated evidence/attestations and each other market calendar require further work. The current `release-evidence.json` deliberately fails. Never populate it with synthetic claims to manufacture GO.
