"""Local preview harness for the OpenStocks v2 SPA (layout/visual checks only).

Serves the REAL SPA_HTML from app/v2_web.py with mocked /v2/api/* responses so
the dashboard renders without the OCI DB or login. Visual verification only —
never used in production.
"""
from __future__ import annotations

import json
import math
import os
import random
import re
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = open(os.path.join(_ROOT, "app", "v2_web.py"), encoding="utf-8").read()
SPA_HTML = list(re.finditer(r'SPA_HTML = r"""(.*?)"""', _SRC, re.S))[-1].group(1)

BOOT = """<script>
window.ME={id:1,username:'demo',role:'admin',signal_execution_mode:'paper'};
window.__origFetch=window.fetch;
addEventListener('DOMContentLoaded',function(){
  setTimeout(function(){try{
    document.getElementById('login').classList.add('hide');
    document.getElementById('app').classList.remove('hide');
    var avatar=document.getElementById('avatar');if(avatar)avatar.textContent='D';
    if(window.refresh)refresh();if(window.loadTicker)loadTicker();
    var v=location.search;
    if(v.indexOf('detail')>=0){setTimeout(function(){stock('PARAS','IN')},250);}
    else if(v.indexOf('positions')>=0){setTimeout(function(){go('positions')},250);}
    else if(v.indexOf('orders')>=0){setTimeout(function(){go('orders')},250);}
    else if(v.indexOf('analyze')>=0){setTimeout(function(){go('analyze');setTimeout(function(){document.getElementById('qsym').value='PARAS';doAnalyze()},200)},250);}
    else if(v.indexOf('account')>=0){setTimeout(function(){go('account')},250);}
    else if(v.indexOf('login')>=0){setTimeout(function(){document.getElementById('app').classList.add('hide');document.getElementById('login').classList.remove('hide')},150);}
    else if(window.go)go('home');
    if(v.indexOf('metrics')>=0){setTimeout(function(){
      var vw=document.documentElement.clientWidth,bad=[];
      document.querySelectorAll('*').forEach(function(el){
        var r=el.getBoundingClientRect();
        if(r.right>vw+1){bad.push((el.id?'#'+el.id:el.className||el.tagName)+'('+Math.round(r.right)+')')}
      });
      document.title='VW='+vw+' SW='+document.documentElement.scrollWidth+' OVERFLOW: '+bad.slice(0,12).join(' | ');
    },600);}
  }catch(e){console.log('boot',e)}},80);
});
</script></body>"""


def _candles(n=90, start=900.0):
    out, p = [], start
    for i in range(n):
        o = p
        drift = 0.004 if i > n * 0.55 else -0.001
        cl = max(5, o * (1 + drift + (random.random() - 0.5) * 0.05))
        hi = max(o, cl) * (1 + random.random() * 0.012)
        lo = min(o, cl) * (1 - random.random() * 0.012)
        out.append([round(o, 2), round(hi, 2), round(lo, 2), round(cl, 2), int(2e6 * random.random()) + 2e5])
        p = cl
    return out


def _pos(sym, mkt, strat, live, pnl, head, qty, val, stop, since):
    ccy = "₹" if mkt == "IN" else "$"
    entry = round(live / (1 + pnl / 100), 2)
    return dict(id=abs(hash(sym)) % 9999, symbol=sym, market=mkt, strategy=strat, ccy=ccy,
                live=live, entry=entry, pnl=pnl, pnl_amt=round(val * pnl / 100, 2), headroom=head, qty=qty,
                value=val, trail=(strat == "gap_momentum"), stop=stop, since=since, today=(since == "today"))


POSITIONS = [
    _pos("PARAS", "IN", "gap_momentum", 1401.95, 7.87, 78, 7, 9098, 1298.7, "today"),
    _pos("AEGISVOPAK", "IN", "swing_meanrev", 299.11, 7.11, 74, 19, 5683, 279.25, "2d ago"),
    _pos("ZENTEC", "IN", "gap_momentum", 1998.6, 5.89, 70, 5, 9437, 1812.15, "1d ago"),
    _pos("PGEL", "IN", "gap_momentum", 563.0, 3.11, 64, 18, 9828, 513.33, "3d ago"),
    _pos("JKCEMENT", "IN", "swing_meanrev", 5529.5, 1.79, 48, 1, 5432, 5301.0, "4d ago"),
    _pos("HCC", "IN", "gap_momentum", 27.1, 0.82, 60, 372, 10008, 25.55, "today"),
    _pos("KPRMILL", "IN", "swing_meanrev", 1142.0, -1.34, 34, 8, 9136, 1157.5, "5d ago"),
    _pos("SWIGGY", "IN", "gap_momentum", 448.2, -2.10, 22, 20, 8964, 457.8, "1d ago"),
]


