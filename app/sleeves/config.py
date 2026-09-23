"""Feature flags and risk allocation for the multi-sleeve system.

Every number here is a fraction of the ONE Rs 10,000 book. Sleeve allocations
are shares of a single pie, never additions to it — `risk.py` asserts that they
sum to <= 1.0 at import so a future edit cannot silently over-allocate.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


# Hard production boundary. Research sleeves retain their own sizing defaults
# for isolated replay and tests, but only these shares can consume the live
# paper book. The quality stock rule failed an after-cost retrospective
# holdout, so it is observed for research only and cannot consume paper cash.
# Keeping the allowlist beside allocation prevents inactive research modules
# from making a promoted sleeve look over-allocated.
PRODUCTION_SLEEVES = ("index_directional",)
OBSERVATION_SLEEVES = ("quality_momentum",)


def _bool(key: str, default: bool) -> bool:
    return os.getenv(key, str(default)).strip().lower() in ("1", "true", "yes", "on")


def _float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, default))
    except (TypeError, ValueError):
        return default


@dataclass
class SleeveConfig:
    enabled: bool
    risk_share: float        # share of the book's RISK budget
    max_positions: int
    note: str = ""


@dataclass
class SleeveSettings:
    """The whole system's tunables, in one place."""

    # ---- book ----------------------------------------------------------
    # Mirrors v2_live.BUDGET["IN"]. Fixed by operator instruction.
    capital: float = _float("PAPER_CAPITAL_INR", 10_000.0)

    # Ceiling on tactical trade risk. The daily-loss allowance can be lower,
    # and sizing includes flat delivery charges plus estimated slippage;
    # Rs 200 of gross stop distance is NOT a Rs 200 possible cash loss.
    risk_per_trade: float = _float("RISK_PER_TRADE", 0.02)

    # Hard stop on a single day's realised + unrealised loss.
    daily_loss_limit: float = _float("DAILY_LOSS_LIMIT", 0.015)

    # Book-wide drawdown brake from the all-time equity peak.
    max_drawdown: float = _float("MAX_DRAWDOWN", 0.10)

    # Never more than this share of the book deployed at once, so the system
    # cannot fully commit into a single regime read.
    max_deployed: float = _float("MAX_DEPLOYED", 0.90)

    # Concurrent positions across ALL sleeves combined.
    max_positions_total: int = int(_float("MAX_POSITIONS_TOTAL", 3))

    # A position smaller than this is not worth its flat charges.
    min_ticket: float = _float("MIN_TICKET_INR", 1_500.0)

    # Minimum expected move, net of round-trip cost, for a trade to be worth
    # taking at all. At Rs 2,000 a delivery round trip is ~1.6%, so a setup
    # promising less than this is negative before it starts.
    min_edge_pct: float = _float("MIN_EDGE_PCT", 3.0)

    # ---- sleeves -------------------------------------------------------
    mean_reversion: SleeveConfig = None
    quality_momentum: SleeveConfig = None
    early_momentum: SleeveConfig = None
    index_directional: SleeveConfig = None
    options_overlay: SleeveConfig = None

    def __post_init__(self) -> None:
        self.mean_reversion = SleeveConfig(
            enabled=_bool("SLEEVE_MEAN_REVERSION", True),
            risk_share=_float("SHARE_MEAN_REVERSION", 0.40),
            max_positions=2,
            note="primary; hardened v2 dip-buying, ON/NEUTRAL only")
        self.quality_momentum = SleeveConfig(
            enabled=_bool("SLEEVE_QUALITY_MOMENTUM", True),
            risk_share=_float("SHARE_QUALITY_MOMENTUM", 0.30),
            max_positions=1,
            note="research-only; NSE Quality 50 with completed-session momentum, monthly")
        self.early_momentum = SleeveConfig(
            enabled=_bool("SLEEVE_EARLY_MOMENTUM", True),
            risk_share=_float("SHARE_EARLY_MOMENTUM", 0.20),
            max_positions=1,
            note="tactical; ignition detector, tighter stops and faster exits")
        self.index_directional = SleeveConfig(
            enabled=_bool("SLEEVE_INDEX_DIRECTIONAL", True),
            risk_share=_float("SHARE_INDEX_DIRECTIONAL", 0.50),
            max_positions=1,
            note="NIFTYBEES trend; 50% passed development and holdout after costs")
        self.options_overlay = SleeveConfig(
            enabled=_bool("SLEEVE_OPTIONS_OVERLAY", True),
            risk_share=_float("SHARE_OPTIONS_OVERLAY", 0.00),
            max_positions=1,
            note="defined-risk spreads only; sizes itself to zero when one lot "
                 "exceeds its allocation, which it does at this capital")


SLEEVES = SleeveSettings()
