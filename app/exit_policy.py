"""Versioned position exit rules; zero sessions explicitly means no time exit.

The strategy proposes this contract once. Configuration changes must not replace
the contract of an existing position. Legacy rows keep their original rules,
except the monthly index contract whose accidental five-session exit is fixed.
"""
from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class ExitPolicy:
    version: str
    stop: float
    target: float = 0.0
    trail: float = 0.0
    max_hold_sessions: int | None = None
    ordered_quotes: bool = True
    regime_exit: str | None = None

    def __post_init__(self):
        if self.version != "position-exit-v1":
            raise ValueError("unsupported exit policy version")
        if not all(math.isfinite(v) and v >= 0 for v in
                   (self.stop, self.target, self.trail)) or self.trail >= 1:
            raise ValueError("invalid exit policy levels")
        if self.max_hold_sessions is not None and (
                isinstance(self.max_hold_sessions, bool)
                or not isinstance(self.max_hold_sessions, int)
                or self.max_hold_sessions < 1):
            raise ValueError("invalid holding-session limit")
        if self.regime_exit not in (None, "monthly_off"):
            raise ValueError("unknown regime exit rule")

    @classmethod
    def create(cls, strategy, stop, target=0, trail=0, max_hold_days=None):
        # Import only at construction: the engine imports this module as well.
        from .v2_live import HOLD_DAYS, MIDSESSION_STRATS
        limit = (0 if strategy == "index_directional" else
                 HOLD_DAYS.get(strategy, 10)) if max_hold_days is None else max_hold_days
        return cls("position-exit-v1", float(stop or 0), float(target or 0),
                   float(trail or 0), int(limit) if limit else None,
                   strategy in MIDSESSION_STRATS or strategy == "manual",
                   "monthly_off" if strategy == "index_directional" else None)

    def encode(self):
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))

    @classmethod
    def decode(cls, value):
        if not value:
            raise ValueError("position exit policy is missing")
        return cls(**json.loads(value))
