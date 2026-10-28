"""Read-only, dated product funnel. Observations never create trading approval.

Screen/confirmation/funding are snapshots with different populations; order
stages are immutable events in the caller's current epoch and IST day. They
must not be interpreted as a single conversion cohort or a profitability claim.
"""
from collections import Counter
from contextlib import closing
from datetime import datetime, time, timedelta, timezone
import json
from pathlib import Path
import sqlite3
from zoneinfo import ZoneInfo

IST = ZoneInfo('Asia/Kolkata')


def report(con, user_id, screen, quotes, *, journal_path, now=None):
    from .screening import automation, plans, tracking
    from .sleeves.base import Candidate
    from .sleeves.risk import RiskManager

    now = now or datetime.now(timezone.utc)
    start = datetime.combine(now.astimezone(IST).date(), time.min, IST)
    end = start + timedelta(days=1)
    stages = {key: dict(key=key, label=label, count=None, as_of=None,
                       status='unavailable', blockers=[]) for key, label in (
        ('screened', 'Screened'), ('confirmed', 'Confirmed'), ('fundable', 'Fundable'),
        ('ordered', 'Ordered'), ('filled', 'Filled'), ('exited', 'Exited'))}
    result = dict(version='paper-funnel-v1', generated_at=now.isoformat(),
                  session_date=start.date().isoformat(), timezone='Asia/Kolkata',
                  user_id=int(user_id), epoch=None, stages=list(stages.values()),
                  note='Screened: latest unique NSE stock evidence. Confirmed: latest active stock-model assessments today (shared strategy). '
                       'Fundable: those confirmed stocks sized independently for your account now, not approvals. '
                       'Ordered/filled/exited: distinct owned paper orders recorded today, including manual orders. '
                       'Different populations and times; these are not conversion rates. Research touches are never fills.')
    def block(stage, reason, count=None):
        stage['blockers'].append(dict(reason=str(reason), count=count))

    s = stages['screened']
    s['as_of'] = screen.get('generated_at')
    if screen.get('screened_count') is not None:
        s.update(count=screen['screened_count'], status=screen.get('status', 'unavailable'))
        s['price_asof'] = screen.get('price_asof')
        s['blockers'] = list(screen.get('rejections') or [])
    else:
        block(s, 'Stock screening receipt unavailable')

    # The NSE-only paper adapter's exit intents historically omit plan.market.
    # Resolve those sells within this adapter; never relabel an explicit US row.
    market_clause = "COALESCE(json_extract(i.payload,'$.plan.market'),CASE WHEN json_extract(i.payload,'$.plan.side')='SELL' THEN 'IN' END)='IN' "
    try:
        row = con.execute("SELECT started_at FROM user_book WHERE user_id=? AND market='IN'", (user_id,)).fetchone()
        if not row:
            raise ValueError('Owned paper book unavailable')
        epoch = result['epoch'] = row[0]
        # Date immutable fills by their event time, never by submission time.
        for key, side, event in (('ordered', 'BUY', None), ('filled', 'BUY', 'FILLED'), ('exited', 'SELL', 'FILLED')):
            stage = stages[key]
            if event:
                sql = ('SELECT COUNT(DISTINCT i.id),MAX(e.observed_at) FROM paper_order_intents i '
                       'JOIN paper_order_events e ON e.order_id=i.id WHERE e.kind=? ')
                args = [event]
                column = 'e.observed_at'
            else:
                sql = 'SELECT COUNT(DISTINCT i.id),MAX(i.submitted_at) FROM paper_order_intents i WHERE 1=1 '
                args = []
                column = 'i.submitted_at'
            sql += ("AND i.user_id=? AND i.epoch=? AND " + market_clause +
                    "AND COALESCE(json_extract(i.payload,'$.plan.side'),'BUY')=? "
                    f'AND julianday({column})>=julianday(?) AND julianday({column})<julianday(?) '
                    f'AND julianday({column})<=julianday(?)')
            count, at = con.execute(sql, args+[user_id, epoch, side, start.isoformat(), end.isoformat(), now.isoformat()]).fetchone()
            stage.update(count=count, as_of=at, status='observed')
        blockers = con.execute(
            "SELECT COALESCE(json_extract(i.payload,'$.plan.side'),'BUY'),s.reason,COUNT(*) "
            'FROM paper_order_intents i JOIN paper_order_state s ON s.order_id=i.id '
            "WHERE i.user_id=? AND i.epoch=? AND " + market_clause +
            "AND (s.status IN ('pending','filling') OR (s.status IN ('rejected','expired','cancelled') "
            'AND julianday(i.submitted_at)>=julianday(?))) AND julianday(i.submitted_at)<=julianday(?) '
            "GROUP BY 1,s.reason ORDER BY COUNT(*) DESC LIMIT 12", (user_id, epoch, start.isoformat(), now.isoformat())).fetchall()
        for side, reason, count in blockers:
            block(stages['exited' if side == 'SELL' else 'filled'], reason or 'Awaiting a later executable exchange quote', count)
    except (sqlite3.Error, ValueError, TypeError):
        for key in ('ordered', 'filled', 'exited'):
            stages[key].update(count=None, status='unavailable')
            block(stages[key], 'Owned order event history unavailable')
        if not result['epoch']:
            for key in ('confirmed', 'fundable'):
                block(stages[key], 'Owned paper book unavailable')
            return result

    try:
        house = con.execute("SELECT started_at FROM v2_book WHERE market='IN'").fetchone()
        if not house:
            raise ValueError('Strategy paper epoch unavailable')
        with closing(sqlite3.connect(Path(journal_path).resolve().as_uri()+'?mode=ro', uri=True, timeout=3)) as journal:
            rows = journal.execute(
                'SELECT p.id,p.symbol,p.payload,s.payload,(SELECT a.payload FROM assessment_events a '
                "WHERE a.publication_id=p.id AND a.kind='ASSESSMENT_OBSERVED' "
                'AND julianday(a.observed_at)<=julianday(?) '
                'ORDER BY julianday(a.observed_at) DESC LIMIT 1) FROM publications p '
                'LEFT JOIN states s ON s.publication_id=p.id '
                "WHERE p.user_id=? AND json_extract(p.payload,'$.automation_epoch')=? "
                "AND json_extract(p.payload,'$.automation_model_version')=? "
                'AND julianday(p.issued_at)<=julianday(?) ORDER BY p.id DESC',
                (now.isoformat(), automation.JOURNAL_OWNER, house[0], automation.MODEL_VERSION, now.isoformat())).fetchall()
        # A stock's overlapping versions are never independent opportunities.
        latest = {}
        for row in rows:
            latest.setdefault(row[1], row)
        confirmed, failures, missing, stamps = [], Counter(), 0, []
        for _, symbol, encoded, state, encoded_assessment in latest.values():
            if json.loads(state).get('status') in tracking.TERMINAL:
                continue
            assessment = json.loads(encoded_assessment) if encoded_assessment else {}
            try:
                at = datetime.fromisoformat(assessment['checked_at'])
                if not start <= at <= now:
                    raise ValueError('Not assessed today')
            except (KeyError, ValueError, TypeError):
                missing += 1
                continue
            stamps.append(at)
            checks = [c for c in assessment.get('checks', []) if c.get('code') != 'risk']
            required = {'model','zone_touch','completed_rebound','next_session','fresh_quote',
                        'entry_zone','later_fill','targets','regime','evidence','selection'}
            if required <= {c.get('code') for c in checks} and all(c.get('passed') is True for c in checks):
                confirmed.append((symbol, json.loads(encoded), at))
            else:
                reasons = {c.get('reason') or c.get('code') for c in checks if not c.get('passed')}
                if not reasons:
                    reasons = {'Incomplete confirmation predicates'}
                failures.update(reasons)
        stage = stages['confirmed']
        observed_at = max(stamps).isoformat() if stamps else None
        stage.update(count=None if missing else len(confirmed), observed_count=len(confirmed),
                     as_of=observed_at, status='partial' if missing else ('historical' if stamps and any((now-at).total_seconds()>120 for at in stamps) else 'observed'))
        stage['blockers'] = [dict(reason=r, count=n) for r, n in failures.most_common(8)]
        if missing:
            block(stage, 'Active stocks lack an assessment today', missing)
        if not latest:
            block(stage, 'No stock plans have reached the automation confirmation journal in this strategy epoch', 0)
        book, error = plans.account_state(con, int(user_id), quotes, now)
        funded, refused, stale = 0, Counter(), 0
        for symbol, plan, at in confirmed:
            if (now-at).total_seconds() > 120:
                stale += 1
                continue
            quote = quotes.get(symbol) or {}
            try:
                quote_at = datetime.fromisoformat(quote['ts'])
                if quote.get('source') != 'upstox-live' or not 0 <= (now-quote_at).total_seconds() <= 120:
                    raise ValueError('Fresh account quote required')
                if error:
                    raise ValueError(error)
                candidate = Candidate(symbol, 'quality_momentum', plan.get('score', 0)/100,
                                      float(quote['price']), plan['stop'], target=plan['t3'], max_hold_days=40)
                allocation = RiskManager().size(candidate, book)
                if allocation.ok:
                    funded += 1
                else:
                    refused[allocation.reason] += 1
            except (KeyError, ValueError, TypeError):
                stale += 1
        stage = stages['fundable']
        stage.update(count=None if missing or stale else funded, observed_count=funded,
                     as_of=now.isoformat(), status='partial' if missing or stale else 'observed')
        stage['blockers'] = [dict(reason=r, count=n) for r, n in refused.most_common(8)]
        if missing:
            block(stage, 'Confirmation coverage incomplete; funding count unavailable', missing)
        if stale:
            block(stage, 'Current confirmation and fresh account quote required; historical checks confer no approval', stale)
        if error:
            stage.update(count=None, status='unavailable')
            block(stage, error)
        if not confirmed and not missing:
            block(stage, 'No signal-confirmed stocks available for account funding checks', 0)
        if stages['ordered']['count'] == 0:
            block(stages['ordered'], 'No owned entry intent recorded today; research and funding checks do not submit orders')
        if stages['filled']['count'] == 0:
            block(stages['filled'], 'No buy-fill event recorded today; a submitted order is not a fill')
        if stages['exited']['count'] == 0:
            block(stages['exited'], 'No sell-fill event recorded today; hypothetical target/stop touches are excluded')
    except (sqlite3.Error, OSError, ValueError, KeyError, TypeError):
        for key in ('confirmed', 'fundable'):
            stages[key].update(count=None, status='unavailable')
            block(stages[key], 'Stock automation assessment journal unavailable')
    return result
