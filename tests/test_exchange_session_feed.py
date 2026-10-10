from datetime import datetime,timedelta,timezone
import sqlite3
import unittest

from app.exchange_session_feed import normalise, CALENDAR
from app import execution_contracts
from app.instrument_catalog import InstrumentError


class SourcedSessionFeedTest(unittest.TestCase):
    def setUp(self):
        self.now=datetime(2026,10,9,5,tzinfo=timezone.utc)
        self.opens=self.now.replace(hour=3,minute=45)
        self.closes=self.now.replace(hour=10,minute=0)
        ms=lambda d:int(d.timestamp()*1000)
        self.timings=dict(status='success',data=[dict(exchange='NSE',start_time=ms(self.opens),end_time=ms(self.closes))])
        self.status=dict(status='success',data=dict(exchange='NSE',status='NORMAL_OPEN',last_updated=ms(self.opens)))

    def test_current_phase_requires_matching_official_day_and_times(self):
        result,_,_=normalise(self.timings,self.status,'2026-10-09',self.now)
        self.assertTrue(result['open'])
        self.assertEqual(datetime.fromisoformat(result['fresh_until']),self.now+timedelta(minutes=3))
        for phase in ('PRE_OPEN_START','CLOSING_START','NORMAL_CLOSE','UNKNOWN'):
            self.status['data']['status']=phase
            self.assertFalse(normalise(self.timings,self.status,'2026-10-09',self.now)[0]['open'])

    def test_holiday_closed_and_open_without_schedule_refuses(self):
        self.timings['data']=[]
        with self.assertRaises(ValueError):normalise(self.timings,self.status,'2026-10-09',self.now)
        self.status['data']['status']='NORMAL_CLOSE'
        self.assertFalse(normalise(self.timings,self.status,'2026-10-09',self.now)[0]['open'])

    def test_expired_positive_session_cannot_fall_back_to_older_open_record(self):
        payload,start,end=normalise(self.timings,self.status,'2026-10-09',self.now)
        with sqlite3.connect(':memory:') as con:
            execution_contracts.record(con,'session',CALENDAR,dict(open=True,opens_at=self.opens.isoformat(),closes_at=self.closes.isoformat()),
                source='synthetic old observation',observed_at=(self.now-timedelta(minutes=1)).isoformat(),effective_from=start.isoformat(),effective_until=end.isoformat(),now=self.now)
            execution_contracts.record(con,'session',CALENDAR,payload,source='synthetic sourced status fixture',
                observed_at=self.now.isoformat(),effective_from=start.isoformat(),effective_until=end.isoformat(),now=self.now)
            self.assertTrue(execution_contracts._latest(con,'session',CALENDAR,self.now)[1]['open'])
            with self.assertRaises(InstrumentError):
                execution_contracts._latest(con,'session',CALENDAR,self.now+timedelta(minutes=3))

    def test_future_duplicate_wrong_exchange_and_wrong_day_refuse(self):
        self.status['data']['last_updated']=int((self.now+timedelta(seconds=1)).timestamp()*1000)
        with self.assertRaises(ValueError):normalise(self.timings,self.status,'2026-10-09',self.now)
        self.status['data']['last_updated']=int(self.opens.timestamp()*1000)
        self.timings['data']*=2
        with self.assertRaises(ValueError):normalise(self.timings,self.status,'2026-10-09',self.now)
