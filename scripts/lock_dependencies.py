#!/usr/bin/env python3
"""Freeze the installed, tested dependency closure; fetch only public PyPI hashes.

Run explicitly after upgrading and testing an isolated environment. This never
installs or upgrades dependencies. Linux production and macOS development
markers are considered for Python 3.12 and 3.14. All published distribution
hashes are retained so pip can select the appropriate platform wheel.
"""
import hashlib
import importlib.metadata as metadata
import json
from pathlib import Path
from urllib.request import urlopen
from urllib.parse import quote
from packaging.requirements import Requirement
from packaging.markers import default_environment
from packaging.utils import canonicalize_name

ROOT=Path(__file__).resolve().parents[1]


def roots(path):
    result=[]
    for line in path.read_text().splitlines():
        text=line.strip()
        if not text or text.startswith('#'):continue
        if text.startswith('-r '):result.extend(roots(path.parent/text[3:]))
        else:result.append(Requirement(text))
    return result


def closure(requirements):
    found={};pending=list(requirements);seen=set()
    while pending:
        req=pending.pop();name=canonicalize_name(req.name);identity=(name,tuple(sorted(req.extras)))
        if identity in seen:continue
        seen.add(identity);dist=metadata.distribution(req.name)
        if dist.version not in req.specifier:raise RuntimeError('Installed version violates manifest: '+name)
        found[name]=dist
        for raw in dist.requires or []:
            child=Requirement(raw)
            environments=[]
            for version in ('3.12','3.14'):
                for system in ('Linux','Darwin'):
                    env=default_environment();env.update(python_version=version,python_full_version=version+'.0',sys_platform='linux' if system=='Linux' else 'darwin',platform_system=system)
                    environments.extend(dict(env,extra=extra) for extra in (set(req.extras)|{''}))
            if not child.marker or any(child.marker.evaluate(env) for env in environments):pending.append(child)
    return found


def generate():
    runtime=closure(roots(ROOT/'requirements.txt'));all_packages=closure(roots(ROOT/'requirements-dev.txt'))
    records=[];requirements={};components=[]
    for name,dist in sorted(all_packages.items()):
        url='https://pypi.org/pypi/'+quote(name,safe='')+'/'+quote(dist.version,safe='')+'/json'
        with urlopen(url,timeout=30) as response:body=json.load(response)
        hashes=sorted({file['digests']['sha256'] for file in body['urls']})
        if not hashes or any(len(h)!=64 or any(c not in '0123456789abcdef' for c in h) for h in hashes):raise RuntimeError('Release hashes unavailable: '+name)
        requirements[name]=name+'=='+dist.version+' \\\n'+ ' \\\n'.join('    --hash=sha256:'+h for h in hashes)+'\n'
        records.append(dict(name=name,version=dist.version,runtime=name in runtime,source=url,hashes=hashes,
                            license_expression=body['info'].get('license_expression'),license=body['info'].get('license')))
        components.append(dict(type='library',name=name,version=dist.version,purl='pkg:pypi/'+name+'@'+dist.version,
                               hashes=[dict(alg='SHA-256',content=h) for h in hashes],
                               externalReferences=[dict(type='distribution',url=url)]))
    (ROOT/'requirements.lock').write_text('# Generated from the tested runtime; all published release hashes.\n'+''.join(requirements[n] for n in sorted(runtime)))
    (ROOT/'requirements-dev.lock').write_text('# Runtime plus exact tested developer/test dependencies.\n-r requirements.lock\n'+''.join(requirements[n] for n in sorted(all_packages) if n not in runtime))
    directory=ROOT/'reports/dependencies';directory.mkdir(parents=True,exist_ok=True)
    lock=dict(schema='openstocks-python-lock-v1',environments=['Linux Python 3.12/3.14','macOS Python 3.12/3.14'],packages=records)
    (directory/'lock.json').write_text(json.dumps(lock,indent=2)+'\n')
    sbom=dict(bomFormat='CycloneDX',specVersion='1.5',version=1,components=components)
    (directory/'sbom.cdx.json').write_text(json.dumps(sbom,indent=2)+'\n')
    print(json.dumps(dict(runtime_packages=len(runtime),total_packages=len(all_packages),installed_only=True,upgrades=0)))


if __name__=='__main__':generate()
