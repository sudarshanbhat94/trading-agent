# OpenStocks release-plan implementation — refreshed 7 October 2026

**TRADING BEHAVIOUR CHANGED; local code only, not deployed. Entire commercial release programme remains incomplete/NO-GO.** Preserve ₹10,000 production epoch, existing model parameters/allowlist and registered experiment. No model promotion, production reset, real order, broker setting/credential change or paid purchase occurred.

## Code delivered in this continuation

- One sourced entry gate for house, personal and broker writers; canonical identity, dated rule/session/action/alias/lot/tick/freeze/circuit checks. Automatic stop rounding tightens to sourced ticks and is recorded; managed no-target sentinel remains valid. Approved orders, manual paper orders and discovery use the same configured catalogue.
- Legacy manual paper requests freeze immutable owner/epoch/request/plan/quantity/levels and use `approved_execution.submit`; concurrent retry fills once. Risk, cash and journal/ledger invariants remain real in new integration tests. Ledger failure rolls back approval, binding and holding together.
- Actual owned Upstox trade-ID ledger with balanced minor-unit postings, FIFO **gross** realized P&L, sourced fees/cash settlements/reversals. External pending broker orders block exposure. Final actual net P&L/margin are unavailable until complete settlement/assessment evidence exists.
- Reviewed normalized official evidence bundle ingestion, hash/rights/source checks, canonical alias/quarantine receipts, daily systemd templates. Templates are not installed; raw official exchange connectors/production rule bundles remain unfinished.
- Angel One owner-bound encrypted-session transport for submit/modify/cancel/status/current-day fills/inventory/holdings/funds/margin. Unknown outcomes are never blindly retried. Native protection unsupported; journal orchestration and live certification remain false.
- Reviewed native policy for new canonical terminal fills; partially filled entry remainder is durably cancelled once and must become terminal before exact owned fill protection activates. Application-only/unknown obligations block new entries. Disabled restored databases cannot cancel real orders. No passing native policy or broker order was created.
- Immutable owned incident acknowledgements; a click does not resolve exposure or clear a risk block. Matching-origin cookie mutation guard and ASGI client/scheme proxy handling.
- Manual subscription status, unique verified payment receipt, expiry and access grant are one serialized transaction. Historical parent crash reproduced with exact Git method; fault/concurrency/duplicate-reference tests pass. Owner receipt UI is explicitly not an automated gateway or tax invoice.
- Transactional versioned critical account/trading migrations with checksum/immutable receipts, structural checks and complete fault rollback. Complete auxiliary market migration work remains.
- Authenticated bounded-memory 1MiB-frame recovery v2 (up to 50GiB disk staging), role/schema/source-set mutation verification, separately escrowed keys, private execution-disabled restore. v1 compatibility retained. Actual complete/off-host backup jobs, quiescence coordination, key rotation and deployed restore drills remain.
- Immutable PIT membership/fundamentals/bars/actions/delivery/news/benchmark evidence with effective/published/observed times and corporate-action adjustment coverage. Storage is not a source connector or model proof.
- Exact hash-locked dependencies, CycloneDX SBOM, retained advisory evidence, AnyIO/Starlette security updates and current 39-package OSV scan with zero active known advisories. CI adds exact-lock installation and advisory evidence. License/SAST/independent/deployed security acceptance remains.
- Revised release gate requires exact reviewed per-session lifecycle evidence; idle days and an aggregate count cannot manufacture 30 relevant sessions. No passing release evidence was written.

## Verification

Final frozen-source verification is recorded in [verification.json](release-audit-2026-10-06/verification.json). 2,511 unittest checks: 2,378 passed, 133 skipped, zero failures/errors; separate research/UI runner 102 passed. Isolated actual-handler paper lifecycle rehearsals pass with balanced cash, no duplicates, no negative cash and zero broker orders. Linux CPython 3.12 exact hash/wheel dry-run passed; actual remote matrix results are separate evidence.

Selected production handlers and actual account components were exercised with disposable synthetic session/account/calendar/quotes at 390×844. The browser displayed ₹10,000 cash/equity/zero positions, reviewed 20-share plan, post-fee fill, target exit and owned sleeve/regime/R report without horizontal overflow. [Current screenshot](release-audit-2026-10-06/ui-evidence/paper-lifecycle-mobile-current.png). This is not full deployed authentication/billing/accessibility or strategy validation.

Read-only production snapshot dated 7 October retains build `2b300b4`, active service, user 2 capital/cash/equity ₹10,000, zero positions and unchanged epoch/protocol. No production repair claim is made.

## Exact phase ledger

