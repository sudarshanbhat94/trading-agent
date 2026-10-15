"""Wire-shape tests only: never authenticate or transmit to a real broker."""
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import Mock, patch
import httpx
from app.angelone_port import AngelOnePort, capabilities
from app.instrument_catalog import InstrumentError


class AngelOneTransportTest(unittest.TestCase):
    def setUp(self):
        self.state=dict(owner_user_id=2,expires_at=(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat())
        for name in ('access_token','api_key','client_local_ip','client_public_ip','client_mac'):
            self.state[name]='synthetic-not-a-credential'
        self.request=Mock()
        self.port=AngelOnePort(lambda uid:self.state,self.request)

    def response(self,data):
        self.request.return_value=httpx.Response(200,json=dict(status=True,data=data),request=httpx.Request('GET','https://example.invalid'))

    def test_submit_ack_is_not_fill_and_timeout_is_not_retried(self):
        self.response(dict(orderid='fixture-order'))
        with patch('app.angelone_port.authorized',return_value=(True,'fixture authorization')):
            result=self.port.submit(2,'NSE_EQ|123',20,'BUY',product='D',tag='fixture-intent',symbol='TEST-EQ')
            self.assertNotIn('filled',result);self.assertEqual(result['order_id'],'fixture-order')
            call=self.request.call_args
            self.assertEqual(call.args[:2],('POST','https://apiconnect.angelone.in/rest/secure/angelbroking/order/v1/placeOrder'))
            self.assertEqual(call.kwargs['json']['quantity'],'20');self.assertFalse(call.kwargs['follow_redirects'])
            self.request.reset_mock();self.request.side_effect=httpx.ReadTimeout('fixture uncertainty')
            with self.assertRaises(httpx.ReadTimeout):self.port.submit(2,'NSE_EQ|123',20,'BUY',product='D',tag='fixture-intent',symbol='TEST-EQ')
            self.assertEqual(self.request.call_count,1)
        self.request.reset_mock()
        with patch('app.angelone_port.authorized',return_value=(False,'not certified')):
            with self.assertRaises(InstrumentError):self.port.submit(2,'NSE_EQ|123',20,'BUY',product='D',tag='fixture-intent',symbol='TEST-EQ')
        self.request.assert_not_called()

    def test_owner_expiry_malformed_and_native_protection_fail_closed(self):
        self.response([])
        for changes in (dict(owner_user_id=3),dict(owner_user_id=True),dict(expires_at='undated'),dict(access_token='')):
            old=dict(self.state);self.state.update(changes)
            with self.assertRaises(InstrumentError):self.port.orders(2)
            self.state=old
        self.request.assert_not_called()
        self.response([dict(exchange='NSE',symboltoken='123',producttype='DELIVERY',transactiontype='BUY',orderid='fixture',filledshares=1,averageprice='NaN')])
        with self.assertRaises(InstrumentError):self.port.orders(2)
        self.response([dict(exchange='BSE',symboltoken='123',producttype='DELIVERY',transactiontype='BUY',orderid='fixture',filledshares=1,averageprice=100)])
        with self.assertRaises(InstrumentError):self.port.orders(2)
        for method in ('place_stop','protection_status','cancel_protection'):
            with self.assertRaises(InstrumentError):getattr(self.port,method)(2)
        self.assertFalse(capabilities()['orchestration_enabled'])

    def test_account_evidence_margin_and_safe_modification(self):
        self.response([dict(exchange='NSE',symboltoken='123',producttype='DELIVERY',transactiontype='BUY',orderid='fixture',fillid='trade',fillsize='2',fillprice='100',filltime='09:30:00')])
        trade=self.port.trades(2)[0]
        self.assertEqual(trade['quantity'],2);self.assertTrue(trade['executed_at'].endswith('+05:30'))
        self.response(dict(availablecash='10000',net='100000',collateral='90000'))
        self.assertEqual(self.port.funds(2)['data']['equity']['available_margin'],10000)
        self.request.reset_mock()
        for rows in ([],[dict(exchange='NFO',qty=1)], [dict(exchange='NSE',qty=True)]):
            with self.assertRaises(InstrumentError):self.port.margin(2,rows)
        with self.assertRaises(InstrumentError):self.port.modify(2,'fixture',quantity=2)
        self.request.assert_not_called()
        self.response(dict(orderid='fixture'))
        original=dict(variety='NORMAL',orderid='fixture',exchange='NSE',ordertype='MARKET',producttype='DELIVERY',duration='DAY',symboltoken='123',tradingsymbol='TEST-EQ')
        self.port.modify(2,'fixture',quantity=3,original=original)
        self.assertEqual(self.request.call_args.kwargs['json']['quantity'],'3')
