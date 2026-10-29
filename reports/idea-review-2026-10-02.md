# Idea performance review — October 2, 2026 (NSE holiday)

NSE equities and F&O were closed for Gandhi Jayanti. Official calendar: https://www.nseindia.com/resources/exchange-communication-holidays . No October 2 price performance is inferred. This review reconciles outstanding October 1 observations with the earlier local checkpoint and archives holiday publications. No code, parameters, portfolio state, execution or broker settings changed.

## Evidence and coverage

Tracker read: 2026-10-02T13:14:40.529300+00:00; scheduled heartbeat: 2026-10-02T10:55:24.118Z. These are distinct timestamps; this is not a claim that the reads occurred at the scheduled time. Latest dated screen read generated 2026-10-02T14:02:01.237268+00:00, using October 1 completed prices. Tracker timer active, poll current, market_open=false. Missing fresh quotes outside a trading session are expected.

19 immutable publications cover 15 distinct stocks. Nine additional publications were issued October 2 at 14:38:01 IST, using October 1 prices; none has any post-publication regular-session observations. Four repeat stocks (APLAPOLLO, IPCALAB, AETHER and LAURUSLABS) must not inflate independent evidence; the other five are new stocks. Original plans remain archived, including the stopped APLAPOLLO plan.

The original ten ideas began at 09:27 IST October 1, first observed near 09:30. Six or seven recorded gaps per plan remain coverage limitations; intervening crossings cannot be ruled out. No gap was added during the holiday. APLAPOLLO's terminal state is frozen at its 13:57 stop; it is not an end-of-day price.

## October 1 original cohort

Five zone-touch scenarios, one stopped, no target touches. These are hypothetical first-zone-touch entries, not confirmed rebound entries or actual trades.

| Stock | Frozen/current scenario state | Move from first observation | Hypothetical net ₹ | Samples/gaps |
| --- | --- | --- | --- | --- |
| DLF | ZONE_TOUCHED | -0.26% | -76.17 | 637/7 |
| LALPATHLAB | WAITING | 0.44% | — | 656/6 |
| AETHER | ZONE_TOUCHED | -0.29% | -52.85 | 655/6 |
| INDHOTEL | WAITING | -1.23% | — | 637/7 |
| PHOENIXLTD | WAITING | -0.23% | — | 637/7 |
| IPCALAB | WAITING | 2.15% | — | 649/6 |
| APLAPOLLO | STOPPED | -4.46% through stop | -133.53 | 475/6 |
| LAURUSLABS | WAITING | 0.37% | — | 637/7 |
| JINDALSTEL | ZONE_TOUCHED | -1.76% | -89.71 | 637/7 |
| HBLENGINE | ZONE_TOUCHED | -0.95% | -51.39 | 655/6 |

APLAPOLLO touched its entry zone at 13:44:07 IST at ₹2,117.80 and stopped at 13:57:25 at the observed ₹2,068.10, below its planned ₹2,072.46 stop. Model outcome: −₹133.53, −1.0337R, after frozen delivery fees and 0.2% slippage each way. A later independently read quote is ₹2,111 at 15:26 IST October 1; it does not reverse the original stopped scenario.

For the five hypothetical alternatives, gross marks sum to ₹9.20, model net to ₹-403.65; assumed entry/exit friction is ₹412.85. These sums are diagnostic only, NOT a feasible portfolio return: ₹10,000 capital, max three simultaneous positions, confirmation, cash and aggregate risk limits were not simulated. The four unresolved scenarios all have negative after-cost marks, including three gross-positive marks. Existing fees are held fixed; no fee change is proposed.

One closed loser is insufficient to estimate win rate or validate the model. The API's 0% scenario win rate has denominator one. Unresolved, untouched, stopped and replaced versions are all retained.

## Selection, access and trends

Original cohort: Healthcare 3/10, Realty 2/10, Capital Goods 2/10; other sectors one each. Nine holiday publications include Healthcare 4/9 and Capital Goods 2/9, but overlap prevents treating them as a new independent sample. This is candidate concentration, not funded exposure.

Original trailing daily turnover spans approximately ₹29.8 crore–₹297.4 crore: no observed small-ticket liquidity bottleneck. Liquidity alone does not demonstrate selection skill.

IPCALAB rose 2.15% from its first observed quote but never reached its entry zone; LALPATHLAB (+0.44%) and LAURUSLABS (+0.37%) also remained untouched. INDHOTEL's observed minimum ₹712.90 stayed above its ₹712.17 entry ceiling; no fill is invented. The original entry ranges deliberately require pullbacks; upward moves without zone access are missed opportunities, not profitable trades.

October 1 completed breadth in the earlier saved screen was 21.7%, versus 26.1% in the prior September 30 screen. This supports market-wide weakness but does not identify stock-specific causes. No verified material company filing is used to explain this single-session cohort; causal news attribution remains unavailable.

## Benchmark limits

Saved Nifty and Bank Nifty series are option put-call-parity spot proxies, not official NSE spot closes. Approximate 09:30–15:15 bucket changes were −0.499% and −0.639%, respectively. Stock observations end at different exact times, and terminal APLAPOLLO ends at 13:57. Therefore no exact relative-strength performance or alpha claim is made using these unmatched endpoints. Obtain timestamp-matched official/index observations before a benchmark comparison.

## Review proposals; not approved or validated

The screen admits negative absolute 20-session performers with positive relative strength; however IPCALAB, with negative 20-session performance, was the strongest observed mover. A hard positive-return filter cannot be justified from these outcomes. Likewise, changing entry levels to capture today's missed moves would fit the discovery sample.

Candidate study version: conditional-pullback-confirmed-review-v2 (research only). Compare first-zone-touch scenarios with precisely specified completed-session rebound/volume confirmation on an untouched forward cohort, freezing rules before observation. Preserve all original outcomes. Simulate each arm separately under ₹10,000 capital, existing max-three-position and portfolio risk constraints, chronological quotes, fees/slippage, gaps and unsuccessful entries. Report access, adverse excursion, after-cost R and sector concentration. Current evidence lacks confirmed-entry timestamps and an independent multi-session cohort, so no comparison has been validated and no rule implementation is authorized.

Audit defect: STOPPED event.price is null because the terminal event call omits price; immutable state still contains exit_price ₹2,068.10. Record a proposed future metadata fix with a regression check that terminal timeline price equals observed state exit price. Do not rewrite existing publications/events or treat this as missing underlying price data. No production monitoring repair was attempted in this read-only heartbeat.

## Paper book and notification

Verified UID2 IN book remains capital ₹10,000, cash ₹10,000, positions zero, current-epoch realised P&L zero. Research scenarios are separate from this book.
No holiday-session performance notification is sent, in accordance with the heartbeat's quiet-on-NSE-closure instruction. Local checkpoint advanced after successful read-only review.
