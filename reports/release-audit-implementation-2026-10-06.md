# Audit refresh and accounting/research implementation — 6 October 2026

**TRADING BEHAVIOUR CHANGED: the durable broker-journal boundary now refuses unsupported market/product/side or malformed quantity before submission. No strategy parameters or production sleeve selection were changed. Local changes only; no deployment, broker settings, real orders, production reset or forward-protocol modification.**

## Concrete changes

- `paper_ledger.py` adds balanced integer currency postings, immutable identities and mutation refusal. Personal opening snapshots, entries and exits post in the same transaction as the book. Retry/rollback/reset isolation is tested. A first-use snapshot honestly labels legacy provenance; it does not invent historical fills. The existing personal book remains authoritative, and mismatches are surfaced. This does not implement live contract-note, margin or derivative settlement accounting.
- `books.py` stores original after-cost initial risk on new personal NSE positions and closed trades. A later trailing stop cannot rewrite the initial R denominator. Historical risk remains unknown.
- `personal_performance.py` separates actual daily/epoch personal fills by sleeve and entry regime, reporting win rate, average R and coverage. Stale open marks produce unavailable equity/P&L rather than an entry-price valuation presented as current.
- Rejected dated assessments append `ASSESSMENT_OBSERVED` alongside the existing immutable confirmation/baseline events. A readonly owner-checked API and Tracking UI show why a publication waited. Protocol identities, existing predicates and original publications remain intact.
- Upstox typed submit/status and journal guards refuse unsupported routes before any transmission. Absence from broker order evidence is UNKNOWN, not rejection. This is still an uncertified cash route; modify/native protection/Angel One and other assets remain missing.
- Health checks use exact quote age and reject future timestamps. This fixes a displayed 121-second stale quote passing a 120-second SLA; entry quote freshness was already checked independently.
- `check_release.py` evaluates reviewed recorded evidence, required gates, exact build, explicit scope, 30 distinct NSE pilot session dates and real lifecycle counts. Dirty source, missing evidence, idle-only sessions and known Critical/Blocker findings fail. This does not independently authenticate evidence, authorize orders or replace legal/broker/strategy certification. Other market calendars need separate evaluators.

## Migration and rollback

Additive schema creation runs through existing startup/`ensure_schema`: original-risk nullable columns, personal ledger tables, and immutable research triggers. No reset or history rewrite occurs. Pre-migration readonly APIs return unavailable, rather than mutating schema from a GET. Opening snapshots are created in the next book mutation transaction; a clean idle existing account can correctly report `uninitialised` without fabricated entries.

Before any separately authorised rollout, copy and restore-rehearse all databases, credentials with separately escrowed encryption key, configuration and frozen forward protocol. Run the prior encrypted-state/session migration checks too: old sessions require reauthentication. Reconcile cash/positions before and after. Never remove ledger events to force a balance; use reviewed correction postings. Full versioned migration/restore certification remains a release blocker.

## Commands

```sh
OPENSTOCKS_DISABLE_V2=1 .venv/bin/python -m unittest discover -s tests -t . -q
OPENSTOCKS_DISABLE_V2=1 .venv/bin/python scripts/run_function_tests.py
OPENSTOCKS_DISABLE_V2=1 .venv/bin/python scripts/rehearse_execution.py
.venv/bin/python scripts/audit_inventory.py
.venv/bin/python scripts/check_release.py --evidence reports/release-audit-2026-10-06/release-evidence.json
```

The final command should currently exit 2 / NO-GO. After review, real recorded evidence must name the exact proposed release commit. Do not fill the manifest with synthetic passing assertions.

Authenticated read-only endpoints: `/v2/api/paper-ledger`, `/v2/api/paper-performance?day=YYYY-MM-DD`, `/v2/api/idea-publications/{id}/assessments?limit=20`, and `/v2/api/instrument?instrument_id=...`. All are tenant-owned except the shared discovery catalogue. The latter is not execution permission.

The refreshed A–G assessment and complete current finding states are in `reports/release-audit-2026-10-06/REPORT.md`. Real native protection, the canonical migration/full ledger, all-adapter orchestration, qualified legal/data/broker evidence and independently validated stock strategies remain unfinished. This work does not establish profitability.
