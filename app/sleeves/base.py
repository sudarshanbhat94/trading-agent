"""Shared contracts every sleeve implements.

A sleeve's only job is to answer "what looks good right now, and why". It does
NOT decide size, does not touch cash, and does not write to the book. That
separation is deliberate: sizing bugs in the old engine came from each lane
doing its own capital maths, and five copies of that logic drifted apart.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Iterable, Literal, Sequence

_LOG = logging.getLogger("openstocks.sleeves")

Regime = Literal["ON", "NEUTRAL", "OFF"]


@dataclass
class Candidate:
    """One proposed entry. Sizing is left to the risk manager."""
    symbol: str
    sleeve: str
    score: float                 # 0..1, comparable WITHIN a sleeve only
    entry: float                 # reference price the plan was built on
    stop: float                  # hard invalidation, absolute price
    target: float = 0.0          # 0 = managed by trail/time instead
    trail_pct: float = 0.0
    max_hold_days: int = 0       # 0 = no time stop
    instrument: str = "EQ"       # EQ | FUT | OPT_SPREAD
    why: dict = field(default_factory=dict)

    @property
    def risk_per_share(self) -> float:
        return max(self.entry - self.stop, 0.0)

    def is_sane(self) -> tuple[bool, str]:
        """Reject anything broken on arrival rather than sizing it."""
        if self.entry <= 0:
            return False, "entry<=0"
        if self.stop <= 0:
            return False, "stop<=0"
        if self.stop >= self.entry:
            return False, "stop above entry (exits instantly)"
        if self.target and self.target <= self.entry:
            return False, "target below entry (exits instantly)"
        if self.risk_per_share <= 0:
            return False, "zero risk per share"
        if self.risk_per_share / self.entry > 0.25:
            return False, "stop wider than 25% (not a trade, a hope)"
        return True, ""


@dataclass
class SleeveDecision:
    """What a sleeve did this pass, including everything it turned down.

    Rejections are first-class. The single hardest thing to debug in the old
    engine was an empty book, because "nothing qualified" and "the gate is
    broken" produced identical output. Every sleeve must be able to say which
    it was.
    """
    sleeve: str
    regime: Regime
    active: bool
    candidates: list[Candidate] = field(default_factory=list)
    rejected: list[tuple[str, str]] = field(default_factory=list)  # (symbol, reason)
    note: str = ""

    def reject(self, symbol: str, reason: str) -> None:
        self.rejected.append((symbol, reason))

    def log(self) -> None:
        if not self.active:
            _LOG.info("sleeve %s: STAND ASIDE (%s) regime=%s",
                      self.sleeve, self.note or "inactive", self.regime)
            return
        top = ", ".join(f"{c.symbol}:{c.score:.2f}" for c in self.candidates[:5])
        _LOG.info("sleeve %s: regime=%s %d candidate(s) [%s] · %d rejected%s",
                  self.sleeve, self.regime, len(self.candidates), top,
                  len(self.rejected), f" · {self.note}" if self.note else "")
        for sym, reason in self.rejected[:12]:
            _LOG.debug("  %s rejected %s: %s", self.sleeve, sym, reason)


class Sleeve:
    """Base class. Subclasses implement `propose`."""

    name: str = "unnamed"
    #: regimes in which this sleeve may take NEW entries
    allowed_regimes: tuple[Regime, ...] = ("ON",)
    #: share of the book's risk budget this sleeve may consume (see risk.py)
    risk_share: float = 0.0

    def enabled(self, cfg) -> bool:
        return bool(getattr(cfg, self.name, None) and getattr(cfg, self.name).enabled)

    def may_run(self, regime: Regime) -> bool:
        return regime in self.allowed_regimes

    def propose(self, ctx) -> SleeveDecision:      # pragma: no cover - interface
        raise NotImplementedError

    # -- helpers shared by concrete sleeves -------------------------------
    def _decision(self, regime: Regime, active: bool = True, note: str = "") -> SleeveDecision:
        return SleeveDecision(sleeve=self.name, regime=regime, active=active, note=note)

    @staticmethod
    def _rank(cands: Iterable[Candidate], limit: int) -> list[Candidate]:
        return sorted(cands, key=lambda c: -c.score)[:limit]

    @staticmethod
    def _sane_only(cands: Sequence[Candidate], dec: SleeveDecision) -> list[Candidate]:
        out = []
        for c in cands:
            ok, why = c.is_sane()
            if ok:
                out.append(c)
            else:
                dec.reject(c.symbol, why)
        return out
