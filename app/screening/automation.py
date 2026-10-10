"""Selective individual-stock PAPER trial, not a validated investment model.

Reuses dated selection and next-session confirmation without changing the
registered research experiment. Publications/observations live in a separate
database; only the central paper exchange may fund a candidate or create fills.
"""
import json
import os
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING
from pathlib import Path

from . import confirmation, plans, selection, store, tracking
from ..sleeves.base import Candidate, Sleeve
from ..sleeves.risk import RiskManager

MODEL_VERSION = 'selective-paper-v2'
# Namespace within the dedicated automation journal, NOT an account user ID.
JOURNAL_OWNER = 1


def default_path():
    from .. import v2_live
    path = Path(os.getenv('EQUITY_AUTOMATION_DB', str(Path(v2_live.V2_DB).parent / 'equity_automation.db'))).resolve()
    research = Path(tracking.default_path(v2_live.MAIN_DB)).resolve()
    reserved = {research, Path(v2_live.V2_DB).resolve(), Path(v2_live.MAIN_DB).resolve(), screen_path()}
    if path in reserved or any(path.exists() and other.exists() and path.samefile(other) for other in reserved):
        raise ValueError('Stock automation requires its own journal; research and books must remain separate')
    return str(path)


def screen_path():
    from .. import v2_live
    return Path(os.getenv('SCREENING_DB', str(Path(v2_live.MAIN_DB).parent / 'screening.db'))).resolve()


def current_screen(now):
    return store.current_report(str(screen_path()), now)


def observe(quotes, now):
    """Fast-loop observation is independent of UI visits and pending orders."""
    path = default_path()
    if Path(path).exists():
        return tracking.observe(path, quotes, now.isoformat())
    return 0


def symbols():
    path = default_path()
    return tracking.symbols(path) if Path(path).exists() else []


def _rows(con, epoch):
    return con.execute('SELECT p.id,p.fingerprint,p.payload,s.payload,a.payload '
        'FROM publications p JOIN states s ON s.publication_id=p.id '
        'LEFT JOIN assessments a ON a.publication_id=p.id '
        "WHERE p.user_id=? AND json_extract(p.payload,'$.automation_epoch')=? ORDER BY p.id",
        (JOURNAL_OWNER, epoch)).fetchall()


def _completed_bars(frame, asof, now):
    bars = []
    if frame is not None:
        for index, bar in frame.iterrows():
            session = str(bar.get('date', index))[:10]
            if session <= str(asof)[:10]:
                bars.append(dict(session=session, known_at=now.isoformat(),
                    **{k: float(bar[k]) for k in ('open', 'high', 'low', 'close', 'volume')}))
    return sorted(bars, key=lambda b: b['session'])[-21:]


def _signal_checks(assessment):
    # Account capacity is checked by the allocator at reservation AND fill.
    # The research protocol still includes its risk check, unchanged; only this
    # separate automation journal exposes the signal independently of the house.
    return [c for c in assessment.get('checks', []) if c['code'] != 'risk']


def _assess(plan, state, bars, quote, screen, current, regime, asof, now, previous, *,
            regime_fresh, risk_ok=False, risk_reason='Account risk is checked at reservation and fill'):
    reason = selection.reject_reason(current, screen, now)
    context = dict(regime=regime, regime_fresh=regime_fresh,
        price_asof=str(asof)[:10],
        evidence_ok=bool(current) and not current.get('flags') and not screen.get('stale') and not screen.get('price_stale'),
        news_checked_at=(current.get('news') or {}).get('checked_at'),
        selection_ok=not reason, selection_reason=reason,
        risk_ok=risk_ok, risk_reason=risk_reason)
    result = confirmation.assess(plan, state, bars, quote, context, now, previous)
    checks = _signal_checks(result)
    result.update(context=context, quote=quote, signal_eligible=bool(checks) and all(c['passed'] for c in checks))
    return result


