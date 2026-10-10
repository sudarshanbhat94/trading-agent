"""Source-backed, paper-only rules for verified Nifty 500 cash equities.

NSE CMTR73927, Part D Annexures 1/10 define the MII fields. The NSE master
specification defines BidIntrvl in paise; confirm it independently against
the official Upstox rupee CSV and raw JSON before accepting a conversion.
EQ uses the NSE Clearing T+1 cycle; T0, special series and derivatives refuse.
This is data permission, never signal approval, live certification or a fill.
"""
from collections import Counter
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from email.utils import parsedate_to_datetime
import csv
import gzip
import io
import json

from . import execution_contracts, instrument_catalog
from .account_safety import atomic
from .exchange_session_feed import archive_bytes, CALENDAR, IST
from .market_regions import INDIA_TRADING_HOLIDAYS

VERSION = 'nse-cash-paper-rules-v1'
JSON_URL = 'https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz'
CSV_URL = 'https://assets.upstox.com/market-quote/instruments/exchange/NSE.csv.gz'
ASM_URL = 'https://www.nseindia.com/api/reportASM'
GSM_URL = 'https://www.nseindia.com/api/reportGSM'
ACTIONS_URL = 'https://www.nseindia.com/api/corporates-corporateActions'
LAYOUT_URL = 'https://nsearchives.nseindia.com/content/circulars/CMTR73927.zip'
UNITS_URL = 'https://nsearchives.nseindia.com/web/sites/default/files/inline-files/NSE_MasterData_Technical_Specifications.pdf'
SETTLEMENT_URL = 'https://www.nseclearing.in/clearing-settlement/capital-market/settlement-cycle'
HEADERS = {'User-Agent': 'Mozilla/5.0', 'Accept': 'application/json,text/csv,*/*',
           'Referer': 'https://www.nseindia.com/all-reports'}


def previous_session(day):
    day -= timedelta(days=1)
    while day.weekday() >= 5 or day.isoformat() in INDIA_TRADING_HOLIDAYS:
        day -= timedelta(days=1)
    return day


def _integer(value):
    if isinstance(value, bool): raise ValueError('Invalid integral rule')
    number = Decimal(str(value))
    if not number.is_finite() or number < 1 or number != int(number):
        raise ValueError('Invalid integral rule')
    return int(number)


def expanded(raw):
    if len(raw) > 32 * 1024 * 1024: raise ValueError('Source too large')
    with gzip.GzipFile(fileobj=io.BytesIO(raw)) as handle:
        data = handle.read(128 * 1024 * 1024 + 1)
    if len(data) > 128 * 1024 * 1024: raise ValueError('Expanded source too large')
    return data.decode('utf-8-sig')


def unique(rows, key):
    result, duplicates = {}, set()
    for row in rows:
        identity = key(row)
        if identity in result: duplicates.add(identity)
        result[identity] = row
    for identity in duplicates: result.pop(identity)
    return result


def surveillance(asm, gsm, day):
    if not isinstance(asm, dict) or not isinstance(gsm, list):
        raise ValueError('Surveillance layout changed')
    rows = []
    for name in ('longterm', 'shortterm'):
        if not isinstance(asm.get(name), dict) or not isinstance(asm[name].get('data'), list):
            raise ValueError('Incomplete ASM source')
        rows.extend(asm[name]['data'])
    rows.extend(gsm)
    banned = set()
    for row in rows:
        if not isinstance(row, dict) or not row.get('isin') or not row.get('symbol'):
            raise ValueError('Unidentified surveillance row')
        stamp = str(row.get('asmTime') or row.get('gsmTime') or '')[:11]
        source_day = datetime.strptime(stamp, '%d-%b-%Y').date()
        if source_day > day: raise ValueError('Future surveillance source')
        banned.add(row['isin'])
    # The sources are current official HTTP observations. Dates on individual
    # restrictions are their effective dates, NOT publication/freshness times.
    return banned


def corporate_actions(rows, start, end):
    if not isinstance(rows, list): raise ValueError('Corporate-action source unavailable')
    pending = set()
    for row in rows:
        if not isinstance(row, dict) or not row.get('isin') or not row.get('symbol'):
            raise ValueError('Unidentified corporate action')
        exdate = datetime.strptime(str(row.get('exDate')), '%d-%b-%Y').date()
        if not start <= exdate <= end: raise ValueError('Corporate-action window mismatch')
        pending.add(row['isin'])
    return pending


