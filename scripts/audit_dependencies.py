#!/usr/bin/env python3
"""Read-only OSV advisory check of the exact lock. Failure is not a clean scan."""
import json
from datetime import datetime,timezone
from pathlib import Path
from urllib.request import Request,urlopen

ROOT=Path(__file__).resolve().parents[1]


def main():
    lock=json.loads((ROOT/'reports/dependencies/lock.json').read_text())
    rows=lock['packages'];queries=[dict(package=dict(name=r['name'],ecosystem='PyPI'),version=r['version']) for r in rows]
    request=Request('https://api.osv.dev/v1/querybatch',data=json.dumps(dict(queries=queries)).encode(),headers={'Content-Type':'application/json'})
    with urlopen(request,timeout=45) as response:result=json.load(response)
    if not isinstance(result.get('results'),list) or len(result['results'])!=len(rows):raise RuntimeError('Incomplete advisory evidence')
    findings=[]
    for package,outcome in zip(rows,result['results']):
        for item in outcome.get('vulns',[]):
            request=Request('https://api.osv.dev/v1/vulns/'+item['id'])
            with urlopen(request,timeout=30) as response:vulnerability=json.load(response)
            if not vulnerability.get('withdrawn'):
                findings.append(dict(package=package['name'],version=package['version'],id=item['id'],summary=vulnerability.get('summary'),source='https://osv.dev/vulnerability/'+item['id']))
    report=dict(schema='openstocks-advisory-review-v1',observed_at=datetime.now(timezone.utc).isoformat(),source='https://api.osv.dev/v1/querybatch',
                checked_packages=len(rows),findings=findings,scope='Known OSV advisories only; not independent security certification')
    (ROOT/'reports/dependencies/advisories.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(dict(checked_packages=len(rows),known_active_advisories=len(findings))))
    return int(bool(findings))


if __name__=='__main__':raise SystemExit(main())
