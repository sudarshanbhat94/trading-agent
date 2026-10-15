# Approved paper/account UI browser evidence — 6 October 2026

Scope: actual `account_ui` component functions/CSS and selected real `v2_web` handlers in `scripts/rehearse_approved_ui.py`. Synthetic account/session override, temporary books/catalogue, invented fixture session/rules/quotes/approval and a synthetic protection warning. No production portfolio, credentials, broker transport or strategy parameter touched. Full production SPA/authentication/watchlist/billing and WCAG certification are not established.

Observed in the browser:

1. Starting fixture: capital/cash/equity ₹10,000, 0 positions, no closed outcomes, win rate/R unavailable. Approval has TEST 20 shares, entry ₹100–101, stop ₹99, target ₹110 and `manual-fixture-v1`.
2. Accessible labelled review dialog presents exact frozen account/quantity/levels and simulated-execution warning. Explicit confirmation returns one paper fill at ₹100. The plan becomes filled and the submit control disables.
3. Fixture-only target advance calls the real paper exit monitor. Account returns to 0 positions with cash/equity ₹10,124.55, closed net ₹124.55. Manual/ON show one trade and original-cost-aware 1.21R; other regimes retain unavailable win rate/R. This manufactured move is not investment performance.
4. Protection card says UNKNOWN/reconciliation required, not protected or filled. Hostile renderer inputs are separately executed in Node tests and remain escaped.
5. At 390×844, document scroll width equals viewport width; key money values/control labels are visible without horizontal overflow. Temporary viewport override reset after verification. [Saved full mobile view](ui-evidence/approved-paper-mobile.png).

Later text-only refinements label the dialog close control `Close` after a fill and keep unrecognised protection states warning-coloured. Node/API tests cover those source components; the screenshot records the preceding semantically equivalent fill/report view. Do not describe it as a screenshot of deployed production.

Refreshed actual-component proof: [paper-lifecycle-mobile-current.png](ui-evidence/paper-lifecycle-mobile-current.png), 390×844 viewport, document width 390 (no overflow). Owned fixture: ₹10,000 → 20 shares at ₹100, cash ₹7,974.03/equity ₹9,974.03 → synthetic target exit, cash/equity ₹10,124.55, zero holdings, one closed net trade ₹124.55, manual/ON and average R 1.21. Quote/session/calendar and protection timeout are synthetic; selected handlers/ledger/report components are actual code. No broker order or profitability evidence. Local fixture server was stopped after inspection.
