"""Research sleeves behind one production allowlist and one risk manager.

Only `index_directional` is promoted to the Rs 10,000 paper book. Each other
sleeve remains importable for replay and reporting but cannot submit a live
paper proposal through the production orchestrator.

    mean_reversion    primary   — hardened v2 dip-buying, ON/NEUTRAL only
    quality_momentum  secondary — quality + intermediate momentum, ON only
    early_momentum    tactical  — pre-top-gainer ignition detector
    index_directional index     — monthly NIFTYBEES trend exposure
    options_overlay   overlay   — defined-risk spreads only

Design rules that apply to every sleeve, enforced by `base.Sleeve`:

  * the DEFAULT ACTION IS STAND ASIDE. A sleeve returns [] unless it has a
    positive reason to act;
  * a sleeve never sizes itself — it proposes, `risk.py` allocates;
  * a sleeve declares the regimes it may trade in, and cannot run outside them;
  * every accept AND reject is logged with a reason, so an idle book can always
    be distinguished from a broken one.

The hard production allowlist lives in `engine.ACTIVE_SLEEVES`; environment
flags cannot promote a failed research sleeve by accident.
"""
from __future__ import annotations

from .base import Candidate, Sleeve, SleeveDecision
from .config import SLEEVES, SleeveConfig
from .regime import RegimeGate, RegimeView

__all__ = ["Candidate", "Sleeve", "SleeveDecision", "SLEEVES", "SleeveConfig",
           "RegimeGate", "RegimeView"]
