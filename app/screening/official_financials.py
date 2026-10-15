"""Bounded NSE XBRL collection; publication time never substitutes for capture.

Only whole-company contexts, explicit units and comparable annual durations
are accepted. Neither quarter sums nor industrial leverage proxies are made
up for lenders. Original filing hashes/URLs accompany every normalized result.
"""
from datetime import datetime, timedelta, timezone
import hashlib
import os
from pathlib import Path
from urllib.parse import urlparse
import xml.etree.ElementTree as ET

from .financials import finite_amount, fiscal_date
from ..exchange_session_feed import archive_bytes

API = 'https://www.nseindia.com/api/'
NS = {'x':'http://www.xbrl.org/2003/instance'}
FIELDS = {
    'annualNetIncome': ('ProfitLossForPeriod','ProfitLossForThePeriod'),
    'annualTotalRevenue': ('RevenueFromOperations',),
    'annualOperatingCashFlow': ('CashFlowsFromUsedInOperatingActivities',),
    'annualStockholdersEquity': ('Equity',),
    'annualBasicEPS': ('BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations',),
}
BANK = {'gross_npa_pct': ('PercentageOfGrossNpa',),
        'net_npa_pct': ('PercentageOfNpa',),
        'capital_adequacy_pct': ('CapitalAdequacyRatio', 'CapitalAdequecyRatio'),
        'cet1_pct': ('CommonEquityTier1Ratio',)}


def _tag(e): return e.tag.rsplit('}',1)[-1]


def document(raw, symbol):
    if len(raw)>2*1024*1024 or b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():
        raise ValueError('Unsafe or oversized financial XML')
    try:root=ET.fromstring(raw)
    except ET.ParseError:raise ValueError('Invalid financial XBRL') from None
    if root.tag!='{'+NS['x']+'}xbrl': raise ValueError('Financial XBRL instance required')
    identities={e.text for e in root if _tag(e)=='Symbol'}
    if identities!={symbol}: raise ValueError('Financial filing symbol mismatch')
    bases={e.text for e in root if _tag(e)=='NatureOfReportStandaloneConsolidated'}
    if len(bases)!=1 or not bases <= {'Standalone','Non-Consolidated','Consolidated'}:
        raise ValueError('Unambiguous statement basis required')
    basis='Standalone' if 'Consolidated' not in bases else 'Consolidated'
    units={}
    for e in root.findall('x:unit',NS):
        measures=[m.text for m in e.findall('x:measure',NS)]
        if measures==['iso4217:INR']:units[e.get('id')]='INR'
        elif measures==['xbrli:pure']:units[e.get('id')]='pure'
        elif (e.findtext('x:divide/x:unitNumerator/x:measure',namespaces=NS)=='iso4217:INR'
              and e.findtext('x:divide/x:unitDenominator/x:measure',namespaces=NS)=='xbrli:shares'):
            units[e.get('id')]='INRPerShare'
    contexts={}
    for e in root.findall('x:context',NS):
        if any(_tag(x) in ('explicitMember','typedMember') for x in e.iter()):continue
        start=e.findtext('x:period/x:startDate',namespaces=NS)
        end=e.findtext('x:period/x:endDate',namespaces=NS) or e.findtext('x:period/x:instant',namespaces=NS)
        if end:contexts[e.get('id')]=(start,end)
    facts={}
    for e in root:
        context=contexts.get(e.get('contextRef'))
        if not context or e.get('{http://www.w3.org/2001/XMLSchema-instance}nil') in ('true','1'):continue
        unit=units.get(e.get('unitRef'))
        if not unit:continue
        if e.get('scale') not in (None,'0'):raise ValueError('Unexpected financial scaling')
        key=(_tag(e),context,unit)
        value=finite_amount(e.text)
        if key in facts and facts[key]!=value:raise ValueError('Conflicting whole-company financial facts')
        facts[key]=value
    series={}; nonannual=[]; bank=[]
    for (tag,(start,end),unit),value in facts.items():
        duration=(fiscal_date(end)-fiscal_date(start)).days+1 if start else None
        annual=duration is not None and 330<=duration<=400
        for field,names in FIELDS.items():
            expected='INRPerShare' if field=='annualBasicEPS' else 'INR'
            if tag in names and unit==expected and (annual or (field=='annualStockholdersEquity' and start is None)):
                series.setdefault(field,{})[end]=value
        if tag in ('BorrowingsCurrent','BorrowingsNoncurrent') and unit=='INR' and start is None:
            series.setdefault(tag,{})[end]=value
        if tag in FIELDS['annualNetIncome'] and start and not annual:
            nonannual.append(dict(start=start,end=end,days=duration))
        for field,names in BANK.items():
            if tag in names and unit=='pure' and start:
                # NSE XBRL uses fractions, even though the tag says percentage.
                if not 0<=value<=1:raise ValueError('Bank ratio must be an identified fraction')
                bank.append(dict(metric=field,period_end=end,value_pct=value*100))
    ends=set(series.get('BorrowingsCurrent',{})) & set(series.get('BorrowingsNoncurrent',{}))
    if ends:series['annualTotalDebt']={end:series['BorrowingsCurrent'][end]+series['BorrowingsNoncurrent'][end] for end in ends}
    return dict(basis=basis,series=series,bank=bank,nonannual_periods=nonannual)


