"""Option-chain analytics: what the open interest is actually saying.

These are the readings Indian options traders lead with, and none of them
existed in the engine. All are pure functions over one session's chain, so they
can be scored against 404 sessions of history before anything trades on them.

A chain row is a dict with: strike, opt_type ("CE"/"PE"), oi, oi_change,
volume, close. Rows for a SINGLE symbol and a SINGLE expiry.

Two things every function here respects:

  * OI is a stock, not a flow. Total OI says who is positioned; the CHANGE says
    what happened today. Conflating them is the most common error in this
    analysis, so they are separate readings.
  * A strike with no volume is not information. Open interest can sit stale for
    weeks on an illiquid strike, and treating it as a wall the market is
    defending reads a ghost as a level.
"""
from __future__ import annotations


def _num(value, default=0.0):
    try:
        out = float(value)
        return out if out == out else default          # NaN check
    except (TypeError, ValueError):
        return default


def _split(chain):
    calls = [r for r in chain if str(r.get("opt_type", "")).upper() == "CE"]
    puts = [r for r in chain if str(r.get("opt_type", "")).upper() == "PE"]
    return calls, puts


def pcr_oi(chain):
    """Put/call ratio by OPEN INTEREST — standing positioning.

    High means puts outnumber calls, which is read as supportive: those puts
    are largely sold by people defending lower levels. It is a crowd measure,
    so only extremes carry information; near 1.0 it says nothing.
    """
    calls, puts = _split(chain)
    call_oi = sum(_num(r.get("oi")) for r in calls)
    put_oi = sum(_num(r.get("oi")) for r in puts)
    if call_oi <= 0 or put_oi <= 0:
        return None
    return put_oi / call_oi


def pcr_volume(chain):
    """Put/call ratio by VOLUME — today's activity, not standing positioning.

    Deliberately separate from pcr_oi: volume can be violently one-sided on a
    day when total OI barely moves, and the two disagreeing is itself the
    signal.
    """
    calls, puts = _split(chain)
    call_v = sum(_num(r.get("volume")) for r in calls)
    put_v = sum(_num(r.get("volume")) for r in puts)
    if call_v <= 0 or put_v <= 0:
        return None
    return put_v / call_v


def max_pain(chain):
    """Strike where option BUYERS lose the most — where writers want expiry.

    Computed properly: for each candidate strike, the total intrinsic value
    that would be paid out across every open contract if the index settled
    there. The minimum is max pain. The common shortcut of "strike with the
    most total OI" is a different quantity and is often several strikes away.
    """
    strikes = sorted({_num(r.get("strike")) for r in chain if _num(r.get("strike")) > 0})
    if not strikes:
        return None
    best, best_pain = None, None
    for settle in strikes:
        pain = 0.0
        for row in chain:
            strike, oi = _num(row.get("strike")), _num(row.get("oi"))
            if strike <= 0 or oi <= 0:
                continue
            side = str(row.get("opt_type", "")).upper()
            if side == "CE" and settle > strike:
                pain += (settle - strike) * oi
            elif side == "PE" and settle < strike:
                pain += (strike - settle) * oi
        if best_pain is None or pain < best_pain:
            best, best_pain = settle, pain
    return best


def oi_levels(chain, min_volume=0.0):
    """(resistance, support) from OI concentration.

    Highest call OI is where writers are defending the upside, highest put OI
    the downside. `min_volume` exists because stale OI on an untraded strike is
    not a level anyone is defending.
    """
    calls, puts = _split(chain)
    calls = [r for r in calls if _num(r.get("volume")) >= min_volume]
    puts = [r for r in puts if _num(r.get("volume")) >= min_volume]
    resistance = max(calls, key=lambda r: _num(r.get("oi")), default=None)
    support = max(puts, key=lambda r: _num(r.get("oi")), default=None)
    return (_num(resistance.get("strike")) if resistance else None,
            _num(support.get("strike")) if support else None)


