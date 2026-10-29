import subprocess
import sys
import unittest


class ConcurrentColdImportsTest(unittest.TestCase):
    def test_first_parallel_account_requests_and_public_exports_do_not_deadlock(self):
        code = '''
from concurrent.futures import ThreadPoolExecutor
from importlib import import_module
from threading import Barrier
names=['app.sleeves.config','app.sleeves.feeds','app.personal_performance','app.entry_readiness']
barrier=Barrier(len(names))
def load(name):
    barrier.wait(timeout=5)
    return import_module(name)
with ThreadPoolExecutor(max_workers=len(names)) as executor:
    result=list(executor.map(load,names))
from app.sleeves import SLEEVES, Candidate, RegimeGate
assert SLEEVES.capital>0 and Candidate and RegimeGate
print('cold parallel imports and public exports passed')
'''
        for _ in range(3):
            result = subprocess.run([sys.executable, '-c', code], text=True, capture_output=True, timeout=12)
            self.assertEqual(result.returncode, 0, result.stderr[-1500:])
            self.assertIn('cold parallel imports and public exports passed', result.stdout)
