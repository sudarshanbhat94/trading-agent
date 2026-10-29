#!/usr/bin/env python3
"""Reproduce the historical billing crash using the exact committed method.

Only temporary account databases are used. The current schema creates the
fixture; the historical method itself is loaded from Git without importing or
starting a second application. No broker, notification or payment call occurs.
"""
import ast
from datetime import datetime,timezone
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import MethodType
from typing import Any
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from app.db import Database,utc_now
from app import billing_ledger


def main():
    baseline='8abd708ecc37c3b581997cc3268940ee602e6046'
    source=subprocess.check_output(['git','show',baseline+':app/db.py'],cwd=ROOT,text=True)
    cls=next(n for n in ast.parse(source).body if isinstance(n,ast.ClassDef) and n.name=='Database')
    method=next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name=='decide_plan_request')
    namespace={'__name__':'app.db','__package__':'app','Any':Any,'utc_now':utc_now}
    exec(compile(ast.Module(body=[method],type_ignores=[]),'<historical billed-access method>','exec'),namespace)
    outcomes={}
    with tempfile.TemporaryDirectory(prefix='openstocks-billing-reproduction-') as temporary:
        for version in ('historical','current'):
            db=Database(Path(temporary)/(version+'.db'));db.init()
            uid=db.create_user('fixture_'+version,'synthetic-non-login-hash',active=True)['id']
            request=db.create_plan_request(uid,'paper',499)
            if version=='historical':
                db.decide_plan_request=MethodType(namespace['decide_plan_request'],db)
                failure=patch.object(db,'update_user',side_effect=OSError('synthetic crash before entitlement'))
            else:
                real=billing_ledger.confirm
                def fault(*args,**kwargs):real(*args,**kwargs);raise OSError('synthetic crash before entitlement')
                failure=patch.object(billing_ledger,'confirm',side_effect=fault)
            with failure:
                try:db.decide_plan_request(request['id'],True,'fixture reviewer','synthetic-receipt')
                except OSError:pass
            outcomes[version]=dict(status=db.plan_request(request['id'])['status'],access_expiry_set=bool(db.user_by_id(uid)['plan_expires_at']))
    reproduced=outcomes['historical']==dict(status='approved',access_expiry_set=False)
    repaired=outcomes['current']==dict(status='pending',access_expiry_set=False)
    report=dict(finding='F54',observed_at=datetime.now(timezone.utc).isoformat(),baseline=baseline,
                baseline_reproduced=reproduced,current_repair_verified=repaired,outcomes=outcomes,production_mutations=0,
                scope='Exact historical decision method; synthetic fault and temporary account schema, not payment-provider certification')
    path=ROOT/'reports/release-audit-2026-10-06/billing-safety-reproduction.json'
    path.write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(dict(reproduced=reproduced,repaired=repaired)))
    return 0 if reproduced and repaired else 1


if __name__=='__main__':raise SystemExit(main())
