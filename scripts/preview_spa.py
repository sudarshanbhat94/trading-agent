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
    return dict(id=abs(hash(sym)) % 9999, symbol=sym, market=mkt, strategy=strat, ccy=ccy,
                live=live, pnl=pnl, pnl_amt=round(val * pnl / 100, 2), headroom=head, qty=qty,
                value=val, trail=(strat == "gap_momentum"), stop=stop, since=since, today=(since == "today"))


POSITIONS = [
    _pos("PARAS", "IN", "gap_momentum", 1401.95, 7.87, 78, 7, 9098, 1298.7, "18 Jun 09:15 IST"),
    _pos("HCC", "IN", "gap_momentum", 27.1, 0.82, 60, 372, 10008, 25.55, "18 Jun 09:15 IST"),
    _pos("ZENTEC", "IN", "gap_momentum", 1998.6, 5.89, 70, 5, 9437, 1812.15, "18 Jun 09:15 IST"),
    _pos("JKCEMENT", "IN", "swing_meanrev", 5529.5, 1.79, 48, 1, 5432, 5301.0, "18 Jun 09:15 IST"),
    _pos("PGEL", "IN", "gap_momentum", 563.0, 3.11, 64, 18, 9828, 513.33, "18 Jun 09:15 IST"),
    _pos("NVDA", "US", "gap_momentum", 178.4, 7.1, 84, 18, 3100, 169.0, "today"),
    _pos("AAPL", "US", "swing_meanrev", 232.1, 1.8, 55, 12, 2780, 226.0, "4d ago"),
    _pos("SMH", "US", "gap_momentum", 662.0, 4.3, 70, 4, 2640, 631.0, "1d ago"),
]


def _order(side, sym, mkt, qty, price, when, pnl=None, val=0, status="filled", reason=""):
    ccy = "₹" if mkt == "IN" else "$"
    return dict(side=side, symbol=sym, market=mkt, ccy=ccy, qty=qty, price=price, when=when,
                pnl=pnl, pnl_amt=(round(val * (pnl or 0) / 100, 2)), value=val, status=status, reason=reason)


ORDERS = [
    _order("BUY", "PARAS", "IN", 7, 1298.7, "18 Jun 09:15", val=9091),
    _order("BUY", "NVDA", "US", 18, 169.0, "19:02 IST", val=3042),
    _order("SELL", "WIPRO", "IN", 80, 542.0, "Yest 15:10", pnl=2.6, val=43000, reason="target"),
    _order("SELL", "TSLA", "US", 9, 248.0, "Yest 22:30", pnl=-1.9, val=2230, reason="stop"),
]


def _market(mkt):
    ccy = "₹" if mkt == "IN" else "$"
    budget = 100000 if mkt == "IN" else 20000
    eq = [round(budget * (1 + 0.0009 * i + (0.012 if i % 6 == 0 else -0.004))) for i in range(40)]
    return dict(market=mkt, ccy=ccy, budget=budget, equity=eq[-1], equity_series=eq,
                cash=round(budget * 0.07), deployed=round(budget * 0.93), deploy_pct=93,
                today_pnl=round(budget * 0.012), overall_pnl=round(eq[-1] - budget),
                today_pct=1.2, overall_pct=round((eq[-1] - budget) / budget * 100, 2),
                positions=5, trades=11, win=64, pf=1.7)