def oi_price_matrix(price_change, oi_change):
    """The four-state read every Indian options desk starts from.

    price up + OI up    long buildup     new money going long, bullish
    price up + OI down  short covering   shorts closing, bullish but weaker
    price down + OI up  short buildup    new money going short, bearish
    price down + OI down long unwinding  longs closing, bearish but weaker

    Buildups are stronger than unwinds because they are NEW commitment rather
    than existing positions being closed, so the two are distinguished rather
    than collapsed into "bullish/bearish".
    """
    if price_change == 0 or oi_change == 0:
        return "neutral", 0
    if price_change > 0 and oi_change > 0:
        return "long_buildup", 1
    if price_change > 0 and oi_change < 0:
        return "short_covering", 1
    if price_change < 0 and oi_change > 0:
        return "short_buildup", -1
    return "long_unwinding", -1


def strike_migration(today, yesterday, top=3):
    """Where OI is being ADDED, versus where it merely sits.

    Positioning moving up the chain is the market repricing its expected range
    upward. This reads the change, not the level, which is what makes it a
    signal rather than a snapshot.
    """
    def weighted(chain, side):
        rows = [r for r in chain if str(r.get("opt_type", "")).upper() == side]
        rows = sorted(rows, key=lambda r: -_num(r.get("oi")))[:top]
        total = sum(_num(r.get("oi")) for r in rows)
        if total <= 0:
            return None
        return sum(_num(r.get("strike")) * _num(r.get("oi")) for r in rows) / total

    out = {}
    for side in ("CE", "PE"):
        now, before = weighted(today, side), weighted(yesterday, side)
        out[side] = (now - before) if (now is not None and before is not None) else None
    return out


def concentration(chain):
    """Share of total OI held by the single largest strike, per side.

    A chain concentrated on one strike behaves very differently from one spread
    across many: concentration is what makes pinning possible.
    """
    out = {}
    for side in ("CE", "PE"):
        rows = [r for r in chain if str(r.get("opt_type", "")).upper() == side]
        total = sum(_num(r.get("oi")) for r in rows)
        top = max((_num(r.get("oi")) for r in rows), default=0.0)
        out[side] = (top / total) if total > 0 else None
    return out


def atm_straddle(chain, spot):
    """Premium of the ATM call + put — the market's own expected move.

    Quoted as an absolute and as a percentage of spot. This is the number to
    beat: if the expected move is 1.5% and your edge is smaller, the option is
    priced against you regardless of direction.
    """
    strikes = sorted({_num(r.get("strike")) for r in chain if _num(r.get("strike")) > 0})
    if not strikes or not spot or spot <= 0:
        return None
    atm = min(strikes, key=lambda k: abs(k - spot))
    call = next((r for r in chain if _num(r.get("strike")) == atm
                 and str(r.get("opt_type", "")).upper() == "CE"), None)
    put = next((r for r in chain if _num(r.get("strike")) == atm
                and str(r.get("opt_type", "")).upper() == "PE"), None)
    if not call or not put:
        return None
    total = _num(call.get("close")) + _num(put.get("close"))
    if total <= 0:
        return None
    return dict(strike=atm, premium=total, pct=total / spot * 100)


def summarise(chain, spot, previous=None, price_change=None, oi_change=None,
              min_volume=0.0):
    """Every reading for one session, in one dict."""
    resistance, support = oi_levels(chain, min_volume=min_volume)
    pain = max_pain(chain)
    out = dict(
        pcr_oi=pcr_oi(chain),
        pcr_volume=pcr_volume(chain),
        max_pain=pain,
        resistance=resistance,
        support=support,
        concentration=concentration(chain),
        straddle=atm_straddle(chain, spot),
        migration=strike_migration(chain, previous) if previous else None,
    )
    if price_change is not None and oi_change is not None:
        out["matrix"], out["matrix_vote"] = oi_price_matrix(price_change, oi_change)
    # Distance to max pain is the tradeable form of it: pinning is a pull
    # TOWARDS a level, so the gap is the signal, not the level itself.
    if pain and spot:
        out["max_pain_gap_pct"] = (pain / spot - 1) * 100
    return out
