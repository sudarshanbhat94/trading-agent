# Runtime bug repairs — 29 October 2026

**TRADING BEHAVIOUR CHANGED:** nonfinite prices/ranges are rejected or replaced by finite price fallbacks at the runtime quote boundary. Corrupt infinity values can no longer drive entry/exit calculations. Valid-data strategy rules, risk settings, publications, books and the registered forward protocol are unchanged. This is not a model promotion or a profitability claim.

## Reproduced defects and repairs

- `app/v2_web.py:_live_map` omitted source metadata required by `execution_funnel.report`: confirmed stocks could remain unassessable for funding. Preserve the actual SQL-filtered source. A real SQLite quote read now traverses the actual funnel with a synthetic confirmed publication; it becomes fundable without an order/fill.
- Engine/web quote readers accepted positive infinity; OHLC/volume helpers propagated it. Require finite positive numbers; malformed ranges/volume use existing finite fallbacks. Tests cover corrupt rows, boolean values and source isolation.
- Quote, optional index/options and index-history readers leaked SQLite handles on errors (and index-history success). Close actual connections on every path; tests assert the connection is unusable after the read.
- `_remember_sleeve_view` claimed all gates passed before canonical contract checks and reservations. Display proposed/checking, actual refusal or pending reservation separately; risk halts dominate. Replayed terminal intents cannot count as new pending orders or crash logging on absent requested quantity. The real production-pass fixture exercises canonical rejection before a later successful reservation/fill.
- The funnel treated stale negative assessments as current zero funding. Historical assessments now make current fundability unknown, retaining observed counts and dated refusal evidence.
- Screening displayed only retained stocks as total coverage, omitting pre-score history/liquidity/identity exclusions. Expose observed checks, scored liquid names and exclusion counts separately. Missing or inconsistent exclusion metadata remains unknown; reference membership is never assumed checked.
- Missing/stale/future marks silently valued owned holdings at entry, creating fake flat P&L/equity. Overview, positions and portfolio now preserve known cash/cost basis and report unknown current valuation; the workspace explains missing marks and does not total them as zero. Historical equity/trades remain unchanged.
- Four UI test methods were stranded inside module main guards and never discovered. Restore them to their classes, repair their obsolete helper calls, and add an AST discovery guard.

## Verification

Local full unittest run: 2,803 tests / 2,670 passed / 133 skipped / zero failures. Additional function fixtures: 135 passed. Isolated production stock entry, durable order, later confirmed fill, automatic stop AND target, restart/replay, bad-depth refusal and balanced after-cost accounting rehearsals passed. No broker orders. Actual SPA checked in a disposable preview at desktop and 390px; mobile document width equals viewport width. Synthetic preview is not an authenticated production UI check.

Follow the exact-commit CI and ignored rollout receipts for deployment state. Production books/configuration/protocol require preservation checks before and after restart.

## Actual observed entry blockers and remaining evidence

Read-only production observation at 15:15 IST: completed candles 2,634/2,634 through October 28; broad quotes 2,652/2,664 fresh and watched quotes 25/25 fresh. Twelve missing identities/quotes remain explicitly unresolved, not fabricated. The retained liquid screen had 354 rows out of a 501-name reference. Reference membership is not the same as observed or fully qualified coverage.

Zero entry-qualified plans; six research candidates, one independently fundable. COFORGE's original October 22 zone (1,831.47–1,846.94) was never observed touched; its current price was 1,963. Participation/rebound checks failed and one-share after-cost stop risk was 260.41 versus the effective 150 allowance. No paper fills existed. Quote-source repair fixes funding reporting; it does not make this stock a valid entry.

The original-zone-after-rebound timing design still needs a separately versioned independent experiment. Preserve original publications and protocol; do not loosen it using these inspected outcomes. Financial-sector capital/asset-quality facts are missing for some stocks; no inferred approval replaces them. A genuine qualifying production lifecycle and a sufficient independent after-cost record remain unobserved. These are unresolved acceptance requirements, not evidence that all bugs or profitability are solved.
