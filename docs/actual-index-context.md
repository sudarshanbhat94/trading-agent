# Actual indices and individual-stock paper entries

**TRADING BEHAVIOUR CHANGED.** The retired cash-fund route, its 200-session
entry gate, benchmark and research runners have been removed. New market
context uses actual Nifty 50 completed daily closes plus eligible individual
stock breadth. The existing broad-market 50-session/2%/−3% rules and breadth
cutoffs are reused; this change does not establish profitable returns.

The evidence job captures Nifty 50 and Bank Nifty daily history from Upstox V3
using the existing configured market-data connection. It rejects incomplete
sessions, non-finite/ambiguous OHLC, wrong instrument identities and fewer than
127 completed sessions. The original capture time and immutable source payload
remain queryable. Missing index history is explicit; the market gate can use
its labelled individual-stock-universe fallback. Index levels cannot be
submitted as cash-equity orders.

New screens use `screening-data-v3`, new stock publications
`conditional-pullback-v4`, new paper intents `selective-paper-v2`, and market
context `actual-index-breadth-v1`. The registered v2 forward experiment and all
original publications remain untouched. Existing v2/v3 assessments stay
queryable under their original policies. Stale prior-model decisions/screens
cannot grant current entry permission.

Individual-stock screening and selective paper entry remain enabled, subject
to dated quality, liquidity, participation, pullback/rebound and account risk.
There is no quota. Nifty/Bank Nifty analysis remains available; derivatives need
separate eligible contract, settlement, adapter and capital certification.
Removing a fund does not force ON or override a genuine risk halt.

The retired exchange identity is excluded from enabled discovery, searches,
price-panel rankings and new paper/broker entries. Historic master/price/trade
records are preserved, as are owned SELL and protection paths. The ₹10,000
paper epoch, balances, settings and protocol are not reset by this change.

Source contract: [Upstox V3 historical candles](https://upstox.com/developer/api-documentation/v3/get-historical-candle-data/).
