"""The multi-sleeve orchestrator.

One pass:

    1. compute the regime view ONCE — the master gate for every sleeve
    2. ask each enabled sleeve for candidates (they never size themselves)
    3. order by sleeve priority, then by score within a sleeve
    4. hand the ordered list to the unified risk manager
    5. log regime, per-sleeve activity, and every accept/reject with a reason

Priority is fixed and deliberate: the primary sleeve gets first claim on a
scarce Rs 10,000 book, and the overlay is allocated last from whatever remains.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable

import pandas as pd

from .base import Candidate, SleeveDecision
from .config import SLEEVES
from .early_momentum import EarlyMomentumSleeve
from .index_directional import IndexDirectionalSleeve
from .mean_reversion import MeanReversionSleeve
from .options_overlay import OptionsOverlaySleeve
from .quality_momentum import QualityMomentumSleeve
from .regime import RegimeGate, RegimeView
from .risk import Allocation, BookState, RiskManager

_LOG = logging.getLogger("openstocks.sleeves.engine")

#: first claim on capital goes leftmost
PRIORITY = ["mean_reversion", "quality_momentum", "early_momentum",
            "index_directional", "options_overlay"]


@dataclass
class SleeveContext:
    """Everything a sleeve is allowed to see. Passed, never imported."""
    tails: dict
    market_df: pd.DataFrame
    asof: object
    live: dict
    regime: RegimeView
    settings: object = field(default_factory=lambda: SLEEVES)
    force: bool = False
    sessions_since_rebalance: int | None = None
    # optional feeds — sleeves degrade gracefully when these are absent
    index_bars: Callable | None = None
    options_view: Callable | None = None
    option_chain: Callable | None = None
    india_vix: Callable | None = None
    delivery_pct: Callable | None = None
    catalyst_score: Callable | None = None
    options_day_pnl: Callable | None = None


@dataclass
class PassResult:
    regime: RegimeView
    decisions: list[SleeveDecision] = field(default_factory=list)
    allocations: list[Allocation] = field(default_factory=list)
    halt_reason: str = ""

    @property
    def traded(self) -> bool:
        return bool(self.allocations)


class SleeveEngine:
    def __init__(self, settings=SLEEVES):
        self.settings = settings
        self.gate = RegimeGate()
        self.risk = RiskManager(settings)
        self.sleeves = {
            "mean_reversion": MeanReversionSleeve(),
            "quality_momentum": QualityMomentumSleeve(),
            "early_momentum": EarlyMomentumSleeve(),
            "index_directional": IndexDirectionalSleeve(),
            "options_overlay": OptionsOverlaySleeve(),
        }

    def run(self, tails, market_df, asof, live, book: BookState, **feeds) -> PassResult:
        regime = self.gate.view(tails, market_df, asof)
        ctx = SleeveContext(tails=tails, market_df=market_df, asof=asof, live=live,
                            regime=regime, settings=self.settings, **feeds)
        result = PassResult(regime=regime)

        halted, why = self.risk.halted(book)
        if halted:
            result.halt_reason = why
            _LOG.warning("PASS HALTED: %s (exits continue to run)", why)
            return result

        ordered: list[Candidate] = []
        for name in PRIORITY:
            sleeve = self.sleeves[name]
            cfg = getattr(self.settings, name)
            if not cfg.enabled:
                _LOG.info("sleeve %s: disabled by feature flag", name)
                continue
            try:
                dec = sleeve.propose(ctx)
            except Exception:
                _LOG.exception("sleeve %s raised; skipping it this pass", name)
                continue
            dec.log()
            result.decisions.append(dec)
            ordered.extend(dec.candidates)

        result.allocations = self.risk.allocate(ordered, book)
        self._summarise(result)
        return result

    @staticmethod
    def _summarise(result: PassResult) -> None:
        if result.halt_reason:
            return
        proposed = sum(len(d.candidates) for d in result.decisions)
        rejected = sum(len(d.rejected) for d in result.decisions)
        active = [d.sleeve for d in result.decisions if d.active]
        _LOG.info("PASS regime=%s · active sleeves %s · %d proposed · %d rejected "
                  "· %d funded", result.regime.state, active or "none",
                  proposed, rejected, len(result.allocations))
        if proposed and not result.allocations:
            _LOG.warning("every proposal was refused by the risk manager — "
                         "check ticket size and sleeve caps, not the signals")
