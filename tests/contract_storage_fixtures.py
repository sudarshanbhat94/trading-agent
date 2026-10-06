"""Isolate legacy storage/risk unit tests from the separate catalogue service.

These are not instrument certification fixtures. Integration coverage uses
real dated contracts in test_entry_contract_gate/test_approved_execution; this
class keeps old fee, isolation, crash and exit tests focused on their subject.
It mocks only external contract discovery, never allocation or book writes.
"""
import hashlib
import unittest
from unittest.mock import patch
from app import entry_contracts


def evidence(market,symbol,quantity,price,stop,target,**kwargs):
    return dict(instrument_id='ins_'+hashlib.sha256(symbol.encode()).hexdigest()[:32],
                broker_key=kwargs.get('key') or 'NSE_EQ|'+symbol,provider='isolated-unit-fixture',
                contract_evidence={'synthetic':True},product=kwargs.get('product','D'),quantity=quantity,
                regime=kwargs.get('regime') or 'ON',decision_at='2026-10-06T00:00:00+00:00')


class ContractStorageCase(unittest.TestCase):
    def run(self,result=None):
        from app import live_release
        with patch.object(entry_contracts,'check',side_effect=evidence),patch.object(live_release,'native_policy',return_value={
                'reference':'isolated unit authorization','source_commit':'a'*40}):
            return super().run(result)
