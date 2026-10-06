# E. Blocker/Critical repairs and remaining integration recipes

Revalidated against local `b588ffd` plus the recorded audit changes; production remains `2b300b4`. **TRADING BEHAVIOUR CHANGED locally; no deployment or production book/protocol reset.** The current register and tests identify implemented repairs. Pseudocode below is a target contract and does not imply that native protection, canonical migration or legal approval already exists.

## F01 — explicit approved capability route

**Current local state: Missing capability.** No repair claimed. Targeted source revalidation retains this finding; missing runtime/external evidence is not inferred from passing tests. Remaining: Create a versioned strategy/instrument/broker capability registry; connect only independently approved stock and index contracts to the shared paper/live path.

```python
def accept_plan(account, plan, mode):
    spec = instruments.current(plan.instrument_id)
    route = capabilities.resolve(account.broker, spec.segment, spec.kind,
                                 plan.product, plan.order_type, mode)
    require(route.supported, "UNSUPPORTED_CAPABILITY")
    require(plan.model_version in promotion_registry.approved_for(spec, mode),
            "MODEL_NOT_PROMOTED")
    require(plan.evidence_available_at <= clock.now(), "FUTURE_EVIDENCE")
    require(plan.expires_at > clock.now(), "PLAN_EXPIRED")
    return canonical_entry(account, plan, route, mode)
```

Build validated stock models and their adapters separately. An index level has `tradable=False`; exposure must map to an actual approved ETF/future/option contract. Publishing ten research alternatives is not funding ten simultaneous positions.

**Acceptance:** stock, ETF and subsequently certified derivative fixtures pass through the same entry/risk/intention path; every unsupported combination refuses explicitly.

## F02 — preserve the position's exit policy

**Current local state: Locally repaired.** Monthly exit policy is persisted and the five-session regression is repaired. Remaining: Deployment and relevant operational certification; no production repair claimed.

```python
@dataclass(frozen=True)
class ExitPolicy:
    version: str
    initial_stop: Decimal | None
    target_rules: tuple
    trailing_rule: dict | None
    max_hold_sessions: int | None  # None = no time stop
    regime_exit_rule: str | None
    expiry_cutoff: datetime | None

# Persist the exact policy when accepting a plan, not just sleeve name.
# A zero/no-time-stop candidate must map explicitly to None.
policy = position.exit_policy
if observed_tradeable_mark <= policy.effective_stop(position.observed_peak):
    request_exit(position.id, "STOP", observed_tradeable_mark)
elif policy.target_hit(observed_tradeable_mark):
    request_exit(position.id, "TARGET", observed_tradeable_mark)
elif policy.max_hold_sessions is not None and held_sessions >= policy.max_hold_sessions:
    request_exit(position.id, "TIME", observed_tradeable_mark)
elif policy.regime_or_expiry_invalidated(current_context):
    request_exit(position.id, "INVALIDATION", observed_tradeable_mark)
```

Use ordered observations; no cumulative high/low crossing without event order. Current open-policy backfill must use the originating version, with unknown policy quarantined rather than guessed. Re-run the monthly ETF replay including actual risk sizing; the memory fixture requested 50% but received 30%.

**Acceptance:** no five-session time exit for the monthly policy; replay/paper agree on exits, quantities and costs.

## F03 — one account risk contract for every new entry

**Current local state: Locally repaired.** Personal and managed broker entries use serialized cost-aware account risk. Native margin and all-route certification remain separate. Remaining: Deployment and relevant operational certification; no production repair claimed.

```python
with ledger.serialized_account_transaction(account.id):
    snapshot = ledger.account_risk_state(account.id, account.epoch, fresh_marks)
    require(snapshot.complete, "VALUATION_UNKNOWN")
    allocation = risk.allocate(plan, snapshot, instrument_spec, cost_schedule)
    require(allocation.ok, allocation.reason)
    intent = ledger.reserve_entry(
        stable_key=(account.id, account.epoch, plan.id, plan.version, "ENTRY"),
        cash=allocation.cash_including_fees,
        loss=allocation.stop_loss_including_costs_and_slippage,
        margin=allocation.margin,
        quantity=allocation.quantity,
    )
# Broker HTTP occurs AFTER the durable transaction commits.
outbox.enqueue(intent.id)
```

