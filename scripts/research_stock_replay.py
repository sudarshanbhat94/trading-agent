"""Research-only stock replay against public daily candles.

The frozen protocol is written before downloads.  Current index constituents
are retrospective survivors, so even a positive holdout is NOT promotion
evidence.  No broker, production book, or order module is imported.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sys
import time
from types import SimpleNamespace
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
import pandas as pd

from app.costs import round_trip
from app.sleeves.base import Candidate
from app.sleeves.risk import BookState, RiskManager
from scripts.audit_market_prices import bars_from_response


SPEC = {
    "version": 2,
    "warmup_start": "2018-01-01",
    "development": ["2021-01-01", "2023-12-31"],
    "retrospective_holdout": ["2024-01-01", "2026-09-22"],
    "capital": 10_000,
    "stock_exposure": 0.30,
    "minimum_ticket": 1_500,
    "stop_risk_budget": 150,
    "slippage_bps_each_side": 20,
    "rules": ["strong_index_regime", "stock_strength_regardless_of_index"],
    "review": "first session of each month, prior completed close to next open",
    "rank": "top three 6/12-month volatility-adjusted momentum, excluding last month; first risk-funded name",
    "eligibility": "Rs 50-3300, median 20-day turnover >=Rs 250m, 6m and 12m positive, <=110% of 50-day mean",
    "exit": "3 ATR stop, 12% trail from prior peak, 45-session time stop; gap through stop at open",
    "costs": "OpenStocks delivery round trip plus 20 bps entry and exit slippage",
    "sizing": "production unified risk manager: 30% sleeve, Rs 150 tactical loss cap including fees/slippage, Rs 1500 minimum ticket",
    "promotion": "forbidden: current constituents create survivorship bias and fundamentals are absent",
}

# Frozen replay settings. Do not silently inherit a later production config
# change and rewrite this experiment's historical result.
REPLAY_SETTINGS = SimpleNamespace(
    capital=10_000., risk_per_trade=.02, daily_loss_limit=.015,
    max_drawdown=.10, max_deployed=.90, max_positions_total=3,
    min_ticket=1_500., min_edge_pct=3.,
    quality_momentum=SimpleNamespace(enabled=True, risk_share=.30, max_positions=1),
)


def _funded_entry(ranked, frames, day, cash, peak, previous_equity):
    """Use the same allocator as paper for each of the three ranked ideas."""
    rm = RiskManager(REPLAY_SETTINGS)
    for _, symbol, reference, atr in ranked[:3]:
        open_price = float(frames[symbol].loc[day, "open"])
        stop = reference - 3 * atr
        candidate = Candidate(symbol=symbol, sleeve="quality_momentum", score=1.,
                              entry=open_price, stop=stop)
        sane, _ = candidate.is_sane()
        if not sane:
            continue
        book = BookState(capital=SPEC["capital"], cash=cash, deployed=0.,
                         open_positions=0, per_sleeve_positions={}, equity=cash,
                         peak_equity=peak, day_pnl=cash-previous_equity)
        allocation = rm.size(candidate, book)
        if allocation.ok:
            return symbol, open_price, stop, allocation.shares
    return None


def _download(symbol: str, out: Path, cache_from: Path | None = None) -> tuple[str, str]:
    path = out / (hashlib.sha256(symbol.encode()).hexdigest()[:16] + ".json")
    if path.exists():
        return symbol, str(path)
    if cache_from is not None:
        cached = cache_from / path.name
        return symbol, str(cached) if cached.is_file() else ""
    start = int(pd.Timestamp(SPEC["warmup_start"], tz="UTC").timestamp())
    end = int(pd.Timestamp("2026-09-23", tz="UTC").timestamp())
    url = "https://query1.finance.yahoo.com/v8/finance/chart/" + quote(symbol + ".NS")
    for attempt in range(3):
        try:
            response = httpx.get(url, params=dict(period1=start, period2=end,
                              interval="1d", events="splits,div"),
                              headers={"User-Agent": "Mozilla/5.0"}, timeout=25)
            response.raise_for_status()
            body = response.json()
            bars, _ = bars_from_response(body)
            if len(bars) < 500:
                raise ValueError("fewer than 500 valid daily bars")
            path.write_text(json.dumps(body), encoding="utf-8")
            return symbol, str(path)
        except (httpx.HTTPError, ValueError, KeyError):
            if attempt == 2:
                return symbol, ""
            time.sleep(1 + attempt * 2)
    return symbol, ""


def _load(symbol: str, path: str):
    if not path:
        return None, "download failed"
    bars, splits = bars_from_response(json.loads(Path(path).read_text()))
    if any(day >= "2020-01-01" for day in splits):
        return None, "split after 2020; unadjusted-bar ambiguity"
    frame = pd.DataFrame.from_dict(bars, orient="index").sort_index()
    frame.index = pd.to_datetime(frame.index)
    if len(frame) < 500 or frame.index[-1] < pd.Timestamp("2026-09-01"):
        return None, "insufficient or stale daily bars"
    return frame, ""


def _atr(frame: pd.DataFrame) -> float:
    h, low, close = frame.high, frame.low, frame.close
    tr = pd.concat((h - low, (h - close.shift()).abs(), (low - close.shift()).abs()), axis=1).max(axis=1)
    return float(tr.tail(14).mean())


def _candidate(symbol: str, history: pd.DataFrame):
    if len(history) < 253:
        return None
    close = history.close
    price = float(close.iloc[-1])
    turnover = float((close * history.volume).tail(20).median())
    if not 50 <= price <= 3300 or turnover < 250_000_000:
        return None
    r6 = float(close.iloc[-22] / close.iloc[-127] - 1)
    r12 = float(close.iloc[-22] / close.iloc[-253] - 1)
    vol = float(close.pct_change().tail(252).std())
    if not all(map(math.isfinite, (r6, r12, vol))) or min(r6, r12, vol) <= 0:
        return None
    if price > float(close.tail(50).mean()) * 1.10:
        return None
    atr = _atr(history)
    if not math.isfinite(atr) or atr <= 0 or price - 3 * atr <= 0:
        return None
    score = (r6 + r12) / (2 * vol * math.sqrt(252))
    return score, symbol, price, atr


def _strong_index(benchmark: pd.DataFrame, asof: pd.Timestamp) -> bool:
    close = benchmark.close.loc[:asof]
    if len(close) < 220:
        return False
    sma = float(close.tail(200).mean())
    earlier = float(close.iloc[:-20].tail(200).mean())
    return float(close.iloc[-1]) > 1.03 * sma and sma > earlier


def replay(frames: dict[str, pd.DataFrame], benchmark: pd.DataFrame,
           rule: str, window: list[str]):
    capital = float(SPEC["capital"])
    cash = peak = capital
    position = None
    trades = []
    curve = []
    eligible_reviews = ranked_reviews = unfunded_reviews = 0
    slip = SPEC["slippage_bps_each_side"] / 10_000
    dates = benchmark.loc[window[0]:window[1]].index
    for i, day in enumerate(dates):
        asof = benchmark.index[benchmark.index < day][-1]
        exited = False
        if position is not None:
            symbol = position["symbol"]
            bar = frames[symbol].loc[day] if day in frames[symbol].index else None
            if bar is not None:
                reason = None
                stop = position["stop"]
                if float(bar.open) <= stop:
                    exit_price, reason = float(bar.open) * (1 - slip), "gap_stop"
                elif float(bar.low) <= stop:
                    exit_price, reason = stop * (1 - slip), "stop"
                elif i - position["day"] >= 45:
                    exit_price, reason = float(bar.close) * (1 - slip), "time"
                if reason:
                    qty = position["qty"]
                    gross = qty * (exit_price - position["entry"])
                    charge = round_trip(qty * position["entry"], qty * exit_price)
                    cash += qty * exit_price - charge
                    trades.append(dict(symbol=symbol, entry_date=position["date"],
                                       exit_date=str(day.date()), pnl=round(gross - charge, 2),
                                       gross=round(gross, 2), fees=round(charge, 2), reason=reason))
                    position = None
                    exited = True
                else:
                    position["mark"] = float(bar.close)
                    position["peak"] = max(position["peak"], float(bar.high))
                    position["stop"] = max(stop, position["peak"] * .88)
        prior = benchmark.index[benchmark.index < day][-1]
        first_session = (day.year, day.month) != (prior.year, prior.month)
        if position is None and not exited and first_session:
            eligible = rule != "strong_index_regime" or _strong_index(benchmark, asof)
            if eligible:
                eligible_reviews += 1
                ranked = []
                for sym, frame in frames.items():
                    if asof not in frame.index or day not in frame.index:
                        continue
                    candidate = _candidate(sym, frame.loc[:asof])
                    if candidate is not None:
                        ranked.append(candidate)
                ranked.sort(key=lambda item: (-item[0], item[1]))
                if ranked:
                    ranked_reviews += 1
                    previous_equity = curve[-1][1] if curve else capital
                    funded = _funded_entry(ranked, frames, day, cash, peak,
                                           previous_equity)
                    if funded:
                        sym, open_price, stop, qty = funded
                        entry = open_price * (1 + slip)
                        cash -= qty * entry
                        position = dict(symbol=sym, qty=qty, entry=entry,
                                        stop=stop, peak=entry, mark=entry,
                                        date=str(day.date()), day=i)
                    else:
                        unfunded_reviews += 1
        if position is not None:
            sym = position["symbol"]
            if day in frames[sym].index:
                position["mark"] = float(frames[sym].loc[day, "close"])
            value = position["qty"] * position["mark"]
            equity = cash + value - round_trip(position["qty"] * position["entry"], value)
        else:
            equity = cash
        peak = max(peak, equity)
        curve.append((str(day.date()), equity, equity / peak - 1))
    if position is not None:
        sym = position["symbol"]
        exit_price = position["mark"] * (1 - slip)
        qty = position["qty"]
        gross = qty * (exit_price - position["entry"])
        charge = round_trip(qty * position["entry"], qty * exit_price)
        cash += qty * exit_price - charge
        trades.append(dict(symbol=sym, entry_date=position["date"],
                           exit_date=curve[-1][0], pnl=round(gross - charge, 2),
                           gross=round(gross, 2), fees=round(charge, 2), reason="research_close"))
        curve[-1] = (curve[-1][0], cash, cash / max(peak, cash) - 1)
    return dict(return_pct=round(100 * (cash / capital - 1), 3),
                net_pnl=round(cash - capital, 2),
                max_drawdown_pct=round(100 * min(x[2] for x in curve), 3),
                trades=len(trades), wins=sum(t["pnl"] > 0 for t in trades),
                gross=round(sum(t["gross"] for t in trades), 2),
                fees=round(sum(t["fees"] for t in trades), 2),
                eligible_reviews=eligible_reviews, ranked_reviews=ranked_reviews,
                unfunded_reviews=unfunded_reviews,
                closed_trades=trades)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols-file", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--cache-from", type=Path,
                        help="Read existing vendor responses offline; never fetch missing files")
    args = parser.parse_args()
    symbols = sorted(set(args.symbols_file.read_text().split()))
    if not 80 <= len(symbols) <= 110:
        raise SystemExit("expected a complete Nifty100 snapshot")
    args.out.mkdir(parents=True, exist_ok=True)
    manifest = args.out / "frozen-spec.json"
    allocator_source = Path(__file__).resolve().parents[1] / "app" / "sleeves" / "risk.py"
    protocol = dict(SPEC, symbols=symbols,
                    snapshot_sha256=hashlib.sha256(args.symbols_file.read_bytes()).hexdigest(),
                    allocator_sha256=hashlib.sha256(allocator_source.read_bytes()).hexdigest())
    encoded = json.dumps(protocol, sort_keys=True, indent=2)
    if manifest.exists() and manifest.read_text() != encoded:
        raise SystemExit("frozen protocol changed; use a new output directory")
    manifest.write_text(encoded)
    cache = args.out / "candles"
    cache.mkdir(exist_ok=True)
    with ThreadPoolExecutor(max_workers=4) as pool:
        downloaded = dict(pool.map(lambda s: _download(s, cache, args.cache_from),
                                   symbols + ["NIFTYBEES"]))
    frames, excluded, source_hashes = {}, {}, {}
    for sym, path in downloaded.items():
        frame, why = _load(sym, path)
        if frame is None:
            excluded[sym] = why
        else:
            frames[sym] = frame
            source_hashes[sym] = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    benchmark = frames.pop("NIFTYBEES", None)
    if benchmark is None:
        raise SystemExit("NIFTYBEES benchmark missing; replay cannot run")
    results = {label: {rule: replay(frames, benchmark, rule, SPEC[label])
                       for rule in SPEC["rules"]}
               for label in ("development", "retrospective_holdout")}
    report = dict(protocol=protocol, usable_symbols=len(frames), excluded=excluded,
                  source_hashes=source_hashes, results=results,
                  verdict="NOT PROMOTABLE: current-membership survivorship and missing fundamentals")
    (args.out / "report.json").write_text(json.dumps(report, indent=2))
    print(json.dumps({"usable_symbols": len(frames), "excluded": len(excluded),
                      "results": {label: {rule: {k: v[k] for k in (
                          "return_pct", "max_drawdown_pct", "trades", "wins", "gross", "fees",
                          "eligible_reviews", "ranked_reviews", "unfunded_reviews")}
                          for rule, v in runs.items()} for label, runs in results.items()}}, indent=2))


if __name__ == "__main__":
    main()
