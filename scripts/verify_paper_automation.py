#!/usr/bin/env python3
"""Isolated production-path proof; synthetic signal, never alpha/live certification.

Run from a development checkout with test dependencies installed. All books,
source fixtures and publications are temporary. No production DB argument exists.
Broker and network calls are forbidden. Only sources, clock, auth membership and
notification sinks are replaced; signal checks, allocator, order/fill/exit,
outbox delivery and accounting execute their production implementations.
"""
from contextlib import ExitStack, closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import logging
import sqlite3
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def scenario(exit_kind):
    from app import books, broker, entry_contracts, execution_outbox, paper_exchange
    from app import paper_ledger, personal_performance, v2_live, v2_web
    from app.costs import round_trip
    from app.screening import automation
    from app.sleeves.engine import SleeveEngine
    from tests import test_selective_paper_automation as fixtures

    case = fixtures.SelectivePaperAutomationTest()
    case.setUp()
    try:
        ctx, _ = case.confirmed()  # Real publication, observed touch, completed rebound, later quote.
        class Clock(datetime):
            current = ctx.observed_at + timedelta(seconds=1)
            @classmethod
            def now(cls, tz=None):
                return cls.current.astimezone(tz) if tz else cls.current.replace(tzinfo=None)
        source = Path(case.f.tmp.name)/'sources.db'
        with closing(sqlite3.connect(source)) as con, con:
            con.execute('CREATE TABLE candles(symbol,ts,open,high,low,close,volume,source)')
            con.execute('CREATE TABLE agent_state(key,value)')
            con.execute('CREATE TABLE index_bars(symbol,ts,open,high,low,close)')
            con.execute('CREATE TABLE latest_quotes(symbol,price,ts)')
            con.execute('CREATE TABLE nfo_quotes(underlying,strike,option_type,price,lot_size,expiry,ts)')
            for day, bar in case.rebound().iterrows():
                con.execute('INSERT INTO candles VALUES(?,?,?,?,?,?,?,?)', ('TEST', day.isoformat(), *list(bar), 'upstox-live:day'))
        engine = SleeveEngine()
        engine.gate.view = lambda *args: case.regime
        current_price = 95.
        bad_depth = False
        missing_depth = False
        stale_depth = False
        timeline = []
        quote_at = Clock.current
        errors = []
        class Errors(logging.Handler):
            def emit(self, record):
                if record.levelno >= logging.ERROR:
                    errors.append(record.getMessage())
        def advance(seconds=1, refresh=True):
            nonlocal quote_at
            Clock.current += timedelta(seconds=seconds)
            if refresh:
                quote_at = Clock.current
        def quotes(*args, **kwargs):
            data = case.quotes(quote_at, current_price)
            data['TEST'].update(high=current_price, low=current_price)
            if missing_depth:
                data['TEST'].pop('execution')
            if stale_depth:
                data['TEST']['execution'] = case.f.snapshot(Clock.current-timedelta(seconds=31),current_price)
            if bad_depth:
                data['TEST']['execution'] = dict(data['TEST']['execution'], instrument_key='NSE_EQ|WRONG')
            return data
        def reopen():
            case.con.close()
            case.con = case.f.con = sqlite3.connect(case.f.path)
        def record(stage):
            timeline.append(dict(stage=stage, at=Clock.current.isoformat(),
                house_positions=case.con.execute('SELECT COUNT(*) FROM v2_positions').fetchone()[0],
                owned_positions=len(books.positions(case.con,2)), cash=books.cash(case.con,2)))
        with ExitStack() as stack:
            handler = Errors()
            logging.getLogger().addHandler(handler)
            stack.callback(logging.getLogger().removeHandler, handler)
            for module in (v2_live, books, v2_web, paper_exchange, personal_performance, entry_contracts):
                stack.enter_context(patch.object(module, 'datetime', Clock))
            # Outbox imports datetime inside its delivery function.
            stack.enter_context(patch('datetime.datetime', Clock))
            stack.enter_context(patch('socket.create_connection', side_effect=AssertionError('Network forbidden in isolated proof')))
            stack.enter_context(patch('socket.socket.connect', side_effect=AssertionError('Network forbidden in isolated proof')))
            order_call = stack.enter_context(patch.object(broker,'place_order',side_effect=AssertionError('Broker order forbidden')))
            stack.enter_context(patch.object(books,'subscribers',return_value=[2]))
            stack.enter_context(patch.object(v2_live,'_SLEEVE_ENGINE',engine))
            stack.enter_context(patch.object(v2_live,'V2_DB',str(case.f.path)))
            stack.enter_context(patch.object(v2_live,'MAIN_DB',str(source)))
            stack.enter_context(patch.dict('os.environ',SCREENING_DB=str(Path(case.f.tmp.name)/'absent-index-history.db')))
            stack.enter_context(patch.object(v2_live,'market_open',return_value=True))
            stack.enter_context(patch.object(v2_live,'_live',side_effect=quotes))
            stack.enter_context(patch.object(v2_live,'_hist',return_value=({'TEST':case.rebound()},fixtures.pd.DataFrame())))
            stack.enter_context(patch.object(v2_live,'_rw',side_effect=lambda:sqlite3.connect(case.f.path)))
            stack.enter_context(patch.object(v2_live,'_ro',side_effect=lambda _:sqlite3.connect(source)))
            stack.enter_context(patch.object(v2_live.eng,'complete_trading_dates',return_value=[fixtures.pd.Timestamp('2026-10-05')]))
            stack.enter_context(patch('app.sleeves.reference.refresh_membership'))
            stack.enter_context(patch('app.sleeves.reference.snapshot',return_value=({'TEST'},{})))
            stack.enter_context(patch.object(automation,'current_screen',return_value=ctx.equity_screen))
            stack.enter_context(patch.object(v2_live,'_SLEEVE_VIEW_FILE',str(Path(case.f.tmp.name)/'view.json')))
            stack.enter_context(patch.object(v2_live,'_sleeve_views',{}))
            stack.enter_context(patch.object(v2_live,'_EQ_SNAP',{}))
            stack.enter_context(patch.object(v2_live,'_forward_updated_day',{'IN':'2026-10-06'}))
            stack.enter_context(patch.object(v2_live,'_option_live',return_value={}))
            stack.enter_context(patch.object(v2_live,'_session_opens',return_value={}))
            stack.enter_context(patch.object(v2_live,'_stale_symbols',return_value=set()))
            stack.enter_context(entry_contracts.using(case.f.catalogue))
            v2_live.sleeve_pass('IN')
            assert len(paper_exchange.pending(case.con,0)) == 1, v2_live._status.get('IN')
            record('entry_reserved_not_filled')
            assert timeline[-1]['house_positions'] == timeline[-1]['owned_positions'] == 0
            assert timeline[-1]['cash'] == 10000
            reopen()  # Durable intent survives a process/database restart.
            advance(.000001, refresh=False)
            v2_live.service_personal_paper(case.con,'IN')
            assert case.con.execute('SELECT COUNT(*) FROM v2_positions').fetchone()[0] == 0
            advance()
            v2_live.service_personal_paper(case.con,'IN')
            record('house_fill_confirmed')
            assert timeline[-1]['house_positions'] == 1
            assert timeline[-1]['owned_positions'] == 0
            assert execution_outbox.drain(case.con) == 1
            assert execution_outbox.drain(case.con) == 0
            assert len(paper_exchange.pending(case.con,2)) == 1
            advance(.000001, refresh=False)
            v2_live.service_personal_paper(case.con,'IN')
            assert books.positions(case.con,2) == []  # Submission-time quote is not a fill.
            advance()
            v2_live.service_personal_paper(case.con,'IN')
            owned = books.positions(case.con,2)[0]
            record('owned_fill_confirmed')
            assert owned['stop'] == 92 and owned['target'] == 112
            assert owned['sleeve'] == 'quality_momentum'
            assert owned['shares']*owned['entry_price'] <= 3000
            assert 0 <= timeline[-1]['cash'] < 10000
            advance()
            current_price = 91.95 if exit_kind == 'stop' else 112.05
            v2_live.exit_monitor('IN')  # Real house monitor derives stop/target exit.
            v2_live.service_personal_paper(case.con,'IN')  # Real owned monitor derives exit.
            record('automatic_'+exit_kind+'_requested_not_sold')
            assert timeline[-1]['house_positions'] == timeline[-1]['owned_positions'] == 1
            assert len(paper_exchange.exit_pending(case.con)) == 2
            assert case.con.execute('SELECT COUNT(*) FROM user_trades').fetchone()[0] == 0
            reopen()
            bad_depth = True
            advance()
            v2_live.service_personal_paper(case.con,'IN')
            assert len(books.positions(case.con,2)) == 1
            assert len(paper_exchange.exit_pending(case.con)) == 2
            bad_depth = False
            missing_depth = True
            advance()
            v2_live.service_personal_paper(case.con,'IN')
            assert len(books.positions(case.con,2)) == 1
            assert len(paper_exchange.exit_pending(case.con)) == 2
            missing_depth = False
            stale_depth = True
            advance()
            v2_live.service_personal_paper(case.con,'IN')
            assert len(books.positions(case.con,2)) == 1
            assert len(paper_exchange.exit_pending(case.con)) == 2
            stale_depth = False
            advance()
            v2_live.service_personal_paper(case.con,'IN')
            assert execution_outbox.drain(case.con) == 1
            assert execution_outbox.drain(case.con) == 0
            v2_live.service_personal_paper(case.con,'IN')  # Replay cannot sell twice.
            record('exit_fill_confirmed_reconciled')
            assert timeline[-1]['house_positions'] == timeline[-1]['owned_positions'] == 0
            assert not paper_exchange.exit_pending(case.con)
            assert case.con.execute('SELECT COUNT(*) FROM user_trades').fetchone()[0] == 1
            assert case.con.execute('SELECT COUNT(*) FROM v2_trades').fetchone()[0] == 1
            trade = case.con.execute('SELECT entry_price,exit_price,shares,pnl,reason FROM user_trades').fetchone()
            entry, exit_price, qty, net, reason = trade
            expected = qty*(exit_price-entry)-round_trip(qty*entry,qty*exit_price,'D')
            assert abs(net-expected) < .011
            assert abs(books.cash(case.con,2)-(10000+net)) < .011
            assert (net < 0) if exit_kind == 'stop' else (net > 0)
            assert exit_kind in reason
            stats = books.stats(case.con,2,'IN',quotes())
            ledger = paper_ledger.report(case.con,2,'IN',books.current_epoch(case.con,2),stats['cash'])
            assert ledger['status']=='ok' and ledger['balanced']
            assert ledger['cash_difference_minor']==0 and ledger['balances_minor'].get('inventory',0)==0
            assert stats['equity'] == stats['cash'] and stats['positions']==0
            order_call.assert_not_called()
            assert not errors, errors
            return dict(scenario=exit_kind,status='passed',capital=10000,sleeve='quality_momentum',model_version=automation.MODEL_VERSION,
                timeline=timeline,quantity=qty,exit_reason=reason,net_pnl=round(net,2),
                cash=stats['cash'],equity=stats['equity'],open_positions=0,
                ledger_cash_difference_minor=0,inventory_difference=0,duplicate_fills=0,
                restarted=True,wrong_identity_quote_refused=True,missing_depth_refused=True,stale_depth_refused=True,engine_errors=0,broker_orders=0)
    finally:
        case.doCleanups()


def run():
    return dict(version='production-paper-lifecycle-proof-v1',status='passed',scope='isolated synthetic production-path proof',
                profitability_evidence=False,production_orders_submitted=0,
                scenarios=[scenario('stop'),scenario('target')])


if __name__ == '__main__':
    if not __debug__:
        raise SystemExit('Run without -O: proof assertions must remain enabled')
    print(json.dumps(run(),sort_keys=True))
