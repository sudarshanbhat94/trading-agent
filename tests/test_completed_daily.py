"""A completed daily snapshot must fix lag, never manufacture current history."""
import asyncio
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
import unittest

import httpx

from app import completed_daily as daily

DAY = '2026-10-27'
NOW = datetime(2026,10,27,12,tzinfo=timezone.utc)
STAMP = int(datetime(2026,10,27,tzinfo=daily.IST).timestamp()*1000)


def payload(**changes):
    return dict(open=100,high=105,low=99,close=104,volume=1000,ts=STAMP,**changes)


class CompletedDailyTest(unittest.TestCase):
    def fetch(self, rows, response, *, status=200, now=NOW):
        calls=[]
        def handler(request):
            calls.append(request)
            return httpx.Response(status,json=response)
        async def run():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                return await daily.fetch(SimpleNamespace(base_url='https://api.upstox.com/v2'),
                                         rows,DAY,now=now,client=client)
        result=asyncio.run(run())
        return (*result,calls)

    def test_bulk_current_daily_bar_has_exact_source_day_and_real_volume(self):
        rows=[dict(symbol='TEST',upstox_instrument_key='NSE_EQ|TEST')]
        bars,receipt,calls=self.fetch(rows,dict(status='success',data={
            'NSE:DISPLAY':dict(instrument_token='NSE_EQ|TEST',live_ohlc=payload())}))
        bar=bars['TEST'][0]
        self.assertEqual((bar.ts,bar.close,bar.volume,bar.source),
                         (DAY+'T00:00:00+05:30',104,1000,'upstox-live:day'))
        self.assertEqual(receipt['status'],'complete')
        self.assertEqual(calls[0].url.path,'/v3/market-quote/ohlc')
        self.assertEqual(calls[0].url.params['interval'],'1d')

    def test_open_session_and_close_publication_buffer_cannot_publish_partial_day(self):
        for minute in (0,29,30,44):
            now=datetime(2026,10,27,15,minute,tzinfo=daily.IST)
            bars,receipt,calls=self.fetch([],{},now=now)
            self.assertFalse(bars);self.assertFalse(calls)
            self.assertEqual(receipt['status'],'target_not_completed')

    def test_previous_current_or_future_wrong_session_is_not_relabelled(self):
        for offset in (-86400000,86400000):
            with self.assertRaises(ValueError):
                daily.daily_bar('TEST',dict(payload(),ts=STAMP+offset),DAY,NOW)
        with self.assertRaises(ValueError):
            daily.daily_bar('TEST',dict(payload(),ts=STAMP/1000),DAY,NOW)

    def test_nonfinite_incoherent_boolean_and_negative_market_fields_refuse(self):
        for change in (dict(high=98),dict(low=102),dict(volume=-1),dict(close=0),
                       dict(close=float('nan')),dict(volume=float('inf')),dict(open=True)):
            with self.subTest(change=change),self.assertRaises((ValueError,TypeError)):
                daily.daily_bar('TEST',dict(payload(),**change),DAY,NOW)

    def test_response_identity_cannot_substitute_a_different_symbol(self):
        rows=[dict(symbol='TEST',upstox_instrument_key='NSE_EQ|TEST')]
        bars,receipt,_=self.fetch(rows,dict(status='success',data={
            'NSE:TEST':dict(instrument_token='NSE_EQ|OTHER',live_ohlc=payload())}))
        self.assertFalse(bars);self.assertEqual(receipt['returned'],0)

    def test_duplicate_source_identity_or_conflicting_daily_fields_refuse(self):
        rows=[dict(symbol='TEST',upstox_instrument_key='NSE_EQ|TEST')]
        item=dict(instrument_token='NSE_EQ|TEST',live_ohlc=payload())
        for data in ({'A':item,'B':item},
                     {'A':dict(item,prev_ohlc=dict(payload(),close=103))}):
            bars,receipt,_=self.fetch(rows,dict(status='success',data=data))
            self.assertFalse(bars);self.assertEqual(receipt['status'],'partial')

    def test_ambiguous_request_aliases_are_not_fetched(self):
        rows=[dict(symbol=s,upstox_instrument_key='NSE_EQ|SAME') for s in ('A','B')]
        bars,receipt,calls=self.fetch(rows,dict(status='success',data={}))
        self.assertFalse(bars);self.assertFalse(calls)
        self.assertEqual(receipt['rejected']['missing_or_ambiguous_identity'],2)

    def test_batches_are_bounded_and_auth_failure_stops_additional_calls(self):
        rows=[dict(symbol=str(i),upstox_instrument_key='NSE_EQ|'+str(i)) for i in range(205)]
        _,receipt,calls=self.fetch(rows,{},status=401)
        self.assertEqual(len(calls),1)
        self.assertEqual(len(calls[0].url.params['instrument_key'].split(',')),100)
        self.assertEqual(receipt['errors'],[dict(type='HTTPStatusError',http_status=401)])

    def test_malformed_success_body_never_becomes_complete(self):
        rows=[dict(symbol='TEST',upstox_instrument_key='NSE_EQ|TEST')]
        for response in ([],dict(status='success',data=[]),dict(status='error',data={})):
            bars,receipt,_=self.fetch(rows,response)
            self.assertFalse(bars);self.assertEqual(receipt['status'],'partial')

    def test_naive_capture_and_notified_closed_day_refuse(self):
        with self.assertRaises(ValueError):
            daily.daily_bar('TEST',payload(),DAY,NOW.replace(tzinfo=None))
        stamp=int(datetime(2026,10,2,tzinfo=daily.IST).timestamp()*1000)
        with self.assertRaises(ValueError):
            daily.daily_bar('TEST',dict(payload(),ts=stamp),'2026-10-02',NOW)


if __name__=='__main__':unittest.main()
