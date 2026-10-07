# Durable paper exchange — 7 October 2026

**TRADING BEHAVIOUR CHANGED locally; not deployed.** New NSE cash paper entries reserve account risk and buying power, then wait for a later executable exchange event. New simulated positions exit on a later bid. Existing production model allowlist, thresholds, ₹10,000 epoch, original history and registered forward protocol are preserved. This is an execution implementation, not evidence of profitable stock selection or broker certification.

## Order lifecycle

1. Freeze the owned manual approval, reviewed model plan, or existing allowlisted house decision. Resolve the effective canonical instrument, sourced rules and session. Serialize the account's pending commitments with its filled positions under the existing risk manager.
2. Record an immutable intent, reservation event and pending status. HTTP submission returns **202**, `qty=0`, `requested_qty`, `paper_recorded=false`. Recorded cash/equity/realised P&L do not change. Buying power and open-risk capacity include the reservation.
3. The production paper worker services that durable intent. Require a strictly later exchange timestamp, evidence no older than 30 seconds, correct canonical alias/symbol, valid source/content fingerprint and a best ask inside the frozen zone. Recheck epoch, regime, original house ownership where relevant, current contract and account risk.
4. Fill the whole approved quantity using at most 10% of the displayed best-ask quantity. Consumption is shared across all accounts for each snapshot and side. Persist the actual paper book, ledger, provenance, fill event and consumed capacity atomically. Restart/replay cannot create a second fill.
5. Stops, targets, time/regime invalidation and requested exits freeze an owned full-position sell. Until a strictly later fresh bid with sufficient unconsumed displayed quantity arrives, shares and exposure remain. A gap can fill below the stop; the trigger price is not a guaranteed execution price. Exit monitoring and servicing run before entry catalogue checks and remain available through OFF or a missing catalogue.
6. Actual exits post fees and P&L once, retaining original sleeve, entry regime, epoch, plan/model and initial-risk provenance. Pending, cancelled, expired and rejected orders remain queryable. Corrupt/unknown outcomes refuse new risk rather than becoming an apparent fill.

Upstox's full-quote response documents snapshot time, bid/ask depth and circuit bounds. Last-trade price and daily volume are used for valuation/research; neither is executable depth. This normalizer implements NSE cash only. [Official full-quote contract](https://upstox.com/developer/api-documentation/get-full-market-quote/).

## Production wiring and product surfaces

`v2_live.sleeve_pass()` queues eligible house decisions; it no longer immediately records those new fills. `v2_live.loop()` calls `service_personal_paper()`, which monitors protection first, services owned exits, then services house/personal entry intents. A confirmed house fill publishes the durable subscriber outbox; subscribers receive independently sized approved pending orders, not invented simultaneous fills. Retired strategy entry events cannot fan out to personal books or brokers.

`GET /v2/api/paper-orders` returns only the authenticated account's current-epoch journal, with explicit simulation scope and `partial_fills_supported=false`. `POST /v2/api/paper-orders/{order_id}/cancel` cancels an unfilled BUY atomically; a filled order cannot be cancelled retrospectively. These safety routes remain available after a subscription lapses. Cancellation of an owned protective SELL is deliberately unsupported.

Orders uses the shared `account_ui` component to display side, requested quantity, pending reason, sleeve, regime and frozen protection. BUY cancellation and loading/error/empty states are implemented. Late responses cannot overwrite another owner, market or newer request. The approval dialog distinguishes pending submission from confirmed fills. Your paper summary exposes reserved and available cash separately from ledger cash. Account performance includes only actual paper fills, by sleeve and entry regime.

## Authentication and migration

Account schema v3 adds shared SQLite login reservations/locks and executable quote storage. Reservations count password work already in flight across workers/restarts; successful login releases only its own reservation. Password, role or activation changes revoke every owned server session in the same transaction. Authenticated `POST /api/auth/logout-all` revokes only the caller's sessions. Existing origin/session protections still apply.

Trading schema v4 adds immutable paper intents/events, monotonic state and per-side depth consumption. Startup migrations are atomic. Production-copy rehearsal checks all pre-existing paper values/rows and selected account/security/configuration tables. Historical positions retain their old execution semantics; they are not silently converted to new exchange-managed fills. No production reset or migration was performed.

## Reproduce the software checks

```sh
OPENSTOCKS_DISABLE_ENGINE=1 OPENSTOCKS_DISABLE_V2=1 .venv/bin/python scripts/rehearse_execution.py
OPENSTOCKS_DISABLE_ENGINE=1 OPENSTOCKS_DISABLE_V2=1 .venv/bin/python scripts/rehearse_approved_ui.py --serve --port 8769
```

The first command exercises the actual journal, risk, house/subscriber outbox, later fills, restart, owned exits, reset isolation and balanced ledger on disposable synthetic files. The second serves selected real handlers/shared product components at localhost; review the plan, submit, inspect pending status, simulate later liquidity, then exit. The favourable synthetic price path does **not** validate a trading model. Neither command touches production or sends broker orders.

## Remaining acceptance

This adapter is whole-order, top-of-book NSE delivery paper simulation. It does not implement partial fills, queue priority, multi-depth VWAP, exchange auction fills, multi-leg compensation or other asset routes. Production requires actual current execution catalogue/session/action coverage and real quote depth; a discovery master alone is insufficient. Broker native protection/reconciliation, final actual fee/margin/settlement integration, additional routes, full deployed product/security/operations certification, independent stock-model evidence, relevant paper sessions and commercial permissions remain separate unfinished phases. The approved release plan reserves deployment and model promotion for concrete authorization; passing fixtures does not supply it.
