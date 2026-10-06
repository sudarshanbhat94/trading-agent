"""Execute account renderers against missing, currency and hostile evidence."""
import json
import shutil
import subprocess
import unittest
from app import account_ui,v2_web


@unittest.skipUnless(shutil.which('node'),'node required for JavaScript renderer checks')
class AccountUIContractTest(unittest.TestCase):
    def test_readiness_shows_actual_blockers_and_escapes_hostile_evidence(self):
        html=self.render('tradingReadinessHtml',dict(symbols=['<img src=x>'],checked_at='fixture',
            paper=dict(status='blocked',production_sleeves=['index_directional'],observation_sleeves=['quality_momentum']),
            live=dict(status='not_certified'),checks=[dict(code='contracts',status='blocked',reason='Reviewed daily evidence missing')],
            instruments=[dict(symbol='<img src=x>',reason='Unreviewed instrument')]))
        self.assertIn('Reviewed daily evidence missing',html);self.assertIn('not_certified',html)
        self.assertIn('does not establish profitability',html);self.assertIn('&lt;img',html);self.assertNotIn('<img',html)
        self.assertIn('Check entry readiness',html)

    def render(self,function,payload):
        helpers=r'''var loadStats=function(){},loadIdeas=function(){},renderBroker=function(){},window={};
function esc(x){return x==null?'':String(x).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
function deskMoney(v){return v==null?'—':'₹'+v;}
'''
        code=helpers+account_ui.JS+'\nprocess.stdout.write('+function+'('+json.dumps(payload)+'));'
        out=subprocess.run(['node'],input=code,text=True,capture_output=True,timeout=10)
        self.assertEqual(out.returncode,0,out.stderr[:300]);return out.stdout

    def report(self,currency='INR'):
        empty=dict(trades=0,net_pnl=0,win_rate=None,average_r=None,r_observations=0)
        return dict(currency=currency,epoch='isolated',day='2026-10-06',book=dict(budget=10000,cash=10000,equity=None,positions=1),
                    valuation_complete=False,missing_marks=['TEST'],current_epoch=empty,by_sleeve_epoch=dict(manual=empty),by_regime_epoch=dict(ON=empty))

    def test_missing_valuation_and_r_do_not_become_zero_or_claim_profit(self):
        html=self.render('personalReportHtml',self.report())
        self.assertIn('Equity unavailable',html);self.assertIn('Current equity</small><b>—',html)
        self.assertIn('0 observed',html);self.assertIn('No win rate or profitability record',html)
        self.assertNotIn('NaN',html);self.assertNotIn('undefined',html)

    def test_us_report_is_dollars_and_escaped_unknown_sleeve(self):
        payload=self.report('USD');payload['by_sleeve_epoch']['<img src=x onerror=alert(1)>']=payload['current_epoch']
        html=self.render('personalReportHtml',payload)
        self.assertIn('$10,000.00',html);self.assertNotIn('₹',html);self.assertNotIn('<img',html)
        self.assertIn('&lt;img',html)

    def test_approved_levels_and_uncertain_stop_are_visible_and_escaped(self):
        html=self.render('approvedPlansHtml',dict(plans=[dict(id='plan_fixture',status='approved',plan=dict(symbol='<img>',sleeve='manual',model_version='v1',quantity=20,entry_low=100,entry_high=101,stop=99,target=110))]))
        for value in ('₹100','₹101','₹99','₹110','20 shares','Review paper buy','&lt;img&gt;'):self.assertIn(value,html)
        self.assertNotIn('<img>',html)
        health=self.render('protectionHealthHtml',dict(protection=dict(rows=[dict(symbol='TEST',quantity=20,stop=99,state='unknown')]),incidents=[dict(code='<img>',detail='<script>')]))
        self.assertIn('Unknown · reconciliation required',health);self.assertNotIn('<script>',health)
        self.assertIn('&lt;script&gt;',health)

    def test_components_are_in_actual_spa_and_retry_preserves_unknown_identity(self):
        self.assertIn('id=approvedPaperPlans',v2_web.SPA_HTML)
        self.assertIn('account-report-design',v2_web.SPA_HTML)
        self.assertIn("r.j.status==='rejected'",account_ui.JS)
        self.assertIn('Outcome unavailable. Retry uses the same request identity.',account_ui.JS)
        self.assertIn("aria-labelledby','approvedPaperHeading'",account_ui.JS)

    def test_payment_receipts_escape_reference_and_show_empty_manual_state(self):
        empty=self.render('billingReceiptsHtml',dict(receipts=[]))
        self.assertIn('No confirmed payments yet',empty)
        html=self.render('billingReceiptsHtml',dict(receipts=[dict(plan='Elite',amount_minor=10000,payment_reference='<img src=x>',confirmed_at='fixture date',ends_at='fixture expiry')]))
        self.assertIn('₹100.00',html);self.assertIn('&lt;img',html);self.assertNotIn('<img',html)
        self.assertIn('not tax invoices',html)
        self.assertIn('ME.id!==owner',account_ui.JS)
        self.assertIn('Retry receipts',account_ui.JS)
