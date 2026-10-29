# Stock-first product UI

## Product workspace revision — 27 October 2026, local preview

Following feedback on the sparse layout, the local revision uses a single-row
desktop navigation, a distinct account balance band, legible stock cards with
conditional entry/stop/target levels and a connected trade-plan card. The plan
action appears near its entry zone. Mobile keeps all navigation accessible and
Review plan scrolls to the selected plan. Search now includes company names;
filtered counts are derived from displayed publications rather than an optional
API count. Original levels, approval ownership and execution controls are retained.

The preview includes three explicitly synthetic companies to exercise populated
layouts; it does not add production ideas, approvals or trades. Browser checks
covered selection, saving, owned frozen-plan review, and 320/390px layouts without
horizontal document overflow. This revision is local and has not been deployed.

## Premium workspace refresh — 27 October 2026

The approved premium design is implemented in `app/product_assets/premium.css`
and the existing product renderer. It uses locally bundled, SIL-licensed Inter
(including rupee glyphs), neutral light/dark surfaces,
compact top navigation, account balances and a two-column research list/plan
workspace. Mobile retains top navigation and places the selected plan immediately
after the research rows, before explanatory material. Account, broker, subscription,
Markets and administration remain accessible through the profile menu.

The Opportunities summary reads the authenticated personal overview, including
cash remaining after pending reservations. Missing or failed account reads display
unknown values or Retry; they do not become zero balances. Rows display conditional
entry zones, not invented live quotes. Selection follows search/sector/view filters,
and preserves the original publication's levels. An owned approved plan displays
its own frozen levels, size and model before invoking the existing order review.
Research publications cannot acquire approval through the selected-plan panel.

This is a presentation change. Strategy parameters, execution endpoints, portfolio
epochs, broker settings and the registered experiment remain unchanged. Local
fixture checks are not a production deployment or profitability evidence.

Verification: full unittest suite 2,695 run / 2,562 passed / 133 skipped;
research/UI function runner 108 passed. Final focused checks revalidate the
selected-plan ownership, source levels, account-summary failure states and page
startup. The initial sandbox run could not bind two localhost fixture servers;
the permitted full run passed. Browser checks exercised the actual assembled SPA
using disposable account data: save/search/filter, approval review, pending order,
later fill, owned protection and recorded target exit. Light/dark appearances,
320px layouts, source-level detail and profile navigation were checked. The local
preview uses the same self-hosted font files as production code.

The original design below describes the previous presentation.

The authenticated app opens on one full-width stock feed. Desktop uses a top
navigation bar; mobile uses Ideas, Portfolio, Orders, Saved and Account along the
bottom. Markets remains available from the desktop navigation and account menu.
The old sidebar and dashboard presentation styles are replaced, rather than
shown beside another dashboard.

- **Ideas:** All ideas includes published conditional research and entry plans.
  Entry plans filters stricter publications; neither category fabricates an
  account approval. Search, sector, saving and immutable Tracking remain.
- **Stock:** a separate page shows the publication's entry range, stop, targets,
  rationale, dated evidence and price history. Research cannot invoke a buy.
  An owned approved plan shows its frozen order levels and opens order review.
  Generic analysis verdicts or guessed levels cannot become order approvals.
- **Review:** desktop side sheet / mobile bottom sheet. Existing account risk,
  stable request identity, approved-order endpoint and later-quote fills remain.
  Pending is explicitly unfilled; repeated clicks cannot manufacture a fill.
- **Portfolio:** capital, cash, equity and realised P&L; then actual filled
  positions with quantity, sleeve/regime, recorded stop and target. Costs and
  quote provenance are explicit. Attribution and equity history are disclosures.
- **Orders:** pending/unfilled requests are separate from recorded fills.
  Pending exits retain inventory until a confirmed bid fill.
- **Saved:** an owned list with stock navigation and removal. Loading failures
  are errors with Retry, not an empty saved list.

Account/Upstox, subscription, administration and session controls remain in the
profile menu. Per-account and per-request guards reject late responses. Cached
positions from another owner/scope cannot appear on a new owner's stock page.
No strategy threshold, model, broker permission, epoch or capital is changed.

## Reproduce verification

```sh
.venv/bin/python -m unittest tests.test_stock_first_product tests.test_spa_startup -q
.venv/bin/python -m unittest discover -s tests -t . -q
.venv/bin/python scripts/run_function_tests.py
.venv/bin/python scripts/preview_product_ui.py --port 64182
```

Open `http://127.0.0.1:64182/product`. It serves the actual assembled SPA with a
clearly labelled disposable synthetic fixture. Its approved-paper endpoints use
the real orchestration; no production DB or broker account is connected.
`POST /fixture/fill` advances the synthetic exchange; `POST /fixture/close`
rehearses a target exit. These fixtures demonstrate UI/lifecycle behaviour, never
strategy profitability. Verify desktop and 390px feed, stock detail, watchlist,
order sheet, pending request, filled position and final recorded exit.
