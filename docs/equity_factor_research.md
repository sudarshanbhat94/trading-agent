# Large-cap quality/momentum paper sleeve — evidence and limits

The candidate universe is the intersection of two current NSE constituent
files: [Nifty 100](https://www.niftyindices.com/IndexConstituent/ind_nifty100list.csv)
and [Nifty500 Multicap Momentum Quality 50](https://www.niftyindices.com/IndexConstituent/ind_nifty500MulticapMomentumQuality50_list.csv).
NSE's [index description](https://www.niftyindices.com/indices/equity/strategy-indices/nifty500-multicap-momentum-quality50)
states that its quality input uses return on equity, leverage and earnings
stability. Membership is a *screen*, not a per-stock fundamental score or
proof that the trading rule makes money.

The production paper screen requires both files to pass complete-set,
duplicate and ISIN checks; the same timestamp is stored for the pair.
A snapshot older than seven days blocks new stock entries. It then requires
completed 6- and 12-month momentum (excluding the most recent month),
₹25 crore median daily turnover, an affordable share price, and a live
quote no more than 10% above the 50-session mean. The master regime must be
ON with the existing strong-trend confirmation. The unified risk manager sizes at most one stock, with an ATR stop,
12% trailing stop and 45-session time stop. Real-broker mirroring is disabled.

The available preliminary point-in-time replay is **not positive evidence**:
using [June 2025 factor holdings, filed July 10](https://bsmedia.business-standard.com/_media/bs/data/announcements/bse/10072025/7be2c14f-ad49-49a5-b46d-057b0fb53bf9.pdf),
[December 2025 factor holdings, filed January 9](https://bsmedia.business-standard.com/_media/bs/data/announcements/bse/09012026/23ab8dc6-c7d2-493e-b89c-bc5a74bc5515.pdf),
and dated Nifty 100 ETF holdings,
a related shorter-lookback monthly rule returned about **−0.39%** during
July–December 2025 and **−0.26%** during February–June 2026 on a ₹10,000
book after simulated delivery costs and 20 bps slippage. It made only four
round trips across both windows. That exploratory rule differs from the
production 6/12-month screen, so these numbers are a warning, not a claimed
backtest of production. The current local daily history does not cover the
earlier observations needed for a clean 2025 replay of the production rule.
There is **no validated profitable stock track record**. This sleeve is an
experimental paper test only; forward results must be measured before
considering live eligibility.

The ₹10,000 book also constrains the index sleeve: ₹5,000 of NIFTYBEES at
its 25% disaster stop risks ₹1,250, exceeding the entire 10% book drawdown
budget. The allocator now reserves ₹150 of stop risk for tactical stock
positions and caps strategic index stop risk at ₹850, leaving the combined
planned stop loss at or below ₹1,000 before slippage or gaps. A gap may exceed
the planned stop; the limit is a sizing control, not a guarantee.
