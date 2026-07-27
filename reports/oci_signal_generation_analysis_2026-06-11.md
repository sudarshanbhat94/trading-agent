# OCI Signal Generation Analysis - 2026-06-11

## Scope

- Host: production VM (address redacted — this repository is public)
- Service: `opentrade.service` active
- Deployed commit: `dd8b92c`
- DB: the candles/quotes SQLite database on the deploy host
- Check time: `2026-06-11T16:52:58Z`

## How Signals Are Made

The live signal path on OCI is:

1. `app/raw_entry_model.py::evaluate_raw_entry` scores each scanner candidate and emits raw `BUY` only when `decision_label == ENTRY_READY`.
2. `app/strategy.py::_raw_entry_action_from_context` converts that raw result into a `Decision` action.
3. `app/agent.py` inserts decisions, then calls `db.upsert_signal_ideas_from_decisions(decisions)`.
4. `app/db.py::_signal_idea_from_decision` converts raw decisions into UI `signal_ideas`, and can downgrade a raw `BUY` into `WATCH`.
5. `app/signal_quality.py::trade_readiness_gate` and `auto_follow_quality_gate` decide whether an idea can be followed.
6. `app/agent.py::_auto_follow_buy_ideas_for_signal_users` creates paper follows only from active BUY ideas for AUTO_PAPER/AUTO_LIVE users.

This means a raw `BUY` in `decisions` is not the same as a trusted trade. The real trading truth is:

`raw decision -> signal idea -> active BUY -> paper follow -> order`

## Current OCI Funnel

Latest decision diagnostics:

- `market_region`: `BOTH`
- `mode`: `dynamic_opportunity_scan`
- `raw_symbol_count`: `1013`
- `scanner_shortlist_count`: `89`
- `full_decision_count`: `102`
- `target_decision_count`: `200`
- `paper_follow_conversion_count`: `0`
- `action_counts`: `{"HOLD": 102}`
- `cycle_duration_seconds`: `71.512`
- Latest cycle is US-only: `target_decision_symbols_by_market = {"US": 200}`
- US scanner shortfall is still large: live rally shortfall `34`, breakout shortfall `27`, sector RS shortfall `22`
- Top blocker: `watch_only_evidence_below_entry_line`, count `94`

Current market regime:

- India: `enabled=false`, `state=risk_off`, `checked_symbols=0`, `momentum_allowed=false`
- US: `state=fade_risk`, `checked_symbols=102`, `momentum_allowed=false`

Issue: India is showing `risk_off` even though the regime has zero checked symbols after market close. That should be `market_closed` or `no_live_regime`, not a defensive market verdict.

## Decisions, Ideas, Follows, Orders

Last 36 hours:

- India raw decisions: `106 BUY`, `32759 HOLD`
- US raw decisions: `0 BUY`, `16110 HOLD`

Last 72 hours signal ideas:

- Active BUY ideas: `0`
- India: `1 BUY EXPIRED`, `1750 WATCH`, `196 WATCH EXPIRED`
- US: `1 BUY STOP_HIT`, `2861 WATCH`, `1132 WATCH EXPIRED`

Last 72 hours paper follows:

- Total PAPER follows: `360`
- Active PAPER follows: `0`
- Exited PAPER follows: `360`
- Zero-quantity follows: `57`
- Gross P/L from follow rows: `-6949.86`
- Invested notional across rows: `4562972.03`
- Broker orders: `0`

Major follow exit reasons:

- `active_follow_hard_blocked`: 137 follows, P/L `-4386.50`
- `active_follow_raw_opportunity_not_entry_ready`: 42 follows, P/L `-2196.24`
- `active_follow_severe_risk_flags`: 42 follows, P/L `-696.60`
- `active_follow_watch_state_exit`: 52 follows, P/L `+2666.82`
- `active_follow_not_tradeable_state`: 12 follows, P/L `-3247.66`

This is not trade-ready behavior. The system is allowing follow creation for raw ideas that later get immediately declared invalid, watch-only, hard-blocked, or severe-risk.

## Raw BUY Examples

Recent raw BUYs that show the problem:

