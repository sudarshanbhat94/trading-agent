OpenStocks idea review — 6 October 2026

Read-only production snapshot at 10:48:52 UTC (16:18:52 IST), compared with the 5 October checkpoint. Sources: local saved `var/idea_tracking_review_2026-10-06.json`, `var/idea_tracking_supplement_2026-10-06.json` and `var/idea_tracking_excursions_2026-10-06.json`. These retain the dated deployed tracker, registered forward replay, evidence screen and sampled excursions. No production settings or protocol were changed.

The actual paper account remains ₹10,000 cash/equity, zero positions, zero trades and zero realised P&L. All outcomes below are hypothetical. Neither a price touching a zone nor research eligibility establishes an actual entry.

The tracker grew from 19 publications/15 stocks to 38 publications/19 stocks: 29 v1 publications and nine v2 research publications. There are now six zone-touch scenarios, two stopped scenarios and one T1 marker. All 38 versions are retained; repeated stocks are correlated observations, not independent trades. No ideas have been invalidated or expired. The original APLAPOLLO loss is retained despite its later versions rising.

New outcomes and deterioration:

- JINDALSTEL #2 stopped at the first captured opening quote, ₹1,071.30, at 09:15:24 IST. Hypothetical net loss ₹147.94, −1.0262R; its prior closing scenario mark was −₹96.09. Its sampled post-touch adverse/favourable excursions were −2.88%/+1.02%. The stop observation was below the frozen ₹1,073.19 stop; no ideal stop-price fill was invented.
- AETHER #8 touched T1 at 11:42:12 IST. It remains open, with an after-cost scenario mark of +₹79.84 (+0.5481R), improving from −₹49.16. Sampled favourable excursion reached +10.85%; this is not a completed win. Its later untouched publication remains separate.
- PHOENIXLTD #6 first touched its original zone at 11:23:40 IST. Its after-cost mark is −₹79.21 despite +₹3.30 gross price P&L. Sampled post-touch adverse excursion reached −1.14%.
- HBLENGINE and DLF remain open hypothetical scenarios at −₹77.81 and −₹53.14, worsening from −₹73.62 and −₹32.50 respectively. Their sampled post-touch adverse/favourable excursions are −0.43%/+3.27% and −0.70%/+3.09%.
- The two completed hypothetical losses, original APLAPOLLO −₹133.53 and JINDALSTEL −₹147.94, total −₹281.47; average R is −1.0299. This is not a funded portfolio return. The sample is far too small to establish profitability.

The tracker applies each frozen plan's delivery-cost profile and 0.2% slippage each way. Excursions are sampled bounds, not exact tick extremes; old observation gaps remain. T1/T2 are progression markers, not realised partial exits. This review does not revise the cost model or historical results.

Registered independent forward comparison:

The existing `idea-forward-portfolio-v1` / `confirmed-pullback-review-v1` protocol starts at 2026-10-05T20:09:38.795063 UTC. Its unchanged SHA-256 is `1264f57f0f6838cc65991cd56bef7d1b7e0b417a99935d516316b86e302802d2`. Both confirmed-rebound and gated zone-only arms include nine publications/nine stocks. Each has ₹10,000 cash/equity, zero fills, zero open positions, zero completed outcomes, no funded-replay rejections and no recorded coverage gaps. Win rate and average R are undefined. The comparison provides no evidence that either entry rule is better.

All nine original entry zones were inaccessible in the captured first session: sampled lows stayed above the zone ceiling by SCI 0.76%, VBL 1.51%, LAURUSLABS 1.97%, ADANIPORTS 2.33%, BHEL 2.86%, IPCALAB 2.88%, SONACOMS 2.94%, APLAPOLLO 3.40% and CGPOWER 3.63%. Each stock has 728 samples from 09:15:24 to 15:29:56 IST, with no recorded gaps exceeding 120 regular-session seconds. Brief between-sample crossings remain unknown. Both entry rules consequently lack observed touches; changing confirmation alone would not create an observed entry today.

Original frozen quantities/notionals were ₹1,542.58–₹2,578.38, with estimated stop losses ₹129.92–₹149.36, within the existing ₹150 single-trade risk limit. This checks individual original-plan feasibility, not simultaneous portfolio funding. Latest assessments at elevated closing prices reject sizing: BHEL, VBL and SCI fall below minimum ticket size; six others size below one share. These current-price failures do not imply their original entry zones failed sizing.

