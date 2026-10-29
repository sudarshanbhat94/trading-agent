"""Evaluate recorded release evidence; tests and idle sessions cannot certify live.

This initial evaluator is for the NSE paper pilot's recorded release evidence.
Additional market calendars and route certifications remain separate work.
It does not grant runtime authority, register a strategy or place orders.
Missing evidence is a failed gate. Evidence must be reviewed externally before
being used as a release record; this evaluator is not a legal certification.
"""
import re
from datetime import date,datetime,timezone
from .market_regions import INDIA_TRADING_HOLIDAYS


REQUIRED = ('risk_and_ownership','order_lifecycle','inventory_reconciliation',
            'accounting','data_and_instruments','security','backup_restore','browser_mobile',
            'independent_strategy_validation','native_protection','broker_permission',
            'legal_commercial_scope','data_rights')


def _review(record,source_commit):
    return isinstance(record,dict) and record.get('source_commit')==source_commit and all(
        isinstance(record.get(key),str) and record[key].strip() for key in ('evidence_reference','reviewed_by'))


def evaluate(evidence, source_commit, now=None):
    if not re.fullmatch(r'[a-f0-9]{40}',source_commit):
        raise ValueError('A complete source commit is required')
    failures=[]
    if not isinstance(evidence,dict):evidence={}
    if evidence.get('source_commit')!=source_commit:
        failures.append('evidence source commit does not match release')
    for key in ('known_blockers','known_critical','unexplained_reconciliation_mismatches'):
        value=evidence.get(key)
        if isinstance(value,bool) or not isinstance(value,int) or value!=0:
            failures.append(key+' must be recorded as zero')
    for gate in REQUIRED:
        record=evidence.get('checks',{}).get(gate,{}) if isinstance(evidence.get('checks'),dict) else {}
        if not _review(record,source_commit) or record.get('passed') is not True:
            failures.append(gate+': reviewed evidence unavailable')
    sessions=evidence.get('paper_sessions',[])
    today=(now or datetime.now(timezone.utc)).date()
    valid_sessions=False
    try:
        dates=[date.fromisoformat(s) for s in sessions] if isinstance(sessions,list) else []
        valid_sessions=(len(set(dates))>=30 and all(d<=today and d.weekday()<5 and d.isoformat() not in INDIA_TRADING_HOLIDAYS for d in dates))
    except (ValueError,TypeError):pass
    if not valid_sessions:
        failures.append('at least 30 distinct verified paper trading sessions required')
    session_records=evidence.get('paper_session_evidence',{})
    event_fields=('confirmed_entries','confirmed_exits','protection_observations','owned_position_observations')
    if valid_sessions:
        for session in sorted(set(sessions)):
            record=session_records.get(session) if isinstance(session_records,dict) else None
            counts=record.get('events') if isinstance(record,dict) else None
            if not _review(record,source_commit) or not isinstance(counts,dict) or any(
                type(counts.get(key,0)) is not int or counts.get(key,0)<0 for key in event_fields) or \
                    sum(counts.get(key,0) for key in event_fields)<1:
                failures.append(session+': dated relevant lifecycle evidence unavailable; idle sessions do not qualify')
    for key in ('confirmed_entries','confirmed_exits','protection_observations'):
        value=evidence.get('lifecycle_counts',{}).get(key) if isinstance(evidence.get('lifecycle_counts'),dict) else None
        if isinstance(value,bool) or not isinstance(value,int) or value<1:
            failures.append(key+': idle sessions are not lifecycle proof')
    scopes=evidence.get('certified_scopes',[])
    if not isinstance(scopes,list) or not scopes or any(not isinstance(s,str) or not re.fullmatch(
            r'(paper|upstox|angelone):NSE:NSE_EQ:(D|I):[a-z][a-z0-9_-]*:[1-9][0-9]*',s) for s in scopes):
        failures.append('explicit broker/asset/model/account/product scopes required')
    return dict(go=not failures,decision='GO' if not failures else 'NO-GO',failures=failures,
                source_commit=source_commit,certified_scopes=scopes if not failures else [],
                note='Recorded evidence gate; not an order permission or a profitability guarantee')
