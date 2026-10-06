# Current review coverage and exclusions

Baseline b588ffd plus the hashed working tree. Every included Python source was parsed for declarations; semantic review is explicitly narrower. This is an A–Z area review and full static inventory, **not a completed semantic certification of every module/function**. Unknown paths remain review work, not a claim of correctness.

## Coverage tiers

- Static inventory: 121 app modules, 69 scripts and 145 test modules, 150 decorated routes, 30 typed models, 149 SQL declaration sites, 287 config fields, 312 unique environment names, 36 job candidates, 7 UI surfaces, 705 assumption/reference candidates and 60 literal declarations. These include dormant/test references and dynamic extraction limits.
- Targeted semantic review: actual startup/loop/manual/delivery/journal/owned exit paths, account risk/epoch and required schema checks, broker contracts/reconciliation/secret/session state, discovery rules and research predicates/reports/UI sinks referenced below. A cited function is not its entire large module.
- New modules: personal posting/performance and recorded release evaluator fully read and tested for their stated narrow contracts; health/transport methods read. Live settlement, extra calendars and real certification remain absent.
- Revalidation: original IDs retained; current cited source and baseline diffs inspected, known repaired invariants covered by isolated regressions. Unchanged dormant/unverified requirements retain their gap and acceptance; they are not counted as reproduced deployed incidents.
- Production: read-only deployed build/service and clean personal-book snapshot only. No production new-build proof, open live lifecycle, protection/recovery, restore/load or browser order journey.
- Test/browser evidence: exact executions/skips/environment limitations in verification.json. The synthetic desktop/390px Tracking component proves only its visible rendering/GET interaction.

## App module coverage