| Phase | Delivered locally | Remaining engineering | Separate acceptance |
|---|---|---|---|
| 0 safety | 69 finding records; targeted risk/ownership/legacy/security/accounting regressions; F54 exact historical crash repaired | Full semantic review of every inventoried legacy module; residual route/security boundaries | Qualified legal/data classification; deployed revalidation |
| 1 universe/paper | All-writer canonical gate; normalized import/quarantine; immutable approved/manual stock-paper lifecycle | Official raw actions/calendars/execution-rule connectors and production rules; full historic mapping; live approved-plan convergence | Real source coverage/rights and funded liquid-stock acceptance |
| 2 execution | Owned actual fill postings; sourced assessment/settlement/reversal API; partial native recovery; acknowledgements | Automatic final fees/margin/FX/settlement sourcing, actual native/stream/amendment certification; consented incident escalation; shared adapters | Actual broker/sandbox protection, reconciliation/outage and recovery certification |
| 3 assets | Angel One transport/ownership/wire contract; unsupported route refusal | Angel journal/native integration; BSE/US/futures/options/FX/commodity; margin/freezes/multi-leg/expiry/physical settlement | Each route/product/account separately certified |
| 4 product | Account P&L, frozen review, receipts/incident/protection states and owner async guards | Full onboarding/risk/help/landing, automated billing/refunds/renewals; all production journeys and accessibility | Deployed browser/account/ledger acceptance |
| 5 operations | Atomic migrations; streaming disabled restore; locks/SBOM/known-advisory scan; origin/proxy/session encryption | Complete backup/quiescence/off-host/escrow/rotation jobs, load/SLO/chaos, SAST/license/privacy/on-call review | Actual production restore and legal/provider/data permissions |
| 6 release | Build/account/route/model evidence gate; independently reviewed relevant-session predicates | Evidence collection, exact CI candidate/canary/rollback package | Independent after-cost stock validation, 30 relevant paper sessions, separate deployment/promotion/live authorization |

Remaining engineering is not relabelled as external approval. Synthetic profitable fixtures are not independent strategy evidence. No consistently profitable stock engine or full commercial launch is claimed.

[A–G deliverables](release-audit-2026-10-06/REPORT.md) include the complete status register, disclosed readiness rubric, Mermaid current/target models, phased roadmap, actual diffs/critical pseudocode, seven remaining implementation tasks and launch checklist.

## Next-session recheck — 7 October

[Run commands, current blockers and evidence](../docs/next-session-readiness.md). Added official bounded BOD discovery, timezone-correct source dates, current-day effective reviewed-bundle checks, owned API/CLI/browser preflight and first-load import-deadlock repair. Production-copy upgrade preserved all existing values/history/epoch; production is unchanged. Discovery remains distinct from execution coverage. Complete NSE raw rule/action/session normalization, broker final accounting/certification, stock-model validation and remaining phases stay open. The full programme is not ready for tomorrow merely because this candidate passes tests.

## Durable next-event paper completion — 7 October

New NSE cash paper house/manual/approved/subscriber intents converge on risk reservations, immutable journal, later depth fills, owned later-bid exits and balanced actual accounting. Pending/cancelled states and capacity are explicit in the shared Orders UI. Protection runs before entry catalogue checks. Persistent login throttling, atomic all-session revocation and no-op startup setting preservation are tested. Actual copied-production startup/migration and synthetic mobile/cancellation journeys passed with zero real orders/production mutation. [Detailed scope and retained failures](paper-exchange-implementation-2026-10-07.md). This finishes that implementation milestone; it does not close partial fills, complete live adapters, source/model/asset/operations/commercial phases or relevant market-session acceptance.

Sourced-session follow-up closes two additional local defects, F58/F59. Paper fills require a dated open session for both quote and fill time; immutable current session evidence can protect through an entry-rule catalogue outage, but missing future session evidence never becomes a fabricated exit. Exact aware source ordering replaces rounded SQLite selection. Trading schema v5 and regression boundaries pass; full source coverage and production certification remain outstanding.

7 October additional recheck: F60–F68 are locally repaired; exact protection/accounting/source/capability scope, reproductions and current evidence are in [broker review](broker-protection-review-2026-10-07.md). Full programme remains incomplete/NO-GO. Production and registered strategy/epoch/protocol are unchanged.

F69 binds broker readiness to the owned financial/order snapshot and serializes its complete comparison/write path. A fill, commitment or closed round trip requires fresh broker evidence; changed inventory cannot reuse old funds permission. Six exact-parent regressions, 40 focused checks and the complete current-source suite passed. This is another locally repaired critical defect, not completion of the outstanding phases.

F70–F72 now preserve executable provider millisecond times, exact rapid-depth ordering and immutable same-time conflict refusals. Current provider-to-paper/ledger, two-worker and rollback regressions pass; a fresh production-copy account-schema-v4 migration and actual app startup preserve all checked original values. Discovery/source rights and independent stock-model promotion remain unresolved.

## Official research capture — 7 October

F73 repairs exact PIT cutoffs/correction/conflict ordering. Dedicated licensed-master/current Nifty 100/delivery raw archives, receipt replay and source coverage are implemented locally. Tests preserve dated identities, all original membership/removals and failed/unsupported evidence; book and experiment databases refuse writes. [Evidence and outstanding full programme](official-research-review-2026-10-07.md). Actual licensed-file acceptance, source availability, complete execution rules and remaining research connectors remain unfinished. Production parameters/books/protocol are unchanged; no source rights, strategy promotion or deployment approval is manufactured.
