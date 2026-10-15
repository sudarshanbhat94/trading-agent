import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from app import executable_quotes as q
from app.market_data import UpstoxMarketDataProvider


class ExecutableQuoteTest(unittest.TestCase):
    def setUp(self):
        self.now=datetime.now(timezone.utc)
        self.item=dict(instrument_token='NSE_EQ|TEST',symbol='TEST',timestamp=self.now.isoformat(),
            lower_circuit_limit=80,upper_circuit_limit=120,
            depth=dict(buy=[dict(price=99.95,quantity=500)],sell=[dict(price=100,quantity=500)]))

    def normalize(self, item=None):
        return q.normalize_upstox(item or self.item,'NSE_EQ|TEST','TEST',observed_at=self.now.isoformat())

    def test_real_snapshot_identity_timestamp_depth_and_circuits_are_required(self):
        self.assertEqual(q.top(self.normalize(),'BUY',key='NSE_EQ|TEST',symbol='TEST',now=self.now),(100,500))
        for change in ({'instrument_token':'NSE_EQ|OTHER'},{'symbol':'OTHER'},{'timestamp':None},
                       {'timestamp':(self.now+timedelta(seconds=1)).isoformat()},{'depth':{}},
                       {'lower_circuit_limit':True},{'upper_circuit_limit':90},{'upper_circuit_limit':'1e999'}):
            with self.subTest(change=change),self.assertRaises(ValueError): self.normalize(dict(self.item,**change))

    def test_crossed_unsorted_fractional_and_missing_side_are_not_executable(self):
        for bids,asks in [([dict(price=100,quantity=10)],[dict(price=100,quantity=10)]),
                          ([dict(price=99,quantity=1),dict(price=99.5,quantity=2)],[dict(price=100,quantity=10)]),
                          ([dict(price=99,quantity=1.5)],[dict(price=100,quantity=10)])]:
            with self.assertRaises(ValueError): self.normalize(dict(self.item,depth=dict(buy=bids,sell=asks)))
        empty=self.normalize(dict(self.item,depth=dict(buy=[],sell=[dict(price=0,quantity=0)])))
        with self.assertRaises(ValueError): q.top(empty,'BUY',key='NSE_EQ|TEST',symbol='TEST',now=self.now)

    def test_latest_snapshot_is_monotonic_and_same_symbol_conflicts_abstain(self):
        with sqlite3.connect(':memory:') as con:
            q.ensure_schema(con);snap=self.normalize();q.write(con,snap)
            old=self.normalize(dict(self.item,timestamp=(self.now-timedelta(seconds=1)).isoformat()))
            q.write(con,old);self.assertEqual(q.read(con,['TEST'])['TEST']['snapshot_id'],snap['snapshot_id'])
            other=q.normalize_upstox(dict(self.item,instrument_token='NSE_EQ|OTHER'),'NSE_EQ|OTHER','TEST',observed_at=self.now.isoformat())
            q.write(con,other)
            self.assertEqual(q.read(con,['TEST']),{})

    def test_symbol_label_cannot_substitute_for_known_broker_identity(self):
        provider=object.__new__(UpstoxMarketDataProvider);row=dict(symbol='TEST',upstox_instrument_key='NSE_EQ|TEST')
        self.assertIsNone(provider._find_quote_item({'NSE_EQ:TEST':dict(last_price=100)},row))
        self.assertIsNone(provider._find_quote_item({'NSE_EQ|TEST':dict(instrument_token='NSE_EQ|OTHER',last_price=100)},row))
        self.assertEqual(provider._find_quote_item({'NSE_EQ:TEST':self.item},row),self.item)
