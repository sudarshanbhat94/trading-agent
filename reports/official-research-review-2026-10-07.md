# Official research-source milestone — 7 October 2026

**TRADING BEHAVIOUR CHANGED in research evidence selection; production entry rules are unchanged.** F73 closes exact research chronology/correction/conflict defects. Added dedicated official-source import/capture/replay, without model promotion, production cutover, book reset, protocol change, broker configuration or orders. This is progress on data task 2, not completion of the release programme.

## Delivered and checked

- `app/research_data.py`: padded SQL candidate search followed by exact aware effective/publication/observation cutoffs, correction ordering by observation rather than corrected effective date, equivalent-offset conflict checks. Exact parent `2c1a185955af00509d2ebd080a996226c6e0f8c4`: five failed assertions, zero errors, five test methods. Current regressions pass.
- `app/official_research.py`: reviewed licensed CM 54-field parser, source-pinned Nifty 100/delivery CSV capture, canonical dated research identities, immutable raw hashes/receipts/failure/exclusion predicates, exact-day delivery joins, complete membership/removal retention. Failed/conflicting newer source evidence cannot borrow older passing identity coverage. Existing trading or registered-experiment databases refuse before writes.
- `scripts/capture_official_research.py`: bounded fixed-host public requests, original-receipt replay and read-only coverage report. Paths/licence details/datasets do not appear in operational output. The job never imports discovery into execution snapshots or changes a production parameter. [Source contracts and commands](../docs/official-research-capture.md).
- 37 focused checks pass. Full suite: 2,537 run, 2,404 passed, 133 skipped, zero failures/errors, 20.237 seconds. Separate research/UI function runner: 102 passed. Skips are not certification. All newly added source fixtures are explicitly synthetic; no actual licensed master fixture is claimed.

## Source acceptance remains open

One bounded official constituent request and one completed-session delivery request timed out. The new delivery connector durably reports failure with master/membership coverage missing in its dedicated temporary evidence database. No dataset, empty-file clearance, historical date, source rights, successful receipt or execution rule was invented. A reviewed source-layout string is not authenticated licence proof.

The licensed master is a separately acquired NSE product, and the specification does not resolve epoch timezone; delivery metadata must independently pin it. The existing public MII CSV is not parsed as that format. Different/new layouts remain unsupported. Circuits, freeze quantity, complete sessions/restrictions/actions, verified filings, action-aware adjusted prices, dated news/benchmarks and commercial rights remain unfinished source work. [Official format specification](https://nsearchives.nseindia.com/web/sites/default/files/inline-files/NSE-Masters%20Data-v1.6.pdf).

## Full programme still outstanding

The [A–G roadmap](release-audit-2026-10-06/roadmap.md) remains authoritative: complete data connectors/operational coverage; actual fees/margin/settlement, native protection/recovery, adapter convergence and Angel/further asset routes; complete product/billing/accessibility/security/backup/SLO/permissions; independent after-cost stock/index validation, relevant observed sessions and a separately authorized verified release. Independent strategy results, operational correctness, deployment and commercial permission remain different gates. The registered experiment and all active books are unchanged.

Exact runtime commit `424d7b475cebefb83aeed94156cb6c9cd93deaf8` passed [CI](https://github.com/sudarshanbhat94/trading-agent/actions/runs/37609794425), including both pinned development/production Python versions. A separate constituent source probe through isolated staging also timed out. No actual source acceptance or production permission is inferred.
