"""Verify the shipped product uses publications and owned execution contracts."""
import json
from pathlib import Path
import shutil
import subprocess
import unittest

from app import product_ui, v2_web, workspace_ui


def plan(symbol='TEST', research=True):
    return dict(symbol=symbol, state='RESEARCH WATCH' if research else 'WAIT FOR PULLBACK',
                sector='Technology', rank=1, entry_low=100, entry_high=102,
                stop=96, t1=108, t2=112, t3=116, qty=2, estimated_stop_loss=48,
                evidence={}, why='Dated evidence', entry_blocker='Waiting for confirmation')


@unittest.skipUnless(shutil.which('node'), 'Node required for actual shipped JavaScript')
class StockFirstProductTest(unittest.TestCase):
    def run_js(self, body):
        esc = next(line for line in Path(v2_web.__file__).read_text().splitlines()
                   if line.startswith('function esc(x)'))
        harness = esc + r'''
var MKT='IN',INR=new Intl.NumberFormat('en-IN'),USD=new Intl.NumberFormat('en-US'),ME={id:2},IDEAS={stock_plans:{ideas:[],research_candidates:[],watchlisted:[],cash:10000}},
IDEA_UI={search:'',sector:'all',view:'discover',sort:'rank',pending:{}},POS=[],BOOK='mine',SUBPOS='all',
APPROVED_PAPER_PLANS={},DESK_DATA=null,cur='detail',CHART;
function ideaMoney(v){return v==null?'—':'₹'+Number(v).toLocaleString('en-IN');}
function deskMoney(v){return ideaMoney(v)};function ideaRepaint(){};function stock(){};function go(t){cur=t};
function newsHtml(){return ''};function drawCandles(){};function inMkt(){return true};
function loadAttrib(){};function loadPortfolio(){};function loadPaperOrders(){};function ordInRange(){return true};
function renderIdeaTracking(){return ''};function loadTradingReadiness(){};function wsPipeline(){return ''};
function wsWatch(){return ''};function ideaStatus(){return ['Waiting for entry']};
const nodes={};const document={getElementById(id){return nodes[id]||(nodes[id]={innerHTML:'',classList:{add(){}},style:{}})},querySelectorAll(){return []}};
'''
        funnel_js = 'function wsFunnel('+workspace_ui.JS.split('function wsFunnel(',1)[1].split('function wsPipeline(',1)[0]
        script = harness + funnel_js + product_ui.JS + '\nPRODUCT.ideaOwner=ME.id;\n(async()=>{' + body + r'''
})().catch(e=>{console.error(e);process.exit(1)});
'''
        result = subprocess.run(['node', '-e', script], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_shipped_shell_replaces_sidebar_and_keeps_one_product_design(self):
        html = v2_web.SPA_HTML
        self.assertNotIn('<nav class=side id=side>', html)
        self.assertNotIn('<style id="desk-design">', html)
        self.assertNotIn('<style id=workspace-design>', html)
        self.assertEqual(html.count('<style id=product-design>'), 1)
        self.assertIn('aria-label="Mobile navigation"', html)
        self.assertIn('productShell();', html)
        self.assertIn('Review paper buy', html)

    def test_full_feed_research_entry_and_saved_filters_are_distinct_and_deduplicated(self):
        research, entry = plan('RESEARCH'), plan('ENTRY', False)
        out = self.run_js(f'''
IDEAS.stock_plans.ideas=[{json.dumps(entry)}];
IDEAS.stock_plans.research_candidates=[{json.dumps(research)},{json.dumps(entry)}];
IDEAS.stock_plans.watchlisted=['RESEARCH'];
const rows=v=>{{IDEA_UI.view=v;return ideaFiltered(IDEAS.stock_plans).map(x=>x.symbol)}};
console.log(JSON.stringify({{all:rows('discover'),entry:rows('entry'),research:rows('research'),saved:rows('saved')}}));
''')
        self.assertEqual(out['all'], ['ENTRY', 'RESEARCH'])
        self.assertEqual(out['entry'], ['ENTRY'])
        self.assertEqual(out['saved'], ['RESEARCH'])

    def test_research_render_has_no_buy_action_and_retains_conditional_levels(self):
        row = plan('<img src=x onerror=bad()>')
        row['why'] = '<script>bad()</script>'
        out = self.run_js(f'''
const html=productFeedRow({json.dumps(row)},IDEAS.stock_plans);
let calls=0;api=()=>{{calls++;throw Error('Unexpected order')}};
console.log(JSON.stringify({{html,calls}}));''')
        self.assertEqual(out['calls'], 0)
        self.assertNotIn('<img', out['html'])
        self.assertIn('&lt;img', out['html'])
        for text in ('Stop-loss', 'Target 1', 'Target 2 / 3', 'Waiting for confirmation', 'ideaToggleWatch'):
            self.assertIn(text, out['html'])
        self.assertNotIn('Review paper buy', out['html'])

    def test_stock_page_uses_published_levels_and_never_promotes_analysis_buy(self):
        out = self.run_js(f'''
IDEAS.stock_plans.research_candidates=[{json.dumps(plan())}];PRODUCT.symbol='TEST';
productPaintDetail({{live:103,entry:999,stop:888,target:2000,verdict:'BUY',candles:[]}});
console.log(JSON.stringify({{html:nodes.detail.innerHTML}}));''')
        self.assertIn('₹96', out['html'])
        self.assertNotIn('₹888', out['html'])
        self.assertNotIn('₹999', out['html'])
        self.assertIn('Entry not approved', out['html'])
        self.assertNotIn('onclick="doBuy', out['html'])

    def test_approved_review_is_only_shown_for_owned_approved_plan(self):
        out = self.run_js('''
PRODUCT.symbol='TEST';
APPROVED_PAPER_PLANS={a:{id:'a',status:'approved',plan:{symbol:'TEST',user_id:3,quantity:20,entry_low:100,entry_high:100,stop:99,target:110,sleeve:'manual'}}};
productPaintDetail(null);const other=nodes.detail.innerHTML;
APPROVED_PAPER_PLANS.a.plan.user_id=2;productPaintDetail(null);const own=nodes.detail.innerHTML;
APPROVED_PAPER_PLANS.a.status='rejected';productPaintDetail(null);
console.log(JSON.stringify({other,own,rejected:nodes.detail.innerHTML}));''')
        self.assertNotIn('reviewApprovedPaper', out['other'])
        self.assertIn('reviewApprovedPaper', out['own'])
        self.assertIn('Approved paper plan', out['own'])
        self.assertIn('₹99', out['own'])
        self.assertNotIn('reviewApprovedPaper', out['rejected'])

    def test_late_stock_response_cannot_replace_a_newer_stock_page(self):
        out = self.run_js('''
const pending={};api=u=>new Promise(resolve=>pending[u]=resolve);
PRODUCT.symbol='FIRST';const first=productFetchStock();
PRODUCT.symbol='SECOND';const second=productFetchStock();
pending['/v2/api/stock/SECOND?market=IN']({ok:true,j:{live:200}});await second;
pending['/v2/api/stock/FIRST?market=IN']({ok:true,j:{live:100}});await first;
console.log(JSON.stringify({symbol:PRODUCT.symbol,detail:PRODUCT.detail,html:nodes.detail.innerHTML}));''')
        self.assertEqual(out['symbol'], 'SECOND')
        self.assertEqual(out['detail']['live'], 200)
        self.assertIn('SECOND', out['html'])
        self.assertNotIn('FIRST', out['html'])

    def test_position_and_order_responses_after_account_change_are_ignored(self):
        out = self.run_js('''
const pending={};api=u=>new Promise(resolve=>pending[u]=resolve);
const a=loadPos(),b=loadOrders();ME={id:3};
pending['/v2/api/positions?scope=mine']({ok:true,j:[{symbol:'PRIVATE'}]});
pending['/v2/api/orders?limit=500&scope=mine']({ok:true,j:[{symbol:'PRIVATE'}]});
await new Promise(resolve=>setTimeout(resolve,0));
console.log(JSON.stringify({positions:nodes.poslist.innerHTML,orders:nodes.ordlist.innerHTML}));''')
        self.assertNotIn('PRIVATE', out['positions'])
        self.assertNotIn('PRIVATE', out['orders'])

    def test_previous_account_position_cannot_appear_on_stock_page(self):
        out = self.run_js("""
PRODUCT.symbol='TEST';POS=[{symbol:'TEST',market:'IN',qty:99,entry:777,stop:700,target:800}];
PRODUCT.positionOwner=3;PRODUCT.positionScope='mine';productPaintDetail(null);
console.log(JSON.stringify({html:nodes.detail.innerHTML}));""")
        self.assertNotIn('₹777', out['html'])
        self.assertNotIn('Your open paper trade', out['html'])

    def test_saved_list_failure_is_not_presented_as_empty(self):
        out = self.run_js("""
api=()=>Promise.resolve({ok:false,j:{detail:'Unavailable'}});await loadWL();
console.log(JSON.stringify({html:nodes.wl2.innerHTML}));""")
        self.assertIn('Saved stocks could not load', out['html'])
        self.assertNotIn('No saved stocks yet', out['html'])

    def test_saved_list_response_from_previous_account_is_ignored(self):
        out = self.run_js("""
let resolve;api=()=>new Promise(r=>resolve=r);const pending=loadWL();ME={id:3};
resolve({ok:true,j:{watch:[{symbol:'PRIVATE',market:'IN'}]}});await pending;
console.log(JSON.stringify({html:nodes.wl2&&nodes.wl2.innerHTML||''}));""")
        self.assertNotIn('PRIVATE', out['html'])

    def test_pending_exit_remains_unfilled_and_does_not_offer_buy_cancellation(self):
        out = self.run_js('''
console.log(JSON.stringify({html:paperOrderCards([{symbol:'TEST',side:'SELL',status:'pending',requested_qty:2,stop:96,target:110}])}));''')
        self.assertIn('no confirmed fill', out['html'])
        self.assertIn('Position and exposure remain', out['html'])
        self.assertNotIn('cancelPaperOrder', out['html'])

    def test_missing_position_protection_is_not_drawn_as_healthy_progress(self):
        out = self.run_js('''
console.log(JSON.stringify({html:productPosition({symbol:'TEST',market:'IN',qty:1,entry:100,live:102,stop:null,target:110,quote_status:'entry_only'})}));''')
        self.assertNotIn('class=product-progress', out['html'])
        self.assertIn('current mark unavailable', out['html'])

    def test_missing_price_cannot_render_flat_profit_or_total_in_actual_workspace(self):
        out=self.run_js('''
POS=[{symbol:'TEST',market:'IN',qty:1,entry:100,live:100,pnl_amt:0,stop:96,target:110,quote_status:'entry_only'}];
renderPos();MINE={equity:null,cash:8000,positions:1,valuation_complete:false,missing_marks:['TEST']};PRODUCT.overviewOwner=2;
console.log(JSON.stringify({html:nodes.poslist.innerHTML,total:nodes.postot.textContent,summary:productOpportunitySummary(null)}));''')
        self.assertIn('awaiting current prices',out['total'])
        self.assertIn('Current mark</small><b>—',out['html'])
        self.assertIn('valuation awaits fresh prices for 1 holdings',out['summary'])

    def test_selected_research_panel_cannot_create_a_buy_approval(self):
        row = plan('RESEARCH')
        row['why'] = '<img src=x onerror=bad()>'
        out = self.run_js(f'''
const html=productPlanPanel({json.dumps(row)},{{price_asof:'2026-10-26'}});
console.log(JSON.stringify({{html}}));''')
        self.assertIn('Conditional entry zone', out['html'])
        self.assertIn('2026-10-26', out['html'])
        self.assertIn('₹96', out['html'])
        self.assertIn('&lt;img', out['html'])
        self.assertNotIn('<img', out['html'])
        self.assertNotIn('reviewApprovedPaper', out['html'])

    def test_selected_approved_panel_uses_owned_frozen_levels_instead_of_research(self):
        row = plan()
        out = self.run_js(f'''
const research={json.dumps(row)};
APPROVED_PAPER_PLANS={{a:{{id:'a',status:'approved',plan:{{symbol:'TEST',user_id:2,market:'IN',quantity:1,entry_low:91,entry_high:92,stop:87,target:107}}}}}};
const own=productPlanPanel(research,{{}});
APPROVED_PAPER_PLANS.a.plan.user_id=3;
const other=productPlanPanel(research,{{}});
console.log(JSON.stringify({{own,other}}));''')
        for text in ('Frozen approved entry zone', '₹91', '₹92', '₹87', '₹107', 'reviewApprovedPaper'):
            self.assertIn(text, out['own'])
        self.assertNotIn('₹96', out['own'])
        self.assertNotIn('reviewApprovedPaper', out['other'])
        self.assertNotIn('₹87', out['other'])

    def test_idea_selection_follows_filter_and_never_leaks_previous_owner(self):
        out = self.run_js(f'''
IDEAS.stock_plans.research_candidates=[{json.dumps(plan('FIRST'))},{json.dumps(plan('SECOND'))}];
PRODUCT.selectionOwner=ME.id;PRODUCT.selectedIdea='SECOND';
const selected=productSelectedIdea(IDEAS.stock_plans).symbol;
IDEA_UI.search='FIRST';const filtered=productSelectedIdea(IDEAS.stock_plans).symbol;
IDEA_UI.search='';ME={{id:3}};PRODUCT.selectedIdea='SECOND';
const nextOwner=productSelectedIdea(IDEAS.stock_plans).symbol;
console.log(JSON.stringify({{selected,filtered,nextOwner}}));''')
        self.assertEqual(out, dict(selected='SECOND', filtered='FIRST', nextOwner='FIRST'))

    def test_summary_uses_available_cash_and_unknown_or_other_owner_never_becomes_zero_book(self):
        out = self.run_js('''
MINE={equity:10000,cash:10000,available_cash:7500,positions:2};PRODUCT.overviewOwner=2;
const own=productOpportunitySummary({risk_cap:150});
ME={id:3};const other=productOpportunitySummary(null);
console.log(JSON.stringify({own,other}));''')
        self.assertIn('₹7,500', out['own'])
        self.assertIn('Open positions</small><b>2', out['own'])
        self.assertNotIn('₹10,000', out['other'])
        self.assertIn('Open positions</small><b>—', out['other'])

    def test_failed_overview_replaces_stale_summary_with_retry(self):
        out = self.run_js('''
MINE={equity:10000,cash:10000,positions:0};PRODUCT.overviewOwner=2;
api=()=>Promise.resolve({ok:false,j:{detail:'Unavailable'}});await loadHome();
console.log(JSON.stringify({html:nodes.productIdeasAccount.innerHTML,owner:PRODUCT.overviewOwner,funnel:nodes.productFunnel.innerHTML}));''')
        self.assertIn('Account summary unavailable', out['html'])
        self.assertIn('Date unavailable', out['funnel'])
        self.assertIn('Retry', out['html'])
        self.assertNotIn('₹10,000', out['html'])
        self.assertIsNone(out['owner'])


    def test_funnel_is_visible_but_another_accounts_cached_counts_are_not(self):
        out=self.run_js("""
DESK_DATA={execution_funnel:{user_id:3,session_date:'2026-10-29',stages:[{key:'filled',count:777,label:'Filled',status:'observed'}]}};
productDiagnostics();const foreign=nodes.productFunnel.innerHTML;
DESK_DATA.execution_funnel.user_id=2;productDiagnostics();
console.log(JSON.stringify({foreign,own:nodes.productFunnel.innerHTML}));
""")
        self.assertNotIn('777',out['foreign'])
        self.assertIn('777',out['own'])
        self.assertIn('From screen to exit',out['own'])


if __name__ == '__main__':
    unittest.main()
