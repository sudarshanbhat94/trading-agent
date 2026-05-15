from __future__ import annotations

from typing import Any

from .models import Candle
from .strategy_presets import evaluate_strategy_presets


def _mean(values: Any) -> float:
    items = list(values)
    return sum(items) / len(items) if items else 0.0


def strategy_backtest_snapshot(
    candles: list[Candle],
    execution_cost_bps: float = 0.0,
) -> dict[str, Any]:
    if len(candles) < 60:
        return {
            "backtest_engine": "per_strategy_walk_forward_v1",
            "strategy_backtests": [],
            "best_strategy_backtest": None,
            "backtest_note": "Need at least 60 daily candles for per-strategy walk-forward evidence.",
        }
    states: dict[str, dict[str, Any] | None] = {}
    trades: dict[str, list[dict[str, Any]]] = {}
    for index in range(40, len(candles)):
        history = candles[: index + 1]
        close = candles[index].close
        signals = evaluate_strategy_presets(history, close)
        signal_by_name = {signal.name: signal for signal in signals}
        for name in list(states):
            position = states.get(name)
            if not position:
                continue
            entry = float(position["entry"] or 0.0)
            if entry > 0:
                position["mae_pct"] = min(float(position.get("mae_pct", 0.0)), ((candles[index].low - entry) / entry) * 100)
                position["mfe_pct"] = max(float(position.get("mfe_pct", 0.0)), ((candles[index].high - entry) / entry) * 100)
            exit_price, exit_reason = _exit_for_position(position, candles[index], index)
            if exit_price is None:
                continue
            pnl_pct = ((exit_price - position["entry"]) / position["entry"]) * 100 if position["entry"] else 0.0
            pnl_pct -= execution_cost_bps / 100.0
            trades.setdefault(name, []).append(
                {
                    "entry_index": position["entry_index"],
                    "exit_index": index,
                    "pnl_pct": pnl_pct,
                    "exit": exit_reason,
                    "hold_periods": index - position["entry_index"],
                    "mae_pct": position.get("mae_pct", 0.0),
                    "mfe_pct": position.get("mfe_pct", 0.0),
                }
            )
            states[name] = None
        for name, signal in signal_by_name.items():
            trades.setdefault(name, [])
            states.setdefault(name, None)
            if states[name] is not None or signal.direction != "BUY" or signal.score < 0.52:
                continue
            atr = _atr(history, 14) or close * 0.025
            risk = max(atr * 1.5, close * 0.01)
            states[name] = {
                "entry": close,
                "entry_index": index,
                "stop": close - risk,
                "target": close + (risk * 2.0),
                "time_stop": 15,
                "mae_pct": 0.0,
                "mfe_pct": 0.0,
            }
    summaries = [_summarize_strategy(name, rows) for name, rows in trades.items()]
    summaries.sort(key=lambda row: (row["expectancy_pct"], row["profit_factor"], row["trades"]), reverse=True)
    best = summaries[0] if summaries else None
    return {
        "backtest_engine": "per_strategy_walk_forward_v1",
        "execution_cost_bps": round(float(execution_cost_bps or 0.0), 4),
        "strategy_backtests": summaries[:10],
        "best_strategy_backtest": best,
        "backtest_note": "Walk-forward proxy uses historical daily candles, ATR stops, 2R targets, 15-period time stop, and configured cost/slippage bps.",
    }


def _exit_for_position(position: dict[str, Any], candle: Candle, index: int) -> tuple[float | None, str | None]:
    if candle.low <= position["stop"]:
        return float(position["stop"]), "atr_stop"
    if candle.high >= position["target"]:
        return float(position["target"]), "target_2r"
    if index - int(position["entry_index"]) >= int(position["time_stop"]):
        return float(candle.close), "time_stop"
    return None, None


def _summarize_strategy(name: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {
            "strategy": name,
            "trades": 0,
            "win_rate": 0.0,
            "expectancy_pct": 0.0,
            "profit_factor": 0.0,
            "max_drawdown_pct": 0.0,
            "avg_mae_pct": 0.0,
            "avg_mfe_pct": 0.0,
            "avg_hold_periods": 0.0,
            "evidence_quality": "none",
            "last_5": [],
        }
    wins = [row for row in rows if row["pnl_pct"] > 0]
    losses = [row for row in rows if row["pnl_pct"] <= 0]
    gross_win = sum(row["pnl_pct"] for row in wins)
    gross_loss = abs(sum(row["pnl_pct"] for row in losses))
    equity = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for row in rows:
        equity += row["pnl_pct"]
        peak = max(peak, equity)
        max_drawdown = min(max_drawdown, equity - peak)
    return {
        "strategy": name,
        "trades": len(rows),
        "win_rate": round(len(wins) / len(rows), 4),
        "expectancy_pct": round(_mean(row["pnl_pct"] for row in rows), 4),
        "profit_factor": round(gross_win / gross_loss, 4) if gross_loss else round(gross_win, 4),
        "max_drawdown_pct": round(max_drawdown, 4),
        "avg_mae_pct": round(_mean(row.get("mae_pct", 0.0) for row in rows), 4),
        "avg_mfe_pct": round(_mean(row.get("mfe_pct", 0.0) for row in rows), 4),
        "avg_hold_periods": round(_mean(row["hold_periods"] for row in rows), 2),
        "evidence_quality": _evidence_quality(len(rows)),
        "last_5": [
            {
                "pnl_pct": round(row["pnl_pct"], 4),
                "exit": row["exit"],
                "hold_periods": row["hold_periods"],
                "mae_pct": round(row.get("mae_pct", 0.0), 4),
                "mfe_pct": round(row.get("mfe_pct", 0.0), 4),
            }
            for row in rows[-5:]
        ],
    }


def _evidence_quality(trades: int) -> str:
    if trades >= 30:
        return "usable"
    if trades >= 12:
        return "thin"
    if trades > 0:
        return "anecdotal"
    return "none"


def _atr(candles: list[Candle], period: int = 14) -> float | None:
    if len(candles) < period + 1:
        return None
    ranges = []
    for previous, candle in zip(candles[-period - 1 : -1], candles[-period:]):
        ranges.append(max(candle.high - candle.low, abs(candle.high - previous.close), abs(candle.low - previous.close)))
    return sum(ranges) / len(ranges) if ranges else None
