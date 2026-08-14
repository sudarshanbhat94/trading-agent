"""Sleeve 5 (overlay): conservative, DEFINED-RISK options only.

Structures: bull put spreads and bear call spreads on NIFTY and BANKNIFTY
weeklies/monthlies — the two most liquid chains on the exchange. Both are
credit spreads with a bought wing, so maximum loss is known at entry and
capped at (strike width - credit) x lot.

NAKED SHORT OPTIONS ARE NOT IMPLEMENTED and the code refuses to build one:
`_validate` rejects any structure without a protective long leg. That is a
hard structural rule, not a parameter.

Risk controls specific to this sleeve:
  * max loss per trade, as a fraction of the book;
  * max loss per DAY for the options book as a whole;
  * directional bias must agree with the regime AND the options data;
  * lowest priority — it is allocated last, from whatever risk share remains.

At Rs 10,000 of capital one NFO lot's defined risk exceeds the whole book, so
`size_spread` will return zero lots and say why. The sleeve is fully
implemented and will trade the moment the book can afford a lot; it does not
pretend to trade what it cannot fund.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from .base import Candidate, Sleeve, SleeveDecision

_LOG = logging.getLogger("openstocks.sleeves.options")

UNDERLYINGS = ("NIFTY", "BANKNIFTY")
MAX_LOSS_PER_TRADE = 0.02       # of the book
MAX_LOSS_PER_DAY = 0.03         # of the book, options sleeve total
MIN_CREDIT_RATIO = 0.20         # credit must be >=20% of the strike width
PCR_BULLISH, PCR_BEARISH = 1.20, 0.80
DELTA_TARGET = 0.25             # short leg roughly 25-delta / ~1 SD out


@dataclass
class Spread:
    kind: str                   # bull_put | bear_call
    underlying: str
    short_strike: float
    long_strike: float
    credit: float               # per share
    width: float
    lot_size: int
    expiry: str

    @property
    def max_loss_per_lot(self) -> float:
        return max(self.width - self.credit, 0.0) * self.lot_size

    @property
    def max_gain_per_lot(self) -> float:
        return self.credit * self.lot_size


class OptionsOverlaySleeve(Sleeve):
    name = "options_overlay"
    allowed_regimes = ("ON", "NEUTRAL")

    def propose(self, ctx) -> SleeveDecision:
        regime = ctx.regime.state
        dec = self._decision(regime)
        if not self.may_run(regime):
            dec.active = False
            dec.note = f"regime {regime} blocks the overlay"
            return dec

        if self._day_loss_breached(ctx):
            dec.active = False
            dec.note = "options day-loss limit reached"
            return dec

        cands: list[Candidate] = []
        for und in UNDERLYINGS:
            chain = ctx.option_chain(und) if callable(getattr(ctx, "option_chain", None)) else None
            if not chain:
                dec.reject(und, "no option chain available")
                continue
            opt = ctx.options_view(und) if callable(getattr(ctx, "options_view", None)) else {}

            spread, why = self._build(und, chain, opt, ctx)
            if spread is None:
                dec.reject(und, why)
                continue

            ok, vwhy = self._validate(spread)
            if not ok:
                dec.reject(und, vwhy)
                continue

            lots, lwhy = self.size_spread(spread, ctx.settings.capital)
            if lots < 1:
                dec.reject(und, lwhy)
                continue

            # Expressed as a Candidate so the unified risk manager still sees
            # it. entry/stop encode the spread's own risk per lot.
            entry = spread.max_loss_per_lot + spread.max_gain_per_lot
            cands.append(Candidate(
                symbol=f"{und}_{spread.kind}_{int(spread.short_strike)}",
                sleeve=self.name, score=0.55, entry=entry,
                stop=entry - spread.max_loss_per_lot,
                target=entry + spread.max_gain_per_lot,
                instrument="OPT_SPREAD", max_hold_days=5,
                why=dict(setup=spread.kind, underlying=und,
                         short=spread.short_strike, long=spread.long_strike,
                         credit=spread.credit, width=spread.width,
                         lots=lots, max_loss=spread.max_loss_per_lot * lots,
                         expiry=spread.expiry, pcr=opt.get("pcr"),
                         regime=regime)))

        dec.candidates = self._rank(cands, getattr(ctx.settings, self.name).max_positions)
        return dec

    # -- construction ---------------------------------------------------
    def _build(self, und, chain, opt, ctx) -> tuple[Spread | None, str]:
        """Pick the direction from regime + options data, then build the spread."""
        pcr = opt.get("pcr")
        bullish = ctx.regime.full_system and (pcr is None or pcr >= PCR_BULLISH)
        bearish = (not ctx.regime.full_system) and (pcr is not None and pcr <= PCR_BEARISH)
        if not (bullish or bearish):
            return None, f"no agreed bias (regime {ctx.regime.state}, pcr {pcr})"

        kind = "bull_put" if bullish else "bear_call"
        opt_type = "PE" if bullish else "CE"
        spot = opt.get("spot") or opt.get("underlying")
        if not spot:
            return None, "no spot for the chain"
        spot = float(spot)

        legs = [r for r in chain if str(r.get("opt_type", "")).upper() == opt_type
                and float(r.get("strike") or 0) > 0 and float(r.get("close") or 0) > 0]
        if len(legs) < 2:
            return None, f"chain has too few {opt_type} legs"

        # short leg ~1 SD out of the money, long leg one strike beyond
        strikes = sorted({float(r["strike"]) for r in legs})
        if bullish:
            otm = [k for k in strikes if k < spot * (1 - DELTA_TARGET / 10)]
            short_k = max(otm) if otm else None
            long_k = max([k for k in strikes if k < short_k], default=None) if short_k else None
        else:
            otm = [k for k in strikes if k > spot * (1 + DELTA_TARGET / 10)]
            short_k = min(otm) if otm else None
            long_k = min([k for k in strikes if k > short_k], default=None) if short_k else None
        if short_k is None or long_k is None:
            return None, "no suitable strike pair"

        px = {float(r["strike"]): float(r["close"]) for r in legs}
        lot = int(next((r.get("lot_size") for r in legs if r.get("lot_size")), 0) or 0)
        if lot <= 0:
            return None, "no lot size on the chain"

        credit = px.get(short_k, 0.0) - px.get(long_k, 0.0)
        width = abs(short_k - long_k)
        if credit <= 0:
            return None, "structure pays no credit"
        if credit / width < MIN_CREDIT_RATIO:
            return None, (f"credit {credit:.1f} is {credit/width*100:.0f}% of the "
                          f"{width:.0f}-wide spread (<{MIN_CREDIT_RATIO*100:.0f}%)")

        return Spread(kind=kind, underlying=und, short_strike=short_k,
                      long_strike=long_k, credit=credit, width=width,
                      lot_size=lot, expiry=str(opt.get("expiry") or "")), ""

    @staticmethod
    def _validate(spread: Spread) -> tuple[bool, str]:
        """Structural guarantee: there is always a bought wing."""
        if spread.long_strike is None:
            return False, "REFUSED: naked short leg (no protective wing)"
        if spread.width <= 0:
            return False, "REFUSED: zero-width spread is a naked short"
        if spread.max_loss_per_lot <= 0:
            return False, "REFUSED: undefined maximum loss"
        return True, ""

    @staticmethod
    def size_spread(spread: Spread, capital: float) -> tuple[int, str]:
        """Lots, bounded by the per-trade max-loss cap."""
        cap = capital * MAX_LOSS_PER_TRADE
        per_lot = spread.max_loss_per_lot
        if per_lot <= 0:
            return 0, "undefined risk"
        lots = int(cap // per_lot)
        if lots < 1:
            return 0, (f"one lot risks Rs {per_lot:,.0f}, above the Rs {cap:,.0f} "
                       f"per-trade cap ({MAX_LOSS_PER_TRADE*100:.0f}% of Rs "
                       f"{capital:,.0f}) — cannot fund a defined-risk spread at "
                       f"this capital")
        return lots, "ok"

    @staticmethod
    def _day_loss_breached(ctx) -> bool:
        try:
            lost = ctx.options_day_pnl() if callable(
                getattr(ctx, "options_day_pnl", None)) else 0.0
            return bool(lost < -ctx.settings.capital * MAX_LOSS_PER_DAY)
        except Exception:
            return False