| Module | Review scope | Current finding evidence |
|---|---|---|
| app/__init__.py | Inventoried declarations; semantic review pending |  |
| app/account.py | Inventoried declarations; semantic review pending |  |
| app/account_safety.py | Targeted cited paths / actual runtime boundary; remaining functions pending | F12:36 |
| app/agent.py | Inventoried declarations; semantic review pending |  |
| app/analysis_tools.py | Inventoried declarations; semantic review pending |  |
| app/analysts.py | Inventoried declarations; semantic review pending |  |
| app/auth.py | Targeted cited paths / actual runtime boundary; remaining functions pending | F37:186, F38:372, F38:475, F50:507 |
| app/bars5m.py | Inventoried declarations; semantic review pending |  |
| app/books.py | Targeted cited paths / actual runtime boundary; remaining functions pending | F03:256, F04:628, F05:607, F11:256, F11:432, F12:106, F49:256 |
| app/broker.py | Targeted cited paths / actual runtime boundary; remaining functions pending | F06:246, F08:592, F25:592, F36:127 |
| app/broker_access.py | Inventoried declarations; semantic review pending |  |
| app/broker_reconciliation.py | Targeted cited paths / actual runtime boundary; remaining functions pending | F09:132, F09:53 |
| app/canonical_trade.py | Inventoried declarations; semantic review pending |  |
| app/config.py | Inventoried declarations; semantic review pending |  |
| app/corpactions.py | Targeted cited paths / actual runtime boundary; remaining functions pending | F26:1 |
| app/costs.py | Targeted cited paths / actual runtime boundary; remaining functions pending | F23:1 |
| app/credential_vault.py | Targeted cited paths / actual runtime boundary; remaining functions pending | F36:37 |
| app/data_readiness.py | Inventoried declarations; semantic review pending |  |
| app/db.py | Targeted cited paths / actual runtime boundary; remaining functions pending | F40:1135, F50:1135 |
| app/decision_contract.py | Inventoried declarations; semantic review pending |  |
| app/decision_diagnostics.py | Inventoried declarations; semantic review pending |  |
| app/delivery_data.py | Inventoried declarations; semantic review pending |  |
| app/desk_ui.py | Inventoried declarations; semantic review pending |  |
| app/event_calendar.py | Inventoried declarations; semantic review pending |  |
| app/execution_outbox.py | Targeted cited paths / actual runtime boundary; remaining functions pending | F11:28, F19:37 |
| app/execution_ports.py | New narrow contract fully read; isolated tests; external certification pending | F21:36, F21:48, F53:48 |
| app/exit_policy.py | Targeted cited paths / actual runtime boundary; remaining functions pending | F02:15 |
| app/factor_investigation.py | Inventoried declarations; semantic review pending |  |
| app/full_spectrum.py | Targeted cited paths / actual runtime boundary; remaining functions pending | F31:1357, F31:1401, F31:1449, F32:408 |
| app/ideas.py | Inventoried declarations; semantic review pending |  |
| app/index_direction.py | Inventoried declarations; semantic review pending |  |
| app/index_spot.py | Inventoried declarations; semantic review pending |  |
| app/india_top_gainers.py | Targeted cited paths / actual runtime boundary; remaining functions pending | F32:425, F32:465 |
| app/indicators.py | Inventoried declarations; semantic review pending |  |
| app/institutional_feeds.py | Inventoried declarations; semantic review pending |  |
| app/instrument_catalog.py | Targeted cited paths / actual runtime boundary; remaining functions pending | F10:124, F10:18 |
| app/jobs_health.py | New narrow contract fully read; isolated tests; external certification pending | F42:110, F42:44, F52:44, F52:66 |
| app/levels.py | Inventoried declarations; semantic review pending |  |
| app/live_trade.py | Targeted cited paths / actual runtime boundary; remaining functions pending | F01:43, F03:381, F06:299, F08:456, F20:340 |
| app/llm_brain.py | Targeted cited paths / actual runtime boundary; remaining functions pending | F34:176, F34:236, F35:980 |
| app/llm_policy.py | Targeted cited paths / actual runtime boundary; remaining functions pending | F33:9 |
| app/llm_usage.py | Inventoried declarations; semantic review pending |  |
| app/macro.py | Inventoried declarations; semantic review pending |  |
| app/macro_calendar.py | Inventoried declarations; semantic review pending |  |
| app/main.py | Targeted cited paths / actual runtime boundary; remaining functions pending | F14:3574, F14:5012, F14:919, F33:101, F37:5012, F40:670 |
| app/manual_execution.py | Targeted cited paths / actual runtime boundary; remaining functions pending | F18:7 |
| app/market_action_radar.py | Inventoried declarations; semantic review pending |  |
| app/market_breadth.py | Inventoried declarations; semantic review pending |  |
| app/market_data.py | Targeted cited paths / actual runtime boundary; remaining functions pending | F26:776 |
| app/market_day_regime.py | Inventoried declarations; semantic review pending |  |
| app/market_internals.py | Inventoried declarations; semantic review pending |  |
| app/market_regions.py | Targeted cited paths / actual runtime boundary; remaining functions pending | F27:152, F27:74 |
| app/meta_filter.py | Inventoried declarations; semantic review pending |  |
| app/models.py | Inventoried declarations; semantic review pending |  |
| app/narrative.py | Inventoried declarations; semantic review pending |  |
| app/nfo_contracts.py | Targeted cited paths / actual runtime boundary; remaining functions pending | F24:103 |
| app/openclaw_bridge.py | Inventoried declarations; semantic review pending |  |
| app/opportunity_scanner.py | Inventoried declarations; semantic review pending |  |
| app/opportunity_state.py | Inventoried declarations; semantic review pending |  |
| app/option_chain.py | Inventoried declarations; semantic review pending |  |
| app/options_intelligence.py | Inventoried declarations; semantic review pending |  |
| app/order_journal.py | Targeted cited paths / actual runtime boundary; remaining functions pending | F03:135, F19:135, F20:231, F53:135 |
| app/order_router.py | Inventoried declarations; semantic review pending |  |
| app/paper_broker.py | Inventoried declarations; semantic review pending |  |
| app/paper_ledger.py | New narrow contract fully read; isolated tests; external certification pending | F11:36 |
| app/personal_alerts.py | Inventoried declarations; semantic review pending |  |
| app/personal_performance.py | New narrow contract fully read; isolated tests; external certification pending | F49:19 |
| app/plans.py | Targeted cited paths / actual runtime boundary; remaining functions pending | F16:66, F48:110 |
| app/portfolio.py | Inventoried declarations; semantic review pending |  |
| app/pre_catalyst_engine.py | Inventoried declarations; semantic review pending |  |
| app/preopen.py | Inventoried declarations; semantic review pending |  |
| app/price_action.py | Inventoried declarations; semantic review pending |  |
| app/rally_plan.py | Inventoried declarations; semantic review pending |  |
| app/raw_entry_model.py | Inventoried declarations; semantic review pending |  |
| app/recommendation.py | Inventoried declarations; semantic review pending |  |
| app/release_gate.py | New narrow contract fully read; isolated tests; external certification pending | F44:20 |
| app/request_context.py | Inventoried declarations; semantic review pending |  |
| app/screening/__init__.py | Inventoried declarations; semantic review pending |  |
| app/screening/confirmation.py | Targeted cited paths / actual runtime boundary; remaining functions pending | F29:28, F51:104 |
| app/screening/plans.py | Targeted cited paths / actual runtime boundary; remaining functions pending | F17:18 |
| app/screening/providers.py | Targeted cited paths / actual runtime boundary; remaining functions pending | F28:141, F28:74, F29:35 |
| app/screening/screen.py | Targeted cited paths / actual runtime boundary; remaining functions pending | F28:56 |
| app/screening/store.py | Inventoried declarations; semantic review pending |  |
| app/screening/tracking.py | Targeted cited paths / actual runtime boundary; remaining functions pending | F51:403 |
| app/screening/validation.py | Targeted cited paths / actual runtime boundary; remaining functions pending | F22:23 |
| app/sector_rotation.py | Inventoried declarations; semantic review pending |  |
| app/sentiment.py | Targeted cited paths / actual runtime boundary; remaining functions pending | F35:715, F35:758 |
| app/signal_quality.py | Inventoried declarations; semantic review pending |  |
| app/sleeves/__init__.py | Inventoried declarations; semantic review pending |  |
| app/sleeves/accounting.py | Inventoried declarations; semantic review pending |  |
| app/sleeves/base.py | Inventoried declarations; semantic review pending |  |
| app/sleeves/config.py | Targeted cited paths / actual runtime boundary; remaining functions pending | F01:19, F22:19 |
| app/sleeves/early_momentum.py | Inventoried declarations; semantic review pending |  |
| app/sleeves/engine.py | Inventoried declarations; semantic review pending |  |
| app/sleeves/feeds.py | Targeted cited paths / actual runtime boundary; remaining functions pending | F25:1 |
| app/sleeves/forward_watch.py | Inventoried declarations; semantic review pending |  |
| app/sleeves/index_directional.py | Targeted cited paths / actual runtime boundary; remaining functions pending | F01:28, F02:31, F30:28 |
| app/sleeves/mean_reversion.py | Inventoried declarations; semantic review pending |  |
| app/sleeves/options_overlay.py | Targeted cited paths / actual runtime boundary; remaining functions pending | F24:1 |
| app/sleeves/performance.py | Inventoried declarations; semantic review pending |  |
| app/sleeves/quality_momentum.py | Inventoried declarations; semantic review pending |  |
| app/sleeves/readiness.py | Targeted cited paths / actual runtime boundary; remaining functions pending | F17:29 |
| app/sleeves/reference.py | Inventoried declarations; semantic review pending |  |
| app/sleeves/regime.py | Targeted cited paths / actual runtime boundary; remaining functions pending | F30:1 |
| app/sleeves/risk.py | Inventoried declarations; semantic review pending |  |
| app/sleeves/universe.py | Inventoried declarations; semantic review pending |  |
| app/strategy.py | Inventoried declarations; semantic review pending |  |
| app/strategy_backtest.py | Targeted cited paths / actual runtime boundary; remaining functions pending | F23:16 |
| app/strategy_presets.py | Inventoried declarations; semantic review pending |  |
| app/telegram_bot.py | Inventoried declarations; semantic review pending |  |
| app/tomorrow_plan.py | Inventoried declarations; semantic review pending |  |
| app/trade_economics.py | Inventoried declarations; semantic review pending |  |
| app/trading_readiness.py | Inventoried declarations; semantic review pending |  |
| app/trading_rules.py | Inventoried declarations; semantic review pending |  |
| app/universe.py | Inventoried declarations; semantic review pending |  |
| app/us_top_movers.py | Inventoried declarations; semantic review pending |  |
| app/v2_engine.py | Inventoried declarations; semantic review pending |  |
| app/v2_live.py | Targeted cited paths / actual runtime boundary; remaining functions pending | F01:4524, F02:3802, F04:3941, F17:4415, F23:1592, F24:4524, F25:4531, F27:46, F40:1026 |
| app/v2_web.py | Targeted cited paths / actual runtime boundary; remaining functions pending | F10:2653, F13:1862, F15:6659, F16:1102, F18:1869, F37:134, F45:6243, F45:6575, F46:5433, F46:5438, F47:6325, F47:6423, F48:1102, F48:1189 |
| app/whatsapp.py | Inventoried declarations; semantic review pending |  |
| app/worker_fencing.py | Targeted cited paths / actual runtime boundary; remaining functions pending | F43:28 |

