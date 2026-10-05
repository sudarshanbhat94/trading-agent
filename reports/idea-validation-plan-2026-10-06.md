# OpenStocks idea validation and completion plan — October 6, 2026

**Verdict: these ideas are not validated buy calls.** The product has a research ranker, conditional price plans and a hypothetical zone-touch tracker. It lacks a precise confirmed-entry evaluator and evidence that the resulting portfolio makes money. I should not have described those parts as a finished trading solution.

This is a read-only audit and implementation plan. No strategy, portfolio, execution or broker setting changed.

## What was actually validated

Production snapshot: 2026-10-05T18:50:13.486607+00:00, equivalent to October 6 at approximately 00:20 IST, before the October 6 NSE session. The price outcomes below therefore end on October 5 or earlier.

- 29 immutable publications across 19 distinct stocks; only October 1 and October 5 have recorded trading-session samples. Fifteen stocks have observations; four newly introduced stocks have none yet.
- Five zone-touch scenarios; one closed at a stop, four open. No target touches and no recorded entry-confirmation events. Twenty-four publications are waiting.
- All 29 plans passed numeric ordering, positive whole-share quantity and frozen fingerprint checks. Frozen stop costs agreed with the published estimates within ₹0.02. Maximum individual estimated stop loss ₹149.38. This validates limited plan arithmetic, not forecasting, combined portfolio risk or execution.
- Paper account: ₹10,000 capital/cash, zero positions, current-epoch realised ₹0.
- Overnight refresh now has October 5 daily prices; breadth is dated October 5 at 25.3%. The previous October 1 data lag cleared. Historical plans remain frozen.

| Hypothetical plan | State | Current after-cost mark | Current R |
| --- | --- | --- | --- |
| DLF #10 | ZONE_TOUCHED | ₹-32.50 | -0.2395R |
| AETHER #8 | ZONE_TOUCHED | ₹-49.16 | -0.3375R |
| APLAPOLLO #4 | STOPPED | ₹-133.53 | -1.0337R |
| JINDALSTEL #2 | ZONE_TOUCHED | ₹-96.09 | -0.6665R |
| HBLENGINE #1 | ZONE_TOUCHED | ₹-73.62 | -0.5259R |

These are independent alternatives, not funded portfolio trades. Their P&L cannot be summed into a ₹10,000 account return. One closed loser is insufficient for a useful win-rate estimate; open scenarios are unresolved. The first model horizon is 40 sessions, while the tracker has only two observed sessions.

Stock-selection quality, executable entry quality and portfolio returns are three separate questions. None is established by business quality alone or by a rising stock that never entered its published zone.

## Issues, evidence and fixes in priority order

### 1. Entry confirmation is unfinished — critical

`app/screening/plans.py` publishes “completed-session rebound with volume confirmation” as text. It does not implement a numerical predicate, timestamp its satisfaction or create an approved entry. `tracking._advance()` assumes an immediate hypothetical purchase on the first observed zone touch.

**Fix:** specify exact price/volume/session and regime/news/risk predicates; emit separate ZONE_TOUCHED, CONFIRMATION_PASSED, ENTRY_ELIGIBLE and EXECUTED events with evidence timestamps. A completed daily-bar confirmation becomes knowable only after that bar closes; evaluate and price any later entry using subsequent executable observations. Never give the confirmation model the earlier zone-touch fill. Preserve the current first-touch scenarios as a separate baseline.

**Acceptance:** deterministic decisions from an evidence snapshot, no look-ahead, replayable rejection reasons and no “confirmed buy” claim before all predicates pass.

### 2. Data coverage does not support precise outcome claims — critical

October 5 has 300–304-second sample gaps in ten stocks around 15:15–15:20 IST. Historical gaps remain. Available index_bars are option-parity estimates with bucket timestamps, not exact official benchmark observation timestamps. The original STOPPED event omits its price even though state retains the exit price.

**Fix:** trace broker quote timestamps → feed writes → tracker captures to isolate the gap; report expected cadence, missed intervals and their effect on crossings. Capture timestamped official Nifty/Bank Nifty observations separately. Include observed price in future terminal events and keep original history intact. Distinguish screen-generation time from price/evidence dates and keep late daily-provider retries visible.

**Acceptance:** no unlabelled regular-session gaps, no invented fills or exact relative performance from unmatched windows, and complete future event timestamps/prices. A healthy timer alone is not sufficient.

### 3. At least one published first target is economically invalid — high

The allocator checks net opportunity at T3, while T1 is generated mechanically. Two published AUROPHARMA versions have a nonpositive first-target outcome; these are one stock, not independent evidence. The latest version loses ₹4.26 at T1 at the published size:

| Plan | Hypothetical net at T1 | Planned loss at stop |
| --- | --- | --- |
| AUROPHARMA #19 | ₹-5.27 | ₹118.86 |
| AUROPHARMA #27 | ₹-4.26 | ₹119.35 |

**Fix:** validate every displayed target's net outcome and reward/risk against the published quantity and frozen cost profile. Hide or reject a nonprofitable target/setup rather than label it a profit target. Determine the actual exit policy before testing: T1/T2 markers, partial exits and full T3 exits are different strategies.

**Acceptance:** displayed profit targets are positive after costs; net R is visible; historical targets are not rewritten. Fees are held fixed. A mathematical T3 reward does not estimate its hit probability.

### 4. Ranking is a hypothesis, not a calibrated predictor — high

Weights in `screen.py` are hand-set. Relative strength rewards falling less than Nifty; 6/10 newest names have negative absolute 20-session returns (BHEL, SONACOMS, CGPOWER, APLAPOLLO, IPCALAB, SCI). 5/10 have below-average completed-session volume (SONACOMS, CGPOWER, LAURUSLABS, VBL, IPCALAB). Volume above average is a scoring bonus, not the promised entry confirmation.

