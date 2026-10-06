# Current runtime trace — baseline b588ffd plus local audit patches

| Runtime boundary | Current path | Evidence and limit |
|---|---|---|
| Startup | main initializes required v2 schema/components; `v2_live.start_background` launches the paper loop unless explicitly disabled | Startup/import/schema failures fail closed in local safety commits. Test suite disables production background execution. |
| Production paper entry | `v2_live.loop` → `sleeve_pass` → `SleeveEngine` → `RiskManager` → `record_entry` | Only `index_directional` is production-approved; quality stock sleeve is observation-only. No new stock strategy approval this pass. |
| Durable account delivery | house entry/close transaction → execution_outbox → fenced drain → exact account paper receipt or managed live mirror | Entry events cannot resurrect a closed source/new epoch. Paper/live orchestration still needs convergence. |
| Personal manual entry | owner-guarded `api_buy` → books.buy → account risk/serialized transaction → receipt + position + ledger | Quantity exact-or-refused; research publication request is refused. It is a manual action, not an approved stock-model fill. |
| Personal exits | loop → books.monitor_positions independently; owner manual sell → books.sell | Scoped position origin, fresh marks and persisted exit policy; no symbol-based closing of unrelated holdings. |
| Managed live entry | manual/live mirror → capability key → actual reconciliation → account risk → durable journal intent → UpstoxPort.submit | NSE cash MARKET/DAY only; live_certified false. Current production ETF sleeve is absent from mirrored allowlist, so automatic production live entry is not wired/approved. |
| Managed live exit | exact journal-owned quantity + unresolved order/cancel handling → exit credentials → intent → supported transport | Entry disarm alone does not abandon exits. Expired credentials/native protection remain a material limitation. |
| Research observations | idea_tracker / confirmation refresh → immutable publication + observation/assessment events → private report/API/UI | Research eligibility and zone-touch scenarios are not actual fills. Registered experiment is preserved. |
| Performance | personal owned readonly endpoint → active epoch actual trades and fresh marks → grouped report; ledger readonly reconciliation | Unknown initial risk/marks remain unknown; house/broker/global fees/benchmark performance not complete. |
| Legacy controls | main legacy entry routes return 410; legacy lane lists are retired; LLM hard-disabled | Dormant Agent/PaperBroker/scoring code remains inventoried and needs removal or review before any future activation. |
| Ops | worker_fencing guards engine/book/journal/outbox writers; jobs_health → health summary; CI unit/function/rehearsal | SQLite coordination is not a distributed all-worker fence; no complete restore/load/SLO certification. |

Route inventory records decorator and local guard candidates, not proof of effective middleware permissions. `/v2` router-level `require_session` and privileged guards are separately covered by the auth-dependency tests; synthetic dependency overrides only verify owner scoping. Jobs inventory is launcher candidates; dynamic production schedule/permissions require deployed verification. Private production configs are not copied into the audit.

## Resumed implementation: actual new paths

`v2_web.api_approved_order()` → `approved_execution.submit()` → frozen owned plan + current epoch → dated `execution_contracts.order_contract()` → cash paper capability → existing serialized `books.buy()` / unified RiskManager → balanced postings and persisted plan/instrument/model IDs → independent `books.monitor_positions()` → owned close + original-R performance. This is an additive approved delivery-paper route; legacy house/manual/broker writers have not all converged on it. GET/POST feature mappings are enforced by `require_session` in production.

`order_journal.submit(BUY)` → account/route/model `live_release.authorized()` → actual broker readiness / unified risk / durable pending intent → immutable intent observation → adapter I/O → fenced transmission observation. `order_journal.reconcile()` → fenced atomic monotonic snapshot + immutable observation + exact-owned protection obligation. Snapshots are not actual fee/settlement postings.

`live_trade.service()` → normal journal reconciliation → submit only explicitly activated `required` native obligations → native status/child attachment → journal fill reconciliation → actual inventory reconciliation → managed application exits. `prepare_exit()` cancels a known transmitted GTT and waits for status/fills; untransmitted queued protection can be withdrawn safely. Uncertainty is not a fill, zero inventory or cancellation confirmation.

`recovery_bundle.restore()` → validated new private destination + `RESTORE_DISABLED`; `v2_live.start_background/loop`, house writes, personal buys/sells and broker journal refuse execution beneath it. No service startup/cutover is performed. Quiescence and complete real source declaration are external operational evidence, not supplied by individual SQLite snapshots.
