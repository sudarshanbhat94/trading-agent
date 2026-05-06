from __future__ import annotations

import csv
import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable

from .models import Candle, Decision, Quote, utc_now


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
                    updated_at text not null
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

                create index if not exists idx_market_ticks_symbol_ts
                    on market_ticks(symbol, ts);
                create index if not exists idx_candles_symbol_ts
                    on candles(symbol, ts);
                create index if not exists idx_decisions_ts
                    on decisions(ts);
                create index if not exists idx_orders_ts
                    on orders(ts);
                """
            )
            self._ensure_column(conn, "universe", "upstox_instrument_key", "text")
            self._ensure_column(conn, "decisions", "strategy", "text not null default 'unknown'")
            self._ensure_column(conn, "decisions", "details_json", "text not null default '{}'")
            self._ensure_column(conn, "orders", "strategy", "text not null default 'unknown'")
            self._ensure_column(conn, "orders", "details_json", "text not null default '{}'")
            self._ensure_column(conn, "positions", "strategy", "text not null default 'unknown'")
            self._ensure_column(conn, "sentiment_events", "confidence", "real not null default 0")
            self._ensure_column(conn, "sentiment_events", "events_json", "text not null default '[]'")

    def _ensure_column(self, conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
        rows = conn.execute(f"pragma table_info({table})").fetchall()
        if column not in {row["name"] for row in rows}:
            conn.execute(f"alter table {table} add column {column} {definition}")

    def seed_universe(self, csv_path: Path) -> None:
        if not csv_path.exists():
            raise FileNotFoundError(f"Universe CSV not found: {csv_path}")
        with csv_path.open("r", newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        symbols = [row["symbol"] for row in rows]
        with self.connect() as conn:
            conn.executemany(
                """
                insert into universe (
                    symbol, name, exchange, yahoo_symbol, kite_symbol, upstox_instrument_key,
                    sector, base_price, enabled
                ) values (
                    :symbol, :name, :exchange, :yahoo_symbol, :kite_symbol, :upstox_instrument_key,
                    :sector, :base_price, :enabled
                )
                on conflict(symbol) do update set
                    name = excluded.name,
                    exchange = excluded.exchange,
                    yahoo_symbol = excluded.yahoo_symbol,
                    kite_symbol = excluded.kite_symbol,
                    upstox_instrument_key = excluded.upstox_instrument_key,
                    sector = excluded.sector,
                    base_price = excluded.base_price,
                    enabled = excluded.enabled
                """,
                rows,
            )
            if symbols:
                placeholders = ",".join("?" for _ in symbols)
                conn.execute(f"update universe set enabled = 0 where symbol not in ({placeholders})", symbols)

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

    def latest_orders(self, limit: int = 80) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "select * from orders order by id desc limit ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

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
