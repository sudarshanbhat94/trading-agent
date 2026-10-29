# Ideas financial-history boundary

**TRADING BEHAVIOUR CHANGED in research data acceptance.** F75 repairs manufactured or missing earnings stability before an Idea can qualify. Model/risk thresholds, levels, active ₹10,000 epoch, execution permissions and the registered forward protocol are unchanged. No deployment, purchase, credential change, portfolio reset or order occurred.

## Defect and implementation

The parent parser overwrote financial series when one metric was split across response blocks, silently chose conflicting values, and counted disconnected profitable years as a consistent annual history. A freshly cached `earnings_years=3` also passed Ideas admission without dated supporting amounts. The repair:

- Merges split blocks by metric and exact fiscal period. Identical duplicates do not inflate counts; conflicting amounts/currencies reject the response instead of letting response order choose.
- Requires valid ISO fiscal dates, identified currencies, finite nonboolean amounts and compatible annual/quarterly duration declarations. Future fiscal points cannot enter the current decision. Ratios cannot return nonfinite values.
- Retains the latest uninterrupted annual-income chain in one reporting currency, up to the original four-year limit. Growth variability uses this same chain. Missing years remain missing; excluded older periods are explicit.
- Records each retained period/amount/currency and `annual-statements-v2`. The fresh and cached evidence screen, selective Ideas admission and current research assessments check this contract, the declared counts/current income, fiscal availability and the existing 550-day annual age limit on the actual decision day; an earlier price session cannot extend its validity. Cached API reads flag obsolete records without changing their original stored payload. Counts without dates are insufficient.
- Wires the existing evidence refresh job to recollect incompatible legacy caches. An honestly normalized short history remains insufficient for Ideas but is not refetched on every cycle just to manufacture coverage. Existing bounded queue/traffic rules remain.

ROE still explicitly means annual earnings divided by ending equity; it was not silently changed to an average-equity measure. Ratios remain **secondary financial statements**, not verified NSE/company filing metrics. Historical source publication/corrections, audited standalone/consolidated scope, unit/restatement reconciliation, official filings, financial-sector asset quality and commercial rights remain open under F28. Stored histories/publications are not rewritten.

The [NSE integrated filing example](https://nsearchives.nseindia.com/corporate/ixbrl/INTEGRATED_FILING_INDAS_152465_23042026210154_iXBRL_WEB.html) distinguishes quarter/YTD periods, currency/rounding, scope and audited status. Those are requirements for a future official filing normalizer; a fiscal-end date is not a publication timestamp. No new official filing adapter or historical availability is claimed here.

## Evidence

Exact parent: `bea2fc629e80a54d72d8fc10affff9fd02ea55ed`. Synthetic parent comparisons reproduce five concrete failures: disconnected years counted as three instead of two; split blocks dropping history from three to one; a conflicting value accepted as 1,500% ROE instead of refused; an undated-count-only Idea admitted instead of rejected; and a cached API reporting RESEARCH instead of REVIEW REQUIRED for that unsupported history. These fixtures are not market observations or performance evidence.

**101 focused checks pass, plus 48 subtests; the revised 18-method boundary class also passes with 24 subtests.** Full clean unit run: **2,578 run, 2,445 passed, 133 skipped, no failures/errors**, 20.106 seconds. All research/UI function fixtures: **103 passed**. Complete local testing uses disposable databases with dotenv disabled and external sockets blocked. Source and statistical certification are separate.

One bounded official filing acquisition failed locally and from a separate read-only runtime location with timeout; one bounded existing secondary-feed schema acquisition returned an HTTP failure. No real source bytes or successful receipt were fabricated. The privately retained negative results do not create passing fundamentals. F28 remains partially repaired, and production/browser acceptance of this commit still requires separately authorized rollout.

Reproduce:

```sh
python -m pytest -q tests/test_financial_history_contract.py tests/test_evidence_screen.py tests/test_selective_ideas.py tests/test_stock_plans.py tests/test_ideas_product_ui.py tests/test_idea_hardening.py
```
