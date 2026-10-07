"""Execute the actual home renderer: stock coverage is not the ETF trade gate."""
import json
from pathlib import Path
import shutil
import subprocess
import unittest

from app import desk_ui


def render(data):
    esc = next(line for line in (Path(__file__).resolve().parents[1]/'app/v2_web.py').read_text().splitlines()
               if line.startswith('function esc(x)'))
    script = esc + '''
var REAL,MINE,HERO;
function col(){return 'up';}
var elements={};
var document={getElementById:function(id){if(id==='radar')return null;return elements[id]||(elements[id]={});}};
''' + desk_ui.JS.split('loadHome=function()', 1)[0]
    script += '\nrenderDesk('+json.dumps(data)+');process.stdout.write(elements.homefeed.innerHTML);'
    run = subprocess.run(['node', '-e', script], text=True, capture_output=True, timeout=10)
    if run.returncode:
        raise AssertionError(run.stderr[:500])
    return run.stdout


def payload(**extra):
    return dict(markets=[dict(market='IN', positions=0, readiness=dict(halted=False))],
                mine=dict(equity=10000, cash=10000, budget=10000, realised=0, overall_pnl=0, positions=0),
                regime_state=dict(IN='OFF'),
                stock_screen=dict(status='current', universe_count=500, screened_count=320,
                                  evidence_passes=4, price_asof='2026-10-06'),
                execution_scope=dict(stock_entries_enabled=False), **extra)


@unittest.skipUnless(shutil.which('node'), 'node required for home renderer')
class DeskStatusCopyTest(unittest.TestCase):
    def test_off_index_gate_does_not_claim_only_an_etf_is_being_screened(self):
        html = render(payload())
        self.assertIn('320 liquid individual NSE stocks from 500 constituents', html)
        self.assertIn('NSE EQUITY SCREENING', html)
        self.assertIn('INDEX ENTRY STATUS', html)
        self.assertIn('INDEX ENTRIES PAUSED', html)
        self.assertIn('Automated individual-stock entries are not enabled', html)
        self.assertNotIn('STRATEGY STATUS', html)
        self.assertNotIn('so the strategy is holding cash', html)

    def test_missing_screen_never_claims_a_current_scan_or_stock_execution(self):
        data = payload(); data.pop('stock_screen'); data.pop('execution_scope')
        html = render(data)
        self.assertIn('SCREEN UNAVAILABLE', html)
        self.assertIn('execution status is unavailable', html)
        self.assertNotIn('SCREEN UPDATED', html)
        self.assertNotIn('The latest screen covers', html)
        self.assertNotIn('undefined', html)

    def test_stale_coverage_is_labelled_historical_without_live_pass_counts(self):
        data = payload(); data['stock_screen']['status'] = 'stale'
        html = render(data)
        self.assertIn('STALE SCREEN', html)
        self.assertIn('counts below are historical', html)
        self.assertIn('Passed evidence gates</small><b>—', html)

    def test_held_positions_are_not_described_as_all_cash_or_clean(self):
        data = payload(); data['markets'][0]['positions'] = 1
        html = render(data)
        self.assertIn('existing strategy positions remain under exit management', html)
        self.assertNotIn('100% cash', html)
        self.assertNotIn('strategy paper book has no open positions', html)

    def test_on_regime_does_not_imply_unapproved_individual_stock_execution(self):
        data = payload(); data['regime_state']['IN'] = 'ON'
        html = render(data)
        self.assertIn('INDEX GATE OPEN', html)
        self.assertIn('Automated individual-stock entries are not enabled', html)
        self.assertNotIn('NO NEW ENTRIES', html)

    def test_external_rejection_text_cannot_inject_markup(self):
        data = payload(); data['stock_screen']['rejections'] = [dict(count=2, reason='<img src=x onerror=alert(1)>')]
        html = render(data)
        self.assertIn('&lt;img', html)
        self.assertNotIn('<img src=x', html)

    def test_enabled_stock_trial_is_visible_and_does_not_claim_validated_returns(self):
        data = payload();data['execution_scope']['stock_entries_enabled'] = True
        html = render(data)
        self.assertIn('Individual-stock paper automation is enabled', html)
        self.assertIn('completed rebound', html)
        self.assertIn('has not established profitable returns', html)
        self.assertNotIn('Automated individual-stock entries are not enabled', html)


if __name__ == '__main__':
    unittest.main()
