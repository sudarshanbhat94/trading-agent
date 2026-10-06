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
    def orders(self, user_id: int) -> list: ...
    def positions(self, user_id: int) -> list: ...
    def holdings(self, user_id: int) -> list: ...
    def funds(self, user_id: int) -> dict: ...
    def trades(self, user_id: int) -> list: ...
    def cancel(self, user_id: int, order_id: str) -> dict: ...


class UpstoxPort:
    """Account-owned read/cancel port; writes still use the durable journal."""
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


def capability_report():
    return dict(routes=[dict(broker=r.broker,venue=r.venue,segment=r.segment,kind=r.kind,
                             product=r.product,order_type=r.order_type,validity=r.validity,
                             implementation=r.enabled,live_certified=False,
                             native_protection=r.native_protection) for r in ROUTES],
                unsupported=["BSE execution", "US execution adapter", "futures", "options",
                             "currency", "commodities", "multi-leg", "Angel One", "Zerodha", "Dhan"],
                note="Instrument discovery is broader than certified execution. Live release remains blocked.")
