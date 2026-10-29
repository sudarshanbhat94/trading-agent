#!/usr/bin/env python3
"""Read a reviewed evidence JSON; exit 2 when commercial/live gates are incomplete."""
import argparse,json,subprocess,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.release_gate import evaluate


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--evidence',type=Path,required=True)
    args=parser.parse_args()
    commit=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip()
    try:
        result=evaluate(json.loads(args.evidence.read_text()),commit)
    except (OSError,ValueError,TypeError):
        result=dict(go=False,decision='NO-GO',failures=['Valid reviewed evidence file unavailable'])
    dirty=subprocess.check_output(['git','status','--porcelain','--untracked-files=all','--',
                                  'app','scripts','tests','deploy','.github','requirements.txt',
                                  'requirements-dev.txt','requirements.lock','requirements-dev.lock','Dockerfile'],text=True).strip()
    if dirty:
        result.update(go=False,decision='NO-GO',certified_scopes=[])
        result['failures'].append('release source contains uncommitted changes')
    print(json.dumps(result,allow_nan=False));return 0 if result['go'] else 2


if __name__=='__main__':sys.exit(main())
