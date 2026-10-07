# Protection and accounting revalidation — 7 October 2026

**TRADING BEHAVIOUR CHANGED locally.** Parent `027bf2b6dc913bf194450704c21f364a3735715f`. Production remains `2b300b4e7de90157362f104bf3654de7cde6215e`; no deployment, real order, broker setting/credential change, portfolio reset or model promotion occurred.

## Concrete defects and repairs

| ID | Reproduced/current evidence | Repair |
|---|---|---|
| F60 | Lease takeover during GTT status I/O committed a SELL before the later fence failure. Fee/reconciliation/stop/exit writes also escaped the active fence. | Recheck fence before every relevant mutation; native child/status changes share a serialized transaction. Stop/exit helpers preserve caller transactions. |
| F61 | A confirmed partial owned exit left oversized coverage ARMED. | Separate original filled quantity from native coverage; mismatch blocks entries. Claim-once authorized reductions await exact status; an oversized raced child is cancelled once and remains reserved through ambiguity. |
| F62 | A submitted sell did not prevent native activation. | Refuse activation while an exact contract/product exit is pending. |
| F63 | Repeated valid ARMED polls did not update age, so protection became stale despite successful observations. | Refresh time and retain an immutable observation even when state is unchanged. |
| F64 | Delivery and intraday holdings could jointly satisfy a delivery sell. | Validate sell availability by exact owned instrument and product. Preserve the existing one-position-per-symbol entry limit. |
| F65 | Receipt order beat submillisecond execution ordering under SQLite Julian rounding. | Sort exact aware source times; tied opposite fills publish no invented P&L. |
| F66 | Reversing a reversal restored postings while the active-fill query still excluded the original fill. | Refuse nested reversal in this direct-reversal model. |
| F67 | A disappearing/replaced native child could re-arm the obligation or attach another SELL. | Preserve the existing child identity and refuse disappearing/conflicting evidence. |
| F68 | API advertised paper intraday implementation, although approved orders only accept delivery. | Advertise delivery-only paper orchestration and explicit intraday unsupported status. |

The first three pre-repair real-code tests failed. Four independent stale-write/product/FIFO/reversal tests also fail against a disposable exact `027bf2b` archive; repaired-source counterparts pass. Two stale-status/child-identity tests and the false capability test also fail against that parent. These are defect reproductions, not trading samples.

## Additional implementation

F08 now has an authenticated owner-bound read-only portfolio collector, immutable normalized observations, generation/lease/reconnect tracking and exact completed-child recovery. No public unauthenticated webhook was added. Notifications do not establish current stop protection, a fill or safe cancellation. Reduction and cancellation preserve unknown outcomes and cannot retry blindly.

F09 now calculates closed-unit net P&L only with exact terminal actual fills and complete sourced final fee totals. Missing/ambiguous fee coverage, indistinguishable value-sensitive execution ordering or oversold inventory leave P&L unavailable. Broker spendable margin/FX/settlement and automatic final-fee sourcing remain separate requirements.

## Verification and limits

- Final frozen source: **2,496 unittest checks run, 2,363 passed, 133 skipped, zero failures/errors**, 19.736 seconds. Research/UI functions: **102 passed**. All four staged-public-source secret checks passed.
- Actual localhost WebSocket frames: owner binding, retained events/gap, unchanged book, zero submissions. Focused source/protection/amendment/accounting/migration checks passed. Entire operational paper rehearsal: balanced ledger, cash difference zero, duplicates/ownership errors zero, no negative cash, zero broker orders; synthetic fixture profit is explicitly not alpha evidence.
- Exact OCI archive: `7776a04b7bf6e0d5c0f140ae8e945e9a6f0f6aabf902755e9e21278f115104b4`; 441 public source/config/test/input files verified. Schema-v6 migration passed on fresh private production copies; final exact-source FastAPI boot rechecks those migrated copies with workers/outbound HTTP blocked. All values in 18 original paper tables and the two existing selected account/configuration tables were preserved, including timestamps/history. Copied capital/cash/equity ₹10,000, zero positions; epoch and forward-protocol digest unchanged. This is not a coordinated backup or deployed browser/worker proof.
- The first bundle omitted required public universe CSV inputs and failed boot; the corrected complete bundle passed. No application repair is claimed for that packaging error. A discovery invocation omitting `-t .` was interrupted and is excluded from acceptance; the package-aware isolated CI command passed. An initial purported pre-repair invocation used the wrong working directory; only the corrected archive invocation is cited above.
- The actual authenticated broker stream, quantity modification, native child cancellation, final fee/settlement ingestion, cross-adapter operation and outages still require scoped broker/operational acceptance. Broker account binding in the collector is not presented as full certification of every credential/control path.

## Remaining programme

The A–G register remains authoritative. Additional-asset adapters/settlement/margin/expiry compensation, complete official point-in-time execution/research sources, independently validated individual-stock models, complete product/security/backup/restore acceptance, 30 relevant observed paper sessions and legal/broker/data permissions remain incomplete. No fixture, idle session, review string, passing suite or readiness evaluator substitutes for those requirements. Full commercial/live release remains NO-GO.
