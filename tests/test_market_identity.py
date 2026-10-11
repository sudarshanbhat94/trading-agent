import sqlite3
import tempfile
import unittest
from datetime import datetime,timezone
from pathlib import Path

from app.db import Database
from app.market_identity import reconcile
from app.models import Candle

NOW=datetime(2026,10,11,10,tzinfo=timezone.utc)
OLD='NSE_EQ|INE111A01011'
NEW='NSE_EQ|INE111A01029'


class MarketIdentityTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.db=Database(Path(self.tmp.name)/'market.db');self.db.init()
        self.db.upsert_universe_rows([dict(symbol='STOCK',upstox_instrument_key=OLD,sector='Original',enabled=1)])
        self.db.upsert_candles({'STOCK':[Candle('STOCK','2026-10-08T00:00:00+05:30',1000,1100,900,1000,200,'upstox-live:day')]})
        self.nse=[{'SYMBOL':'STOCK','SERIES':'EQ','ISIN NUMBER':NEW.split('|')[1]}]
        self.broker=[dict(trading_symbol='STOCK',isin=NEW.split('|')[1],instrument_key=NEW,segment='NSE_EQ',instrument_type='EQ')]

    def test_changed_key_archives_old_price_basis_and_preserves_metadata(self):
        result=reconcile(self.db,self.nse,self.broker,{'STOCK'},set(),NOW)
        self.assertEqual(result['updated'],['STOCK'])
        with self.db.connect() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM candles').fetchone()[0],0)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM market_identity_archive').fetchone()[0],1)
            self.assertEqual(tuple(c.execute('SELECT upstox_instrument_key,sector,enabled FROM universe').fetchone()),(NEW,'Original',1))
            with self.assertRaises(sqlite3.IntegrityError):c.execute('DELETE FROM market_identity_archive')
        self.assertEqual(reconcile(self.db,self.nse,self.broker,{'STOCK'},set(),NOW)['updated'],[])

    def test_held_or_pending_identity_is_never_relabelled(self):
        result=reconcile(self.db,self.nse,self.broker,{'STOCK'},{'STOCK'},NOW)
        self.assertIn('held_identity_change',result['issues'][0]['reason'])
        with self.db.connect() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM candles').fetchone()[0],1)
            self.assertEqual(c.execute('SELECT upstox_instrument_key FROM universe').fetchone()[0],OLD)

    def test_independent_identity_conflict_or_ambiguous_broker_refuses_change(self):
        for nse,broker in (([],self.broker),(self.nse,self.broker*2)):
            self.assertEqual(reconcile(self.db,nse,broker,{'STOCK'},set(),NOW)['updated'],[])

    def test_missing_eq_member_is_added_but_not_a_special_series(self):
        nse=[dict(self.nse[0],SYMBOL='NEWSTOCK')]
        broker=[dict(self.broker[0],trading_symbol='NEWSTOCK')]
        self.assertEqual(reconcile(self.db,nse,broker,{'NEWSTOCK'},set(),NOW)['added'],['NEWSTOCK'])

    def test_disabled_name_stays_disabled(self):
        with self.db.connect() as c:c.execute("UPDATE universe SET enabled=0 WHERE symbol='STOCK'")
        reconcile(self.db,self.nse,self.broker,{'STOCK'},set(),NOW)
        with self.db.connect() as c:self.assertEqual(c.execute('SELECT enabled FROM universe').fetchone()[0],0)

    def test_existing_us_symbol_is_quarantined_without_rolling_back_other_updates(self):
        self.db.upsert_universe_rows([dict(symbol='COLLISION',exchange='NASDAQ',enabled=1)])
        nse=self.nse+[dict(self.nse[0],SYMBOL='COLLISION')]
        broker=self.broker+[dict(self.broker[0],trading_symbol='COLLISION')]
        r=reconcile(self.db,nse,broker,{'STOCK','COLLISION'},set(),NOW)
        self.assertEqual(r['updated'],['STOCK'])
        self.assertIn(dict(symbol='COLLISION',reason='cross_market_symbol_collision'),r['issues'])
