"""Transparent evidence ranking, independent of production strategy gates.

Weights are research hypotheses, not probabilities or a proven trading edge.
Unknown evidence earns no points and never means 'no adverse news'.
"""
import math
from datetime import datetime

import pandas as pd

from .store import latest

MIN_TURNOVER = 250_000_000
BENCHMARKS = {"NIFTY": "NIFTYBEES", "BANKNIFTY": "BANKBEES"}


def _finite(value):
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _features(frame, asof):
    if not isinstance(frame.index, pd.DatetimeIndex) or not {'open','high','low','close','volume'}.issubset(frame.columns):
        return None
    g = frame.loc[:asof].sort_index()
    if len(g) < 127 or g.index[-1] != asof or not g.index.is_unique:
        return None
    cols = g[["open", "high", "low", "close", "volume"]].tail(127).apply(pd.to_numeric, errors="coerce")
    if (not cols.map(lambda x: math.isfinite(x)).all().all()
            or (cols[["open", "high", "low", "close"]] <= 0).any().any()
            or (cols.volume < 0).any() or (cols.high < cols.low).any()
            or (cols.close > cols.high).any() or (cols.close < cols.low).any()
            or (cols.open > cols.high).any() or (cols.open < cols.low).any()):
        return None
    c, v = cols.close, cols.volume
    # Unreviewed jumps can be a split, consolidation or corrupt adjustment.
    if c.pct_change().abs().max() > .40:
        return None
    price = float(c.iloc[-1])
    prior_volume = float(v.iloc[-21:-1].mean())
    tr = pd.concat([cols.high-cols.low, (cols.high-c.shift()).abs(),
                    (cols.low-c.shift()).abs()], axis=1).max(axis=1)
    high20, low20 = float(cols.high.iloc[-21:-1].max()), float(cols.low.iloc[-21:-1].min())
    return dict(price=price, turnover=float((c*v).tail(20).median()),
        return20_pct=float((c.iloc[-1]/c.iloc[-21]-1)*100),
        return126_pct=float((c.iloc[-1]/c.iloc[-127]-1)*100),
        return20_start=str(c.index[-21])[:10], return20_end=str(c.index[-1])[:10],
        return126_start=str(c.index[-127])[:10], return126_end=str(c.index[-1])[:10],
        above50=bool(price > c.tail(50).mean()),
        relative_volume=float(v.iloc[-1]/prior_volume) if prior_volume > 0 else None,
        distance_high_pct=(price/high20-1)*100,
        support=low20, resistance=high20, atr_pct=float(tr.tail(14).mean()/price*100),
        setup=("controlled breakout" if high20 < price <= high20*1.02 else
               "pullback" if high20*.92 <= price <= high20*.98 and price > c.tail(50).mean()
               else "no controlled entry setup"))


def _quality(f, sector, price):
    if f is None:
        return None, ["financial statements unavailable or stale"]
    flags, points = [], 0
    bank = any(word in sector.lower() for word in ("bank", "financial", "finance", "insurance"))
    roe, growth, margin = (_finite(f.get(k)) for k in ("roe_pct", "earnings_growth_pct", "profit_margin_pct"))
    if roe is not None: points += 10 * min(max(roe/20, 0), 1)
    if growth is not None: points += 10 * min(max(growth/15, 0), 1)
    if margin is not None: points += 5 * min(max(margin/15, 0), 1)
    years = int(f.get("earnings_years") or 0)
    if years >= 3: points += 5 * (int(f.get("positive_earnings_years") or 0)/years)
    if bank:
        flags.append("financial-sector asset quality / capital adequacy not verified")
        # Do not reward low industrial leverage or OCF for a lender.
    else:
        debt, conversion = (_finite(f.get(k)) for k in ("debt_equity", "cash_conversion"))
        if debt is not None: points += 5 * max(0, 1-debt/2)
        if conversion is not None: points += 5 * min(max(conversion, 0), 1)
        if debt is None or conversion is None: flags.append("leverage or cash-flow evidence missing")
        if debt is not None and debt > 2: flags.append("high leverage; review required")
        if conversion is not None and conversion < 0: flags.append("negative operating cash flow")
    if roe is None or growth is None or margin is None or years < 3:
        flags.append("incomplete profitability / earnings history")
    if growth is not None and growth < 0:
        flags.append("annual earnings contracted")
    if (_finite(f.get("annual_income")) or 0) <= 0: flags.append("annual loss")
    eps = _finite(f.get("basic_eps"))
    f = dict(f, price_to_annual_eps=round(price/eps, 2)
             if eps and eps > 0 and f.get("statement_currency") == "INR" else None)
    return dict(f, points=round(points, 2)), flags


