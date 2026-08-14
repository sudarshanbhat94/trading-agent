"""Multi-sleeve trading architecture.

Five independent sleeves share ONE Rs 10,000 paper book. Each proposes
candidates; the unified risk manager in `risk.py` decides what actually gets
capital, and the regime gate in `regime.py` is the master switch above all of
them.

    mean_reversion    primary   — hardened v2 dip-buying, ON/NEUTRAL only
    quality_momentum  secondary — quality + intermediate momentum, ON only
    early_momentum    tactical  — pre-top-gainer ignition detector
    index_directional index     — NIFTY / BANKNIFTY directional
    options_overlay   overlay   — defined-risk spreads only

Design rules that apply to every sleeve, enforced by `base.Sleeve`:

  * the DEFAULT ACTION IS STAND ASIDE. A sleeve returns [] unless it has a
    positive reason to act;
  * a sleeve never sizes itself — it proposes, `risk.py` allocates;
  * a sleeve declares the regimes it may trade in, and cannot run outside them;
  * every accept AND reject is logged with a reason, so an idle book can always
    be distinguished from a broken one.

Feature flags live in `config.SLEEVES`; each sleeve can be switched off
independently without touching the others.
"""
from __future__ import annotations

from .base import Candidate, Sleeve, SleeveDecision
from .config import SLEEVES, SleeveConfig
from .regime import RegimeGate, RegimeView

__all__ = ["Candidate", "Sleeve", "SleeveDecision", "SLEEVES", "SleeveConfig",
           "RegimeGate", "RegimeView"]
