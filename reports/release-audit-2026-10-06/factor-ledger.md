**Revalidation:** dormant scoring code and model parameters unchanged from the inspected baseline; this ledger is static factor analysis, not independent strategy approval or live evidence.

# Legacy confluence predicate ledger

The implementation has four capped buckets totalling an advertised maximum of 26; it is not 26 independent factors. The predicates below were extracted from and reviewed against `_confluence_score`. A present predicate is not proof of predictive benefit. This legacy score is not the active production-sleeve selector.

| Clause | Source line | Bucket | Predicate | Contribution |
|---|---:|---|---|---|
| 1 | 1379 | macro | trend_context['daily'] in {'STRONG_UPTREND', 'WEAK_UPTREND'} | +2 |
| 2 | 1381 | macro | global_context.get('risk_score', 0) > 0.15 | +1 |
| 3 | 1383 | macro | filters.get('trend_not_down') | +1 |
| 4 | 1385 | macro | global_context.get('regime') == 'risk-on' | +1 |
| 5 | 1387 | macro | filters.get('within_25pct_period_high') | +1 |
| 6 | 1390 | macro | flow_bias is not None and flow_bias > 0.1 | +1 |
| 7 | 1394 | technical | institutional.get('wyckoff_phase') in {'phase_c_spring_candidate', 'markup'} | +2 |
| 8 | 1396 | technical | institutional.get('fair_value_gap', {}).get('present') | +1 |
| 9 | 1398 | technical | institutional.get('liquidity_sweep', {}).get('low_sweep') | +2 |
| 10 | 1399 | technical | unconditional | +min(2, round(abs(chart_patterns.get('score', 0)) * 6)) |
| 11 | 1401 | technical | institutional.get('premium_discount') == 'discount' | +1 |
| 12 | 1403 | technical | filters.get('above_200dma') or (filters.get('above_200dma') is None and indicators['moving_averages'].get('sma_50')) | +1 |
| 13 | 1405 | technical | indicators.get('ichimoku', {}).get('bias') == 'bullish_cloud' | +1 |
| 14 | 1407 | technical | indicators.get('divergence_proxy', {}).get('signal') == 'bullish_divergence' | +1 |
| 15 | 1411 | candle | abs(candlestick_v2.get('score', 0)) >= 0.35 | +2 |
| 16 | 1413 | candle | indicators.get('macd', {}).get('bias') == 'bullish' and (indicators.get('obv_slope') or 0) >= 0 | +1 |
| 17 | 1415 | candle | filters.get('volume_ratio_min_1_5') | +1 |
| 18 | 1417 | candle | filters.get('rsi_40_70') | +1 |
| 19 | 1419 | candle | liquidity.get('tradeable') and (not liquidity.get('circuit_risk_proxy')) | +1 |
| 20 | 1423 | news | sentiment_score > 0.15 | +1 |
| 21 | 1425 | news | sentiment_score > -0.25 | +1 |
| 22 | 1427 | news | any((signal.get('direction') == 'BUY' for signal in strategy_signals)) | +1 |
| 23 | 1429 | news | filters.get('volume_ratio_min_1_5') | +1 |
| 24 | 1431 | news | delivery.get('bias') == 'accumulation' | +1 |
| 25 | 1433 | news | relative_strength.get('bias') == 'outperforming' | +1 |
| 26 | 1435 | news | backtest.get('expectancy') and backtest.get('expectancy') > 0 | +1 |
| 27 | 1440 | news | institutional_flow.get('symbol_flags', {}).get('official_announcements_count', 0) > 0 and sentiment_score > 0.15 | +1 |
| 28 | 1443 | news | delivery_score > 0.6 | +2 |
| 29 | 1445 | news | NOT (delivery_score > 0.6) AND delivery_score < -0.6 | -2 |
| 30 | 1447 | technical | stage_analysis.get('stage') == 'Stage2_Markup' | +2 if stage_analysis.get('stage_confidence') == 'high' else 1 |
| 31 | 1449 | technical | NOT (stage_analysis.get('stage') == 'Stage2_Markup') AND stage_analysis.get('stage') in {'Stage3_Distribution', 'Stage4_Decline'} | -3 |
| 32 | 1452 | technical | alignment_grade == 'A' | +2 |
| 33 | 1454 | technical | NOT (alignment_grade == 'A') AND alignment_grade == 'B' | +1 |
| 34 | 1456 | technical | NOT (alignment_grade == 'A') AND NOT (alignment_grade == 'B') AND alignment_grade == 'D' | -2 |
| 35 | 1457 | technical | unconditional | +float(price_volume_divergence.get('divergence_score') or 0.0) |
| 36 | 1459 | candle | entry_quality.get('volume_confirmation') | +1 |
| 37 | 1461 | candle | float(entry_quality.get('last_close_position_in_range') or 0.0) > 0.75 | +1 |

## Correctness and validation notes

- Macro clauses can contribute at most seven points, while the bucket advertises eight; investigate score-normalisation before reusing thresholds.
- Absolute chart/candlestick scores can reward bearish patterns in a long-only confluence total. Directional pattern evidence must preserve its sign.
- Missing 200-session evidence falls back to any available 50-session mean, rather than proving price above that mean. Missing eligibility evidence must not create a positive score.
- Volume is rewarded in multiple buckets; news has a three-point ceiling, so many later confirmations saturate rather than contribute independently. Measure redundancy and ablation.
- Zero/neutral sentiment can earn a point. Distinguish no-news, unavailable coverage and neutral verified events.
- Stage and alignment use derived proxies. Use completed calendar-week aggregation and dated inputs; the current daily fallback is not equivalent to a 30-week MA.
- Validate every row with positive/negative/missing inputs, availability times, direction, matching timeframe and coefficient ablation. Existing source inspection does not prove all statistical weights correct.
- The VCP routine partitions a trailing window; Darvas returns window extrema. They are heuristic batch proxies, not a persisted confirm/invalidate/roll state machine. Preserve pivot publication time before claiming a stateful detector.
