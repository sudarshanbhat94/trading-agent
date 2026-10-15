# Stock-first product UI

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
