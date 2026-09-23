"""Unified risk manager. The single place capital is allocated.

Every sleeve proposes; nothing sizes itself. One allocator means one place to
audit, and it is the fix for the class of bug where each lane did its own
capital maths and they drifted (a hardcoded ATR that made every position 1.6x
its slot; option positions charged against the equity book's cash and slots).

Allocation rules, in order:

 1. Book-level brakes first. Daily loss limit, all-time drawdown, and a cap on
    total deployed capital. If any trips, NOTHING is allocated — exits are
    unaffected and run elsewhere.
 2. Risk-based sizing. Start from the price stop, then shrink whole-share
    quantity until the estimated stop loss includes delivery charges and
    20 bp slippage on both sides. A wide stop or expensive small ticket
    therefore cannot breach the cash loss budget unnoticed.
 3. Hard caps. One slot's notional, remaining cash, the sleeve's EXPOSURE share
    of the book, the sleeve's own position count, and the book-wide count.
 4. Viability. A ticket below `min_ticket` is refused: flat charges do not
    scale down and a Rs 500 position pays ~6% round trip.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field

from .base import Candidate
from .config import PRODUCTION_SLEEVES, SLEEVES
from ..costs import round_trip

_LOG = logging.getLogger("openstocks.sleeves.risk")

# A future edit must not be able to over-allocate the single book.
_TOTAL_SHARE = sum(getattr(SLEEVES, name).risk_share for name in PRODUCTION_SLEEVES)
assert _TOTAL_SHARE <= 1.0 + 1e-9, f"sleeve risk shares sum to {_TOTAL_SHARE} (>1.0)"

# A stop is not a Rs-only price move. This is the same conservative 20 bp per
# side assumed by the frozen stock replay. Delivery charges include flat
# brokerage and DP, which dominate a Rs 1,500-3,000 ticket.
SLIPPAGE = 0.002


def stop_loss_including_costs(entry: float, stop: float, shares: float) -> float:
    """Estimated cash lost at an equity stop, including both execution legs."""
    if shares <= 0 or not 0 < stop <= entry:
        return 0.0
    buy = shares * entry * (1 + SLIPPAGE)
    sell = shares * stop * (1 - SLIPPAGE)
    return buy - sell + round_trip(buy, sell, "D")


@dataclass
class BookState:
    capital: float
    cash: float
    deployed: float
    open_positions: int
    per_sleeve_positions: dict
    equity: float
    peak_equity: float
    day_pnl: float
    per_sleeve_notional: dict = field(default_factory=dict)
    open_risk: float = 0.0
    strategic_open_risk: float = 0.0


@dataclass
class Allocation:
    candidate: Candidate
    shares: int
    notional: float
    risk_amount: float
    reason: str = "ok"

    @property
    def ok(self) -> bool:
        return self.shares > 0


class RiskManager:
    def __init__(self, settings=SLEEVES):
        self.s = settings

    # -- book-level brakes ------------------------------------------------
    def halted(self, book: BookState) -> tuple[bool, str]:
        """True when no new risk may be opened, for any sleeve."""
        if not all(math.isfinite(v) for v in (book.capital, book.cash, book.deployed,
                   book.equity, book.peak_equity, book.day_pnl, book.open_risk,
                   book.strategic_open_risk)) or book.capital <= 0 or book.open_risk < 0 or book.strategic_open_risk < 0 or book.strategic_open_risk > book.open_risk:
            return True, "invalid book valuation"
        if book.peak_equity > 0:
            dd = book.equity / book.peak_equity - 1
            if book.equity <= book.peak_equity * (1 - self.s.max_drawdown):
                return True, (f"drawdown halt: {dd*100:.1f}% off peak "
                              f"(limit {self.s.max_drawdown*100:.0f}%)")
        if book.day_pnl <= -book.capital * self.s.daily_loss_limit:
            return True, (f"daily loss limit: {book.day_pnl/book.capital*100:.1f}% "
                          f"(limit {self.s.daily_loss_limit*100:.1f}%)")
        if book.capital > 0 and book.deployed / book.capital >= self.s.max_deployed:
            return True, (f"fully deployed: {book.deployed/book.capital*100:.0f}% "
                          f"(cap {self.s.max_deployed*100:.0f}%)")
        if book.open_positions >= self.s.max_positions_total:
            return True, (f"position cap: {book.open_positions}/"
                          f"{self.s.max_positions_total} open")
        return False, ""

    # -- per-candidate sizing --------------------------------------------
    def size(self, cand: Candidate, book: BookState) -> Allocation:
        halted, why = self.halted(book)
        if halted:
            return Allocation(cand, 0, 0.0, 0.0, why)
        if (not all(math.isfinite(v) for v in (cand.entry, cand.stop, cand.target))
                or cand.entry <= 0 or not 0 < cand.stop < cand.entry):
            return Allocation(cand, 0, 0.0, 0.0, "invalid candidate price")
        cfg = getattr(self.s, cand.sleeve, None)
        if cfg is None:
            return Allocation(cand, 0, 0.0, 0.0, "unknown sleeve")
        if not cfg.enabled:
            return Allocation(cand, 0, 0.0, 0.0, "sleeve disabled")

        held = book.per_sleeve_positions.get(cand.sleeve, 0)
        if held >= cfg.max_positions:
            return Allocation(cand, 0, 0.0, 0.0,
                              f"sleeve full ({held}/{cfg.max_positions})")

        rps = cand.risk_per_share
        if rps <= 0:
            return Allocation(cand, 0, 0.0, 0.0, "zero risk per share")

        # Equal rupee risk per trade. A wider stop buys a SMALLER position,
        # not a bigger loss — the property whose absence let a stop widening
        # silently raise risk per trade by 43% in the old engine.
        #
        # `risk_share` is NOT applied here. It caps a sleeve's total EXPOSURE
        # (below), because multiplying it into per-trade risk double-discounts:
        # on a Rs 10,000 book that produced a Rs 20 risk budget, which cannot
        # buy one share of anything, and every sleeve sized to zero.
        total_room = max(0.0, book.capital * self.s.max_drawdown - book.open_risk)
        tactical_risk = book.open_risk - book.strategic_open_risk
        tactical_room = max(0.0, book.capital * self.s.daily_loss_limit
                            + min(book.day_pnl, 0.0) - tactical_risk)
        strategic_room = max(0.0, book.capital *
                             (self.s.max_drawdown - self.s.daily_loss_limit)
                             - book.strategic_open_risk)
        risk_budget = min(total_room, strategic_room if cand.allocation_pct
                          else min(book.capital * self.s.risk_per_trade,
                                   tactical_room))
        if cand.allocation_pct:
            # The index has a wide disaster stop. At Rs 10k, blindly filling
            # 50% would risk >10% of the whole book and starve stock entries.
            # Reserve the daily tactical budget, then cap index shares by
            # actual stop loss as well as the intended allocation.
            by_risk = risk_budget / rps
            by_slot = book.capital * cand.allocation_pct / cand.entry
        else:
            by_risk = risk_budget / rps
            slot = book.capital / max(self.s.max_positions_total, 1)
            by_slot = slot / cand.entry
        by_cash = max(book.cash, 0.0) / (cand.entry * (1 + SLIPPAGE))
        # this sleeve may not hold more than its share of the book at once
        sleeve_room = max(book.capital * cfg.risk_share
                          - book.per_sleeve_notional.get(cand.sleeve, 0.0), 0.0)
        by_sleeve = sleeve_room / cand.entry

        by_deployment = max(book.capital * self.s.max_deployed - book.deployed, 0) / cand.entry
        shares = int(min(by_risk, by_slot, by_cash, by_sleeve, by_deployment))
        if cand.instrument == "EQ":
            while shares > 0 and stop_loss_including_costs(
                    cand.entry, cand.stop, shares) > risk_budget + 1e-9:
                shares -= 1
        if shares < 1:
            return Allocation(cand, 0, 0.0, 0.0,
                              f"sizes to <1 share after stop, fees and slippage "
                              f"(risk Rs {risk_budget:.0f}, price Rs {cand.entry:.2f})")

        notional = shares * cand.entry
        if notional < self.s.min_ticket:
            return Allocation(cand, 0, notional, 0.0,
                              f"ticket Rs {notional:.0f} below Rs "
                              f"{self.s.min_ticket:.0f} minimum — flat charges "
                              f"would dominate")

        # the move on offer must beat the round trip by a sensible margin
        if cand.target:
            edge = (cand.target / cand.entry - 1) * 100
            if cand.instrument == "EQ":
                buy = notional * (1 + SLIPPAGE)
                sell = shares * cand.target * (1 - SLIPPAGE)
                edge = (sell - buy - round_trip(buy, sell, "D")) / buy * 100
            if edge < self.s.min_edge_pct:
                return Allocation(cand, 0, notional, 0.0,
                                  f"target offers {edge:.1f}% < {self.s.min_edge_pct:.1f}% "
                                  f"minimum edge")

        loss = (stop_loss_including_costs(cand.entry, cand.stop, shares)
                if cand.instrument == "EQ" else shares * rps)
        return Allocation(cand, shares, notional, loss)

    # -- the pass ---------------------------------------------------------
    def allocate(self, candidates: list[Candidate], book: BookState) -> list[Allocation]:
        """Size an ordered candidate list, respecting caps as they fill up."""
        halted, why = self.halted(book)
        if halted:
            _LOG.warning("RISK HALT — no new entries: %s", why)
            return []

        out: list[Allocation] = []
        cash = book.cash
        deployed = book.deployed
        open_risk = book.open_risk
        strategic_open_risk = book.strategic_open_risk
        seen = set()
        opened = book.open_positions
        per_sleeve = dict(book.per_sleeve_positions)
        per_notional = dict(book.per_sleeve_notional)

        for cand in candidates:
            if cand.symbol in seen:
                continue
            if opened >= self.s.max_positions_total:
                _LOG.info("risk: stopping, book position cap reached")
                break
            probe = BookState(capital=book.capital, cash=cash, deployed=deployed,
                              open_positions=opened, per_sleeve_positions=per_sleeve,
                              per_sleeve_notional=per_notional,
                              equity=book.equity, peak_equity=book.peak_equity,
                              day_pnl=book.day_pnl, open_risk=open_risk,
                              strategic_open_risk=strategic_open_risk)
            alloc = self.size(cand, probe)
            if not alloc.ok:
                _LOG.info("risk: %s/%s refused — %s", cand.sleeve, cand.symbol, alloc.reason)
                continue
            cash -= alloc.notional
            deployed += alloc.notional
            open_risk += alloc.risk_amount
            if cand.allocation_pct:
                strategic_open_risk += alloc.risk_amount
            seen.add(cand.symbol)
            opened += 1
            per_sleeve[cand.sleeve] = per_sleeve.get(cand.sleeve, 0) + 1
            per_notional[cand.sleeve] = per_notional.get(cand.sleeve, 0.0) + alloc.notional
            out.append(alloc)
            _LOG.info("risk: %s/%s %d sh @ Rs %.2f = Rs %.0f (risk Rs %.0f)",
                      cand.sleeve, cand.symbol, alloc.shares, cand.entry,
                      alloc.notional, alloc.risk_amount)
        return out
