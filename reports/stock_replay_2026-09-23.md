# Stock sleeve promotion decision — 2026-09-23

The currently promoted stock rule has **no demonstrated net-profit edge** at
₹10,000. It is moved to observation-only paper research. This deliberately
changes trading behaviour: `quality_momentum` may display its screen, but
cannot open a paper position or publish a buy idea. The index sleeve is
unchanged. No live broker order is enabled by this decision.

## Frozen independent replay

`scripts/research_stock_replay.py` wrote its protocol before downloading
public daily candles. It used the current official Nifty 100 constituent
snapshot, 20 bp slippage on both entry and exit, the app's delivery cost
function, completed-close signals and next-open fills. The stock ticket was
capped at 30% of ₹10,000, with ₹150 initial stop risk and a ₹1,500 minimum.
The screen follows the current 6/12-month momentum, turnover, price, ATR
stop and time/trailing rules. Two predeclared variants were compared: entry
only under the strong index regime and entry whenever stock strength passed.

| Period | Rule | Net return | Max drawdown | Trades | Wins |
| --- | --- | ---: | ---: | ---: | ---: |
| 2021–23 development | Strong index regime | +4.19% | −4.47% | 4 | 2 |
| 2021–23 development | Stock strength regardless of index | +15.44% | −2.93% | 7 | 6 |
| 2024–Sep 2026 retrospective holdout | Strong index regime | **−1.66%** | −6.92% | 5 | 2 |
| 2024–Sep 2026 retrospective holdout | Stock strength regardless of index | **−9.92%** | −14.41% | 11 | 3 |

The stock-only OFF exception looked attractive in development and failed
after costs in the later window. The current factor intersection was also
checked separately using the same frozen rule: seven names had usable
split-free candles. Strong-index entry returned +0.64% in development and
**−3.95%** in the retrospective holdout (three trades). That small sample
cannot prove a persistent loss, but it cannot justify promotion either.

## Limits and next evidence needed

This is a *rejection* test, not a certification of any alternative. The
universe uses **current** Nifty 100 membership for past dates, which creates
survivorship bias. Thirty-three of 100 symbols were excluded, mainly for
post-2020 split events that the unadjusted candle replay cannot safely
normalize. The index-factor intersection also lacks historical membership
and historical fundamental snapshots. Daily OHLC cannot reproduce all
intraday stop sequencing or actual fills. The sample includes only a handful
of trades per variant.

Historical, point-in-time membership and corporate-action-adjusted prices
are needed before another stock rule is promoted. [NSE Indices describes the
quality/momentum methodology](https://www.niftyindices.com/indices/equity/strategy-indices/nifty500-multicap-momentum-quality50)
and [offers historical constituent data](https://www.niftyindices.com/offerings/data-subscription).
[Upstox's historical-candle V3 documentation](https://upstox.com/developer/api-documentation/v3/get-historical-candle-data/)
states that daily candles are available back to 2000, but the current broker
connection has no valid token, so that feed was not used here. Even with a
better dataset, an untouched holdout and forward paper results after costs
are required before any profit claim or live routing.
