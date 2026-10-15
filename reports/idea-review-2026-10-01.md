# Idea review — October 1, 2026, approximately 13:50 IST

Read-only review of Sudarshan's ten original publications, recorded from 09:27:07 IST. First valid stock observations arrived around 09:30; the earlier five-minute feed coverage gaps remain marked. All ten had fresh quotes at the review. This snapshot will change with prices.

Eight stocks were below their first observed price. The equal-weight mean price change was −1.157%; this is a shortlist diagnostic, not a realised portfolio return. None had reached a target or stop. Four had touched their entry zones; six had not. No completed scenarios exist, so a win rate cannot yet be calculated.

| Original idea | Change from first observation | Original 20-session return | Entry-zone status |
|---|---:|---:|---|
| DLF | −0.98% | −0.17% | Touched |
| LALPATHLAB | +1.19% | +4.34% | Untouched |
| AETHER | −1.72% | +0.93% | Untouched |
| INDHOTEL | −1.73% | +1.48% | Untouched |
| PHOENIXLTD | −0.75% | +2.24% | Untouched |
| IPCALAB | +2.46% | −3.05% | Untouched |
| APLAPOLLO | −3.50% | −1.97% | Touched |
| LAURUSLABS | −0.94% | +6.36% | Untouched |
| JINDALSTEL | −3.08% | −2.16% | Touched |
| HBLENGINE | −2.52% | +20.52% | Touched |

## What the code actually does

`app/screening/plans.py:shortlist` requires above-50-session-average price, positive six-month return, positive relative strength versus Nifty and positive sector relative strength. It does **not** require a positive absolute 20-session return. DLF, APLAPOLLO, JINDALSTEL and IPCALAB consequently qualified despite negative recent absolute returns. Relative strength can mean falling more slowly than a falling benchmark; it does not predict that the stock will rise next. IPCALAB's gain today also shows why rejecting every negative recent return cannot be declared an improvement from this one session.

The screen gives binary technical and sector points, uses completed-session prices, and awards participation points rather than requiring a volume surge for every shortlisted stock. Original relative volume was 0.43× for HBLENGINE, 0.93× for APLAPOLLO and 0.76× for LAURUSLABS. This does not demonstrate fresh buying pressure today. The original sector mix includes two Realty and two Capital Goods names, plus three Healthcare names; ten tickers do not ensure independent risks.

Every plan uses the same ATR pullback construction: upper entry = prior close − one ATR; stop = prior close − two ATR; targets = upper entry + 2/3/4 times price risk. These scenarios have not demonstrated stock-specific predictive accuracy or support/resistance alignment. Fundamentals and historical relative strength are useful context but do not confirm an intraday reversal.

The published buy condition explicitly requires a **completed-session rebound with volume confirmation**. Today's session was still open at this review. `app/screening/tracking.py` instead observes a hypothetical entry on the first entry-zone quote, expressly without satisfying that confirmation. The four touched scenarios therefore measure the risk of buying a dip immediately, not the result of the complete published entry condition. Calling them executed strategy trades would be incorrect.

## Why the hypothetical net figures are negative

At the snapshot, three of the four entry-touch scenarios had negative gross price P&L; HBLENGINE was slightly positive from its zone-touch price despite falling from the earlier publication baseline. Independent gross marks were DLF −₹7.35, APLAPOLLO −₹28.80, JINDALSTEL −₹34.80 and HBLENGINE +₹4.30. Frozen estimated delivery charges and slippage were about ₹80–84 per scenario. Their respective hypothetical net marks were −₹90.38, −₹112.69, −₹119.22 and −₹76.02.

These are full-exit estimates, including an assumed future sale. They are not charges already paid, actual fills or an approved portfolio. Summing independent alternatives is not a portfolio backtest: the real book's maximum-position and confirmation gates still apply. Fee constants were not changed in this review.

The actual current-epoch paper book remained **₹10,000 cash, zero positions and ₹0 realised P&L**.

## Market comparison and evidence limits

Stored index observations over roughly 09:30–13:50 showed Nifty about −1.09% and Bank Nifty about −1.02%. These `index_bars` are **put-call-parity estimates**, not an independently verified official spot return. The latest bucket was still incomplete, so these figures support only an approximate comparison. Completed-session breadth was 26.1% above the 50-session average, dated September 30; that is not today's live breadth. The shortlist's decline is consistent with broad weakness, but this review cannot attribute each stock's move to a particular news event or quantify causal market exposure.

The official NSE live-equity page was also checked; its text response returned a loading table rather than current numeric quotes. Search results did not establish a same-session company-specific catalyst for these losses. No external headline was used to invent a cause.

## Required validation before adjusting future selection

The weakness to investigate is selection and timing: distinguish positive absolute momentum from defensive relative strength, measure sector concentration, and separately evaluate entry-zone touches versus the stated confirmed-rebound entry. Do not rewrite these original ideas or activate paper/broker buys merely because a price entered the range. Compare proposed filters and entry confirmations on independent data after fees, slippage and portfolio limits; collect subsequent sessions before claiming an improvement. One intraday cohort cannot establish either profitability or a general rule that a stricter momentum filter will improve results.

No deployed rules, portfolio state, original plans or broker settings changed during this review.