def assemble(nse_rows, broker_rows, csv_rows, asm, gsm, actions, members, *, day, master_day):
    """Pure parser; no writes, inferred identities, or source-time backdating."""
    if master_day != previous_session(day) or not 400 <= len(members) <= 600:
        raise ValueError('Complete membership and previous NSE master required')
    nse = unique((r for r in nse_rows if r.get('SctySrs') == 'EQ'), lambda r: r.get('TckrSymb'))
    brokers = unique((r for r in broker_rows if r.get('segment') == 'NSE_EQ' and r.get('instrument_type') == 'EQ'), lambda r: r.get('trading_symbol'))
    csv_master = unique((r for r in csv_rows if r.get('exchange') == 'NSE_EQ' and r.get('instrument_type') == 'EQUITY'), lambda r: r.get('tradingsymbol'))
    banned = surveillance(asm, gsm, day)
    pending = corporate_actions(actions, master_day, day + timedelta(days=7))
    accepted, rejected = [], Counter()
    for symbol in sorted(members):
        try:
            n, b, c = nse[symbol], brokers[symbol], csv_master[symbol]
            isin = n['ISIN']; alias = 'NSE_EQ|' + isin
            if len(isin) != 12 or b.get('exchange') != 'NSE' or b.get('isin') != isin or \
                    b.get('instrument_key') != alias or c.get('instrument_key') != alias or \
                    str(n['FinInstrmId']) != str(b.get('exchange_token')) or str(n['FinInstrmId']) != str(c.get('exchange_token')):
                raise ValueError('identity conflict')
            # Status 6 is price discovery, NOT suspended (CMTR73927 Part D).
            # Session phase is checked separately against the current source.
            if n.get('DelFlg') != 'N' or n.get('ElgbltyNrmlMkt') != '1' or \
                    n.get('SctyStsNrmlMkt') not in {'1','2','4','5','6'} or n.get('SttlmTp') != '1':
                raise ValueError('ineligible or unsupported NSE cash contract')
            tick = instrument_catalog._positive_decimal(n['BidIntrvl'], 'NSE paise tick') / 100
            if tick != instrument_catalog._positive_decimal(c['tick_size'], 'Upstox rupee tick') or \
                    tick * 100 != instrument_catalog._positive_decimal(b['tick_size'], 'Upstox raw tick'):
                raise ValueError('tick unit conflict')
            lot = _integer(n['NewBrdLotQty'])
            if lot != _integer(b['lot_size']) or lot != _integer(c['lot_size']):
                raise ValueError('lot conflict')
            freeze = _integer(b['freeze_quantity'])
            low, high = [instrument_catalog._positive_decimal(x, 'NSE circuit') for x in n['PricRg'].split('-')]
            if low >= high or low % tick or high % tick or freeze < lot:
                raise ValueError('invalid circuit or freeze rule')
            spec = instrument_catalog.Instrument('NSE', 'NSE_EQ', 'EQUITY', symbol, 'INR', isin,
                'EQ', lot_size=lot, tick_size=str(tick), freeze_quantity=freeze, settlement='T+1')
            rules = dict(lot_size=lot, tick_size=str(tick), freeze_quantity=freeze,
                lower_circuit=str(low), upper_circuit=str(high), settlement='T+1', calendar=CALENDAR,
                banned=isin in banned, corporate_action_pending=isin in pending,
                normalizer_version=VERSION, master_day=master_day.isoformat(), execution_scope='paper')
            accepted.append((spec, alias, rules))
        except KeyError:
            rejected['missing or ambiguous source identity'] += 1
        except (ValueError, ArithmeticError) as exc:
            rejected[str(exc) if str(exc) in {'identity conflict','tick unit conflict','lot conflict',
                'ineligible or unsupported NSE cash contract','invalid circuit or freeze rule'} else 'invalid source rule'] += 1
    if not accepted: raise ValueError('No source-backed equity contracts')
    return accepted, dict(rejected)


