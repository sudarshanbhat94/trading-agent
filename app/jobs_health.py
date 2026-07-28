"""Background-job health: is each data pipeline actually producing anything?

Every serious bug found in this codebase recently was a pipeline that failed
SILENTLY. The candle ingester deadlocked and still exited 0 after writing
700k rows. Alerts were only evaluated while a browser was open. The
announcements poller had no backfill, so an outage lost a day permanently.
None of those raised, and none showed up as an error anywhere.

The existing `/v2/api/health` checks quote and candle freshness, but its daily
candle tolerance is FIVE DAYS — wide enough that a feed lagging by one or two
trading sessions, which is what actually happened, passes as healthy.

So this assesses each pipeline against a threshold sized to how often it is
supposed to produce, and says plainly when something is behind. Pure functions
over observations the caller has already gathered, so the judgement is testable
without a database.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

_LOG = logging.getLogger("openstocks.jobs_health")

IST = timezone(timedelta(hours=5, minutes=30))

# How stale each pipeline may be before it is a problem, in hours, and what it
# is supposed to do. Sized to the cadence, not to a convenient round number.
PIPELINES = {
    "quotes": {"label": "live quote feed", "max_age_hours": 0.25,
               "note": "polls continuously while the market is open"},
    "daily_candles": {"label": "daily candle ingest", "max_age_hours": 30,
                      "note": "one bar per trading session"},
    "catalysts": {"label": "NSE announcements", "max_age_hours": 24,
                  "note": "material filings arrive on every trading day"},
    "shareholding": {"label": "shareholding patterns", "max_age_hours": 24 * 120,
                     "note": "quarterly; companies file over several weeks"},
    "engine": {"label": "engine heartbeat", "max_age_hours": 0.5,
               "note": "written by exit_monitor every cycle"},
}


def _age_hours(timestamp, now=None):
    """Hours since an ISO timestamp, or None when it cannot be read."""
    if not timestamp:
        return None
    now = now or datetime.now(timezone.utc)
    text = str(timestamp).strip().replace("Z", "+00:00")
    for candidate in (text, text.replace(" ", "T")):
        try:
            moment = datetime.fromisoformat(candidate)
        except ValueError:
            continue
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=IST)   # bare timestamps here are IST
        return round((now - moment).total_seconds() / 3600.0, 2)
    # A bare date (the shareholding quarter) still tells us the age.
    try:
        day = datetime.strptime(text[:10], "%Y-%m-%d").replace(tzinfo=IST)
    except ValueError:
        return None
    return round((now - day).total_seconds() / 3600.0, 2)


def assess(observations, now=None, market_open=True):
    """Judge each pipeline. `observations` maps a pipeline key to
    {"latest": <timestamp or None>, "rows": <int or None>}.

    Returns a list of checks, worst first, so a UI can show the problem without
    sorting. An ABSENT pipeline is reported as unknown rather than failing:
    shareholding does not exist until its ingester is deployed, and calling
    that "broken" would cry wolf.
    """
    checks = []
    for key, spec in PIPELINES.items():
        observed = observations.get(key) if isinstance(observations, dict) else None
        if not isinstance(observed, dict):
            checks.append({"pipeline": key, "label": spec["label"], "status": "unknown",
                           "detail": "no data source found", "age_hours": None,
                           "rows": None, "note": spec["note"]})
            continue

        age = _age_hours(observed.get("latest"), now)
        rows = observed.get("rows")
        limit = spec["max_age_hours"]

        # The quote feed and the engine only produce while the market is open;
        # judging them at 2am would report a false outage every night.
        if key in ("quotes", "engine") and not market_open:
            status, detail = "idle", "market closed"
        elif age is None:
            status, detail = "unknown", "no timestamp"
        elif age <= limit:
            status, detail = "ok", f"{age:.2f}h old"
        else:
            status = "stale"
            detail = f"{age:.1f}h old, expected under {limit}h"
        checks.append({"pipeline": key, "label": spec["label"], "status": status,
                       "detail": detail, "age_hours": age, "rows": rows,
                       "note": spec["note"]})

    order = {"stale": 0, "unknown": 1, "idle": 2, "ok": 3}
    checks.sort(key=lambda c: (order.get(c["status"], 9), c["pipeline"]))
    return checks


def summarise(checks):
    """One-line verdict over the checks."""
    checks = list(checks or [])
    stale = [c for c in checks if c["status"] == "stale"]
    unknown = [c for c in checks if c["status"] == "unknown"]
    if stale:
        return {"ok": False, "headline": f"{len(stale)} pipeline(s) behind",
                "stale": [c["pipeline"] for c in stale],
                "unknown": [c["pipeline"] for c in unknown]}
    if unknown:
        return {"ok": True, "headline": f"{len(unknown)} pipeline(s) not reporting",
                "stale": [], "unknown": [c["pipeline"] for c in unknown]}
    return {"ok": True, "headline": "all pipelines current", "stale": [], "unknown": []}