def build(tails, eligible, sectors, con, now, asof):
    features = {s: _features(g, asof) for s, g in tails.items()}
    benchmark = features.get("NIFTYBEES")
    def matched(feat):
        return bool(benchmark and (feat['return20_start'],feat['return20_end']) ==
                    (benchmark['return20_start'],benchmark['return20_end']))
    universe = []
    excluded = {}
    for symbol in sorted(eligible or []):
        feat = features.get(symbol)
        if not feat:
            excluded[symbol] = "missing / invalid completed-session history"
        elif feat["turnover"] < MIN_TURNOVER or feat["price"] < 50:
            excluded[symbol] = "below research liquidity / price floor"
        else:
            universe.append(symbol)
    sector_returns = {}
    for symbol in universe:
        sector = sectors.get(symbol)
        if sector and sector not in ("NSE Listed Equity", "Other", "Unknown") and matched(features[symbol]):
            sector_returns.setdefault(sector, []).append(features[symbol]["return20_pct"])
    peer_returns = {s: sorted(v)[len(v)//2] for s,v in sector_returns.items() if len(v) >= 3}
    rows = []
    for symbol in universe:
        feat = dict(features[symbol])
        sector = sectors.get(symbol) or "Unknown"
        rs = feat["return20_pct"]-benchmark["return20_pct"] if matched(feat) else None
        sector_rs = peer_returns.get(sector)
        sector_rs = sector_rs-benchmark["return20_pct"] if sector_rs is not None and benchmark else None
        f, flags = _quality(latest(con, symbol, "fundamentals", now, 14), sector, feat["price"])
        news = latest(con, symbol, "news", now, 2/24)
        participation = latest(con, symbol, "participation", now, 7)
        earnings = latest(con, symbol, "earnings", now, 2)
        # Published delivery and deals are previous/completed-session evidence.
        if participation and participation.get("session") != str(asof)[:10]:
            participation = None
        delivery = _finite((participation or {}).get("delivery_pct"))
        avg = _finite((participation or {}).get("delivery_avg20_pct"))
        if news is None: flags.append("official news feed not freshly checked")
        else:
            if any(e["classification"] == "risk_review" for e in news.get("events", [])):
                flags.append("adverse filing headline; manual review required")
        if earnings is None: flags.append("earnings calendar not freshly checked")
        elif earnings.get("date"):
            days = (datetime.fromisoformat(earnings["date"]).date()-now.date()).days
            if 0 <= days <= 2: flags.append("results due within two days")
        if delivery is None or avg is None: flags.append("delivery confirmation unavailable")
        if rs is None:
            flags.append("Nifty comparison dates do not match" if benchmark else "Nifty benchmark history unavailable")
        if sector_rs is None: flags.append("sector comparison unavailable")
        if feat["setup"] == "no controlled entry setup":
            flags.append("no controlled entry setup")
        technical = 10*float(feat["above50"]) + 10*float(rs is not None and rs > 0) + 10*float(feat["return126_pct"] > 0)
        part_score = 7.5*float(delivery is not None and avg is not None and delivery > avg)
        part_score += 7.5*float(feat["relative_volume"] is not None and feat["relative_volume"] >= 2)
        sector_score = 15*float(sector_rs is not None and sector_rs > 0)
        points = dict(quality=(f or {}).get("points", 0), technical=technical,
                      participation=part_score, sector=sector_score)
        status = "RESEARCH" if not flags else "REVIEW REQUIRED"
        rows.append(dict(symbol=symbol, sector=sector, status=status,
            sector_evidence=latest(con, symbol, "sector", now, 7),
            score=round(sum(points.values()), 2), components=points,
            metrics=dict(feat, rs_vs_nifty20_pct=round(rs, 2) if rs is not None else None,
                         sector_rs20_pct=round(sector_rs, 2) if sector_rs is not None else None,
                         rs_benchmark="NIFTYBEES", rs_benchmark_kind="Nifty equity ETF proxy"),
            fundamentals=f, news=news, earnings=earnings, participation=participation,
            flags=flags, actionable=False))
    rows.sort(key=lambda r: (bool(r["flags"]), -r["score"], r["symbol"]))
    # Valuation is a comparable-sector diagnostic, not 'cheap = buy'.
    pe_peers = {}
    for row in rows:
        pe = (row["fundamentals"] or {}).get("price_to_annual_eps")
        if pe: pe_peers.setdefault(row["sector"], []).append(pe)
    for row in rows:
        peers = pe_peers.get(row["sector"], [])
        if row["fundamentals"]:
            row["fundamentals"]["sector_median_pe"] = sorted(peers)[len(peers)//2] if len(peers) >= 3 else None
    market = latest(con, "MARKET", "market", now, 7)
    market = dict(market or {}, breadth_above50_pct=round(
        sum(features[s]["above50"] for s in universe)/len(universe)*100, 1) if universe else None,
        breadth_session=str(asof)[:10])
    indices = []
    for index, proxy in BENCHMARKS.items():
        feat = features.get(proxy)
        options = latest(con, index, "options", now, 5/1440)
        historical_options = latest(con, index, "options_snapshot", now, 4)
        indices.append(dict(symbol=index, price_proxy=proxy, metrics=feat,
            options=options, last_option_snapshot=historical_options, market=market, actionable=False,
            flags=(["price proxy history unavailable"] if not feat else []) +
                  (["fresh option chain unavailable"] if options is None else []),
            note="ETF completed-session price proxy; no futures / options execution claim"))
    return dict(status="ok" if eligible else "membership_unavailable", version="evidence-v1",
        generated_at=now.isoformat(), price_asof=str(asof)[:10], data_contract_version="screening-data-v2",
        source="verified NSE membership + completed prices + independently captured evidence",
        universe_count=len(eligible or []), liquid_count=len(universe),
        equities=rows, indices=indices, exclusions=excluded,
        validation=dict(status="unvalidated", note="Dated snapshots accumulate forward; scores are not proven profit probabilities"),
        note="Independent research screen; does not open trades or bypass production risk / regime gates")
