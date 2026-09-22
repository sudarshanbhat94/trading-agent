import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from app import v2_live, v2_web, books


class AccountBoundaryTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=str(Path(self.tmp.name)/'paper.db')
        con=sqlite3.connect(self.path);v2_live.ensure_schema(con);v2_web._uwl(con);con.close()
        self.patch=patch.object(v2_web,'V2_DB',self.path);self.patch.start();self.addCleanup(self.patch.stop)
        self.user={'id':7,'account_plan':'auto'}

    def test_paper_buy_never_calls_broker_even_if_armed(self):
        quote={'TEST':{'price':100,'ts':datetime.now(timezone.utc).isoformat()}}
        with patch.object(v2_web,'_market_shut',return_value=None), \
             patch.object(v2_web,'_live_map',return_value=quote), \
             patch('app.broker.state',return_value={'live_ready':True}), \
             patch('app.live_trade.mirror_entry',side_effect=AssertionError('real order')):
            r=v2_web.api_buy({'symbol':'TEST','mode':'paper'},self.user)
        self.assertEqual(r.status_code,200)
        self.assertTrue(json.loads(r.body)['paper_recorded'])

    def test_failed_live_buy_never_creates_paper_holding(self):
        quote={'TEST':{'price':100,'ts':datetime.now(timezone.utc).isoformat()}}
        with patch.object(v2_web,'_market_shut',return_value=None), \
             patch.object(v2_web,'_live_map',return_value=quote), \
             patch.object(v2_web,'MAIN_DB',self.path), \
             patch('app.broker.state',return_value={'live_ready':False}):
            r=v2_web.api_buy({'symbol':'TEST','mode':'live'},self.user)
        self.assertEqual(r.status_code,409)
        with sqlite3.connect(self.path) as c:
            self.assertEqual(books.open_symbols(c,7,'IN'),set())

    def test_same_symbol_watchlist_and_alerts_are_private(self):
        other={'id':8}
        v2_web.api_watchlist_add({'symbol':'TEST','folder':'mine'},self.user)
        v2_web.api_watchlist_add({'symbol':'TEST','folder':'theirs'},other)
        v2_web.api_alerts_add({'symbol':'TEST','kind':'above','value':120},other)
        with patch.object(v2_web,'_daychg',return_value={}):
            got=json.loads(v2_web.api_watchlist(self.user).body)
        self.assertEqual(got['watch'][0]['folder'],'mine')
        self.assertEqual(got['alerts'],[])
        v2_web.api_watchlist_del('TEST','IN',self.user)
        with sqlite3.connect(self.path) as c:
            aid=c.execute('SELECT id FROM user_price_alerts WHERE user_id=8').fetchone()[0]
        v2_web.api_alerts_del(aid,self.user)
        with patch.object(v2_web,'_daychg',return_value={}):
            got=json.loads(v2_web.api_watchlist(other).body)
        self.assertEqual(len(got['watch']),1)
        self.assertEqual(len(got['alerts']),1)

    def test_ownerless_legacy_watchlist_is_not_assigned_to_another_user(self):
        with sqlite3.connect(self.path) as c:
            c.execute("INSERT INTO v2_watch_user(symbol,market) VALUES('OLD','IN')")
        with patch.object(v2_web,'_daychg',return_value={}):
            self.assertEqual(json.loads(v2_web.api_watchlist(self.user).body)['watch'],[])