def refresh_pending(paper, quotes, regime_view, now):
    """Recheck existing stock signals without publishing, sizing or a full screen.

    Pending orders and open house origins need current signal evidence even
    while their own reservation/fill occupies the only house stock slot. Read
    current source bars and predicates; never renew a stored positive decision.
    """
    epoch_row = paper.execute("SELECT started_at FROM v2_book WHERE market='IN'").fetchone()
    if not epoch_row:
        return 0
    epoch = epoch_row[0]
    rows = paper.execute("SELECT i.payload FROM paper_order_intents i "
        "JOIN paper_order_state s ON s.order_id=i.id "
        "WHERE json_extract(i.payload,'$.plan.sleeve')='quality_momentum' "
        "AND COALESCE(json_extract(i.payload,'$.plan.side'),'BUY')='BUY' "
        "AND (s.status='pending' OR (i.user_id=0 AND s.status='filled' AND EXISTS "
        "(SELECT 1 FROM v2_positions p WHERE p.id=json_extract(s.result,'$.position_id') AND p.market='IN')))").fetchall()
    identities = set()
    for (encoded,) in rows:
        metadata = json.loads(encoded)['plan'].get('selective_paper') or {}
        if metadata.get('model_version') == MODEL_VERSION and metadata.get('epoch') == epoch:
            identities.add((metadata.get('publication_id'), metadata.get('fingerprint')))
    if not identities:
        return 0

    from .. import v2_live, corpactions
    from ..sleeves.reference import snapshot
    import pandas as pd
    screen = current_screen(now)
    eligible, _ = snapshot(now)
    counts = Counter(r.get('symbol') for r in screen.get('equities', []))
    evidence = {r['symbol']: r for r in screen.get('equities', [])
                if counts[r.get('symbol')] == 1 and r.get('symbol') in (eligible or set())}
    asof = str(regime_view.get('asof') or '')[:10]
    today = now.astimezone(tracking.IST).date()
    try:
        day = datetime.fromisoformat(asof).date()
        regime_fresh = (day < today and confirmation.next_session(day) == today
                        and regime_view.get('cycle_date') == today.isoformat()
                        and screen.get('price_asof') == asof)
    except ValueError:
        regime_fresh = False
    market = v2_live._ro(v2_live.MAIN_DB)
    con = tracking.connect(default_path())
    changed = 0
    try:
        for pid, fingerprint, encoded, stored, prev in _rows(con, epoch):
            if (pid, fingerprint) not in identities:
                continue
            plan, state = json.loads(encoded), json.loads(stored)
            if state.get('status') in tracking.TERMINAL:
                continue
            symbol = plan['symbol']
            daily = market.execute("SELECT substr(ts,1,10),open,high,low,close,volume FROM candles "
                "WHERE symbol=? AND source='upstox-live:day' AND substr(ts,1,10)<=? ORDER BY ts DESC LIMIT 21",
                (symbol, asof)).fetchall()
            frame = pd.DataFrame(daily, columns=['date', 'open', 'high', 'low', 'close', 'volume']).sort_values('date')
            frame, _ = corpactions.clean(frame)
            bars = _completed_bars(frame, asof, now)
            result = _assess(plan, state, bars, quotes.get(symbol, {}), screen, evidence.get(symbol, {}),
                regime_view.get('regime'), asof, now, json.loads(prev) if prev else {},
                regime_fresh=regime_fresh and bool(bars) and bars[-1]['session'] == asof)
            confirmation.record_assessment(con, pid, result, json.loads(prev) if prev else {})
            changed += 1
        con.commit()
    finally:
        con.close()
        market.close()
    return changed


