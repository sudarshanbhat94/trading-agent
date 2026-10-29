# Data refresh repair — 28 October 2026

**TRADING BEHAVIOUR CHANGED through corrected data inputs and refresh timing.**
Selection predicates, risk limits, paper epochs, broker settings and the registered
forward experiment are unchanged. Data availability is not strategy validation.

## Reproduced defects and repairs

- Daily historical calls returned older bars while the job replayed over two
  million rows without advancing the completed session. The collector now uses
  Upstox's batched, source-dated OHLC V3 daily snapshot first. It accepts only
  finite, coherent OHLCV for the exact completed NSE session, after 15:45 IST;
  request time never becomes the source date. Historical fallback remains for
  missing bars and cold-start history. Authentication/quota failures defer the
  broader retry rather than repeating doomed requests.
- Automation publications were absent from the research-only hot quote watch,
  and the broad worker retained its startup universe indefinitely. Both journals
  now contribute hot watches; the broad universe refreshes every minute. One
  bounded missing-symbol retry retains successful first responses. Returned,
  fresh, missing and stale quotes remain distinct; source timestamps are intact.
  Market-hour inspection also reproduced a deployed 300-second broad cadence
  against the existing 120-second freshness gate. The service now uses 8-second
  hot and 60-second broad polling; the worker clamps slower legacy overrides to
  60 seconds. This leaves headroom for broker batches without weakening the
  freshness gate. Missing identities and stale source trades remain explicit.
- Financial filing pagination was treated as a fatal incomplete index, valid
  integrated XBRL exceeded the XML bound, and failed bank requests repeatedly
  consumed the first alphabetical slots. Bounded pagination, an 8 MiB safe XML
  bound, a disclosed CET1 tag alias, standalone snapshot selection, rotating
  requests and versioned cache invalidation repair collection. Older revisions
  cannot fill missing ratios in a newer snapshot. Capture times and source hashes
  are retained. Missing CAR, NBFC and insurance facts are still unavailable;
  bank entry policy is not promoted.

## Verification evidence and limits

A read-only OCI source replay for 27 October recovered 2,615 of 2,634 requested
identities in 27 requests, including 496 of 497 enabled Nifty 500 identities
(501 reference constituents). Four other constituents were unsupported or lacked
independently verified identity. The 19 missing responses are not counted as
fresh. Three sampled gaps had obsolete ISINs that Upstox explicitly rejected;
identity reconciliation must independently agree with NSE and preserve separate
price bases. No symbol substitution or automatic history concatenation is used.

HDFC and ICICI official standalone filings dated 30 September 2026, published
17 October, reproduce the repaired collection paths. Neither tested XBRL
contains a distinct usable capital-adequacy fact. This release does not claim
complete evidence for all financial names or profitability.

The quote worker persists bounded `quote_feed_coverage_IN_hot` and `_full`
receipts in the market database and minute samples under `var/quote_feed_coverage`.
Samples contain counts and small gap lists, not entire quote datasets. Only actual
market-hour polls are archived; expected closed-market timestamps are not feed
failures. Fourteen dated files per lane and a 2 MiB per-file cap bound retention.
These samples cannot prove continuous coverage between polls. A post-fix actual
session is required before claiming market-hour coverage has been verified.

Read the actual session evidence without running a collector or strategy:

```sh
.venv/bin/python scripts/data_coverage_report.py --date 2026-10-28 \
  --db /opt/opentrade/var/trading_agent.db
```

The exit status is nonzero for missing/partial verification. Archive files default
to the feed's database directory; `--archive-dir` accepts a copied private archive
for offline review. Unobserved session edges and gaps longer than 90 seconds are
reported as partial, even when all observed responses were fresh.

Official source contract:
[Upstox OHLC V3](https://upstox.com/developer/api-documentation/get-market-quote-ohlc-v3/).

## Acceptance

Local verification: 126 initial focused data/report checks and 39 cadence/coverage checks passed; the complete suite
ran 2,758 checks (2,625 passed, 133 skipped), with no failures or errors. All 108
function fixtures, isolated execution rehearsal and repository secret hygiene
passed. The rehearsal used disposable fixture books and submitted no real orders.

Deploy a pinned commit with writers quiesced and
authenticated encrypted backups of all five affected databases. Verify paper
state, protocol and protected configuration before resuming jobs. Inspect the
actual dated candle receipt, new financial captures and the next real session's
hot/full quote coverage; do not turn partial evidence into a green status.
