# Selective Ideas: no daily quota

**TRADING BEHAVIOUR CHANGED:** new stock research publications use `conditional-pullback-v3` / `selective-ideas-v1`. Admission is stricter, with a maximum of three alternatives and one per sector. Zero qualifying ideas is a valid result; there is no requested daily count. This is an Ideas selection change, not approval to execute stock trades or a demonstrated profitable strategy.

## Admission and display

- Retain the existing completed-session liquidity floor (₹25 crore median daily turnover), controlled structure, positive six-month momentum, strength against the matched NIFTYBEES interval and positive sector strength.
- Require complete fundamentals: ROE ≥15%, debt/equity between zero and one, cash conversion ≥0.5, positive income and margin, nonnegative earnings growth, and every reported year profitable across at least three years. Unknown facts fail. Existing official-feed/news/earnings flags still reject candidates.
- Require completed volume ≥1.5 times the prior 20-session mean, with delivery above its 20-session average on the same completed session. Neither a high score nor a positive distant target compensates for a failed gate.
- Use the original pullback levels, stop and 2/3/4 price-risk target scenarios. Size through the existing owned-account allocator. T1 must remain positive after modeled costs; the full T3 exit scenario must cover the estimated cost-inclusive stop loss. Targets are not raised to get a candidate through this test.
- Rank only qualifying candidates; keep at most three and at most one from each sector. A fresh quote below the entry zone or stop prevents a new publication. Stale screens do not produce new plans. After-hours quote absence alone does not constitute a feed failure or prevent a completed-session research plan.
- Show the no-quota policy, the after-cost final-target scenario and honest empty states with rejection reasons in the shipped Ideas renderer. Existing watchlists and original tracking history remain queryable.

The financial-quality dimensions are consistent with the ROE, leverage and earnings-stability dimensions described by [Nifty Indices](https://www.niftyindices.com/indices/equity/strategy-indices/nifty100-quality-30). Our absolute thresholds and participation filters are new conservative hypotheses, not that index methodology, its performance, or an independently validated stock edge. Delivery is participation evidence, not proof of institutional buying. A 1R scenario is an economic admission floor, not an expectancy estimate.

## Tracking and account boundaries

V3 records its selection policy with the immutable plan. Unchanged refreshes preserve its first publication; changed model versions do not rewrite older publications. V3 research assessments retain the existing rebound / later-quote entry requirements and recheck current selection evidence. Missing or deteriorated selection facts block hypothetical entry eligibility.

The existing registered portfolio comparison still accepts **v2 only**. V3 does not enter that cohort or inherit its evidence, and the registered protocol is not overwritten. V3 requires its own prospective evaluation before any promotion. Original losses, untouched ideas and removed versions remain in tracking. There is no daily publication job or forced replenishment added here; a genuinely changed qualifying plan can still have a new immutable version.

The active ₹10,000 epoch, capital settings, sleeve production allowlist, stop/target mechanics, broker settings and live execution are unchanged. This review branch has not been deployed. Broader release requirements remain separate.

## Verification

Exact parent: `b45b53811064f23f8756ce2b2b46b2deb1cf9f99`. In synthetic, reproducible comparisons the parent returns ten instead of three, admits a weak-volume record, a weak-ROE record and a below-1R final reward, and returns two records from one sector. The new selector rejects those weaknesses and keeps one sector representative.

Focused tests: **82 passed, plus 21 passing subtests**. Complete clean unit run: **2,560 run, 2,427 passed, 133 skipped, zero failures/errors**. All discovered research/UI function fixtures: **103 passed**. These are correctness checks, not profitability evidence. An initial non-isolated unit run reused a local billing fixture and collided on a payment reference; the successful complete run uses disposable databases, disabled dotenv loading and blocked external socket connections.

Focused reproduction:

```sh
python -m pytest -q tests/test_selective_ideas.py tests/test_stock_plans.py tests/test_ideas_product_ui.py tests/test_idea_hardening.py tests/test_idea_validation.py tests/test_idea_tracking.py
```

The actual v2/v3 research-cycle fixture hashes the paper database before and after; it proves assessment events do not change the book. A mixed-version replay proves adding a v3 plan cannot alter the registered v2 portfolio result. The JavaScript fixtures execute the shipped renderer and controls, including no qualifying setups, rejection reasons, after-cost scenarios, watchlisting and disabled research order review.
