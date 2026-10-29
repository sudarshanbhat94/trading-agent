# Dated paper execution funnel

Presentation and read-only reporting only. No strategy, confirmation threshold,
account limit, order lifecycle, broker setting, publication or epoch changes.

The Opportunities page displays **Screened → Confirmed → Fundable → Ordered →
Filled → Exited** above the shortlist, with an IST session date, source timestamps,
and expandable blockers. Additive API field: `GET /v2/api/overview` →
`execution_funnel` (`paper-funnel-v1`).

- **Screened:** unique stocks actually present in the latest evidence receipt,
  with completed-price date and current/stale status. Not a claim that every
  exchange-listed or index constituent has usable data.
- **Confirmed:** latest immutable assessment per latest active stock publication
  in the current strategy epoch/model, dated today. All non-account confirmation
  predicates must pass. Overlapping versions count once per symbol; untouched,
  expired or historical research publications never count as fills. Missing
  assessments yield a partial/unknown count. Checks older than 120 seconds are
  explicitly historical, including expected after-hours assessments.
- **Fundable:** confirmed stocks independently sized **now** for the caller's
  current paper epoch using the existing allocator, quotes and reservations.
  This is account affordability, not approval or cumulative portfolio capacity.
  Stale confirmation/quotes or incomplete coverage show unknown, not zero.
- **Ordered:** distinct owned IN buy intents submitted during today's IST window.
  Includes manual and automated orders; rejected/cancelled submissions still count
  as orders. Current pending and recent terminal reasons remain visible.
- **Filled/Exited:** distinct owned buy/sell orders with immutable `FILLED` events
  today. Yesterday's submission filled today counts as a fill today. Duplicate
  callbacks/events cannot inflate counts. Pending exits are not completed exits.
  Existing NSE paper sell intents omit market; the read adapter resolves those
  without altering records. Explicit non-IN orders remain excluded.

These are separate snapshot/event populations, **not a conversion cohort**.
The UI says so and exposes definitions. A missing journal, unknown account or
failed API response displays unavailable stages; counts are not invented. The
renderer fences cached data by account ID and clears the funnel on overview
failure. Responsive grid: six / three / two columns.

## Verification

Run focused tests:

```sh
.venv/bin/python -m unittest tests.test_execution_funnel tests.test_workspace_product tests.test_affordable_product tests.test_repo_secret_hygiene -q
```

The isolated lifecycle fixture proves reservation → later quote fill → exit
request → later sell fill, with account/epoch isolation. Additional cases cover
IST midnight, duplicate events, overlapping versions, unassessed/stale evidence,
after-cost funding refusal, missing journals and escaped UI text. The preview
uses disposable synthetic data; it does not contact a broker:

```sh
.venv/bin/python scripts/preview_product_ui.py --port 64193
```

Read-only production replay on 29 October 2026 (IST, before market opening)
found 354 stocks in the latest screen, unavailable confirmation/funding counts
because today's assessments were not yet recorded, and zero owned order/fill/exit
events today. This is a dated observation, not evidence of signal quality or
profitability. Private replay and browser receipts are kept in ignored `var/`.
