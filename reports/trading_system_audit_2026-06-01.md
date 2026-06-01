# Trading System Audit - 2026-06-01

## Executive Verdict

The system is not doing the job it is supposed to do.

It is collecting a lot of data, but the decision pipeline is not turning that data into reliable, actionable trades. The main failure is not one single strategy. It is the full stack:

- The scanner promotes too few symbols into full strategy evaluation.
- The gates are over-constrained, overlapping, and sometimes logically wrong.
- The strategies are rule presets, not validated trading edges.
- There is no strong missed-move feedback loop proving what should have been bought.
- Paper execution is split between admin decisions, signal ideas, and user follows, so "BUY" does not clearly mean "trade placed."
- Accuracy cannot be trusted yet because the system does not maintain enough clean outcome evidence per strategy.

The blunt answer: the current strategies are mostly watchlist generators dressed up as trade strategies.

## Evidence Snapshot

Source: OCI SQLite DB on `/opt/opentrade/var/trading_agent.db`, service deployed at commit `cc56cf1`.

### Data Inventory

| Area | Evidence |
|---|---:|
| Enabled universe | 13,035 symbols |
| NSE enabled universe | 2,658 symbols |
| Upstox instrument coverage | 2,658 enabled symbols |
| Latest quotes | 13,037 rows |
| Quote sources | 8,744 `alpaca-iex-live`, 2,565 `upstox-live`, 1,635 `yahoo-delayed`, 93 `upstox-last-traded` |
| Candle rows | 6,014,874 rows |
| Upstox intraday coverage | 2,657 symbols, latest `2026-06-01T15:15:00+05:30` |
| Upstox daily coverage | 2,658 symbols, latest `2026-05-29` |
| Delivery data | 2,618 symbols, latest `2026-05-29` |
| Sentiment rows | 59,891 rows |
| Sentiment coverage in last 7 days | 2,003 symbols |
| Orders table | 0 rows |
| User idea follows | 119 rows |

The data volume is not the main problem. The interpretation and decision path are the problem.

## What Happened In Indian Market Today

Indian regular session window used: `2026-06-01T03:45Z` to `2026-06-01T10:00Z`.

| Result | Count |
|---|---:|
| BUY decisions | 5 |
| HOLD decisions | 2,335 |
| BUY rate | 0.21% |

BUY strategies:

| Strategy | BUY count |
|---|---:|
| aggressive_relative_strength_breakout | 3 |
| btst_buy_candidate | 1 |
| volume_price_accumulation | 1 |

Top HOLD strategies:

| Strategy | HOLD count |
|---|---:|
| no_actionable_strategy | 437 |
| volume_price_accumulation | 336 |
| vwap_reclaim_order_flow | 322 |
| live_intraday_momentum | 257 |
| darvas_box_breakout | 228 |
| minervini_trend_template | 184 |

Top blockers:

| Gate | Count | Unique symbols |
|---|---:|---:|
| fresh_market_data_gate | 2,129 | 199 |
| overall_quality_gate | 1,059 | 167 |
| system_rule_GRADE_VIOLATION | 1,026 | 124 |
| technical_score_gate | 983 | 109 |
| opportunity_scan_entry_window | 840 | 138 |
| entry_grade_gate | 744 | 103 |
| delivery_gate | 674 | 63 |
| system_rule_DELIVERY_CONFLICT | 625 | 63 |

Important: before the fix, 84 unique symbols had live quote/stale-intraday contradictions, and 21 symbols had that as the only blocker. That means some real opportunities were being held for the wrong reason.

## Latest Live Cycle After Diagnostics

Latest diagnostic cycle:

`10377 raw symbols -> 139 scanner selections -> 139 decisions -> 1 BUY -> 0 auto-follows`

Health flags:

- `scanner_shortlist_too_narrow`
- `live_quote_blocked_by_stale_intraday_only`
- `buy_decisions_not_followed`

This proves the problem still exists after the Indian fix, especially in the US/live-reference path. The same class of freshness logic issue is still showing up outside the original Indian stale-intraday bug.

## Accuracy

