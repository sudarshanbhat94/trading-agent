"""Daily performance report for the OpenStocks paper book.

READ-ONLY. Opens the book with mode=ro, so it cannot write, cannot trade, and
cannot alter strategy state. Safe to run at any time, including mid-session.

    python3 scripts/daily_report.py                    # book + all-time splits
    python3 scripts/daily_report.py --epoch            # current book epoch only
    python3 scripts/daily_report.py --since 2026-08-01
    python3 scripts/daily_report.py --today
    python3 scripts/daily_report.py --no-quotes        # skip the live-price mark

Live quotes are used only to mark open positions. If the feed is unavailable
the report still renders, marking positions at their entry price and saying so.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.sleeves import performance as perf   # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))
V2_DB = os.environ.get("V2_PAPER_DB", "/opt/opentrade/var/v2_paper.db")


def _quotes(market: str) -> dict:
    """Live prices for marking open positions. Best-effort by design."""
    try:
        from app import v2_live
        live = v2_live._live(market)
        try:
            live.update(v2_live._option_live())
        except Exception:
            pass
        return live
    except Exception as exc:
        print(f"(live quotes unavailable: {str(exc)[:60]} — marking at entry)\n",
              file=sys.stderr)
        return {}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--market", default="IN")
    ap.add_argument("--db", default=V2_DB)
    ap.add_argument("--since", default=None,
                    help="only trades entered on/after this date (YYYY-MM-DD)")
    ap.add_argument("--today", action="store_true", help="shorthand for --since today")
    ap.add_argument("--epoch", action="store_true",
                    help="only trades closed in the CURRENT book epoch")
    ap.add_argument("--no-quotes", action="store_true",
                    help="do not fetch live prices; mark positions at entry")
    a = ap.parse_args()

    since = a.since
    if a.today:
        since = datetime.now(IST).date().isoformat()

    if not os.path.exists(a.db):
        print(f"no paper book at {a.db}", file=sys.stderr)
        return 1

    con = perf.open_readonly(a.db)
    try:
        prices = {} if a.no_quotes else _quotes(a.market)
        print(perf.report(con, a.market, since=since, prices=prices,
                          epoch_only=a.epoch))
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
