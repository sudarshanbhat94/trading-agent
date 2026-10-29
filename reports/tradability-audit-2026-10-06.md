# OpenStocks tradability audit — 6 October 2026

The previous short list was incomplete. OpenStocks currently has a working research shortlist and tracker, an isolated clean personal paper book, and some durable broker-order handling. It does **not** yet have a consistent, validated route from a good individual-stock idea to a risk-approved fill, protected position and independently managed exit.

This audit identifies **36 implementation and validation work items**, not 36 proven incidents or an exhaustive security certification. Items marked **confirmed defect** were traced in code or reproduced; **missing capability** describes an absent route; **validation required** means the current evidence cannot support the advertised behaviour. Nothing was enabled, reset, traded or deployed during this audit.

## Live evidence and why there are zero trades

Read-only production snapshot at **09:27:46 IST, 6 October 2026**, deployed commit `2b300b4`:

- Sudarshan: capital ₹10,000; cash ₹10,000; equity ₹10,000; current-epoch realised P&L ₹0; open positions 0.
- Research tracker: 38 publication versions across 19 distinct stocks; 33 versions waiting; five hypothetical zone touches; two resolved stops; no T1/T2/T3 hits or profitable resolved scenarios.
- APLAPOLLO hypothetical net −₹133.53 (−1.0337R); JINDALSTEL hypothetical net −₹147.94 (−1.0262R). These are **separate conditional scenarios**, not actual paper trades, independently funded portfolio results, or 38 independent stock observations.
- Tracker last poll 09:27:18 IST: all 18 active distinct symbols fresh, no missing symbols, no confirmation error.
- Earlier 09:19:45 IST production inspection: app, quote feed and research timers active; master regime OFF from completed 5 October data; execution drawdown/daily-loss halt **false**.

The idle paper book is chiefly an architectural/model issue: `PRODUCTION_SLEEVES` contains only `index_directional`, which proposes **NIFTYBEES only**, in ON regime, on a monthly review or one fresh-book bootstrap. Quality momentum is observation-only; the stock research shortlist is not an approved execution source. The broker-mirror allowlist excludes even this sole production sleeve. Opening NSE does not make any of those restrictions disappear.

Sources: [production allowlist](/Users/pavithramayya/Documents/Sudarshan/trading-agent/app/sleeves/config.py:19), [ETF rule](/Users/pavithramayya/Documents/Sudarshan/trading-agent/app/sleeves/index_directional.py:28), [paper production pass](/Users/pavithramayya/Documents/Sudarshan/trading-agent/app/v2_live.py:4223), [broker allowlist](/Users/pavithramayya/Documents/Sudarshan/trading-agent/app/live_trade.py:43).

## Reproduced defects

A local in-memory SQLite demonstration used no production book, disk mutations or network:

1. A ₹10,000 account with a ₹200 daily loss was correctly halted by `RiskManager` at its 1.5% limit. Calling the personal paper-book manual buy still purchased **33 test shares at ₹100**.
2. That manually opened position had `strategy=manual` and no house `src_id`. A house mirror exit for the same symbol nevertheless closed it.
3. A monthly ETF candidate declares `max_hold_days=0`. With an unchanged ₹100 price, no stop/target event and five held sessions, production `evaluate_exit()` returned **exit ₹100, reason time** because `HOLD_DAYS["index_directional"]=5`.

The third defect is especially consequential: a monthly trend replay cannot validate a production position that receives a five-session legacy time exit. The existing ETF evidence must be rechecked against the corrected executable contract.

## P0 — make the execution and book safe before adding stock entries

### 1. Preserve the researched exit contract — confirmed defect
The monthly ETF rule's no-time-stop contract is overridden by the five-session runtime exit. Candidate `max_hold_days` is not passed into the position writer.
**Change:** persist a versioned exit policy per position and execute that policy; reconcile monthly regime exits and disaster stops explicitly.
**Acceptance:** the same frozen event stream yields identical exits and after-cost returns in replay and production paper. Rerun the ETF evidence before retaining any performance claim.
Evidence: [candidate exit policy](/Users/pavithramayya/Documents/Sudarshan/trading-agent/app/sleeves/index_directional.py:62), [legacy hold map](/Users/pavithramayya/Documents/Sudarshan/trading-agent/app/v2_live.py:3655), [runtime exit evaluator](/Users/pavithramayya/Documents/Sudarshan/trading-agent/app/v2_live.py:3758), [candidate writer](/Users/pavithramayya/Documents/Sudarshan/trading-agent/app/v2_live.py:4444).

