"""Execute response races against actual component code, not source assertions."""
import shutil
import subprocess
import unittest
from app.account_ui import JS


@unittest.skipUnless(shutil.which('node'),'node required')
class AccountResponseRaceTest(unittest.TestCase):
    def test_prior_account_and_out_of_order_requests_cannot_overwrite_current_view(self):
        setup=r'''
var loadStats=function(){},loadIdeas=function(){},renderBroker=function(){},window={},BOOK='mine',MKT='IN',ME={id:2};
var pending=[],nodes={};for(var id of ['statlist','approvedPaperPlans','accountReceipts'])nodes[id]={innerHTML:'',querySelectorAll:()=>[]};
var document={getElementById:id=>nodes[id]};
function api(){return new Promise(resolve=>pending.push(resolve));}
function esc(x){return String(x||'').replace(/</g,'&lt;');}function deskMoney(x){return '₹'+x;}
'''
        assertions=r'''
(async function(){
 for(var pair of [[loadStats,'statlist'],[loadApprovedPlans,'approvedPaperPlans'],[loadBillingReceipts,'accountReceipts']]){
  ME={id:2};pair[0]();var previous=pending.shift();ME={id:3};nodes[pair[1]].innerHTML='new account';
  previous({ok:true,j:{receipts:[],plans:[],book:{budget:12345},current_epoch:{},by_sleeve_epoch:{},by_regime_epoch:{},currency:'INR'}});
  await Promise.resolve();await Promise.resolve();if(nodes[pair[1]].innerHTML!=='new account')throw Error('Cross-account '+pair[1]);
  ME={id:2};pair[0]();var first=pending.shift();pair[0]();var last=pending.shift();
  last({ok:true,j:{receipts:[],plans:[],book:{budget:10000},current_epoch:{},by_sleeve_epoch:{},by_regime_epoch:{},currency:'INR'}});
  await Promise.resolve();await Promise.resolve();var current=nodes[pair[1]].innerHTML;
  first({ok:false,j:{detail:'stale rejection'}});await Promise.resolve();await Promise.resolve();await Promise.resolve();
  if(nodes[pair[1]].innerHTML!==current)throw Error('Stale response '+pair[1]);
 }
 console.log('account and request races passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
        result=subprocess.run(['node'],input=setup+JS+assertions,text=True,capture_output=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr[:500])
        self.assertIn('races passed',result.stdout)
