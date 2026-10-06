"""Explicit broker capabilities; discovery must never imply order support."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .instrument_catalog import Instrument, InstrumentError


@dataclass(frozen=True)
class Route:
    broker: str
    venue: str
    segment: str
    kind: str
    product: str
    order_type: str
    validity: str
    enabled: bool
    native_protection: bool = False


ROUTES = tuple(Route(broker, "NSE", "NSE_EQ", kind, product, "MARKET", "DAY", True)
               for broker in ("paper", "upstox") for kind in ("EQUITY", "ETF") for product in ("D", "I"))


def route_for(broker, spec: Instrument, product="D", order_type="MARKET", validity="DAY"):
    for route in ROUTES:
        if (broker, spec.venue, spec.segment, spec.kind, product, order_type, validity) == (
                route.broker, route.venue, route.segment, route.kind, route.product, route.order_type, route.validity):
            if spec.tradable and route.enabled:
                return route
    raise InstrumentError("UNSUPPORTED_CAPABILITY: segment/product/order type is not implemented/certified")


class BrokerPort(Protocol):
    def submit(self, user_id: int, instrument_key: str, quantity: int, side: str,
               *, product: str, tag: str) -> dict: ...
    def order_status(self, user_id: int, order_id: str) -> dict | None: ...
    def orders(self, user_id: int) -> list: ...
    def positions(self, user_id: int) -> list: ...
    def holdings(self, user_id: int) -> list: ...
    def funds(self, user_id: int) -> dict: ...
    def trades(self, user_id: int) -> list: ...
    def cancel(self, user_id: int, order_id: str) -> dict: ...
    def modify(self, user_id: int, order_id: str, *, quantity: int) -> dict: ...
    def margin(self, user_id: int, instruments: list[dict]) -> dict: ...
    def place_stop(self, user_id: int, instrument_key: str, quantity: int,
                   *, product: str, stop: float) -> dict: ...
    def protection_status(self, user_id: int, protection_id: str) -> list: ...
    def cancel_protection(self, user_id: int, protection_id: str) -> dict: ...


class UpstoxPort:
    """Account-owned read/cancel port; writes still use the durable journal."""
    def submit(self, user_id, instrument_key, quantity, side, *, product, tag):
        # Only the implemented cash-equity/ETF MARKET/DAY route can cross
        # this transport boundary. Catalogue discovery never enables F&O.
        if not isinstance(instrument_key,str) or not instrument_key.startswith('NSE_EQ|') or not instrument_key.split('|',1)[1] or \
                isinstance(quantity,bool) or not isinstance(quantity,int) or quantity<1 or \
                side not in {'BUY','SELL'} or product not in {'D','I'}:
            raise InstrumentError('UNSUPPORTED_CAPABILITY: invalid or unsupported order route')
        from . import broker
        return broker.place_order(user_id,instrument_key,quantity,side,price=0.0,product=product,tag=tag)

    def order_status(self,user_id,order_id):
        rows=[r for r in self.orders(user_id) if str(r.get('order_id'))==str(order_id)]
        if len(rows)>1:
            raise InstrumentError('ambiguous broker order evidence')
        return rows[0] if rows else None  # Absence is unknown, never rejection.

    def orders(self, user_id):
        from . import broker
        return broker.orders(user_id)

    def positions(self, user_id):
        from . import broker
        return broker.positions(user_id)

    def holdings(self, user_id):
        from . import broker
        return broker.holdings(user_id)

    def funds(self, user_id):
        from . import broker
        return broker.funds(user_id)

    def trades(self, user_id):
        from . import broker
        return broker.trades(user_id)

    def cancel(self, user_id, order_id):
        from . import broker
        return broker.cancel_order(user_id, order_id)

    def modify(self,user_id,order_id,*,quantity):
        from . import broker
        return broker.modify_order(user_id,order_id,quantity=quantity)

    def margin(self,user_id,instruments):
        from . import broker
        return broker.margin_required(user_id,instruments)

    def place_stop(self,user_id,instrument_key,quantity,*,product,stop):
        from . import broker
        return broker.place_stop(user_id,instrument_key,quantity,product=product,stop=stop)

    def protection_status(self,user_id,protection_id):
        from . import broker
        return broker.protection_status(user_id,protection_id)

    def cancel_protection(self,user_id,protection_id):
        from . import broker
        return broker.cancel_protection(user_id,protection_id)


def capability_report():
    from .angelone_port import capabilities as angel_capabilities
    return dict(routes=[dict(broker=r.broker,venue=r.venue,segment=r.segment,kind=r.kind,
                             product=r.product,order_type=r.order_type,validity=r.validity,
                             implementation=r.enabled,live_certified=False,
                             native_protection=r.native_protection) for r in ROUTES],
                additional_adapters=[angel_capabilities()],
                unsupported=["BSE execution", "US execution adapter", "futures", "options",
                             "currency", "commodities", "multi-leg", "Angel One", "Zerodha", "Dhan"],
                note="Instrument discovery is broader than certified execution. Live release remains blocked.")