## Non-app source review

New/changed scripts: audit_inventory extracts names/hashes without app import; check_release refuses missing/dirty-build evidence; rehearse_execution exercises an isolated actual paper entry/delivery/close/ledger. The legacy regression runner executes current isolated invariants. Tests are assertions/evidence, not an independent full semantic review of all dormant sources.

## Excluded, name-only or inaccessible sources

Private runtime databases/configs/credentials, ignored broker tokens/vault keys and ignored market datasets are excluded from the source inventory. Production config values were not copied. Report artifacts are excluded from AST/source extraction and read separately as audit evidence. The exact inventory exclusions follow; reasons distinguish deliberate exclusion from failed reads.

| File | Reason |
|---|---|
| .claude/launch.json | non-source/binary; name only |
| .gitignore | non-source/binary; name only |
| app/static/openstocks-mark.svg | non-source/binary; name only |
| data/universe.csv | non-source/binary; name only |
| data/us_universe.csv | non-source/binary; name only |
| reports/execution-hardening-2026-10-06.md | runtime/private values or report artifact excluded from source extraction; names only |
| reports/idea-hardening-2026-10-06.md | runtime/private values or report artifact excluded from source extraction; names only |
| reports/idea-review-2026-10-01.md | runtime/private values or report artifact excluded from source extraction; names only |
| reports/idea-review-2026-10-02.md | runtime/private values or report artifact excluded from source extraction; names only |
| reports/idea-review-2026-10-03.md | runtime/private values or report artifact excluded from source extraction; names only |
| reports/idea-review-2026-10-05.md | runtime/private values or report artifact excluded from source extraction; names only |
| reports/idea-review-2026-10-06.md | runtime/private values or report artifact excluded from source extraction; names only |
| reports/idea-validation-plan-2026-10-06.md | runtime/private values or report artifact excluded from source extraction; names only |
| reports/oci_signal_generation_analysis_2026-06-11.md | runtime/private values or report artifact excluded from source extraction; names only |
| reports/paper_risk_audit_2026-09-23.md | runtime/private values or report artifact excluded from source extraction; names only |
| reports/release-audit-2026-10-06/REPORT.md | runtime/private values or report artifact excluded from source extraction; names only |
| reports/release-audit-2026-10-06/architecture.md | runtime/private values or report artifact excluded from source extraction; names only |
| reports/release-audit-2026-10-06/browser-component-check.md | runtime/private values or report artifact excluded from source extraction; names only |
| reports/release-audit-2026-10-06/coverage.md | runtime/private values or report artifact excluded from source extraction; names only |
| reports/release-audit-2026-10-06/critical-fixes.md | runtime/private values or report artifact excluded from source extraction; names only |
| reports/release-audit-2026-10-06/factor-ledger.json | runtime/private values or report artifact excluded from source extraction; names only |
| reports/release-audit-2026-10-06/factor-ledger.md | runtime/private values or report artifact excluded from source extraction; names only |
| reports/release-audit-2026-10-06/findings.json | runtime/private values or report artifact excluded from source extraction; names only |
| reports/release-audit-2026-10-06/historical-safety-reproductions-2b300b4.json | runtime/private values or report artifact excluded from source extraction; names only |
| reports/release-audit-2026-10-06/inventory.json | runtime/private values or report artifact excluded from source extraction; names only |
| reports/release-audit-2026-10-06/inventory.md | runtime/private values or report artifact excluded from source extraction; names only |
| reports/release-audit-2026-10-06/key-diffs.patch | runtime/private values or report artifact excluded from source extraction; names only |
| reports/release-audit-2026-10-06/release-evidence.json | runtime/private values or report artifact excluded from source extraction; names only |
| reports/release-audit-2026-10-06/release-gate-result.json | runtime/private values or report artifact excluded from source extraction; names only |
| reports/release-audit-2026-10-06/reproduce_known_defects.py | runtime/private values or report artifact excluded from source extraction; names only |
| reports/release-audit-2026-10-06/roadmap.md | runtime/private values or report artifact excluded from source extraction; names only |
| reports/release-audit-2026-10-06/runtime-paths.md | runtime/private values or report artifact excluded from source extraction; names only |
| reports/release-audit-2026-10-06/safety-reproductions.json | runtime/private values or report artifact excluded from source extraction; names only |
| reports/release-audit-2026-10-06/ui-review.md | runtime/private values or report artifact excluded from source extraction; names only |
| reports/release-audit-2026-10-06/verification.json | runtime/private values or report artifact excluded from source extraction; names only |
| reports/release-audit-implementation-2026-10-06.md | runtime/private values or report artifact excluded from source extraction; names only |
| reports/release-safety-implementation-2026-10-06.md | runtime/private values or report artifact excluded from source extraction; names only |
| reports/stock_replay_2026-09-23.md | runtime/private values or report artifact excluded from source extraction; names only |
| reports/tradability-audit-2026-10-06.md | runtime/private values or report artifact excluded from source extraction; names only |
| reports/trading_system_audit_2026-06-01.md | runtime/private values or report artifact excluded from source extraction; names only |

