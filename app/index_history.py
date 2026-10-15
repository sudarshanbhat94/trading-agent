"""Dated actual index levels for research. Index points are never cash shares."""
from datetime import datetime, timedelta, timezone
import math
import os
from pathlib import Path
import sqlite3
from urllib.parse import quote
from zoneinfo import ZoneInfo

import pandas as pd

from .screening import store

INDEX_KEYS = {'NIFTY':'NSE_INDEX|Nifty 50','BANKNIFTY':'NSE_INDEX|Nifty Bank'}
IST = ZoneInfo('Asia/Kolkata')


def normalize(symbol, data, now):
    if symbol not in INDEX_KEYS or data.get('status') != 'success':
        raise ValueError('identified successful index history required')
    rows = {}
    for values in data['data']['candles']:
        stamp = store.timestamp(values[0]).astimezone(IST)
        # Only completed sessions; a source response cannot turn today's partial
        # bar into yesterday's evidence. Preserve the original source timestamp.
        if stamp.date() >= now.astimezone(IST).date(): continue
        if len(values)<6 or any(isinstance(v,bool) for v in values[1:6]):
            raise ValueError('numeric index OHLC required')
        nums = [float(v) for v in values[1:6]]
        o,h,l,c,v = nums
        if not all(math.isfinite(n) for n in nums) or min(o,h,l,c)<=0 or v<0 or not l<=min(o,c)<=max(o,c)<=h:
            raise ValueError('invalid index OHLC')
        day = stamp.date().isoformat()
        row = dict(session=day,ts=values[0],open=o,high=h,low=l,close=c,volume=v)
        if day in rows and rows[day]!=row: raise ValueError('ambiguous index session')
        rows[day]=row
    if len(rows)<127: raise ValueError('127 completed actual index sessions required')
    return dict(symbol=symbol,instrument_key=INDEX_KEYS[symbol],
                price_asof=max(rows),bars=[rows[d] for d in sorted(rows)],
                kind='INDEX_LEVEL',note='Actual Upstox index history; analysis reference, not an executable cash instrument')


def frames(con, now):
    result = {}
    for symbol in INDEX_KEYS:
        data = store.latest(con,symbol,'index_history',now,4)
        if not data: continue
        try:
            checked = normalize(symbol,dict(status='success',data=dict(candles=[
                [r['ts'],r['open'],r['high'],r['low'],r['close'],r['volume']] for r in data['bars']])),now)
            if checked['price_asof'] != data['price_asof'] or data['instrument_key'] != INDEX_KEYS[symbol]:
                continue
            frame = pd.DataFrame(checked['bars']).set_index('session')
            frame.index = pd.to_datetime(frame.index)
            frame.attrs.update(source=data['source'],known_at=data['known_at'],instrument_key=INDEX_KEYS[symbol])
            result[symbol]=frame
        except (ValueError,TypeError,KeyError,IndexError): continue
    return result


def load(now=None, path=None):
    now = now or datetime.now(timezone.utc)
    path = path or os.getenv('SCREENING_DB',str(Path(__file__).resolve().parents[1]/'var'/'screening.db'))
    try:
        with sqlite3.connect(f'file:{path}?mode=ro',uri=True,timeout=5) as con:
            return frames(con,now)
    except (sqlite3.Error,OSError): return {}


def capture(con, asof, errors):
    """Two read-only vendor requests when completed-session history is due."""
    import httpx
    from .config import Settings, settings_from_overrides
    from .market_data import normalize_upstox_access_token
    now = datetime.now(timezone.utc)
    cached = frames(con,now)
    due = [s for s in INDEX_KEYS if s not in cached or str(cached[s].index[-1])[:10]!=str(asof)[:10]]
    if not due: return cached
    import json
    try:
        base = Settings()
        with sqlite3.connect(base.database_path.resolve().as_uri()+'?mode=ro',uri=True) as main:
            overrides={}
            for key,value in main.execute('SELECT key,value FROM runtime_settings'):
                try: overrides[key]=json.loads(value)
                except (ValueError,TypeError): overrides[key]=value
        settings = settings_from_overrides(base,overrides)
    except (sqlite3.Error,OSError,ValueError):
        errors.append('Actual index history unavailable: market-data settings could not be read')
        return cached
    token = normalize_upstox_access_token(settings.upstox_access_token)
    if not token:
        errors.append('Actual index history unavailable: configured market-data token missing')
        return cached
    end = now.astimezone(IST).date()-timedelta(days=1)
    with httpx.Client(timeout=20,headers={'Authorization':'Bearer '+token,'Accept':'application/json'}) as http:
        for symbol in due:
            try:
                url='https://api.upstox.com/v3/historical-candle/'+quote(INDEX_KEYS[symbol],safe='')+'/days/1/'+str(end)+'/'+str(end-timedelta(days=550))
                response=http.get(url);response.raise_for_status()
                payload=normalize(symbol,response.json(),datetime.now(timezone.utc))
                seen=datetime.now(timezone.utc)
                store.save(con,symbol,'index_history','Upstox actual index daily candles',payload,seen.isoformat(),seen)
            except (httpx.HTTPError,ValueError,KeyError,TypeError,IndexError) as exc:
                errors.append(symbol+' actual index history unavailable: '+type(exc).__name__)
    return frames(con,datetime.now(timezone.utc))
