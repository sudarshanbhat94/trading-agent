"""Pre-trade factor investigation.

Before the engine is allowed to buy anything, every candidate is run through a
thorough, cross-sectional multi-factor investigation and a battery of hard risk
gates. Grounded in standard systematic practice:

  * a multi-factor composite — trend, momentum, relative strength, volume,
    volatility-quality, setup, liquidity — each standardized ACROSS the universe
    (z-score -> 0..100) so we rank a name against its peers, not an absolute scale
  * hard gates that can veto regardless of score — liquidity floor, price floor,
    news veto (fraud/regulatory/downgrade), deep-drawdown (falling knife),
    regime, sector-concentration cap, data quality
  * ATR/volatility-based position sizing (risk a constant fraction of the pool;
    smaller size when volatility is high) and liquidity-aware caps

The composite weights differ by strategy intent: mean-reversion rewards a healthy
dip near support with relative strength; gap-momentum rewards trend + momentum +
volume confirmation near highs.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import v2_engine as eng

# hard gates -------------------------------------------------------------------
LIQ_FLOOR = {"IN": 1.0e7, "US": 5.0e6}     # min median 20d turnover (₹ / $)
MIN_PRICE = {"IN": 50.0, "US": 5.0}
MAX_DRAWDOWN = -0.55                         # skip names down >55% from 1y high (falling knife)
MAX_PER_SECTOR = 3                           # portfolio concentration cap
# catch-all labels in the universe metadata that are NOT real sectors — applying
# the concentration cap to these freezes the whole book (e.g. 3 positions tagged
# "NSE Listed Equity" blocked 70 candidates). Exempt them from the cap.
GENERIC_SECTORS = {"", "unknown", "n/a", "none", "nse listed equity", "bse listed equity",
                   "us listed equity", "listed equity", "equity", "misc", "others", "other"}

# composite weights by strategy (sum ~1.0)
WEIGHTS = {
    "swing_meanrev": dict(trend=0.10, momentum=0.06, rel_strength=0.18, volume=0.14,
                          vol_quality=0.14, setup=0.28, liquidity=0.10),
    "gap_momentum":  dict(trend=0.20, momentum=0.22, rel_strength=0.16, volume=0.20,
                          vol_quality=0.06, setup=0.06, liquidity=0.10),
    "mom_breakout":  dict(trend=0.22, momentum=0.24, rel_strength=0.18, volume=0.18,
                          vol_quality=0.04, setup=0.06, liquidity=0.08),
}
BUY_MIN = 58.0      # composite >= this AND all gates pass -> eligible to buy
WATCH_MIN = 45.0


def _rsi(c: pd.Series, n: int = 14) -> pd.Series:
    d = c.diff()
    up = d.clip(lower=0).rolling(n).mean()
    dn = (-d.clip(upper=0)).rolling(n).mean()
    rs = up / dn.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(50)


def _z(s: pd.Series, sign: int = 1) -> pd.Series:
    """Cross-sectional z-score mapped to 0..100 (50 = median)."""
    sd = s.std()
    if not sd or np.isnan(sd):
        return pd.Series(50.0, index=s.index)
    z = ((s - s.mean()) / sd).clip(-3, 3)
    return (50 + sign * z / 3 * 50).clip(0, 100)


def build_factor_panel(syms: dict, market_df: pd.DataFrame, asof) -> pd.DataFrame:
    """Per-symbol raw factor values at `asof` (one row per symbol)."""
    rows = []
    for sym, g in syms.items():
        if asof not in g.index:
            continue
        gi = g.loc[:asof]
        if len(gi) < 70:      # match the signal path's minimum — a 120-bar floor here
            continue          # made 62% of valid candidates invisible (auto-rejected)
        gf = eng.compute_features(gi, market_df)
        row = gf.iloc[-1]
        if any(pd.isna(row.get(k)) for k in ("sma50", "atr14", "rs20", "rvol", "atr_pct")):
            continue
        c, v = gf["close"], gi["volume"]
        sma50 = gf["sma50"]
        slope = float(sma50.iloc[-1] / sma50.iloc[-6] - 1) if len(sma50) > 6 and sma50.iloc[-6] else 0.0
        v20 = v.tail(20).mean()
        rows.append(dict(
            symbol=sym, close=float(c.iloc[-1]), atr14=float(row["atr14"]), atr_pct=float(row["atr_pct"]),
            mom20=float(row["mom20"]), mom63=float(row["mom63"]) if not pd.isna(row["mom63"]) else 0.0,
            rs20=float(row["rs20"]), rvol=float(row["rvol"]), dist_hi20=float(row["dist_hi20"]),
            rng_pos=float(row["rng_pos"]) if not pd.isna(row["rng_pos"]) else 0.5,
            vol_contract=float(row["vol_contract"]) if not pd.isna(row["vol_contract"]) else 1.0,
            rsi=float(_rsi(c).iloc[-1]),
            trend_align=int(c.iloc[-1] > row["sma20"]) + int(row["sma20"] > row["sma50"]) + int(slope > 0),
            sma50_slope=slope,
            turnover=float((c * v).tail(20).median()),
            dd252=float(c.iloc[-1] / c.tail(252).max() - 1) if c.tail(252).max() else 0.0,
            vol_trend=float(v.tail(5).mean() / v20) if v20 else 1.0,
        ))
    return pd.DataFrame(rows).set_index("symbol") if rows else pd.DataFrame()


def score_panel(fp: pd.DataFrame) -> pd.DataFrame:
    """Cross-sectional factor scores (0..100) for every symbol in the panel."""
    sc = pd.DataFrame(index=fp.index)
    sc["trend"] = (fp["trend_align"] / 3 * 60 + _z(fp["sma50_slope"]) * 0.4).clip(0, 100)
    sc["momentum"] = (_z(fp["mom63"]) * 0.5 + _z(fp["mom20"]) * 0.3
                      + (100 - (fp["rsi"] - 50).abs() * 1.5).clip(0, 100) * 0.2)
    sc["rel_strength"] = _z(fp["rs20"])
    sc["volume"] = (_z(fp["rvol"]) * 0.6 + _z(fp["vol_trend"]) * 0.4)
    ideal = 0.025
    sc["vol_quality"] = (100 - ((fp["atr_pct"] - ideal).abs() / ideal * 55).clip(0, 100))
    sc["setup_mr"] = ((-fp["dist_hi20"] / 0.10).clip(0, 1) * 55 + (1 - fp["rng_pos"]).clip(0, 1) * 45)
    sc["setup_mom"] = (fp["rng_pos"].clip(0, 1) * 60 + (fp["dist_hi20"] > -0.03).astype(float) * 40)
    sc["liquidity"] = _z(fp["turnover"])
    return sc


def _size_multiplier(atr_pct: float) -> float:
    """Scale a full equal-weight slot inversely with volatility (ATR%), so each
    position risks a more constant fraction of the pool. Clamped 0.4x..1.4x."""
    base = 0.025
    return float(np.clip(base / max(atr_pct, 0.005), 0.4, 1.4))


def _reasons(s: pd.Series, f: pd.Series, strategy: str) -> list[str]:
    out = []
    if s["rel_strength"] >= 65:
        out.append(f"strong relative strength ({s['rel_strength']:.0f})")
    if s["volume"] >= 65:
        out.append(f"volume confirmation (rvol {f['rvol']:.1f})")
    if strategy == "swing_meanrev" and s["setup_mr"] >= 60:
        out.append(f"healthy dip near support ({f['dist_hi20']*100:.0f}% off 20d high)")
    if strategy == "gap_momentum" and s["setup_mom"] >= 60:
        out.append("trading near highs / breakout")
    if s["vol_quality"] < 35:
        out.append(f"volatility off-ideal (atr {f['atr_pct']*100:.1f}%)")
    if f["rsi"] > 75:
        out.append(f"overbought (rsi {f['rsi']:.0f})")
    return out[:4]


def investigate(symbol: str, fp: pd.DataFrame, sc: pd.DataFrame, market: str, strategy: str,
                regime_state: str, news_severe: bool, held_sectors: dict, sector_map: dict) -> dict:
    """Full pre-trade report for one candidate: composite, hard gates, sizing, reasons.
    `regime_state` is "ON"/"NEUTRAL"/"OFF" (see v2_engine.regime_state)."""
    base = dict(symbol=symbol, market=market, strategy=strategy)
    if symbol not in fp.index:
        return {**base, "verdict": "AVOID", "composite": 0.0, "gates_failed": ["no_fresh_data"], "size_mult": 0.0}
    f, s = fp.loc[symbol], sc.loc[symbol]
    gates = []
    if f["turnover"] < LIQ_FLOOR.get(market, 0):
        gates.append("illiquid")
    if f["close"] < MIN_PRICE.get(market, 0):
        gates.append("price_floor")
    if news_severe:
        gates.append("news_veto")
    if f["dd252"] < MAX_DRAWDOWN:
        gates.append("deep_drawdown")
    if strategy == "swing_meanrev" and regime_state == "OFF":
        gates.append("regime_risk_off")
    sector = sector_map.get(symbol) or "unknown"
    if sector == "ETF":                      # never trade ETFs/funds as single-stock picks
        gates.append("is_etf")
    if sector.strip().lower() not in GENERIC_SECTORS and held_sectors.get(sector, 0) >= MAX_PER_SECTOR:
        gates.append("sector_full")
    w = WEIGHTS[strategy]
    setup = float(s["setup_mom"] if strategy in ("gap_momentum", "mom_breakout") else s["setup_mr"])
    composite = float(
        w["trend"] * s["trend"] + w["momentum"] * s["momentum"] + w["rel_strength"] * s["rel_strength"]
        + w["volume"] * s["volume"] + w["vol_quality"] * s["vol_quality"] + w["setup"] * setup
        + w["liquidity"] * s["liquidity"])
    # choppy/neutral market: only the very best dip-buys clear the bar
    buy_min = BUY_MIN + (8.0 if strategy == "swing_meanrev" and regime_state == "NEUTRAL" else 0.0)
    verdict = "AVOID" if gates else ("BUY" if composite >= buy_min else ("WATCH" if composite >= WATCH_MIN else "AVOID"))
    return {**base, "composite": round(composite, 1), "verdict": verdict, "gates_failed": gates,
            "size_mult": round(_size_multiplier(f["atr_pct"]), 2), "sector": sector,
            "factors": {k: round(float(s[k])) for k in ("trend", "momentum", "rel_strength",
                                                        "volume", "vol_quality", "liquidity")},
            "setup": round(setup), "reasons": _reasons(s, f, strategy)}