OVERVIEW = dict(as_of="10 Jul, 13:45 IST", regime={"IN": True, "US": True},
                regime_state={"IN": "NEUTRAL", "US": "STRONG"},
                markets=[_market("IN"), _market("US")])

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
            "/v2/api/engine-status": dict(engine=dict(running=True), market_open=dict(IN=False, US=True)),
            "/v2/api/watch": [
                dict(symbol="NAUKRI", market="IN", ccy="\u20b9", strategy="gap_momentum", badge="gap 9%", live=1159.3, chg=13.09),
                dict(symbol="KERNEX", market="IN", ccy="\u20b9", strategy="mom_breakout", badge="breakout +34%", live=412.1, chg=2.4),
                dict(symbol="WABAG", market="IN", ccy="\u20b9", strategy="swing_meanrev", badge="dip \u00b7 0.81", live=2206.3, chg=-1.2),
                dict(symbol="ENTG", market="US", ccy="$", strategy="swing_meanrev", badge="dip \u00b7 0.93", live=104.6, chg=-2.8),
                dict(symbol="JBL", market="US", ccy="$", strategy="gap_momentum", badge="gap 6%", live=385.3, chg=4.1),
            ],
            "/v2/api/health": dict(ok=True, checks=[]),
            "/v2/api/watchlist": dict(
                watch=[dict(symbol="RELIANCE", market="IN", ccy="\u20b9", price=1304.9, chg=-1.24),
                       dict(symbol="TCS", market="IN", ccy="\u20b9", price=3128.4, chg=0.62),
                       dict(symbol="NVDA", market="US", ccy="$", price=208.9, chg=1.85)],
                alerts=[dict(id=1, symbol="RELIANCE", market="IN", ccy="\u20b9", kind="above", value=1350.0, active=True, triggered_at=None, triggered_price=None),
                        dict(id=2, symbol="NVDA", market="US", ccy="$", kind="below", value=195.0, active=False, triggered_at="09 Jul 21:12 IST", triggered_price=194.6)]),
            "/v2/api/movers": {
                "IN": dict(up=[dict(symbol="NAUKRI", price=1159.3, chg=13.09, ccy="\u20b9"), dict(symbol="GUJGASLTD", price=327.5, chg=10.7, ccy="\u20b9"),
                              dict(symbol="PWL", price=148.35, chg=9.81, ccy="\u20b9"), dict(symbol="WABAG", price=2206.3, chg=7.35, ccy="\u20b9"), dict(symbol="RITES", price=231.6, chg=7.15, ccy="\u20b9")],
                           down=[dict(symbol="E2E", price=432.2, chg=-8.4, ccy="\u20b9"), dict(symbol="TEJASNET", price=612.0, chg=-6.1, ccy="\u20b9"),
                                dict(symbol="INOXWIND", price=141.2, chg=-4.9, ccy="\u20b9"), dict(symbol="SYRMA", price=512.3, chg=-4.2, ccy="\u20b9"), dict(symbol="KAYNES", price=4980.0, chg=-3.8, ccy="\u20b9")]),
                "US": dict(up=[dict(symbol="SNDK", price=2101.0, chg=6.2, ccy="$"), dict(symbol="WDC", price=641.0, chg=5.1, ccy="$"),
                              dict(symbol="MU", price=142.2, chg=4.4, ccy="$"), dict(symbol="JBL", price=385.3, chg=4.1, ccy="$"), dict(symbol="STX", price=148.9, chg=3.2, ccy="$")],
                          down=[dict(symbol="ENPH", price=38.2, chg=-5.6, ccy="$"), dict(symbol="SEDG", price=22.1, chg=-4.8, ccy="$"),
                               dict(symbol="PLUG", price=2.8, chg=-4.1, ccy="$"), dict(symbol="RUN", price=11.9, chg=-3.7, ccy="$"), dict(symbol="FSLR", price=261.0, chg=-3.1, ccy="$")])},
            "/v2/api/attribution": dict(
                strategies=[dict(market="IN", ccy="\u20b9", strategy="swing_meanrev", closed=10, win=80, realized=2217.0, avg_ret=2.64, open=9, unrealized=571.0),
                            dict(market="IN", ccy="\u20b9", strategy="gap_momentum", closed=0, win=0, realized=0.0, avg_ret=0.0, open=2, unrealized=310.0),
                            dict(market="US", ccy="$", strategy="swing_meanrev", closed=4, win=50, realized=-71.0, avg_ret=-2.29, open=1, unrealized=44.0),
                            dict(market="US", ccy="$", strategy="gap_momentum", closed=5, win=0, realized=-451.0, avg_ret=-5.8, open=0, unrealized=0.0)],
                equity={"IN": dict(days=["07-0%d" % i for i in range(1, 8)], equity=[100000, 100400, 101100, 100900, 101800, 102400, 102790], maxdd=1.2),
                        "US": dict(days=["07-0%d" % i for i in range(1, 8)], equity=[20000, 19900, 19750, 19600, 19700, 19560, 19520], maxdd=2.4)}),
        }
        for k, v in routes.items():
            if p.startswith(k):
                return self._send(json.dumps(v))
        return self._send(json.dumps([]))


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8777
    print(f"preview SPA on http://127.0.0.1:{port}", flush=True)
    HTTPServer(("127.0.0.1", port), H).serve_forever()