Mandatory checks: loss/drawdown, pending exposure, aggregate and correlated risk, funds/margin, lot/freeze/tick, source/session/news eligibility and explicit manual/auto policy. No manual branch bypasses account brakes.

**Acceptance:** halted-account and simultaneous mirror/manual reproducers refuse safely.

## F04 — independent personal exit obligations

**Current local state: Locally repaired.** Personal exits are evaluated independently of house membership and subscription. Remaining: Deployment and relevant operational certification; no production repair claimed.

```python
for obligation in ledger.open_exit_obligations():
    position = ledger.position(obligation.position_id)
    mark = quotes.fresh_executable(position.instrument_id)
    if mark is None:
        alerts.raise_once(position.id, "PROTECTION_MARK_UNKNOWN")
        continue  # no fabricated fill; resident protection is checked independently
    action = exit_policy.evaluate(position, mark, venue_calendar)
    if action:
        canonical_exit(position.account_id, position.id, action)
```

Select all personal and managed positions, including lapsed subscriptions and holdings whose originating house trade has closed. Paper and broker adapters implement the same obligation lifecycle with different evidence for fills.

**Acceptance:** a personal-only holding exits under its own policy.

## F05 — position identity, not ticker fanout

**Current local state: Locally repaired.** House mirrors close only the exact source-position origin. Unknown ownership is refused. Remaining: Deployment and relevant operational certification; no production repair claimed.

```python
def mirror_exit(origin_position_id, action):
    mirrors = ledger.positions_by_origin(origin_position_id)
    for p in mirrors:
        require(p.origin_position_id == origin_position_id, "ORIGIN_MISMATCH")
        require(p.epoch == ledger.active_epoch(p.account_id), "EPOCH_MISMATCH")
        canonical_exit(p.account_id, p.id, action)
```

Do not attach an existing manual position to a house origin merely because its ticker matches. Historical mapping requires explicit unambiguous evidence.

**Acceptance:** the F05 manual holding remains open while an actual linked mirror closes.

## F06 — entry consent and exit authority are distinct

**Current local state: Locally repaired.** Entry disarm retains managed exit authority while credentials are valid. Token expiry still requires an explicit operational incident and native protection. Remaining: Deployment and relevant operational certification; no production repair claimed.

```python
def can_submit(account, intent):
    if intent.increases_risk:
        return account.entry_armed and not account.entry_kill and preflight.ready_for_entry
    require(intent.position_id in account.authorised_managed_positions, "NOT_OWNED")
    require(intent.qty <= reconciled_remaining_qty(intent.position_id), "OVERSELL")
    return preflight.can_manage_existing_position  # credentials, venue, permitted exit type

def disarm(account):
    ledger.set_entry_armed(account.id, False)
    protection.reconcile_existing(account.id)
```

An explicit emergency workflow may separately request cancellation/flattening; it must retain unresolved ownership and report confirmed outcomes. Expired/revoked credentials cannot be magically bypassed: preserve obligations and alert the owner to use their broker.

**Acceptance:** normal disarm never silently removes protection.

## F07 — exposure-priority hot quotes

**Current local state: Locally repaired.** Held-symbol hot feed includes personal, pending and residual exposure. Remaining: Deployment and relevant operational certification; no production repair claimed.

```python
hot = (
    house_positions.instrument_ids
    | personal_positions.instrument_ids
    | managed_broker_positions.instrument_ids
    | pending_orders.instrument_ids
    | protection_orders.instrument_ids
    | monitored_research.instrument_ids
)
feed_scheduler.request(hot, priority="EXPOSURE_FIRST", rate_budget=provider_budget)
health.check_each_exposure_quote_age(hot, regular_session_sla)
```

Deduplicate by instrument identity; include all spread legs and expired-but-unreconciled obligations. Do not treat an unknown query/feed failure as an empty set.

