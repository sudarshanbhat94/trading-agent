# Independent equity and index evidence screen

This is a research screen, not a new trading strategy. It does not change sleeve
parameters, regime gates, paper capital, ideas publication or order routing.
The Ideas page displays it separately from funded ideas, even when entries are
blocked. `/v2/api/screen` returns shared, authenticated evidence plus a personal conditional stock-plan preview.

## Data and ranking

- Universe: the dated official NSE Nifty 500 membership snapshot, with at least
  127 completed daily bars, price >= ₹50 and 20-session median turnover >= ₹25cr.
  Research does not discard a quality business merely because one share is too
  expensive for the current paper account. This pass does not size trades.
- Quality (40 points): ROE, earnings growth, profit margin, positive earnings
  history, debt/equity and operating-cash-flow conversion. Financial companies
  do not receive the industrial leverage/cash-flow points; missing capital
  adequacy and asset-quality verification remains an explicit review flag.
- Technical (30): price above its 50-session mean, positive 126-session return,
  and 20-session relative strength against actual Nifty 50 index history.
  Matched observation dates are required. Missing benchmark data earns no RS points.
- Participation (15): completed-session delivery above its preceding average
  and volume >=2x the **preceding** 20 sessions. Current-session delivery,
  sector mapping and FII/DII flows are fetched directly from official NSE
  reports, independently of lagging legacy ingestion. Delivery comparisons
  require all 20 prior observations within 45 calendar days. Bulk disclosures are shown as
  context and never interpreted as proof of institutional accumulation.
- Sector (15): sector-median 20-session return above the benchmark; requires
  at least three liquid peers and a real sector mapping.
- Valuation: annual EPS-based P/E and sector median, where comparable data
  exists. No fixed P/E cutoff or automatic 'cheap = buy' conclusion.
- Structure: prior 20-session support/resistance, ATR%, controlled breakout or
  pullback label. These are descriptive research hypotheses, not entry rules.
- News: official NSE announcement dissemination times and document links.
  Deterministic adverse/catalyst headline labels require filing review. Generic
  'Financial Results' announcements do not imply good results. Absence of a
  filing does not guarantee absence of adverse news. No LLM decisions.
- Results calendar: announced financial-results board meetings; unknown dates
  remain unknown. A meeting within two days is flagged. Latest notice wins.
- Indices: Nifty and Bank Nifty each show their completed-session ETF proxy,
  fresh nearest-unexpired options PCR/OI/max-pain context when obtainable,
  plus a clearly historical, timestamped chain when the market is closed, and
  dated India VIX, FII/DII and participant positioning. Cash index prices,
  futures execution and stock-level option coverage are not implied.

Numerical statements come from Yahoo Finance's public financial-statement
feed and are labelled **secondary, not exchange-verified ratios**. Ratios use
matching fiscal periods and consistent statement currencies; INR prices are
never divided by foreign-currency EPS. Annual growth requires consecutive
periods. Annual fiscal dates are displayed; stale/missing data earns no quality
points. Fresh quarterly year-on-year figures are included when available.

News risks and incomplete evidence are visible review flags. All rows are
`actionable=false`. The 0–100 score is a transparent research ranking, **not a
win probability or established after-cost edge**. Current data cannot establish
historical predictive performance; validation is explicitly `unvalidated`.

## Availability and operation

Each capture has a UTC `known_at`. An earlier evaluation cannot read a later
capture, even if its fiscal period/session predates that evaluation. Historical
local delivery/deal reports first captured now are not backdated as previously
available evidence. Future prices and filings are excluded. Inputs expire;
failed providers retain dated evidence instead of writing 'all clear'.

The job reads the market database in SQLite read-only mode and writes only a
dedicated screening database. It refuses a market/paper database as output,
even if renamed. It never imports the trading loop, broker or startup module.

```sh
.venv/bin/python scripts/evidence_screen.py
.venv/bin/python scripts/evidence_screen.py --fundamentals-limit 0
```

Paths can be supplied with `--market-db`, `--reference-db`, `--output-db`, or
`OPENSTOCKS_DB`, `SLEEVE_REFERENCE_DB`, `SCREENING_DB`. The supplied systemd
service/timer refreshes every 30 minutes, including when paper entries are
blocked. The screen uses completed daily prices; it is **not** an intraday
scanner. Evidence refreshes independently. Fundamentals rotate through up to
40 stale/missing liquid names per run with three bounded HTTP workers; successful
captures remain cached for seven days, then refresh. Failed names rotate so they
do not starve other names. No credentials or paid feeds are required.

The UI shows the last price session, capture timestamp, source, review flags,
filing links and stale status. Reports older than two hours are visibly stale.
The full ranked universe and index evidence remain available in the API;
the page shows up to ten conditional stock plans, with the wider evidence list expandable.

`screening.db` retains dated inputs and immutable screen snapshots for future
forward assessment and metric-ablation studies after actual data accumulates.
No retrospective or forward profit record is invented in this pass.

## Conditional stock plans on Ideas

`app/screening/plans.py` turns a completed-session screen into explicit **planning
scenarios**, without publishing funded ideas or changing strategy configuration.
The top ten alternatives exclude review flags and require positive stock/sector
relative strength, positive six-month momentum and price above its 50-session mean.
If fewer names fit the evidence and account limits, the page shows fewer.

For completed close C and 14-session ATR A: entry range is C−1.25A to C−A,
stop is C−2A, and T1/T2/T3 are upper entry +2R/+3R/+4R, where R is upper
entry minus stop. These targets are price-risk scenarios, **not forecasts,
validated signals, or the managed production sleeve's exit rules**. Quantities
use the existing unified allocator and the viewer's current paper epoch, cash,
positions, loss allowance and drawdown. Missing held quotes/stops block sizing.
Fees and 0.2% slippage on both sides are included in estimated stop loss and
target proceeds. Net reward/loss is shown separately from price R. A gap can
lose more than the estimate. Each quantity is one alternative; the ten cannot
be combined without a new portfolio-wide allocation.

Cards show entry, stop, targets, quantity, reason, confirmation needed,
invalidation, horizon and dated evidence. A fresh quote in the zone does not
become BUY NOW: rebound/volume confirmation and strategy validation remain
required. Quotes older than two minutes cannot imply current entry readiness.
Stale screens are labelled. No new broker or paper order endpoint is added;
stock execution stays unpromoted. Existing funded ideas retain actual exit
rules and their independent, honestly labelled performance statistics.

## Ideas product controls

The Ideas page separates Discover and Watchlisted stock plans, with search,
sector filtering and sorting. Stars add/remove only the signed-in user's
existing watchlist; state is returned from that user's persisted rows on reload.
Failed requests preserve the previous state and show an error. Pending requests
block duplicate taps. The complete personal watchlist is linked separately.

Cards show the latest fresh quote or a labelled completed-session close, entry
range, stop/targets, quantity, allocation and estimated risk. View Plan opens
an accessible dialog with target arithmetic, net proceeds, confirmation,
invalidation and evidence. Review Buy shows paper order eligibility; research
plans have a disabled submit button and never call an order endpoint. Existing
manual buys have different exit rules and are not offered as execution of the
research plan. Current strategy/risk rules and funded-idea buying are unchanged.
