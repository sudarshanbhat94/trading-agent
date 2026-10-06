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
        from tests.contract_source_fixtures import catalogue
        from app import entry_contracts
        source=catalogue();self.addCleanup(source.close)
        context=entry_contracts.using(source);context.__enter__();self.addCleanup(context.__exit__,None,None,None)
        regime=patch.object(v2_web,'_regime_state',return_value='ON');regime.start();self.addCleanup(regime.stop)

    def test_paper_buy_never_calls_broker_even_if_armed(self):
        quote={'TEST':{'price':100,'ts':datetime.now(timezone.utc).isoformat()}}
        with patch.object(v2_web,'_market_shut',return_value=None), \
             patch.object(v2_web,'_live_map',return_value=quote), \
             patch('app.broker.state',return_value={'live_ready':True}), \
             patch('app.live_trade.mirror_entry',side_effect=AssertionError('real order')):
            r=v2_web.api_buy({'symbol':'TEST','mode':'paper','stop':99,'target':110},self.user)
        self.assertEqual(r.status_code,200)
        self.assertTrue(json.loads(r.body)['paper_recorded'])
        with sqlite3.connect(self.path) as con:
            owned=con.execute('SELECT user_id,plan_id,instrument_id FROM user_positions').fetchone()
            self.assertEqual(owned[0],7);self.assertTrue(owned[1].startswith('plan_'));self.assertTrue(owned[2].startswith('ins_'))
            self.assertEqual(con.execute('SELECT COUNT(*) FROM manual_plan_bindings').fetchone()[0],1)

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

    def test_api_preserves_requested_quantity_and_original_receipt(self):
        quote={'TEST':{'price':100,'ts':datetime.now(timezone.utc).isoformat()}}
        request={'symbol':'TEST','mode':'paper','stop':99,'target':110,'qty':20,'request_key':'stable-click-identity'}
        with patch.object(v2_web,'_market_shut',return_value=None),patch.object(v2_web,'_live_map',return_value=quote):
            first=v2_web.api_buy(request,self.user)
            self.assertEqual(first.status_code,200)
            self.assertEqual(json.loads(first.body)['qty'],20)
            with sqlite3.connect(self.path) as con:books.sell(con,7,'IN','TEST',110)
            quote['TEST']['price']=101
            retry=v2_web.api_buy(request,self.user)
            self.assertEqual(retry.status_code,200)
            self.assertEqual(json.loads(retry.body)['entry'],100)
            rebound=v2_web.api_buy(dict(request,qty=21),self.user)
            self.assertEqual(rebound.status_code,409)
        with sqlite3.connect(self.path) as con:self.assertEqual(books.positions(con,7),[])

    def test_retried_manual_plan_does_not_require_a_new_quote_or_rebind_default_levels(self):
        quote={'TEST':{'price':100,'ts':datetime.now(timezone.utc).isoformat()}}
        request={'symbol':'TEST','mode':'paper','stop':99,'target':110,'qty':20,'request_key':'manual-stable-after-close'}
        with patch.object(v2_web,'_market_shut',return_value=None),patch.object(v2_web,'_live_map',return_value=quote):
            first=v2_web.api_buy(request,self.user)
            self.assertEqual(first.status_code,200)
            with sqlite3.connect(self.path) as con:books.sell(con,7,'IN','TEST',110)
            quote.clear()
            repeated=v2_web.api_buy(request,self.user)
            self.assertEqual(repeated.status_code,200)
            self.assertEqual(json.loads(first.body),json.loads(repeated.body))

    def test_immutable_manual_binding_cannot_be_deleted_or_reassigned(self):
        quote={'TEST':{'price':100,'ts':datetime.now(timezone.utc).isoformat()}}
        with patch.object(v2_web,'_market_shut',return_value=None),patch.object(v2_web,'_live_map',return_value=quote):
            result=v2_web.api_buy({'symbol':'TEST','stop':99,'target':110,'qty':20,'request_key':'immutable-manual-request'},self.user)
        self.assertEqual(result.status_code,200)
        with sqlite3.connect(self.path) as con:
            for statement in ('DELETE FROM manual_plan_bindings','UPDATE manual_plan_bindings SET user_id=8'):
                with self.assertRaises(sqlite3.IntegrityError):con.execute(statement)
                con.rollback()

    def test_api_rejects_fractional_quantity_and_does_not_resize_large_request(self):
        quote={'TEST':{'price':100,'ts':datetime.now(timezone.utc).isoformat()}}
        with patch.object(v2_web,'_market_shut',return_value=None),patch.object(v2_web,'_live_map',return_value=quote):
            for qty in (1.5,True,-1):
                result=v2_web.api_buy({'symbol':'TEST','mode':'paper','stop':99,'target':110,'qty':qty},self.user)
                self.assertEqual(result.status_code,400)
            for key in (False,0,''):
                result=v2_web.api_buy({'symbol':'TEST','mode':'paper','stop':99,'target':110,'request_key':key},self.user)
                self.assertEqual(result.status_code,400)
            result=v2_web.api_buy({'symbol':'TEST','mode':'paper','stop':99,'target':110,'qty':10000},self.user)
            self.assertEqual(result.status_code,409)
        with sqlite3.connect(self.path) as con:self.assertEqual(books.positions(con,7),[])

    def test_unapproved_plan_cannot_fall_back_to_manual_buy(self):
        result=v2_web.api_buy({'symbol':'TEST','plan_id':123,'mode':'paper'},self.user)
        self.assertEqual(result.status_code,409)
        self.assertEqual(json.loads(result.body)['code'],'PLAN_NOT_APPROVED')

    def test_execution_health_never_exposes_other_accounts_incidents(self):
        from app.execution_outbox import incident
        with sqlite3.connect(self.path) as con:
            incident(con,7,'OWN','same','own account evidence')
            incident(con,8,'OTHER','same','another account evidence')
        result=json.loads(v2_web.api_execution_health(self.user).body)
        self.assertEqual([i['code'] for i in result['incidents']],['OWN'])
        self.assertFalse(result['capabilities']['routes'][0]['live_certified'])

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
