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
from .account_safety import atomic


MAX_AGE_SECONDS = 30


def ensure_schema(con):
    con.execute('CREATE TABLE IF NOT EXISTS market_execution_quotes('
                'instrument_key TEXT PRIMARY KEY,symbol TEXT NOT NULL,source TEXT NOT NULL,'
                'snapshot_id TEXT NOT NULL,snapshot_at TEXT NOT NULL,observed_at TEXT NOT NULL,payload TEXT NOT NULL)')
    con.execute('CREATE INDEX IF NOT EXISTS market_execution_quotes_symbol ON market_execution_quotes(symbol)')
    con.execute('CREATE TABLE IF NOT EXISTS market_execution_quote_conflicts('
                'instrument_key TEXT NOT NULL,snapshot_at TEXT NOT NULL,first_snapshot_id TEXT NOT NULL,'
                'conflicting_snapshot_id TEXT NOT NULL,observed_at TEXT NOT NULL,payload TEXT NOT NULL,'
                'PRIMARY KEY(instrument_key,first_snapshot_id,conflicting_snapshot_id))')
    for operation in ('UPDATE', 'DELETE'):
        con.execute(f'CREATE TRIGGER IF NOT EXISTS quote_conflicts_no_{operation.lower()} '
                    f'BEFORE {operation} ON market_execution_quote_conflicts '
                    "BEGIN SELECT RAISE(ABORT,'immutable executable quote conflict'); END")


def moment(value):
    if not isinstance(value, str): raise ValueError('Aware exchange timestamp required')
    stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if stamp.tzinfo is None: raise ValueError('Aware exchange timestamp required')
    return stamp


def _upstox_moment(value):
    """Provider v2 supports aware ISO or epoch milliseconds, never guessed units.

    Numeric exchange times must have millisecond width (2001–2286). Seconds,
    floats and booleans are ambiguous inputs and cannot authorize a fill.
    Keep generic journal/evidence timestamps strictly timezone-aware ISO.
    """
    if type(value) is int or (isinstance(value, str) and value.isascii() and value.isdigit()):
        milliseconds = int(value)
        if not 10**12 <= milliseconds < 10**13:
            raise ValueError('Upstox epoch milliseconds required')
        seconds, remainder = divmod(milliseconds, 1000)
        return datetime.fromtimestamp(seconds, timezone.utc).replace(microsecond=remainder * 1000)
    return moment(value).astimezone(timezone.utc)


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
    at = _upstox_moment(item.get('timestamp')); observed = moment(observed_at).astimezone(timezone.utc)
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
    stamp, observed = moment(payload['snapshot_at']), moment(payload['observed_at'])
    if stamp > observed: raise ValueError('Future executable snapshot')
    text = json.dumps(payload, sort_keys=True, separators=(',', ':'), allow_nan=False)
    # Julian-day conversion rounds rapid source times. Serialize an exact aware
    # comparison instead; a nested caller still owns its transaction.
    with atomic(con):
        old = con.execute('SELECT snapshot_id,snapshot_at,payload FROM market_execution_quotes '
                          'WHERE instrument_key=?', (payload['instrument_key'],)).fetchone()
        if old:
            previous = moment(old[1])
            if stamp < previous: return
            if stamp == previous:
                if payload['snapshot_id'] != old[0]:
                    # Conflicting depth at the same source instant must not
                    # select whichever worker happened to arrive first.
                    conflict = json.dumps(dict(first=json.loads(old[2]), conflicting=payload),
                                          sort_keys=True, separators=(',', ':'), allow_nan=False)
                    con.execute('INSERT OR IGNORE INTO market_execution_quote_conflicts VALUES(?,?,?,?,?,?)',
                                (payload['instrument_key'], old[1], old[0], payload['snapshot_id'],
                                 payload['observed_at'], conflict))
                return  # Repeating the original cannot clear its conflict.
        con.execute('INSERT INTO market_execution_quotes VALUES(?,?,?,?,?,?,?) '
                    'ON CONFLICT(instrument_key) DO UPDATE SET symbol=excluded.symbol,source=excluded.source,'
                    'snapshot_id=excluded.snapshot_id,snapshot_at=excluded.snapshot_at,'
                    'observed_at=excluded.observed_at,payload=excluded.payload',
                    (payload['instrument_key'], payload['symbol'], payload['source'], payload['snapshot_id'],
                     payload['snapshot_at'], payload['observed_at'], text))


def read(con, symbols):
    grouped = {}; symbols = sorted(set(symbols))
    for offset in range(0, len(symbols), 500):
        chunk = symbols[offset:offset+500]
        rows = con.execute('SELECT q.symbol,q.payload,EXISTS(SELECT 1 FROM market_execution_quote_conflicts c '
                           'WHERE c.instrument_key=q.instrument_key AND c.first_snapshot_id=q.snapshot_id) '
                           'FROM market_execution_quotes q WHERE q.symbol IN ('+
                           ','.join('?' for _ in chunk)+')', chunk).fetchall()
        for symbol, text, conflict in rows: grouped.setdefault(symbol, []).append((text, conflict))
    # A same-symbol alias collision needs canonical resolution, not first-row selection.
    return {symbol: json.loads(rows[0][0]) for symbol, rows in grouped.items()
            if len(rows)==1 and not rows[0][1]}


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
