"""Read-only, forward-observed stock-screen outcomes; not paper trade P&L."""
from pathlib import Path
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.sleeves.forward_watch import PATH, summary

if __name__ == "__main__":
    print(json.dumps(dict(source=PATH, by_regime=summary()), indent=2))
