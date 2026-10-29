"""Sizing approval must not be presented as completed execution approval."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from app import v2_live
from app.sleeves.base import Candidate
from app.sleeves.engine import PassResult
from app.sleeves.regime import RegimeView
from app.sleeves.risk import Allocation


class ExecutionReportingTest(unittest.TestCase):
    def view(self, execution=None, halt=None):
        candidate=Candidate('TEST','quality_momentum',.9,100,98,target=112)
        result=PassResult(RegimeView('ON',True,.7,'ON','fixture'),allocations=[Allocation(candidate,20,2000,120)],halt_reason=halt)
        with tempfile.TemporaryDirectory() as directory,patch.object(v2_live,'_SLEEVE_VIEW_FILE',str(Path(directory)/'view.json')),patch.object(v2_live,'_sleeve_views',{}):
            v2_live._remember_sleeve_view('IN',result,'2026-10-28','2026-10-29',execution=execution)
            return v2_live.sleeve_view('IN')

    def test_sized_candidate_is_not_an_approved_or_submitted_order(self):
        result=self.view()
        self.assertEqual(result['state'],'CHECKING EXECUTION')
        self.assertEqual(result['execution']['status'],'not_evaluated')
        self.assertNotIn('cleared every gate',result['reason'])

    def test_contract_refusal_retains_actual_reason_and_no_reserved_order(self):
        result=self.view(dict(proposed=1,reserved=0,rejected=[dict(symbol='TEST',reason='Sourced rules expired')],status='checked'))
        self.assertEqual(result['state'],'ENTRY BLOCKED')
        self.assertIn('Sourced rules expired',result['reason'])

    def test_reservation_is_pending_and_never_a_fill(self):
        result=self.view(dict(proposed=1,reserved=1,rejected=[],status='checked'))
        self.assertEqual(result['state'],'ORDER PENDING')
        self.assertIn('later exchange fill',result['reason'])

    def test_risk_halt_has_priority_over_candidates_or_stale_reservation_metadata(self):
        result=self.view(dict(proposed=1,reserved=1,rejected=[],status='checked'),halt='Daily loss limit')
        self.assertEqual(result['state'],'STAND ASIDE')
        self.assertEqual(result['reason'],'Daily loss limit')
