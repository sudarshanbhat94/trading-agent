"""Register and report a separate Rs 10,000 forward experiment; no book writes."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.screening import validation


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tracking-db',default='var/idea_tracking.db')
    parser.add_argument('--user',type=int,required=True)
    parser.add_argument('--protocol',default='var/idea_forward_protocol.json')
    parser.add_argument('--register',action='store_true',help='freeze a new future-only protocol; refuses overwrite')
    args=parser.parse_args();path=Path(args.protocol)
    if args.register:
        if path.exists():parser.error('Protocol already exists; original registration must be preserved')
        path.parent.mkdir(parents=True,exist_ok=True)
        with path.open('x') as handle:json.dump(validation.protocol(),handle,indent=2)
    data=validation.report(args.tracking_db,args.user,json.loads(path.read_text()))
    print(json.dumps(data,allow_nan=False))


if __name__=='__main__':main()
