#!/usr/bin/env python3
"""Create/restore a private encrypted deployment bundle; no service control."""
import argparse,json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.recovery_bundle import create,restore,private_key,RecoveryError


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=['create','restore']);p.add_argument('--key-file',type=Path,required=True)
    p.add_argument('--bundle',type=Path,required=True);p.add_argument('--manifest',type=Path)
    p.add_argument('--destination',type=Path);p.add_argument('--broker-key-file',type=Path)
    p.add_argument('--format',choices=['v1','v2'],default='v2',help='v2 streams large databases; v1 restores older Fernet bundles')
    a=p.parse_args();key=private_key(a.key_file)
    if a.format=='v2':
        from app.recovery_stream import create as create_stream,restore as restore_stream
    if a.action=='create':
        if not a.manifest:p.error('create requires --manifest')
        spec=json.loads(a.manifest.read_text())
        result=(create_stream(spec['sources'],a.bundle,key,source_commit=spec['source_commit'],quiescence_reference=spec['quiescence_reference'])
                if a.format=='v2' else create(spec['sources'],a.bundle,key,source_commit=spec['source_commit'],
                      quiescence_reference=spec['quiescence_reference'],require_complete=True))
    else:
        if not a.destination:p.error('restore requires --destination')
        result=(restore_stream if a.format=='v2' else restore)(a.bundle,a.destination,key,
                       broker_escrow_key=private_key(a.broker_key_file) if a.broker_key_file else None)
    print(json.dumps(result));return 0


if __name__=='__main__':
    try:sys.exit(main())
    except (RecoveryError,ValueError,KeyError,OSError):
        print('Recovery refused: invalid/incomplete declaration, key or archive; no execution enabled.',file=sys.stderr)
        sys.exit(2)
