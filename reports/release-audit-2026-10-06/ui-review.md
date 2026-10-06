# Product UI review — current audit

The UI is assembled by `SPA_HTML` and `desk_ui.enhance`; it is not React. Current source was inventoried and targeted controls/renderers reviewed. This document distinguishes local implementation from deployed/browser acceptance. Production remains the earlier build.

| Surface | Current implementation / issue | Required reusable component and acceptance |
|---|---|---|
| Home / book summary | Personal account/epoch separation repaired locally. Research/house/account values still need complete full-page runtime reconciliation. | AccountModeHeader + BookSummary with loading/stale/unavailable states; cash/equity/P&L match owned ledger. |
| Ideas / shortlist | Conditional research plans expose entry zone/stop/T1–T3/quantity/after-cost risk, watchlist and non-executable review. Stocks remain unapproved; no profitability claim. | OpportunityCard + EvidenceDrawer + OrderReview distinguishing research/approved/intent/fill; explicit reason when unavailable. |
| Tracking | Original publications retained; new Decision history fetches immutable dated predicates and reasons. Hypothetical scenarios are visibly separate from actual fills. | AssessmentTimeline with owner/paging/loading/error/empty states and escaped evidence. One isolated desktop/390px component check passed; full journey pending. |
| Portfolio / positions | Original model/sleeve/regime/policy and account state are retained locally; exact owned protection/actual marks need complete browser certification. | PositionCard + ProtectionStatus; pending/unknown broker data cannot become zero holdings or “protected”. |
| Orders | Durable journal and execution-health APIs exist; remaining lifecycle adapter actions/certification absent. | OrderTimeline with stable intent/partial/unknown/cancel-requested/confirmed status; reject unsupported routes before action. |
| Performance | Actual personal daily/epoch sleeve/regime API and original-risk coverage now exist. Full report UI/broker settlement performance remain open. | PerformanceSummary with separate actual/hypothetical tabs; missing marks/R/benchmarks explicitly unavailable. |
| Broker onboarding | Upstox/Elite gate exists, local encrypted state/session revisions tested. No Angel One implementation or native stop certification. | BrokerReadiness checks account identity/token/sell permission/route/protection, keeps exit obligations after entry disarm. |
| Account / risk | Explicit ₹10,000 epoch and reset-copy repairs local; no production reset this pass. | EpochHistory + RiskProfile with exact owned state and preserved history; approved capital changes only. |
| Plans / billing | Manual UPI requests/admin decisions exist; renewals/refunds/receipt/webhook reconciliation incomplete. | PlanCapability + BillingStatus, loading/error/expired/rejected; no paid promises for unimplemented execution. |
| Admin / operations | Critical unknown-job health and durable incidents improved locally. Alert delivery/ack/escalation missing. | ExecutionHealth + IncidentQueue, account ownership, actionable protection/reconciliation status, no legacy restart controls. |
| Landing / help / privacy | Commercial scope/data/registration not approved; full disclosure/help/retention flows remain work. | Truthful ScopeMatrix, Support/Grievance/Privacy flows reviewed against actual approved service. |

Use one spacing/type/color system, semantic buttons and focus states, labelled dialogs, accessible errors, and account/mode/epoch-bound request generations. Verify no horizontal clipping at 360/390/768 and desktop. Never clear a failed data fetch into a false zero balance. WCAG AA and performance targets are acceptance criteria, not measured achievements here.

The [isolated browser component record](browser-component-check.md) is limited to Tracking. Full production authentication, approved buying/protection, subscriptions and accessible keyboard/mobile journeys remain open. Earlier browser restrictions are not treated as evidence of a passed flow; no alternate production route was used to bypass them.
