"""Source-dated Upstox daily bars when historical candles lag publication.

OHLC V3 returns a dated daily candle and volume for a batch of instruments.
Only a matching, completed NSE day is accepted. Request/capture time is never
substituted for the candle date; incomplete current sessions remain unavailable.
"""
from collections import Counter
from datetime import date, datetime, time, timezone
import asyncio
import math
from zoneinfo import ZoneInfo

import httpx

from .models import Candle
from .market_regions import INDIA_TRADING_HOLIDAYS

IST = ZoneInfo('Asia/Kolkata')


def _number(value):
    if isinstance(value, bool):
        raise ValueError('Boolean OHLC value')
    result = float(value)
    if not math.isfinite(result):
        raise ValueError('Non-finite OHLC value')
    return result


def daily_bar(symbol, payload, target, now):
    """Pure parser: exact source session, finite prices, volume and chronology."""
    if not isinstance(payload, dict) or now.utcoffset() is None:
        raise ValueError('Identified daily candle and aware capture required')
    target = date.fromisoformat(str(target))
    stamp = _number(payload['ts'])
    # This API documents milliseconds. Do not guess seconds or invent a date.
    opened = datetime.fromtimestamp(stamp / 1000, timezone.utc).astimezone(IST)
    closed = datetime.combine(target, time(15, 45), IST)
    if (opened.date() != target or opened > now or now < closed or target.weekday() >= 5
            or target.isoformat() in INDIA_TRADING_HOLIDAYS):
        raise ValueError('Source daily candle is not the completed target session')
    values = [_number(payload[k]) for k in ('open', 'high', 'low', 'close', 'volume')]
    o, h, low, c, volume = values
    if min(o, h, low, c) <= 0 or volume < 0 or not low <= min(o, c) <= max(o, c) <= h:
        raise ValueError('Invalid daily OHLC or volume')
    return Candle(symbol, datetime.combine(target, time(), IST).isoformat(),
                  o, h, low, c, volume, 'upstox-live:day')


async def fetch(provider, rows, target, *, now=None, client=None):
    """Return bars and bounded diagnostics; unknown/ambiguous identities refuse.

    At most 100 identities/request avoids oversized query strings and reduces
    a 2,600-name close refresh to roughly 27 calls instead of thousands. Caller
    retains historical retrieval for older gaps and initial history bootstrap.
    """
    now = now or datetime.now(timezone.utc)
    day = date.fromisoformat(str(target))
    if now.utcoffset() is None:
        raise ValueError('Aware capture time required')
    diagnostics = dict(target=day.isoformat(), captured_at=now.isoformat(), requested=len(rows),
                       returned=0, requests=0, errors=[], rejected={}, source='upstox-ohlc-v3:1d')
    if now < datetime.combine(day, time(15, 45), IST):
        diagnostics['status'] = 'target_not_completed'
        return {}, diagnostics
    base = provider.base_url.rstrip('/')
    if not base.endswith('/v2'):
        diagnostics['status'] = 'unsupported_api_base'
        return {}, diagnostics
    url = base[:-3] + '/v3/market-quote/ohlc'
    keys = Counter(r.get('upstox_instrument_key') for r in rows)
    symbols = Counter(r.get('symbol') for r in rows)
    accepted = [r for r in rows if isinstance(r.get('symbol'), str)
                and bool(r['symbol'].strip())
                and isinstance(r.get('upstox_instrument_key'), str)
                and r['upstox_instrument_key'].startswith('NSE_EQ|')
                and len(r['upstox_instrument_key']) > len('NSE_EQ|')
                and symbols[r['symbol']] == keys[r['upstox_instrument_key']] == 1]
    rejected = Counter({'missing_or_ambiguous_identity': len(rows)-len(accepted)})
    result = {}

    async def collect(http):
        for offset in range(0, len(accepted), 100):
            batch = accepted[offset:offset+100]
            diagnostics['requests'] += 1
            try:
                response = await http.get(url, params=dict(interval='1d', instrument_key=','.join(
                    r['upstox_instrument_key'] for r in batch)))
                response.raise_for_status()
                body = response.json()
                data = body.get('data') if isinstance(body, dict) and body.get('status') == 'success' else None
                if not isinstance(data, dict):
                    raise ValueError('Daily snapshot response unavailable')
                by_key = {}
                for item in data.values():
                    if isinstance(item, dict):
                        by_key.setdefault(item.get('instrument_token'), []).append(item)
                for row in batch:
                    matches = by_key.get(row['upstox_instrument_key'], [])
                    if len(matches) != 1:
                        rejected['response_identity_missing_or_ambiguous'] += 1
                        continue
                    bars = []
                    for field in ('live_ohlc', 'prev_ohlc'):
                        try:
                            bars.append(daily_bar(row['symbol'], matches[0][field], day, now))
                        except (ValueError, TypeError, KeyError, OverflowError, OSError):
                            continue
                    if not bars or any(b != bars[0] for b in bars[1:]):
                        rejected['target_session_missing_invalid_or_conflicting'] += 1
                        continue
                    result[row['symbol']] = [bars[0]]
            except (httpx.HTTPError, ValueError, TypeError) as exc:
                # Never serialize authentication headers, full request URLs or response bodies.
                error = {'type': type(exc).__name__}
                if isinstance(exc, httpx.HTTPStatusError):
                    error['http_status'] = exc.response.status_code
                if len(diagnostics['errors']) < 5:
                    diagnostics['errors'].append(error)
                if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in (401, 403, 429):
                    break
            if offset+100 < len(accepted):
                await asyncio.sleep(.1)

    if client is None:
        async with httpx.AsyncClient(timeout=15, headers=provider._headers()) as http:
            await collect(http)
    else:
        await collect(client)
    diagnostics.update(returned=len(result), rejected=dict(rejected),
                       status='complete' if len(result)==len(rows) else 'partial')
    return result, diagnostics
