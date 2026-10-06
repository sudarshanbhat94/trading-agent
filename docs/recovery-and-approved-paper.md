# Recovery and approved-paper execution

**TRADING BEHAVIOUR CHANGED locally.** New broker entries require reviewed, build/account/route/model-specific release evidence. A connected or armed account does not bypass it. Strategy thresholds, enabled sleeves, capital and the registered experiment are unchanged. None of these changes has been deployed.

## Supported implementation

The new approved-plan route implements an owned NSE cash-equity/ETF delivery **paper** lifecycle. It freezes quantity, canonical identity, model version, evidence date, entry zone, stop and target before account risk is checked. An approved plan may fill once. A network retry uses the same request identity; a confirmed refusal needs a new request. A changed epoch invalidates the approval. No public endpoint promotes research or creates an approval automatically.

The trusted approval service uses `app.approved_execution.approve()`. A manual approval must be labelled manual and cannot masquerade as a research publication. A validated-model approval requires an independent validation reference and trusted review. References are attribution, not proof that validation succeeded; do not populate them with synthetic results to authorize production.

`GET /v2/api/approved-plans` returns only the caller's plans. `POST /v2/api/approved-orders` accepts only `plan_id` and `request_key`; quantity/levels cannot be overridden. Both require a server session and the appropriate subscription feature. Research eligibility cannot fall back to manual execution.

Before a fill, `app.execution_contracts.order_contract()` requires dated canonical binding, lot/tick/freeze/settlement/circuit rules, reviewed restrictions/actions and an explicitly sourced open session. Conflicting, future, stale or absent evidence refuses. Production exchange-rule ingestion is not implemented by inventing fixture values: it remains required. The paper fill also passes the existing shared account risk limits, fresh-quote/zone/regime checks, fee-aware sizing and balanced postings. Position and trade records retain plan/instrument/model provenance.

## Rehearsals

Run in a disposable process from the repository:

```sh
.venv/bin/python scripts/rehearse_approved_ui.py
.venv/bin/python scripts/rehearse_execution.py
.venv/bin/python scripts/rehearse_approved_ui.py --serve --port 8769
```

The last command serves a **synthetic, isolated** localhost fixture with selected real API handlers and new account UI components. It creates temporary books and overrides authentication for a fake account. No production files, broker credentials or broker order endpoint are used. The visible target-exit button is fixture-only. This proves lifecycle/UI invariants, not full production authentication/billing journeys, exchange liquidity, or a profitable model. Do not publish this fixture server.

Performance → Your paper shows current cash/equity/open positions and selected-day/current-epoch closed P&L by sleeve and entry regime. Unknown marks and original R are unavailable, not zero. INR/USD are explicit. Ideas separates approved paper review from research watches. Account execution health labels application-only protection and unknown/failed/triggered/cancellation states.

## Native protection

Upstox transport supports one SELL GTT for an already confirmed owned cash position. An operator must separately activate the obligation with `app.protection.request_native()` under current `native-protection` authorization and complete dated contract rules. Activation requires a terminal entry and exact remaining ownership; it cannot protect an unresolved partial entry or another account's position. `live_trade.service()` submits only explicitly activated obligations.

The worker durably claims submission before network I/O. Acceptance is UNKNOWN until the exact broker status matches. Missing GTT is UNKNOWN because the details endpoint omits completed triggers. A timeout/crash is never blindly retried. A scheduled trigger is not a fill. Native child orders attach to the existing owned journal before confirmed fills are applied.

An application exit first withdraws an untransmitted obligation or reserves cancellation of a transmitted stop. A cancel acknowledgement alone does not free the sell right. Cancellation/trigger races and partial fills retain the obligation until actual terminal evidence. Historical same-ticker entries cannot revive an old stop. Uncertain/failing protection blocks new risk and creates an owned incident. Worker generations fence claims, cancellations, acknowledgements and observations.

This implementation is **not native-stop certification**. Automatic activation for future certified model fills, broker sandbox/contract testing, active-order reconciliation, quantity amendment, sell authorization, outage escalation and completed-GTT resolution remain release work. Existing application-monitored positions must never be labelled broker protected. [Upstox GTT details](https://upstox.com/developer/api-documentation/get-gtt-order-details/), [best-effort GTT policy](https://upstox.com/files/terms-and-condition/conditional-orders.pdf).

## Private live-release record

`OPENSTOCKS_BUILD_COMMIT` is the full immutable build SHA. `OPENSTOCKS_LIVE_RELEASE_EVIDENCE` points to a private regular JSON file (no group/other permissions) reviewed for that build. It contains the existing release-gate evidence and an `execution_authorization` with `source_commit`, textual `approved_by`/`reference`, aware `issued_at`/`expires_at`, and exact scopes containing `account_id`, `broker`, `venue`, `segment`, `product`, `model`. The same scope must appear in the certified-scope list. Wrong/expired/ambiguous scope refuses. No passing record has been created.

This is a trusted operator record, not a cryptographic attestation. Legal/broker/data/model approvals and 30 relevant observed paper sessions must be real. Do not use the unit-test fixture's passing evidence as production permission. Owned exits retain their separate safeguards when entry authorization is absent.

## Recovery bundle

The old nightly single-database backup no longer prunes the live source's equity history. It still does not constitute a complete deployment backup. The new recovery CLI accepts an explicitly declared source manifest; all writers must first be externally quiesced and that evidence recorded. Individual SQLite snapshots do not form a cross-database transaction.

The private manifest contains a full `source_commit`, `quiescence_reference`, and `sources` mapping safe relative destination names to source `path`, `kind` and `roles`. Kinds: `sqlite`, `file`, `protocol`, `broker-state`. Required declared roles: `paper_book`, `accounts`, `market_data`, `idea_tracking`, `forward_protocol`, `runtime_config`, `permissions`, `instrument_catalogue`. One genuine database may cover several roles. Declared roles alone do not discover or prove complete deployment coverage. Include all real sources, including encrypted broker files where present. Do not include escrow keys. Broker state must already use account-bound encryption.

After preparing a private manifest and separately escrowed keys, an operator can run:

```sh
.venv/bin/python scripts/recovery_bundle.py create --manifest /secure/recovery-sources.json --key-file /secure/recovery.key --bundle /secure/deployment.enc
.venv/bin/python scripts/recovery_bundle.py restore --bundle /secure/deployment.enc --key-file /secure/recovery.key --broker-key-file /secure/broker-vault.key --destination /isolated/new-restore
```

Use reviewed local paths; no production command has been run. Restore verifies archive authentication, hashes, declared coverage, SQLite integrity, exact protocol bytes and broker credential ownership. It refuses traversal, links, duplicates, corruption and existing destinations. Output is private and contains `RESTORE_DISABLED` before files are published. The engine and book/journal mutations refuse execution beneath that sentinel. Restore never starts service, removes the sentinel, replaces production or automatically replays orders.

Current format limit is **512 MiB total plaintext** and 256 members. Encryption uses an in-memory archive: validate actual deployment size/memory and implement a reviewed streaming format before using it on larger data. Off-host retention, actual writer-quiescence, complete production restores, key rotation, ledger/inventory reconciliation and authorized cutover are separate requirements. A declared coverage flag or fixture restore is not a completed production disaster-recovery drill.