These are candidate weaknesses, not proof that those stocks will lose. IPCALAB was the strongest original observed mover despite a negative 20-session return, so a blanket positive-return filter fitted to these losers is unjustified.

**Fix:** evaluate ranking discrimination and entry rules separately against simple dated liquid-universe/sector baselines. Preserve current scoring as development v1. Pre-register a small number of changes with a new model version; test each on untouched dates/cohorts. Separate pullback and breakout setup hypotheses instead of applying one entry-price formula to every type.

**Acceptance:** after-cost out-of-sample improvement, stable across sectors/regimes, with uncertainty and rejected/untouched opportunities included. A score of 89 is not an 89% probability of success.

### 5. “Quality” evidence needs deeper verification — high

Fundamentals are explicitly secondary financial statements, not exchange-verified ratios. The ranker uses ROE, growth, margin, positive-earnings years, leverage and cash conversion, but it does not implement a complete five-year EPS-variability measure or point-in-time statement-publication history. Financial-sector stocks are conservatively excluded without bank-specific asset-quality/capital evidence.

Official [Nifty100 Quality 30 methodology](https://www.niftyindices.com/indices/equity/strategy-indices/nifty100-quality-30) uses quality factors; the [NSE equity-index methodology](https://nsearchives.nseindia.com/content/indices/Method_NIFTY_Equity_Indices.pdf) describes ROE, financial leverage and five-year EPS-growth variability. That is a defensible research reference, not proof of this app's trading edge.

**Fix:** reconcile shortlisted names to NSE/company annual and quarterly filings; store period, dissemination/availability date, provenance and corporate adjustments. Validate quality metrics by sector; financials need their own measures. Historical tests must use only facts available at each decision time.

**Acceptance:** reproducible sourced ratios, no current fundamentals retrofitted into past decisions, and explicit incomplete evidence.

### 6. The scenarios are not a portfolio validation — critical

Each plan is sized against the current account as an alternative. The tracker does not enforce simultaneous cash, daily risk, max positions, sleeve exposure or correlated-sector allocation across its hypothetical entries. Four overlapping stock versions in the earlier lists, and additional versions now, cannot count as independent successes.

**Fix:** add a separate chronological paper replay using the existing risk manager and ₹10,000 capital, including positions already open, entry confirmation, integer shares, fees/slippage, gaps and actual exit policy. Retain independent idea observations for selection diagnostics. Cluster statistical evaluation by stock and overlapping time interval.

**Acceptance:** cash/equity/realised reconciliation, no cash below zero, no risk-limit breaches, no impossible overlapping positions and separate stock-selection, scenario and actual-portfolio reports.

### 7. The product promise exceeds the evidence — high

There is no validated individual-stock execution model behind the cards. The current stock previews are non-actionable; the production entry path remains separate. Targets are risk multiples, not forecasts. News/risk cancellation wording is not represented by the price-only scenario tracker. New versions can be published after close but have no post-publication observations yet.

**Fix:** expose Research Watch → Entry Eligible → Paper Executed as distinct states; show stop, targets, net R, dated evidence, setup type, coverage and exact blockers. Track later adverse-news/regime invalidations separately without rewriting original outcomes. Only a promoted model's eligible entries may feed the paper loop. Index/F&O strategies require their own validation and contract/risk evidence.

**Acceptance:** the website explains why a stock is waiting, bought, rejected or cancelled; “today's ideas” does not imply ten immediate purchases or demonstrated profit.

## Implementation sequence and completion gates

1. **Make evidence trustworthy.** Diagnose the five-minute gaps, complete terminal-event metadata, capture timestamped benchmarks and expose provider-price lag. Verify against a captured regular-session trace and focused regression checks.
2. **Finish the executable idea contract.** Define one initial confirmed-pullback model, exact data availability, economic target checks, news/regime cancellation and actual exit policy. Freeze a versioned specification before using further outcomes to select its rules.
3. **Validate selection and portfolio separately.** Use a dated, adjusted liquid NSE universe with point-in-time membership and fundamentals. Treat previously inspected history as development data. Reserve a genuinely untouched interval or collect a new forward cohort; retain delisted/dropped/rejected names, missing-data exclusions and all losers. Replay with ₹10,000 and the existing risk manager. Compare after-cost return, drawdown, average R, hit/access rates, adverse excursion, sector concentration and matched Nifty/Bank Nifty intervals. Test worse execution assumptions without retuning.
4. **Forward-test and promote only the tested model.** Freeze rules, accumulate independent completed outcomes and inspect confidence intervals; two sessions and one loss are inadequate. Promotion requires positive after-cost expectancy/portfolio results on independent evidence, compliance with existing drawdown/daily-risk limits, and no unresolved accounting or execution defects. More activity alone is not acceptance.
5. **Complete product integration.** Wire approved entry events to the paper loop, reconcile orders/fills/stops and the website, then verify the end-to-end states in the browser. Keep original research outcomes and each model's track record intact.

Exact success thresholds and the holdout/cohort must be registered before evaluation. Do not claim statistical validation from a magic trade count or tune thresholds until the same holdout passes. Define profitability relative to realistic capital, risk and time horizon, rather than promise perfect trades.

## What remains unverified

No confirmed-entry labels, independent completed forward track record, precise matched benchmark series, point-in-time fundamental history or jointly allocated stock portfolio result is available. These observations cannot support a claim that the engine is profitable. The plan above makes those claims testable.

Frozen scoped audit inputs are saved locally in `var/idea_validation_2026-10-06.json`; the deployed publication ledger preserves original evidence and levels. This audit read all 29 UID2 publications and both available sample dates. It did not change production or the heartbeat checkpoint.
