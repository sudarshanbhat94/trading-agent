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


OVERVIEW = dict(as_of="20 Jun, 13:45 IST", regime={"IN": False, "US": True},
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
                                    price=p2["live"], chg=round((random.random() - 0.4) * 3, 2)) for p2 in POSITIONS],
            "/v2/api/stats": [],
            "/v2/api/engine-status": dict(engine=dict(running=True), market_open=dict(IN=False, US=True)),
            "/v2/api/watch": [],
        }
        for k, v in routes.items():
            if p.startswith(k):
                return self._send(json.dumps(v))
        return self._send(json.dumps([]))


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8777
    print(f"preview SPA on http://127.0.0.1:{port}", flush=True)
    HTTPServer(("127.0.0.1", port), H).serve_forever()
