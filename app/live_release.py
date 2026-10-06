"""New live risk needs explicit account/route/model execution authorization.

Connecting or arming a broker is not commercial/strategy certification. This
reads a private, reviewed release record; it never creates or promotes one.
Existing owned exits continue to use their separate protection safeguards.
"""
import json
import os
from datetime import datetime,timezone
from pathlib import Path

from .release_gate import evaluate


def authorized(user_id,*,broker='upstox',venue='NSE',segment='NSE_EQ',product='D',model='manual',now=None):
    now=now or datetime.now(timezone.utc)
    if type(user_id) is not int or user_id<1:return False,'Invalid execution account'
    try:
        path=Path(os.environ['OPENSTOCKS_LIVE_RELEASE_EVIDENCE'])
        commit=os.environ['OPENSTOCKS_BUILD_COMMIT']
        if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
            return False,'Private reviewed live-release evidence unavailable'
        evidence=json.loads(path.read_text())
        release=evaluate(evidence,commit,now)
        if not release['go']:return False,'Live-release acceptance evidence incomplete'
        approval=evidence.get('execution_authorization',{})
        scopes=approval.get('scopes',[])
        if not isinstance(scopes,list) or any(not isinstance(item,dict) or
                type(item.get('account_id')) is not int or set(item)!={'account_id','broker','venue','segment','product','model'} or
                any(not isinstance(item.get(key),str) or not item[key].strip() for key in ('broker','venue','segment','product','model'))
                for item in scopes):
            return False,'Invalid execution authorization scopes'
        issued=datetime.fromisoformat(approval['issued_at'].replace('Z','+00:00'))
        expires=datetime.fromisoformat(approval['expires_at'].replace('Z','+00:00'))
        scope={'account_id':user_id,'broker':broker,'venue':venue,'segment':segment,'product':product,'model':model}
        if approval.get('source_commit')!=commit or not isinstance(approval.get('approved_by'),str) or not approval['approved_by'].strip() or \
                not isinstance(approval.get('reference'),str) or not approval['reference'].strip() or \
                issued.tzinfo is None or expires.tzinfo is None or not issued<=now<expires or \
                scope not in scopes or f'{broker}:{venue}:{segment}:{product}:{model}:{user_id}' not in release['certified_scopes']:
            return False,'This account/route/model lacks current execution authorization'
        return True,''
    except (OSError,KeyError,ValueError,TypeError,AttributeError):
        return False,'Live execution authorization unavailable'
