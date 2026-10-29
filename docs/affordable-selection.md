# Account-aware stock alternatives

**TRADING BEHAVIOUR CHANGED:** research sizing now respects pending paper-order
reservations, and automation publications use the same settings as their engine
context. Quality/participation/confirmation rules, ranking score, stops, targets,
paper epochs, broker settings and the registered experiment are unchanged.

## Limits preserved

The deployed ₹10,000 account has a ₹1,500 **minimum ticket**, ₹200 nominal
per-trade ceiling and a stricter ₹150 daily loss allowance before existing losses
and open risk. Aggregate stop-risk room is ₹1,000 and the quality sleeve has
₹3,000 notional capacity and one position slot. All these limits remain binding;
the user's ₹1,500 risk ceiling does not increase any tighter configured limit.
Estimated risk includes the existing slippage and delivery charges; realised gap
losses can exceed an estimated stop loss.

## What changed

- The Ideas account reader previously ignored pending commitments while actual
  execution reserved their cash, risk, exposure and slots. It now applies the
  existing reservation state, scoped to account and epoch, without database writes.
  Incomplete or interrupted order state blocks the preview.
- The existing allocator now returns structured facts explaining cash, one-share
  after-cost loss, position/sleeve/deployment capacity, minimum viable ticket,
  maximum whole-share quantity and estimated cash required. These are calculated
  from its actual sizing constraints; they do not create another allocator.
- Qualifying candidates retain their quality order. Unfundable leaders cannot
  consume a funded shortlist/sector slot. Separate quality and funding ranks make
  this visible. Three alternatives are independent choices, not permission to
  spend their combined cash or risk. One quality slot remains one slot.
- The API adds `funding`, `cash_required`, `funding_rank` and `quality_rank` to
  fundable plans, plus `funding_summary` and `funding_rejections`. Research-only
  refusals retain their `watch` qualification; they are not entry-qualified ideas.
- Automation diagnostics rank only confirmed, fundable candidates and retain
  their required cash/risk. Catalogue, reservation and later-fill checks remain
  mandatory. No candidate acquires order approval from affordability alone.
  The supplied settings are kept intact: rerunning their initializer would
  reconstruct nested sleeve settings and discard tighter limits. Disabled,
  zero/exhausted-slot and lower-exposure contexts remain binding.
- The actual product UI shows required cash/risk on a conditional plan and a
  disclosure for otherwise-qualifying stocks that cannot fit. ₹1,500 minimum
  ticket and ₹150 effective allowance are explicitly separate. Unfunded rows
  have no buy control; loading older payloads does not fabricate amounts.

## Dated production-data replay

A read-only replay at 13:32 IST on 28 October used the owned ₹10,000 account and
the 13:20 IST evidence screen. Seven businesses passed research-watch admission;
one fit funding: TECHM, one share, estimated cash ₹1,780.78 and stop loss ₹141.34.
No row passed complete entry admission. These are conditional scenarios, not
orders or recommendations to buy at the current price.

Examples of explicit refusals at their planned entry levels:

- PERSISTENT: one share ₹5,619.04, exceeding ₹3,000 sleeve capacity; after-cost
  stop loss ₹318.59 also exceeds ₹150.
- COFORGE: one-share stop loss ₹152.73 exceeds ₹150.
- MPHASIS: one-share stop loss ₹173.51 exceeds ₹150.
- TCS: one-share stop loss ₹169.91 exceeds ₹150.
- INFY: maximum funded ticket about ₹1,117 is below the ₹1,500 minimum; the
  response includes the minimum quantity's cash and stop loss so this is explained.

The replay wrote zero database rows and submitted zero orders. Prices, quantities
and eligibility can change; API and execution recompute account checks.

## Verification

27 funding function fixtures and four renderer tests cover expensive-leader
fallback, cash/risk/slot/minimum-ticket explanations, pending account/epoch
isolation, custom settings, zero read-side writes, unmodified fingerprints and
registered protocol, confirmation separation and HTML escaping. The full suite
ran 2,762 checks: 2,629 passed, 133 skipped, no failures/errors. All 135 function
fixtures and the isolated paper lifecycle rehearsal passed. A synthetic browser
rehearsal verified expanded reasons on desktop and 390px mobile without horizontal
overflow. These checks do not establish strategy profitability or broker readiness.