**Acceptance:** a sole personal/manual holding remains fast-polled outside research.

## F08 — resident protective orders from confirmed fills

**Current local state: Missing capability.** No repair claimed. Targeted source revalidation retains this finding; missing runtime/external evidence is not inferred from passing tests. Remaining: Implement broker-supported SL/GTT protection tied to confirmed filled size, with amendment/cancellation reconciliation and unprotected-position escalation.

```python
on_confirmed_fill(fill):
    ledger.apply_monotonic_fill(fill)
    position = ledger.managed_position(fill.position_id)
    spec = capabilities.protection_for(position)
    if not spec.supported:
        ledger.mark_unprotected(position.id, "UNSUPPORTED_PROTECTION")
        alerts.raise_once(position.id, "UNPROTECTED_POSITION")
        prohibit_further_entries(position.account_id)
        return
    target_qty = position.confirmed_remaining_qty
    protection.reconcile_then_submit_or_amend(
        stable_key=(position.id, "PROTECTION", position.policy.version),
        qty=target_qty, stop=position.policy.stop, adapter=spec,
    )
```

Do not create a “protected” badge from a submitted request. Reconcile broker acceptance/quantity/status; detect rejected/cancelled protective orders. Long protective legs must precede any short spread exposure.

**Acceptance:** outages/partial fills/cancel-versus-fill races leave no silent unprotected or oversold state.

## F09 — actual broker reconciliation

**Current local state: Partially repaired locally.** Actual positions, holdings, trades and funds are reconciled before managed entry, with account-specific incidents. Remaining: Real-account, overnight settlement, actual fees and protection reconciliation are not certified.

```python
snapshot = adapter.read_orders_trades_positions_holdings_funds()
with ledger.serialized_account_transaction(account.id):
    ledger.apply_owned_confirmed_fills(snapshot.trades)
    mismatches = reconcile(ledger.managed_ownership, snapshot.actual_inventory)
    ledger.record_reconciliation(snapshot.reference, mismatches)
    if mismatches:
        ledger.block_new_entries(account.id, "BROKER_INVENTORY_MISMATCH")
        alerts.enqueue_reconciliation_incident(account.id, mismatches)
# Never sell/adopt a broker asset because it exists in the account.
# Resolve manual/external changes via an explicit ownership adjustment event.
```

Stream updates are acceleration, not the only source of truth. Startup, periodic and gap recovery must include old-day trades that today's order list cannot establish.

**Acceptance:** an external sell reduces permissible managed sale quantity without adopting unrelated holdings.

## F10 — instrument ID migration

**Current local state: Partially repaired locally.** Dated canonical instrument discovery and ambiguity/lot/tick/freeze checks exist; canonical ID lookup is now public-read API capable. Remaining: House, personal and journal ownership still contain ticker keys and a legacy NSE fallback; effective restrictions/actions/calendars and migration quarantine are incomplete.

```sql
-- Proposed schema only; do not run directly against production.
CREATE TABLE instrument (
  id UUID PRIMARY KEY,
  venue TEXT NOT NULL,
  segment TEXT NOT NULL,
  venue_key TEXT NOT NULL,
  series TEXT NOT NULL,
  kind TEXT NOT NULL,
  UNIQUE (venue, segment, venue_key)
);
-- All quotes/plans/positions/intents reference instrument.id.
-- symbol is display/search metadata, never a globally unique key.
```

Snapshot before migration; populate venue/series/contract identity and broker bindings; emit an ambiguity report. Backfill foreign references only when unambiguous, dual-read and reconcile, then remove ticker-only uniqueness. No ambiguous position or contract can trade.

**Acceptance:** NSE/BSE duplicate-ticker and same-underlying expiry/strike fixtures remain independent.

## F11 — atomic cash and fill postings

**Current local state: Partially repaired locally.** House close/outbox commits are atomic; personal paper cash movements now have immutable balanced postings in the same transaction. Remaining: This is not the complete live settlement/fees/margin ledger or canonical all-adapter reservation pipeline. Historical ambiguity is not silently backfilled.

