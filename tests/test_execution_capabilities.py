import unittest
from unittest.mock import patch
from app import broker,order_journal
from app.execution_ports import UpstoxPort,InstrumentError,capability_report


class ExecutionCapabilityTest(unittest.TestCase):
    def test_capabilities_do_not_advertise_unimplemented_paper_intraday_lifecycle(self):
        result=capability_report()
        for route in result['routes']:
            self.assertFalse(route['live_certified'])
            if route['broker']=='paper':self.assertEqual(route['implementation'],route['product']=='D')
        self.assertIn('paper intraday order lifecycle',result['unsupported'])

    def test_unsupported_segments_never_reach_broker_or_account_reads(self):
        with patch.object(broker,'state',side_effect=AssertionError('account read')),patch.object(broker,'place_order',side_effect=AssertionError('order')):
            for key in ('NSE_FO|OPT','NSE_INDEX|Nifty 50','BSE_EQ|STOCK','MCX_FO|FUT','NYSE|STOCK','NSE_EQ|'):
                result=order_journal.submit(None,1,'IN','STOCK',key,'BUY',1,100,'D','fixture')
                self.assertIn('unsupported execution capability',result)
                with self.assertRaises(InstrumentError):UpstoxPort().submit(1,key,1,'BUY',product='D',tag='fixture')

    def test_malformed_quantity_and_side_are_refused_before_transport(self):
        with patch.object(broker,'place_order',side_effect=AssertionError('order')):
            for qty in (True,1.5,0,-1):
                self.assertEqual(order_journal.submit(None,1,'IN','STOCK','NSE_EQ|TEST','BUY',qty,100,'D','fixture'),'rejected: invalid order')
            with self.assertRaises(InstrumentError):UpstoxPort().submit(1,'NSE_EQ|TEST',1,'SHORT',product='D',tag='fixture')

    def test_cash_transport_preserves_identity_and_absent_status_is_unknown(self):
        with patch.object(broker,'place_order',return_value={'ok':True,'order_id':'owned'}) as place:
            self.assertTrue(UpstoxPort().submit(2,'NSE_EQ|TEST',1,'BUY',product='D',tag='stable')['ok'])
            place.assert_called_once_with(2,'NSE_EQ|TEST',1,'BUY',price=0.0,product='D',tag='stable')
        with patch.object(broker,'orders',return_value=[]):self.assertIsNone(UpstoxPort().order_status(2,'missing'))
