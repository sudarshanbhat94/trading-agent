"""Unified risk manager. The single place capital is allocated.

Every sleeve proposes; nothing sizes itself. One allocator means one place to
audit, and it is the fix for the class of bug where each lane did its own
capital maths and they drifted (a hardcoded ATR that made every position 1.6x
its slot; option positions charged against the equity book's cash and slots).

Allocation rules, in order:

 1. Book-level brakes first. Daily loss limit, all-time drawdown, and a cap on
    total deployed capital. If any trips, NOTHING is allocated — exits are
    unaffected and run elsewhere.
 2. Risk-based sizing. shares = (book * risk_per_trade) / (entry - stop).
    Equal rupee risk per trade, so a wider stop buys a smaller position rather
    than a bigger loss.
 3. Hard caps. One slot's notional, remaining cash, the sleeve's EXPOSURE share
    of the book, the sleeve's own position count, and the book-wide count.
 4. Viability. A ticket below `min_ticket` is refused: flat charges do not
    scale down and a Rs 500 position pays ~6% round trip.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .base import Candidate
from .config import SLEEVES

_LOG = logging.getLogger("openstocks.sleeves.risk")

# A future edit must not be able to over-allocate the single book.
_TOTAL_SHARE = (SLEEVES.mean_reversion.risk_share + SLEEVES.quality_momentum.risk_share
                + SLEEVES.early_momentum.risk_share + SLEEVES.index_directional.risk_share
                + SLEEVES.options_overlay.risk_share)
assert _TOTAL_SHARE <= 1.0 + 1e-9, f"sleeve risk shares sum to {_TOTAL_SHARE} (>1.0)"


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
        if book.peak_equity > 0:
            dd = book.equity / book.peak_equity - 1
            if dd < -self.s.max_drawdown:
                return True, (f"drawdown halt: {dd*100:.1f}% off peak "
                              f"(limit {self.s.max_drawdown*100:.0f}%)")
        if book.capital > 0 and book.day_pnl / book.capital < -self.s.daily_loss_limit:
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
        risk_budget = book.capital * self.s.risk_per_trade
        by_risk = risk_budget / rps

        slot = book.capital / max(self.s.max_positions_total, 1)
        by_slot = slot / cand.entry
        by_cash = max(book.cash, 0.0) / cand.entry
        # this sleeve may not hold more than its share of the book at once
        sleeve_room = max(book.capital * cfg.risk_share
                          - book.per_sleeve_notional.get(cand.sleeve, 0.0), 0.0)
        by_sleeve = sleeve_room / cand.entry

        shares = int(min(by_risk, by_slot, by_cash, by_sleeve))
        if shares < 1:
            return Allocation(cand, 0, 0.0, 0.0,
                              f"sizes to <1 share (risk Rs {risk_budget:.0f}, "
                              f"stop Rs {rps:.2f}, price Rs {cand.entry:.2f})")

        notional = shares * cand.entry
        if notional < self.s.min_ticket:
            return Allocation(cand, 0, notional, 0.0,
                              f"ticket Rs {notional:.0f} below Rs "
                              f"{self.s.min_ticket:.0f} minimum — flat charges "
                              f"would dominate")

        # the move on offer must beat the round trip by a sensible margin
        if cand.target:
            edge = (cand.target / cand.entry - 1) * 100
            if edge < self.s.min_edge_pct:
                return Allocation(cand, 0, notional, 0.0,
                                  f"target offers {edge:.1f}% < {self.s.min_edge_pct:.1f}% "
                                  f"minimum edge")

        return Allocation(cand, shares, notional, shares * rps)

    # -- the pass ---------------------------------------------------------
    def allocate(self, candidates: list[Candidate], book: BookState) -> list[Allocation]:
        """Size an ordered candidate list, respecting caps as they fill up."""
        halted, why = self.halted(book)
        if halted:
            _LOG.warning("RISK HALT — no new entries: %s", why)
            return []

        out: list[Allocation] = []
        cash = book.cash
        opened = book.open_positions
        per_sleeve = dict(book.per_sleeve_positions)
        per_notional = dict(book.per_sleeve_notional)

        for cand in candidates:
            if opened >= self.s.max_positions_total:
                _LOG.info("risk: stopping, book position cap reached")
                break
            probe = BookState(capital=book.capital, cash=cash, deployed=book.deployed,
                              open_positions=opened, per_sleeve_positions=per_sleeve,
                              per_sleeve_notional=per_notional,
                              equity=book.equity, peak_equity=book.peak_equity,
                              day_pnl=book.day_pnl)
            alloc = self.size(cand, probe)
            if not alloc.ok:
                _LOG.info("risk: %s/%s refused — %s", cand.sleeve, cand.symbol, alloc.reason)
                continue
            cash -= alloc.notional
            opened += 1
            per_sleeve[cand.sleeve] = per_sleeve.get(cand.sleeve, 0) + 1
            per_notional[cand.sleeve] = per_notional.get(cand.sleeve, 0.0) + alloc.notional
            out.append(alloc)
            _LOG.info("risk: %s/%s %d sh @ Rs %.2f = Rs %.0f (risk Rs %.0f)",
                      cand.sleeve, cand.symbol, alloc.shares, cand.entry,
                      alloc.notional, alloc.risk_amount)
        return out
