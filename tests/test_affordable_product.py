"""Funding explanations do not manufacture approval or expose unsafe markup."""
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


@unittest.skipUnless(shutil.which('node'), 'node required for actual renderer')
class AffordableProductTest(unittest.TestCase):
    def render(self, payload):
        source = (Path(__file__).resolve().parents[1]/'app/product_assets/workspace.js').read_text()
        start = source.index('function productFunding(')
        body = source[start:source.index('renderStockPlans=function', start)]
        js = "function esc(v){return String(v??'').replace(/[&<>\"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',\"'\":'&#39;'}[c]));}\n"
        js += "function ideaMoney(v){return v==null?'—':'₹'+Number(v).toFixed(2);}\n"
        js += body+'\nconsole.log(productFunding('+json.dumps(payload)+'));'
        with tempfile.TemporaryDirectory() as directory:
            script=Path(directory)/'render.js';script.write_text(js)
            result=subprocess.run(['node',str(script)],capture_output=True,text=True,timeout=15)
        self.assertEqual(result.returncode,0,result.stderr)
        return result.stdout

    def test_risk_and_minimum_ticket_are_distinct_and_blocked_stock_has_no_buy_action(self):
        html=self.render(dict(risk_cap=150,funding_summary=dict(minimum_ticket=1500,
            sleeve_position_limit=1,sleeve_positions_committed=0),funding_rejections=[dict(
                symbol='EXPENSIVE',qualification='entry',quality_rank=1,reason='Insufficient sleeve allocation',
                funding=dict(cash_available=10000,one_share_cash_required=5540,one_share_stop_loss=280,
                    risk_budget=150,sleeve_notional_remaining=3000,minimum_ticket=1500,
                    blockers=[dict(reason='One share needs Rs 5500; remaining sleeve allocation is Rs 3000')]))]))
        for text in ('Minimum ticket','₹1500.00','Effective loss allowance','₹150.00',
                     'EXPENSIVE','Cash for one share','₹5540.00','₹280.00','₹3000.00','Not fundable'):
            self.assertIn(text,html)
        for control in ('onclick=','Review paper buy','ideaBuy(', 'reviewApprovedPaper('):
            self.assertNotIn(control,html)
        self.assertIn('No order is approved',html)

    def test_unfunded_research_is_not_labelled_as_entry_qualified_and_html_is_escaped(self):
        html=self.render(dict(risk_cap=150,funding_rejections=[dict(symbol='<img onerror=bad>',
            qualification='watch',reason='<script>bad()</script>',funding={})]))
        self.assertIn('Research candidate',html)
        self.assertNotIn('Entry checks passed',html)
        self.assertNotIn('<img',html)
        self.assertNotIn('<script>',html)
        self.assertIn('&lt;script&gt;',html)
        self.assertNotIn('₹NaN',html)

    def test_no_rejections_does_not_invent_approval_or_old_payload_numbers(self):
        self.assertEqual(self.render({}).strip(),'')
        html=self.render(dict(risk_cap=150,funding_summary=dict(minimum_ticket=1500)))
        self.assertIn('independent alternatives',html)
        self.assertIn('entry confirmation and approval remain required',html)
        self.assertNotIn('Not fundable',html)
        self.assertNotIn('₹NaN',html)

    def test_entry_alternatives_precede_research_even_when_local_ranks_overlap(self):
        source=(Path(__file__).resolve().parents[1]/'app/product_assets/workspace.js').read_text()
        rows=source[source.index('function productRows('):source.index('function productPlan(')]
        filtered=source[source.index('ideaFiltered=function'):source.index('ideaCards=function')]
        payload=dict(ideas=[dict(symbol='ENTRY1',rank=1),dict(symbol='ENTRY2',rank=2)],
                     research_candidates=[dict(symbol='WATCH1',rank=1,state='RESEARCH WATCH')])
        js="var IDEA_UI={search:'',sector:'all',view:'discover',sort:'rank'};\n"+rows+filtered
        js+='\nconsole.log(JSON.stringify(ideaFiltered('+json.dumps(payload)+').map(r=>r.symbol)));'
        with tempfile.TemporaryDirectory() as directory:
            script=Path(directory)/'rank.js';script.write_text(js)
            result=subprocess.run(['node',str(script)],capture_output=True,text=True,timeout=15)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(json.loads(result.stdout),['ENTRY1','ENTRY2','WATCH1'])
