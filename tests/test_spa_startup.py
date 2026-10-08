"""Boot every script in the served page without inventing legacy helpers."""
from html.parser import HTMLParser
import json
import re
import shutil
import subprocess
import unittest

from app import v2_web


class _PageElements(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.elements = {}
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if attrs.get('id'):
            self.elements[attrs['id']] = (attrs.get('class') or '').split()


@unittest.skipUnless(shutil.which('node'), 'Node required for full-page startup')
class ServedPageStartupTest(unittest.TestCase):
    def test_expired_session_reaches_login_instead_of_a_blank_page(self):
        payload = dict(elements=_PageElements(v2_web.SPA_HTML).elements,
                       scripts=re.findall(r'<script\b[^>]*>(.*?)</script>',
                                          v2_web.SPA_HTML, re.S | re.I))
        harness = r'''
const vm=require('vm'), assert=require('assert');
const data=JSON.parse(require('fs').readFileSync(0,'utf8'));
function element(classes){const names=new Set(classes);return {
 classList:{add:x=>names.add(x),remove:x=>names.delete(x),contains:x=>names.has(x),
  toggle:(x,on)=>{if(on===undefined)on=!names.has(x);on?names.add(x):names.delete(x);}},
 style:{},dataset:{},value:'',innerHTML:'',textContent:'',addEventListener(){},
 setAttribute(){},appendChild(){},querySelector(){return null;},querySelectorAll(){return [];}
};}
const elements=Object.fromEntries(Object.entries(data.elements).map(([id,c])=>[id,element(c)]));
const calls=[];
const context={console,Intl,URL,URLSearchParams,Date,JSON,Promise,
 document:{documentElement:element([]),body:element([]),
  getElementById:id=>elements[id]||null,querySelector:()=>null,querySelectorAll:()=>[],
  addEventListener(){},createElement:()=>element([])},
 localStorage:{getItem:()=>null,setItem(){}},matchMedia:()=>({matches:false}),
 addEventListener(){},scrollTo(){},setInterval:()=>0,setTimeout:()=>0,
 fetch:(url,options)=>{calls.push({url,method:(options||{}).method||'GET'});
  return Promise.resolve({ok:false,status:401,json:()=>Promise.resolve({detail:'Login required'})});}
};
context.window=context;
vm.createContext(context);
for(const script of data.scripts)vm.runInContext(script,context,{timeout:2000});
setImmediate(()=>setImmediate(()=>{
 assert(calls.some(c=>c.url==='/api/auth/me'),'Boot never reached authentication');
 assert(calls.every(c=>c.method==='GET'),'Page startup submitted a mutation');
 assert(!elements.login.classList.contains('hide'),'Login remained hidden');
 assert(elements.app.classList.contains('hide'),'Unauthenticated workspace visible');
 assert.strictEqual(typeof context.loadStats,'function');
 context.BOOK='house';context.loadStats();
 process.stdout.write(JSON.stringify({loginVisible:true,unauthenticatedAppHidden:true}));
}));
'''
        result = subprocess.run(['node', '-e', harness], input=json.dumps(payload),
                                text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr[-1800:])
        self.assertTrue(json.loads(result.stdout)['loginVisible'])
