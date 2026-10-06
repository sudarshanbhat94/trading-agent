"""The same sourced instrument gate for house, personal and broker entries.

Missing evidence refuses new risk. Existing owned exits retain their separate
management path, so a daily catalogue outage cannot abandon an open position.
The context injects a catalogue connection, never a bypass or approval flag.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime,timezone
import json
import os
from pathlib import Path
import sqlite3
from decimal import Decimal,ROUND_CEILING

from . import execution_contracts
from .instrument_catalog import InstrumentError,resolve,_positive_decimal
from .execution_ports import route_for

CATALOGUE=ContextVar('entry_catalogue',default=None)


@contextmanager
def using(catalogue,now=None):
    token=CATALOGUE.set((catalogue,now))
    try:yield
    finally:CATALOGUE.reset(token)


@contextmanager
def open_catalogue():
    """One source for API discovery, approval, entry and protection checks."""
    injected=CATALOGUE.get()
    if injected:
        catalogue,now=injected
        yield catalogue,now or datetime.now(timezone.utc)
        return
    from .v2_live import MAIN_DB
    path=Path(os.environ.get('OPENSTOCKS_CATALOGUE_DB',MAIN_DB)).resolve()
    try:catalogue=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)
    except sqlite3.Error as exc:raise InstrumentError('Canonical catalogue unavailable') from exc
    try:yield catalogue,datetime.now(timezone.utc)
    finally:catalogue.close()


def check(market,symbol,quantity,price,stop,target,*,broker='paper',product='D',key=None,regime=None):
    if market!='IN':raise InstrumentError('UNSUPPORTED_CAPABILITY: this entry gate supports sourced NSE cash routes')
    if type(quantity) is not int or quantity<1:raise InstrumentError('Whole contract quantity required')
    if regime is None:
        from . import v2_live
        saved=v2_live.sleeve_view(market) or {}
        from zoneinfo import ZoneInfo
        today=datetime.now(ZoneInfo('Asia/Kolkata')).date()
        try:
            asof=datetime.fromisoformat(str(saved['asof'])[:10]).date()
            if asof>today or v2_live.trading_days_held(asof.isoformat(),today,market)>1:
                raise ValueError('Stale regime evidence')
            regime=saved.get('regime')
        except (ValueError,KeyError):regime=None
    if regime not in {'ON','NEUTRAL'}:raise InstrumentError('Current regime is OFF or unavailable; new equity risk refused')
    injected=CATALOGUE.get();owned=injected is None
    if injected:
        catalogue,now=injected;now=now or datetime.now(timezone.utc)
    else:
        from .v2_live import MAIN_DB
        path=Path(os.environ.get('OPENSTOCKS_CATALOGUE_DB',MAIN_DB)).resolve()
        try:catalogue=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)
        except sqlite3.Error as exc:raise InstrumentError('Canonical entry catalogue unavailable') from exc
        now=datetime.now(timezone.utc)
    try:
        spec,alias=resolve(catalogue,symbol=symbol,venue='NSE',segment='NSE_EQ',now=now)
        if key is not None and alias!=key:raise InstrumentError('Broker alias disagrees with canonical entry identity')
        spec,alias,evidence=execution_contracts.order_contract(catalogue,instrument_id=spec.id,
                                       quantity=quantity,price=price,now=now)
        route_for(broker,spec,product)
        spec.validate_order(quantity,stop,now=now)
        # Zero is the existing explicit trail/time-managed target sentinel.
        # It is not a zero-price exchange order.
        if isinstance(target,bool):raise InstrumentError('Target must be a price or explicit managed-exit sentinel')
        if target not in (None,0):spec.validate_order(quantity,target,now=now)
        return dict(instrument_id=spec.id,broker_key=alias,provider='upstox',contract_evidence=evidence,
                    product=product,quantity=quantity,regime=regime,decision_at=now.isoformat())
    except sqlite3.Error as exc:raise InstrumentError('Dated entry contract evidence unavailable') from exc
    finally:
        if owned:catalogue.close()


def normalise_long_levels(symbol,entry,stop,target):
    """Round automatic long triggers toward less risk, before persistence.

    Manual/frozen approvals retain strict validation. Never infer a tick from
    the symbol. A one-tick tighter stop is recorded in the decision evidence.
    """
    injected=CATALOGUE.get();owned=injected is None
    if injected:catalogue,now=injected;now=now or datetime.now(timezone.utc)
    else:
        from .v2_live import MAIN_DB
        path=Path(os.environ.get('OPENSTOCKS_CATALOGUE_DB',MAIN_DB)).resolve()
        try:catalogue=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)
        except sqlite3.Error as exc:raise InstrumentError('Canonical entry catalogue unavailable') from exc
        now=datetime.now(timezone.utc)
    try:
        spec,_=resolve(catalogue,symbol=symbol,venue='NSE',segment='NSE_EQ',now=now)
        _,rules=execution_contracts._latest(catalogue,'rules',spec.id,now)
        tick=_positive_decimal(rules['tick_size'],'tick');raw=_positive_decimal(stop,'stop')
        entry_value=_positive_decimal(entry,'entry')
        if not raw<entry_value:raise InstrumentError('Invalid automatic stop')
        rounded=(raw/tick).to_integral_value(rounding=ROUND_CEILING)*tick
        if rounded>=entry_value:raise InstrumentError('Tick-aligned stop reaches entry; no risk interval')
        if isinstance(target,bool):raise InstrumentError('Invalid automatic target')
        if target in (0,None):aligned_target=0.0
        else:
            value=_positive_decimal(target,'target')
            if value<=entry_value:raise InstrumentError('Invalid automatic target')
            aligned_target=float((value/tick).to_integral_value(rounding=ROUND_CEILING)*tick)
        return float(rounded),aligned_target
    except sqlite3.Error as exc:raise InstrumentError('Dated entry contract evidence unavailable') from exc
    finally:
        if owned:catalogue.close()


def protective_contract(symbol,quantity,price,product,key):
    """Use the identical canonical catalogue for after-hours native triggers."""
    injected=CATALOGUE.get();owned=injected is None
    if injected:catalogue,now=injected;now=now or datetime.now(timezone.utc)
    else:
        from .v2_live import MAIN_DB
        path=Path(os.environ.get('OPENSTOCKS_CATALOGUE_DB',MAIN_DB)).resolve()
        try:catalogue=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)
        except sqlite3.Error as exc:raise InstrumentError('Canonical protection catalogue unavailable') from exc
        now=datetime.now(timezone.utc)
    try:
        spec,alias=resolve(catalogue,symbol=symbol,venue='NSE',segment='NSE_EQ',now=now)
        if alias!=key:raise InstrumentError('Owned protection alias changed')
        spec,alias,evidence,_=execution_contracts.protection_contract(catalogue,instrument_id=spec.id,quantity=quantity,price=price,now=now)
        route_for('upstox',spec,product)
        return spec,alias,evidence
    except sqlite3.Error as exc:raise InstrumentError('Protection contract evidence unavailable') from exc
    finally:
        if owned:catalogue.close()


def ensure_schema(con):
    con.execute('''CREATE TABLE IF NOT EXISTS entry_contract_records(
      scope TEXT NOT NULL,user_id INTEGER NOT NULL,position_id INTEGER NOT NULL,
      payload TEXT NOT NULL,PRIMARY KEY(scope,user_id,position_id))''')
    for action in ('UPDATE','DELETE'):
        con.execute(f"CREATE TRIGGER IF NOT EXISTS immutable_entry_contract_{action.lower()} BEFORE {action} "
                    "ON entry_contract_records BEGIN SELECT RAISE(ABORT,'immutable entry contract'); END")


def record(con,scope,user_id,position_id,evidence):
    if not con.in_transaction:raise RuntimeError('Entry evidence must commit with its owned position')
    ensure_schema(con)
    con.execute('INSERT INTO entry_contract_records VALUES(?,?,?,?)',
                (scope,user_id,position_id,json.dumps(evidence,sort_keys=True,allow_nan=False)))
