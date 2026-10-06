from unittest.mock import patch
from app import entry_contracts,books,broker,order_journal
from tests.test_approved_execution import ApprovedPaperPipelineTest


class SharedEntryContractGateTest(ApprovedPaperPipelineTest):
    def test_direct_personal_buy_cannot_bypass_missing_contract_or_off_regime(self):
        with entry_contracts.using(self.catalogue,self.now):
            for symbol,regime in (('UNKNOWN','ON'),('TEST','OFF')):
                self.assertEqual(books.buy(self.con,2,'IN','manual',symbol,100,20,99,110,regime=regime),0)
            self.assertEqual(books.positions(self.con,2),[])
            self.assertEqual(books.cash(self.con,2),10000)
            self.assertEqual(books.buy(self.con,2,'IN','manual','TEST',100,20,99,110,regime='ON'),20)
            self.assertEqual(self.con.execute("SELECT scope,user_id FROM entry_contract_records").fetchone(),('personal',2))

    def test_no_fixed_target_and_sourced_automatic_rounding_are_not_rejected(self):
        with entry_contracts.using(self.catalogue,self.now):
            stop,target=entry_contracts.normalise_long_levels('TEST',100,98.973,0)
            self.assertEqual((stop,target),(99.0,0.0));self.assertGreaterEqual(stop,98.973)
            self.assertTrue(entry_contracts.check('IN','TEST',20,100,stop,target,regime='ON'))
            with self.assertRaises(ValueError):entry_contracts.normalise_long_levels('UNKNOWN',100,99,0)

    def test_house_writer_and_live_alias_use_same_contract_rules(self):
        from app import v2_live
        with entry_contracts.using(self.catalogue,self.now):
            args=dict(market='IN',strategy='index_directional',symbol='TEST',entry_date=self.now.date().isoformat(),
                      entry_price=100,shares=20,stop=99,target=110,trail=0,conviction=1,why=None,regime='ON')
            self.assertFalse(v2_live.record_entry(self.con,**dict(args,symbol='UNKNOWN')))
            self.assertTrue(v2_live.record_entry(self.con,**args))
            with self.assertRaisesRegex(ValueError,'alias'):
                entry_contracts.check('IN','TEST',20,100,99,110,broker='upstox',key='NSE_EQ|OTHER',regime='ON')
            with self.assertRaises(ValueError):entry_contracts.check('IN','TEST',20,100,99.03,110,regime='ON')
            with self.assertRaises(ValueError):entry_contracts.check('US','TEST',20,100,99,110,regime='ON')

    def test_real_journal_contract_gate_precedes_transmission_and_unknown_retry(self):
        from app import broker_reconciliation
        broker_reconciliation.reconcile(self.con,2,positions=[],holdings=[],trades=[],orders=[],
                                        funds={'data':{'equity':{'available_margin':10000}}})
        state=dict(live_ready=True,exit_ready=True,budget=10000,owner_user_id=2)
        with entry_contracts.using(self.catalogue,self.now),patch.object(broker,'state',return_value=state), \
                patch.object(order_journal,'live_scope_authorized',return_value=(True,'isolated review')), \
                patch('app.live_release.native_policy',return_value={'reference':'isolated policy','source_commit':'a'*40}), \
                patch('app.v2_live.sleeve_view',return_value={'asof':self.now.isoformat(),'regime':'ON'}), \
                patch.object(broker,'place_order',side_effect=TimeoutError('fixture ambiguous transmission')) as wire:
            args=dict(con=self.con,uid=2,market='IN',symbol='UNKNOWN',key='NSE_EQ|TEST',side='BUY',qty=20,reference=100,
                      product='D',reason='fixture manual',stop=99,target=110,strategy='manual',available_cash=10000,semantic_key='fixture-stable')
            rejected=order_journal.submit(**args)
            self.assertIn('unknown or ambiguous',rejected);wire.assert_not_called()
            args['symbol']='TEST'
            self.assertEqual(order_journal.submit(**args),'unknown')
            self.assertEqual(order_journal.submit(**args),'unknown')
            self.assertEqual(wire.call_count,1)
            self.assertEqual(self.con.execute("SELECT COUNT(*) FROM entry_contract_records WHERE scope='broker'").fetchone()[0],1)
            self.assertEqual(self.con.execute('SELECT filled_qty FROM v2_live_orders').fetchone()[0],0)

    def test_live_native_policy_missing_refuses_before_new_intent(self):
        with patch.object(broker,'state',return_value=dict(live_ready=True)), \
                patch.object(order_journal,'live_scope_authorized',return_value=(True,'')), \
                patch('app.live_release.native_policy',return_value=None),patch.object(broker,'place_order') as wire:
            result=order_journal.submit(self.con,2,'IN','TEST','NSE_EQ|TEST','BUY',20,100,'D','fixture',stop=99,target=110,available_cash=10000)
        self.assertIn('native coverage',result);wire.assert_not_called()
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM v2_live_orders').fetchone()[0],0)
