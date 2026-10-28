# Automation lifecycle verification

This milestone changes **tests and read-only observation only**. It does not
change the deployed engine, risk parameters, paper epoch, existing publications,
registered forward protocol, broker settings or execution permissions.

## Reproduce the isolated proof

From the development checkout with test dependencies installed:

```sh
.venv/bin/python scripts/verify_paper_automation.py
.venv/bin/python -m unittest tests.test_automation_verification tests.test_paper_automation_observer tests.test_selective_paper_automation tests.test_paper_exchange -q
```

Each stop/target case uses a fresh temporary ₹10,000 house/owned paper book and
synthetic TEST evidence. Publication, observed zone touch, completed rebound and
later quote are processed by the actual stock assessor. `v2_live.sleeve_pass()`
uses the real engine, sizing and canonical execution gate to reserve an intent.
`service_personal_paper()` fills only a later executable event. The actual outbox
delivers the owned account request; it remains pending until its own later quote.

House `exit_monitor()` and owned `books.monitor_positions()` independently detect
the frozen stop/target. Neither trigger erases inventory or books a completed
sale. Missing, stale and wrong-instrument bids are refused, the database is reopened across entry
and exit reservations, and only a later valid bid completes each sale. Replaying
worker/outbox processing cannot duplicate delivery, fills or exits.

Assertions cover cash/equity, costs, frozen protection, one exit per book,
zero final inventory, no pending exits, exact minor-unit accounting and no engine
error logs. Broker order calls and network connections are forbidden. Sources,
clock, subscriber membership and notification/source adapters are fixture inputs;
signal predicates, allocation, orchestration and accounting are not replaced.
The main loop's infinite scheduler is not run; its real entry and fast-service
functions are exercised directly with controlled event timing.

29 October 2026 proof receipt, synthetic outcomes only:

- Stop: 17 shares, net −₹127.04; cash/equity ₹9,872.96.
- Target: 17 shares, net +₹214.31; cash/equity ₹10,214.31.
- Both: zero final positions, cash/inventory differences, duplicate fills,
  engine errors and real broker orders.

These deliberately constructed prices verify mechanics. They do not establish
strategy profitability, live broker fill quality, partial fills or commercial
readiness. The current paper exchange supports whole-order fills only.

## Observe production without causing a trade

```sh
.venv/bin/python scripts/observe_paper_automation.py \
  --paper-db /path/to/v2_paper.db \
  --journal-db /path/to/equity_automation.db \
  --market-db /path/to/trading_agent.db \
  --user-id 2 \
  --since 2026-10-28T00:00:00+00:00
```

The observer opens SQLite connections with `mode=ro`, queries bounded summaries
and does not execute refresh jobs, submit/cancel orders, initialize a ledger or
advance a protocol. Its output separates current/historical assessments,
confirmation observations, actual owned fills, pending/terminal reasons, sourced
protection, worker lease, quote coverage and cash/inventory reconciliation.
Overlapping publication versions are not independent stock observations.
Missing data is unknown; an untouched empty ledger is uninitialised. Stale
closing quotes/assessments outside market hours are expected.

The automation monitor must compare a small local checkpoint and advance it only
following successful reads. Alert only on a new qualifying signal, actual fill or
exit, changed refusal, missing market-hour coverage/worker, unprotected inventory,
or unexplained divergence. Never infer an engine failure merely because a
candidate did not become an order: contract, risk and entry checks still apply.
Any orderless eligible signal needs its dated decision and gate reasons examined.
Monitor scope is paper for the identified account; no live route is certified.

The first production observation (29 October, 00:41 IST) found a running worker,
₹10,000 cash/equity, no positions or orders, two historical active stock plans and
zero signal confirmations during the preceding 24 hours. The latest assessment
was 28 October at 15:27 IST. No cash/inventory divergence was observed, but the
empty ledger has no real production lifecycle to reconcile yet. A qualifying
production entry/fill/exit remains to be observed; none was forced.


## Scheduled observation

The dedicated **OpenStocks paper execution** thread monitor is active at 09:45,
10:45, 11:45, 12:45, 13:45, 14:45 and 15:45 Asia/Kolkata on weekdays. The existing
idea-performance monitor remains separate. This is periodic observation, not the
engine's fast stop/target protection cadence. It stays silent when unchanged and
never places orders. The private connection settings and first successful
checkpoint are ignored local files in `var/`; no host/key material is committed.
