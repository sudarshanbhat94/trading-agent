# Executable cash quote contract

**TRADING BEHAVIOUR CHANGED:** valid Upstox v2 full-quote millisecond timestamps now supply executable paper depth; rapid updates retain exact source ordering; contradictory same-time depth refuses fills. Strategy thresholds, production sleeves, the ₹10,000 epoch and forward protocol are unchanged. This repair grants no production deployment, broker entry or model promotion.

The provider timestamp accepts aware ISO or integer/digit epoch milliseconds. Units are explicit; seconds-width values, floats, booleans, naive and future values refuse. Epoch decoding preserves milliseconds without floating-point rounding; equivalent offset ISO inputs normalize to UTC. Generic journal/evidence timestamp parsing remains aware ISO only. Upstox documents milliseconds and includes aware ISO examples. [Official full-quote response](https://upstox.com/developer/api-documentation/get-full-market-quote/).

Valuation remains separate. Last price, volume, last-trade time and candles do not establish available bid/ask liquidity. Missing executable depth leaves a valuation mark useful, but cannot confirm a paper fill. Provider diagnostics retain up to 20 missing cash-depth symbols; no raw credentials/responses are logged.

The writer serializes source-time comparison in a transaction without committing an enclosing transaction. It compares aware datetimes at full precision, rather than rounded SQLite Julian days. Older responses cannot restore superseded quantities. Equal source time with different content appends immutable `market_execution_quote_conflicts` evidence. Readers withhold the first snapshot, including after a replay; only a strictly later source snapshot restores permission. Same-symbol aliases still refuse instead of selecting one instrument.

`account-schema-v4` adds the conflict table/immutable triggers through the existing atomic startup migration. Existing quote rows, users, settings, paper epoch and historical trading values are retained. An existing unupgraded quote database cannot silently provide conflict-blind permission through the new reader.

## Verification

```sh
.venv/bin/python -m unittest tests.test_executable_quote_times \
  tests.test_executable_quotes tests.test_schema_migrations tests.test_paper_exchange -q
```

Nine new regressions include the actual provider → stored depth → owned pending paper order → balanced ledger path, two-worker ordering, parent rollback, conflict preservation and invalid-input refusal. Inputs, account, instrument, session and favourable prices are synthetic; they establish operational correctness, not strategy profitability or sourced real execution rules.

The isolated OCI runtime verified 443 exact source hashes and booted the actual application after fresh production-copy migrations. Checked original paper/account/configuration values and ₹10,000 cash/equity remain unchanged. Workers and outbound I/O were disabled. This is not a deployed cycle, broker certification or full commercial release; see the [current audit](../reports/release-audit-2026-10-06/REPORT.md).