The honest answer: we do not yet have enough clean evidence to claim strategy accuracy.

Current available proxies:

### BUY Signal Ideas

| Metric | Value |
|---|---:|
| Total BUY signal ideas | 14 |
| Avg current return | +0.17% |
| Avg peak return | +1.69% |
| Avg worst return | -1.63% |
| Current winners | 5 of 14 |
| Stop-hit BUY ideas | 3 |
| Stop-hit avg return | -1.37% |

This sample is too small. It is not enough to say the system is accurate.

### BUY Decisions Using Latest Quote Proxy

| Metric | Value |
|---|---:|
| Total BUY decisions | 69 |
| Current winners by latest quote | 32 of 69 |
| Avg latest quote return | +3.16% |
| Min | -20.76% |
| Max | +114.99% |

This proxy is polluted by stale quotes, US/India market timing differences, and possible corporate-action/outlier effects. It is useful as a smoke test, not a real accuracy measurement.

### Paper Follows

| Status | Count | Avg return | Winners |
|---|---:|---:|---:|
| PAPER EXITED | 95 | +0.51% | 42 |
| PAPER ACTIVE | 22 | +1.25% | 16 |

This looks better, but it is user-follow performance, not central strategy-order performance. The `orders` table has 0 rows. So if the expected behavior is "system places paper trades centrally," it is not doing that.

## What Is Idiotic

1. Reporting that thousands of symbols are scanned while only a tiny shortlist reaches decisions.

   Today's Indian scan fetched 2,657 quotes, but full decisions were made on a much smaller repeated candidate set. Latest all-market cycle selected only 139 of 10,377 raw symbols, or 1.34%.

2. Treating stale cached candles as a hard no-buy even when the live quote is valid.

   This was fixed for the Indian live quote path, but a related freshness problem still appears in current diagnostics.

3. Multiple gates block the same weakness.

   Examples: `system_rule_GRADE_VIOLATION`, `entry_grade_gate`, `overall_quality_gate`, `risk_overrides`, and related phase gates can all punish the same underlying issue. This creates fake rigor and suppresses trades without better accuracy.

4. `no_actionable_strategy` still creates watch rows that sometimes move.

   That label should mean "not useful." Instead it appears in hundreds of rows and has positive average watch returns. This means classification is noisy.

5. Runtime says `cycle_timeout_seconds=120`, but observed cycles are 360 to 403 seconds.

   The scheduler expectation and actual runtime are inconsistent. A 180-second interval is meaningless if one cycle takes around 6 minutes.

6. The system has an `orders` table with 0 orders while user paper follows exist.

   That is confusing. A BUY decision, a signal idea, a paper follow, and an order are not the same thing, but the product experience blurs them.

7. The diagnostic briefly reported 600% auto-follow rate.

   That was a metric bug caused by 6 users following 1 BUY. It is fixed in `cc56cf1`; the correct denominator is eligible user-by-BUY opportunities.

8. LLM settings exist, but LLM is offline.

   The system sounds like it has analyst reasoning, but `llm_provider=offline` and `llm_decision_mode=offline`. These are deterministic rules.

## Why It Is Not Performing

1. The scanner is too narrow for the universe size.

   A 120 candidate limit across 13,000 symbols is not enough, especially with both Indian and US markets enabled. It should be per-market and per-setup, not one global bottleneck.

2. Strategies are not calibrated by real expectancy.

   The repo has `strategy_backtest_snapshot`, but the live system does not persist a per-strategy scorecard that disables weak strategies or promotes strong ones.

3. Gates are defensive, not opportunity-aware.

   The gate layer is good at finding reasons to say no. It is poor at saying "this setup has edge, size it correctly, and take the trade."

4. Freshness logic is still too coarse.

   Quote freshness, intraday candle freshness, daily history, delivery data, and delayed reference data need to be separate. They are still bleeding into one another.

5. It lacks a missed-move engine.

   `missed_move_reviews` has 0 rows. The system is not systematically asking: "What moved 3% to 8% today that we did not buy, and why?"

