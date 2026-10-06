"""Current isolated regression runner. Legacy reproductions are retained separately.
No production database, credential or broker network operation is authorised.
"""
import os,subprocess,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
env=dict(os.environ,OPENSTOCKS_DISABLE_V2="1")
commands=[[sys.executable,"-m","unittest","tests.test_release_safety","tests.test_execution_hardening","tests.test_paper_ledger","tests.test_assessment_audit","tests.test_execution_capabilities","tests.test_release_evidence_gate","-q"],[sys.executable,"scripts/rehearse_execution.py"]]
for command in commands:
    result=subprocess.run(command,cwd=ROOT,env=env)
    if result.returncode:sys.exit(result.returncode)
