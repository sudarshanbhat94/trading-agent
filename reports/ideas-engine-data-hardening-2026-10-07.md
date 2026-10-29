# Ideas and engine data boundary review — 7 October 2026

**TRADING BEHAVIOUR CHANGED locally:** invalid, conflicting, expired, or future-dated evidence is refused more strictly. Strategy thresholds, sleeve promotion, paper epochs and the registered forward protocol are unchanged. Broker work is **Upstox only**, per the latest human correction. This is a targeted data/Ideas/engine milestone, not completion of the full release programme.

## What changed

- Screening evidence is immutable, exact replays are idempotent, corrections need new observed identities, and equal-time conflicting sources are unknown. Exact timezone-aware availability checks prevent SQLite timestamp rounding from exposing future evidence.
- Screen reads verify their recorded availability, handle corrupt payloads with a complete unavailable response, and recompute price/news/earnings/fundamental freshness. Old cached news cannot keep a passed review status.
- Price features refuse duplicate sessions, invalid OHLC ranges and unreviewed jumps within the full momentum window. Stock/sector comparisons use the same observed return endpoints as their benchmark. NIFTYBEES is explicitly identified as an ETF proxy, including in idea rationale.
- Engine/account quote validation refuses naive, future-dated and boolean-price inputs. Research ideas remain conditional and never become orders merely because the screen ranks them.

## Evidence and limits

Exact working parent: `a1042df84d1fbb020f0ed8cc24e7b6d4ffcc2f5b`. Fifteen new regression methods pass. The exact parent fails these new regressions (15 failed assertions and two errors, including subtests). Full unittest: **2,552 run, 2,419 passed, 133 skipped, zero failures/errors**. Research/UI function fixtures: **102 passed**. The initial sandbox run failed only on localhost fixture bindings; the affected wire groups passed outside that sandbox, followed by a complete passing run.

The isolated paper lifecycle rehearsal passed: no duplicate deliveries, ownership errors, negative cash or ledger divergence. Its trades and prices are synthetic; it does not establish an edge or certify actual broker execution. Existing order acknowledgement/unknown-outcome, owned reconciliation, protection and paper entry/exit tests remain passing. No real order, production deployment, reset, broker-setting change or model promotion occurred.

Current production measurements and candidate snapshots are retained privately, not included in this public report. Complete source rights, canonical execution rules, independent stock validation and operational broker acceptance remain open. Research ranking, confirmation eligibility, approved order intent and confirmed fill must continue to be assessed separately.

## Next priority

Continue the same data → immutable idea → engine eligibility → owned order → confirmed fill → outcome path. Validate actual source coverage first, then reproduce any missing publication/decision/order links and rejection causes. Preserve original losing, untouched and superseded ideas. Broader release work and the private margin draft are deferred behind this human priority; no additional broker implementation is in scope.

## Reviewed source hashes

- `app/screening/store.py`: `050fc4a9109000a6be59f5bf27c3cb3cc5a3e3ba9383fdd0dc57f5cbe2a1c0e0`
- `app/screening/screen.py`: `8965973187ae47d6f73b5686ec3cb827cdc904aa4d661ce472c5644f68a74937`
- `app/screening/plans.py`: `a774ec51652b67da7a1104c401d14449f398c8bc42a88121694e6a0bf8f38651`
- `app/sleeves/feeds.py`: `971782b23047a554abf041c8adfb856f41d54f4bb94e023aecc869786f49eb8f`
- `tests/test_screening_data_contract.py`: `a579c8575ae492c322ab9d19fb50d3a2e47761e902c4cc78c2a8a0058cd0e407`
