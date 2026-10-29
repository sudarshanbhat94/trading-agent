# Executable-depth review — 7 October 2026

**TRADING BEHAVIOUR CHANGED locally.** F70–F72 repair discarded provider millisecond timestamps, rounded rapid-depth ordering and contradictory same-time liquidity. No strategy parameter, production allowlist, book epoch, forward protocol, credential or deployed service changed. The full release programme is incomplete.

## Reproduction and repair

The seven timestamp/storage regressions run against exact parent `de4fb8a5ee345a13e1e1f85f1d04a3b65e41031b` fail with four failed assertions and two millisecond-input errors. Current nine regressions pass, including provider → stored quote → actual pending paper order → ledger reconciliation and a two-worker race. Focused related suite: 29 checks, exit 0. The first integration fixture incorrectly put source time after the actual observation clock; correcting that fixture retained the production future rejection.

Provider v2 timestamps are explicit epoch milliseconds or aware ISO. They normalize to UTC; ambiguous units/floats/naive/future values remain unavailable. Exact aware comparisons under a serialized, parent-preserving transaction replace Julian-day ordering. Conflicting same-time snapshots append immutable evidence and remain unavailable until a strictly newer source snapshot. [Official provider schema](https://upstox.com/developer/api-documentation/get-full-market-quote/).

## Final evidence

- 2,511 unittest checks run: 2,378 passed, 133 skipped, zero failures/errors, 19.932 seconds, exit 0. Existing resource warnings remain; skipped checks are not passing certification.
- 102 research/UI function checks pass. Isolated whole paper/fault rehearsal reconciles cash, has zero duplicates/ownership errors/negative cash and zero broker orders. Its favourable synthetic prices are not profitability evidence.
- 443 exact runtime/config/test/input hashes match the isolated OCI candidate. Source archive SHA-256 `e66200cc1affc8f61c3093d48421a5d0a3caf6e40358ec2a49b99cf2ff017f62`.
- Fresh production-copy migrations and actual application boot pass on pinned CPython 3.12. Every checked original paper and selected account/configuration value, including timestamps/history, is preserved. Candidate capital/cash/equity ₹10,000, zero positions; unauthenticated positions API 401. Account-schema-v4 is additive. Workers/outbound I/O remain disabled.
- One bounded current-day official NSE master request from OCI timed out. No bytes or passing execution-rule receipt were fabricated. A static broker BOD discovery list still cannot supply the missing execution rules. [Official NSE master publication](https://nsearchives.nseindia.com/content/circulars/MSD60315.pdf).

[Selected exact-parent diff](release-audit-2026-10-06/quote-key-diffs.patch) applies cleanly. The [A–G register](release-audit-2026-10-06/REPORT.md) now has 72 findings: 38 locally repaired, 19 partial, 11 missing capabilities, four unverified requirements. Production connectors, independent stock evidence, additional routes, broker/net/settlement/recovery/product/operations acceptance and commercial permissions remain open. No deployment, live order, model promotion or full-ready claim is supplied by this review.
