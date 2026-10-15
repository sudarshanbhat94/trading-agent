# Official research captures

**TRADING BEHAVIOUR CHANGED in research evidence selection.** Exact decision-time checks replace rounded SQLite time ordering. Production strategy rules, execution permissions, books and registered forward protocols are unchanged. This source job never imports into a trading/experiment database or promotes a model.

## Supported sources and limits

- **Licensed NSE CM `security.txt.gz`:** an explicitly reviewed 54-field layout from the version 1.6 specification. Receipt-pinned header version and epoch timezone are mandatory. Normal-market eligibility, active ordinary equity identity, rupee tick and settlement are normalized. The supplied freeze percentage is not converted to a guessed quantity. Unsupported/deleted/suspended/test/contingency securities retain their rejection predicates and raw evidence. This is a separately acquired licensed product, not the public MII CSV. [NSE product specification](https://nsearchives.nseindia.com/web/sites/default/files/inline-files/NSE-Masters%20Data-v1.6.pdf).
- **Current Nifty 100 constituent CSV:** exactly 100 distinct sourced EQ/ISIN identities; company/industry and full membership captured. Additions/removals become known at first observation. Current membership does not establish historical membership or a change's original effective date. [Official constituent source](https://www.niftyindices.com/IndexConstituent/ind_nifty100list.csv).
- **NSE completed-session delivery CSV:** date, quantities and percentage consistency checked. A symbol/series binds only to the licensed master from that report's date, observed by capture time. A current ticker is never used to infer an older identity. Missing, conflicting or subsequently failed source coverage produces zero unsupported facts and explicit unmatched counts. Unavailable delivery fields remain unavailable. [NSE reports catalogue](https://www.nseindia.com/all-reports).

All source bytes are stored privately with a SHA-256, original capture times and a receipt. Success receipt/facts commit atomically; failed parsing retains an immutable failure receipt and raw bytes. Repeat captures with the same source metadata are idempotent. Different source bytes at the same observation time remain conflicting until a newer capture. The dedicated database has versioned schema/immutable research, source and identity records. A database containing other application/experiment tables is refused before source writes.

Public fetching uses fixed official URLs, one bounded request per source, no redirects or automatic retries, size/expansion limits and sanitized errors. Source failure does not overwrite a previous successful capture or erase negative publications. Last successful evidence is not silently reported as current success after an outage.

## Run and replay

Use a dedicated private evidence database and archive, distinct from paper/account/market/screening/experiment databases:

```sh
.venv/bin/python scripts/capture_official_research.py \
  --db var/research_evidence.db --archive var/research_sources \
  import --feed master --input /private/source/security.txt.gz \
  --receipt /private/source/security-receipt.json

.venv/bin/python scripts/capture_official_research.py \
  --db var/research_evidence.db --archive var/research_sources \
  refresh --delivery-session YYYY-MM-DD \
  --rights-reference 'public observation only; commercial rights unverified'

.venv/bin/python scripts/capture_official_research.py \
  --db var/research_evidence.db report
```

The master receipt must contain `source` (the exact NSE specification URL identifying the licensed format), `source_day`, actual `published_at`/`observed_at`, raw `sha256`, `rights_reference`, `layout` (`nse-cm-security-1.6-54`), actual reviewed `header_version`, independently reviewed `epoch_timezone` (`UTC` or `Asia/Kolkata`), `review_reference` and the licensed acquisition `delivery_reference`. The specification does not resolve epoch timezone, so there is no guessed default. A review-reference string is provenance, not proof of commercial rights or certification. Unknown/new layouts need a separately reviewed normalizer.

For CSV replay, `import --feed nifty100|delivery` uses original raw bytes and a receipt with the matching fixed official source URL, report/capture day, original first observation, digest and rights classification. Do not manufacture historical observation times by fetching today's data. The original observation must be no later than import time; publication for an undated current file is conservatively first observation.

Refresh requires an explicit completed delivery-session date. An actual sourced exchange calendar must select that date; this command does not invent holiday/session rules. No scheduled production service/timer was installed. Report opens an existing database read-only and does not create missing files. CLI output contains bounded statuses/counts, not source paths, licence details or datasets. Exit 0 means the requested source was captured; 2 means partial/missing/stale/conflicting coverage; 1 means failure. None means execution or release permission.

## Remaining acceptance

Synthetic tests cover layout/units, source chronology, immutable receipts, duplicate identities, dated ownership, source outages/conflicts, original membership/removals, quantities, archive corruption/limits and protection of existing books/experiments. They do not certify an actual licensed feed. No current raw licensed master was available for independent source acceptance, and bounded public-source retrievals timed out. That is recorded as failed/missing coverage, not a fabricated passing fixture.

The remaining data milestone includes official execution circuits/freezes, complete calendars/restrictions/actions, verified filing fundamentals, action-adjusted historical prices, dated news/benchmark ingestion, source-specific rights, reviewed broker alias joins, operational daily coverage and independent source acceptance. Existing screening, production allowlists and independent forward experiment are not wired to these new research captures. That convergence requires validated evidence and a distinct model/release review; storage alone cannot close F01/F10/F22/F26–F30.