def _order(side, sym, mkt, qty, price, when, pnl=None, val=0, status="filled", reason=""):
    ccy = "₹" if mkt == "IN" else "$"
    import datetime as _dt
    _t = _dt.date.today()
    ts = (_t if "Today" in when else _t - _dt.timedelta(days=1)).isoformat()
    return dict(side=side, symbol=sym, market=mkt, ccy=ccy, qty=qty, price=price, when=when,
                pnl=pnl, pnl_amt=(round(val * (pnl or 0) / 100, 2)), value=val, status=status, reason=reason,
                today=("Today" in when), ts=ts)


ORDERS = [
    _order("BUY", "NEWGEN", "IN", 12, 533.3, "Today 09:26", val=6400),
    _order("BUY", "MAPMYINDIA", "IN", 4, 1054.4, "Today 09:20", val=4218),
    _order("BUY", "J&KBANK", "IN", 44, 186.0, "Today 09:15", val=8184),
    _order("SELL", "AVANTIFEED", "IN", 9, 984.9, "Today 14:10", pnl=-5.1, val=8864, reason="stop"),
    _order("SELL", "KIRLOSENG", "IN", 2, 2165.7, "Today 13:40", pnl=-11.06, val=4331, reason="stop"),
    _order("SELL", "WIPRO", "IN", 80, 542.0, "Yest 15:10", pnl=2.6, val=43360, reason="target"),
    _order("SELL", "TATAPOWER", "IN", 24, 412.0, "Yest 14:22", pnl=-1.9, val=9888, reason="stop"),
]


def _market(mkt):
    ccy = "₹" if mkt == "IN" else "$"
    budget = 100000 if mkt == "IN" else 20000
    eq = [round(budget * (1 + 0.0009 * i + (0.012 if i % 6 == 0 else -0.004))) for i in range(40)]
    # hero mock: a DOWN day (today dips below yesterday's close) over an UP
    # month — exercises the exact contradiction the redesign fixes
    prev_eq = round(budget * 1.025)
    today = [round(prev_eq * (1 + 0.004 * (i / 60.0) - 0.018 * (i / 75.0) ** 0.7 + 0.006 * (1 if i % 17 == 0 else 0))) for i in range(75)]
    daily = [round(budget * (1 + 0.0012 * i + (0.008 if i % 7 == 0 else -0.002))) for i in range(55)] + [today[-1]]
    return dict(market=mkt, ccy=ccy, budget=budget, equity=today[-1], equity_series=eq,
                today_series=today, prev_equity=prev_eq, daily_series=daily, daily_start="2026-05-12",
                cash=round(budget * 0.07), deployed=round(budget * 0.93), deploy_pct=93,
                today_pnl=today[-1] - prev_eq, overall_pnl=round(today[-1] - budget),
                today_pct=round((today[-1] - prev_eq) / prev_eq * 100, 2),
                overall_pct=round((today[-1] - budget) / budget * 100, 2),
                sharpe=1.84, maxdd=-4.2,
                positions=5, trades=11, win=64, pf=1.7)


OVERVIEW = dict(as_of="22 Jul, 13:45 IST", regime={"IN": True},
                regime_state={"IN": "NEUTRAL"},
                markets=[_market("IN")])

NEWS = [
    dict(label="price_momentum", title="Paras Defence hits fresh 52-week high on order-book optimism", when="2h ago", score=0.4),
    dict(label="partnership_expansion", title="Company signs MoU for indigenous drone subsystems", when="1d ago", score=0.3),
    dict(label="analyst_downgrade", title="Brokerage flags rich valuation after the run-up", when="3d ago", score=-0.2),
]


