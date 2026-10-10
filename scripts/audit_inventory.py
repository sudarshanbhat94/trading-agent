"""Read repository text into a release-audit inventory; never import the app.

Stores names, locations and declared types, not secret values or live datasets.
Usage: .venv/bin/python scripts/audit_inventory.py
"""
import ast
import collections
import json
import re
import subprocess
import hashlib
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'reports' / 'release-audit-2026-10-06'


def run():
    OUT.mkdir(parents=True, exist_ok=True)
    files = sorted(set(subprocess.check_output(
        ['git', 'ls-files', '--cached', '--others', '--exclude-standard'],
        cwd=ROOT, text=True).splitlines()))
    result = dict(files=files, modules=[], routes=[], models=[], tables=[],
                  config_fields=[], env_vars=[], jobs=[], ui=[],
                  instrument_assumption_candidates=[], literal_assumptions=[], unread=[])
    result['baseline'] = dict(source_commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
                             generated_at=datetime.now(timezone.utc).isoformat(),
                             dirty_source=bool(subprocess.check_output(['git','status','--porcelain','--untracked-files=all','--','app','scripts','tests','deploy','.github','requirements.txt','requirements-dev.txt'],cwd=ROOT,text=True).strip()),
                             coverage='static declarations; semantic review recorded separately')
    result['source_hashes']={}
    for rel in files:
        path = ROOT / rel
        if not path.is_file():
            result['unread'].append(dict(file=rel, reason='missing/non-regular'))
            continue
        if rel == '.env.example':
            for no,line in enumerate(path.read_text().splitlines(),1):
                match=re.match(r'\s*([A-Z][A-Z0-9_]+)\s*=',line)
                if match:result['env_vars'].append(dict(name=match[1],file=rel,line=no))
            continue
        if rel.startswith(('var/', '.env', 'reports/')):
            result['unread'].append(dict(file=rel, reason='runtime/private values or report artifact excluded from source extraction; names only'))
            continue
        if path.suffix not in ('.py', '.js', '.html', '.css', '.service', '.timer', '.yml', '.yaml', '.txt', '.md') and path.name != 'Dockerfile':
            result['unread'].append(dict(file=rel, reason='non-source/binary; name only'))
            continue
        try:
            source = path.read_text()
        except (OSError, UnicodeError) as exc:
            result['unread'].append(dict(file=rel, reason=type(exc).__name__))
            continue
        result['source_hashes'][rel]=hashlib.sha256(source.encode()).hexdigest()
        if path.suffix == '.py':
            try:
                tree = ast.parse(source)
            except SyntaxError as exc:
                result['unread'].append(dict(file=rel, reason=f'AST parse error at {exc.lineno}'))
                continue
            mod = dict(file=rel, lines=len(source.splitlines()), imports=[], symbols=[])
            prefixes = {}
            for node in ast.walk(tree):
                if rel.startswith(('app/','scripts/')) and isinstance(node,ast.Assign):
                    targets=[t.id for t in node.targets if isinstance(t,ast.Name)]
                    for target in targets:
                        if re.search(r'SYMBOL|INDEX|EXCHANGE|LOT|TICK|EXPIR|SERIES|PRODUCT|MARKET|FREEZE|CIRCUIT|STRIKE|WATCH_HOT|INSTRUMENT_TOKEN',target,re.I) and not re.search(r'SECRET|PASSWORD|ACCESS_TOKEN|AUTH_TOKEN|API_TOKEN|API_KEY',target,re.I):
                            try:value=ast.literal_eval(node.value)
                            except (ValueError,TypeError):continue
                            if isinstance(value,(str,int,float,bool,list,tuple,dict)):
                                result['literal_assumptions'].append(dict(file=rel,line=node.lineno,name=target,type=type(value).__name__))
                if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call) and ast.unparse(node.value.func).endswith('APIRouter'):
                    prefix = next((k.value.value for k in node.value.keywords if k.arg == 'prefix' and isinstance(k.value, ast.Constant)), '')
                    for target in node.targets:
                        if isinstance(target, ast.Name): prefixes[target.id] = prefix
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    mod['imports'].append(node.module if isinstance(node, ast.ImportFrom) else ','.join(a.name for a in node.names))
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    mod['symbols'].append(dict(name=node.name, line=node.lineno, kind=type(node).__name__))
                if isinstance(node, ast.Call):
                    func = ast.unparse(node.func)
                    if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                        key = node.args[0].value
                        if (func.endswith(('getenv', 'environ.get')) or func in ('_bool', '_float', '_int')) and re.fullmatch('[A-Z][A-Z0-9_]+', key):
                            result['env_vars'].append(dict(name=key, file=rel, line=node.lineno))
                    if func.endswith(('create_task', 'Thread', 'start_background', 'start_background_task', 'add_job', 'start_background_thread')):
                        result['jobs'].append(dict(file=rel, line=node.lineno, launcher=func,
                            target=ast.unparse(node.args[0].func) if node.args and isinstance(node.args[0], ast.Call) else next((ast.unparse(k.value) for k in node.keywords if k.arg == 'target'), 'dynamic/no positional target')))
                if isinstance(node, ast.Subscript) and ast.unparse(node.value).endswith('environ') and isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str):
                    result['env_vars'].append(dict(name=node.slice.value, file=rel, line=node.lineno))
                if isinstance(node, ast.ClassDef):
                    bases = [ast.unparse(b) for b in node.bases]
                    decorators = [ast.unparse(d) for d in node.decorator_list]
                    if 'BaseModel' in bases or any('dataclass' in d for d in decorators):
                        fields = [dict(name=n.target.id, annotation=ast.unparse(n.annotation), line=n.lineno) for n in node.body if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name)]
                        result['models'].append(dict(name=node.name, file=rel, line=node.lineno, bases=bases, fields=fields))
                        if node.name.endswith(('Settings', 'Config')):
                            result['config_fields'].extend(dict(file=rel, owner=node.name, **f) for f in fields)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    for d in node.decorator_list:
                        if isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and d.func.attr in ('get','post','put','patch','delete','websocket','head','options'):
                            value=d.args[0] if d.args else None
                            route=value.value if isinstance(value, ast.Constant) and isinstance(value.value,str) else 'DYNAMIC'
                            owner=ast.unparse(d.func.value)
                            result['routes'].append(dict(method=d.func.attr.upper(), path=prefixes.get(owner,'')+route, file=rel, line=node.lineno, handler=node.name,
                                guards=sorted(set(n.func.id for n in ast.walk(node) if isinstance(n,ast.Call) and isinstance(n.func,ast.Name) and (n.func.id.startswith('require_') or n.func.id=='Depends')))))
                if isinstance(node, ast.Constant) and isinstance(node.value,str):
                    for match in re.finditer(r'CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z0-9_]+)\s*\(', node.value, re.I):
                        definition=node.value[match.end():].split(';',1)[0]
                        columns=[dict(name=m[1],type=m[2].upper()) for m in re.finditer(r'(?:^|,|\n)\s*([A-Za-z_][A-Za-z0-9_]*)\s+(TEXT|INTEGER|INT|REAL|NUMERIC|BOOL|BLOB|DATETIME)\b',definition,re.I)]
                        result['tables'].append(dict(name=match[1], file=rel, line=node.lineno+node.value[:match.start()].count('\n'),columns=columns))
                    if rel in ('app/v2_web.py', 'app/desk_ui.py') and '<' in node.value and ('function ' in node.value or '<html' in node.value):
                        funcs=collections.Counter(re.findall(r'\b(?:async\s+)?function\s+(\w+)\s*\(',node.value))
                        result['ui'].append(dict(file=rel, line=node.lineno, page_ids=sorted(set(re.findall(r'id=["\']?p([A-Za-z][A-Za-z0-9_]*)',node.value))), navigation=sorted(set(re.findall(r'go\(["\']([^"\']+)',node.value))), functions=dict(funcs), duplicate_functions={k:v for k,v in funcs.items() if v>1}))
            result['modules'].append(mod)
        if path.suffix in ('.service','.timer'):
            fields={}
            for line in source.splitlines():
                if '=' in line and line.split('=',1)[0] in ('Description','After','Wants','Requires','Type','Restart','User','OnCalendar','OnUnitActiveSec','OnBootSec','Persistent','Unit'):
                    k,v=line.split('=',1); fields[k]=v
            scripts=sorted(set(re.findall(r'(?:scripts/|scripts\.)[A-Za-z0-9_./-]+',source)))
            result['jobs'].append(dict(file=rel, line=1, launcher='systemd', target=scripts, fields=fields))
        if path.suffix in ('.js','.html','.css'):
            result['ui'].append(dict(file=rel, line=1, lines=len(source.splitlines()), functions=dict(collections.Counter(re.findall(r'\b(?:async\s+)?function\s+(\w+)\s*\(',source)))))
        if rel.startswith(('app/','scripts/')):
            for no,line in enumerate(source.splitlines(),1):
                names=sorted(set(re.findall(r'\b(?:NIFTY|BANKNIFTY|FINNIFTY|MIDCPNIFTY|NSE_EQ|NSE_FO|BSE_EQ|BSE_FO|NSE|BSE|MCX|CNC|MIS|NRML|SL-M|NASDAQ|NYSE)\b',line)))
                markers=[m for m in ('lot_size','tick_size','freeze_quantity','expiry','instrument_token','instrument_key') if re.search(r'\b'+m+r'\b',line)]
                if names or markers:
                    result['instrument_assumption_candidates'].append(dict(file=rel,line=no,names=names,fields=markers))
    (OUT/'inventory.json').write_text(json.dumps(result,indent=2)+'\n')
    counts={k:len(result[k]) for k in ('files','modules','routes','models','tables','config_fields','jobs','ui','unread','instrument_assumption_candidates','literal_assumptions')}
    counts['unique_env_vars']=len({x['name'] for x in result['env_vars']})
    md=['# Repository inventory — 6 October 2026','', 'Generated by `scripts/audit_inventory.py` without importing the application. Source inventory is not a semantic review of every function. Actual environment values, credentials and runtime datasets are not copied. Route guards listed below are lexical hints; router-level dependencies and called service guards require separate verification.','', '## Counts','']
    md += [f'- {k}: {v}' for k,v in counts.items()]
    md += ['', '## Every Python module', '', '| File | Lines | Declared symbols |', '|---|---:|---|']
    md += [f"| {m['file']} | {m['lines']} | "+', '.join(f"{s['name']}:{s['line']}" for s in m['symbols'])+' |' for m in result['modules']]
    md += ['', '## Every decorated API route', '', '| Method | Path | Handler / evidence | Lexical guards |','|---|---|---|---|']
    md += [f"| {r['method']} | {r['path']} | {r['file']}:{r['line']} {r['handler']} | {', '.join(r['guards'])} |" for r in result['routes']]
    md += ['', '## Declared data models', '']
    md += [f"- {m['file']}:{m['line']} **{m['name']}** — "+', '.join(f"{f['name']}: {f['annotation']}" for f in m['fields']) for m in result['models']]
    md += ['', '## SQL table declaration sites', '']
    md += [f"- {t['name']} — {t['file']}:{t['line']}; columns: "+', '.join(f"{c['name']}:{c['type']}" for c in t['columns']) for t in result['tables']]
    md += ['', '## Configuration fields', '']
    md += [f"- {f['owner']}.{f['name']}: {f['annotation']} — {f['file']}:{f['line']}" for f in result['config_fields']]
    md += ['', '## Environment variable names and read sites', '']
    env=collections.defaultdict(list)
    for row in result['env_vars']:env[row['name']].append(f"{row['file']}:{row['line']}")
    md += [f"- {name} — "+', '.join(sorted(set(sites))) for name,sites in sorted(env.items())]
    md += ['', '## Background jobs and launch sites', '']
    md += [f"- {j['file']}:{j['line']} — {j['launcher']} → {j['target']}"+(f"; {j['fields']}" if 'fields' in j else '') for j in result['jobs']]
    md += ['', '## UI surfaces and duplicate declarations', '']
    for u in result['ui']:
        md.append(f"- {u['file']}:{u['line']} — pages/navigation: {u.get('navigation',u.get('page_ids',[]))}; duplicate functions: {u.get('duplicate_functions',{})}; function names: {sorted(u.get('functions',{}))}")
    md += ['', '## Instrument-related literal declarations and candidate references', '',
           'These are static search candidates, not an exhaustive semantic proof or a defect list. Dynamic expressions, aliases and broker-supplied values require tracing. Values are deliberately omitted; instrument credential values are never included.', '']
    md += [f"- {a['file']}:{a['line']} — {a['name']} ({a['type']})" for a in result['literal_assumptions']]
    md += ['', 'All keyword/reference locations are retained under `instrument_assumption_candidates` in inventory.json. Check these against the capability matrix, rather than removing every occurrence of an exchange or index name.', '']
    md += ['', '## Files deliberately not read / unavailable', '']
    md += [f"- {u['file']} — {u['reason']}" for u in result['unread']]
    md += ['', '## All repository file names', '']+[f'- {f}' for f in files]
    (OUT/'inventory.md').write_text('\n'.join(line.rstrip() for line in md)+'\n')
    print(json.dumps(counts))


if __name__=='__main__':run()
