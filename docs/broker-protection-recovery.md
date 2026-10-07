# Owned broker protection and accounting — 7 October 2026

**TRADING BEHAVIOUR CHANGED locally.** This candidate repairs protection/accounting safety; it does not authorize broker activation, deployment, model promotion or a change to strategy parameters. The production ₹10,000 epoch and registered experiment remain intact.

## Protection invariants

- Every child/status write after broker I/O rechecks the current worker lease and current owned obligation in the same transaction. A late worker cannot attach an exit or overwrite cancellation.
- A pending sell prevents native activation. Scheduled coverage must equal the remaining confirmed holding; an oversized stop is UNKNOWN, not ARMED. Valid repeated REST observations refresh coverage age and remain in the immutable event log.
- A recorded, separately authorized native policy and fresh broker reconciliation can reduce a known SCHEDULED stop. An immutable intent commits before transmission. Acceptance, timeout and restart all wait for exact later quantity evidence; there is no blind retry, quantity increase or OPEN-order modification.
- A child winning the reduction race keeps the sell reservation. An oversized active child is cancelled once; actual terminal evidence is required before another exit. Cancelled protection with remaining inventory blocks new exposure.
- A previously attached child cannot disappear, change identity or become a second child silently.

Upstox omits completed GTTs from its REST list. The separate authenticated portfolio collector retains exact owner-bound child observations through reconnect/restart. Unsequenced notifications never arm a stop or prove cancellation. A COMPLETED notification can associate the child; actual owned order/trade/inventory evidence is still required for fills and accounting. [GET GTT details](https://upstox.com/developer/api-documentation/get-gtt-order-details/), [portfolio stream](https://upstox.com/developer/api-documentation/get-portfolio-stream-feed/).

## Read-only collector

The execution database must already have schema v6. The collector uses existing private per-user credentials, verifies the broker profile, obtains a one-use Upstox socket URI and binds the stream to the account. It never logs/stores the signed URI or places/modifies/cancels an order. An already bound broker profile cannot be silently shared or reassigned; that requires ownership reconciliation. Expired/replaced/conflicting connections refuse ingestion and cannot report healthy.

```sh
.venv/bin/python scripts/broker_portfolio_feed.py \
  --database "$PAPER_DB" --once --duration 20
```

For a separately reviewed operational installation, `--loop` and `deploy/opentrade-portfolio-feed.service` provide a supervised collector. The service is packaged but has not been installed or enabled. Link/credential/risk settings are not arguments. Missing schema refuses accidental creation of a substitute empty book. [Socket authorization](https://upstox.com/developer/api-documentation/get-portfolio-stream-feed-authorize/), [scheduled GTT modification](https://upstox.com/developer/api-documentation/modify-gtt-order/).

## Accounting contract

- A sell may consume only its owned exact instrument/product inventory; another product cannot supply shares.
- Actual fill timestamps are sorted as aware datetimes at full precision, independently of receipt order. Opposite fills or priced/fee-bearing buy lots with an indistinguishable order produce unavailable P&L, rather than an invented ordering.
- A reversal cannot itself be reversed while the active-event accounting model only supports direct sourced reversals. Append independently sourced replacement evidence.
- Final fee evidence can supply `covered_quantity` to `broker_ledger.record_fee`. Net realised P&L is available only when each participating order has one active sourced final total covering its actual whole filled quantity, matching terminal journal evidence. Missing/ambiguous/reversed/incomplete fees leave net P&L unavailable. Entry fees are allocated once in paise to the closed units; the open portion retains its cost.
- These trade-date postings are not broker available margin, settlement cash, certified performance or proof of profitability. Automatic final-fee/settlement ingestion and broker-native operational certification remain open.

## Verification

Use the package-aware runner so the disposable test environment initializes before application configuration:

```sh
.venv/bin/python -m unittest discover -s tests -t . -q
.venv/bin/python scripts/run_function_tests.py
.venv/bin/python scripts/rehearse_execution.py
```

The actual local WebSocket fixture transfers frames, closes the connection, retains observations, refuses cross-account adoption and leaves the book unchanged. The isolated actual OCI app boot/migration preserves all checked original paper/account/configuration values, ₹10,000 cash/equity, epoch and protocol. Neither is a running production paper cycle or a real broker certification.
