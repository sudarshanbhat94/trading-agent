#!/usr/bin/env python3
"""Bounded read-only lifecycle observations; no refresh, broker I/O or execution.

Dates/paths are supplied by the operator. This tool never creates a database,
registers a protocol, advances a checkpoint, or changes a production decision.
"""
from collections import Counter
from contextlib import closing
from datetime import datetime, timedelta, timezone
import argparse
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))


def observe(paper_path, journal_path, market_path, user_id=2, since=None, now=None):
    from app import books, paper_exchange, paper_ledger
    from app.market_regions import market_session_for_region
    from app.screening import automation, tracking
    from app.screening.store import timestamp
    from app.sleeves.feeds import fresh_quotes

    now = now or datetime.now(timezone.utc)
    since = timestamp(since) if since else now-timedelta(days=1)
    if since > now:
        raise ValueError('Observation start cannot be in the future')
    def ro(path):
        con = sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True,timeout=5)
        con.execute('BEGIN')
        return con
    result = dict(version='paper-lifecycle-observer-v1',observed_at=now.isoformat(),
                  since=since.isoformat(),user_id=int(user_id),mode='paper',
                  market_open=bool(market_session_for_region('IN',now)['is_open']),
                  status='ok',alerts=[],journal=dict(status='unavailable'))
    with closing(ro(paper_path)) as con:
        if not con.execute("SELECT 1 FROM user_book WHERE user_id=? AND market='IN'",(user_id,)).fetchone():
            raise ValueError('Owned paper book unavailable')
        epoch = books.current_epoch(con,user_id,'IN')
        result['epoch'] = epoch
        positions = books.positions(con,user_id,'IN')
        symbols = {p['symbol'] for p in positions}
        house = con.execute("SELECT started_at FROM v2_book WHERE market='IN'").fetchone()
        if not house:
            raise ValueError('Strategy epoch unavailable')
        try:
            with closing(ro(journal_path)) as journal:
                # Newest stock publication supersedes overlapping versions for this view.
                rows = journal.execute('SELECT p.symbol,s.payload,(SELECT a.payload FROM assessment_events a '
                    "WHERE a.publication_id=p.id AND a.kind='ASSESSMENT_OBSERVED' "
                    'AND julianday(a.observed_at)<=julianday(?) ORDER BY julianday(a.observed_at) DESC LIMIT 1) '
                    'FROM publications p LEFT JOIN states s ON s.publication_id=p.id '
                    "WHERE p.user_id=? AND json_extract(p.payload,'$.automation_epoch')=? "
                    "AND json_extract(p.payload,'$.automation_model_version')=? AND julianday(p.issued_at)<=julianday(?) "
                    'ORDER BY p.id DESC',(now.isoformat(),automation.JOURNAL_OWNER,house[0],automation.MODEL_VERSION,now.isoformat())).fetchall()
                seen, active, current, confirmed, historical, missing, reasons, stamps = set(), 0, 0, [], 0, 0, Counter(), []
                for symbol,state,encoded in rows:
                    if symbol in seen:continue
                    seen.add(symbol)
                    if json.loads(state)['status'] in tracking.TERMINAL:continue
                    active+=1;symbols.add(symbol)
                    if not encoded:missing+=1;continue
                    assessment=json.loads(encoded);at=timestamp(assessment['checked_at']);stamps.append(at)
                    recent=0 <= (now-at).total_seconds() <= 120
                    current+=int(recent);historical+=int(not recent)
                    checks=[c for c in assessment.get('checks',[]) if c.get('code')!='risk']
                    # Signal eligibility is written by the production automation assessor.
                    if recent and assessment.get('signal_eligible') is True and checks and all(c.get('passed') is True for c in checks):
                        confirmed.append(symbol)
                    reasons.update({c.get('code','unknown') for c in checks if not c.get('passed')})
                observed = journal.execute('SELECT COUNT(DISTINCT p.symbol) FROM assessment_events a JOIN publications p ON p.id=a.publication_id '
                    "WHERE p.user_id=? AND json_extract(p.payload,'$.automation_epoch')=? "
                    "AND json_extract(p.payload,'$.automation_model_version')=? AND a.kind='ASSESSMENT_OBSERVED' "
                    "AND json_extract(a.payload,'$.signal_eligible')=1 AND julianday(a.observed_at)>julianday(?) "
                    'AND julianday(a.observed_at)<=julianday(?)',
                    (automation.JOURNAL_OWNER,house[0],automation.MODEL_VERSION,since.isoformat(),now.isoformat())).fetchone()[0]
                result['journal']=dict(status='observed',model_version=automation.MODEL_VERSION,
                    active_stocks=active,current_assessments=current,historical_assessments=historical,
                    missing_assessments=missing,current_confirmed_symbols=sorted(confirmed)[:10],
                    last_assessment_at=max(stamps).isoformat() if stamps else None,
                    confirmed_stocks_observed_in_window=observed,
                    blocking_predicates=dict(reasons.most_common(8)))
        except (sqlite3.Error,OSError,ValueError,TypeError,KeyError):
            result['status']='partial';result['alerts'].append('Automation journal unavailable; signal coverage unknown')
        quotes={}
        if symbols:
            try:
                with closing(ro(market_path)) as market:
                    data=market.execute("SELECT symbol,price,ts FROM latest_quotes WHERE source='upstox-live' AND symbol IN ("+
                        ','.join('?' for _ in symbols)+')',sorted(symbols)).fetchall()
                    quotes={symbol:dict(price=price,ts=at,source='upstox-live') for symbol,price,at in data}
            except (sqlite3.Error,OSError):
                result['status']='partial'
        marks=fresh_quotes(quotes,now)
        stats=books.stats(con,user_id,'IN',marks)
        missing_marks=[p['symbol'] for p in positions if p['symbol'] not in marks]
        result['book']={k:stats[k] for k in ('budget','cash','equity','realised','positions','pending_orders','reserved_cash')}
        result['book']['valuation_complete']=not missing_marks
        if missing_marks:result['book']['equity']=None
        result['quote_coverage']=dict(watched=len(symbols),fresh=len(set(marks)&symbols),
                                     stale_or_missing=len(symbols-set(marks)))
        ledger=paper_ledger.report(con,user_id,'IN',epoch,stats['cash'])
        inventory=sum(paper_ledger.minor(p['shares']*p['entry_price']) for p in positions)
        gap=ledger['balances_minor'].get('inventory',0)-inventory if ledger['events'] else None
        result['reconciliation']=dict(status=ledger['status'],cash_difference_minor=ledger['cash_difference_minor'],
            inventory_difference_minor=gap,balanced=ledger['balanced'],ledger_events=ledger['events'])
        if ledger['status']=='mismatch' or gap not in (None,0) or not ledger['balanced']:
            result['alerts'].append('Paper cash/inventory ledger divergence')
        if not ledger['events'] and (positions or stats['trades']):
            result['alerts'].append('Active paper activity has no initialized accounting ledger')
        result['unprotected_owned_positions']=con.execute('SELECT COUNT(*) FROM user_positions p '
            "WHERE p.user_id=? AND p.market='IN' AND p.book_epoch=? AND (p.stop IS NULL OR p.stop<=0 OR "
            "NOT EXISTS(SELECT 1 FROM entry_contract_records c WHERE c.scope='personal' AND c.user_id=p.user_id AND c.position_id=p.id))",
            (user_id,epoch)).fetchone()[0]
        if result['unprotected_owned_positions']:result['alerts'].append('Owned paper position lacks sourced protection evidence')
        issue=paper_exchange.integrity_reason(con,allow_filling=False)
        if issue:result['alerts'].append(issue)
        market_clause=" AND COALESCE(json_extract(i.payload,'$.plan.market'),CASE WHEN json_extract(i.payload,'$.plan.side')='SELL' THEN 'IN' END)='IN' "
        events=con.execute('SELECT COALESCE(json_extract(i.payload,\'$.plan.side\'),\'BUY\'),COUNT(DISTINCT i.id),MAX(e.observed_at) '
            'FROM paper_order_events e JOIN paper_order_intents i ON i.id=e.order_id '
            "WHERE i.user_id=? AND i.epoch=? AND e.kind='FILLED' AND julianday(e.observed_at)>julianday(?) "
            'AND julianday(e.observed_at)<=julianday(?) '+market_clause+' GROUP BY 1',
            (user_id,epoch,since.isoformat(),now.isoformat())).fetchall()
        result['fills_in_window']={side:dict(orders=count,last_at=at) for side,count,at in events}
        result['order_states']=dict(con.execute('SELECT s.status,COUNT(*) FROM paper_order_intents i '
            'JOIN paper_order_state s ON s.order_id=i.id WHERE i.user_id=? AND i.epoch=? '+market_clause+' GROUP BY s.status',(user_id,epoch)))
        result['pending_blockers']=[dict(status=s,reason=r,count=n) for s,r,n in con.execute(
            'SELECT s.status,s.reason,COUNT(*) FROM paper_order_intents i JOIN paper_order_state s ON s.order_id=i.id '
            "WHERE i.user_id=? AND i.epoch=? AND s.status IN ('pending','filling') "+market_clause+" GROUP BY s.status,s.reason LIMIT 8",(user_id,epoch))]
        result['terminal_order_blockers']=[dict(status=s,reason=r,count=n) for s,r,n in con.execute(
            'SELECT s.status,s.reason,COUNT(*) FROM paper_order_intents i JOIN paper_order_state s ON s.order_id=i.id '
            "WHERE i.user_id=? AND i.epoch=? AND s.status IN ('rejected','expired','cancelled') "+market_clause+
            'AND EXISTS(SELECT 1 FROM paper_order_events e WHERE e.order_id=i.id '
            "AND e.kind IN ('REJECTED','EXPIRED','CANCELLED') AND julianday(e.observed_at)>julianday(?) "
            'AND julianday(e.observed_at)<=julianday(?)) GROUP BY s.status,s.reason LIMIT 8',
            (user_id,epoch,since.isoformat(),now.isoformat()))]
        lease=con.execute("SELECT expires FROM worker_leases WHERE name='paper'").fetchone()
        result['worker']='running' if lease and lease[0]>now.timestamp() else 'not observed'
        if result['market_open']:
            if result['worker']!='running':result['alerts'].append('Paper worker lease missing during market hours')
            if symbols-set(marks):result['alerts'].append('Watched quote coverage incomplete during market hours')
        result['note']='Current eligibility is not an order/fill. Historical after-hours checks are expected. An uninitialised empty ledger is not a reconciliation failure. Owned fill counts exclude research touches; no profitability conclusion.'
    if result['alerts'] and result['status']=='ok':result['status']='attention'
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--paper-db',required=True);parser.add_argument('--journal-db',required=True)
    parser.add_argument('--market-db',required=True);parser.add_argument('--user-id',type=int,default=2)
    parser.add_argument('--since')
    args=parser.parse_args()
    print(json.dumps(observe(args.paper_db,args.journal_db,args.market_db,args.user_id,args.since),sort_keys=True))
