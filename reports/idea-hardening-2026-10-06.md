# Idea validation hardening — October 6, 2026

**TRADING BEHAVIOUR CHANGED.** Quote ingestion is monotonic, rejects future/invalid marks and requires broker instrument identity. Future research plans reject unprofitable after-cost targets. Production sleeve rules and broker settings are unchanged; individual-stock execution is still unpromoted.

## What was wrong

- Older slow-lane quotes could overwrite newer hot-lane quotes. Feed-update timestamps were confused with last-trade timestamps, and absent timestamps were replaced with the local clock.
- Direct index names did not map to broker aliases. A partial quote response containing one different instrument could be assigned to the requested stock. The broker read-only endpoint confirmed the correct index identities and returned prices for both indices.
- The allocator tested T3 economics, allowing AUROPHARMA plans with negative net T1. Historical versions are preserved; future versions with any nonpositive target are rejected.
- Entry confirmation was only prose. Zone-touch scenarios were neither confirmed entries nor simultaneous funded portfolio results.
- Original stop events could omit the price held in state; gap counts lacked interval details and benchmark comparisons lacked matching direct observations.

## Implemented contract

New research model `conditional-pullback-v2` freezes precise completed-rebound predicates, source availability, next-session eligibility, news/regime/risk checks and target economics. Immutable assessment events record the evidence used. Corrected daily bars get new availability times; an earlier zone touch can never be reused as a confirmed fill. UI checks expose waiting/eligible states while the Buy action remains unpromoted.

The separate `idea-forward-portfolio-v1` experiment compares gated zone eligibility with rebound confirmation on **future publications only**, using the existing risk manager and ₹10,000. It enforces simultaneous cash, sleeve/position limits, whole shares, cost-inclusive stops, no overlapping stock positions and a later observed entry. Both legs reconcile to the frozen fee model. Stop gaps use actual observed prices. It does not alter the actual paper ledger or announce profitability.

## Evidence limits and remaining work

The inspected cohort has only two real NSE sessions, five hypothetical touches, one closed loser and no targets. Those previously inspected outcomes are development evidence and cannot validate the new hypothesis. Daily confirmation inputs use only bars actually observed by the evaluator; unavailable point-in-time fundamentals cannot be backfilled into a credible historical holdout.

The changes fix reproduced ingestion and measurement defects; they do not establish the specific cause of every historical five-minute gap. New regular-session observations must verify continuity. Fundamental reconciliation to dated company/exchange filings, sector-specific quality models, independently completed portfolio outcomes and stock/index execution promotion remain separate validation gates. Zero independent outcomes is not a positive track record.

See `docs/idea-tracking.md` for policy, report fields and commands. Original publications and the current paper epoch must remain intact through deployment.

## Verification

The broad suite passed 2,155 checks and 536 subtests, with 133 intentional skips. Eleven errors came solely from sandbox-denied loopback binding; the complete 13-test broker-wire module subsequently passed against its local mock server. Focused checks cover corrected instrument mapping, stale/future/older quotes, negative T1 economics, confirmation availability, immutable legacy history, user isolation, cash reconciliation and shared portfolio limits. An integration check verifies research assessments leave the paper database byte-for-byte unchanged. No real order endpoint was used.
