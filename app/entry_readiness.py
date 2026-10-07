"""Read-only account preflight. Passing checks is not a signal or certification.

Report the same dated contract/quote/book predicates used by entry. Never
create a book, approval, migration, risk observation or broker request here.
"""
from datetime import datetime, timezone
import math
import sqlite3

from . import books, catalogue_refresh, execution_contracts, instrument_catalog, paper_ledger
from .sleeves.config import PRODUCTION_SLEEVES, OBSERVATION_SLEEVES
from .sleeves.feeds import fresh_quotes
from .sleeves.risk import RiskManager


def report(book, catalogue, user_id, quotes, *, symbols, regime, regime_current,
           engine_observed_at=None, now=None):
    now = now or datetime.now(timezone.utc)
    if type(user_id) is not int or user_id < 1 or now.tzinfo is None:
        raise ValueError('Owned account and aware preflight time required')
    symbols = list(dict.fromkeys(symbols))
    if not symbols or len(symbols) > 20 or any(not isinstance(s, str) or not s or len(s) > 40 for s in symbols):
        raise ValueError('Select between one and twenty NSE cash symbols')
    checks = []
    def check(code, status, reason):
        checks.append(dict(code=code, status=status, reason=reason))
    state = None
    held = []
    summary = None
    try:
        exists = book.execute("SELECT 1 FROM user_book WHERE user_id=? AND market='IN'", (user_id,)).fetchone()
        if not exists:
            check('book', 'blocked', 'Your paper book has not been initialized')
        else:
            held = books.positions(book, user_id, 'IN')
            summary = books.stats(book, user_id, 'IN', quotes)
            summary['epoch'] = books.current_epoch(book, user_id, 'IN')
            complete = all(p['symbol'] in fresh_quotes(quotes, now) for p in held)
            summary['valuation_complete'] = complete
            if not complete:
                summary['equity'] = None
                summary['overall_pnl'] = None
                summary['unrealised'] = None
            try:
                ledger = paper_ledger.report(book, user_id, 'IN', summary['epoch'], summary['cash'])
                clean = not held and summary['trades'] == 0 and summary['cash'] == summary['budget']
                expected_inventory = sum(paper_ledger.minor(p['entry_price'] * p['shares']) for p in held)
                ledger_ok = (ledger['status'] == 'ok' and ledger['balanced'] and
                             ledger['balances_minor'].get('inventory', 0) == expected_inventory) or \
                            (ledger['status'] == 'uninitialised' and clean)
                check('paper_ledger', 'ready' if ledger_ok else 'blocked',
                      'Cash and inventory reconcile; a clean opening anchor is recorded atomically with first entry' if ledger_ok else
                      'Paper cash/inventory ledger is missing or diverges from the owned book')
            except sqlite3.Error:
                check('paper_ledger', 'blocked', 'Paper ledger schema unavailable')
            if book.in_transaction:
                # risk_state may observe a peak only in a caller's write txn.
                # A preflight must not participate in such a transaction.
                raise ValueError('Readiness requires a read-only transaction boundary')
            state, reason = books.risk_state(book, user_id, 'IN', quotes)
            halted, reason = RiskManager().halted(state) if state is not None else (True, reason)
            check('book_risk', 'blocked' if halted else 'ready', reason or 'Current account risk checks pass')
    except sqlite3.Error:
        check('book', 'blocked', 'Paper accounting schema unavailable; no guessed balance')
    check('regime', 'ready' if regime_current and regime in {'ON', 'NEUTRAL'} else 'blocked',
          ('Current regime is ' + str(regime)) if regime_current else 'Current dated regime evidence unavailable')
    rows = []
    sessions = []
    discovery = dict(status='missing', order_permission=False)
    if catalogue is not None:
        discovery = catalogue_refresh.report(catalogue, now=now)
    for symbol in symbols:
        row = dict(symbol=symbol, status='blocked', contract_status='blocked', quote_status='missing')
        try:
            if catalogue is None:
                raise ValueError('Reviewed daily instrument catalogue unavailable')
            spec, key = instrument_catalog.resolve(catalogue, symbol=symbol, venue='NSE', segment='NSE_EQ', now=now)
            quote = fresh_quotes({symbol: quotes.get(symbol, {})}, now).get(symbol)
            # Rule coverage can be assessed outside the session without
            # inventing a current quote. Read the rules separately first.
            _, rules = execution_contracts._latest(catalogue, 'rules', spec.id, now)
            if rules['banned'] or rules['corporate_action_pending'] or \
                    (now - execution_contracts._moment(rules['actions_reviewed_at'])).total_seconds() > 25 * 3600:
                raise ValueError('Restriction or corporate-action review blocks new orders')
            if not spec.tradable or spec.series == 'UNKNOWN' or spec.kind not in {'EQUITY', 'ETF'}:
                raise ValueError('Contract is not a reviewed tradable NSE cash instrument')
            row.update(instrument_id=spec.id, contract_status='ready')
            _, session = execution_contracts._latest(catalogue, 'session', rules['calendar'], now)
            opened = session['open'] and execution_contracts._moment(session['opens_at']) <= now < execution_contracts._moment(session['closes_at'])
            sessions.append(opened)
            if not opened:
                row.update(status='waiting', reason='Exchange session is closed', quote_status='after_hours')
            elif quote is None:
                row['reason'] = 'Selected instrument quote is missing or stale during the session'
            else:
                price = quote['price']
                execution_contracts.order_contract(catalogue, instrument_id=spec.id, quantity=rules['lot_size'], price=price, now=now)
                from .executable_quotes import top
                top(quote.get('execution'), 'BUY', key=key, symbol=symbol, now=now)
                row.update(status='ready', quote_status='fresh', reason='Current contract, session, reference quote and executable ask pass')
        except ValueError as exc:
            row['reason'] = str(exc)
        except (sqlite3.Error, KeyError, TypeError):
            row['reason'] = 'Reviewed daily identity, rules, restrictions, session or price checks unavailable'
        rows.append(row)
    coverage = sum(r['contract_status'] == 'ready' for r in rows)
    check('contracts', 'ready' if coverage == len(rows) else 'blocked',
          f'{coverage}/{len(rows)} selected instruments have reviewed daily rule coverage')
    if len(sessions) != len(rows):
        check('exchange_session', 'blocked', 'Sourced exchange session evidence is incomplete')
    elif all(sessions):
        check('exchange_session', 'ready', 'Selected exchange sessions are open')
    else:
        check('exchange_session', 'waiting', 'Exchange closed; after-hours stale quotes are expected')
    if sessions and all(sessions):
        check('quotes', 'ready' if all(r['quote_status'] == 'fresh' for r in rows) else 'blocked',
              'Fresh selected-instrument quotes required during the session')
    try:
        at = execution_contracts._moment(engine_observed_at)
        age = (now - at).total_seconds()
        engine_fresh = math.isfinite(age) and -5 <= age <= 180
    except ValueError:
        engine_fresh = False
    engine_required = len(sessions) == len(rows) and all(sessions)
    check('paper_worker', 'ready' if engine_fresh else 'blocked' if engine_required else 'waiting',
          'Recent production paper cycle observed' if engine_fresh else
          'No recent production paper cycle observed' if engine_required else 'Worker freshness is checked during an open session')
    blocked = [c['code'] for c in checks if c['status'] == 'blocked']
    waiting = [c['code'] for c in checks if c['status'] == 'waiting']
    manual_ready = not blocked and not waiting and all(r['status'] == 'ready' for r in rows)
    # Worker freshness matters to ongoing protection, including a manual fill.
    # No approval/signal/strategy result is manufactured by these checks.
    return dict(owner_user_id=user_id, checked_at=now.isoformat(), scope='selected NSE cash paper entry preflight',
                symbols=symbols, book=summary, regime=regime if regime_current else 'UNKNOWN',
                checks=checks, instruments=rows, blockers=blocked, waiting=waiting,
                paper=dict(status='ready_for_order_checks' if manual_ready else 'blocked' if blocked else 'waiting',
                           order_checks_required=True, production_sleeves=list(PRODUCTION_SLEEVES),
                           observation_sleeves=list(OBSERVATION_SLEEVES), signal_or_approval_required=True),
                discovery=discovery,
                live=dict(status='not_certified', reason='Exact-account release, native protection and reconciliation evidence required'),
                profitability_verified=False, commercial_release_certified=False)
