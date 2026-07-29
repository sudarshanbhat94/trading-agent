"""Detect unadjusted corporate actions in the daily candle history.

The feed stores raw traded prices. When a stock splits 1:5 or issues a 1:1
bonus, the close drops by a factor overnight with no adjustment to the earlier
bars, so the series contains a -80% "return" that never happened. There are 32
such bars in the IN history (KOTYARK -91%, FISCHER -89%, ZFCVINDIA -83%).

They are rare — 0.004% of bars — but they are not harmless noise, because they
are exactly what a mean-reversion scorer is hunting for. A stock that appears
to have fallen 83% is maximally oversold on every dip metric at once and will
rank first every time. One artefact outranks every genuine setup in the book.

Detection is deliberately conservative. A real crash and a split look identical
in price alone, so price is not enough:

  * the move must be larger than any ordinary session (default 25%);
  * the ratio must sit near a plausible corporate-action ratio (1:2, 1:5, 2:1,
    3:2 ...) within a tolerance — a genuine -30% news crash rarely lands within
    2% of 1/2 or 1/5;
  * turnover must NOT collapse the way price did. A split leaves value traded
    roughly intact while the share count multiplies; a real crash usually comes
    with a violent change in turnover.

Anything ambiguous is left alone. A missed split costs one bad signal; a
wrongly "corrected" real crash silently rewrites history and would hide a true
loss, which is worse.
"""
from __future__ import annotations

import logging

_LOG = logging.getLogger("openstocks.corpactions")

MOVE_THRESHOLD = 0.25          # only consider moves beyond any ordinary session
RATIO_TOLERANCE = 0.03         # how close to a clean split ratio the move must sit
# Beyond this, a preserved turnover is enough on its own: no ordinary stock
# loses 60% of its price in one session while the value traded holds up.
EXTREME_MOVE = 0.60
# Ratios seen in Indian corporate actions, as new_price / old_price.
SPLIT_RATIOS = (
    1 / 2, 1 / 3, 1 / 4, 1 / 5, 1 / 10, 1 / 20,      # splits and bonuses
    2 / 5, 3 / 10, 1 / 100,                          # face-value changes
    2.0, 3.0, 5.0, 10.0,                             # reverse splits
)


def _near_split_ratio(ratio, tolerance=RATIO_TOLERANCE):
    """Closest plausible corporate-action ratio, or None."""
    if ratio <= 0:
        return None
    for candidate in SPLIT_RATIOS:
        if abs(ratio / candidate - 1) <= tolerance:
            return candidate
    return None


def detect(frame, move_threshold=MOVE_THRESHOLD, tolerance=RATIO_TOLERANCE):
    """Indices of bars that look like an unadjusted corporate action.

    `frame` is one symbol's date-indexed OHLCV. Returns a list of index labels
    whose RETURN is an artefact — the bar itself is a valid price, it is the
    step between it and the previous bar that is fictitious.
    """
    if frame is None or len(frame) < 2:
        return []
    closes = frame["close"].astype(float)
    volumes = frame["volume"].astype(float) if "volume" in frame else None
    hits = []
    for i in range(1, len(closes)):
        prev, cur = closes.iloc[i - 1], closes.iloc[i]
        if prev <= 0 or cur <= 0:
            continue
        ratio = cur / prev
        if abs(ratio - 1) < move_threshold:
            continue
        turnover_held = None
        if volumes is not None:
            prev_turnover = prev * float(volumes.iloc[i - 1] or 0)
            cur_turnover = cur * float(volumes.iloc[i] or 0)
            # A split multiplies share count while value traded survives. If
            # turnover collapsed with the price, this is a real crash.
            if prev_turnover > 0:
                # bool() is load-bearing: pandas yields numpy.bool_, and
                # `numpy.bool_(False) is False` is itself False, so an identity
                # test silently let every collapsed-turnover crash through.
                turnover_held = bool(cur_turnover >= prev_turnover * 0.15)
        if turnover_held is False:
            continue
        # Two independent ways to qualify. Ratio-matching alone missed most of
        # the real cases (KOTYARK -91%, ZFCVINDIA -83%) because Indian actions
        # use ratios like 1:6 or combine a split with a same-day move, so the
        # arithmetic never lands within tolerance. Turnover is the sounder
        # discriminator: no ordinary stock loses 60% of its price while the
        # value traded holds up. Ratio-matching still handles the milder 25-60%
        # cases, where a real crash IS plausible and more care is needed.
        if _near_split_ratio(ratio, tolerance) is not None:
            hits.append(frame.index[i])
        elif abs(ratio - 1) >= EXTREME_MOVE and turnover_held:
            hits.append(frame.index[i])
    return hits


def clean(frame, **kwargs):
    """Return (frame, n_fixed) with corporate-action steps back-adjusted.

    Prices BEFORE the action are rescaled by the ratio so the series is
    continuous, which is what every rolling window and return calculation
    assumes. Volume before the action is scaled inversely, so turnover — the
    quantity the liquidity screens actually use — stays comparable.
    """
    hits = detect(frame, **kwargs)
    if not hits:
        return frame, 0
    out = frame.copy()
    # Cast first: an integer price column rejects the rescaled floats outright,
    # and the exception was being swallowed by clean_all's guard — so a real
    # split was silently left in place while the caller was told it was fine.
    for column in ("open", "high", "low", "close", "volume"):
        if column in out:
            out[column] = out[column].astype(float)
    closes = out["close"]
    for label in hits:
        position = out.index.get_loc(label)
        ratio = float(closes.iloc[position]) / float(closes.iloc[position - 1])
        earlier = out.index[:position]
        for column in ("open", "high", "low", "close"):
            if column in out:
                out.loc[earlier, column] = out.loc[earlier, column] * ratio
        if "volume" in out and ratio > 0:
            out.loc[earlier, "volume"] = out.loc[earlier, "volume"] / ratio
        closes = out["close"]
    return out, len(hits)


def clean_all(panel, **kwargs):
    """Apply clean() across {symbol: frame}. Returns (panel, {symbol: n_fixed})."""
    fixed = {}
    out = {}
    for symbol, frame in panel.items():
        try:
            cleaned, n = clean(frame, **kwargs)
        except Exception:
            out[symbol] = frame
            continue
        out[symbol] = cleaned
        if n:
            fixed[symbol] = n
    if fixed:
        _LOG.info("corporate actions back-adjusted in %d symbols (%d bars)",
                  len(fixed), sum(fixed.values()))
    return out, fixed
