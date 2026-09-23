"""Presentation-only desk shell. All account actions retain explicit API routing."""

CSS = r'''
:root{--bg:#f4f6fa;--surf:#fff;--card:#fff;--tx:#1d2a3d;--hd:#142034;--mut:#67758b;--line:#e2e7ef;--inf:#087d70;--infb:#e8f6f2;--up:#087d55;--upb:#e9f7ef;--dn:#c04443;--dnb:#fff0ee}
[data-theme=dark]{--bg:#101822;--surf:#192330;--card:#192330;--tx:#e6edf7;--hd:#f2f6fc;--mut:#9aacbf;--line:#304052;--inf:#70d9be;--infb:#183f37;--up:#70d9ae;--upb:#183c31;--dn:#ff9c98;--dnb:#452a2c}
body{background:var(--bg);color:var(--tx);font-family:Inter,ui-sans-serif,-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}
button,input,select{font:inherit}button:focus-visible,a:focus-visible,input:focus-visible,select:focus-visible{outline:3px solid var(--inf);outline-offset:3px}
.side{width:208px;padding:25px 15px;background:var(--card);border-right:1px solid var(--line)}
.side .b{font-size:23px;letter-spacing:-1px;padding:0 12px 25px}.side a{border-radius:9px;margin:4px 0;padding:12px 13px;font-size:13px}.side a.on{background:var(--infb);color:var(--inf);font-weight:650}
.main{max-width:1520px;padding:0 32px 40px}.top{position:relative;background:transparent!important;border-bottom:1px solid var(--line);padding:17px 0}.ticker{display:none!important}
.indexbar{border:0;background:transparent;gap:20px;padding:13px 0;overflow:hidden}.indexbar>div{background:transparent!important}
.home-grid{grid-template-columns:minmax(0,1fr) 280px!important;gap:24px!important}.home-rail{position:static;max-height:none;overflow:visible;padding-top:27px}
.fd-greet{margin:25px 0 20px}.fd-hi{font-size:28px;letter-spacing:-.8px;font-weight:700}.fd-sub{font-size:13px;line-height:1.6}.hp-ailive{display:none}
#homefeed{display:block!important}.desk-panel{background:var(--card);border:1px solid var(--line);border-radius:15px;padding:23px;margin-bottom:18px;min-width:0}
.desk-eyebrow{font-size:10px;text-transform:uppercase;letter-spacing:1.5px;color:var(--mut);font-weight:700}.desk-head{display:flex;align-items:center;justify-content:space-between;gap:14px;flex-wrap:wrap;margin-bottom:15px}.desk-head h2{margin:5px 0 0;font-size:18px;font-weight:650}
.desk-switch{display:flex;gap:3px;background:var(--bg);border:1px solid var(--line);border-radius:9px;padding:4px;max-width:100%}.desk-switch button{border:0;padding:8px 12px;border-radius:6px;background:transparent;color:var(--mut);font-size:12px;cursor:pointer}.desk-switch button.on{background:var(--card);color:var(--inf);box-shadow:0 1px 4px #0001;font-weight:650}
.desk-balance{font-size:42px;font-weight:700;letter-spacing:-1.5px;font-variant-numeric:tabular-nums;line-height:1.2;margin:8px 0}.desk-pnl{font-size:13px}.desk-pnl span{margin-left:10px;color:var(--mut)}
.desk-metrics{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:13px;margin:23px 0 6px;padding-top:19px;border-top:1px solid var(--line)}.desk-metrics small{display:block;color:var(--mut);font-size:11px;margin-bottom:7px}.desk-metrics b{font-size:17px;font-weight:600;font-variant-numeric:tabular-nums;overflow-wrap:anywhere}
.desk-chart{height:90px;overflow:hidden;margin:19px 0 4px}.desk-chart svg{height:90px!important;width:100%!important}.desk-note{color:var(--mut);font-size:11px;line-height:1.6;margin:8px 0 0}
.desk-grid{display:grid;grid-template-columns:1fr 1fr;gap:18px}.desk-grid .desk-panel{margin-bottom:0}.desk-status{display:inline-block;font-size:10px;letter-spacing:.6px;font-weight:700;padding:5px 9px;border-radius:5px;background:var(--infb);color:var(--inf)}.desk-status.warn{background:var(--dnb);color:var(--dn)}
.desk-message{font-size:14px;line-height:1.65;margin:15px 0}.desk-action{display:inline-flex;align-items:center;justify-content:center;padding:10px 14px;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--tx);font-size:12px;cursor:pointer;gap:6px}.desk-action.primary{background:var(--inf);color:var(--bg);border-color:var(--inf)}
.desk-index{display:flex;justify-content:space-between;align-items:center;gap:10px;padding:15px 0;border-bottom:1px solid var(--line)}.desk-index:last-child{border:0}.desk-index b{display:block;font-size:14px}.desk-index small{display:block;font-size:11px;color:var(--mut);margin-top:5px}
.desk-table{width:100%;border-collapse:collapse;font-size:12px}.desk-table th{text-align:left;color:var(--mut);font-size:10px;letter-spacing:.5px;text-transform:uppercase;font-weight:500;padding:11px 5px}.desk-table td{padding:13px 5px;border-top:1px solid var(--line)}.desk-empty{padding:23px 5px;line-height:1.65;font-size:13px;color:var(--mut)}
.sec{font-size:14px;letter-spacing:0;text-transform:none;margin:25px 0 13px}.card,.raise,.ig-card,.pos,.ord{background:var(--card);border:1px solid var(--line);border-radius:12px;box-shadow:none}.ig-card{padding:20px}.ig-sym{font-size:18px}.ig-track{height:5px}.ig-leg{background:var(--bg);border-radius:8px;padding:12px}.ig-list{gap:18px}.seg{background:var(--bg);border:1px solid var(--line);padding:4px;border-radius:9px}.seg b{border-radius:6px;padding:8px 13px}.seg b.on{background:var(--card);color:var(--inf)}
.bk-tabs{padding:4px;background:var(--card);border:1px solid var(--line);border-radius:9px;width:fit-content}.bk-tab{padding:9px 16px;border-radius:6px}.bk-tab.on{background:var(--infb);color:var(--inf)}
#login{background:var(--card);border:1px solid var(--line);border-radius:20px;padding:36px;box-shadow:0 18px 80px #10203a0c;margin-top:10vh;max-width:400px}#login h1{font-size:32px;letter-spacing:-1px}input,select{border-radius:9px!important;min-height:40px}button.pri{background:var(--inf);color:var(--bg);border:0;border-radius:9px}
.desk-top-actions{display:flex;gap:8px;flex-wrap:wrap}.desk-history{font-size:12px;color:var(--mut);padding-top:15px}.desk-history summary{cursor:pointer}
@media(min-width:860px){.side{flex:0 0 208px}.main{margin:0 auto}}
@media(max-width:1150px){.home-grid{grid-template-columns:minmax(0,1fr)!important}.home-rail{position:static;display:none}.main{padding-left:24px;padding-right:24px}}
@media(max-width:859px){.main{margin-left:0;padding:0 16px 90px}.side{display:none}.nav{background:var(--card);max-width:none;padding-bottom:env(safe-area-inset-bottom);border-top:1px solid var(--line)}.nav a{font-size:10px;min-width:0;padding:11px 2px}.nav a[data-t=watch],.nav a[data-t=analyze]{display:none}.nav a.on{color:var(--inf)}.nav svg{width:20px;height:20px}.top{padding:12px 0}.indexbar{display:flex;flex-wrap:nowrap;gap:12px;font-size:10px}.indexbar>*{min-width:0;flex:1}.indexbar .idx{display:flex;flex-direction:column;align-items:flex-start;gap:4px;font-size:11px;white-space:nowrap}.fd-hi{font-size:25px}.fd-greet{margin:21px 0 17px}.desk-panel{padding:19px 16px;border-radius:12px}.desk-balance{font-size:36px}.desk-grid{grid-template-columns:1fr;gap:15px}.desk-metrics{grid-template-columns:repeat(2,minmax(0,1fr));gap:20px}.desk-head{gap:12px}.desk-switch{width:100%;justify-content:space-between}.desk-switch button{flex:1;padding:9px 6px}.desk-table th:nth-child(3),.desk-table td:nth-child(3){display:none}.desk-chart{height:68px}.desk-chart svg{height:68px!important}#login{margin:8vh 18px 0;max-width:none;padding:26px}#ideasList{grid-template-columns:1fr!important}.ig-card{padding:16px}.desk-top-actions{width:100%}.desk-top-actions>*{flex:1}}
'''