### 2. Apply one risk decision to the actual account — confirmed defect
The house allocator is cost-aware, but personal manual buys, subscriber mirrors and broker sizing use different checks. A house-approved quantity does not prove that an already depleted or manually invested user's account can safely take it.
**Change:** obtain an account-specific risk snapshot and reservation for every entry route, including approved auto, discretionary manual and mirrors. Explicitly define whether manual entries may override a strategy gate; never let them override cash, loss or aggregate-risk limits.
**Acceptance:** no route opens new risk after its account's loss/drawdown brake; simultaneous requests cannot exceed the shared account limits.
Evidence: [personal buy](/Users/pavithramayya/Documents/Sudarshan/trading-agent/app/books.py:248), [subscriber entry mirror](/Users/pavithramayya/Documents/Sudarshan/trading-agent/app/books.py:431), [broker sizing](/Users/pavithramayya/Documents/Sudarshan/trading-agent/app/live_trade.py:232), [house risk manager](/Users/pavithramayya/Documents/Sudarshan/trading-agent/app/sleeves/risk.py:85).

### 3. Independently manage personal paper exits — missing capability
The fast exit monitor selects `v2_positions` only. Personal manual holdings reside in `user_positions` and have no independent equivalent stop/target/time monitor.
**Change:** maintain exit obligations for each personal position, regardless of subscription changes or whether the house holds the symbol.
**Acceptance:** a manual personal stop, target and time invalidation close exactly the owning position on the next eligible observed price.
Evidence: [house-only exit monitor](/Users/pavithramayya/Documents/Sudarshan/trading-agent/app/v2_live.py:3853), [personal position schema](/Users/pavithramayya/Documents/Sudarshan/trading-agent/app/books.py:51).

### 4. Link house exits to the exact mirrored position — confirmed defect
`books.mirror_exit()` closes every personal holding of the same ticker, including independently opened manual positions.
**Change:** require originating position/plan identity and epoch, with explicit handling of partial or independently modified holdings.
**Acceptance:** closing a house position leaves unrelated manual positions and other epochs untouched.
Evidence: [symbol-only exit fanout](/Users/pavithramayya/Documents/Sudarshan/trading-agent/app/books.py:448).

### 5. Separate entry disarming from exit obligations — confirmed defect
Live service, mirror exits and journal submission all require `live_ready`. Disarming or engaging the current kill switch therefore also disables managed exits.
**Change:** distinguish “disable new entries”, “continue protection” and an explicit emergency workflow. Invalid credentials must trigger an actionable manual handoff, not silent protection loss.
**Acceptance:** disarming prevents buys while existing positions retain supported exit/protection management; no unauthorised account is touched.
Evidence: [mirror exit gate](/Users/pavithramayya/Documents/Sudarshan/trading-agent/app/live_trade.py:269), [service gate](/Users/pavithramayya/Documents/Sudarshan/trading-agent/app/live_trade.py:333), [journal gate](/Users/pavithramayya/Documents/Sudarshan/trading-agent/app/order_journal.py:123).

### 6. Keep every exposed instrument in the fast quote set — confirmed coverage gap
The feed's held-symbol query reads only house positions. Personal manual holdings, managed broker residuals and pending/protective orders can fall outside the hot set unless independently watched by research.
**Change:** construct the hot set from all account position, pending-order and protection obligations, with bounded batching and priority.
**Acceptance:** a stock held only by a personal account remains within the execution freshness SLA; full-universe five-minute polling cannot be its only protection feed.
Evidence: [held-symbol query](/Users/pavithramayya/Documents/Sudarshan/trading-agent/scripts/v2_quote_feed.py:53).

