"""The multi-sleeve orchestrator.

One pass:

    1. compute the regime view ONCE — the master gate for every sleeve
    2. ask each enabled sleeve for candidates (they never size themselves)
    3. order by sleeve priority, then by score within a sleeve
    4. hand the ordered list to the unified risk manager
    5. log regime, per-sleeve activity, and every accept/reject with a reason

Production paper promotes only the NIFTYBEES sleeve. The large-cap factor
screen remains visible for research but cannot allocate capital.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from typing import Callable

import pandas as pd

from .base import Candidate, SleeveDecision
from .config import OBSERVATION_SLEEVES, PRODUCTION_SLEEVES, SLEEVES
from .early_momentum import EarlyMomentumSleeve
from .index_directional import IndexDirectionalSleeve
from .mean_reversion import MeanReversionSleeve
from .options_overlay import OptionsOverlaySleeve
from .quality_momentum import QualityMomentumSleeve
from .regime import RegimeGate, RegimeView
from .risk import Allocation, BookState, RiskManager

_LOG = logging.getLogger("openstocks.sleeves.engine")

#: first claim on capital goes leftmost
PRIORITY = ["index_directional", "mean_reversion", "quality_momentum",
            "early_momentum", "options_overlay"]

# Explicit paper allowlist. Observed sleeves may explain the tape, but only
# PRODUCTION_SLEEVES can ever send candidates to the risk manager.
ACTIVE_SLEEVES = PRODUCTION_SLEEVES + OBSERVATION_SLEEVES


@dataclass
class SleeveContext:
    """Everything a sleeve is allowed to see. Passed, never imported."""
    tails: dict
    market_df: pd.DataFrame
    asof: object
    live: dict
    regime: RegimeView
    settings: object = field(default_factory=lambda: SLEEVES)
    trade_date: object | None = None
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
    require_live_quotes: bool = False
    routable_instruments: tuple | None = None
    quality_scores: dict | None = None
    factor_symbols: set | None = None
    eligible_symbols: set | None = None
    require_reference_data: bool = False
    # A newly reset book must not sit idle until the next calendar month when
    # the already-completed trend signal is ON.  This grants one immediate
    # evaluation; after the book has traded, normal monthly cadence resumes.
    bootstrap_entry: bool = False


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

        ordered: list[Candidate] = []
        for name in PRIORITY:
            sleeve = self.sleeves[name]
            cfg = getattr(self.settings, name)
            if name not in ACTIVE_SLEEVES:
                result.decisions.append(SleeveDecision(
                    name, regime.state, False, note="not promoted: independent replay failed or unavailable"))
                continue
            if not cfg.enabled:
                _LOG.info("sleeve %s: disabled by feature flag", name)
                continue
            if ctx.require_reference_data and name in ("mean_reversion", "early_momentum") and ctx.eligible_symbols is None:
                result.decisions.append(SleeveDecision(name, regime.state, False, note="Nifty membership snapshot unavailable"))
                continue
            if ctx.require_reference_data and name == "quality_momentum" and not ctx.factor_symbols:
                result.decisions.append(SleeveDecision(name, regime.state, False,
                                                       note="verified NSE factor constituents unavailable"))
                continue
            try:
                # Filter before ranking so ineligible names cannot crowd out
                # eligible candidates. The regime above uses the full panel.
                sleeve_ctx = ctx
                if name in ("mean_reversion", "quality_momentum", "early_momentum"):
                    allowed = ctx.factor_symbols if name == "quality_momentum" else ctx.eligible_symbols
                    sleeve_ctx = replace(ctx, tails={sym: frame for sym, frame in tails.items()
                        if (allowed is None or sym in allowed)
                        and (name in OBSERVATION_SLEEVES or not ctx.require_live_quotes or sym in live)})
                dec = sleeve.propose(sleeve_ctx)
            except Exception:
                _LOG.exception("sleeve %s raised; skipping it this pass", name)
                continue
            if name in OBSERVATION_SLEEVES:
                # The prior paper promotion had no positive after-cost
                # holdout. Preserve diagnostics but hard-block its orders.
                dec.diagnostics["entry_gate"] = dec.note
                dec.note = "research only: retrospective stock holdout lost money after costs"
                dec.active = False
                dec.candidates = []
                result.decisions.append(dec)
                dec.log()
                continue
            result.decisions.append(dec)
            accepted = []
            for cand in dec.candidates:
                sane, why = cand.is_sane()
                if not sane:
                    dec.reject(cand.symbol, why)
                elif not sleeve.may_run(regime.state):
                    dec.reject(cand.symbol, f"regime {regime.state} blocks entry")
                elif ctx.require_live_quotes and cand.instrument == "EQ" and cand.symbol not in live:
                    dec.reject(cand.symbol, "fresh entry quote unavailable")
                elif (cand.instrument == "EQ" and cand.sleeve == "quality_momentum"
                      and ctx.factor_symbols is not None and cand.symbol not in ctx.factor_symbols):
                    dec.reject(cand.symbol, "outside verified NSE factor intersection")
                elif (cand.instrument == "EQ" and cand.sleeve in ("mean_reversion", "early_momentum")
                      and ctx.eligible_symbols is not None and cand.symbol not in ctx.eligible_symbols):
                    dec.reject(cand.symbol, "outside verified liquid NSE universe")
                elif ctx.routable_instruments is not None and cand.instrument not in ctx.routable_instruments:
                    dec.reject(cand.symbol, "instrument routing unavailable")
                else:
                    ordered.append(cand)
                    accepted.append(cand)
            # Ideas consume this same validated list. Refused proposals must
            # not leak into actionable recommendations through a second path.
            dec.candidates = accepted
            dec.log()

        result.allocations = [] if halted else self.risk.allocate(ordered, book)
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