Matched benchmark evidence is now available for the nine v2 publications and ten new v1 versions. Over exactly 09:15:24–15:29:56 IST, identified index observations show Nifty +0.8325% and Bank Nifty +0.3717%, with zero endpoint skew. Six of nine v2 stocks beat both indices in observed price movement. BHEL moved +5.28% (+4.4475 percentage points versus Nifty) and LAURUSLABS +4.13% (+3.2975 points). IPCALAB −0.75%, VBL +0.14% and SCI +0.12% lagged both. These are missed observed moves, not attainable trading profits. Matching benchmarks remain unavailable for the older October 1–2 publication intervals, including the stopped scenarios; today's index return cannot be substituted for those intervals.

Eligibility audit and evidence limits:

No immutable confirmation/eligibility events exist yet. The latest after-hours assessments reject all nine: no observed zone touch, closing price outside original zone, no subsequent confirmed fill, OFF regime and current-price risk failures. Eight also lack the specified rebound predicate; SCI passes that predicate alone. Expected after-hours quote freshness failures are not feed failures. These are latest mutable assessments, not an immutable history of what failed throughout the session.

Production archives positive confirmation, confirmed eligibility and baseline eligibility events, but replaces latest assessments and does not archive rejected predicates. Therefore zero replay rejection events means zero eligible decision attempts, not nine successful gate passes. Historical negative decisions and their dated contexts cannot be fully reconstructed. That is a specific audit limitation, separate from today's demonstrably inaccessible zones.

The current completed-session regime remains OFF, breadth 20.7%, with no drawdown halt. The official-membership screen is dated through October 5, improving from the prior October 1 lag. Its October 6 15:50 IST snapshot reports 501 members, 364 liquid names and no feed errors. October 6 completed daily candles were not yet available at review; successful ingestion and an overnight retry do not establish a failure. Today's >120-second stock gaps are absent; October 5's gaps remain recorded.

Candidate concentration is three Capital Goods, two Healthcare, two Services, one Auto Components and one FMCG name. These are candidate counts, not invested exposure. Dated completed-session turnover among the 19 reviewed stocks ranges from approximately ₹33.00 crore (AETHER) to ₹293.00 crore (LAURUSLABS); turnover alone does not establish execution liquidity. BHEL's delivery share was 41.35% versus its 43.53% average; LAURUSLABS 46.42% versus 52.29%; AETHER 49.96% versus 44.65%. Participation confirmation is therefore uneven.

The captured official announcement metadata includes an AETHER tax-notice update, BHEL newspaper publication and a JINDALSTEL newspaper publication after its opening stop. Attempts to read the full [AETHER filing](https://nsearchives.nseindia.com/corporate/AETHER_02102026101322_3.pdf), [BHEL filing](https://nsearchives.nseindia.com/corporate/BHEL_05102026165729_SEINTIMATION.pdf) and [JINDALSTEL filing](https://nsearchives.nseindia.com/corporate/JINDALSTEL_06102026153956_STX_061026.pdf) were unavailable through the browser tool. No price move is attributed to these headlines, and no tax magnitude or earnings conclusion is inferred. Fundamental ratios in the plans are secondary financial statements, explicitly not exchange-verified ratios; missing earnings dates remain unknown.

Concrete proposals, not validated changes:

- Prepare `assessment-observation-audit-v1`: append dated rejected-predicate observations with publication/model identity, quote source/time, completed-bar digest, evidence known-at time and contemporaneous risk context, while preserving existing positive events and entry rules. This addresses overwritten rejection evidence, not returns. Validate replay determinism, ownership and no execution effects on isolated fixtures, then use an independent future cohort to check complete in-session coverage. Risks: storage/write load and accidental duplication of decision events. No deployment is authorised by this heartbeat.
- Preserve the registered comparison unchanged. Keep collecting untouched sessions until both arms have eligible decisions and completed funded outcomes. Today's data can diagnose entry accessibility but cannot validate wider zones, looser regimes or a profitable replacement. Any future entry adjustment needs a distinct preregistered model/cohort, realistic ₹10,000 allocation, the same costs/risk constraints and independently observed outcomes. Promoting a rule fitted to today's missed winners would be selection bias.

No validated strategy adjustment is ready for implementation. Original publications, outcomes and protocol remain intact.