6. Capital and sizing are not aligned with Indian trading economics.

   The INR min useful paper trade is now `7500`, but paper capital and user cash pools can still produce tiny trades or skipped trades. For Indian live-like testing, the paper pool should be at least 75k to 100k per active strategy sleeve.

7. It is not measuring outcome by strategy properly.

   BUY ideas are only 14 samples. That is not a strategy performance base. WATCH rows have more data, and some WATCH strategies are outperforming BUY strategy samples.

8. It is mixing markets with different data quality.

   India uses Upstox live quote/candles. US uses Alpaca/Yahoo paths. A single scanner/gate interpretation across both markets creates false blockers.

## What Is Missing

1. Daily missed-move review.

   Required output:

   - top gainers not bought
   - top volume shockers not bought
   - high RS breakouts not bought
   - reason each was rejected
   - whether rejection was correct after 1 day, 3 days, and 5 days

2. Per-strategy expectancy table.

   Required fields:

   - strategy
   - market
   - setup
   - sample count
   - win rate
   - average win/loss
   - max adverse excursion
   - max favorable excursion
   - post-cost expectancy
   - decision: enable, watch-only, disable

3. Clean trade journal.

   Every BUY must produce one of:

   - central paper order placed
   - user paper follow placed
   - blocked by exact reason
   - signal-only by policy

4. Data freshness audit by source.

   Freshness should be separate for:

   - live quote
   - intraday candles
   - daily candles
   - delivery
   - sentiment/news
   - corporate actions
   - announcements
   - circuit/ASM/GSM

5. Corporate action and adjusted-price handling.

   The latest-quote return proxy has a +114.99% outlier. That should not be trusted until splits/corporate actions and stale quote issues are normalized.

6. Strategy kill-switches.

   If a strategy has negative expectancy or too few samples, it should not be allowed to generate BUY decisions.

## What Should Be Improved First

### P0 - Must Fix

1. Build missed-move review and populate `missed_move_reviews`.

   This is the fastest path to learning why the system misses trades.

2. Split scanner limits by market and setup.

   Example:

   - India: 200 full decisions per open cycle
   - US: 200 full decisions per open cycle
   - reserve slots for top gainers, volume shockers, BTST, breakout continuation, sector leaders, and high relative strength

3. Fix freshness gates everywhere.

   The Indian stale-intraday bug was fixed, but current diagnostics still show `live_quote_blocked_by_stale_intraday_only` in the latest cycle.

4. Make order conversion explicit.

   If admin execution is disabled, say "signal only." If user paper follows are the actual paper execution path, label them as such. Do not let users think central paper orders are happening when `orders=0`.

### P1 - Strategy Quality

5. Promote only strategies with evidence.

   Based on current watch performance, candidates worth validating first:

   - donchian_momentum_breakout
   - 52_week_high_volume_breakout
   - live_intraday_momentum
   - time_series_momentum_trend

   Strategies needing caution:

   - volume_price_accumulation
   - minervini_trend_template
   - breadth_aligned_leadership
   - volume_profile_value_area_breakout

6. Convert strategy presets into entry playbooks.

   A strategy should define:

   - exact entry trigger
   - invalidation
   - stop
   - target
   - late-chase rule
   - required liquidity
   - expected holding period

### P2 - Runtime and Product

7. Reduce cycle time.

   Latest cycles are around 360 to 403 seconds. Either increase the interval honestly or split scan workers by market.

8. Add decision-funnel UI.

   Show:

   `raw -> quoted -> tradeable -> selected -> decisions -> BUY -> followed/order -> active PnL`

9. Add per-user capital realism.

   Indian paper/live testing should not run on quantities that cannot beat charges.

## Bottom Line

The system is not failing because the market had no opportunities. It is failing because the pipeline is built like a defensive filter, not a trading engine.

It needs:

1. missed-move truth serum,
2. per-strategy expectancy,
3. cleaner freshness rules,
4. market-specific scanner budgets,
5. explicit paper/order conversion,
6. fewer overlapping gates.

Until those are done, calling the strategies "junk" is mostly fair. Some components may have signal, but the current system does not prove edge and does not convert opportunity into trades consistently.
