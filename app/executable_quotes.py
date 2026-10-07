"""Exchange snapshot evidence, separate from last-trade valuation marks.

Upstox full quotes document five bids/asks and circuit bounds. Never derive
depth from volume, last price or a candle. Snapshot time is not last-trade time.
Only evidence actually used for an order is frozen in the execution journal.
"""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math


MAX_AGE_SECONDS = 30


def ensure_schema(con):
    con.execute('CREATE TABLE IF NOT EXISTS market_execution_quotes('
                'instrument_key TEXT PRIMARY KEY,symbol TEXT NOT NULL,source TEXT NOT NULL,'
                'snapshot_id TEXT NOT NULL,snapshot_at TEXT NOT NULL,observed_at TEXT NOT NULL,payload TEXT NOT NULL)')
    con.execute('CREATE INDEX IF NOT EXISTS market_execution_quotes_symbol ON market_execution_quotes(symbol)')


def moment(value):
    if not isinstance(value, str): raise ValueError('Aware exchange timestamp required')
    stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if stamp.tzinfo is None: raise ValueError('Aware exchange timestamp required')
    return stamp


def _price(value):
    if isinstance(value, bool): raise ValueError('Invalid executable price')
    try: price = Decimal(str(value))
    except (InvalidOperation, ValueError): raise ValueError('Invalid executable price') from None
    if not price.is_finite() or price <= 0 or not math.isfinite(float(price)):
        raise ValueError('Invalid executable price')
    return price


def _levels(rows, reverse):
    if not isinstance(rows, list) or len(rows) > 5: raise ValueError('Five-level depth required')
    levels = []
    for row in rows:
        if not isinstance(row, dict): raise ValueError('Invalid depth row')
        quantity = row.get('quantity')
        if type(quantity) is not int or quantity < 0: raise ValueError('Whole depth quantity required')
        if quantity == 0: continue  # Provider pads empty levels with zeros.
        price = _price(row.get('price'))
        levels.append({'price': str(price), 'quantity': quantity})
    prices = [Decimal(r['price']) for r in levels]
    if prices != sorted(set(prices), reverse=reverse): raise ValueError('Depth must be ordered and unambiguous')
    return levels


def normalize_upstox(item, key, symbol, *, observed_at):
    if not isinstance(item, dict) or item.get('instrument_token') != key or item.get('symbol') != symbol:
        raise ValueError('Quote identity differs from requested instrument')
    if not key.startswith('NSE_EQ|'): raise ValueError('Executable snapshot implements NSE cash only')
    at = moment(item.get('timestamp')); observed = moment(observed_at)
    if at > observed: raise ValueError('Future executable snapshot')
    depth = item.get('depth')
    if not isinstance(depth, dict): raise ValueError('Exchange depth unavailable')
    bids, asks = _levels(depth.get('buy'), True), _levels(depth.get('sell'), False)
    lower, upper = _price(item.get('lower_circuit_limit')), _price(item.get('upper_circuit_limit'))
    if lower >= upper: raise ValueError('Invalid circuit bounds')
    if any(not lower <= Decimal(r['price']) <= upper for r in bids+asks): raise ValueError('Depth outside circuit bounds')
    if bids and asks and Decimal(bids[0]['price']) >= Decimal(asks[0]['price']):
        raise ValueError('Locked or crossed quote is not executable evidence')
    payload = dict(instrument_key=key, symbol=symbol, source='upstox-full-quote-v2',
                   snapshot_at=at.isoformat(), bids=bids, asks=asks,
                   lower_circuit=str(lower), upper_circuit=str(upper))
    payload['snapshot_id'] = 'snap_'+hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    payload['observed_at'] = observed.isoformat()
    return payload


def write(con, payload):
    _verify_fingerprint(payload)
    con.execute('INSERT INTO market_execution_quotes VALUES(?,?,?,?,?,?,?) '
                'ON CONFLICT(instrument_key) DO UPDATE SET symbol=excluded.symbol,source=excluded.source,'
                'snapshot_id=excluded.snapshot_id,snapshot_at=excluded.snapshot_at,observed_at=excluded.observed_at,payload=excluded.payload '
                'WHERE julianday(excluded.snapshot_at)>julianday(market_execution_quotes.snapshot_at)',
                (payload['instrument_key'], payload['symbol'], payload['source'], payload['snapshot_id'],
                 payload['snapshot_at'], payload['observed_at'], json.dumps(payload, sort_keys=True, separators=(',', ':'))))


def read(con, symbols):
    grouped = {}; symbols = sorted(set(symbols))
    for offset in range(0, len(symbols), 500):
        chunk = symbols[offset:offset+500]
        rows = con.execute('SELECT symbol,payload FROM market_execution_quotes WHERE symbol IN ('+
                           ','.join('?' for _ in chunk)+')', chunk).fetchall()
        for symbol, text in rows: grouped.setdefault(symbol, []).append(text)
    # A same-symbol alias collision needs canonical resolution, not first-row selection.
    return {symbol: json.loads(rows[0]) for symbol, rows in grouped.items() if len(rows)==1}


def top(snapshot, side, *, key, symbol, now, after=None):
    if side not in {'BUY','SELL'}:raise ValueError('Executable side required')
    if not isinstance(snapshot, dict) or snapshot.get('source') != 'upstox-full-quote-v2' or \
            snapshot.get('instrument_key') != key or snapshot.get('symbol') != symbol:
        raise ValueError('Dated executable quote identity unavailable')
    _verify_fingerprint(snapshot)
    stamp, observed = moment(snapshot['snapshot_at']), moment(snapshot['observed_at'])
    if not stamp <= observed <= now or (now-stamp).total_seconds() > MAX_AGE_SECONDS:
        raise ValueError('Executable depth is stale or future')
    if after is not None and stamp <= after: raise ValueError('Awaiting a later exchange snapshot')
    levels = snapshot.get('asks' if side == 'BUY' else 'bids')
    if not levels: raise ValueError('No executable liquidity on this side')
    price = _price(levels[0]['price']); quantity = levels[0]['quantity']
    if type(quantity) is not int or quantity < 1: raise ValueError('Invalid executable quantity')
    if not _price(snapshot['lower_circuit']) <= price <= _price(snapshot['upper_circuit']):
        raise ValueError('Price outside current circuit bounds')
    return float(price), quantity


def _verify_fingerprint(snapshot):
    body = {field:snapshot[field] for field in ('instrument_key','symbol','source','snapshot_at',
                                               'bids','asks','lower_circuit','upper_circuit')}
    expected = 'snap_'+hashlib.sha256(json.dumps(body, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    if snapshot.get('snapshot_id') != expected: raise ValueError('Executable snapshot content changed')