def _rows(http, symbol, now, path):
    local=now.astimezone(timezone(timedelta(hours=5,minutes=30)))
    r=http.get(API+path,params=dict(index='equities',symbol=symbol,
        from_date=(local-timedelta(days=5*366)).strftime('%d-%m-%Y'),to_date=local.strftime('%d-%m-%Y')),
        follow_redirects=False)
    r.raise_for_status()
    if len(r.content)>4*1024*1024:raise ValueError('Oversized financial filing index')
    p=r.json();rows=p if isinstance(p,list) else p.get('data')
    if not isinstance(rows,list) or any(not isinstance(row,dict) for row in rows):raise ValueError('Financial filing index unavailable')
    if isinstance(p,dict) and int(p.get('totalCount',len(rows)))>len(rows):
        raise ValueError('Financial filing index is incomplete')
    return rows


def filings(http,symbol,now,bank_only=False):
    if now.tzinfo is None:raise ValueError('Aware financial capture time required')
    rows=_rows(http,symbol,now,'integrated-filing-results')
    if not bank_only:rows += _rows(http,symbol,now,'corporates-financial-results')
    valid=[]
    for r in rows:
        if r.get('symbol')!=symbol or ('type' in r and 'Financial' not in r['type']):continue
        url=r.get('xbrl') or ''
        parsed=urlparse(url)
        if parsed.scheme!='https' or parsed.hostname!='nsearchives.nseindia.com' or not parsed.path.startswith('/corporate/xbrl/') or not parsed.path.endswith('.xml'):continue
        try:published=datetime.strptime(r.get('broadcast_Date') or r.get('broadCastDate'),'%d-%b-%Y %H:%M:%S').replace(tzinfo=timezone(timedelta(hours=5,minutes=30)))
        except (ValueError,TypeError):continue
        if published>now:continue
        if not bank_only and r.get('audited')!='Audited':continue
        valid.append((published,url))
    valid=sorted(set(valid),reverse=True)
    if not valid:raise ValueError('No identified official financial filings')
    documents=[]
    for published,url in valid[:(2 if bank_only else 8)]:
        r=http.get(url,follow_redirects=False);r.raise_for_status()
        parsed=document(r.content,symbol)
        archive=Path(os.getenv('SCREENING_DB',str(Path(__file__).resolve().parents[2]/'var'/'screening.db'))).parent/'financial-source'
        archive_bytes(archive,r.content,suffix='.xml')
        parsed.update(source=url,sha256=hashlib.sha256(r.content).hexdigest(),published_at=published.isoformat())
        documents.append(parsed)
    return documents


def fetch_statements(http,symbol,now):
    from .providers import TYPES,statements
    docs=filings(http,symbol,now)
    # Never splice standalone earnings with consolidated balance/cash flows.
    basis='Standalone' if any(d['basis']=='Standalone' for d in docs) else 'Consolidated'
    merged={}
    chosen=[d for d in docs if d['basis']==basis]
    for d in chosen:  # newest disclosed version of a period wins; originals stay identified
        for field,values in d['series'].items():
            for end,value in values.items():merged.setdefault(field,{}).setdefault(end,value)
    result=[]
    for field in TYPES:
        if field in merged:
            result.append({field:[dict(asOfDate=end,periodType='12M',currencyCode='INR',reportedValue=dict(raw=value))
                                  for end,value in sorted(merged[field].items())]})
    normalized=statements(dict(timeseries=dict(result=result)),now)
    normalized.update(statement_basis=basis,source_name='NSE official financial XBRL',
        reliability='Official NSE financial filings; explicit whole-company contexts and units',
        source_filings=[{k:d[k] for k in ('source','sha256','published_at')} for d in chosen],
        excluded_nonannual_periods=[p for d in chosen for p in d['nonannual_periods'] if p['days']>400])
    return normalized


def fetch_bank_metrics(http,symbol,now):
    docs=filings(http,symbol,now,bank_only=True)
    points=[(p,d) for d in docs if d['basis']=='Standalone' for p in d['bank']]
    if not points:raise ValueError('Official standalone bank ratios unavailable')
    end=max(p['period_end'] for p,d in points)
    if not 0<=(now.date()-fiscal_date(end)).days<=200:raise ValueError('Bank ratios are stale')
    values={};sources=[]
    for p,d in points:
        if p['period_end']!=end:continue
        if p['metric'] in values and values[p['metric']]!=p['value_pct']:raise ValueError('Conflicting bank ratios')
        values[p['metric']]=p['value_pct'];sources.append(d['source'])
    return dict(period_end=end,statement_basis='Standalone',**values,
        complete=all(k in values for k in ('gross_npa_pct','net_npa_pct','capital_adequacy_pct'))
                 and 0<=values['net_npa_pct']<=values['gross_npa_pct']<=100
                 and 0<values['capital_adequacy_pct']<=100,
        source_filings=sorted(set(sources)),contract_version='nse-bank-ratios-v1',
        note='Disclosed bank facts only; no bank entry policy is promoted')
