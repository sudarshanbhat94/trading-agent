"""Meta-label filter — the secondary "should we actually take this signal?"
model that sits on top of the deterministic v2 engine (Lopez de Prado
meta-labeling).

The primary engine (v2_engine) decides the SIDE. This model, trained offline by
scripts/meta_label_train.py on the full IN candle history with purged
walk-forward validation, estimates P(win) for each swing/gap candidate from
point-in-time features and lets the engine (a) drop low-probability signals and
(b) rank the survivors by quality.

Proven on real OCI data (15 months, ~26k signals, out-of-sample): the raw
engine is a net loser after costs (avg -0.27%/trade, PF 0.91); filtering to the
model's higher-confidence signals lifts it to +0.5..+1.0%/trade (PF 1.2-1.4).
The edge is dominated by regime/breadth timing, not stock selection.

Fail-safe: if the model file is missing or anything errors, score() returns
None for every symbol and the engine trades exactly as before. Live trading is
never blocked by this layer.
"""
from __future__ import annotations

import os

import pandas as pd

from . import v2_engine as eng

MODEL_PATH = os.environ.get("META_MODEL_PATH", "/opt/opentrade/var/meta_model_%s.pkl")
# probability floor below which a signal is dropped. Tunable per market via env.
FLOOR = float(os.environ.get("META_FLOOR", "0.58"))

# must match scripts/meta_label_train.py FEATURES order exactly
FEATURES = ["conviction", "mom20", "mom63", "rs20", "atr_pct", "dist_hi20",
            "rng_pos", "rvol", "vol_contract", "mkt_mom20", "gap_pct",
            "regime_on", "regime_neutral", "regime_strong", "is_gap",
            "day_rank", "day_breadth", "dow"]

_CACHE: dict = {}   # market -> (mtime, bundle) so we pick up retrains without a restart


def _bundle(market):
    path = MODEL_PATH % market if "%s" in MODEL_PATH else MODEL_PATH
    try:
        mt = os.path.getmtime(path)
    except OSError:
        return None
    cached = _CACHE.get(market)
    if cached and cached[0] == mt:
        return cached[1]
    try:
        import joblib
        bundle = joblib.load(path)
        _CACHE[market] = (mt, bundle)
        return bundle
    except Exception:
        return None


def _f(row, key):
    v = row.get(key)
    try:
        return float(v) if pd.notna(v) else 0.0
    except Exception:
        return 0.0


def score(sigs, tails, mdf, asof, state, strong, market="IN"):
    """Return {symbol: P(win)} for the swing/gap candidates in `sigs`.

    Recomputes features with the SAME eng.compute_features used in training, so
    the live feature vector is identical to the trained one. Symbols the model
    doesn't cover (e.g. mom_breakout) get None. Any failure -> all None (no-op).
    """
    out = {s["symbol"]: None for s in sigs}
    bundle = _bundle(market)
    if bundle is None:
        return out
    model, feats = bundle["model"], bundle.get("features", FEATURES)
    # candidate set = the swing/gap signals the model was trained on; breadth &
    # rank are computed over exactly this set (mirrors the research assemble()).
    # CRITICAL: training counted swing candidates only when regime != OFF (the
    # engine gates them out on OFF days) — day_breadth is the model's #1
    # feature, so live must count exactly the same population.
    cand = [s for s in sigs if s["strategy"] == "gap_momentum"
            or (s["strategy"] == "swing_meanrev" and state != "OFF")]
    if not cand:
        return out
    cand_sorted = sorted(cand, key=lambda s: -s["score"])
    breadth = len(cand_sorted)
    rank_of = {id(s): r for r, s in enumerate(cand_sorted)}
    try:
        dow = pd.Timestamp(asof).dayofweek
        rows = []
        keep = []
        for s in cand:
            g = tails.get(s["symbol"])
            if g is None or asof not in g.index:
                continue
            gf = eng.compute_features(g, mdf)
            if asof not in gf.index:
                continue
            r = gf.loc[asof]
            is_gap = 1 if s["strategy"] == "gap_momentum" else 0
            gap_pct = 0.0
            if is_gap:
                try:
                    prev = float(g["close"].iloc[g.index.get_loc(asof) - 1])
                    gap_pct = (float(r["open"]) / prev - 1) * 100 if prev > 0 else 0.0
                except Exception:
                    gap_pct = 0.0
            rows.append({
                "conviction": float(s["score"]),
                "mom20": _f(r, "mom20"), "mom63": _f(r, "mom63"), "rs20": _f(r, "rs20"),
                "atr_pct": _f(r, "atr_pct"), "dist_hi20": _f(r, "dist_hi20"),
                "rng_pos": _f(r, "rng_pos"), "rvol": _f(r, "rvol"),
                "vol_contract": _f(r, "vol_contract"), "mkt_mom20": _f(r, "mkt_mom20"),
                "gap_pct": gap_pct,
                "regime_on": 1 if state == "ON" else 0,
                "regime_neutral": 1 if state == "NEUTRAL" else 0,
                "regime_strong": 1 if strong else 0,
                "is_gap": is_gap,
                "day_rank": rank_of[id(s)], "day_breadth": breadth, "dow": int(dow),
            })
            keep.append(s["symbol"])
        if not rows:
            return out
        X = pd.DataFrame(rows)[feats]
        probs = model.predict_proba(X)[:, 1]
        for sym, p in zip(keep, probs):
            out[sym] = float(p)
    except Exception:
        return {s["symbol"]: None for s in sigs}
    return out


def floor(market="IN"):
    """The probability floor to trade; None if no model is loaded (=> no-op)."""
    b = _bundle(market)
    if b is None:
        return None
    return float(b.get("floor", FLOOR))
