# Current source coverage — 6 October 2026

Inventory is static extraction. Targeted boundary review and invariant fixtures do not establish a full semantic audit of every inventoried module. Current hashes are in inventory.json; original finding evidence remains in findings.json. No private runtime file contents were copied.

| Module | Review status | Finding evidence |
|---|---|---|
| app/__init__.py | Inventoried; full semantic review pending |  |
| app/executable_quotes.py | Narrow source normalization/storage/identity/time/depth boundaries reviewed; production feed certification pending | F25 |
| app/paper_exchange.py | New owned paper reservation/later-event fill/exit path and runtime outage tests reviewed; partial fills/other routes not implemented | F01, F05, F25 |
| app/login_guard.py | Narrow shared login reservations/concurrency/restart boundaries reviewed; production security review pending | F38 |
| app/account.py | Inventoried; full semantic review pending |  |
| app/account_safety.py | Targeted cited runtime boundary; other functions not certified | F12:36 |
| app/account_ui.py | New narrow module read/reviewed with isolated acceptance; production/external certification pending |  |
| app/agent.py | Inventoried; full semantic review pending |  |
| app/analysis_tools.py | Inventoried; full semantic review pending |  |
| app/analysts.py | Inventoried; full semantic review pending |  |
| app/approved_execution.py | New narrow module read/reviewed with isolated acceptance; production/external certification pending | F01:1 |
| app/auth.py | Targeted cited runtime boundary; other functions not certified | F37:185, F38:372, F38:475, F50:500 |
| app/bars5m.py | Inventoried; full semantic review pending |  |
| app/books.py | Targeted cited runtime boundary; other functions not certified | F03:261, F04:639, F05:618, F11:261, F11:441, F12:110, F49:261 |
| app/broker.py | Targeted cited runtime boundary; other functions not certified | F06:246, F08:1, F25:590, F36:127 |
| app/broker_access.py | Inventoried; full semantic review pending |  |
| app/broker_reconciliation.py | Targeted cited runtime boundary; other functions not certified | F09:53, F09:132 |
| app/canonical_trade.py | Inventoried; full semantic review pending |  |
| app/config.py | Inventoried; full semantic review pending |  |
| app/corpactions.py | Targeted cited runtime boundary; other functions not certified | F26:1 |
| app/costs.py | Targeted cited runtime boundary; other functions not certified | F23:1 |
| app/credential_vault.py | Targeted cited runtime boundary; other functions not certified | F36:37 |
| app/data_readiness.py | Inventoried; full semantic review pending |  |
| app/db.py | Targeted cited runtime boundary; other functions not certified | F40:1131, F50:1131 |
| app/decision_contract.py | Inventoried; full semantic review pending |  |
| app/decision_diagnostics.py | Inventoried; full semantic review pending |  |
| app/delivery_data.py | Inventoried; full semantic review pending |  |
| app/desk_ui.py | Inventoried; full semantic review pending |  |
| app/event_calendar.py | Inventoried; full semantic review pending |  |
| app/execution_contracts.py | New narrow module read/reviewed with isolated acceptance; production/external certification pending | F01:1 |
| app/execution_events.py | New narrow module read/reviewed with isolated acceptance; production/external certification pending |  |
| app/execution_outbox.py | Targeted cited runtime boundary; other functions not certified | F11:28, F19:37 |
| app/execution_ports.py | Targeted cited runtime boundary; other functions not certified | F21:36, F21:54, F53:54 |
| app/exit_policy.py | Targeted cited runtime boundary; other functions not certified | F02:15 |
| app/factor_investigation.py | Inventoried; full semantic review pending |  |
| app/full_spectrum.py | Targeted cited runtime boundary; other functions not certified | F31:1357, F31:1357, F31:1357, F32:408 |
| app/ideas.py | Inventoried; full semantic review pending |  |
| app/index_direction.py | Inventoried; full semantic review pending |  |
| app/index_spot.py | Inventoried; full semantic review pending |  |
| app/india_top_gainers.py | Targeted cited runtime boundary; other functions not certified | F32:425, F32:465 |
| app/indicators.py | Inventoried; full semantic review pending |  |
| app/institutional_feeds.py | Inventoried; full semantic review pending |  |
| app/instrument_catalog.py | Targeted cited runtime boundary; other functions not certified | F10:18, F10:137 |
| app/jobs_health.py | Targeted cited runtime boundary; other functions not certified | F42:44, F42:110, F52:44, F52:66 |
| app/levels.py | Inventoried; full semantic review pending |  |
| app/live_release.py | New narrow module read/reviewed with isolated acceptance; production/external certification pending |  |
| app/live_trade.py | Targeted cited runtime boundary; other functions not certified | F03:381, F06:299, F08:1, F20:340 |
| app/llm_brain.py | Targeted cited runtime boundary; other functions not certified | F34:176, F34:225, F35:978 |
| app/llm_policy.py | Targeted cited runtime boundary; other functions not certified | F33:9 |
| app/llm_usage.py | Inventoried; full semantic review pending |  |
| app/macro.py | Inventoried; full semantic review pending |  |
| app/macro_calendar.py | Inventoried; full semantic review pending |  |
| app/main.py | Targeted cited runtime boundary; other functions not certified | F14:919, F14:5012, F14:3574, F33:101, F37:5012, F40:670 |
| app/manual_execution.py | Targeted cited runtime boundary; other functions not certified | F18:5 |
| app/market_action_radar.py | Inventoried; full semantic review pending |  |
| app/market_breadth.py | Inventoried; full semantic review pending |  |
| app/market_data.py | Targeted cited runtime boundary; other functions not certified | F26:761 |
| app/market_day_regime.py | Inventoried; full semantic review pending |  |
| app/market_internals.py | Inventoried; full semantic review pending |  |
| app/market_regions.py | Targeted cited runtime boundary; other functions not certified | F27:69, F27:152 |
| app/meta_filter.py | Inventoried; full semantic review pending |  |
| app/models.py | Inventoried; full semantic review pending |  |
| app/narrative.py | Inventoried; full semantic review pending |  |
| app/nfo_contracts.py | Targeted cited runtime boundary; other functions not certified | F24:82 |
| app/openclaw_bridge.py | Inventoried; full semantic review pending |  |
| app/opportunity_scanner.py | Inventoried; full semantic review pending |  |
| app/opportunity_state.py | Inventoried; full semantic review pending |  |
| app/option_chain.py | Inventoried; full semantic review pending |  |
| app/options_intelligence.py | Inventoried; full semantic review pending |  |
| app/order_journal.py | Targeted cited runtime boundary; other functions not certified | F03:155, F19:155, F20:155, F53:155 |
| app/order_router.py | Inventoried; full semantic review pending |  |
| app/paper_broker.py | Inventoried; full semantic review pending |  |
| app/paper_ledger.py | Targeted cited runtime boundary; other functions not certified | F11:36 |
| app/personal_alerts.py | Inventoried; full semantic review pending |  |
| app/personal_performance.py | Targeted cited runtime boundary; other functions not certified | F49:19 |
| app/plans.py | Targeted cited runtime boundary; other functions not certified | F16:66, F48:110 |
| app/portfolio.py | Inventoried; full semantic review pending |  |
| app/pre_catalyst_engine.py | Inventoried; full semantic review pending |  |
| app/preopen.py | Inventoried; full semantic review pending |  |
| app/price_action.py | Inventoried; full semantic review pending |  |
| app/protection.py | New narrow module read/reviewed with isolated acceptance; production/external certification pending | F08:1 |
| app/rally_plan.py | Inventoried; full semantic review pending |  |
| app/raw_entry_model.py | Inventoried; full semantic review pending |  |
| app/recommendation.py | Inventoried; full semantic review pending |  |
| app/recovery_bundle.py | New narrow module read/reviewed with isolated acceptance; production/external certification pending | F41:1 |
| app/recovery_guard.py | New narrow module read/reviewed with isolated acceptance; production/external certification pending | F41:1 |
| app/release_gate.py | Targeted cited runtime boundary; other functions not certified | F44:20 |
| app/request_context.py | Inventoried; full semantic review pending |  |
| app/screening/__init__.py | Inventoried; full semantic review pending |  |
| app/screening/confirmation.py | Targeted cited runtime boundary; other functions not certified | F29:28, F51:104 |
| app/screening/plans.py | Targeted cited runtime boundary; other functions not certified | F17:18 |
| app/screening/providers.py | Targeted cited runtime boundary; other functions not certified | F28:74, F28:74, F29:35 |
| app/screening/screen.py | Targeted cited runtime boundary; other functions not certified | F28:56 |
| app/screening/store.py | Inventoried; full semantic review pending |  |
| app/screening/tracking.py | Targeted cited runtime boundary; other functions not certified | F51:403 |
| app/screening/validation.py | Targeted cited runtime boundary; other functions not certified | F22:23 |
| app/sector_rotation.py | Inventoried; full semantic review pending |  |
| app/sentiment.py | Targeted cited runtime boundary; other functions not certified | F35:703, F35:703 |
| app/signal_quality.py | Inventoried; full semantic review pending |  |
| app/sleeves/__init__.py | Inventoried; full semantic review pending |  |
| app/sleeves/accounting.py | Inventoried; full semantic review pending |  |
| app/sleeves/base.py | Inventoried; full semantic review pending |  |
| app/sleeves/config.py | Targeted cited runtime boundary; other functions not certified | F22:19 |
| app/sleeves/early_momentum.py | Inventoried; full semantic review pending |  |
| app/sleeves/engine.py | Inventoried; full semantic review pending |  |
| app/sleeves/feeds.py | Targeted cited runtime boundary; other functions not certified | F25:1 |
| app/sleeves/forward_watch.py | Inventoried; full semantic review pending |  |
| app/sleeves/index_directional.py | Targeted cited runtime boundary; other functions not certified | F02:31, F30:27 |
| app/sleeves/mean_reversion.py | Inventoried; full semantic review pending |  |
| app/sleeves/options_overlay.py | Targeted cited runtime boundary; other functions not certified | F24:1 |
| app/sleeves/performance.py | Inventoried; full semantic review pending |  |
| app/sleeves/quality_momentum.py | Inventoried; full semantic review pending |  |
| app/sleeves/readiness.py | Targeted cited runtime boundary; other functions not certified | F17:10 |
| app/sleeves/reference.py | Inventoried; full semantic review pending |  |
| app/sleeves/regime.py | Targeted cited runtime boundary; other functions not certified | F30:1 |
| app/sleeves/risk.py | Inventoried; full semantic review pending |  |
| app/sleeves/universe.py | Inventoried; full semantic review pending |  |
| app/strategy.py | Inventoried; full semantic review pending |  |
| app/strategy_backtest.py | Targeted cited runtime boundary; other functions not certified | F23:14 |
| app/strategy_presets.py | Inventoried; full semantic review pending |  |
| app/telegram_bot.py | Inventoried; full semantic review pending |  |
| app/tomorrow_plan.py | Inventoried; full semantic review pending |  |
| app/trade_economics.py | Inventoried; full semantic review pending |  |
| app/trading_readiness.py | Inventoried; full semantic review pending |  |
| app/trading_rules.py | Inventoried; full semantic review pending |  |
| app/universe.py | Inventoried; full semantic review pending |  |
| app/us_top_movers.py | Inventoried; full semantic review pending |  |
| app/v2_engine.py | Inventoried; full semantic review pending |  |
| app/v2_live.py | Targeted cited runtime boundary; other functions not certified | F01:1, F02:3808, F04:3947, F17:4310, F23:1559, F24:4310, F25:4310, F27:46, F40:998 |
| app/v2_web.py | Targeted cited runtime boundary; other functions not certified | F10:2695, F13:1846, F15:6659, F16:1096, F18:1869, F37:134, F45:6243, F45:6575, F46:5433, F46:5438, F47:6325, F47:6423, F48:1096, F48:1189 |
| app/whatsapp.py | Inventoried; full semantic review pending |  |
| app/worker_fencing.py | Targeted cited runtime boundary; other functions not certified | F43:28 |
| scripts/audit_inventory.py | Inventoried; full semantic review pending |  |
| scripts/audit_market_prices.py | Inventoried; full semantic review pending |  |
| scripts/audit_trade_costs.py | Inventoried; full semantic review pending |  |
| scripts/backtest_entry_authority_v2.py | Inventoried; full semantic review pending |  |
| scripts/backtest_executable_trade_contract.py | Inventoried; full semantic review pending |  |
| scripts/backtest_exits.py | Inventoried; full semantic review pending |  |
| scripts/backtest_intraday.py | Inventoried; full semantic review pending |  |
| scripts/backtest_investigation.py | Inventoried; full semantic review pending |  |
| scripts/backtest_redesign.py | Inventoried; full semantic review pending |  |
| scripts/backtest_signal_quality.py | Inventoried; full semantic review pending |  |
| scripts/backtest_strategies.py | Inventoried; full semantic review pending |  |
| scripts/backtest_upstox_history_entry_authority.py | Inventoried; full semantic review pending |  |
| scripts/backtest_v2.py | Inventoried; full semantic review pending |  |
| scripts/backtest_walkforward.py | Inventoried; full semantic review pending |  |
| scripts/backtest_yahoo_presets.py | Inventoried; full semantic review pending |  |
| scripts/backup_db.py | Targeted cited runtime boundary; other functions not certified | F41:1 |
| scripts/bigmove_detector.py | Inventoried; full semantic review pending |  |
| scripts/breakout_target_bt.py | Inventoried; full semantic review pending |  |
| scripts/btst_bt.py | Inventoried; full semantic review pending |  |
| scripts/candle_ingest.py | Inventoried; full semantic review pending |  |
| scripts/check_release.py | Inventoried; full semantic review pending |  |
| scripts/continuation_bt.py | Inventoried; full semantic review pending |  |
| scripts/daily_report.py | Inventoried; full semantic review pending |  |
| scripts/evidence_screen.py | Inventoried; full semantic review pending |  |
| scripts/exit_cost_analysis.py | Inventoried; full semantic review pending |  |
| scripts/exit_stop_bt.py | Inventoried; full semantic review pending |  |
| scripts/exit_structure_sweep.py | Inventoried; full semantic review pending |  |
| scripts/experiment_report.py | Inventoried; full semantic review pending |  |
| scripts/fetch_yahoo_intraday.py | Inventoried; full semantic review pending |  |
| scripts/fo_ingest.py | Inventoried; full semantic review pending |  |
| scripts/gap_momentum.py | Inventoried; full semantic review pending |  |
| scripts/idea_tracker.py | Inventoried; full semantic review pending |  |
| scripts/import_sleeve_reference.py | Inventoried; full semantic review pending |  |
| scripts/index_call_backtest.py | Inventoried; full semantic review pending |  |
| scripts/index_option_target_study.py | Inventoried; full semantic review pending |  |
| scripts/intraday_backfill.py | Inventoried; full semantic review pending |  |
| scripts/intraday_momentum_bt.py | Inventoried; full semantic review pending |  |
| scripts/intraday_recorder.py | Inventoried; full semantic review pending |  |
| scripts/intraday_research.py | Inventoried; full semantic review pending |  |
| scripts/levels_chain_scan.py | Inventoried; full semantic review pending |  |
| scripts/meta_label_research.py | Inventoried; full semantic review pending |  |
| scripts/meta_label_train.py | Inventoried; full semantic review pending |  |
| scripts/meta_portfolio_eval.py | Inventoried; full semantic review pending |  |
| scripts/news_ingest.py | Inventoried; full semantic review pending |  |
| scripts/nse_announcements.py | Inventoried; full semantic review pending |  |
| scripts/nse_reference_ingest.py | Inventoried; full semantic review pending |  |
| scripts/overnight_catalyst_bt.py | Inventoried; full semantic review pending |  |
| scripts/preview_spa.py | Inventoried; full semantic review pending |  |
| scripts/price_action_scan.py | Inventoried; full semantic review pending |  |
| scripts/quality_forward_report.py | Inventoried; full semantic review pending |  |
| scripts/recovery_bundle.py | Inventoried; full semantic review pending |  |
| scripts/regime_isolation.py | Inventoried; full semantic review pending |  |
| scripts/rehearse_approved_ui.py | Inventoried; full semantic review pending |  |
| scripts/rehearse_execution.py | Inventoried; full semantic review pending |  |
| scripts/research_factor_etf.py | Inventoried; full semantic review pending |  |
| scripts/research_index_baselines.py | Inventoried; full semantic review pending |  |
| scripts/research_index_fresh_start.py | Inventoried; full semantic review pending |  |
| scripts/research_sleeves.py | Inventoried; full semantic review pending |  |
| scripts/research_stock_replay.py | Targeted cited runtime boundary; other functions not certified | F22:1, F26:111 |
| scripts/run_function_tests.py | Inventoried; full semantic review pending |  |
| scripts/score_rebuild.py | Inventoried; full semantic review pending |  |
| scripts/shareholding_ingest.py | Inventoried; full semantic review pending |  |
| scripts/strategy_refine_analysis.py | Inventoried; full semantic review pending |  |
| scripts/sync_instrument_catalog.py | Inventoried; full semantic review pending |  |
| scripts/test_nubra_market_watch.py | Inventoried; full semantic review pending |  |
| scripts/update_universe_from_upstox.py | Inventoried; full semantic review pending |  |
| scripts/update_us_universe.py | Inventoried; full semantic review pending |  |
| scripts/v2_live_runner.py | Inventoried; full semantic review pending |  |
| scripts/v2_paper_runner.py | Inventoried; full semantic review pending |  |
| scripts/v2_quote_feed.py | Targeted cited runtime boundary; other functions not certified | F07:53 |
| scripts/validate_ideas.py | Inventoried; full semantic review pending |  |
| tests/__init__.py | Inventoried; full semantic review pending |  |
| tests/broker_evidence_fixtures.py | Inventoried; full semantic review pending |  |
| tests/test_account_boundaries.py | Inventoried; full semantic review pending |  |
| tests/test_account_evidence_api.py | Inventoried; full semantic review pending |  |
| tests/test_account_ui.py | Inventoried; full semantic review pending |  |
| tests/test_admin_panel.py | Inventoried; full semantic review pending |  |
| tests/test_agent_callbacks.py | Inventoried; full semantic review pending |  |
| tests/test_alerts.py | Inventoried; full semantic review pending |  |
| tests/test_alpaca_market_data.py | Inventoried; full semantic review pending |  |
| tests/test_analysts.py | Inventoried; full semantic review pending |  |
| tests/test_approved_execution.py | Inventoried; full semantic review pending |  |
| tests/test_approved_order_api.py | Inventoried; full semantic review pending |  |
| tests/test_assessment_audit.py | Inventoried; full semantic review pending |  |
| tests/test_audit_regressions.py | Inventoried; full semantic review pending |  |
| tests/test_auth_session_security.py | Inventoried; full semantic review pending |  |
| tests/test_auth_signup.py | Inventoried; full semantic review pending |  |
| tests/test_bars5m.py | Inventoried; full semantic review pending |  |
| tests/test_breakeven_lock.py | Inventoried; full semantic review pending |  |
| tests/test_broker.py | Inventoried; full semantic review pending |  |
| tests/test_broker_privacy.py | Inventoried; full semantic review pending |  |
| tests/test_broker_wire.py | Inventoried; full semantic review pending |  |
| tests/test_candle_ingest_freshness.py | Inventoried; full semantic review pending |  |
| tests/test_canonical_trade_contract.py | Inventoried; full semantic review pending |  |
| tests/test_catalyst_alerts.py | Inventoried; full semantic review pending |  |
| tests/test_corpactions.py | Inventoried; full semantic review pending |  |
| tests/test_cross_alerts.py | Inventoried; full semantic review pending |  |
| tests/test_dashboard_journal_contract.py | Inventoried; full semantic review pending |  |
| tests/test_data_coverage.py | Inventoried; full semantic review pending |  |
| tests/test_decision_contract.py | Inventoried; full semantic review pending |  |
| tests/test_decision_diagnostics.py | Inventoried; full semantic review pending |  |
| tests/test_dependency_contract.py | Inventoried; full semantic review pending |  |
| tests/test_desk_status_copy.py | Inventoried; full semantic review pending |  |
| tests/test_engine_decision_logic.py | Inventoried; full semantic review pending |  |
| tests/test_equity_option_book_isolation.py | Inventoried; full semantic review pending |  |
| tests/test_event_calendar.py | Inventoried; full semantic review pending |  |
| tests/test_evidence_screen.py | Inventoried; full semantic review pending |  |
| tests/test_executable_replay_calibration.py | Inventoried; full semantic review pending |  |
| tests/test_execution_capabilities.py | Inventoried; full semantic review pending |  |
| tests/test_execution_contracts.py | Inventoried; full semantic review pending |  |
| tests/test_execution_events.py | Inventoried; full semantic review pending |  |
| tests/test_execution_hardening.py | Inventoried; full semantic review pending |  |
| tests/test_exit_rules.py | Inventoried; full semantic review pending |  |
| tests/test_expiry_churn_guard.py | Inventoried; full semantic review pending |  |
| tests/test_falling_knife_guard.py | Inventoried; full semantic review pending |  |
| tests/test_fo_ingest.py | Inventoried; full semantic review pending |  |
| tests/test_fundamental_analyst.py | Inventoried; full semantic review pending |  |
| tests/test_idea_broker_buy.py | Inventoried; full semantic review pending |  |
| tests/test_idea_hardening.py | Inventoried; full semantic review pending |  |
| tests/test_idea_quote_priority.py | Inventoried; full semantic review pending |  |
| tests/test_idea_tracking.py | Inventoried; full semantic review pending |  |
| tests/test_idea_validation.py | Inventoried; full semantic review pending |  |
| tests/test_ideas.py | Inventoried; full semantic review pending |  |
| tests/test_ideas_product_ui.py | Inventoried; full semantic review pending |  |
| tests/test_index_chart_ui.py | Inventoried; full semantic review pending |  |
| tests/test_index_direction.py | Inventoried; full semantic review pending |  |
| tests/test_index_options_pass.py | Inventoried; full semantic review pending |  |
| tests/test_index_settings_ui.py | Inventoried; full semantic review pending |  |
| tests/test_index_spot.py | Inventoried; full semantic review pending |  |
| tests/test_index_view_ui.py | Inventoried; full semantic review pending |  |
| tests/test_india_remediation.py | Inventoried; full semantic review pending |  |
| tests/test_india_top_gainers.py | Inventoried; full semantic review pending |  |
| tests/test_indicators_advanced.py | Inventoried; full semantic review pending |  |
| tests/test_internals_in_index_call.py | Inventoried; full semantic review pending |  |
| tests/test_intraday_backfill.py | Inventoried; full semantic review pending |  |
| tests/test_intraday_bt_loader.py | Inventoried; full semantic review pending |  |
| tests/test_intraday_momentum_lane.py | Inventoried; full semantic review pending |  |
| tests/test_intraday_recorder.py | Inventoried; full semantic review pending |  |
| tests/test_jobs_health.py | Inventoried; full semantic review pending |  |
| tests/test_levels.py | Inventoried; full semantic review pending |  |
| tests/test_live_release.py | Inventoried; full semantic review pending |  |
| tests/test_live_trade.py | Inventoried; full semantic review pending |  |
| tests/test_llm_analyst_packet.py | Inventoried; full semantic review pending |  |
| tests/test_llm_hard_disable.py | Inventoried; full semantic review pending |  |
| tests/test_macro_analyst.py | Inventoried; full semantic review pending |  |
| tests/test_manual_buy_books.py | Inventoried; full semantic review pending |  |
| tests/test_market_action_radar.py | Inventoried; full semantic review pending |  |
| tests/test_market_regions.py | Inventoried; full semantic review pending |  |
| tests/test_meta_floor_alarm.py | Inventoried; full semantic review pending |  |
| tests/test_meta_population_features.py | Inventoried; full semantic review pending |  |
| tests/test_midsession_entry_extremes.py | Inventoried; full semantic review pending |  |
| tests/test_monitor_scope.py | Inventoried; full semantic review pending |  |
| tests/test_narrative.py | Inventoried; full semantic review pending |  |
| tests/test_net_trade_pnl.py | Inventoried; full semantic review pending |  |
| tests/test_nfo_contracts.py | Inventoried; full semantic review pending |  |
| tests/test_nse_announcements.py | Inventoried; full semantic review pending |  |
| tests/test_nse_reference_ingest.py | Inventoried; full semantic review pending |  |
| tests/test_opportunity_scanner.py | Inventoried; full semantic review pending |  |
| tests/test_option_buying_retired.py | Inventoried; full semantic review pending |  |
| tests/test_option_chain.py | Inventoried; full semantic review pending |  |
| tests/test_option_expiry_churn.py | Inventoried; full semantic review pending |  |
| tests/test_option_expiry_exit.py | Inventoried; full semantic review pending |  |
| tests/test_option_liquidity_and_size.py | Inventoried; full semantic review pending |  |
| tests/test_option_quote_staleness.py | Inventoried; full semantic review pending |  |
| tests/test_option_risk_cap.py | Inventoried; full semantic review pending |  |
| tests/test_order_timestamps.py | Inventoried; full semantic review pending |  |
| tests/test_overnight_size_cap.py | Inventoried; full semantic review pending |  |
| tests/test_panel_ui_render.py | Inventoried; full semantic review pending |  |
| tests/test_paper_ledger.py | Inventoried; full semantic review pending |  |
| tests/test_pattern_alerts.py | Inventoried; full semantic review pending |  |
| tests/test_pattern_filter.py | Inventoried; full semantic review pending |  |
| tests/test_personal_performance.py | Inventoried; full semantic review pending |  |
| tests/test_phase1_quality.py | Inventoried; full semantic review pending |  |
| tests/test_phase2_data_readiness.py | Inventoried; full semantic review pending |  |
| tests/test_phase3_strategy_logic.py | Inventoried; full semantic review pending |  |
| tests/test_phase4_performance_feedback.py | Inventoried; full semantic review pending |  |
| tests/test_plan_enforcement.py | Inventoried; full semantic review pending |  |
| tests/test_point_in_time_universe.py | Inventoried; full semantic review pending |  |
| tests/test_portfolio.py | Inventoried; full semantic review pending |  |
| tests/test_portfolio_ui_render.py | Inventoried; full semantic review pending |  |
| tests/test_position_exit_terms_ui.py | Inventoried; full semantic review pending |  |
| tests/test_position_mark_refresh.py | Inventoried; full semantic review pending |  |
| tests/test_pre_catalyst_engine.py | Inventoried; full semantic review pending |  |
| tests/test_preopen.py | Inventoried; full semantic review pending |  |
| tests/test_protection_lifecycle.py | Inventoried; full semantic review pending |  |
| tests/test_quality_forward_watch.py | Inventoried; full semantic review pending |  |
| tests/test_rally_plan.py | Inventoried; full semantic review pending |  |
| tests/test_real_money_readiness.py | Inventoried; full semantic review pending |  |
| tests/test_recommendation.py | Inventoried; full semantic review pending |  |
| tests/test_record_entry.py | Inventoried; full semantic review pending |  |
| tests/test_recovery_bundle.py | Inventoried; full semantic review pending |  |
| tests/test_release_evidence_gate.py | Inventoried; full semantic review pending |  |
| tests/test_release_integrity.py | Inventoried; full semantic review pending |  |
| tests/test_release_safety.py | Inventoried; full semantic review pending |  |
| tests/test_repo_secret_hygiene.py | Inventoried; full semantic review pending |  |
| tests/test_research_baselines.py | Inventoried; full semantic review pending |  |
| tests/test_research_stock_replay_risk.py | Inventoried; full semantic review pending |  |
| tests/test_reset_is_shared.py | Inventoried; full semantic review pending |  |
| tests/test_retired_entry_controls.py | Inventoried; full semantic review pending |  |
| tests/test_shareholding_ingest.py | Inventoried; full semantic review pending |  |
| tests/test_sleeve_data_integrity.py | Inventoried; full semantic review pending |  |
| tests/test_sleeves.py | Inventoried; full semantic review pending |  |
| tests/test_stock_plans.py | Inventoried; full semantic review pending |  |
| tests/test_stock_technicals_block.py | Inventoried; full semantic review pending |  |
| tests/test_stock_ui_render.py | Inventoried; full semantic review pending |  |
| tests/test_strategy_plans.py | Inventoried; full semantic review pending |  |
| tests/test_strategy_safety.py | Inventoried; full semantic review pending |  |
| tests/test_strategy_stats.py | Inventoried; full semantic review pending |  |
| tests/test_today_bar.py | Inventoried; full semantic review pending |  |
| tests/test_tomorrow_plan.py | Inventoried; full semantic review pending |  |
| tests/test_total_drawdown_halt.py | Inventoried; full semantic review pending |  |
| tests/test_trial_and_upgrade.py | Inventoried; full semantic review pending |  |
| tests/test_trial_banner_ui.py | Inventoried; full semantic review pending |  |
| tests/test_upstox_candles.py | Inventoried; full semantic review pending |  |
| tests/test_upstox_history_backtest.py | Inventoried; full semantic review pending |  |
| tests/test_upstox_news.py | Inventoried; full semantic review pending |  |
| tests/test_us_top_movers.py | Inventoried; full semantic review pending |  |
| tests/test_user_books.py | Inventoried; full semantic review pending |  |
| tests/test_user_preferences.py | Inventoried; full semantic review pending |  |
| tests/test_v2_api_auth.py | Inventoried; full semantic review pending |  |
| tests/test_volume_surge_sizing.py | Inventoried; full semantic review pending |  |
| tests/test_watchlist_grouping.py | Inventoried; full semantic review pending |  |
| tests/test_watchlist_ui_render.py | Inventoried; full semantic review pending |  |
| tests/test_whatsapp_alerts.py | Inventoried; full semantic review pending |  |
| app/angelone_port.py | New narrow module reviewed with isolated acceptance; production/source/commercial certification pending | F21 |
| app/billing_ledger.py | New narrow module reviewed with isolated acceptance; production/source/commercial certification pending | F48, F54 |
| app/broker_ledger.py | New narrow module reviewed with isolated acceptance; production/source/commercial certification pending | F09, F11 |
| app/catalogue_ingestion.py | New narrow module reviewed with isolated acceptance; production/source/commercial certification pending | F10 |
| app/entry_contracts.py | New narrow module reviewed with isolated acceptance; production/source/commercial certification pending | F01, F10 |
| app/incident_inbox.py | New narrow module reviewed with isolated acceptance; production/source/commercial certification pending | F20 |
| app/recovery_stream.py | New narrow module reviewed with isolated acceptance; production/source/commercial certification pending | F41 |
| app/request_security.py | New narrow module reviewed with isolated acceptance; production/source/commercial certification pending | F37 |
| app/research_data.py | New narrow module reviewed with isolated acceptance; production/source/commercial certification pending | F28 |
| app/schema_migrations.py | New narrow module reviewed with isolated acceptance; production/source/commercial certification pending | F40 |

