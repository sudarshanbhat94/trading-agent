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


def _public_user(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if not row:
        return None
    broker_accounts = {
        "upstox": {
            "api_key_saved": bool(row.get("upstox_api_key")),
            "api_secret_saved": bool(row.get("upstox_api_secret")),
            "access_token_saved": bool(row.get("upstox_access_token")),
            "redirect_uri_saved": bool(row.get("upstox_redirect_uri")),
            "connected": bool(row.get("upstox_access_token")),
            "base_url": row.get("upstox_api_base_url") or "",
            "updated_at": row.get("broker_updated_at"),
        },
        "kite": {
            "api_key_saved": bool(row.get("kite_api_key")),
            "access_token_saved": bool(row.get("kite_access_token")),
            "connected": bool(row.get("kite_access_token")),
            "updated_at": row.get("broker_updated_at"),
        },
    }
    return {
        "id": int(row["id"]),
        "username": row["username"],
        "role": row.get("role") or "user",
        "active": bool(row.get("active")),
        "credit_balance": round(float(row.get("credit_balance") or 0.0), 6),
        "daily_credit_limit": round(float(row.get("daily_credit_limit") or 0.0), 6),
        "broker_accounts": broker_accounts,
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "last_login_at": row.get("last_login_at"),
    }


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    @staticmethod
    def _decode_json(value: Any) -> Any:
        try:
            return json.loads(value or "{}")
        except (TypeError, json.JSONDecodeError):
            return {}

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
                    industry text,
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

                create table if not exists users (
                    id integer primary key autoincrement,
                    username text not null unique,
                    password_hash text not null,
                    role text not null default 'user',
                    active integer not null default 1,
                    credit_balance real not null default 0,
                    daily_credit_limit real not null default 0,
                    upstox_api_key text not null default '',
                    upstox_api_secret text not null default '',
                    upstox_redirect_uri text not null default '',
                    upstox_access_token text not null default '',
                    upstox_api_base_url text not null default '',
                    kite_api_key text not null default '',
                    kite_access_token text not null default '',
                    broker_updated_at text,
                    created_at text not null,
                    updated_at text not null,
                    last_login_at text
                );

                create table if not exists user_credit_ledger (
                    id integer primary key autoincrement,
                    ts text not null,
                    user_id integer not null,
                    entry_type text not null,
                    amount real not null,
                    balance_after real not null,
                    base_cost real not null default 0,
                    platform_margin real not null default 0,
                    description text not null,
                    details_json text not null default '{}',
                    foreign key(user_id) references users(id)
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

                create table if not exists llm_usage_events (
                    id integer primary key autoincrement,
                    ts text not null,
                    component text not null,
                    purpose text not null,
                    provider text not null,
                    model text not null,
                    prompt_tokens integer not null default 0,
                    completion_tokens integer not null default 0,
                    total_tokens integer not null default 0,
                    cache_hit_tokens integer not null default 0,
                    cache_miss_tokens integer not null default 0,
                    estimated_tokens integer not null default 0,
                    input_chars integer not null default 0,
                    output_chars integer not null default 0,
                    cost_usd real not null default 0,
                    latency_ms integer not null default 0,
                    user_id integer,
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
                create index if not exists idx_users_username
                    on users(username);
                create index if not exists idx_llm_usage_ts
                    on llm_usage_events(ts);
                create index if not exists idx_llm_usage_purpose_ts
                    on llm_usage_events(purpose, ts);
                create index if not exists idx_user_credit_ledger_user_ts
                    on user_credit_ledger(user_id, ts);
                create index if not exists idx_delivery_symbol_date
                    on delivery_data(symbol, date);
                """
            )
            self._ensure_column(conn, "universe", "upstox_instrument_key", "text")
            self._ensure_column(conn, "universe", "nubra_symbol", "text")
            self._ensure_column(conn, "universe", "nubra_ref_id", "integer")
            self._ensure_column(conn, "universe", "industry", "text")
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
            self._ensure_column(conn, "llm_usage_events", "cache_hit_tokens", "integer not null default 0")
            self._ensure_column(conn, "llm_usage_events", "cache_miss_tokens", "integer not null default 0")
            self._ensure_column(conn, "llm_usage_events", "estimated_tokens", "integer not null default 0")
            self._ensure_column(conn, "llm_usage_events", "input_chars", "integer not null default 0")
            self._ensure_column(conn, "llm_usage_events", "output_chars", "integer not null default 0")
            self._ensure_column(conn, "llm_usage_events", "cost_usd", "real not null default 0")
            self._ensure_column(conn, "llm_usage_events", "latency_ms", "integer not null default 0")
            self._ensure_column(conn, "llm_usage_events", "user_id", "integer")
            conn.execute(
                """
                create index if not exists idx_llm_usage_user_ts
                    on llm_usage_events(user_id, ts)
                """
            )
            self._ensure_column(conn, "users", "role", "text not null default 'user'")
            self._ensure_column(conn, "users", "active", "integer not null default 1")
            self._ensure_column(conn, "users", "credit_balance", "real not null default 0")
            self._ensure_column(conn, "users", "daily_credit_limit", "real not null default 0")
            self._ensure_column(conn, "users", "upstox_api_key", "text not null default ''")
            self._ensure_column(conn, "users", "upstox_api_secret", "text not null default ''")
            self._ensure_column(conn, "users", "upstox_redirect_uri", "text not null default ''")
            self._ensure_column(conn, "users", "upstox_access_token", "text not null default ''")
            self._ensure_column(conn, "users", "upstox_api_base_url", "text not null default ''")
            self._ensure_column(conn, "users", "kite_api_key", "text not null default ''")
            self._ensure_column(conn, "users", "kite_access_token", "text not null default ''")
            self._ensure_column(conn, "users", "broker_updated_at", "text")
            self._ensure_column(conn, "users", "created_at", "text not null default ''")
            self._ensure_column(conn, "users", "updated_at", "text not null default ''")
            self._ensure_column(conn, "users", "last_login_at", "text")

    def _ensure_column(self, conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
        rows = conn.execute(f"pragma table_info({table})").fetchall()
        if column not in {row["name"] for row in rows}:
            conn.execute(f"alter table {table} add column {column} {definition}")

    def ensure_default_admin_user(self, username: str, password_hash: str | None) -> None:
        username = (username or "admin").strip()
        if not username or not password_hash:
            return
        now = utc_now()
        with self.connect() as conn:
            existing = conn.execute(
                "select id from users where lower(username) = lower(?)",
                (username,),
            ).fetchone()
            if existing:
                return
            count = conn.execute("select count(*) as count from users").fetchone()["count"]
            if count:
                return
            conn.execute(
                """
                insert into users (username, password_hash, role, active, created_at, updated_at)
                values (?, ?, 'admin', 1, ?, ?)
                """,
                (username, password_hash, now, now),
            )

    def has_active_users(self) -> bool:
        with self.connect() as conn:
            row = conn.execute("select count(*) as count from users where active = 1").fetchone()
        return bool(row and row["count"])

    def has_admin_user(self) -> bool:
        with self.connect() as conn:
            row = conn.execute("select count(*) as count from users where active = 1 and role = 'admin'").fetchone()
        return bool(row and row["count"])

    def active_admin_count(self) -> int:
        with self.connect() as conn:
            row = conn.execute("select count(*) as count from users where active = 1 and role = 'admin'").fetchone()
        return int(row["count"] or 0) if row else 0

    def user_by_username(self, username: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "select * from users where lower(username) = lower(?)",
                ((username or "").strip(),),
            ).fetchone()
        return dict(row) if row else None

    def user_by_id(self, user_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("select * from users where id = ?", (user_id,)).fetchone()
        return dict(row) if row else None

    def list_users(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                select *
                from users
                order by role = 'admin' desc, username collate nocase
                """
            ).fetchall()
        users = []
        for row in rows:
            public = _public_user(dict(row)) or {}
            public["credit_usage"] = self.user_credit_summary(int(row["id"]), include_ledger=False)
            users.append(public)
        return users

    def create_user(self, username: str, password_hash: str, role: str = "user", active: bool = True) -> dict[str, Any]:
        now = utc_now()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                insert into users (username, password_hash, role, active, created_at, updated_at)
                values (?, ?, ?, ?, ?, ?)
                """,
                (username.strip(), password_hash, role, 1 if active else 0, now, now),
            )
            user_id = int(cursor.lastrowid)
        user = self.user_by_id(user_id)
        return _public_user(user) if user else {}

    def update_user(
        self,
        user_id: int,
        *,
        role: str | None = None,
        active: bool | None = None,
        password_hash: str | None = None,
    ) -> dict[str, Any] | None:
        assignments: list[str] = []
        values: list[Any] = []
        if role is not None:
            assignments.append("role = ?")
            values.append(role)
        if active is not None:
            assignments.append("active = ?")
            values.append(1 if active else 0)
        if password_hash is not None:
            assignments.append("password_hash = ?")
            values.append(password_hash)
        if not assignments:
            user = self.user_by_id(user_id)
            return _public_user(user) if user else None
        assignments.append("updated_at = ?")
        values.append(utc_now())
        values.append(user_id)
        with self.connect() as conn:
            conn.execute(f"update users set {', '.join(assignments)} where id = ?", values)
        user = self.user_by_id(user_id)
        return _public_user(user) if user else None

    def mark_user_login(self, user_id: int) -> None:
        now = utc_now()
        with self.connect() as conn:
            conn.execute(
                "update users set last_login_at = ?, updated_at = ? where id = ?",
                (now, now, user_id),
            )

    def update_user_daily_credit_limit(self, user_id: int, daily_credit_limit: float) -> dict[str, Any]:
        limit = max(float(daily_credit_limit or 0.0), 0.0)
        with self.connect() as conn:
            conn.execute(
                "update users set daily_credit_limit = ?, updated_at = ? where id = ?",
                (limit, utc_now(), user_id),
            )
        return self.user_credit_summary(user_id)

    def adjust_user_credits(
        self,
        user_id: int,
        amount: float,
        description: str,
        details: Any | None = None,
        entry_type: str = "allocation",
    ) -> dict[str, Any]:
        delta = float(amount or 0.0)
        now = utc_now()
        with self.connect() as conn:
            row = conn.execute("select credit_balance from users where id = ?", (user_id,)).fetchone()
            if row is None:
                raise ValueError("user not found")
            balance = max(float(row["credit_balance"] or 0.0) + delta, 0.0)
            conn.execute(
                "update users set credit_balance = ?, updated_at = ? where id = ?",
                (balance, now, user_id),
            )
            conn.execute(
                """
                insert into user_credit_ledger
                    (ts, user_id, entry_type, amount, balance_after, base_cost, platform_margin, description, details_json)
                values (?, ?, ?, ?, ?, 0, 0, ?, ?)
                """,
                (now, user_id, entry_type, delta, balance, description, json.dumps(details or {}, default=str, separators=(",", ":"))),
            )
        return self.user_credit_summary(user_id)

    def charge_user_credits(
        self,
        user_id: int,
        base_cost: float,
        description: str,
        details: Any | None = None,
        *,
        margin_pct: float = 0.20,
        minimum_charge: float = 0.01,
    ) -> dict[str, Any]:
        base = max(float(base_cost or 0.0), 0.0)
        charge = max(base * (1.0 + max(float(margin_pct or 0.0), 0.0)), float(minimum_charge or 0.0))
        platform_margin = max(charge - base, 0.0)
        now = utc_now()
        with self.connect() as conn:
            row = conn.execute("select credit_balance from users where id = ?", (user_id,)).fetchone()
            if row is None:
                raise ValueError("user not found")
            balance = float(row["credit_balance"] or 0.0)
            if balance + 1e-9 < charge:
                raise ValueError("insufficient user credits")
            balance_after = balance - charge
            conn.execute(
                "update users set credit_balance = ?, updated_at = ? where id = ?",
                (balance_after, now, user_id),
            )
            conn.execute(
                """
                insert into user_credit_ledger
                    (ts, user_id, entry_type, amount, balance_after, base_cost, platform_margin, description, details_json)
                values (?, ?, 'usage', ?, ?, ?, ?, ?, ?)
                """,
                (
                    now,
                    user_id,
                    -charge,
                    balance_after,
                    base,
                    platform_margin,
                    description,
                    json.dumps(details or {}, default=str, separators=(",", ":")),
                ),
            )
        return self.user_credit_summary(user_id)

    def user_credit_summary(self, user_id: int, include_ledger: bool = True, ledger_limit: int = 80) -> dict[str, Any]:
        today = utc_now()[:10]
        with self.connect() as conn:
            user = conn.execute(
                "select id, username, credit_balance, daily_credit_limit from users where id = ?",
                (user_id,),
            ).fetchone()
            if user is None:
                return {}
            usage_today = conn.execute(
                """
                select
                    coalesce(sum(case when amount < 0 then -amount else 0 end), 0) as credits_used,
                    coalesce(sum(base_cost), 0) as base_cost,
                    coalesce(sum(platform_margin), 0) as platform_margin,
                    count(*) as entries
                from user_credit_ledger
                where user_id = ? and substr(ts, 1, 10) = ?
                """,
                (user_id, today),
            ).fetchone()
            usage_all = conn.execute(
                """
                select
                    coalesce(sum(case when amount < 0 then -amount else 0 end), 0) as credits_used,
                    coalesce(sum(base_cost), 0) as base_cost,
                    coalesce(sum(platform_margin), 0) as platform_margin,
                    count(*) as entries
                from user_credit_ledger
                where user_id = ?
                """,
                (user_id,),
            ).fetchone()
            ledger_rows = []
            if include_ledger:
                ledger_rows = conn.execute(
                    """
                    select id, ts, entry_type, amount, balance_after, description, details_json
                    from user_credit_ledger
                    where user_id = ?
                    order by id desc
                    limit ?
                    """,
                    (user_id, ledger_limit),
                ).fetchall()
        balance = float(user["credit_balance"] or 0.0)
        daily_limit = float(user["daily_credit_limit"] or 0.0)
        used_today = float(usage_today["credits_used"] or 0.0)
        daily_remaining = max(daily_limit - used_today, 0.0) if daily_limit > 0 else balance
        return {
            "user_id": int(user_id),
            "username": user["username"],
            "credit_balance": round(balance, 6),
            "daily_credit_limit": round(daily_limit, 6),
            "credits_used_today": round(used_today, 6),
            "daily_credits_remaining": round(min(balance, daily_remaining), 6),
            "today": {
                "credits_used": round(used_today, 6),
                "base_cost": round(float(usage_today["base_cost"] or 0.0), 8),
                "platform_margin": round(float(usage_today["platform_margin"] or 0.0), 8),
                "entries": int(usage_today["entries"] or 0),
            },
            "all_time": {
                "credits_used": round(float(usage_all["credits_used"] or 0.0), 6),
                "base_cost": round(float(usage_all["base_cost"] or 0.0), 8),
                "platform_margin": round(float(usage_all["platform_margin"] or 0.0), 8),
                "entries": int(usage_all["entries"] or 0),
            },
            "ledger": [
                {
                    "id": row["id"],
                    "ts": row["ts"],
                    "entry_type": row["entry_type"],
                    "amount": round(float(row["amount"] or 0.0), 6),
                    "balance_after": round(float(row["balance_after"] or 0.0), 6),
                    "description": row["description"],
                    "details": self._decode_json(row["details_json"]),
                }
                for row in ledger_rows
            ],
        }

    def admin_credit_usage_summary(self) -> dict[str, Any]:
        today = utc_now()[:10]
        with self.connect() as conn:
            rows = conn.execute(
                """
                select u.id, u.username, u.role, u.active,
                    u.credit_balance, u.daily_credit_limit,
                    coalesce(sum(case when l.amount < 0 and substr(l.ts, 1, 10) = ? then -l.amount else 0 end), 0) as today_credits,
                    coalesce(sum(case when substr(l.ts, 1, 10) = ? then l.base_cost else 0 end), 0) as today_base,
                    coalesce(sum(case when substr(l.ts, 1, 10) = ? then l.platform_margin else 0 end), 0) as today_margin,
                    coalesce(sum(case when l.amount < 0 then -l.amount else 0 end), 0) as all_credits,
                    coalesce(sum(l.base_cost), 0) as all_base,
                    coalesce(sum(l.platform_margin), 0) as all_margin
                from users u
                left join user_credit_ledger l on l.user_id = u.id
                group by u.id
                order by today_credits desc, all_credits desc, u.username collate nocase
                """,
                (today, today, today),
            ).fetchall()
        return {
            "updated_at": utc_now(),
            "today_utc": today,
            "margin_policy": "Admin view includes OpenTrade platform margin.",
            "users": [
                {
                    "id": row["id"],
                    "username": row["username"],
                    "role": row["role"],
                    "active": bool(row["active"]),
                    "credit_balance": round(float(row["credit_balance"] or 0.0), 6),
                    "daily_credit_limit": round(float(row["daily_credit_limit"] or 0.0), 6),
                    "today_credits_used": round(float(row["today_credits"] or 0.0), 6),
                    "today_base_cost": round(float(row["today_base"] or 0.0), 8),
                    "today_platform_margin": round(float(row["today_margin"] or 0.0), 8),
                    "all_time_credits_used": round(float(row["all_credits"] or 0.0), 6),
                    "all_time_base_cost": round(float(row["all_base"] or 0.0), 8),
                    "all_time_platform_margin": round(float(row["all_margin"] or 0.0), 8),
                }
                for row in rows
            ],
        }

    def user_has_credit_for(self, user_id: int, estimated_charge: float) -> tuple[bool, dict[str, Any]]:
        summary = self.user_credit_summary(user_id, include_ledger=False)
        charge = max(float(estimated_charge or 0.0), 0.0)
        ok = (
            summary.get("credit_balance", 0.0) + 1e-9 >= charge
            and summary.get("daily_credits_remaining", 0.0) + 1e-9 >= charge
        )
        return ok, summary

    def average_signal_credit_charge(self, fallback: float = 0.01) -> float:
        with self.connect() as conn:
            row = conn.execute(
                """
                select avg(charge) as avg_charge
                from (
                    select -amount as charge
                    from user_credit_ledger
                    where entry_type = 'usage' and amount < 0
                    order by id desc
                    limit 50
                )
                """
            ).fetchone()
        value = row["avg_charge"] if row else None
        try:
            return round(max(float(value or 0.0), fallback), 6)
        except (TypeError, ValueError):
            return fallback

    def latest_llm_usage_id(self) -> int:
        with self.connect() as conn:
            row = conn.execute("select coalesce(max(id), 0) as id from llm_usage_events").fetchone()
        return int(row["id"] or 0) if row else 0

    def llm_usage_cost_since(self, user_id: int, after_id: int) -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute(
                """
                select count(*) as calls,
                    coalesce(sum(cost_usd), 0) as cost_usd,
                    coalesce(sum(total_tokens), 0) as total_tokens,
                    coalesce(sum(input_chars), 0) as input_chars,
                    coalesce(sum(output_chars), 0) as output_chars
                from llm_usage_events
                where id > ? and user_id = ?
                """,
                (after_id, user_id),
            ).fetchone()
        return {
            "calls": int(row["calls"] or 0),
            "cost_usd": round(float(row["cost_usd"] or 0.0), 8),
            "total_tokens": int(row["total_tokens"] or 0),
            "input_chars": int(row["input_chars"] or 0),
            "output_chars": int(row["output_chars"] or 0),
        }

    def update_user_broker(self, user_id: int, values: dict[str, Any]) -> dict[str, Any] | None:
        allowed = {
            "upstox_api_key",
            "upstox_api_secret",
            "upstox_redirect_uri",
            "upstox_access_token",
            "upstox_api_base_url",
            "kite_api_key",
            "kite_access_token",
        }
        assignments: list[str] = []
        params: list[Any] = []
        for key in allowed:
            if key in values:
                assignments.append(f"{key} = ?")
                params.append(str(values.get(key) or "").strip())
        if not assignments:
            return _public_user(self.user_by_id(user_id))
        assignments.extend(["broker_updated_at = ?", "updated_at = ?"])
        now = utc_now()
        params.extend([now, now, user_id])
        with self.connect() as conn:
            conn.execute(f"update users set {', '.join(assignments)} where id = ?", params)
        return _public_user(self.user_by_id(user_id))

    def assign_runtime_upstox_to_user(self, user_id: int, runtime_settings: dict[str, Any]) -> dict[str, Any] | None:
        return self.update_user_broker(
            user_id,
            {
                "upstox_api_key": runtime_settings.get("upstox_api_key", ""),
                "upstox_api_secret": runtime_settings.get("upstox_api_secret", ""),
                "upstox_redirect_uri": runtime_settings.get("upstox_redirect_uri", ""),
                "upstox_access_token": runtime_settings.get("upstox_access_token", ""),
                "upstox_api_base_url": runtime_settings.get("upstox_api_base_url", ""),
            },
        )

    def seed_universe(self, csv_path: Path, disable_missing: bool = True) -> None:
        if not csv_path.exists():
            raise FileNotFoundError(f"Universe CSV not found: {csv_path}")
        with csv_path.open("r", newline="", encoding="utf-8") as handle:
            rows = [self._normalize_universe_row(row) for row in csv.DictReader(handle)]
        self.upsert_universe_rows(rows, disable_missing=disable_missing)

    def upsert_universe_rows(self, rows: Iterable[dict[str, Any]], disable_missing: bool = False) -> int:
        normalized = [self._normalize_universe_row(row) for row in rows]
        if not normalized:
            return 0
        symbols = [row["symbol"] for row in normalized]
        with self.connect() as conn:
            conn.executemany(
                """
                insert into universe (
                    symbol, name, exchange, yahoo_symbol, kite_symbol, upstox_instrument_key,
                    nubra_symbol, nubra_ref_id,
                    sector, industry, base_price, enabled
                ) values (
                    :symbol, :name, :exchange, :yahoo_symbol, :kite_symbol, :upstox_instrument_key,
                    :nubra_symbol, :nubra_ref_id,
                    :sector, :industry, :base_price, :enabled
                )
                on conflict(symbol) do update set
                    name = excluded.name,
                    exchange = excluded.exchange,
                    yahoo_symbol = excluded.yahoo_symbol,
                    kite_symbol = excluded.kite_symbol,
                    upstox_instrument_key = case
                        when excluded.upstox_instrument_key != '' then excluded.upstox_instrument_key
                        else universe.upstox_instrument_key
                    end,
                    nubra_symbol = excluded.nubra_symbol,
                    nubra_ref_id = excluded.nubra_ref_id,
                    sector = case when excluded.sector != '' then excluded.sector else universe.sector end,
                    industry = case when excluded.industry != '' then excluded.industry else universe.industry end,
                    base_price = case when excluded.base_price != 100 then excluded.base_price else universe.base_price end,
                    enabled = excluded.enabled
                """,
                normalized,
            )
            if disable_missing and symbols:
                placeholders = ",".join("?" for _ in symbols)
                conn.execute(f"update universe set enabled = 0 where symbol not in ({placeholders})", symbols)
        return len(normalized)

    def _normalize_universe_row(self, row: dict[str, Any]) -> dict[str, Any]:
        symbol = str(row.get("symbol", "")).strip().upper()
        exchange = str(row.get("exchange") or "NSE").strip().upper() or "NSE"
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
            "industry": row.get("industry") or "",
            "base_price": _optional_float(row.get("base_price")) or 100,
            "enabled": int(float(row.get("enabled"))) if row.get("enabled") not in (None, "") else 1,
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

    def candles_for_symbols(
        self,
        symbols: list[str],
        limit_per_symbol: int = 80,
        source: str | None = None,
        source_like: str | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        if not symbols:
            return {}
        output: dict[str, list[dict[str, Any]]] = {}
        with self.connect() as conn:
            for symbol in symbols:
                where = "where symbol = ?"
                params: list[Any] = [symbol]
                if source is not None:
                    where += " and source = ?"
                    params.append(source)
                elif source_like is not None:
                    where += " and source like ?"
                    params.append(source_like)
                params.append(limit_per_symbol)
                rows = conn.execute(
                    f"""
                    select * from candles
                    {where}
                    order by ts desc
                    limit ?
                    """,
                    params,
                ).fetchall()
                output[symbol] = [dict(row) for row in reversed(rows)]
        return output

    def recent_candles_by_symbol(
        self,
        symbols: list[str],
        limit_per_symbol: int = 96,
        source: str | None = None,
        source_like: str | None = None,
    ) -> dict[str, list[Candle]]:
        raw = self.candles_for_symbols(
            symbols,
            limit_per_symbol=limit_per_symbol,
            source=source,
            source_like=source_like,
        )
        return self._candle_rows_to_models(raw)

    def recent_candle_sets_by_symbol(self, symbols: list[str]) -> dict[str, dict[str, list[Candle]]]:
        if not symbols:
            return {}
        intraday = self.recent_candles_by_symbol(symbols, limit_per_symbol=120, source_like="upstox-live:%minute")
        legacy_intraday = self.recent_candles_by_symbol(symbols, limit_per_symbol=120, source="upstox-live")
        daily = self.recent_candles_by_symbol(symbols, limit_per_symbol=260, source="upstox-live:day")
        weekly = self.recent_candles_by_symbol(symbols, limit_per_symbol=160, source="upstox-live:week")
        output: dict[str, dict[str, list[Candle]]] = {}
        for symbol in symbols:
            intraday_candles = intraday.get(symbol) or legacy_intraday.get(symbol) or []
            daily_candles = daily.get(symbol) or self._resample_daily(intraday_candles)
            weekly_candles = weekly.get(symbol) or self._resample_weekly(daily_candles)
            analysis_candles = daily_candles or intraday_candles
            output[symbol] = {
                "intraday": intraday_candles,
                "daily": daily_candles,
                "weekly": weekly_candles,
                "analysis": analysis_candles,
            }
        return output

    def _candle_rows_to_models(self, raw: dict[str, list[dict[str, Any]]]) -> dict[str, list[Candle]]:
        output: dict[str, list[Candle]] = {}
        for symbol, rows in raw.items():
            candles: list[Candle] = []
            for row in rows:
                try:
                    candles.append(
                        Candle(
                            symbol=str(row["symbol"]),
                            ts=str(row["ts"]),
                            open=float(row["open"]),
                            high=float(row["high"]),
                            low=float(row["low"]),
                            close=float(row["close"]),
                            volume=float(row["volume"] or 0),
                            source=str(row["source"]),
                        )
                    )
                except (TypeError, ValueError, KeyError):
                    continue
            if candles:
                output[symbol] = candles
        return output

    def _resample_daily(self, candles: list[Candle]) -> list[Candle]:
        return self._resample_candles(candles, "day")

    def _resample_weekly(self, candles: list[Candle]) -> list[Candle]:
        return self._resample_candles(candles, "week")

    def _resample_candles(self, candles: list[Candle], timeframe: str) -> list[Candle]:
        grouped: dict[str, list[Candle]] = {}
        for candle in candles:
            key = self._candle_bucket(candle.ts, timeframe)
            if not key:
                continue
            grouped.setdefault(key, []).append(candle)
        output: list[Candle] = []
        for key in sorted(grouped):
            bucket = sorted(grouped[key], key=lambda item: item.ts)
            first = bucket[0]
            last = bucket[-1]
            output.append(
                Candle(
                    symbol=first.symbol,
                    ts=key,
                    open=first.open,
                    high=max(candle.high for candle in bucket),
                    low=min(candle.low for candle in bucket),
                    close=last.close,
                    volume=sum(float(candle.volume or 0) for candle in bucket),
                    source=f"{first.source}:resampled_{timeframe}",
                )
            )
        return output

    def _candle_bucket(self, ts: str, timeframe: str) -> str | None:
        value = str(ts or "")
        if not value:
            return None
        try:
            from datetime import datetime

            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if timeframe == "week":
                year, week, _ = parsed.isocalendar()
                return f"{year}-W{week:02d}"
            return parsed.date().isoformat()
        except ValueError:
            return value[:10] if timeframe == "day" else None

    def get_universe(self, enabled_only: bool = True) -> list[dict[str, Any]]:
        sql = "select * from universe"
        if enabled_only:
            sql += " where enabled = 1"
        sql += " order by symbol"
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(sql).fetchall()]

    def universe_summary(self) -> dict[str, Any]:
        with self.connect() as conn:
            total = conn.execute("select count(*) from universe").fetchone()[0]
            enabled = conn.execute("select count(*) from universe where enabled = 1").fetchone()[0]
            priced = conn.execute(
                """
                select count(*)
                from universe u
                join latest_quotes q on q.symbol = u.symbol
                where u.enabled = 1
                """
            ).fetchone()[0]
            priced_low = conn.execute(
                """
                select count(*)
                from universe u
                join latest_quotes q on q.symbol = u.symbol
                where u.enabled = 1 and q.price <= 100
                """
            ).fetchone()[0]
            sectors = conn.execute(
                """
                select coalesce(nullif(sector, ''), 'Unknown') as sector, count(*) as count
                from universe
                where enabled = 1
                group by coalesce(nullif(sector, ''), 'Unknown')
                order by count desc, sector
                limit 12
                """
            ).fetchall()
        return {
            "total": total,
            "enabled": enabled,
            "priced_symbols": priced,
            "low_price_enabled": priced_low,
            "top_sectors": [dict(row) for row in sectors],
        }

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

    def insert_llm_usage(self, event: dict[str, Any]) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                insert into llm_usage_events (
                    ts, component, purpose, provider, model, prompt_tokens,
                    completion_tokens, total_tokens, cache_hit_tokens, cache_miss_tokens,
                    estimated_tokens, input_chars, output_chars, cost_usd, latency_ms, user_id, details_json
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.get("ts") or utc_now(),
                    event.get("component") or "llm",
                    event.get("purpose") or "chat",
                    event.get("provider") or "unknown",
                    event.get("model") or "unknown",
                    int(event.get("prompt_tokens") or 0),
                    int(event.get("completion_tokens") or 0),
                    int(event.get("total_tokens") or 0),
                    int(event.get("cache_hit_tokens") or 0),
                    int(event.get("cache_miss_tokens") or 0),
                    1 if event.get("estimated_tokens") else 0,
                    int(event.get("input_chars") or 0),
                    int(event.get("output_chars") or 0),
                    float(event.get("cost_usd") or 0),
                    int(event.get("latency_ms") or 0),
                    _optional_int(event.get("user_id")),
                    json.dumps(event.get("details") or {}, default=str, separators=(",", ":")),
                ),
            )

    def llm_usage_summary(self, recent_limit: int = 25) -> dict[str, Any]:
        def aggregate(conn: sqlite3.Connection, where: str = "", params: tuple[Any, ...] = ()) -> dict[str, Any]:
            row = conn.execute(
                f"""
                select
                    count(*) as calls,
                    coalesce(sum(prompt_tokens), 0) as prompt_tokens,
                    coalesce(sum(completion_tokens), 0) as completion_tokens,
                    coalesce(sum(total_tokens), 0) as total_tokens,
                    coalesce(sum(cache_hit_tokens), 0) as cache_hit_tokens,
                    coalesce(sum(cache_miss_tokens), 0) as cache_miss_tokens,
                    coalesce(sum(estimated_tokens), 0) as estimated_calls,
                    coalesce(sum(input_chars), 0) as input_chars,
                    coalesce(sum(output_chars), 0) as output_chars,
                    coalesce(sum(cost_usd), 0) as cost_usd,
                    coalesce(avg(latency_ms), 0) as avg_latency_ms
                from llm_usage_events
                {where}
                """,
                params,
            ).fetchone()
            return {
                "calls": int(row["calls"] or 0),
                "prompt_tokens": int(row["prompt_tokens"] or 0),
                "completion_tokens": int(row["completion_tokens"] or 0),
                "total_tokens": int(row["total_tokens"] or 0),
                "cache_hit_tokens": int(row["cache_hit_tokens"] or 0),
                "cache_miss_tokens": int(row["cache_miss_tokens"] or 0),
                "estimated_calls": int(row["estimated_calls"] or 0),
                "input_chars": int(row["input_chars"] or 0),
                "output_chars": int(row["output_chars"] or 0),
                "cost_usd": round(float(row["cost_usd"] or 0), 8),
                "avg_latency_ms": round(float(row["avg_latency_ms"] or 0), 1),
            }

        today = utc_now()[:10]
        with self.connect() as conn:
            today_summary = aggregate(conn, "where substr(ts, 1, 10) = ?", (today,))
            all_time_summary = aggregate(conn)
            recent_rows = conn.execute(
                """
                select id, ts, component, purpose, provider, model, prompt_tokens,
                    completion_tokens, total_tokens, cache_hit_tokens, cache_miss_tokens,
                    estimated_tokens, input_chars, output_chars, cost_usd, latency_ms, user_id, details_json
                from llm_usage_events
                order by id desc
                limit ?
                """,
                (recent_limit,),
            ).fetchall()
            by_purpose_rows = conn.execute(
                """
                select purpose, count(*) as calls, coalesce(sum(total_tokens), 0) as total_tokens,
                    coalesce(sum(cost_usd), 0) as cost_usd
                from llm_usage_events
                where substr(ts, 1, 10) = ?
                group by purpose
                order by cost_usd desc
                """,
                (today,),
            ).fetchall()
            by_model_rows = conn.execute(
                """
                select model, count(*) as calls, coalesce(sum(total_tokens), 0) as total_tokens,
                    coalesce(sum(cost_usd), 0) as cost_usd
                from llm_usage_events
                where substr(ts, 1, 10) = ?
                group by model
                order by cost_usd desc
                """,
                (today,),
            ).fetchall()

        return {
            "updated_at": utc_now(),
            "token_rule": "fallback estimate uses tokens = english_characters * 0.3",
            "currency": "USD",
            "today_utc": today_summary,
            "all_time": all_time_summary,
            "by_purpose_today": [
                {
                    "purpose": row["purpose"],
                    "calls": int(row["calls"] or 0),
                    "total_tokens": int(row["total_tokens"] or 0),
                    "cost_usd": round(float(row["cost_usd"] or 0), 8),
                }
                for row in by_purpose_rows
            ],
            "by_model_today": [
                {
                    "model": row["model"],
                    "calls": int(row["calls"] or 0),
                    "total_tokens": int(row["total_tokens"] or 0),
                    "cost_usd": round(float(row["cost_usd"] or 0), 8),
                }
                for row in by_model_rows
            ],
            "recent": [
                {
                    "id": row["id"],
                    "ts": row["ts"],
                    "component": row["component"],
                    "purpose": row["purpose"],
                    "provider": row["provider"],
                    "model": row["model"],
                    "prompt_tokens": row["prompt_tokens"],
                    "completion_tokens": row["completion_tokens"],
                    "total_tokens": row["total_tokens"],
                    "cache_hit_tokens": row["cache_hit_tokens"],
                    "cache_miss_tokens": row["cache_miss_tokens"],
                    "estimated_tokens": bool(row["estimated_tokens"]),
                    "input_chars": row["input_chars"],
                    "output_chars": row["output_chars"],
                    "cost_usd": round(float(row["cost_usd"] or 0), 8),
                    "latency_ms": row["latency_ms"],
                    "user_id": row["user_id"],
                    "details": self._decode_json(row["details_json"]),
                }
                for row in recent_rows
            ],
        }

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

    def performance_summary(self) -> dict[str, Any]:
        with self.connect() as conn:
            orders = conn.execute(
                """
                select
                    count(*) as total_orders,
                    sum(case when status = 'FILLED' then 1 else 0 end) as filled_orders,
                    sum(case when status = 'VETOED' then 1 else 0 end) as vetoed_orders,
                    sum(case when status = 'FILLED' and side = 'BUY' then 1 else 0 end) as buy_fills,
                    sum(case when status = 'FILLED' and side = 'SELL' then 1 else 0 end) as sell_fills,
                    sum(case when status = 'FILLED' then notional else 0 end) as filled_notional
                from orders
                """
            ).fetchone()
            positions = conn.execute(
                """
                select
                    count(*) as tracked_positions,
                    sum(case when qty > 0 then 1 else 0 end) as open_positions,
                    sum(realized_pnl) as realized_pnl,
                    sum((market_price - avg_price) * qty) as unrealized_pnl,
                    sum(case when qty = 0 and realized_pnl > 0 then 1 else 0 end) as closed_winners,
                    sum(case when qty = 0 and realized_pnl < 0 then 1 else 0 end) as closed_losers
                from positions
                """
            ).fetchone()
            equity_rows = conn.execute(
                "select equity from portfolio_snapshots order by id asc"
            ).fetchall()
        order_data = dict(orders) if orders else {}
        position_data = dict(positions) if positions else {}
        equity_values = [float(row["equity"]) for row in equity_rows if row["equity"] is not None]
        max_drawdown = 0.0
        peak = equity_values[0] if equity_values else 0.0
        for equity in equity_values:
            peak = max(peak, equity)
            if peak > 0:
                max_drawdown = min(max_drawdown, (equity - peak) / peak)
        winners = int(position_data.get("closed_winners") or 0)
        losers = int(position_data.get("closed_losers") or 0)
        closed = winners + losers
        realized = float(position_data.get("realized_pnl") or 0.0)
        return {
            "orders": {
                "total": int(order_data.get("total_orders") or 0),
                "filled": int(order_data.get("filled_orders") or 0),
                "vetoed": int(order_data.get("vetoed_orders") or 0),
                "buy_fills": int(order_data.get("buy_fills") or 0),
                "sell_fills": int(order_data.get("sell_fills") or 0),
                "filled_notional": round(float(order_data.get("filled_notional") or 0.0), 2),
            },
            "positions": {
                "tracked": int(position_data.get("tracked_positions") or 0),
                "open": int(position_data.get("open_positions") or 0),
                "closed": closed,
                "closed_winners": winners,
                "closed_losers": losers,
            },
            "pnl": {
                "realized": round(realized, 2),
                "unrealized": round(float(position_data.get("unrealized_pnl") or 0.0), 2),
                "win_rate": round(winners / closed, 4) if closed else 0.0,
                "expectancy_per_closed_trade": round(realized / closed, 2) if closed else 0.0,
                "max_drawdown_pct": round(max_drawdown * 100, 4),
            },
        }

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
