#!/usr/bin/env python3
"""Import a reviewed source bundle into the catalogue, never reset a book."""
import argparse
import json
from pathlib import Path
import sqlite3
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.catalogue_ingestion import import_bundle


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle',type=Path,required=True);parser.add_argument('--db',type=Path,required=True)
    args=parser.parse_args()
    with sqlite3.connect(args.db) as con:
        result=import_bundle(con,json.loads(args.bundle.read_text()),args.bundle.parent)
    print(json.dumps(result,sort_keys=True))


if __name__=='__main__':main()
