#!/usr/bin/env python3
"""Actual product SPA with disposable paper handlers; localhost only, no broker I/O.

Synthetic prices/publications are UI fixtures, never strategy evidence. The
approved-order pipeline is real; only the disposable fixture book can be written.
"""
from datetime import datetime, timezone, timedelta
from pathlib import Path
import argparse
import sys
import sqlite3

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def create_app():
    from fastapi.responses import HTMLResponse, JSONResponse
    from fastapi import Request, APIRouter
    from fastapi.staticfiles import StaticFiles
    from scripts.rehearse_approved_ui import fixture_app
    from app import v2_web, books
    app = fixture_app()
    app.mount('/static', StaticFiles(directory=Path(__file__).resolve().parents[1]/'app'/'static'), name='static')
    fixture = app.state.fixture
    routes = APIRouter()
    for route in v2_web.router.routes:
        if route.path == '/v2/api/orders':
            routes.routes.append(route)
    app.include_router(routes)
    saved = set()
    now = datetime.now(timezone.utc)
    day = now.date().isoformat()
    research = dict(symbol='TEST', name='Example Technologies', state='RESEARCH WATCH', sector='Technology', rank=1,
                    entry_low=100, entry_high=102, stop=96, t1=108, t2=112, t3=116,
                    qty=2, estimated_stop_loss=48, evidence={},
                    why='Synthetic layout fixture: strong relative trend with dated participation evidence.',
                    entry_blocker='Waiting for a confirmed rebound within the published range.',
                    invalidation='A close below the recorded stop invalidates this scenario.')
    # Explicitly synthetic names and prices exercise a populated product layout.
    # Only TEST has the fixture's approved-order contract; these add no approvals.
    research_rows = [research, dict(research, symbol='DEMO2', name='Example Industries',
                    sector='Industrials', rank=2, entry_low=250, entry_high=254,
                    stop=239, t1=279, t2=291, t3=303, qty=1, estimated_stop_loss=82,
                    entry_blocker='Waiting for volume confirmation at the entry zone.'),
                    dict(research, symbol='DEMO3', name='Example Consumer',
                    sector='Consumer', rank=3, entry_low=450, entry_high=456,
                    stop=430, t1=498, t2=520, t3=542, qty=1, estimated_stop_loss=98,
                    entry_blocker='Waiting for a controlled pullback into the entry zone.')]

    def tracking_fixture():
        summary={k:0 for k in ('published','zone_touched','t1','t2','t3','stopped',
                               'resolved','waiting','invalidated','expired')}
        return dict(status='ok', health=dict(market_open=True, polled_at=now.isoformat()),
                    summary=summary, rows=[], total=0, offset=0, limit=20,
                    note='Synthetic preview has no observation history or performance evidence.')

    @app.get('/product', response_class=HTMLResponse)
    def product():
        note = '<div role="note" style="padding:8px 16px;text-align:center;font-size:11px;background:#fff3ce;color:#644b13">Isolated synthetic UI rehearsal · no broker connection</div>'
        return v2_web.SPA_HTML.replace('<body>', '<body>'+note, 1)

    @app.get('/api/auth/me')
    def me():
        return dict(user=dict(id=2, username='fixture', role='admin', signal_execution_mode='paper', account_plan='auto'))

    @app.get('/api/account')
    def account():
        return dict(paper=dict(cash_by_market=dict(IN=10000, US=0)))

    @app.get('/v2/api/me')
    def plan():
        return dict(account_plan='auto', plan='auto', is_admin=True, username='fixture', id=2)

    @app.get('/v2/api/overview')
    def overview():
        with sqlite3.connect(fixture.path) as con:
            cash=books.cash(con, 2); positions=books.positions(con, 2)
            value=sum(p['shares']*p['entry_price'] for p in positions)
        mine=dict(budget=10000, cash=cash, equity=cash+value, realised=0, holdings=positions, positions=len(positions))
        return dict(mine=mine, markets=[dict(mine, market='IN')], regime_state=dict(IN='ON'),
                    stock_screen=dict(screened_count=3, evidence_passes=0, status='current', universe_count=3, price_asof=day),
                    paper_pipeline=dict(status='ok', worker='isolated fixture', completed=0, exits=0, orders={}),
                    execution_data=dict(rules=True, session='Synthetic fixture', reason='Disposable source evidence'),
                    research_watch=dict(status='current', rows=[], price_asof=day))

    @app.get('/v2/api/ideas')
    def ideas():
        return dict(stock_plans=dict(ideas=[], count=0, research_candidates=research_rows, watchlisted=sorted(saved),
                    funding_summary=dict(version='account-funding-v1',minimum_ticket=1500,
                        sleeve_position_limit=1,sleeve_positions_committed=0),
                    funding_rejections=[dict(symbol='SYNTHETIC-LARGE',qualification='watch',quality_rank=1,
                        reason='One share exceeds the remaining sleeve allocation and after-cost risk allowance.',
                        actionable=False,execution_ready=False,funding=dict(cash_available=10000,
                            one_share_cash_required=5540,one_share_stop_loss=280,risk_budget=150,
                            sleeve_notional_remaining=3000,minimum_ticket=1500,
                            blockers=[dict(reason='One share needs Rs 5500; remaining sleeve allocation is Rs 3000.')]))],
                    cash=10000, risk_cap=150, price_asof=day, market_open=True,
                    discovery_health=dict(screened=3, generated_at=now.isoformat(), rejection_counts=[]),
                    tracking=tracking_fixture()))

    @app.get('/v2/api/stock/{symbol}')
    def stock(symbol: str):
        row=next((r for r in research_rows if r['symbol']==symbol),None)
        if not row:return JSONResponse(dict(error='Unknown fixture symbol'), status_code=404)
        px=row['entry_low']
        return dict(symbol=symbol, live=px, candles=[[px-1+i*.01,px+1+i*.01,px-2+i*.01,px+i*.01,1000000] for i in range(90)],
                    dates=[(now-timedelta(days=90-i)).date().isoformat() for i in range(90)], news=[])

    @app.get('/v2/api/watchlist')
    def watchlist():
        return dict(watch=[dict(symbol=r['symbol'],market='IN',ccy='₹',price=r['entry_low'],chg=0,folder='',tags=['Synthetic fixture']) for r in research_rows if r['symbol'] in saved],alerts=[])

    @app.get('/v2/api/idea-tracking')
    def idea_tracking():
        return tracking_fixture()

    @app.post('/v2/api/watchlist')
    async def add_watch(request: Request):
        data=await request.json()
        if data.get('symbol') not in {r['symbol'] for r in research_rows}:return JSONResponse(dict(error='Fixture only'),status_code=400)
        saved.add(data['symbol']);return dict(ok=True)

    @app.delete('/v2/api/watchlist/{symbol}')
    def remove_watch(symbol: str):
        saved.discard(symbol);return dict(ok=True)

    @app.get('/v2/api/search')
    def search(q: str=''):
        return [dict(symbol='TEST',market='IN',name='Isolated synthetic equity')] if q.upper() in 'TEST' else []

    @app.get('/v2/api/{path:path}')
    def supplementary(path: str):
        if path=='health':return dict(ok=True, status='ok')
        if path=='attribution':return dict(strategies=[], equity={})
        if path=='portfolio':return dict(total_value=0, total_risk=0, sector_exposure=None)
        if path=='account':return dict(account_plan='auto', plan='auto', broker=dict(connected=False))
        if path=='broker':return dict(connected=False, armed=False)
        if path in {'ticker','indices','watch','alerts','activity','catalysts','engines','stats'}:return []
        return JSONResponse(dict(error='Unavailable in isolated fixture'),status_code=404)
    return app


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--port',type=int,default=64182)
    args=p.parse_args()
    import uvicorn
    uvicorn.run(create_app(),host='127.0.0.1',port=args.port,log_level='warning')