Current release safety boundaries reviewed: sourced entry/approval/catalogue, actual broker ledger/reconciliation, native partial recovery, atomic billing/migration, incident ownership, request origin/proxy, recovery frames and PIT immutability. This does not turn all inventoried legacy files into semantically reviewed files.

The corrected source added the no-op runtime-settings preservation regression. Inventory now includes 179 test modules. Actual pinned-runtime app startup was checked against fresh copied production originals after the reproduced timestamp defect; this verifies those stated preservation boundaries, not full semantic review of every settings consumer.

F58/F59 received targeted semantic review of paper exit/session-cache and exact execution-contract time selection. Six new boundary tests cover sourced closure/reopening, conflict/expiry/future evidence, source outage/cache immutability and pre-open quote times. The inventory now has 180 test modules. This extends narrow runtime review without claiming every legacy module has been semantically reviewed.

## Additional narrow semantic review — 7 October

Protection status/activation/fill/cancellation, amendment claims/resolution, owner-stream collection and exact ledger fee/FIFO/reversal boundaries were reviewed and exercised on current fixtures. Actual broker operation and all other module paths remain uncertified. New modules: `app/protection_amendments.py`, `app/portfolio_stream.py`, `scripts/broker_portfolio_feed.py`; findings F08/F09/F60–F68.

F69 received targeted semantic review of the complete reconciliation comparison/write path and its journal/native-maintenance callers. Six exact-parent regressions cover competing commits, cash-changing zero-inventory round trips, stale fills/commitments, malformed legacy evidence and cross-owner/nonfinancial observations. This adds narrow reviewed scope, not full semantic review of every inventoried file.

Current refreshed static inventory includes 182 test modules. Only explicitly described boundaries are semantically reviewed.

F70–F72: narrowly reviewed provider normalization, exact serialized depth updates, immutable conflict withholding, account-schema-v4 and actual provider → stored depth → owned pending paper order → ledger fixture. Current-date public NSE source retrieval timed out; no full raw-source or production execution coverage is claimed.
