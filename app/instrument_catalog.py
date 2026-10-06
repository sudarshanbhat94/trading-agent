"""Dated contract identity. Discovery is not execution permission.

Broker tokens are aliases, not identities: venue/series/expiry remain part of
the contract even when two instruments share a display symbol.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal,InvalidOperation

from .account_safety import atomic


class InstrumentError(ValueError):
    pass


def _positive_decimal(value,label):
    try:
        if isinstance(value,bool):raise InstrumentError('invalid '+label)
        number=Decimal(str(value))
        if not number.is_finite() or number<=0:raise InstrumentError('invalid '+label)
        return number
    except (InvalidOperation,TypeError,ValueError) as exc:
        raise InstrumentError('invalid '+label) from exc


@dataclass(frozen=True)
class Instrument:
    venue: str
    segment: str
    kind: str
    symbol: str
    currency: str
    security_id: str
    series: str = "UNKNOWN"
    expiry: str | None = None
    strike: str | None = None
    right: str | None = None
    underlying: str | None = None
    lot_size: int = 1
    tick_size: str | None = None
    freeze_quantity: int | None = None
    settlement: str = "UNKNOWN"
    tradable: bool = True

    def __post_init__(self):
        if self.kind not in {"EQUITY", "ETF", "INDEX", "FUTURE", "OPTION", "CURRENCY", "COMMODITY"}:
            raise InstrumentError("unknown asset class")
        if not all(isinstance(x, str) and x.strip() for x in (
                self.venue, self.segment, self.symbol, self.currency, self.security_id, self.series)):
            raise InstrumentError("incomplete instrument identity")
        if isinstance(self.lot_size, bool) or not isinstance(self.lot_size, int) or self.lot_size < 1:
            raise InstrumentError("invalid lot size")
        if self.tick_size is not None:_positive_decimal(self.tick_size,'tick size')
        if self.freeze_quantity is not None and (type(self.freeze_quantity) is not int or self.freeze_quantity < self.lot_size):
            raise InstrumentError("invalid freeze quantity")
        if self.kind == "INDEX" and self.tradable:
            raise InstrumentError("an index level is not an orderable instrument")
        if type(self.tradable) is not bool:raise InstrumentError('invalid tradability flag')
        if self.kind in {"FUTURE", "OPTION", "CURRENCY", "COMMODITY"}:
            if not self.expiry or not self.underlying:
                raise InstrumentError("derivative identity needs expiry and underlying")
            try:date.fromisoformat(self.expiry)
            except (ValueError,TypeError) as exc:raise InstrumentError('invalid expiry') from exc
        if (self.kind == "OPTION" or self.right is not None) and (self.right not in {"CE", "PE"} or self.strike is None or
                                      not _positive_decimal(self.strike,'strike')):
            raise InstrumentError("option identity needs a positive strike and CE/PE")

    @property
    def id(self):
        # Lot/tick/symbol can change without changing security ownership.
        fields = {key: getattr(self, key) for key in (
            "venue", "segment", "kind", "security_id", "series", "expiry", "right", "underlying")}
        fields["strike"] = str(Decimal(self.strike).normalize()) if self.strike else None
        return "ins_" + hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest()[:32]

    def validate_order(self, quantity, price, *, now=None, expiry_cutoff=None):
        now = now or datetime.now(timezone.utc)
        if not self.tradable:
            raise InstrumentError("instrument is observation-only")
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 1 or quantity % self.lot_size:
            raise InstrumentError("quantity must be a positive whole number of lots")
        if self.tick_size is None or self.freeze_quantity is None or self.series == "UNKNOWN":
            raise InstrumentError("order rules are incomplete; catalogue discovery is not permission")
        if quantity > self.freeze_quantity:
            raise InstrumentError("quantity exceeds freeze limit; use approved sliced intents")
        price = _positive_decimal(price,'price')
        if not price.is_finite() or price <= 0 or price % Decimal(self.tick_size):
            raise InstrumentError("price must be positive and aligned to the contract tick")
        if self.expiry:
            if self.settlement == "UNKNOWN" or expiry_cutoff is None:
                raise InstrumentError("settlement/expiry cut-off unavailable")
            if now.tzinfo is None or expiry_cutoff.tzinfo is None:
                raise InstrumentError('timezone-aware settlement cut-off required')
            if now >= expiry_cutoff or now.date() > date.fromisoformat(self.expiry):
                raise InstrumentError("contract is past its permitted exit cut-off")


def ensure_schema(con):
    con.execute("""CREATE TABLE IF NOT EXISTS instrument_snapshots(
      id TEXT PRIMARY KEY,provider TEXT NOT NULL,source TEXT NOT NULL,
      source_day TEXT NOT NULL,observed_at TEXT NOT NULL,count INTEGER NOT NULL)""")
    con.execute("""CREATE TABLE IF NOT EXISTS instrument_contracts(
      snapshot_id TEXT NOT NULL,instrument_id TEXT NOT NULL,broker_key TEXT NOT NULL,
      symbol TEXT NOT NULL,venue TEXT NOT NULL,segment TEXT NOT NULL,payload TEXT NOT NULL,
      PRIMARY KEY(snapshot_id,instrument_id),UNIQUE(snapshot_id,broker_key))""")
    con.execute("CREATE INDEX IF NOT EXISTS ix_instrument_lookup ON instrument_contracts(symbol,venue,segment)")


def import_snapshot(con, instruments, *, provider, source, source_day, observed_at, now=None):
    now = now or datetime.now(timezone.utc)
    observed = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
    if observed.tzinfo is None or observed > now or date.fromisoformat(source_day) > observed.date():
        raise InstrumentError("source observation is naive or in the future")
    rows = [(spec, str(key)) for spec, key in instruments]
    if not rows or not provider or not source or any(not key for _, key in rows):
        raise InstrumentError("empty or unattributed catalogue")
    if len({s.id for s, _ in rows}) != len(rows) or len({key for _, key in rows}) != len(rows):
        raise InstrumentError("duplicate instrument identity or provider alias")
    content = sorted((s.id, key, asdict(s)) for s, key in rows)
    sid = hashlib.sha256(json.dumps([provider, source_day, observed_at, content], sort_keys=True).encode()).hexdigest()
    ensure_schema(con)
    with atomic(con):
        con.execute("INSERT OR IGNORE INTO instrument_snapshots VALUES(?,?,?,?,?,?)",
                    (sid, provider, source, source_day, observed_at, len(rows)))
        con.executemany("INSERT OR IGNORE INTO instrument_contracts VALUES(?,?,?,?,?,?,?)",
                        [(sid, s.id, key, s.symbol, s.venue, s.segment, json.dumps(asdict(s), sort_keys=True))
                         for s, key in rows])
    return sid


def resolve(con, *, instrument_id=None, symbol=None, venue=None, segment=None, provider="upstox", now=None):
    now = now or datetime.now(timezone.utc)
    snapshot = con.execute("SELECT id,observed_at,source_day FROM instrument_snapshots WHERE provider=? "
                           "AND julianday(observed_at)<=julianday(?) ORDER BY julianday(observed_at) DESC LIMIT 1",
                           (provider, now.isoformat())).fetchone()
    if not snapshot or now - datetime.fromisoformat(snapshot[1].replace("Z", "+00:00")) > timedelta(hours=25) or \
            (now.date() - date.fromisoformat(snapshot[2])).days > 1:
        raise InstrumentError("dated instrument catalogue is missing or stale")
    sql, args = "SELECT payload,broker_key FROM instrument_contracts WHERE snapshot_id=?", [snapshot[0]]
    for field, value in (("instrument_id", instrument_id), ("symbol", symbol), ("venue", venue), ("segment", segment)):
        if value is not None:
            sql += " AND " + field + "=?"
            args.append(value)
    rows = con.execute(sql, args).fetchall()
    if len(rows) != 1:
        raise InstrumentError("instrument is unknown or ambiguous; use venue/segment/instrument_id")
    return Instrument(**json.loads(rows[0][0])), rows[0][1]


def upstox_contract(row):
    """Preserve unknown metadata; do not invent series or derivative units."""
    segment, raw_type = str(row["segment"]), str(row["instrument_type"])
    kind = "INDEX" if segment.endswith("INDEX") else (
        "OPTION" if raw_type in {"CE", "PE", "OPTIDX", "OPTSTK"} else
        "FUTURE" if raw_type in {"FUT", "FUTIDX", "FUTSTK"} else
        "ETF" if raw_type == "ETF" else "EQUITY" if segment.endswith("EQ") else None)
    if segment in {"NCD_FO", "BCD_FO"}:
        kind = "CURRENCY"
    elif segment == "MCX_FO":
        kind = "COMMODITY"
    if kind is None:
        raise InstrumentError("unclassified provider contract")
    expiry = row.get("expiry")
    if expiry is not None and isinstance(expiry, (float, int)):
        expiry = datetime.fromtimestamp(expiry / 1000, timezone(timedelta(hours=5,minutes=30))).date().isoformat()
    key = str(row["instrument_key"])
    # JSON tick_size is provider-scaled. Require a sourced rupee conversion;
    # the sync importer stores the raw value in its evidence, not a guessed tick.
    tick = row.get("normalized_tick_size")
    lot = row.get("lot_size")
    if isinstance(lot,bool) or not isinstance(lot,(int,float)) or not math.isfinite(lot) or int(lot)!=lot:
        raise InstrumentError("provider lot size is invalid")
    freeze=row.get('freeze_quantity')
    if freeze is not None and (isinstance(freeze,bool) or not isinstance(freeze,(int,float)) or \
                               not math.isfinite(freeze) or int(freeze)!=freeze or freeze<1):
        raise InstrumentError('provider freeze quantity is invalid')
    spec = Instrument(str(row["exchange"]), segment, kind, str(row["trading_symbol"]),
                      "INR", str(row.get("isin") or row.get("underlying_key") or key),
                      str(row.get("series") or "UNKNOWN"), str(expiry) if expiry else None,
                      str(row["strike_price"]) if row.get("strike_price") is not None else None,
                      raw_type if raw_type in {"CE", "PE"} else row.get("option_type"),
                      row.get("underlying_key"), int(lot),
                      str(tick) if tick else None,
                      int(freeze) if freeze is not None else None,
                      str(row.get("settlement") or "UNKNOWN"), kind != "INDEX")
    return spec, key
