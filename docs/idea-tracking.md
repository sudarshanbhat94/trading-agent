# Forward idea tracking

Open Ideas → **Tracking**. Publication versions include every issued conditional stock plan, even if it later leaves the shortlist. Expanding a row shows original levels, quantity, first/latest observed prices, entry-zone and target/stop observations, and timestamps in IST. Older versions remain accessible with pagination; the summary counts the whole history.

Each Ideas or Screen API delivery registers the original plan once for that subscriber. A changed price date, entry/stop/target or quantity creates a new version; refreshing unchanged plans never rewrites their publication time, frozen levels or fee assumptions. Current-price observations are not plan revisions. The plan-generation model has a version identifier to distinguish future rule changes.

The independent `opentrade-idea-tracker.timer` reads the existing Upstox quote feed every 30 seconds. Active issued ideas are also included in the feed’s fast quote lane, so unheld stocks do not wait for the slow whole-universe refresh. It writes only `var/idea_tracking.db` and imports no broker, trading loop or application startup. Paper portfolios and strategy parameters are untouched. The web view refreshes its observations every minute while Ideas is open, without republishing plans.

## Interpretation

- **Waiting:** no quote was observed inside the original entry range. Gains before an entry touch are not counted as successful trade outcomes.
- **Entry zone touched:** an observed quote reached the range; this is only a hypothetical entry scenario. Other confirmations, regime, strategy approval, position limits and simultaneous portfolio allocation have not been satisfied or simulated.
- **Target observed / stop observed:** post-touch quotes reached the original levels. Targets 1 and 2 are observations, not partial profit-taking. The full-size scenario closes only on a stop, target 3, or its 40-session time exit. A gap beyond a stop uses the actual observed price, not an invented stop fill.
- **Invalidated before entry / expired untouched:** no trade P&L or win is assigned. Untouched plans expire after 40 regular NSE sessions; active scenarios need a fresh regular-session quote for a time exit.
- **Observed move:** percentage change from the first post-publication quote to the last captured quote, not a return on an assumed purchase.
- **Scenario net / R / win rate:** estimates after the fee schedule and 0.2% slippage per side frozen at publication. R divides net by the cost-inclusive stop loss from the hypothetical entry. Only closed, entry-touched scenarios enter the win-rate/average-R denominator. These statistics are separate from actual paper P&L and cannot establish a portfolio edge.

Only positive, finite, identified Upstox quotes no older than 120 seconds at capture, within NSE regular sessions and after publication are accepted. Duplicate and out-of-order ticks do not advance a plan. A gap exceeding two minutes of regular-session time is marked; overnight and exchange closures are excluded. Sampled quotes may miss crossings between observations. Daily highs/lows from before publication are never used to invent hits. Stale prices remain labelled historical, and missing coverage remains visible.

The initial October 1 capture starts at 09:27:07 IST. Earlier displayed plans had no immutable publication ledger; earlier price moves are **not** backfilled as tracked results. The temporary capture is imported idempotently, preserving its original timestamps.

## Run and inspect

From the checkout, with production environment variables loaded:

```sh
.venv/bin/python scripts/idea_tracker.py
.venv/bin/python scripts/idea_tracker.py --report-user 2 --limit 100 --offset 0
systemctl status opentrade-idea-tracker.timer
journalctl -u opentrade-idea-tracker.service --since today
```

`--report-user` is read-only, does not poll the feed or publish new plans, and returns JSON suitable for daily reviews. The authenticated `/v2/api/idea-tracking?limit=100&offset=0` endpoint always scopes records to the signed-in user, enforces the Ideas subscription gate, and disables shared caching. Summaries count all versions even when rows are paginated.

Set `IDEA_TRACKING_DB` to a dedicated tracker database if needed; the default is alongside `OPENSTOCKS_DB`. Known paper/market/evidence filenames and databases containing other tables are rejected before schema creation. Deploy the service and timer in `deploy/`, then enable the timer. A thread heartbeat reviews the report after market close and flags meaningful new outcomes or coverage failures; routine polling is performed by the server rather than an LLM.

## Forward confirmation and validation — v2

**TRADING BEHAVIOUR CHANGED:** market-data ingestion now rejects older/future quotes and requires instrument identity; future research plans reject any nonpositive after-cost target. Production sleeve parameters, the paper epoch and broker settings are unchanged. Individual-stock execution is still unpromoted.

New publications freeze `conditional-pullback-v2` and `confirmed-pullback-review-v1`. After an observed zone touch, confirmation requires a completed positive daily rebound above the prior close, a close in the top 40% of its range, and volume at least 1.5× the prior 20-session average. A candidate can become shadow-eligible only during the next NSE session, at a fresh quote in its original range, strictly after the confirmation became available, with a current supportive regime, official-news checks and account risk checks. These numerical rules are a preregistered hypothesis, not a demonstrated improvement. Legacy first-touch scenarios remain unchanged.

Tracking now displays each predicate and rejection reason. Eligibility is a **research event**, never an order or fill, and expires in the UI after 120 seconds without a new assessment. Original losing, untouched and removed publications remain accessible. New gaps have explicit start/end times; old gaps without detailed records are not reconstructed. Historical event prices missing from the event table may be displayed from their frozen state, without rewriting the event.

The hot quote request includes direct Nifty 50 and Bank Nifty instruments. Those observations go only to the research ledger, never the equity universe. Relative moves require two matched sampled endpoints, each within 30 seconds of the stock observation; skew is disclosed. Missing historical benchmarks remain unavailable. After-hours quote staleness is expected.

Register the independent experiment **before** its forward cohort, then generate a read-only comparison:

```sh
.venv/bin/python scripts/validate_ideas.py --user 2 --register
.venv/bin/python scripts/validate_ideas.py --user 2 > var/idea_forward_review.json
```

Registration is exclusive and refuses to overwrite an existing protocol. The protocol freezes its timestamp, settings, cost schedule and exit policy. The separate shadow portfolio starts at ₹10,000 cash and zero positions; it is not the user's actual portfolio. Only subsequent v2 publications enter the comparison. The baseline uses zone-only eligibility; the alternative adds completed-rebound confirmation. Both require the same fresh market/news/risk checks and use the existing allocator. A fill requires a later quote within 120 seconds and the original entry range. T1/T2 are markers; full positions close at the first observed stop/T3 crossing or 40-session time exit. Gaps use the observed price and remain flagged.

Example report fields: `confirmed` and `first_touch_baseline`, each with capital, cash, equity, open positions, realised P&L, fills, rejected decisions, win rate, average R, unique stocks and sampled drawdown. `profitability_established` is always false: this tool does not automatically declare or promote a profitable model. Overlapping versions share one stock position. Cash includes frozen charges on their actual entry/exit legs. A separate clean experiment does not account for later changes to the real user book; production promotion requires additional reconciliation and independent completed evidence.

Deployment must restart the application, feed and tracker together before creating the extra research metadata tables, because older tracker code refuses unfamiliar tables. Preserve the existing production feed cadence. Back up the research database; do not reset or restate the paper portfolio. Regular-session monitoring is required to verify continuity improvements; overnight service health alone does not prove gap-free data.
