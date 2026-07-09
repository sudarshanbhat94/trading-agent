"""Nightly backup of the paper trading book (v2_paper.db).

The book is a single unreplicated SQLite file — corruption or a bad deploy loses
the entire track record. This uses SQLite's online backup API (safe alongside
live writers) into var/backups/, keeping the newest 14.

  python3 scripts/backup_db.py
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone

SRC = os.environ.get("V2_PAPER_DB", "/opt/opentrade/var/v2_paper.db")
DST_DIR = "/opt/opentrade/var/backups"
KEEP = 14


def main():
    os.makedirs(DST_DIR, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    dst = os.path.join(DST_DIR, f"v2_paper_{stamp}.db")
    src = sqlite3.connect(SRC, timeout=60)
    out = sqlite3.connect(dst)
    with out:
        src.backup(out)
    out.close(); src.close()
    print(f"backup ok: {dst} ({os.path.getsize(dst)} bytes)", flush=True)
    backups = sorted(f for f in os.listdir(DST_DIR) if f.startswith("v2_paper_") and f.endswith(".db"))
    for old in backups[:-KEEP]:
        os.remove(os.path.join(DST_DIR, old))
        print(f"pruned {old}", flush=True)


if __name__ == "__main__":
    main()