- `APARINDS`: latest raw BUY at `14802`, strategy `extended_momentum_watch`, raw label `ENTRY_READY`, raw family `live_momentum`. A watch strategy should not become an auto-followable BUY without separate buy-now confirmation.
- `GLAND`: raw BUY at `2370`, strategy `big_runner_ignition`, raw label `ENTRY_READY`; six zero-quantity PAPER follows were created and exited.
- `SIGMAADV`: PAPER follows created with qty `32`, then exited as `active_follow_hard_blocked`.
- `BAJAJCON`: PAPER follows created with qty `26`, then exited as `active_follow_severe_risk_flags`.
- `GOLDIAM`: only non-active India BUY idea in the last 72h; expired with peak `+4.161%`, but current lifecycle did not convert it to a clean active follow.

## Missed Movers

Latest India missed-move review:

- Review id: `1576`
- Reviewed movers: `47`
- Status counts:
  - `absent_from_prior_watchlist`: `33`
  - `correctly_watched_before_move`: `10`
  - `correctly_avoided_late_chase`: `2`
  - `caught_same_cycle`: `1`
  - `low_quality_watch_before_move`: `1`

Important missed examples:

- `AEGISLOG`: `+19.06%`, 52-week high + price shocker + top gainer + volume shocker, volume multiplier `27.96`, absent from prior watchlist.
- `AARVI`: `+5.57%`, top gainer + volume shocker, absent.
- `ABDL`: `+6.60%`, strong intraday gain + top gainer, absent.
- `AMANTA`: `+5.73%`, all-time high + top gainer, absent.
- `APARINDS`: `+3.40%`, 52-week high + all-time high + volume shocker, absent.

US missed-move review is better but still not clean:

- Latest US review id: `1629`
- Reviewed movers: `5`
- `correctly_watched_before_move`: `1`
- `caught_same_cycle`: `4`

## What Is Wrong

P0 issues:

1. Raw BUY authority is still too permissive. It emits `ENTRY_READY` for watch strategies such as `extended_momentum_watch`.
2. Raw BUY and follow truth are inconsistent. The system creates paper follows and then immediately exits them as hard-blocked, severe-risk, or not-entry-ready.
3. Zero-quantity paper follows still exist. `GLAND` produced six zero-qty PAPER follows today.
4. Missed-move learning is diagnostic only. It records that 33 of 47 India movers were absent, but it does not yet force tomorrow/pre-open detection improvements.
5. India market regime is stale/misleading after close. `checked_symbols=0` should not produce `risk_off`.
6. Latest diagnostics are overwritten by the current market cycle. The UI/state does not preserve the last India open-cycle funnel separately from the latest US cycle.
7. Rally Plan is still mostly watch-only. It shows candidates and levels, but it does not reliably promote clean ignition entries before or at the move.
8. Current active BUY ideas are zero, but the system still generated many raw BUYs and paper follows recently. That means the safety layer is blocking damage, while the upstream alpha layer remains noisy.

## Fix Goal

The next implementation goal should be:

Make the signal lifecycle atomic and truthful: no raw `BUY`, signal `BUY`, or paper follow can be created unless the same candidate passes entry authority, market regime, tradeability, quantity/economics, and hard-risk checks at the same time.

Required fixes:

1. Block raw `ENTRY_READY` for watch-only strategy names unless a separate buy-now confirmation exists.
2. Move hard/severe follow blockers before paper-follow creation, not after creation.
3. Reject zero-quantity follows before insert; record a skipped follow audit instead.
4. Add a per-market diagnostics history table or state key so India open-cycle evidence is not overwritten by US cycles.
5. Change closed/no-data market regime from `risk_off` to `market_closed` or `no_live_regime`.
6. Feed missed-move review output back into pre-catalyst/rally-plan scoring, especially for 52-week high + top gainer + volume shocker combinations like `AEGISLOG`.
7. Add a lifecycle audit view for each signal: raw decision, signal idea conversion, quality gate, follow decision, exit decision, and reason.
8. Add tests proving that watch strategies cannot create auto-followable BUYs, zero-qty follows are impossible, and hard-blocked follows are skipped before insertion.

## Verdict

OCI is operationally safer than before because broker orders are zero and active BUY ideas are zero. But the signal engine is not yet live-trading ready. It is still generating noisy raw BUYs, creating bad paper follows, immediately cleaning them up, and missing too many India movers before they run.
