# OpenStocks release-plan implementation status — 6 October 2026

**TRADING BEHAVIOUR CHANGED locally; not deployed.** This continues the plan from `3ca1a9d`. It does not mark the entire six-phase commercial programme complete. New broker entries require exact reviewed execution authorization; restored copies cannot execute; uncertain native stops reserve sell rights. No production capital/epoch, strategy threshold, model allowlist, broker setting, credential or registered forward protocol was changed. No real order or deployment occurred.

## Concrete engineering delivered

- Immutable owned approval → dated canonical cash-contract/session checks → serialized account risk → exact-quantity paper fill → persisted exit policy/provenance → close → balanced P&L. One fill per plan, stable retries, definitive refusals, epoch isolation and no research-to-manual fallback.
- Expanded Upstox modify/margin/GTT transport. Durable native-stop obligations and append-only state events handle timeouts, restart, absent status, partial fills, same-ticker re-entry and cancellation/trigger races. Only separately activated/account-authorized stops are submitted by the production service. Application-only stops remain explicitly labelled.
- Append-only journal intent/transmission/status observations deduplicate callbacks and retain normalized owned state. They are not an actual broker fee/settlement ledger. Worker takeover now fences late acknowledgement/cancellation/observation writes and preserves uncertainty for the next worker.
- Private build/account/route/model live authorization checked in the actual journal; connection/arming alone cannot permit new risk. Native activation requires its own scope. No fabricated authorization record was created.
- Encrypted declared-deployment backup/disabled-restore tools, separate escrow and ownership verification; execution guard on restored books. Single-DB backup no longer deletes live equity rows. Coverage declarations and size/operational limitations are explicit.
- Actual SPA account components show owned paper cash/equity/positions and daily/epoch sleeve/regime results with win-rate/R coverage, explicit currency/unknown marks, frozen paper-review dialog, and visible uncertain native protection/incidents. New routes are mapped to subscription features. Unsafe broker hint/redirect values are escaped.

See [operator documentation](../../docs/recovery-and-approved-paper.md), [current A–G audit](release-audit-2026-10-06/REPORT.md), [findings](release-audit-2026-10-06/findings.json) and [key diffs](release-audit-2026-10-06/key-diffs.patch).

## Evidence

Final unittest: **2,321 run; 2,188 passed; 133 skipped; zero failures/errors**. Separate research/UI function runner: **102 passed**. Focused fault/lifecycle/account/UI checks passed; both isolated paper rehearsals passed, with balanced cash and zero broker orders. Skipped tests are not passing certifications. Deprecation/resource warnings in older fixtures remain.

The first broad run was not green: 12 failures and one error. The new live authorization invalidated five old unapproved journal fixtures, and two paper routes lacked feature mappings. Fixtures now explicitly isolate approval; authorization refusal has independent tests. Source inspection assertions were also invalidated by source edits during that run. The final run used frozen source and passed; no failed run was masked.

The browser executed the actual new components with selected production API handlers and a disposable synthetic account. It verified ₹10,000 clean balances, frozen levels, a single paper fill, actual target-close/P&L/R, and 390px without horizontal overflow. The synthetic ₹124.55 outcome is **not investment performance**. This is not full production SPA/authentication/watchlist/billing/accessibility certification. [Mobile fixture screenshot](release-audit-2026-10-06/ui-evidence/approved-paper-mobile.png).

A read-only deployed check still found build `2b300b4e7de90157362f104bf3654de7cde6215e`, active service, personal paper budget/cash/equity ₹10,000, zero open positions, current-epoch realised ₹0. Epoch remained `2026-09-22T13:34:11.273061+00:00`. It is a dated database snapshot, not a browser check or ongoing assurance. Peak/drawdown keys were unavailable in that particular deployed report and are not invented. Local changes are not visible on production yet.

## Exact phase status

| Phase | Verified local work | Remaining engineering | External/operational acceptance |
|---|---|---|---|
| 0: safety/audit | Existing IDs refreshed; risk/ownership/session/ledger/legacy refusals tested; runtime authorization added | Complete semantic review of inventoried modules and residual cross-route/security boundaries | Qualified commercial/data classification; authorized deployed revalidation |
| 1: universe/paper | Immutable approved NSE cash-stock fixture traverses full personal paper lifecycle; dated rule/session refusals | Official daily rule/action/calendar ingestion; canonical historical quarantine; convergence of house/manual/broker adapters | Source coverage/licensing and real liquid-stock data acceptance |
| 2: execution | Durable journal, owned inventory reconciliation, reservations/fencing/outbox, native-stop recovery state machine, disabled restore | Automatic certified-fill protection activation/amendments; active-order/GTT-completion resolution; complete actual fill/fee/margin/settlement ledger; incident delivery/acknowledgement | Broker sandbox/native protection, chaos and production restore certification |
| 3: assets | Canonical asset identities and explicit unsupported route refusals | Angel One; certified BSE/US/futures/options/currency/commodity adapters; slicing, margin, multi-leg compensation and settlement | Per-route broker/exchange permissions, lot/capital and settlement certification |
| 4: product | Account report, approved paper review, owned protection warnings, loading/error/empty/currency states | Full production journeys; onboarding/risk/help, billing/refunds/renewals, complete responsive/accessibility acceptance | Browser/account/ledger reconciliation in deployed candidate |
| 5: operations | Secret encryption/revocation; private recovery tooling and recorded NO-GO evaluator | Dependency lock/SBOM/advisories, complete supervised backups, load/SLO/restore drills, privacy/on-call delivery | Actual escrow/off-host recovery, provider/legal/data rights |
| 6: release | Scope-specific authorization refuses incomplete evidence | Complete evidence collection, reviewed canary/rollback package | Untouched model validation, ≥30 relevant paper sessions, separate promotion/deployment/live authorization |

These remaining code tasks are not disguised as external dependencies. The project is **NO-GO for commercial automated multi-asset/live release**. Neither a passing suite nor a placeholder reference supplies strategy profitability, permissions, or market sessions. No asset/model was promoted merely to create trades.