### 7. Add broker-resident protection and verify it — missing capability
Current managed stop/target protection is local SQLite plus a roughly minute-spaced service pass. The order wrapper always supplies a zero trigger price; no production path installs a broker-resident stop.
**Change:** use instrument-supported SL/GTT protection, reconcile its confirmed size/status, amend after partial fills, and cancel conflicting exits safely. Treat rejection or absence of protection as a critical state.
**Acceptance:** the supported protective order remains visible and reconciled through a simulated app outage; neither an exit nor a restart causes an oversell.
Evidence: [local protection service](/Users/pavithramayya/Documents/Sudarshan/trading-agent/app/live_trade.py:333), [order payload](/Users/pavithramayya/Documents/Sudarshan/trading-agent/app/broker.py:550). Broker capabilities must be verified against the current [Upstox order/GTT documentation](https://upstox.com/developer/api-documentation/get-portfolio-stream-feed/); native protection still has venue and execution failure risks.

### 8. Reconcile actual broker inventory, not only app orders — missing capability
The durable journal recognises owned tags/order IDs and confirmed fills. External trades, holding changes and corporate actions do not update that managed inventory. A UI account snapshot is not ledger reconciliation.
**Change:** compare managed ownership with actual broker positions/holdings/funds; quarantine mismatches and resolve them without adopting or selling unrelated assets. Consume portfolio updates with a polling fallback.
**Acceptance:** an external reduction cannot cause the app to sell more managed shares than remain; unknown reconciliation blocks new entries.
Evidence: [owned-order reconciliation](/Users/pavithramayya/Documents/Sudarshan/trading-agent/app/order_journal.py:41). [Upstox portfolio stream](https://upstox.com/developer/api-documentation/get-portfolio-stream-feed/) supports orders, holdings and positions, including updates from other platforms.

### 9. Make cash reservations and fill accounting consistent — confirmed accounting gap
Personal paper cash subtracts entry notional and adds net closed-trade P&L; entry charges are not reserved on entry, and open P&L is gross. Personal buy check/insert is not one serialized account reservation.
**Change:** book/reserve costs and actual fill legs consistently, maintain available versus committed cash, and serialize competing personal writes. Preserve the already fixed published fee schedule.
**Acceptance:** cash + marked holdings reconciles to equity and ledger costs after partial fills, cancellations and concurrent buys; usable cash never becomes negative.
Evidence: [cash calculation](/Users/pavithramayya/Documents/Sudarshan/trading-agent/app/books.py:194), [buy transaction](/Users/pavithramayya/Documents/Sudarshan/trading-agent/app/books.py:248), [sale accounting](/Users/pavithramayya/Documents/Sudarshan/trading-agent/app/books.py:292).

### 10. Persist capital epochs and high-water marks explicitly — confirmed design defect
`ensure_book()` silently resizes an existing allocation to the default and reanchors its epoch. Personal equity replaces the day's row. House risk peak is derived from LIVE snapshots that retention deletes.
**Change:** make capital/epoch migration an explicit audited action; persist epoch peak independently of chart retention, alongside a durable session baseline. Reconcile UI readiness to production's cost-aware tactical/strategic risk.
**Acceptance:** changing configuration or pruning snapshots cannot hide P&L or lower the historical peak; a ₹10,000 reset preserves separately queryable legacy history.
Evidence: [automatic resize](/Users/pavithramayya/Documents/Sudarshan/trading-agent/app/books.py:77), [daily snapshot replacement](/Users/pavithramayya/Documents/Sudarshan/trading-agent/app/books.py:378), [production peak query](/Users/pavithramayya/Documents/Sudarshan/trading-agent/app/v2_live.py:4332), [pruning](/Users/pavithramayya/Documents/Sudarshan/trading-agent/scripts/backup_db.py:23), [readiness risk calculation](/Users/pavithramayya/Documents/Sudarshan/trading-agent/app/sleeves/readiness.py:24). Peak relaxation is a reproduced-code risk, not a claimed observed live loss.

### 11. Fail closed on unknown trading-critical state — confirmed defect
The manual market-session guard permits the action on exceptions. Health aggregation reports `ok=True` for unknown pipelines; quote-health allowance is much looser than execution freshness.
**Change:** explicitly block entries when calendar, required quote, risk, evidence or broker state is unknown; distinguish harmless closed-session ageing from actual active-session failure.
**Acceptance:** simulated calendar/feed/critical-job errors show a clear refusal and still preserve exit escalation.
Evidence: [manual session error handling](/Users/pavithramayya/Documents/Sudarshan/trading-agent/app/v2_web.py:1862), [job health policy](/Users/pavithramayya/Documents/Sudarshan/trading-agent/app/jobs_health.py:66), [unknown health aggregation](/Users/pavithramayya/Documents/Sudarshan/trading-agent/app/jobs_health.py:108).

### 12. Disable legacy control paths at the route boundary — confirmed reachable paths
Legacy startup loops are disabled by default, and the v2 loop does not call old entry passes. However authenticated legacy start/run-once and the bridge run-cycle routes still invoke the old agent, maintaining a second strategy/ledger path.
**Change:** retire their entry side effects or route them through the approved canonical execution contract. Retain separately labelled historical reads.
**Acceptance:** HTTP controls, bridge calls and startup flags cannot re-enable a second entry engine. Do not interpret current startup-disabled status as proof that no API can run it.
Evidence: [legacy start/run controls](/Users/pavithramayya/Documents/Sudarshan/trading-agent/app/main.py:5025), [bridge cycle](/Users/pavithramayya/Documents/Sudarshan/trading-agent/app/main.py:3585), [startup-only gate](/Users/pavithramayya/Documents/Sudarshan/trading-agent/app/main.py:918).

## P1 — build an executable, evidence-backed equity and index product

### 13. Give every actionable idea an immutable trade-plan identity — confirmed route mismatch
The manual buy API ignores the published idea's stop, targets and requested quantity, substituting ±6% and separate sizing.
**Change:** submit a plan ID/version, authorised execution mode and approved quantity; revalidate expiry, fresh price, bounds and account risk on the server.
**Acceptance:** submitted levels/size trace to the exact displayed plan; a changed, invalidated or research-only plan cannot silently become an order.
Evidence: [manual API](/Users/pavithramayya/Documents/Sudarshan/trading-agent/app/v2_web.py:1868), [manual broker action](/Users/pavithramayya/Documents/Sudarshan/trading-agent/app/manual_execution.py:6), [frozen research plans](/Users/pavithramayya/Documents/Sudarshan/trading-agent/app/screening/plans.py:1).

### 14. Connect the selected stock model through an explicit promotion registry — missing capability
The conditional stock shortlist and observation-only quality-momentum sleeve are distinct models. Research eligibility events are not production entry instructions.
**Change:** choose and version the executable model, connect its approved events to account risk and paper execution, and declare supported broker mappings explicitly.
**Acceptance:** each actual stock fill cites an approved model/plan and decision; production/observation/rejected statuses are visible. Adding a name to an allowlist alone is not validation.
Evidence: [engine observation separation](/Users/pavithramayya/Documents/Sudarshan/trading-agent/app/sleeves/engine.py:39), [confirmation assessment](/Users/pavithramayya/Documents/Sudarshan/trading-agent/app/screening/confirmation.py:28), [allowlists](/Users/pavithramayya/Documents/Sudarshan/trading-agent/app/sleeves/config.py:19), [broker mappings](/Users/pavithramayya/Documents/Sudarshan/trading-agent/app/live_trade.py:43).

### 15. Research the requested individual-stock strategies before promotion — validation required
Imported sleeve classes and a ranked watchlist are not a stock trading track record.
**Change:** build a simple liquid-stock executable hypothesis with entry, stop, time/trailing policy and documented economic rationale; test mean reversion, quality momentum and early momentum as separate candidates rather than combine unvalidated scores.
**Acceptance:** retain only models that pass preregistered after-cost untouched validation and subsequent independent forward paper evaluation. Publish failed candidates too.
Evidence: [current stock screen](/Users/pavithramayya/Documents/Sudarshan/trading-agent/app/screening/screen.py:88), [stock replay limitations](/Users/pavithramayya/Documents/Sudarshan/trading-agent/scripts/research_stock_replay.py:1), [forward protocol](/Users/pavithramayya/Documents/Sudarshan/trading-agent/app/screening/validation.py:23).

### 16. Validate the regime gate against the instruments and horizon — validation required
The master ETF trend gate dominates every production entry; available benchmark mode does not use breadth as a hard gate. A green intraday index alone does not test a completed-data trend model.
**Change:** study gate accuracy, opportunity loss and downside control by stock setup and market regime; evaluate any less restrictive or additional directional policy as a new version.
**Acceptance:** record opportunity funnels and after-cost outcomes in ON/NEUTRAL/OFF with matching timestamps. Do not force ON or bypass risk merely to manufacture trades.
Evidence: [regime calculation](/Users/pavithramayya/Documents/Sudarshan/trading-agent/app/sleeves/regime.py:1), [ETF monthly condition](/Users/pavithramayya/Documents/Sudarshan/trading-agent/app/sleeves/index_directional.py:28).

### 17. Build a point-in-time liquid universe and adjusted price history — validation required
Current verified NSE membership and a ₹25 crore turnover filter are useful. Current constituents alone cannot validate historical stock selection.
**Change:** retain dated Nifty 50/100/500 membership, entry-eligible turnover/depth, suspension/delisting history and corporate-action adjustments.
**Acceptance:** a historical selection uses only then-available membership and consistent adjusted prices; dropped names and losing selections remain in the sample.
Evidence: [universe and liquidity screen](/Users/pavithramayya/Documents/Sudarshan/trading-agent/app/screening/screen.py:88), [historical survivor warning](/Users/pavithramayya/Documents/Sudarshan/trading-agent/scripts/research_stock_replay.py:1).

### 18. Reconcile quality metrics to dated official filings — validation required
Secondary statements supply profitability/leverage/cash-flow and some earnings variability. They are explicitly not exchange-verified ratios; financial stocks lack verified sector-specific capital/asset-quality inputs.
**Change:** retain source filings, publication/availability times, restatements, quarterly development and industry-appropriate metrics. Reconcile secondary values rather than present them as official quality proof.
**Acceptance:** ROE/leverage/earnings-stability fields trace to a filing available before selection; missing financial-sector evidence is visible.
Evidence: [fundamental derivation](/Users/pavithramayya/Documents/Sudarshan/trading-agent/app/screening/providers.py:74), [sector quality exclusions](/Users/pavithramayya/Documents/Sudarshan/trading-agent/app/screening/screen.py:56).

### 19. Turn official news and events into structured risk/catalyst evidence — validation required
Official announcements, meeting dates and heuristic event flags already exist. Their predictive contribution has not been demonstrated.
**Change:** capture dissemination time, earnings surprise where supported, guidance/material risks and catalyst expiry; invalidate or reassess plans after material changes. Keep unknown news coverage distinct from “checked, no event”.
**Acceptance:** tests prevent use of future announcements; entry logs show the actual news context; performance ablation measures benefit before any weight change.
Evidence: [official event extraction](/Users/pavithramayya/Documents/Sudarshan/trading-agent/app/screening/providers.py:35), [screen evidence](/Users/pavithramayya/Documents/Sudarshan/trading-agent/app/screening/screen.py:88).

### 20. Validate session data and direct benchmark coverage continuously — validation required
Recent instrument/timestamp fixes and fresh current coverage do not reconstruct older sampling gaps.
**Change:** check per-instrument regular-session observations, completed bars, delivery data, bid/ask where required and matching direct index samples. Store availability and gaps without backdating evidence.
**Acceptance:** stale/future/mismatched data never creates a trade; relative performance uses matching observation windows, or says unavailable.
Evidence: [quote feed](/Users/pavithramayya/Documents/Sudarshan/trading-agent/scripts/v2_quote_feed.py:53), [tracker report and benchmark matching](/Users/pavithramayya/Documents/Sudarshan/trading-agent/app/screening/tracking.py:300).

### 21. Use one executable replay and a capital-constrained forward portfolio — validation required
Independent scenario outcomes, retrospective survivors and large-capital tests do not validate simultaneous ₹10,000 trading.
**Change:** make replay and paper consume the same entry/exit contract, exact cost assumptions and whole-unit allocator; preserve the already registered new forward cohort. Report uncertainty, failed models and untouched periods.
**Acceptance:** after-cost returns, drawdown, exposure and missed signals reconcile across paths; there is independent evidence beyond two resolved losing scenarios and overlapping plan versions.
Evidence: [registered forward comparison](/Users/pavithramayya/Documents/Sudarshan/trading-agent/app/screening/validation.py:55), [monthly ETF replay](/Users/pavithramayya/Documents/Sudarshan/trading-agent/scripts/research_index_fresh_start.py:66).

### 22. Measure and validate entry accessibility — validation required
The new pullback/rebound/next-session/original-zone confirmation may filter losses but also miss moves. Its present score is not a win probability.
**Change:** log shortlist → zone touch → completed confirmation → fresh eligible price → risk approval → fill; record timeouts, unreachable zones and adverse excursions.
**Acceptance:** compare proposed confirmation changes on untouched forward data with the same capital/portfolio rules. No outcome is removed because it lost or never touched.
Evidence: [confirmation policy](/Users/pavithramayya/Documents/Sudarshan/trading-agent/app/screening/confirmation.py:28), [frozen shortlist economics](/Users/pavithramayya/Documents/Sudarshan/trading-agent/app/screening/plans.py:59).

### 23. Add portfolio concentration controls — missing capability
Cash, position counts and sleeve limits exist, but correlated stocks can still consume the same economic risk.
**Change:** cap sector/group concentration and estimate overlapping exposure, including an ETF plus its constituent stocks; reserve overnight/event-gap risk.
**Acceptance:** a ten-stock research list is not presented as ten simultaneously affordable buys; the funded subset passes concentration and total-risk checks.
Evidence: [current allocator](/Users/pavithramayya/Documents/Sudarshan/trading-agent/app/sleeves/risk.py:109), [independent alternative plans](/Users/pavithramayya/Documents/Sudarshan/trading-agent/app/screening/plans.py:1).

### 24. Implement and validate genuine Nifty and Bank Nifty routes — missing capability
The active index sleeve is long-only NIFTYBEES; there is no active Bank Nifty or bearish directional route. The production paper writer routes EQ instruments only.
**Change:** implement separately validated directional contracts with honest tradable ETF/futures/defined-risk product mappings, explicit invalidation and time policies.
**Acceptance:** executable units, account affordability, timestamps and after-cost results are demonstrated for each promoted index route. Do not simulate an unfundable futures lot as an arbitrary cash-index quantity.
Evidence: [ETF-only candidate](/Users/pavithramayya/Documents/Sudarshan/trading-agent/app/sleeves/index_directional.py:1), [EQ-only routing](/Users/pavithramayya/Documents/Sudarshan/trading-agent/app/v2_live.py:4437).

### 25. Complete derivatives contract, margin and multi-leg safety — missing capability
An options-overlay proposal is not a routable funded spread. Protective-leg fills, actual premium, lot sizes, margin and expiry obligations need a separate execution implementation.
**Change:** load current instrument masters and venue calendars; price from executable quotes; acquire the protective leg before short exposure; compensate/escalate partial legging and reconcile expiry/roll/EOD obligations.
**Acceptance:** max loss and required funds reflect actual confirmed legs and margin; no naked short remains after failure.
Evidence: [overlay prototype](/Users/pavithramayya/Documents/Sudarshan/trading-agent/app/sleeves/options_overlay.py:1). Current [NSE contract specifications](https://www.nseindia.com/static/products-services/equity-derivatives-contract-specifications) list Nifty weekly and monthly products, with Bank Nifty monthly/quarterly products; expiry rules must not be hardcoded from obsolete assumptions. [Upstox multi-order](https://upstox.com/developer/api-documentation/place-multi-order/) can return partial success, so a batch is not an atomic spread.

### 26. Verify broker preflight and the commercial multi-account route — validation required
Per-user state files and owner checks exist. They do not prove token identity, sell authorisation, permitted egress or an approved platform arrangement.
**Change:** validate broker identity, token status, funds, delivery-sale rights, app type, instrument/product permissions and applicable Algo Name headers. Establish broker-supported egress for unrelated customer accounts rather than assume one shared retail-app IP works.
**Acceptance:** each account has documented, read-only preflight evidence; failures are displayed before arming, without exposing credentials.
Evidence: [broker account gates](/Users/pavithramayya/Documents/Sudarshan/trading-agent/app/broker.py:497), [order wrapper](/Users/pavithramayya/Documents/Sudarshan/trading-agent/app/broker.py:550). [Current Upstox API requirements](https://upstox.com/developer/api-documentation/announcements/algo-trading-circular/) describe user-level registered static IPs and conditional algo-app headers. The shared-server commercial architecture requires broker confirmation; this audit did not test customer order permissions.

### 27. Deduplicate by trading intent and test recovery — validation required
Persist-before-submit, UUID tags, reservations and monotonic partial-fill reconciliation are already implemented. A UUID per request is not a stable signal/user/epoch identity.
**Change:** bind intents to immutable plan + account + epoch + action; define unresolved-order escalation, restart recovery and replacement/cancellation semantics.
**Acceptance:** duplicate API deliveries and process restarts produce at most one intended position; uncertain broker responses are never blindly retried.
Evidence: [durable order submission](/Users/pavithramayya/Documents/Sudarshan/trading-agent/app/order_journal.py:123), [partial-fill reconciliation](/Users/pavithramayya/Documents/Sudarshan/trading-agent/app/order_journal.py:41).

### 28. Connect actionable failure alerts to actual execution failures — confirmed integration gap
The real-order failure helper exists but has no caller in the active journal path. Persisting an error row or logging a service failure is not owner notification.
**Change:** notify on rejected/unknown orders, inventory mismatch, missing protection and failed exits; retain acknowledgement, deduplication and escalation state.
**Acceptance:** each injected critical failure creates one owner-visible alert with the actual account, exposure and recovery action.
Evidence: [unused failure-alert helper](/Users/pavithramayya/Documents/Sudarshan/trading-agent/app/live_trade.py:305), [journal response handling](/Users/pavithramayya/Documents/Sudarshan/trading-agent/app/order_journal.py:175).

## P2 — make the product, reporting and operation truthful and usable

### 29. Consolidate the UI around one account and trade state — confirmed UI contradiction
The active Account renderer still advertises a ₹1,00,000 reset deleting history, while the reset confirmation describes a ₹10,000 new epoch with history retained.
**Change:** remove duplicate/stale renderers; derive capital, mode and permissions from canonical account state; show Research/Eligible/Submitted/Partial/Filled/Protected/Closed separately. Keep retired options history out of live capital.
**Acceptance:** desktop and mobile visibly agree with the ledger; an idea Buy uses its plan/size; watchlist and lifecycle actions work; an API acknowledgement is not shown as a fill.
Evidence: [active stale account copy](/Users/pavithramayya/Documents/Sudarshan/trading-agent/app/v2_web.py:6107), [reset confirmation](/Users/pavithramayya/Documents/Sudarshan/trading-agent/app/v2_web.py:6439), [manual buy route](/Users/pavithramayya/Documents/Sudarshan/trading-agent/app/v2_web.py:1868). Expanded current browser acceptance remains unverified.

### 30. Reconcile per-account performance by model, sleeve and regime — missing complete product view
House sleeve/regime reporting and research tracking already exist. Their figures cannot be mixed with personal books or actual broker P&L.
**Change:** derive daily/open/closed/net results from actual attributed fills, original risk and account epoch; show fees/slippage, partial exits, exposure, drawdown and matched benchmarks separately from hypothetical scenarios.
**Acceptance:** UI, downloadable report and ledger agree to the paise; winners, losers, inactive versions and gaps remain visible.
Evidence: [existing house report](/Users/pavithramayya/Documents/Sudarshan/trading-agent/app/sleeves/performance.py:164), [research tracker](/Users/pavithramayya/Documents/Sudarshan/trading-agent/app/screening/tracking.py:300), [personal ledger](/Users/pavithramayya/Documents/Sudarshan/trading-agent/app/books.py:292).

### 31. Verify subscription access through the entire lifecycle — validation required
Plans, expiry logic, personal books and Elite broker entitlement already exist; do not rebuild them as if absent.
**Change:** test purchase/renewal/admin approval/expiry and idempotent billing transitions against UI access and execution entitlements. Separate expiry's entry restriction from ongoing exit obligations.
**Acceptance:** payment/plan state cannot duplicate allocations, grant another user's account access or strand an existing position.
Evidence: [plan expiry and entitlements](/Users/pavithramayya/Documents/Sudarshan/trading-agent/app/plans.py:110), [entry versus safety entitlement](/Users/pavithramayya/Documents/Sudarshan/trading-agent/app/broker_access.py:1).

### 32. Verify security of every exposed route and credential store — validation required
Authentication, password hashing, secure-cookie handling, login throttling and public-repo secret checks exist.
**Change:** extend negative ownership/role tests across legacy/bridge/broker/export paths; verify CSRF/session/proxy boundaries, broker credentials at rest, file permissions, logs and revocation. Keep order-capable secrets out of client responses and public artifacts.
**Acceptance:** user A cannot inspect or operate user B's book or broker by changing an ID, using an old route or replaying a session; audit output contains no secrets.
Evidence: [session security](/Users/pavithramayya/Documents/Sudarshan/trading-agent/app/auth.py:186), [broker access](/Users/pavithramayya/Documents/Sudarshan/trading-agent/app/broker_access.py:1), [legacy controls](/Users/pavithramayya/Documents/Sudarshan/trading-agent/app/main.py:5025). This is an acceptance requirement, not a claim that all listed vulnerabilities were found.

### 33. Bound engine, API and SQLite work under load — validation required
The production engine runs in a web-process thread; heavy feature work and concurrent writers can affect response/exit latency. Routine feed cadences and timers already exist.
**Change:** measure cycle latency, queue lag and lock time; isolate heavy calculations where needed; keep bounded worker scheduling and explicit failure states. Preserve the deployed feed cadence until a measured change is justified.
**Acceptance:** peak-load/restart/DB-lock tests meet recorded quote and protection SLAs without duplicate engine owners or missed exits.
Evidence: [in-process engine startup](/Users/pavithramayya/Documents/Sudarshan/trading-agent/app/main.py:631), [production loop](/Users/pavithramayya/Documents/Sudarshan/trading-agent/app/v2_live.py:4542).

### 34. Back up and restore the whole source of truth — missing complete recovery
The automated backup covers the paper DB only, keeps local copies and prunes intraday equity before the backup.
**Change:** protect auth/account data, research publications/assessments, frozen protocols, execution ledger and model/config versions; add encrypted off-host retention and tested restore/migration/rollback. Never derive a durable risk peak from disposable chart rows.
**Acceptance:** restore a separate environment and reconcile balances, epochs, intentions, protected inventory and publication fingerprints without replaying orders.
Evidence: [current backup coverage](/Users/pavithramayya/Documents/Sudarshan/trading-agent/scripts/backup_db.py:1).

### 35. Add end-to-end acceptance evidence — validation required
Broad unit tests and mock-broker wire tests do not demonstrate the user journey or model profitability.
**Change:** replay controlled NSE sessions through the actual approved paper path; include multi-user concurrent requests, partial/rejected/unknown fills, stop gaps, token loss, feed outages, material news, restart and mobile UI flows.
**Acceptance:** a published approved stock plan produces an affordable paper fill and a correctly attributed exit; each negative case refuses safely with an explanation. Then validate broker contracts in the supported sandbox before any authorised careful live rollout.
Evidence: [wire tests](/Users/pavithramayya/Documents/Sudarshan/trading-agent/tests/test_broker_wire.py:1), [manual paper tests](/Users/pavithramayya/Documents/Sudarshan/trading-agent/tests/test_manual_buy_books.py:1). Existing browser verification showed nine current research cards and ₹10,000 cash; expanded Tracking/Portfolio verification was rejected by automatic approval review after a connection failure and a prohibited retry. It remains unverified; no workaround was used.

### 36. Complete the paid-product launch requirements — validation required
Subscription screens and a functioning app do not establish verified investment results or authorisation to offer every proposed research/execution service.
**Change:** document the applicable research/advisory and broker/algo-provider arrangement, data-use/redistribution rights, customer disclosures and retention requirements; substantiate public claims with the actual model/account evidence.
**Acceptance:** product wording, permissions and commercial setup match the verified scope before offering paid automated execution.
Primary references: [SEBI Research Analyst master circular, February 2026](https://www.sebi.gov.in/sebi_data/attachdocs/feb-2026/1770375507051.pdf), [NSE empanelled algo-provider framework](https://betanseapi.nseindia.com/static/trade/empanelled-algo-providers-exchange), [Upstox API requirements](https://upstox.com/developer/api-documentation/announcements/algo-trading-circular/). The applicable commercial classification was not determined in this engineering audit.

## Implementation order and completion gates

1. **Execution correctness:** resolve items 1–12, with deterministic reproductions and account-isolation tests. Persist exit/epoch identity and one risk contract before adding an equity route.
2. **Tradable equities:** connect one independently validated liquid-stock model through items 13–23, paper-first on the existing ₹10,000 epoch. Preserve research history and preregistered cohorts; do not reset losses to create a good record.
3. **Index/derivatives and broker readiness:** implement items 24–28 using real contracts, actual account affordability and confirmed protection. Instrument routes are individually promoted, not enabled together because classes exist.
4. **Product and operational release:** finish items 29–36 and visibly exercise the desktop/mobile lifecycle and recovery procedures. Start UI/accounting repairs alongside phase 1 where independent.

Changes to exits, risk, entries or strategy gates alter trading behaviour and must be explicitly reported as **TRADING BEHAVIOUR CHANGED** when implemented. This audit itself changes no trading behaviour.

## Existing work worth retaining

- Per-user paper tables, explicit epochs, per-user broker file paths and Elite access gates.
- Cost-aware house risk allocation and the already corrected fee schedule.
- Monotonic quote timestamps and strict broker instrument matching.
- Immutable idea publications, gap reporting and matched benchmark evidence.
- Research-only numerical confirmation and the separately registered ₹10,000 forward comparison.
- Persisted broker intent reservations, confirmed partial-fill accounting and cancellation handling.
- Existing sleeve/regime reporting, authentication and meaningful regression tests.

Keep and reconcile these components. Replacing everything would discard useful safeguards without resolving the mismatches above.

## Evidence boundaries

This audit traced startup, active/legacy entry routes, paper and broker writers, risk/exit/feed dependencies, screening/confirmation/validation, accounting, subscriptions, UI sources, health and backup code. Production inspection was read-only; no strategy was run manually and no broker order was sent. Three critical behaviours were demonstrated in isolated memory. Existing broad/focused tests were reviewed, not rerun as a claim that these new defects are covered.

It does not establish profitability, verify all customer broker configurations, certify security/legal compliance or complete the browser checks blocked by automatic approval review. The two resolved idea losses and zero actual paper fills do not support an “excellent track record” claim. The next implementation must prove consistent behaviour before making performance claims.
