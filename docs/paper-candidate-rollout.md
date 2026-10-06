# Paper candidate preparation — 7 October 2026

**TRADING BEHAVIOUR CHANGED locally:** retain quote/daily-bar coverage for disabled held names; refuse expired daily reviews even when a prior rule for the same identity remains valid; exclude unclosed/future bars from this ingestion job. Strategy thresholds, production sleeves, the ₹10,000 paper epoch and registered experiment are unchanged. No production cutover or broker entry is authorized by this document.

## Verified repairs

- Fast quotes use all quote-only universe rows while the full selection lane remains enabled-only. The mapping refreshes each minute. Conflicting aliases/venues refuse; a missing inventory schema retains the other available exposures with a coverage warning. Broker residuals are grouped by contract alias. Removed unknown identities still require sourced resolution.
- Daily ingestion prioritizes house/personal/managed exposures, including disabled names. Coverage means a bar for the expected closed session, rather than any returned history or a future date. It retries the actual stale tail, uses the shared notified NSE holiday list and exits 2 for partial/failed/unsupported coverage. Useful partial history is retained. Exceptional-session and historical-provenance certification remain separate.
- Reviewed daily imports validate their own effective intervals/current payload; an expired new review cannot borrow earlier passing evidence for the same identity.
- `scripts/rehearse_paper_upgrade.py` migrates only private SQLite copies. Streaming fingerprints check every original paper column/row and existing selected account/security/configuration tables. Changed values, lost history, changed capital/epoch/protocol, corruption or unsafe destinations refuse. Copies remain beneath `RESTORE_DISABLED`.

## Actual OCI preparation

Production lacks a required encryption dependency and has older packages. A separate private temporary runtime installed the hash-pinned `requirements.lock`; production was not changed. Candidate schema migrations on actual database copies preserved all values in 18 original paper tables and the two existing selected account/configuration tables. Capital/cash/equity stayed ₹10,000, positions zero, epoch and forward protocol unchanged.

The actual FastAPI application booted in that isolated runtime with outbound HTTP blocked and workers disabled. Unauthenticated positions returned 401; copied paper balances stayed intact. The first verification used a nonexistent `n_positions` accessor; the corrected check used `books.positions()` and passed. No application repair is claimed for that harness error.

This is not a production browser check, a running production paper cycle, broker reconciliation, a coordinated backup or profitability evidence. Read-only checks found October 6 daily bars for 2,628 symbols including NIFTYBEES. The stored OFF decision was not forced ON. Official NSE report-page/archive requests timed out locally and from OCI; no missing rules were fabricated.

## Reproduce the migration check

Use the pinned runtime and existing reviewed source paths. The destination must be new/private/separate. No broker settings or credentials are arguments.

```sh
.venv/bin/python scripts/rehearse_paper_upgrade.py \
  --paper "$PAPER_DB" --accounts "$ACCOUNTS_DB" \
  --protocol "$FORWARD_PROTOCOL" --destination "$NEW_PRIVATE_REHEARSAL" \
  --user-id "$ACCOUNT_ID"
```

Exit 0 proves the stated copy/migration invariants; exit 1 refuses. JSON records the tables actually reviewed, zero real orders, `coordinated_backup=false`, `deployment_authorized=false` and `release_certified=false`. Market history outside the selected account tables is copied but not semantically certified. There is no apply/start/unguard mode.

## Concrete cutover scope

1. Pin reviewed commit/archive hashes, matching green CI and clean source; prepare a new pinned production runtime while retaining the old runtime.
2. Complete actual current normalized NSE cash identity/rule/action/session coverage and owned preflight. BOD discovery alone cannot pass. Preserve OFF/unknown/stale/unsupported refusals.
3. Inventory all actual writers/state/configuration, account/session storage, broker states and protection obligations. Complete encrypted backup/escrow and verified private restore with recorded writer quiescence. Existing managed exits must survive a cutover. Independent SQLite copies do not meet that requirement.
4. Obtain concrete authorization for the exact paper-scoped cutover. Take a coordinated backup, migrate additively and verify unchanged book/protocol identities before starting one engine. Broker entries/model promotion/paid purchases remain separate.
5. Check deployed build, authenticated owner balances, source/quote coverage, approved-plan order status, personal exits and desktop/mobile journeys. Run a production cycle without forcing a trade; report its actual decision.
6. On failure retain new-entry refusal and the appropriate exit path. Do not restore old databases over subsequently confirmed fills or retry unknown broker intents blindly.

**Release remains blocked:** complete current execution sources, independently approved individual-stock models/forward outcomes, actual native broker protection/accounting/recovery, relevant paper burn-in and applicable commercial permissions. This does not complete all release phases. [Official instrument-source scope](https://upstox.com/developer/api-documentation/instruments/), [NSE master publication](https://nsearchives.nseindia.com/content/circulars/MSD60315.pdf).
