# Next-session entry checks — 7 October 2026

**TRADING BEHAVIOUR CHANGED: accept correctly dated Indian masters at the UTC/IST boundary; new diagnostics do not authorize trades. Strategy rules, production sleeve allowlist, ₹10,000 epoch and registered forward protocol are unchanged. Full commercial/live release is NO-GO.**

## What was verified

Read-only deployed build remains `2b300b4`, with active app/feed/screening/tracking. The observed personal book has capital, cash and equity ₹10,000, zero positions and zero current-epoch realised P&L. Its epoch and registered-protocol digest are unchanged. Last quotes were at the prior NSE session close; this after-hours age is expected. The persisted decision remains OFF, based on the October 5 completed session.

A consistent private production-paper database copy was migrated with the candidate schema. Every pre-existing column value and historical row was preserved, as were the active epoch and ₹10,000 balances. This is a migration rehearsal, not a production deployment or a broker reconciliation drill.

The new fixed-host source refresh retrieved 74,802 official Upstox NSE records, including 2,685 cash contracts. Raw bytes were archived privately with SHA-256 and the actual October 6 source date from Last-Modified. Source counts are discovery coverage; they are **not** execution coverage. No tick-unit conversion, circuit limits, settlement, sessions, restrictions or action clearance was guessed. The separate `upstox-discovery` provider cannot replace reviewed execution snapshots. [Official source description](https://upstox.com/developer/api-documentation/instruments/).

An actual browser rehearsal of current components and production handlers displayed the clean book, selected-instrument preflight and an explicit unknown-contract refusal at 390px without horizontal overflow. It reproduced a concurrent first-load import deadlock; lazy package exports repair it without changing strategy parameters. Browser evidence uses a disposable synthetic account, session and quote feed, not deployed UI or alpha proof. [Mobile evidence](../reports/release-audit-2026-10-06/ui-evidence/readiness-mobile-2026-10-07.png).

## Daily source job

Configure **private** `OPENSTOCKS_CATALOGUE_DB` and `OPENSTOCKS_CONTRACT_BUNDLE` paths. A reviewed bundle must pin actual official archives, normalization version, source dates/rights classification, broker aliases, rules, action review and exchange sessions. Public availability is not commercial permission. The normalizer for the full raw NSE security/circuit/action/calendar sources is still an unresolved connector requirement; the BOD download is not a substitute.

```sh
.venv/bin/python scripts/refresh_contract_catalogue.py \
  --db /opt/opentrade/var/instrument_catalogue.db \
  --archive /opt/opentrade/var/contract_sources \
  --bundle /opt/opentrade/var/contracts/reviewed-today/bundle.json
```

Exit 0 means current-day reviewed effective rule/session coverage was imported; it does not certify orders. Exit 2 means discovery alone or no reviewed rules; exit 1 means download/source/validation failed. Failed reviewed-bundle validation rolls back its import. Downloads have bounded compressed/expanded size, one request, no redirect, no credentials, and immutable source receipts. The 08:30 IST service/timer template now calls this job; it has **not been installed or enabled on production**.

## Website/API/operator checks

Ideas shows an owned **Trading readiness** panel. Enter up to 20 NSE cash symbols. It distinguishes regime, contract coverage, sourced session, quote freshness, account risk, paper cash/inventory reconciliation and paper-worker freshness. Closed-session quotes are not labelled a feed failure. Approved-plan cards now say entry checks still apply.

`GET /v2/api/trading-readiness?symbols=RELIANCE,HDFCBANK` requires the existing session, never creates a book and sends `private, no-store`. Discovery, research-only sleeves and live certification are separate fields. Passing preflight is neither a strategy signal nor a reservation; the actual order path rechecks price, quantity, stop/target, current regime, cash and risk atomically.

```sh
.venv/bin/python scripts/check_paper_readiness.py \
  --paper /opt/opentrade/var/v2_paper.db \
  --market /opt/opentrade/var/trading_agent.db \
  --catalogue /opt/opentrade/var/instrument_catalogue.db \
  --user-id "$ACCOUNT_ID" --symbols RELIANCE,HDFCBANK,NIFTYBEES
```

Paths must already exist. Exit 0 means ready for further **paper** order checks; 2 means waiting/blocked; 1 means unavailable. This command never submits an order. Safety status remains accessible to the authenticated owner without a paid subscription.

## What still prevents tomorrow's full release

- Production has the older build and no reviewed execution catalogue/session tables; the new deployment and contract timer are absent.
- Complete normalized current NSE execution evidence remains unavailable. Two official NSE download probes timed out; no passing receipt or invented values were substituted.
- Automatic individual-stock promotion has no validated after-cost forward outcomes. Registered independent cohort: 10 publications, 9 stocks, zero funded shadow fills/completed outcomes in both variants, ₹10,000 shadow cash/equity. Earlier tracked zone-touch scenarios include two stopped outcomes, zero resolved wins; they are neither actual trades nor independent model validation.
- Final actual broker fee/net/margin/settlement integration, full native-protection/recovery certification, additional-asset routes and remaining product/operations work stay open in the A–G register. A connected broker token does not close these gaps.
- The required 30 relevant paper sessions, observed lifecycle/recovery events, independent strategy evidence and applicable legal/broker/data permissions cannot be created overnight. NSE lists October 7/8 as regular weekdays rather than notified 2026 holidays, but actual exceptional-session notices and sourced current sessions still apply. [Official cash-market holiday circular](https://nsearchives.nseindia.com/content/circulars/CMTR71775.pdf).

Do not merge/deploy to manufacture activity, force OFF to ON, promote an unvalidated model or reset the book. Prepare an exact reviewed deployment only after complete scoped evidence; real orders, promotion, paid purchases and deployment retain their separate authorisation requirements from the approved plan.
