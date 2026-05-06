from __future__ import annotations

import csv
import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable

from .models import Candle, Decision, Quote, utc_now


def _optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    @contextmanager
    def connect(self):
        with self._lock:
            conn = sqlite3.connect(self.path)
            conn.row_factory = sqlite3.Row
            try:
                yield conn
                conn.commit()
            finally:
                conn.close()

    def init(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                create table if not exists universe (
                    symbol text primary key,
                    name text not null,
                    exchange text not null default 'NSE',
                    yahoo_symbol text,
                    kite_symbol text,
                    upstox_instrument_key text,
                    nubra_symbol text,
                    nubra_ref_id integer,
                    sector text,
                    base_price real not null default 100,
                    enabled integer not null default 1
                );

                create table if not exists latest_quotes (
                    symbol text primary key,
                    ts text not null,
                    price real not null,
                    open real,
                    high real,
                    low real,
                    close real,
                    volume real,
                    source text not null
                );

                create table if not exists market_ticks (
                    id integer primary key autoincrement,
                    ts text not null,
                    symbol text not null,
                    price real not null,
                    source text not null
                );

                create table if not exists candles (
                    symbol text not null,
                    ts text not null,
                    open real not null,
                    high real not null,
                    low real not null,
                    close real not null,
                    volume real not null,
                    source text not null,
                    primary key (symbol, ts, source)
                );

                create table if not exists decisions (
                    id integer primary key autoincrement,
                    ts text not null,
                    symbol text not null,
                    action text not null,
                    strategy text not null default 'unknown',
                    confidence real not null,
                    price real not null,
                    technical_score real not null,
                    sentiment_score real not null,
                    reason text not null,
                    details_json text not null default '{}'
                );

                create table if not exists orders (
                    id integer primary key autoincrement,
                    ts text not null,
                    symbol text not null,
                    side text not null,
                    strategy text not null default 'unknown',
                    qty integer not null,
                    price real not null,
                    notional real not null,
                    status text not null,
                    reason text not null,
                    details_json text not null default '{}'
                );

                create table if not exists positions (
                    symbol text primary key,
                    strategy text not null default 'unknown',
                    qty integer not null,
                    avg_price real not null,
                    market_price real not null,
                    realized_pnl real not null default 0,
                    updated_at text not null,
                    details_json text not null default '{}'
                );

                create table if not exists portfolio_snapshots (
                    id integer primary key autoincrement,
                    ts text not null,
                    cash real not null,
                    invested real not null,
                    market_value real not null,
                    equity real not null,
                    realized_pnl real not null,
                    unrealized_pnl real not null
                );

                create table if not exists agent_state (
                    key text primary key,
                    value text not null
                );

                create table if not exists runtime_settings (
                    key text primary key,
                    value text not null,
                    updated_at text not null
                );

                create table if not exists sentiment_events (
                    id integer primary key autoincrement,
                    ts text not null,
                    symbol text not null,
                    score real not null,
                    headline_count integer not null,
                    headlines_json text not null,
                    confidence real not null default 0,
                    events_json text not null default '[]'
                );

                create table if not exists agent_logs (
                    id integer primary key autoincrement,
                    ts text not null,
                    level text not null,
                    component text not null,
                    event text not null,
                    message text not null,
                    details_json text not null default '{}'
                );

                create table if not exists delivery_data (
                    symbol text not null,
                    date text not null,
                    close real,
                    total_volume real,
                    delivery_volume real,
                    delivery_pct real,
                    primary key (symbol, date)
                );

                create index if not exists idx_market_ticks_symbol_ts
                    on market_ticks(symbol, ts);
                create index if not exists idx_candles_symbol_ts
                    on candles(symbol, ts);
                create index if not exists idx_decisions_ts
                    on decisions(ts);
                create index if not exists idx_orders_ts
                    on orders(ts);
                create index if not exists idx_agent_logs_ts
                    on agent_logs(ts);
                create index if not exists idx_delivery_symbol_date
                    on delivery_data(symbol, date);
                """
            )
            self._ensure_column(conn, "universe", "upstox_instrument_key", "text")
            self._ensure_column(conn, "universe", "nubra_symbol", "text")
            self._ensure_column(conn, "universe", "nubra_ref_id", "integer")
            self._ensure_column(conn, "decisions", "strategy", "text not null default 'unknown'")
            self._ensure_column(conn, "decisions", "details_json", "text not null default '{}'")
            self._ensure_column(conn, "orders", "strategy", "text not null default 'unknown'")
            self._ensure_column(conn, "orders", "details_json", "text not null default '{}'")
            self._ensure_column(conn, "positions", "strategy", "text not null default 'unknown'")
            self._ensure_column(conn, "positions", "details_json", "text not null default '{}'")
            self._ensure_column(conn, "sentiment_events", "confidence", "real not null default 0")
            self._ensure_column(conn, "sentiment_events", "events_json", "text not null default '[]'")
            self._ensure_column(conn, "delivery_data", "close", "real")
            self._ensure_column(conn, "delivery_data", "total_volume", "real")
            self._ensure_column(conn, "delivery_data", "delivery_volume", "real")
            self._ensure_column(conn, "delivery_data", "delivery_pct", "real")

    def _ensure_column(self, conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
        rows = conn.execute(f"pragma table_info({table})").fetchall()
        if column not in {row["name"] for row in rows}:
            conn.execute(f"alter table {table} add column {column} {definition}")

    def seed_universe(self, csv_path: Path) -> None:
        if not csv_path.exists():
            raise FileNotFoundError(f"Universe CSV not found: {csv_path}")
        with csv_path.open("r", newline="", encoding="utf-8") as handle:
            rows = [self._normalize_universe_row(row) for row in csv.DictReader(handle)]
        symbols = [row["symbol"] for row in rows]
        with self.connect() as conn:
            conn.executemany(
                """
                insert into universe (
                    symbol, name, exchange, yahoo_symbol, kite_symbol, upstox_instrument_key,
                    nubra_symbol, nubra_ref_id,
                    sector, base_price, enabled
                ) values (
                    :symbol, :name, :exchange, :yahoo_symbol, :kite_symbol, :upstox_instrument_key,
                    :nubra_symbol, :nubra_ref_id,
                    :sector, :base_price, :enabled
                )
                on conflict(symbol) do update set
                    name = excluded.name,
                    exchange = excluded.exchange,
                    yahoo_symbol = excluded.yahoo_symbol,
                    kite_symbol = excluded.kite_symbol,
                    upstox_instrument_key = excluded.upstox_instrument_key,
                    nubra_symbol = excluded.nubra_symbol,
                    nubra_ref_id = excluded.nubra_ref_id,
                    sector = excluded.sector,
                    base_price = excluded.base_price,
                    enabled = excluded.enabled
                """,
                rows,
            )
            if symbols:
                placeholders = ",".join("?" for _ in symbols)
                conn.execute(f"update universe set enabled = 0 where symbol not in ({placeholders})", symbols)

    def _normalize_universe_row(self, row: dict[str, Any]) -> dict[str, Any]:
        symbol = str(row.get("symbol", "")).strip()
        exchange = str(row.get("exchange") or "NSE").strip() or "NSE"
        return {
            "symbol": symbol,
            "name": row.get("name") or symbol,
            "exchange": exchange,
            "yahoo_symbol": row.get("yahoo_symbol") or (f"{symbol}.NS" if exchange == "NSE" else ""),
            "kite_symbol": row.get("kite_symbol") or f"{exchange}:{symbol}",
            "upstox_instrument_key": row.get("upstox_instrument_key") or "",
            "nubra_symbol": row.get("nubra_symbol") or symbol,
            "nubra_ref_id": _optional_int(row.get("nubra_ref_id")),
            "sector": row.get("sector") or "",
            "base_price": row.get("base_price") or 100,
            "enabled": row.get("enabled") if row.get("enabled") not in (None, "") else 1,
        }

    def upsert_candles(self, candles_by_symbol: dict[str, list[Candle]]) -> None:
        rows = [candle.to_dict() for candles in candles_by_symbol.values() for candle in candles]
        if not rows:
            return
        with self.connect() as conn:
            conn.executemany(
                """
                insert into candles (symbol, ts, open, high, low, close, volume, source)
                values (:symbol, :ts, :open, :high, :low, :close, :volume, :source)
                on conflict(symbol, ts, source) do update set
                    open = excluded.open,
                    high = excluded.high,
                    low = excluded.low,
                    close = excluded.close,
                    volume = excluded.volume
                """,
                rows,
            )

    def upsert_delivery_data(self, rows: Iterable[dict[str, Any]]) -> None:
        normalized = []
        for row in rows:
            symbol = str(row.get("symbol") or "").strip().upper()
            date = str(row.get("date") or "").strip()
            if not symbol or not date:
                continue
            normalized.append(
                {
                    "symbol": symbol,
                    "date": date,
                    "close": _optional_float(row.get("close")),
                    "total_volume": _optional_float(row.get("total_volume")),
                    "delivery_volume": _optional_float(row.get("delivery_volume")),
                    "delivery_pct": _optional_float(row.get("delivery_pct")),
                }
            )
        if not normalized:
            return
        with self.connect() as conn:
            conn.executemany(
                """
                insert into delivery_data (symbol, date, close, total_volume, delivery_volume, delivery_pct)
                values (:symbol, :date, :close, :total_volume, :delivery_volume, :delivery_pct)
                on conflict(symbol, date) do update set
                    close = excluded.close,
                    total_volume = excluded.total_volume,
                    delivery_volume = excluded.delivery_volume,
                    delivery_pct = excluded.delivery_pct
                """,
                normalized,
            )

    def delivery_rows(self, symbol: str, limit: int = 20) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                select * from delivery_data
                where symbol = ?
                order by date desc
                limit ?
                """,
                (symbol.upper(), limit),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def candles_for_symbols(self, symbols: list[str], limit_per_symbol: int = 80) -> dict[str, list[dict[str, Any]]]:
        if not symbols:
            return {}
        output: dict[str, list[dict[str, Any]]] = {}
        with self.connect() as conn:
            for symbol in symbols:
                rows = conn.execute(
                    """
                    select * from candles
                    where symbol = ?
                    order by ts desc
                    limit ?
                    """,
                    (symbol, limit_per_symbol),
                ).fetchall()
                output[symbol] = [dict(row) for row in reversed(rows)]
        return output

    def get_universe(self, enabled_only: bool = True) -> list[dict[str, Any]]:
        sql = "select * from universe"
        if enabled_only:
            sql += " where enabled = 1"
        sql += " order by symbol"
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(sql).fetchall()]

    def universe_row(self, symbol: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("select * from universe where symbol = ?", (symbol,)).fetchone()
        return dict(row) if row else None

    def upsert_quotes(self, quotes: dict[str, Quote]) -> None:
        rows = [quote.to_dict() for quote in quotes.values()]
        if not rows:
            return
        with self.connect() as conn:
            conn.executemany(
                """
                insert into latest_quotes (
                    symbol, ts, price, open, high, low, close, volume, source
                ) values (
                    :symbol, :asof, :price, :open, :high, :low, :close, :volume, :source
                )
                on conflict(symbol) do update set
                    ts = excluded.ts,
                    price = excluded.price,
                    open = excluded.open,
                    high = excluded.high,
                    low = excluded.low,
                    close = excluded.close,
                    volume = excluded.volume,
                    source = excluded.source
                """,
                rows,
            )
            conn.executemany(
                """
                insert into market_ticks (ts, symbol, price, source)
                values (:asof, :symbol, :price, :source)
                """,
                rows,
            )

    def insert_decisions(self, decisions: Iterable[Decision]) -> None:
        rows = [decision.to_dict() for decision in decisions]
        if not rows:
            return
        with self.connect() as conn:
            conn.executemany(
                """
                insert into decisions (
                    ts, symbol, action, strategy, confidence, price, technical_score,
                    sentiment_score, reason, details_json
                ) values (
                    :asof, :symbol, :action, :strategy, :confidence, :price,
                    :technical_score, :sentiment_score, :reason, :details_json
                )
                """,
                rows,
            )

    def insert_order(
        self,
        symbol: str,
        side: str,
        qty: int,
        price: float,
        status: str,
        reason: str,
        strategy: str = "unknown",
        details_json: str = "{}",
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                insert into orders (ts, symbol, side, strategy, qty, price, notional, status, reason, details_json)
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (utc_now(), symbol, side, strategy, qty, price, qty * price, status, reason, details_json),
            )

    def get_state(self, key: str, default: Any = None) -> Any:
        with self.connect() as conn:
            row = conn.execute("select value from agent_state where key = ?", (key,)).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except json.JSONDecodeError:
            return row["value"]

    def set_state(self, key: str, value: Any) -> None:
        encoded = json.dumps(value)
        with self.connect() as conn:
            conn.execute(
                """
                insert into agent_state (key, value) values (?, ?)
                on conflict(key) do update set value = excluded.value
                """,
                (key, encoded),
            )

    def runtime_settings(self) -> dict[str, Any]:
        with self.connect() as conn:
            rows = conn.execute("select key, value from runtime_settings").fetchall()
        settings: dict[str, Any] = {}
        for row in rows:
            try:
                settings[row["key"]] = json.loads(row["value"])
            except json.JSONDecodeError:
                settings[row["key"]] = row["value"]
        return settings

    def update_runtime_settings(self, values: dict[str, Any]) -> None:
        if not values:
            return
        rows = [(key, json.dumps(value), utc_now()) for key, value in values.items()]
        with self.connect() as conn:
            conn.executemany(
                """
                insert into runtime_settings (key, value, updated_at)
                values (?, ?, ?)
                on conflict(key) do update set
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                rows,
            )

    def insert_agent_log(
        self,
        level: str,
        component: str,
        event: str,
        message: str,
        details: Any | None = None,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                insert into agent_logs (ts, level, component, event, message, details_json)
                values (?, ?, ?, ?, ?, ?)
                """,
                (
                    utc_now(),
                    level.upper(),
                    component,
                    event,
                    message,
                    json.dumps(details or {}, default=str, separators=(",", ":")),
                ),
            )
            conn.execute(
                """
                delete from agent_logs
                where id not in (
                    select id from agent_logs order by id desc limit 2000
                )
                """
            )

    def latest_agent_logs(self, limit: int = 300) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                select * from agent_logs
                order by id desc
                limit ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def reset_trading_ledger(self, cash: float) -> None:
        with self.connect() as conn:
            conn.execute("delete from decisions")
            conn.execute("delete from orders")
            conn.execute("delete from positions")
            conn.execute("delete from portfolio_snapshots")
            conn.execute(
                """
                insert into agent_state (key, value) values ('cash', ?)
                on conflict(key) do update set value = excluded.value
                """,
                (json.dumps(cash),),
            )

    def latest_quotes(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                select q.*
                from latest_quotes q
                join universe u on u.symbol = q.symbol
                where u.enabled = 1
                order by q.symbol
                """
            ).fetchall()
            return [dict(row) for row in rows]

    def latest_decisions(self, limit: int = 80) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "select * from decisions order by id desc limit ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def latest_decision_summaries(self, limit: int = 80) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                select id, ts, symbol, action, strategy, confidence, price,
                    technical_score, sentiment_score, reason
                from decisions
                order by id desc
                limit ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def decision_by_id(self, decision_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "select * from decisions where id = ?",
                (decision_id,),
            ).fetchone()
        return dict(row) if row else None

    def latest_orders(self, limit: int = 80) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "select * from orders order by id desc limit ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def latest_order_summaries(self, limit: int = 80) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                select id, ts, symbol, side, strategy, qty, price, notional, status, reason
                from orders
                order by id desc
                limit ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def order_by_id(self, order_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "select * from orders where id = ?",
                (order_id,),
            ).fetchone()
        return dict(row) if row else None

    def positions(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "select * from positions where qty != 0 order by symbol"
            ).fetchall()
        return [dict(row) for row in rows]

    def latest_portfolio(self) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "select * from portfolio_snapshots order by id desc limit 1"
            ).fetchone()
        return dict(row) if row else None

    def recent_equity(self, limit: int = 120) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "select ts, equity from portfolio_snapshots order by id desc limit ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def strategy_metrics(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            open_rows = conn.execute(
                """
                select
                    strategy,
                    count(*) as open_positions,
                    sum(qty * market_price) as exposure,
                    sum((market_price - avg_price) * qty) as unrealized_pnl
                from positions
                where qty > 0
                group by strategy
                """
            ).fetchall()
            realized_rows = conn.execute(
                """
                select strategy, sum(realized_pnl) as realized_pnl
                from positions
                group by strategy
                """
            ).fetchall()
            order_rows = conn.execute(
                """
                select
                    strategy,
                    count(*) as filled_orders,
                    sum(case when side = 'BUY' then notional else 0 end) as buy_notional,
                    sum(case when side = 'SELL' then notional else 0 end) as sell_notional
                from orders
                where status = 'FILLED'
                group by strategy
                """
            ).fetchall()
        by_strategy: dict[str, dict[str, Any]] = {}
        for row in order_rows:
            strategy = row["strategy"] or "unknown"
            by_strategy[strategy] = {
                "strategy": strategy,
                "filled_orders": row["filled_orders"] or 0,
                "buy_notional": round(row["buy_notional"] or 0, 2),
                "sell_notional": round(row["sell_notional"] or 0, 2),
                "open_positions": 0,
                "exposure": 0.0,
                "unrealized_pnl": 0.0,
                "realized_pnl": 0.0,
            }
        for row in open_rows:
            strategy = row["strategy"] or "unknown"
            metrics = by_strategy.setdefault(
                strategy,
                {
                    "strategy": strategy,
                    "filled_orders": 0,
                    "buy_notional": 0.0,
                    "sell_notional": 0.0,
                    "open_positions": 0,
                    "exposure": 0.0,
                    "unrealized_pnl": 0.0,
                    "realized_pnl": 0.0,
                },
            )
            metrics["open_positions"] = row["open_positions"] or 0
            metrics["exposure"] = round(row["exposure"] or 0, 2)
            metrics["unrealized_pnl"] = round(row["unrealized_pnl"] or 0, 2)
        for row in realized_rows:
            strategy = row["strategy"] or "unknown"
            metrics = by_strategy.setdefault(
                strategy,
                {
                    "strategy": strategy,
                    "filled_orders": 0,
                    "buy_notional": 0.0,
                    "sell_notional": 0.0,
                    "open_positions": 0,
                    "exposure": 0.0,
                    "unrealized_pnl": 0.0,
                    "realized_pnl": 0.0,
                },
            )
            metrics["realized_pnl"] = round(row["realized_pnl"] or 0, 2)
        return sorted(by_strategy.values(), key=lambda item: abs(item["unrealized_pnl"]), reverse=True)

    def latest_sentiment(self, limit: int = 80) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                select * from sentiment_events
                order by id desc
                limit ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]