```python
with ledger.serialized_account_transaction(account.id):
    require(intent.reservation_valid, "RESERVATION_REQUIRED")
    require(event.fill_id not in ledger.applied_fills, "DUPLICATE_FILL")
    ledger.post(fill_quantity_delta, execution_value, actual_or_reserved_fees,
                currency, settlement_date, position_id, intent_id)
    ledger.adjust_pending_reservation(intent.id, remaining_quantity)
    ledger.assert_cash_equity_position_invariants(account.id)
```

Use fixed precision/Decimal and broker-specific quantity denomination. Futures variation margin, short-option obligations and US FX/settlement need distinct postings, not reused equity cash subtraction. Never await a network request inside a DB writer lock.

**Acceptance:** paise-level reconciliation and no negative available cash under concurrent/partial actions.

## F12 — explicit epoch and persistent high-water state

**Current local state: Locally repaired.** Explicit epochs and persistent high-water observations prevent implicit resizing and chart pruning from resetting risk. Remaining: Deployment and relevant operational certification; no production repair claimed.

```python
def ensure_book(account):
    return ledger.get_or_create_once(account.id, approved_initial_capital)
    # Never reconcile an existing allocation to a module default here.

def record_valuation(account, valuation):
    with ledger.serialized_account_transaction(account.id):
        ledger.append_valuation(account.epoch, valuation)
        ledger.update_peak_max(account.epoch, valuation.equity)
        ledger.persist_session_baseline_if_needed(account.epoch, valuation)
# A user-requested reset is an explicit new epoch; legacy postings are retained.
# Chart retention cannot delete the peak/session source of truth.
```

**Acceptance:** pruning, daily replacements and configuration changes cannot change loss history or drawdown limits.

## F13 — unknown calendar refuses entries

**Current local state: Locally repaired.** Manual entry calendars and required-component checks fail closed. Remaining: Deployment and relevant operational certification; no production repair claimed.

```python
try:
    session = venue_calendar.current(instrument.venue, instrument.segment, now)
except CalendarUnavailable:
    return Refusal("MARKET_STATE_UNKNOWN")
if not session.permits(intent.order_type, intent.product, intent.increases_risk):
    return Refusal("MARKET_CLOSED_OR_PHASE_UNSUPPORTED")
```

No fallback to “cannot tell, permit”. Existing exposed positions retain separate protection/escalation.

**Acceptance:** exceptions, holidays, half days and segment mismatches create no entry fills.

## F14 — retire alternate mutation routes

**Current local state: Locally repaired.** Legacy HTTP entry controls and bridge entry paths are retired at their boundaries. Remaining: Deployment and relevant operational certification; no production repair claimed.

```python
@app.post("/api/control/run-once")
def legacy_control(...):
    raise HTTPException(410, "Legacy entry engine retired; use approved execution workflow")
# Apply to legacy start, bridge cycle, reset/strategy mutation routes as appropriate.
# Startup and worker registrations reference only the canonical engine.
```

Preserve authenticated historical reads and unrelated admin/account management. Enumerate all mutation routes from inventory and test them; a startup environment flag is not a route guard.

**Acceptance:** direct HTTP/bridge requests cannot restart old strategies or modify a competing ledger.

## F15 — remove untrusted HTML sinks

**Current local state: Locally repaired.** External filing text is escaped or written with textContent; injected-text regression passes. Independent full-browser security review remains pending. Remaining: Deployment and relevant operational certification; no production repair claimed.

```javascript
const subject = document.createElement('div');
subject.className = 'mut';
subject.textContent = String(filing.subject ?? '');
const row = document.createElement('button');
row.type = 'button';
row.addEventListener('click', () => openInstrument(filing.instrument_id));
row.append(subject);
container.replaceChildren(...safeRows);
```

If a legacy string renderer is temporarily retained, escape text and attributes separately and validate identifiers; text escaping does not secure JavaScript embedded in HTML handlers. Review news, catalyst, API error and admin data sinks. Introduce CSP through a staged nonce/external-script migration, not a policy that breaks existing protection controls.