class SelectivePaperSleeve(Sleeve):
    name = 'quality_momentum'
    allowed_regimes = ('ON', 'NEUTRAL')

    def propose(self, ctx):
        now = ctx.observed_at or datetime.now(timezone.utc)
        dec = self._decision(ctx.regime.state, self.may_run(ctx.regime.state))
        if not ctx.paper_epoch or ctx.book is None or not ctx.equity_screen:
            dec.active = False
            dec.note = 'Stock automation requires current evidence and an identified paper epoch'
            return dec
        screen = ctx.equity_screen
        if screen.get('price_asof') != str(ctx.asof)[:10]:
            dec.active = False
            dec.note = 'Stock evidence and completed-session regime dates do not match'
            return dec
        # A duplicate evidence identity is ambiguous, even if one version passes.
        counts = Counter(r.get('symbol') for r in screen.get('equities', []))
        evidence = {r['symbol']: r for r in screen.get('equities', [])
                    if counts[r.get('symbol')] == 1 and r.get('symbol') in (ctx.eligible_symbols or set())}
        screen = dict(screen, equities=list(evidence.values()))
        path = default_path()
        con = tracking.connect(path)
        try:
            active = {json.loads(r[2])['symbol'] for r in _rows(con, ctx.paper_epoch)
                      if json.loads(r[3]).get('status') not in tracking.TERMINAL}
        finally:
            con.close()
        shortlist = plans.shortlist(screen, ctx.book, ctx.live, now) if dec.active else dict(ideas=[], rejected=[])
        publications = [dict(p, automation_epoch=ctx.paper_epoch, automation_model_version=MODEL_VERSION)
                        for p in shortlist['ideas'] if p['symbol'] not in active]
        if publications:
            tracking.publish(path, JOURNAL_OWNER, publications, now=now)
        # Submission-time quotes cannot retrospectively touch a new plan.
        tracking.observe(path, ctx.live, now.isoformat())
        for r in shortlist['rejected']:
            dec.reject(r['symbol'], r['reason'])
        con = tracking.connect(path)
        try:
            rows = _rows(con, ctx.paper_epoch)
            eligible = {}
            for pid, fingerprint, encoded, stored, prev in rows:
                plan, state = json.loads(encoded), json.loads(stored)
                if state.get('status') in tracking.TERMINAL:
                    continue
                symbol = plan['symbol']; quote = ctx.live.get(symbol, {})
                current = evidence.get(symbol, {})
                bars = _completed_bars(ctx.tails.get(symbol), ctx.asof, now)
                candidate = Candidate(symbol, self.name, plan['score']/100,
                    float(quote.get('price') or plan['entry_high']), plan['stop'], target=plan['t3'], max_hold_days=40,
                    why=dict(selective_paper=dict(model_version=MODEL_VERSION, publication_id=pid,
                        fingerprint=fingerprint, epoch=ctx.paper_epoch)))
                allocation = RiskManager(ctx.settings).size(candidate, ctx.book)
                previous = json.loads(prev) if prev else {}
                result = _assess(plan, state, bars, quote, screen, current, ctx.regime.state, ctx.asof, now, previous,
                    regime_fresh=screen.get('price_asof')==str(ctx.asof)[:10],
                    risk_ok=allocation.ok, risk_reason=allocation.reason)
                confirmation.record_assessment(con, pid, result, previous)
                if result['eligible'] and symbol not in eligible:
                    eligible[symbol] = candidate
                else:
                    dec.reject(symbol, result['reason'])
            con.commit()
            dec.candidates = sorted(eligible.values(), key=lambda c: (-c.score, c.symbol))[:3]
            dec.diagnostics = dict(model_version=MODEL_VERSION, validation='paper trial; returns unvalidated',
                publications=len(rows), new_publications=len(publications), confirmed_candidates=len(dec.candidates))
            dec.note = (f'{len(dec.candidates)} confirmed stock plan(s); account allocation and later exchange fill required'
                        if dec.candidates else 'No confirmed stock entry: waiting for observed pullback and next-session rebound checks')
            if not dec.active:
                dec.note = f'Regime {ctx.regime.state} blocks new stock longs; existing plans remain observed'
            return dec
        finally:
            con.close()