def fetch(http, url, root, *, params=None, compressed=False, now=None):
    with http.stream('GET', url, params=params, follow_redirects=False) as response:
        response.raise_for_status(); parts = []; size = 0
        for part in response.iter_bytes():
            size += len(part)
            if size > 32 * 1024 * 1024: raise ValueError('Source response exceeds size bound')
            parts.append(part)
        raw = b''.join(parts)
        headers = dict(response.headers)
    # Availability begins after the response, never at refresh-job start.
    now = now or datetime.now(timezone.utc)
    digest, filename = archive_bytes(root, raw, suffix='.gz' if compressed else '.json')
    return raw, dict(source=url, params=params, sha256=digest, path=filename,
                     observed_at=now.isoformat()), headers


def refresh(con, root, members, *, client=None, now=None):
    """Current official observations; only the dedicated source catalogue writes."""
    import httpx
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None: raise ValueError('Aware source time required')
    day = now.astimezone(IST).date(); master_day = previous_session(day)
    master_url = 'https://nsearchives.nseindia.com/content/cm/NSE_CM_security_' + master_day.strftime('%d%m%Y') + '.csv.gz'
    http = client or httpx.Client(headers=HEADERS, timeout=15, follow_redirects=False)
    sources = []
    source_now = now if client is not None else None
    try:
        nraw, source, _ = fetch(http, master_url, root, compressed=True, now=source_now); sources.append(source)
        nse_rows = list(csv.DictReader(io.StringIO(expanded(nraw))))
        if not nse_rows or not {'TckrSymb','SctySrs','ISIN','BidIntrvl','PricRg'} <= nse_rows[0].keys():
            raise ValueError('NSE master layout changed')
        results = []
        for url in (JSON_URL, CSV_URL):
            raw, source, headers = fetch(http, url, root, compressed=True, now=source_now)
            published = parsedate_to_datetime(headers['last-modified'])
            if published.tzinfo is None or published > now or published.astimezone(IST).date() != day:
                raise ValueError('Current official broker master required')
            source['published_at'] = published.isoformat(); sources.append(source); results.append(expanded(raw))
        jrows, crows = json.loads(results[0]), list(csv.DictReader(io.StringIO(results[1])))
        data = []
        for url in (ASM_URL, GSM_URL, ACTIONS_URL):
            params = dict(index='equities', from_date=master_day.strftime('%d-%m-%Y'),
                          to_date=(day+timedelta(days=7)).strftime('%d-%m-%Y')) if url == ACTIONS_URL else None
            raw, source, _ = fetch(http, url, root, params=params, now=source_now)
            sources.append(source); data.append(json.loads(raw))
        rows, rejected = assemble(nse_rows,jrows,crows,*data,members,day=day,master_day=master_day)
        # The snapshot's source day remains the real NSE master day. Current
        # observations and rules expire at midnight; a Monday refresh cannot
        # reuse Friday's rule review or falsify its availability timestamp.
        observed = datetime.now(timezone.utc) if client is None else now
        if observed.astimezone(IST).date() != day:
            raise ValueError('Source refresh crossed the session-day boundary')
        end = datetime.combine(day+timedelta(days=1),datetime.min.time(),IST)
        source = json.dumps(dict(normalizer_version=VERSION,sources=sources,
            specifications=[LAYOUT_URL,UNITS_URL,SETTLEMENT_URL]), sort_keys=True)
        with atomic(con):
            snapshot = instrument_catalog.import_snapshot(con,[(s,k) for s,k,_ in rows],provider='upstox',
                source=source,source_day=master_day.isoformat(),observed_at=observed.isoformat(),now=observed)
            for spec,alias,rules in rows:
                rules['actions_reviewed_at'] = observed.isoformat()
                execution_contracts.record(con,'rules',spec.id,rules,source=source,
                    observed_at=observed.isoformat(),effective_from=observed.isoformat(),
                    effective_until=end.isoformat(),now=observed)
        return dict(status='ok',version=VERSION,execution_scope='paper',snapshot_id=snapshot,
            master_day=master_day.isoformat(),observed_at=observed.isoformat(),members=len(members),
            rules=len(rows),restricted=sum(r['banned'] or r['corporate_action_pending'] for _,_,r in rows),
            rejected=rejected,broker_execution=False)
    finally:
        if client is None: http.close()