## Public data header check

- data/universe.csv: 2657 rows; header names symbol, name, exchange, yahoo_symbol, kite_symbol, upstox_instrument_key, sector, industry, base_price, enabled. No price/fundamental row values copied. These headers are not a complete effective-dated master or PIT-membership certification.
- data/us_universe.csv: 10430 rows; header names symbol, name, exchange, yahoo_symbol, kite_symbol, upstox_instrument_key, sector, industry, base_price, enabled. No price/fundamental row values copied. These headers are not a complete effective-dated master or PIT-membership certification.

## Remaining independent evidence

Hosted CI status; broker/API/hosting/algorithm registration and protection permissions; market-data redistribution licences; exact settlement/fee statements; current vulnerability/SBOM findings; production proxy/IAM/CSRF boundary testing; key escrow and full database/config/protocol restore; cross-process chaos/load; full production browser/mobile/WCAG flows; and independent strategy profitability all remain unverified. No zero-vulnerability or all-files correctness claim is made.

## Next semantic review order

1. Every remaining reachable writer and fallback, including dormant Agent/PaperBroker/control/bridge code, canonical reservation/fill/fee/protection state machine and recovery.
2. Instrument identity/effective rule calendars, corporate actions/restrictions/bans, multi-leg/settlement/margin and all feed contracts.
3. Each retained factor/news/fundamental/sleeve model, PIT availability, immutable evidence and untouched allocator/exit/cost parity.
4. Cross-route ownership/auth/session/CSRF/proxy/security, secret escrow/privacy/data rights and billing.
5. Complete assembled SPA critical journeys, accessibility/race states, notifications/SLOs, migrations/restore/load and independent release evidence.

Record exact functions reviewed and acceptance evidence at each step. No static inventory count or test count substitutes for completing these reviews.