def binding(metadata, symbol, stop, target, epoch, catalogue, now):
    """Bind an order to original levels AND immutable positive decision evidence.

    Called at reservation and again before fill. A forged candidate, legacy
    stock signal, changed epoch or expired/news-invalidated evidence fails closed.
    """
    if not isinstance(metadata, dict) or metadata.get('model_version') != MODEL_VERSION or metadata.get('epoch') != epoch:
        raise ValueError('An identified selective paper trial publication is required')
    path = default_path()
    con = sqlite3.connect(Path(path).as_uri()+'?mode=ro', uri=True, timeout=5)
    try:
        row = con.execute('SELECT p.fingerprint,p.payload,a.payload,s.payload FROM publications p '
            'JOIN assessments a ON a.publication_id=p.id JOIN states s ON s.publication_id=p.id WHERE p.id=? AND p.user_id=?',
            (metadata.get('publication_id'), JOURNAL_OWNER)).fetchone()
        if not row:
            raise ValueError('Stock publication or confirmation is unavailable')
        plan, assessment = json.loads(row[1]), json.loads(row[2])
        state = json.loads(row[3])
        if state.get('status') in tracking.TERMINAL or not state.get('entry_at'):
            raise ValueError('Stock publication was invalidated or no longer has an observed entry touch')
        if row[0] != metadata.get('fingerprint') or tracking.fingerprint(plan) != row[0] or \
                plan.get('automation_epoch') != epoch or plan.get('automation_model_version') != MODEL_VERSION or \
                plan.get('model_version') != selection.MODEL_VERSION or plan['symbol'] != symbol:
            raise ValueError('Stock publication identity changed')
        checked = store.timestamp(assessment['checked_at'])
        event = con.execute("SELECT payload FROM assessment_events WHERE publication_id=? AND kind='ASSESSMENT_OBSERVED' AND observed_at=?",
            (metadata['publication_id'], assessment['checked_at'])).fetchone()
        checks = _signal_checks(assessment)
        if not event or json.loads(event[0]) != assessment or \
                not checks or not all(c['passed'] for c in checks) or \
                not 0 <= (now-checked).total_seconds() <= 120:
            raise ValueError('A fresh immutable confirmed stock decision is required')
    finally:
        con.close()
    screen = current_screen(now)
    rows = [r for r in screen.get('equities', []) if r.get('symbol') == symbol]
    if len(rows) != 1:
        raise ValueError('Current stock evidence identity unavailable or ambiguous')
    reason = selection.reject_reason(rows[0], screen, now)
    news = rows[0].get('news') or {}
    if screen.get('price_asof') != assessment.get('context', {}).get('price_asof'):
        raise ValueError('Stock evidence date changed since confirmation')
    if reason or not 0 <= (now-store.timestamp(news.get('checked_at'))).total_seconds() <= 7200:
        raise ValueError(reason or 'Fresh official news checks are required before stock fill')
    from ..instrument_catalog import resolve
    from .. import execution_contracts
    spec, _ = resolve(catalogue, symbol=symbol, venue='NSE', segment='NSE_EQ', now=now)
    _, rules = execution_contracts._latest(catalogue, 'rules', spec.id, now)
    tick = Decimal(str(rules['tick_size']))
    normalized = [float((Decimal(str(plan[k]))/tick).to_integral_value(rounding=ROUND_CEILING)*tick) for k in ('stop', 't3')]
    if [stop, target] != normalized:
        raise ValueError('Stock stop or target differs from the frozen publication')
    return plan


def check_economics(plan, quantity):
    """A smaller allocation must not inherit larger-ticket fee assumptions."""
    scaled = dict(plan, qty=quantity)
    if any(tracking.net(scaled, plan['entry_high'], plan[k]) <= 0 for k in ('t1', 't2', 't3')):
        raise ValueError('Stock targets do not clear frozen costs at this account quantity')


def source_fill(con, position_id, epoch):
    """Read actual house fill provenance, never trust an outbox claim alone."""
    row = con.execute("SELECT i.payload FROM paper_order_intents i JOIN paper_order_state s ON s.order_id=i.id "
        "WHERE i.user_id=0 AND i.epoch=? AND s.status='filled' AND json_extract(s.result,'$.position_id')=?",
        (epoch, position_id)).fetchone()
    if not row:
        raise ValueError('Selective stock mirror requires a confirmed house exchange fill')
    plan = json.loads(row[0])['plan']
    if plan.get('model_version') != MODEL_VERSION or not plan.get('selective_paper'):
        raise ValueError('Legacy stock fill cannot authorise the selective paper trial')
    return plan


def report(epoch):
    """Small read-only UI status; observations are never labelled fills."""
    try:
        con = sqlite3.connect(Path(default_path()).as_uri()+'?mode=ro', uri=True, timeout=5)
        try:
            rows = _rows(con, epoch)
        finally:
            con.close()
        return dict(status='ok', model_version=MODEL_VERSION, validation='unvalidated paper trial',
                    published=len(rows), waiting=sum(json.loads(r[3]).get('status') not in tracking.TERMINAL for r in rows),
                    note='Publication and confirmation counts are observations, not orders or fills.')
    except (sqlite3.Error, OSError, ValueError):
        return dict(status='unavailable', model_version=MODEL_VERSION, published=None, waiting=None,
                    note='No readable stock automation journal yet; counts unavailable')
