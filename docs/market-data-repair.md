# Daily market data and financial evidence

**TRADING BEHAVIOUR CHANGED through corrected source inputs.** Entry thresholds,
regime rules, risk limits, the ₹10,000 paper epoch, broker settings and the
registered forward experiment are unchanged. Data repairs do not prove returns.

The daily job reconciles current Upstox identities with the independent NSE
security list. Changed ISIN histories and quotes are archived immutably before
rebuilding the new price basis. Held/pending exposure changes, incomplete exposure
inventory, symbol collisions, ambiguous aliases and unverified identities refuse
repair. New verified EQ members can enter data collection; special series cannot.
Quarantine also applies to screening even when older prices exist.

Daily history is fetched directly, with 420 calendar days of lookback. Existing
close/overnight retries remain; an 08:10 IST weekday retry catches overnight
master changes before opening. Actual target-session bars determine freshness.
A complete dated NSE report can explain absent non-held bars or explicit zero
volume, but never missing held prices, malformed volumes or a truncated report.
Missing bars, identity quarantine and excluded research names remain separate.
The service completion flag covers its verified ingestion scope only; it is not
proof of complete constituent, execution or commercial coverage.

Stale/malformed secondary statements fall back to official NSE XBRL. Symbol,
statement basis, whole-company context, units, duration and publication time are
checked; original hashes and source URLs are retained privately. Quarter and
15-month income are not annualized, and standalone/consolidated facts are never
spliced. The existing consecutive-year requirement still applies. Bank ratios
are collected separately; missing capital adequacy remains missing, and no
financial-sector entry model is enabled by these data changes. Delivery blanks
and unsupported BE-series evidence remain explicit exclusions.

Home → Data coverage & reasons shows the last daily receipt, source quarantine
and precise financial-feed errors. The read-only overview API includes the same
`market_data` receipt. Counts and check times do not assert current certification.

Run `python -m unittest discover -s tests -t .` in the disposable test runtime,
and `python scripts/run_function_tests.py` for standalone research/UI fixtures.
The isolated production-copy rehearsal rebuilt all 16 changed equity identities,
added five EQ constituents, preserved account rows, and kept old bases separate.
Official-source checks refreshed Nestlé FY2026 but found only two comparable
annual years; HDFC Bank GNPA/NNPA were available while CAR was not disclosed in
the collected filing. Neither incomplete record becomes an approved buy.