**Acceptance:** an inert synthetic HTML/event-handler payload is literal text; keyboard use remains supported.

## F16 — commercial scope is an explicit release gate

**Current local state: Unverified requirement.** No repair claimed. Targeted source revalidation retains this finding; missing runtime/external evidence is not inferred from passing tests. Remaining: Obtain written qualified classification and broker/provider arrangement; gate paid recommendations/automated live features by the approved scope and terms.

```python
profile = approvals.commercial_profile(product_version, broker, market)
require(profile.qualified_review_complete, "COMMERCIAL_SCOPE_UNAPPROVED")
require(profile.allows(service_kind, execution_mode), "SERVICE_NOT_APPROVED")
require(profile.required_broker_arrangement_verified, "BROKER_ROUTE_UNAPPROVED")
require(profile.client_terms_accepted, "TERMS_REQUIRED")
# Existing safety/reconciliation obligations are never lost when access expires.
```

This code cannot decide whether RA/IA registration is legally required. The documented legal/broker review must determine the classification, hosting/tagging/data obligations and approved service scope. Do not treat transparent scoring as a blanket exemption.

**Acceptance:** unsupported paid recommendation/automated-live services remain unavailable; approved scope and evidence are reviewable.

## Resumed local implementation versus remaining Critical acceptance

F01 now has a complete **synthetic/manual approved NSE-stock paper lifecycle**, with immutable plan/epoch identity and exact owned risk/fill/close/P&L; it does not establish automatic stock alpha or multi-asset coverage. F08 now has explicit-activation native transport, durable stop obligations, cancellation/trigger/unknown/partial/re-entry recovery and owned incidents. F09/F11 gain atomic immutable journal observations; actual broker fees/settlement remain unfinished. F10 has decision-time contract/session restrictions for the new approved path. F43 now fences late acknowledgements/cancellation/status writes after worker takeover. F44 checks exact live authorization at the journal boundary. F41 adds encrypted disabled restoration and stops source equity pruning. None was deployed.

Operational commands and limitations are in `docs/recovery-and-approved-paper.md`; invariant tests are `test_approved_execution`, `test_approved_order_api`, `test_execution_contracts`, `test_execution_events`, `test_protection_lifecycle`, `test_live_release`, `test_recovery_bundle`, and `test_account_ui`. Existing remaining recipes above still apply where full canonical migration, adapter convergence, actual fee/settlement accounting and independent model/legal/data/broker certification are absent. Do not mark those acceptances passed from these fixtures.

## Current concrete deltas — 7 October

F01/F03/F10/F11/F18: legacy manual paper entry now freezes immutable owner/epoch/request/plan levels and uses the same approved fill pipeline. All three entry writers enforce canonical dated contracts and the unchanged regime/risk rules. New actual catalogue integration/concurrency/fault tests do not replace risk or ledger code with mocks.

F08/F09: exact reviewed native policy handles new canonical terminal fills; a partial remainder is cancelled once and waits for actual terminal evidence. Actual owned broker trade IDs enter balanced append-only cash postings before inventory reconciliation; complete final net fees/margin/settlement and GTT completion remain unimplemented acceptance.

F54: reproduced on exact `8abd708` method in temporary SQLite: approved request status committed before access, then a fault left no expiry. `Database.decide_plan_request` now checks pending state and commits immutable unique verified manual receipt, expiry/access and status within `BEGIN IMMEDIATE`; any fault rolls back all. Tests cover two reviewers and reused bank reference. [Reproduction](billing-safety-reproduction.json). This is an actual local repair, not payment-gateway integration or a deployed result.

Critical remaining broker sequence (precise pseudocode, **not implemented/certified**): persist approved canonical broker plan and account reservation → transmit once → ingest stable actual trade IDs → attach exact owned lots/protection → obtain actual final assessments and cash/holding settlement → reconcile gross/net cash and margin independently → complete/amend GTT only from terminal child/fill evidence → append adjustments with source and ownership → release reservations only after confirmed terminal outcome. Never fill missing fee/margin/settlement with zero or replay an ambiguous transmission.