JS = r'''
var DESK_ACCOUNT='mine',DESK_DATA=null;
function deskMoney(v){return v==null?'—':'₹'+new Intl.NumberFormat('en-IN',{maximumFractionDigits:2}).format(v);}
function deskMetric(label,v){return '<div><small>'+esc(label)+'</small><b>'+v+'</b></div>';}
function deskAccount(mode){DESK_ACCOUNT=mode;if(DESK_DATA)renderDesk(DESK_DATA);}
function renderDesk(d){
 DESK_DATA=d;var house=(d.markets||[]).find(x=>x.market==='IN')||{},mine=d.mine,real=d.real;
 REAL=real||null;MINE=mine||null;HERO=null;
 var selected=DESK_ACCOUNT==='house'?house:(DESK_ACCOUNT==='broker'?real:mine),isBroker=DESK_ACCOUNT==='broker';
 var labels={mine:'Your paper account',house:'OpenStocks strategy account',broker:'Your Upstox account'};
 var tabs=['mine','house','broker'].map(x=>'<button class="'+(DESK_ACCOUNT===x?'on':'')+'" onclick="deskAccount(\''+x+'\')">'+({mine:'My paper',house:'AI record',broker:'Upstox'})[x]+'</button>').join('');
 var h='<section class=desk-panel><div class=desk-head><div><div class=desk-eyebrow>ACCOUNT OVERVIEW</div><h2>'+labels[DESK_ACCOUNT]+'</h2></div><div class=desk-switch aria-label="Account view">'+tabs+'</div></div>';
 if(!selected){h+='<div class=desk-empty>'+(isBroker?'Broker balances are unavailable. Check your connection and token in Account. No paper balance is substituted.':'Your plan does not currently include a personal paper account.')+'</div><button class=desk-action onclick="go(\''+(isBroker?'account':'upgrade')+'\')">'+(isBroker?'Connection settings':'View plans')+'</button>';}else{
 var pnl=isBroker?selected.unrealised:selected.overall_pnl;
 h+='<div class=desk-eyebrow>'+(isBroker?'REAL MONEY · BROKER BALANCE':'SIMULATED · TOTAL EQUITY')+'</div><div class=desk-balance>'+deskMoney(selected.equity)+'</div>'
  +'<div class="desk-pnl '+col(pnl||0)+'">'+(pnl>0?'+':'')+deskMoney(pnl)+'<span>'+(isBroker?'unrealised P&L':'overall P&L · current epoch')+'</span></div>'
  +'<div class=desk-metrics>'+deskMetric('Available cash',deskMoney(selected.cash))+deskMetric(isBroker?'Invested':'Starting capital',deskMoney(isBroker?selected.invested:selected.budget))
  +deskMetric('Open positions',String(isBroker?selected.n_positions||0:selected.positions||0))+deskMetric(isBroker?'Mode':'Realised P&L',isBroker?'Live account':deskMoney(selected.realised))+'</div>';
 var series=selected.series||selected.daily_series||[];
 if(!isBroker&&series.length>1)h+='<div class=desk-chart>'+heroChart(series,selected.budget)+'</div>';
 h+='<p class=desk-note>'+(isBroker?'Only an explicitly confirmed live action can submit a broker order.':(DESK_ACCOUNT==='house'?'Shared strategy performance. Your personal cash is separate.':'This is your personal paper book. A reset affects only this account.'))+'</p>';
 }
 h+='</section>';
 var regime=(d.regime_state||{}).IN||'UNKNOWN',risk=house.readiness||{};
 var blocked=risk.halted||regime==='OFF'||regime==='UNKNOWN';
 var explanation=risk.halted?risk.reason:(regime==='OFF'?'NIFTYBEES is below its completed-session 200-day trend, so the strategy is holding cash.':(regime==='UNKNOWN'?'Waiting for a current, completed-session market reading.':'Only candidates that pass data, conviction and account risk checks can enter.'));
 var statusNote=risk.halted
  ?'Paper execution is paused until this book-risk condition clears.'
  :(house.positions?'No new entries; existing positions remain under exit management.'
    :(regime==='OFF'?'The paper book is 100% cash. Nothing is currently held.'
      :'The engine is screening; no position has qualified yet.'));
 h+='<div class=desk-grid><section class=desk-panel><div class=desk-head><div class=desk-eyebrow>STRATEGY STATUS</div><span class="desk-status '+(blocked?'warn':'')+'">'+(blocked?'NO NEW ENTRIES':'SCREENING')+'</span></div><h2 style="font-size:23px;margin:8px 0">'+regime+' regime</h2><p class=desk-message>'+esc(explanation)+'</p><p class=desk-note>'+esc(statusNote)+'</p><button class=desk-action style="margin-top:15px" onclick="go(\'ideas\')">Review opportunities →</button></section>'
  +'<section class=desk-panel><div class=desk-eyebrow>INDEX DESK</div><h2 style="font-size:18px;margin:10px 0">Nifty & Bank Nifty</h2><p class=desk-note>Market context, charts and instrument eligibility in one place.</p>'
  +['NIFTY','BANKNIFTY'].map(x=>'<div class=desk-index><div><b>'+({NIFTY:'Nifty 50',BANKNIFTY:'Bank Nifty'})[x]+'</b><small>View price, breadth and options context</small></div><button class=desk-action onclick="stock(\''+x+'\',\'IN\')">View →</button></div>').join('')+'</section></div>';
 h+='<section class=desk-panel style="margin-top:18px"><div class=desk-head><div><div class=desk-eyebrow>YOUR WORKSPACE</div><h2>Positions & activity</h2></div><div class=desk-top-actions><button class=desk-action onclick="BOOK=\'mine\';go(\'positions\')">Portfolio</button><button class=desk-action onclick="BOOK=\'mine\';go(\'orders\')">Order history</button></div></div>';
 var held=mine&&mine.holdings||[];
 h+=held.length?'<table class=desk-table><thead><tr><th>Instrument</th><th>Quantity</th><th>Sleeve</th><th>P&L</th></tr></thead><tbody>'+held.map(p=>'<tr><td>'+esc(p.symbol)+'</td><td>'+p.shares+'</td><td>'+esc(p.sleeve||p.strategy||'manual')+'</td><td class="'+col(p.pnl||0)+'">'+deskMoney(p.pnl)+'</td></tr>').join('')+'</tbody></table>':'<div class=desk-empty>No open personal paper positions. Qualified opportunities will appear in Ideas with their entry conditions and risk.</div>';
 var ob=d.options||{};h+='<details class=desk-history><summary>Historical options record</summary><p>Retired ledger only · '+(ob.options_trades||0)+' closed trades · '+deskMoney(ob.options_realised)+' realised. Excluded from all current account totals.</p></details></section>';
 document.getElementById('homefeed').innerHTML=h;
 document.getElementById('fd-hi').textContent='Your trading workspace';
 document.getElementById('fd-sub').textContent='Understand your account. Know why a trade qualifies. Track what actually happens.';
 document.getElementById('clock').textContent=d.as_of||'Updating';
 var legacyRadar=document.getElementById('radar');
 if(legacyRadar){legacyRadar.style.display='none';if(legacyRadar.previousElementSibling)legacyRadar.previousElementSibling.style.display='none';}
}
loadHome=function(){api('/v2/api/overview').then(r=>{if(!r.ok)throw Error(r.j.detail||'Account data unavailable');renderDesk(r.j);}).catch(e=>{document.getElementById('homefeed').innerHTML='<div class=desk-panel><h2>Account data unavailable</h2><p class=desk-note>'+esc(e.message)+'</p><button class=desk-action onclick="loadHome()">Retry</button></div>';});};
var deskOriginalGo=go;
go=function(t,noPush){var page=document.getElementById('indices');if(t!=='indices'&&page)page.classList.remove('on');
 if(t==='indices'){if(!noPush&&cur!==t)NAVHIST.push(cur);cur=t;document.querySelectorAll('.tab').forEach(e=>e.classList.toggle('on',e.id===t));document.querySelectorAll('.side a,.nav a').forEach(e=>e.classList.toggle('on',e.dataset.t===t));loadIndexDesk();window.scrollTo(0,0);return;}deskOriginalGo(t,noPush);};
function loadIndexDesk(){var box=document.getElementById('indexDeskBody');box.innerHTML='<div class=desk-empty>Loading index context…</div>';
 api('/v2/api/indices').then(r=>{if(!r.ok)throw Error('Index data unavailable');var rows=Array.isArray(r.j)?r.j:(r.j.indices||[]);
 box.innerHTML='<div class=desk-grid>'+['NIFTY','BANKNIFTY'].map(sym=>{var p=rows.find(x=>x.key===sym||x.name===({NIFTY:'Nifty 50',BANKNIFTY:'Bank Nifty'})[sym])||{};var state=sym==='NIFTY'?'PAPER ELIGIBLE · NIFTYBEES':'ANALYSIS ONLY';return '<section class=desk-panel><div class=desk-head><div class=desk-eyebrow>'+sym+'</div><span class="desk-status '+(sym==='NIFTY'?'':'warn')+'">'+state+'</span></div><h2>'+({NIFTY:'Nifty 50',BANKNIFTY:'Bank Nifty'})[sym]+'</h2><div class=desk-balance style="font-size:30px">'+(p.last?new Intl.NumberFormat('en-IN').format(p.last):'—')+'</div><p class=desk-note>'+(p.last?'Latest available reference level':'No current reference quote')+'</p><button class=desk-action style="margin-top:18px" onclick="stock(\''+sym+'\',\'IN\')">Open chart & analysis →</button></section>';}).join('')+'</div><section class=desk-panel style="margin-top:18px"><div class=desk-eyebrow>EXECUTION ELIGIBILITY</div><h2>Paper execution paths</h2><p class=desk-message>NIFTYBEES and verified large-cap quality/momentum stocks are screened for paper entries on monthly reviews, only in an ON regime. Bank Nifty remains analysis-only.</p><p class=desk-note>Futures and option spreads remain disabled at ₹10,000. Forward paper results are still required before live eligibility.</p></section>';}).catch(e=>box.innerHTML='<div class=desk-panel>'+esc(e.message)+'</div>');}
function deskManual(side,sym,mkt,explicitMode){if(DESK_ACCOUNT==='house'){toast('The shared strategy record is read-only. Choose My paper or Upstox.');return;}
 var mode=explicitMode||(DESK_ACCOUNT==='broker'?'live':'paper');
 if(!confirm((mode==='live'?'REAL MONEY: ':'PAPER: ')+side+' '+sym+' in your '+(mode==='live'?'Upstox':'personal paper')+' account?'))return;
 api('/v2/api/'+(side==='BUY'?'buy':'sell'),{method:'POST',body:JSON.stringify({symbol:sym,market:mkt,mode:mode})}).then(r=>{if(!r.ok){toast(r.j.error||r.j.detail||'Order refused');return;}toast(mode==='live'?'Broker '+r.j.broker_status+'; check confirmed fills.':'Paper '+side.toLowerCase()+' recorded');renderStock(sym,mkt,'detail');refresh();});}
doBuy=function(sym,mkt){deskManual('BUY',sym,mkt,'paper');};doSell=function(sym,mkt){deskManual('SELL',sym,mkt,'paper');};
'''


def enhance(html):
    html=html.replace('</head>','<style id="desk-design">'+CSS+'</style></head>',1)
    nav='<a data-t=indices onclick="go(\'indices\')"><svg viewBox="0 0 24 24"><path d="M3 19h18M5 15l5-6 4 3 6-8"/></svg><span class=lbl>Indices</span></a>'
    html=html.replace('<a data-t=analyze onclick=',nav+'<a data-t=analyze onclick=')
    page='<div id=indices class=tab><div class=fd-greet><div class=desk-eyebrow>MARKETS</div><h1 class=fd-hi>Index desk</h1><p class=desk-note>Nifty 50 and Bank Nifty · analysis, risk and execution readiness</p></div><div id=indexDeskBody></div></div>'
    html=html.replace('<div id=ideas class=tab>',page+'<div id=ideas class=tab>',1)
    anchor='boot();setInterval(()=>{if(ME)'
    if anchor not in html:
        raise ValueError('Desk boot anchor missing')
    return html.replace(anchor,JS+'\n'+anchor,1)
