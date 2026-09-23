"""Research sleeves behind one production allowlist and one risk manager.

`index_directional` and `quality_momentum` can submit Rs 10,000 paper-book
proposals. The stock sleeve uses a verified NSE constituent intersection and
is not eligible for real-broker mirroring. Other sleeves remain research-only.

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
