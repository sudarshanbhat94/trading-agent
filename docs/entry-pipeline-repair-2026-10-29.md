# Entry pipeline repair — 29 October 2026

**TRADING BEHAVIOUR CHANGED:** completed index inputs now roll over with stocks at 16:00 IST. Paper workflow `selective-paper-v4-observe-first` creates new frozen observation contracts from qualified, fundable research before entry participation arrives. It does not change the final entry thresholds, original-zone/rebound/next-session policy, risk percentages, stop/target calculations or broker permissions. This remains an unvalidated paper trial, not a profitability claim.

## Actual causes

The deployed current screen dated October 29 had 354 liquid stocks and zero usable Nifty benchmark comparisons. `index_history.normalize` unconditionally discarded today's index candle, and `capture` only requested yesterday's historical endpoint. Stock prices advanced after 16:00 while both benchmarks stayed behind. That erased stock/sector relative strength and rejected every stock. Two read-only vendor probes returned HTTP 200 and the correctly dated October 29 daily candles for Nifty and Bank Nifty.

The independent earlier market-hour audit found just two automated stock publications and 13 distinct latest stock/day assessments in fourteen calendar days. Only one stock/day passed selection and one passed completed rebound; every latest stock/day failed the later-quote gate. These overlapping checks are not independent trade outcomes, and do not establish a return or expectancy. The engine also required entry-day setup/participation BEFORE creating a watchable plan, while confirmation required an already-published plan and an observed subsequent touch. The phase ordering starved prospective observations. Increasing money did not fix either cause.

## Repair and evidence

`completed_cutoff` is shared with the stock collector. Before 16:00, current-day index bars remain excluded. Afterwards, append the source-dated daily candle from the vendor's intraday endpoint to validated history. Cached history avoids repeated full downloads. Exact session, identity, OHLC, finite numbers, duplicate conflicts and known-at chronology stay mandatory. A missing current candle never becomes a renamed previous candle.

A private replay with captured production evidence removed all 354 benchmark failures, aligned both indices to October 29, and restored eight independently fundable research candidates. The existing sector/rank cap selected PERSISTENT. Zero stocks passed every strict entry predicate; this result is not an actual fill or advice to buy PERSISTENT.

Automation now constructs a NEW dated observation contract, retains its research-origin model identity, and binds it to the new paper workflow version and current epoch. Original research publications and old workflow records are unchanged. The strict entry shortlist retains priority; at most three new observations, one per sector, are admitted per cycle. Publication itself has no order authority. Current quality, controlled setup, volume/delivery, observed zone touch, completed rebound, next-session later quote, instrument contract, account risk and confirmed exchange fill remain required. No trading-cycle override or test order is used in production.

Local final suite: 2,813 run / 2,680 passed / 133 skipped. 135 function fixtures and isolated actual production-path stop/target lifecycle proofs passed. New tests reproduce the close mismatch, reject missing-today substitution and backdating, and follow a Rs 100,000 pre-participation watch through strict confirmation while proving it cannot create an early order. Independent future returns and the remaining original-zone timing hypothesis still require forward evidence.

Production acceptance uses exact-commit CI, immutable source captures/new screen output, unchanged paper balances/epoch/trade/order records, unchanged protected configuration/protocol and actual website API output. Private rollout receipts record deployment; fixtures and a private replay are not deployed correctness.

## Primary API references

[Upstox historical V3](https://upstox.com/developer/api-documentation/v3/get-historical-candle-data/) directs current-day retrieval to the intraday API. [Upstox daily intraday example](https://upstox.com/developer/api-documentation/example-code/historical-data/v3/intra-day-candle-data/) documents `days/1`. Neither endpoint permits treating a partial session as completed evidence.
