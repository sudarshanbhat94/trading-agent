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
    from scripts.rehearse_approved_ui import fixture_app
    from app import v2_web, books
    app = fixture_app()
    fixture = app.state.fixture
    routes = APIRouter()
    for route in v2_web.router.routes:
        if route.path == '/v2/api/orders':
            routes.routes.append(route)
    app.include_router(routes)
    saved = set()
    now = datetime.now(timezone.utc)
    day = now.date().isoformat()
    research = dict(symbol='TEST', state='RESEARCH WATCH', sector='Technology', rank=1,
                    entry_low=100, entry_high=102, stop=96, t1=108, t2=112, t3=116,
                    qty=2, estimated_stop_loss=48, evidence={},
                    why='Synthetic layout fixture: strong relative trend with dated participation evidence.',
                    entry_blocker='Waiting for a confirmed rebound within the published range.',
                    invalidation='A close below the recorded stop invalidates this scenario.')

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
                    stock_screen=dict(screened_count=1, evidence_passes=0, status='current', universe_count=1, price_asof=day),
                    paper_pipeline=dict(status='ok', worker='isolated fixture', completed=0, exits=0, orders={}),
                    execution_data=dict(rules=True, session='Synthetic fixture', reason='Disposable source evidence'),
                    research_watch=dict(status='current', rows=[], price_asof=day))

    @app.get('/v2/api/ideas')
    def ideas():
        return dict(stock_plans=dict(ideas=[], research_candidates=[research], watchlisted=sorted(saved),
                    cash=10000, risk_cap=150, price_asof=day, market_open=True,
                    discovery_health=dict(screened=1, generated_at=now.isoformat(), rejection_counts=[]),
                    tracking=dict(summary=dict(published=0), ideas=[])))

    @app.get('/v2/api/stock/{symbol}')
    def stock(symbol: str):
        if symbol!='TEST':return JSONResponse(dict(error='Fixture only contains TEST'), status_code=404)
        return dict(symbol=symbol, live=100, candles=[[99+i*.01,101+i*.01,98+i*.01,100+i*.01,1000000] for i in range(90)],
                    dates=[(now-timedelta(days=90-i)).date().isoformat() for i in range(90)], news=[])

    @app.get('/v2/api/watchlist')
    def watchlist():
        return dict(watch=[dict(symbol=s,market='IN',ccy='₹',price=100,chg=0,folder='',tags=['Synthetic fixture']) for s in sorted(saved)],alerts=[])

    @app.post('/v2/api/watchlist')
    async def add_watch(request: Request):
        data=await request.json()
        if data.get('symbol')!='TEST':return JSONResponse(dict(error='Fixture only'),status_code=400)
        saved.add('TEST');return dict(ok=True)

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