def _stock(sym, mkt):
    ccy = "₹" if mkt == "IN" else "$"
    cd = _candles(90, 900 if mkt == "IN" else 150)
    live = cd[-1][3]
    return dict(symbol=sym, market=mkt, live=round(live, 2), verdict="BUY", score=0.82,
                entry=round(live, 2), stop=round(live * 0.86, 2), target=round(live * 1.22, 2), rr=1.6,
                regime=(mkt == "US"),
                factors=dict(trend=78, rel_strength=64, volume=71, pullback=52, volatility=44),
                held=dict(strategy="gap", entry=round(live * 0.93, 2), qty=7,
                          pnl=7.87, rule="trailing stop 10% (now " + str(round(live * 0.927, 2)) + ")"),
                chart=[c[3] for c in cd], candles=cd, news=NEWS)


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, body, ctype="application/json"):
        b = body.encode() if isinstance(body, str) else body
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_POST(self):
        self._send(json.dumps({"ok": True}))

    def do_GET(self):
        p = self.path.split("?")[0]
        if p in ("/", "/index.html"):
            return self._send(SPA_HTML.replace("</body>", BOOT, 1), "text/html; charset=utf-8")
        if p == "/api/me/telegram":
            return self._send(json.dumps(dict(has_token=True, bot="pavithra_alerts_bot", linked=True,
                                               deep_link="https://t.me/pavithra_alerts_bot",
                                               alerts_buy=True, alerts_sell=True, alerts_radar=True, alerts_summary=True, alerts_price=True)))
        if p == "/api/auth/me":
            return self._send(json.dumps(dict(ok=True, user=dict(id=1, username="demo", role="admin", signal_execution_mode="paper"))))
        if p.startswith("/v2/api/stock/"):
            sym = p.rsplit("/", 1)[-1]
            mkt = "US" if "market=US" in self.path else "IN"
            return self._send(json.dumps(_stock(sym, mkt)))
        routes = {
            "/v2/api/overview": OVERVIEW,
            "/v2/api/positions": POSITIONS,
            "/v2/api/orders": ORDERS,
            "/v2/api/ticker": [dict(symbol=p2["symbol"], market=p2["market"], ccy=p2["ccy"],
                                    price=p2["live"], pnl=p2["pnl"], held=True, open=True) for p2 in POSITIONS],
            "/v2/api/stats": [],
            "/v2/api/engine-status": dict(engine=dict(running=True), market_open=dict(IN=True)),
            "/v2/api/watch": [
                dict(symbol="NAUKRI", market="IN", ccy="\u20b9", strategy="gap_momentum", badge="gap 9%", live=1159.3, chg=13.09),
                dict(symbol="KERNEX", market="IN", ccy="\u20b9", strategy="mom_breakout", badge="breakout +34%", live=412.1, chg=2.4),
                dict(symbol="WABAG", market="IN", ccy="\u20b9", strategy="swing_meanrev", badge="dip \u00b7 0.81", live=2206.3, chg=-1.2),
                dict(symbol="TEJASNET", market="IN", ccy="\u20b9", strategy="swing_meanrev", badge="dip \u00b7 0.77", live=612.0, chg=-2.1),
                dict(symbol="RITES", market="IN", ccy="\u20b9", strategy="gap_momentum", badge="gap 7%", live=231.6, chg=7.15),
            ],
            "/v2/api/health": dict(ok=True, checks=[]),
            "/v2/api/watchlist": dict(
                watch=[dict(symbol="RELIANCE", market="IN", ccy="\u20b9", price=1304.9, chg=-1.24),
                       dict(symbol="TCS", market="IN", ccy="\u20b9", price=3128.4, chg=0.62),
                       dict(symbol="INFY", market="IN", ccy="\u20b9", price=1598.2, chg=0.94)],
                alerts=[dict(id=1, symbol="RELIANCE", market="IN", ccy="\u20b9", kind="above", value=1350.0, active=True, triggered_at=None, triggered_price=None),
                        dict(id=2, symbol="TCS", market="IN", ccy="\u20b9", kind="below", value=3050.0, active=False, triggered_at="21 Jul 14:12 IST", triggered_price=3044.6)]),
            "/v2/api/ticker": [dict(symbol=s, market="IN", ccy="₹", price=p, pnl=c) for s, p, c in [
                ("RELIANCE", 1268.6, -0.8), ("TCS", 2250.0, 0.4), ("HDFCBANK", 739.6, -0.3),
                ("ICICIBANK", 1422.9, 0.6), ("INFY", 1038.8, -1.1), ("SBIN", 1005.9, 0.2),
                ("BHARTIARTL", 1886.8, 0.9), ("ITC", 280.7, -0.4), ("LT", 3612.6, 1.2)]],
            "/v2/api/indices": [
                dict(name="Nifty 50", last=23645.2, chg=-0.94),
                dict(name="Bank Nifty", last=56079.2, chg=-0.91),
                dict(name="Sensex", last=75726.0, chg=-0.87),
            ],
            "/v2/api/sectors": {"IN": [
                dict(sector="Automobiles", chg=2.8, n=14, top=["TVSMOTOR", "BAJAJ-AUTO", "EICHERMOT"]),
                dict(sector="Technology & Telecom", chg=1.4, n=22, top=["INFY", "COFORGE", "CYIENT"]),
                dict(sector="Financial Services", chg=0.9, n=31, top=["KARURVYSYA", "M&MFIN", "CSBBANK"]),
                dict(sector="Healthcare", chg=0.3, n=18, top=["CIPLA", "SUNPHARMA", "LUPIN"]),
                dict(sector="Energy & Utilities", chg=-0.6, n=16, top=["NTPC", "POWERGRID", "COALINDIA"]),
                dict(sector="Consumer", chg=-1.1, n=20, top=["ITC", "NESTLEIND", "BRITANNIA"]),
                dict(sector="Infrastructure", chg=-1.9, n=12, top=["LT", "ADANIPORTS", "GMRINFRA"]),
            ]},
            "/v2/api/catalysts": [
                dict(symbol="TVSMOTOR", kind="Q results", cat="results", subject="Outcome of Board Meeting — Q1 FY27 results", when="23-Jul-2026 17:00"),
                dict(symbol="DATAPATTNS", kind="New order", cat="order", subject="Receipt of order — defence contract", when="23-Jul-2026 16:41"),
                dict(symbol="KARURVYSYA", kind="Q results", cat="results", subject="Financial Results for quarter ended June 2026", when="23-Jul-2026 15:58"),
                dict(symbol="CIPLA", kind="Corp action", cat="corp_action", subject="Board approves buyback of equity shares", when="23-Jul-2026 15:10"),
            ],
            "/v2/api/movers": {
                "IN": dict(up=[dict(symbol="NAUKRI", price=1159.3, chg=13.09, ccy="\u20b9"), dict(symbol="GUJGASLTD", price=327.5, chg=10.7, ccy="\u20b9"),
                              dict(symbol="PWL", price=148.35, chg=9.81, ccy="\u20b9"), dict(symbol="WABAG", price=2206.3, chg=7.35, ccy="\u20b9"), dict(symbol="RITES", price=231.6, chg=7.15, ccy="\u20b9")],
                           down=[dict(symbol="E2E", price=432.2, chg=-8.4, ccy="\u20b9"), dict(symbol="TEJASNET", price=612.0, chg=-6.1, ccy="\u20b9"),
                                dict(symbol="INOXWIND", price=141.2, chg=-4.9, ccy="\u20b9"), dict(symbol="SYRMA", price=512.3, chg=-4.2, ccy="\u20b9"), dict(symbol="KAYNES", price=4980.0, chg=-3.8, ccy="\u20b9")])},
            "/v2/api/attribution": dict(
                strategies=[dict(market="IN", ccy="\u20b9", strategy="swing_meanrev", closed=10, win=80, realized=2217.0, avg_ret=2.64, open=9, unrealized=571.0),
                            dict(market="IN", ccy="\u20b9", strategy="gap_momentum", closed=3, win=67, realized=418.0, avg_ret=1.9, open=2, unrealized=310.0)],
                equity={"IN": dict(days=["07-1%d" % i for i in range(5, 8)] + ["07-2%d" % i for i in range(0, 3)], equity=[100000, 100400, 101100, 100900, 101800, 102790], maxdd=1.2)}),
        }
        # longest-prefix match so /v2/api/watchlist isn't shadowed by /v2/api/watch
        best = None
        for k, v in routes.items():
            if p.startswith(k) and (best is None or len(k) > len(best[0])):
                best = (k, v)
        if best:
            return self._send(json.dumps(best[1]))
        return self._send(json.dumps([]))


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8777
    print(f"preview SPA on http://127.0.0.1:{port}", flush=True)
    HTTPServer(("127.0.0.1", port), H).serve_forever()
