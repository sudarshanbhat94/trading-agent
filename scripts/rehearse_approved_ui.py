#!/usr/bin/env python3
"""Isolated real-handler paper UI rehearsal. No production files or broker I/O.

--serve exposes only a disposable localhost fixture, not a production app.
Synthetic approvals/calendar/prices do not demonstrate a strategy edge.
"""
import argparse
from datetime import datetime,timezone,timedelta
import json
import os
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))


def fixture_app():
    from fastapi import FastAPI,APIRouter
    from fastapi.responses import HTMLResponse
    from app import account_ui,desk_ui,v2_web,books,broker,v2_live
    from tests.test_approved_execution import ApprovedPaperPipelineTest
    fixture=ApprovedPaperPipelineTest();fixture.setUp()
    # No fixture handle is shared across HTTP worker threads. The catalogue is
    # copied to disk so real handlers open normal read-only connections.
    main_path=Path(fixture.tmp.name)/'main.db'
    destination=sqlite3.connect(main_path);fixture.catalogue.backup(destination);destination.close()
    v2_web.V2_DB=str(fixture.path);v2_web.MAIN_DB=str(main_path)
    old_catalogue=os.environ.get('OPENSTOCKS_CATALOGUE_DB')
    os.environ['OPENSTOCKS_CATALOGUE_DB']=str(main_path)
    def restore_catalogue():
        if old_catalogue is None:os.environ.pop('OPENSTOCKS_CATALOGUE_DB',None)
        else:os.environ['OPENSTOCKS_CATALOGUE_DB']=old_catalogue
    fixture.addCleanup(restore_catalogue)
    broker.STATE_DIR=str(Path(fixture.tmp.name)/'brokers');broker.LEGACY_PATH=str(Path(fixture.tmp.name)/'absent.json')
    broker.verify=lambda *a,**kw:False
    v2_web._regime_state=lambda market:'ON'
    original_view=v2_live.sleeve_view
    fixture.addCleanup(setattr,v2_live,'sleeve_view',original_view)
    v2_live.sleeve_view=lambda market='IN':dict(regime='ON',asof=datetime.now(timezone.utc).date().isoformat())
    fixture.con.execute("INSERT INTO v2_equity VALUES('IN',?,10000,10000,0,0)", ('LIVE_'+datetime.now(timezone.utc).isoformat(),))
    fixture.con.commit()
    def fixture_quote(market,symbols=None):
        at=datetime.now(timezone.utc)-timedelta(seconds=1)
        return {'TEST':dict(price=100,ts=at.isoformat(),execution=fixture.snapshot(at))}
    v2_web._live_map=fixture_quote
    app=FastAPI();app.state.fixture=fixture
    app.dependency_overrides[v2_web.require_session]=lambda:dict(id=2,username='fixture',account_plan='auto')
    # Mount only reviewed read/order endpoints, not reset/admin/broker linking.
    selected={'/v2/api/approved-plans','/v2/api/approved-orders','/v2/api/paper-orders','/v2/api/paper-orders/{order_id}/cancel','/v2/api/paper-performance','/v2/api/paper-ledger','/v2/api/positions','/v2/api/trades','/v2/api/execution-health','/v2/api/trading-readiness'}
    routes=APIRouter()
    for route in v2_web.router.routes:
        if route.path in selected:routes.routes.append(route)
    app.include_router(routes)
    helpers="""var BOOK='mine',MKT='IN',ME={id:2},BRK={owner_user_id:2};
function esc(x){return x==null?'':String(x).replace(/[&<>\"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',\"'\":'&#39;'}[c]));}
function deskMoney(v){return v==null?'—':'₹'+new Intl.NumberFormat('en-IN',{maximumFractionDigits:2}).format(v);}
function api(u,o){return fetch(u,Object.assign({headers:{'Content-Type':'application/json'}},o||{})).then(r=>r.json().then(j=>({ok:r.ok,j:j})));}
function fixtureFill(){fetch('/fixture/fill',{method:'POST'}).then(()=>{loadApprovedPlans();loadPaperOrders();loadStats();});}
function fixtureExit(){fetch('/fixture/close',{method:'POST'}).then(()=>{loadPaperOrders();loadStats();});}
function loadStats(){}function renderBroker(){}function loadIdeas(){}function loadPos(){}
"""
    @app.get('/',response_class=HTMLResponse)
    def page():
        return '<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><title>OpenStocks isolated UI rehearsal</title><style>'+desk_ui.CSS+account_ui.CSS+'</style></head><body style="padding:20px;max-width:1000px;margin:auto"><header><h1>OpenStocks · isolated paper rehearsal</h1><p role=note>Disposable synthetic fixture. No broker orders. No strategy-profitability evidence.</p></header><nav class=account-report-toolbar><button class=desk-action onclick="loadStats()">Refresh performance</button><button class=desk-action onclick="loadApprovedPlans();loadPaperOrders()">Refresh approvals</button></nav><button class=desk-action id=fixtureFill onclick="fixtureFill()">Simulate later ask fill</button><button class=desk-action id=fixtureClose onclick="fixtureExit()">Simulate fixture target exit</button><div id=statlist></div><div id=tradingReadiness></div><div id=approvedPaperPlans></div><div id=paperPendingOrders aria-live=polite></div><div id=brokerBox></div><script>'+helpers+account_ui.JS+"TRADING_READINESS_SYMBOLS='TEST';loadTradingReadiness();loadStats();loadApprovedPlans();loadPaperOrders();document.getElementById('brokerBox').innerHTML=protectionHealthHtml({protection:{rows:[{symbol:'TEST',quantity:20,stop:99,state:'unknown'}]},incidents:[{code:'SYNTHETIC_STOP_TIMEOUT',detail:'Fixture only: uncertain broker outcome blocks duplicate sell.'}]});</script></body></html>"
    @app.post('/fixture/fill')
    def fill():
        from app import paper_exchange
        from datetime import timedelta
        at=datetime.now(timezone.utc)+timedelta(seconds=1)
        with sqlite3.connect(fixture.path) as con,sqlite3.connect(main_path) as catalogue:
            result=paper_exchange.service(con,catalogue,{'TEST':dict(price=100,ts=at.isoformat(),execution=fixture.snapshot(at))},regime='ON',now=at)
        return dict(filled=sum(x.get('status')=='filled' for x in result),outcomes=result,fixture_only=True)

    @app.post('/fixture/close')
    def close():
        from app import paper_exchange
        from datetime import timedelta
        with sqlite3.connect(fixture.path) as con:
            result=books.monitor_positions(con,'IN',{'TEST':dict(price=110,ts=datetime.now(timezone.utc).isoformat())})
            at=datetime.now(timezone.utc)+timedelta(seconds=1)
            result=paper_exchange.service_exits(con,{'TEST':dict(price=110,ts=at.isoformat(),execution=fixture.snapshot(at,price=110.05))},now=at)
        return dict(closed=sum(x.get('status')=='filled' for x in result),outcomes=result,fixture_only=True)
    return app


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--serve',action='store_true');p.add_argument('--port',type=int,default=8769)
    args=p.parse_args()
    if args.serve:
        import uvicorn
        uvicorn.run(fixture_app(),host='127.0.0.1',port=args.port,log_level='warning')
    else:
        from tests.test_approved_execution import ApprovedPaperPipelineTest
        import unittest
        outcome=unittest.TextTestRunner(verbosity=0).run(unittest.defaultTestLoader.loadTestsFromTestCase(ApprovedPaperPipelineTest))
        print(json.dumps(dict(passed=outcome.wasSuccessful(),checks=outcome.testsRun,isolated=True,broker_orders=0,profitability_evidence=False)))
        return 0 if outcome.wasSuccessful() else 1
    return 0


if __name__=='__main__':raise SystemExit(main())
