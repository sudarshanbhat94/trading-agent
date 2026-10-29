from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from app.db import Database
from app import billing_ledger


class BillingAtomicityTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.db=Database(Path(self.tmp.name)/'account.db');self.db.init()
        self.uid=self.db.create_user('fixture_owner','not-a-login-password-hash',role='user',active=True)['id']
        self.req=self.db.create_plan_request(self.uid,'paper',499)

    def test_confirmation_fault_rolls_back_request_receipt_and_entitlement(self):
        original=billing_ledger.confirm
        def fault(*args,**kwargs):original(*args,**kwargs);raise OSError('fixture storage fault')
        with patch.object(billing_ledger,'confirm',side_effect=fault):
            with self.assertRaises(OSError):self.db.decide_plan_request(self.req['id'],True,'fixture-admin','receipt-one')
        self.assertEqual(self.db.plan_request(self.req['id'])['status'],'pending')
        with self.db.connect() as con:self.assertEqual(billing_ledger.report(con,self.uid)['receipts'],[])
        self.assertFalse(self.db.user_by_id(self.uid)['plan_expires_at'])

    def test_concurrent_confirmation_once_and_reference_reuse_refusal(self):
        def approve(_):return self.db.decide_plan_request(self.req['id'],True,'fixture-admin','receipt-one')
        with ThreadPoolExecutor(max_workers=2) as pool:self.assertEqual([r['status'] for r in pool.map(approve,range(2))],['approved','approved'])
        with self.db.connect() as con:
            receipts=billing_ledger.report(con,self.uid)['receipts'];self.assertEqual(len(receipts),1)
            self.assertEqual(receipts[0]['amount_minor'],49900);self.assertEqual(billing_ledger.report(con,self.uid+1)['receipts'],[])
        next_request=self.db.create_plan_request(self.uid,'paper',499)
        before=self.db.user_by_id(self.uid)['plan_expires_at']
        with self.assertRaises(ValueError):self.db.decide_plan_request(next_request['id'],True,'fixture-admin','receipt-one')
        self.assertEqual(self.db.user_by_id(self.uid)['plan_expires_at'],before)

    def test_string_boolean_and_missing_payment_confirmation_are_refused(self):
        for value,reference in (('false','receipt'),(True,'')):
            with self.assertRaises(ValueError):self.db.decide_plan_request(self.req['id'],value,'fixture-admin',reference)
        self.assertEqual(self.db.plan_request(self.req['id'])['status'],'pending')
