# Trading integrity changes

**TRADING BEHAVIOUR CHANGED.** These corrections change entry eligibility,
position sizing, exit timing and broker execution accounting. They do not
establish strategy profitability or make the complete system live-ready.

## What changed

- Live exits for the multi-sleeve strategies use successive observed prices,
  including on later holding days. A low from earlier in the day cannot cross
  a trailing stop raised afterwards. Moves between observations can still be
  missed; this is not a tick-replay fill model.
- Exit cash uses released cost basis plus the ledger's net P&L. Epoch filters
  normalize timezone offsets. Charts and daily baselines exclude prior epochs,
  and the API includes the realised P&L the card displays.
- Risk allocation caps remaining deployment across the whole pass, rejects
  non-finite valuations, stops at the daily loss boundary, and reserves current
  marked-to-stop exposure against remaining daily risk. Stops do not guarantee
  execution prices through gaps; this budget excludes additional exit fees.
- Schema upgrades preserve existing capital and accounting epochs. Restarting
  the engine no longer liquidates positions through the old resize migration.
  Personal resets preserve history and never reset the shared house book.
- Production equity entries require fresh timestamped quotes, a recent completed
  daily panel and verified index membership. Universe/quote filtering occurs
  before ranking. Rejected proposals cannot leak into actionable Ideas.
- Production quality momentum requires dated fundamentals. Early momentum
  requires completed-session delivery data. Missing inputs are not invented
  confirmation. Quality rebalance spacing is derived from recorded entries.
- Options intelligence reads the actual timestamped service output. Daily
  index bars aggregate open/high/low/close correctly. Unsupported instrument
  routes are rejected before funding; index analysis remains visible.
- Broker requests have durable intent tags and explicit pending/submitted/
  partial/filled/rejected/cancelled/unknown states. Exposure comes from verified
  fills, not acceptance. Uncertain requests block blind retries. Exit obligations
  survive removal of paper positions; protection applies to manual entries too.
- New automatic broker entries recognize the new equity sleeves and require
  Elite entitlement. Account owners retain status, disarm and disconnect after
  a downgrade. Shared index configuration is admin-only. Zero budget stays zero.
- Retired runner CLIs refuse to run. UI labels distinguish broker submission
  from fills and index analysis from historical options performance. OFF-regime
  text no longer claims momentum buys remain active.

## Reference data

Nifty membership refreshes from the public NSE constituent file into a dedicated
reference database. Observed snapshots are timestamped at retrieval and are not
retroactively used as historical membership. The CSV parser handles quoted commas.

Provider-sourced fundamentals can be loaded with:

```sh
.venv/bin/python scripts/import_sleeve_reference.py snapshot.json --db var/sleeve_reference.db
```

The script documents the JSON schema. Use actual availability timestamps, ROE in
percent, debt/equity as a ratio, and earnings-growth variability in percentage
points. Records must have a source. Future timestamps, malformed values and
incomplete constituent snapshots are rejected before writes. An importer is not
a supplied fundamental dataset; production quality selection needs coverage.

## Reproduce verification

```sh
OPENSTOCKS_DISABLE_ENGINE=1 .venv/bin/python -m pytest -q
.venv/bin/python scripts/daily_report.py --db /path/to/paper.db --epoch --today --no-quotes
```

`--no-quotes` values any open positions at entry and is not a live valuation.
The report opens its book read-only. Broker wire tests use a localhost mock with
fake credentials. Data-integrity tests execute the actual paper entry writer on
isolated books and assert ON/OFF gating, sizing and no real broker calls.

## Remaining release requirements

1. Reconcile historical accepted orders using broker-confirmed fills and current
   holdings. Old `sent` rows intentionally remain unresolved. The daily order
   endpoint cannot supply previous sessions' fills. Never infer a zero holding
   from missing daily results. [Upstox order-book documentation](https://upstox.com/developer/api-documentation/get-order-book/).
2. Reconcile account activity outside this application and validate independent
   live-account drawdown/day-risk limits. A shared paper decision is not a full
   risk model for an account with different fills and cash.
3. Handle cancellation of a partially filled pending buy before an urgent exit;
   current unresolved-order handling blocks conflicting submissions.
4. Supply point-in-time fundamentals, historical membership, corporate actions
   and suitable independent price data. Freeze parameters before chronological
   out-of-sample testing; report costs, slippage, benchmark and uncertainty.
5. Implement contract lots/margin, expiry/settlement and spread-leg recovery
   before enabling futures or option-spread execution. Those routes remain
   unavailable; the index card is analysis, not an active derivatives book.

No paper capital reset, strategy edge claim or live deployment is implied by
these checks. No broker acceptance is presented as a completed purchase.

## Session boundary and dashboard release

**TRADING BEHAVIOUR CHANGED:** `sleeve_pass()` rejects direct calls outside
the exchange session, in addition to the existing loop gate. Diagnostics must
not bypass the exchange calendar. No conviction thresholds were changed.

The dashboard exposes the current house-book halt reason from the unified risk
manager, with stale held-position quotes explicitly marked provisional. Broker
status reports unresolved orders separately from connection status. These are
specific gates, not a certification that the broker account is ready for live use.

Independent public daily-price attribution can be generated read-only:

```sh
.venv/bin/python scripts/audit_market_prices.py --book /path/to/paper.db \
  --since YYYY-MM-DD --out /private/path/to/audit
```

The output retains raw public responses, checksums, missing-data counts and
per-trade gross/net results. Daily-range checks allow 0.5% tolerance and cannot
validate intraday execution or establish out-of-sample profitability. Keep
reports containing account history outside the public repository.
