# OpenStocks handoff — 2026-09-23

This is a dated orientation, not a live portfolio or broker-status report. Verify changing facts from the production book, API, and code before reporting them.

## Current trading path

- `app/v2_live.py:loop()` calls `sleeve_pass()` for paper entries. `app/sleeves/config.py` lists `index_directional` as the only production sleeve; `quality_momentum` is observation-only. Legacy lanes remain disabled. `app/sleeves/risk.py` applies unified limits.
- Subscriber paper-book mirrors cap their quantity at the house-approved shares and their own available cash. They do not independently validate strategy profitability.
- The default paper capital is ₹10,000. The current regime gate may intentionally hold cash. Do not equate an idle cycle with a broken feed, or a displayed research watch with a funded trade.
- `app/live_trade.py:MIRRORED_LANES` does not include `index_directional`; the production sleeve therefore cannot automatically mirror its entries to a broker. Do not claim automatic live trading works without verifying this path and broker readiness.
- The corrected stock replay in `reports/stock_replay_2026-09-23.md` applies the production allocator. It yielded just one holdout trade and did **not** establish an after-cost equity edge. Its retrospective sample has survivorship and coverage limits. There is no verified consistently profitable engine.

## Next product objective

The user wants liquid, high-quality individual NSE equities, with index exposure as one choice. Build a dated, adjusted, point-in-time universe and quality dataset; test stock rules after fees and slippage on an untouched holdout; then forward-test successful candidates in the ₹10,000 paper book. Keep broker execution gated until paper and broker reconciliation are verified. Do not enable a stock sleeve merely to produce trades.

## Non-negotiables

The repo is public: never commit credentials, host addresses, keys, or account tokens. Paper-book correctness outranks features. State **TRADING BEHAVIOUR CHANGED** prominently in both commit and report when applicable. Never promise profit, enter the user's API credentials, or place real trades on the user's behalf.

## Idea observations — 2026-10-01

`app/screening/tracking.py` archives each subscriber's first delivered conditional stock plan in a separate `var/idea_tracking.db`; changed levels/date/quantity create separate versions. `scripts/idea_tracker.py` and `deploy/opentrade-idea-tracker.timer` observe the existing quote feed every 30 seconds without touching paper books or strategy rules. Ideas → Tracking and the private `/v2/api/idea-tracking` endpoint expose frozen plans and forward observations. Entry-zone scenarios are hypothetical, not approved entries or actual paper P&L. See `docs/idea-tracking.md` for methodology and the read-only report command. Initial capture began October 1 at 09:27:07 IST; earlier history is not backfilled.
