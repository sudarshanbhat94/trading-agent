"""Subscription tiers, and what each one may reach.

ONE table, used by the admin panel, the API gate and the UI. The alternative —
a plan check written inline at each route — is the failure this codebase keeps
producing: four separate index gates, each individually plausible, each
silently disabling a whole lane, and no single place to read the truth from.

`users.account_plan` has existed since the schema was written and enforced
NOTHING. Every logged-in user reached every endpoint, so free and paid were the
same product.

TIERS follow the capability boundaries that already exist in the engine rather
than invented feature flags, which is what makes them defensible:

  watch  SIGNAL_ONLY   see the calls and the catalyst feed
  paper  AUTO_PAPER    own paper book, full analytics, all equity lanes
  auto   AUTO_LIVE     connect a broker, index options, exports

`admin` is a ROLE, not a plan. An admin manages other people's plans; it does
not change what their own book does. Keeping them separate is what lets the
operator hold a real account and still administer — the two questions are
"what may this account reach" and "may this account manage others", and
conflating them is how admin accidentally becomes a billing tier.
"""
from __future__ import annotations

# Order matters: a plan grants everything its predecessors grant.
#
# `free` is not a package — it is where an account sits before it subscribes and
# after a trial lapses. It reaches only the always-open routes (own profile,
# health, the plans screen), which is what lets a lapsed user still log in and
# pay rather than meeting a locked door with no way through it.
TIERS = ("free", "watch", "paper", "auto")
PACKAGES = ("watch", "paper", "auto")       # the three that are actually sold
DEFAULT_TIER = "free"
# What a NEW account starts on. Separate from DEFAULT_TIER on purpose: that one
# is the fallback for an unreadable stored value and stays at `free` so garbage
# fails closed, while this is a product decision about what a signup is worth.
# Everyone therefore keeps the signals and the catalyst feed for good, and only
# loses the paper book and the analytics when the trial lapses.
SIGNUP_TIER = "watch"
# The live book predates plans, so every existing row reads 'standard'. It maps
# to the top tier deliberately — the ten accounts already on it are the
# operator's own people, and silently demoting them on deploy would be a
# migration that breaks working accounts to satisfy a naming change.
LEGACY_ALIASES = {"standard": "auto", "": DEFAULT_TIER}

FEATURES = {
    # feature key -> the lowest tier that may reach it
    "signals": "watch",
    "catalysts": "watch",
    "index_call": "watch",
    "telegram_alerts": "paper",     # Pro and above
    "market_internals": "paper",
    "option_chain": "paper",
    "index_candles": "paper",
    "paper_book": "paper",
    "history_full": "paper",
    "manual_trade": "paper",
    "index_options": "auto",
    "broker_connect": "auto",
    "export": "auto",
}

LABELS = {"free": "Free", "watch": "Starter", "paper": "Pro", "auto": "Elite"}
# Monthly, in rupees. PRICES is what is charged; LIST_PRICES is what it is shown
# against. The list price must be a real intended price, not decoration — a
# permanent "discount" that never expires is the kind of thing that reads as
# dishonest once a subscriber notices it never changes.
PRICES = {"free": 0, "watch": 199, "paper": 499, "auto": 999}
LIST_PRICES = {"watch": 499, "paper": 999, "auto": 1999}
ONE_LINERS = {
    "free": "Sign in and see your plan.",
    "watch": "Signals and the live NSE catalyst feed.",
    "paper": "Your own paper book, market internals, option chain and full history.",
    "auto": "Index options, broker connect and exports.",
}
HIGHLIGHTS = {
    "watch": ["Daily CE/PE index call", "Live NSE announcements", "Top movers and radar"],
    "paper": ["Everything in Starter", "Your own ₹1L paper book",
              "Market internals: breadth, FII, VIX", "Option chain and index candles",
              "Full trade history and per-lane stats",
              "Telegram alerts"],
    "auto": ["Everything in Pro", "Index options auto-trading",
             "Connect your own broker", "Data export"],
}

# TRIAL. A new account gets the middle tier for a week, then falls back to the
# free one — it does not get locked out. Someone who tried the product and did
# not pay should still see the catalyst feed and the calls; turning them into a
# 402 wall converts a warm lead into a churned one, and costs nothing to avoid.
TRIAL_DAYS = 7
TRIAL_TIER = "paper"

# A PAID PLAN RUNS OUT. Without this an approval was permanent: pay Rs 999 once
# and hold Elite forever, which is a one-off sale wearing the word
# "subscription". The billing period is what makes it recurring.
SUBSCRIPTION_DAYS = 30
# How close to the end the UI starts saying so. Long enough to act on, short
# enough not to nag from day one.
RENEWAL_WARN_DAYS = 5


def subscription_state(account_plan, plan_expires_at, now=None):
    """{active, days_left, expires, expired} for a PAID plan.

    A free-tier account has nothing to expire, so it is never "expired" — that
    word is reserved for a subscription that lapsed, which is a different thing
    to say to somebody and drives different words on screen.
    """
    from datetime import datetime, timezone
    plan = normalize(account_plan)
    if plan == DEFAULT_TIER or plan == SIGNUP_TIER:
        return dict(active=False, days_left=0, expires=None, expired=False, paid=False)
    if not plan_expires_at:
        # A paid plan with no end date predates this feature. Treat it as
        # running rather than lapsed: demoting real subscribers to fix a schema
        # gap is the worse of the two mistakes.
        return dict(active=True, days_left=None, expires=None, expired=False, paid=True)
    try:
        ends = datetime.fromisoformat(str(plan_expires_at).replace("Z", "+00:00"))
        if ends.tzinfo is None:
            ends = ends.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return dict(active=True, days_left=None, expires=None, expired=False, paid=True)
    remaining = (ends - (now or datetime.now(timezone.utc))).total_seconds()
    return dict(active=remaining > 0, expired=remaining <= 0, paid=True,
                days_left=max(0, int(-(-remaining // 86400))), expires=ends.isoformat())


def trial_state(trial_ends_at, now=None):
    """{active, days_left, ends} for an account's trial.

    A NULL end date means no trial, not an expired one — the ten accounts that
    predate this feature must keep the plan they already have rather than being
    treated as lapsed triallists and demoted.
    """
    from datetime import datetime, timezone
    if not trial_ends_at:
        return dict(active=False, days_left=0, ends=None, had_trial=False)
    try:
        ends = datetime.fromisoformat(str(trial_ends_at).replace("Z", "+00:00"))
        if ends.tzinfo is None:
            ends = ends.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return dict(active=False, days_left=0, ends=None, had_trial=False)
    now = now or datetime.now(timezone.utc)
    remaining = (ends - now).total_seconds()
    return dict(active=remaining > 0,
                # ceil, so the last partial day still reads "1 day left" rather
                # than "0 days left" while the trial is genuinely still running
                days_left=max(0, int(-(-remaining // 86400))),
                ends=ends.isoformat(), had_trial=True)


def effective(account_plan, trial_ends_at=None, now=None, plan_expires_at=None):
    """The tier this account may actually use RIGHT NOW.

    Three inputs, in this order:
      * a LAPSED subscription drops back to SIGNUP_TIER — not to `free`, so a
        former subscriber keeps the signals and can still see how to renew;
      * an active trial lifts the account to TRIAL_TIER;
      * but a trial never DEMOTES: a subscriber on `auto` whose trial window
        happens to still be open must not be dropped to `paper` by it.
    """
    stored = normalize(account_plan)
    sub = subscription_state(stored, plan_expires_at, now)
    if sub["paid"] and sub["expired"]:
        stored = SIGNUP_TIER
    trial = trial_state(trial_ends_at, now)
    if trial["active"] and rank(TRIAL_TIER) > rank(stored):
        return TRIAL_TIER
    return stored

# WHICH ROUTE NEEDS WHICH FEATURE — one table, enforced by a single dependency
# on the router, exactly like authentication. Annotating 37 routes one at a time
# is how you end up with the hole: this codebase has already shipped four
# separate index gates, each individually plausible, each silently disabling a
# whole lane, with no single place to read the truth from.
#
# `None` means "any logged-in user" — free, and deliberately explicit rather
# than absent, so the table doubles as the inventory of what is free.
ROUTE_FEATURES = {
    "/v2": None, "/v2/": None,
    "/v2/api/me": None,
    "/v2/api/health": None,
    "/v2/api/engine-status": None,
    "/v2/api/ticker": None,
    "/v2/api/indices": None,
    "/v2/api/catalysts": "catalysts",
    "/v2/api/index-call": "index_call",
    "/v2/api/movers": "signals",
    "/v2/api/search": "signals",
    "/v2/api/watch": "signals",
    "/v2/api/sectors": "signals",
    "/v2/api/preopen": "signals",
    # paper tier
    "/v2/api/overview": "paper_book",
    "/v2/api/positions": "paper_book",
    "/v2/api/portfolio": "paper_book",
    "/v2/api/stream": "paper_book",
    "/v2/api/stats": "history_full",
    "/v2/api/orders": "history_full",
    "/v2/api/trades": "history_full",
    "/v2/api/attribution": "history_full",
    "/v2/api/stock/{symbol}": "market_internals",
    "/v2/api/index-candles": "index_candles",
    "/v2/api/index-detail": "market_internals",
    "/v2/api/watchlist": "paper_book",
    "/v2/api/watchlist/{symbol}": "paper_book",
    "/v2/api/alerts": "paper_book",
    "/v2/api/alerts/{aid}": "paper_book",
    "/v2/api/buy": "manual_trade",
    "/v2/api/sell": "manual_trade",
    "/v2/api/reset": "manual_trade",
    "/v2/api/positions/{pid}/exit": "manual_trade",
    # auto tier
    "/v2/api/index-settings": "index_options",
    # admin routes carry their own ROLE check; a plan must not gate
    # administration, or an admin on a low tier could not manage anyone
    "/v2/api/upgrade": None,
    "/v2/api/pay-qr": None,
    "/v2/api/payment-settings": None,
    "/v2/api/admin/requests": None,
    "/v2/api/admin/requests/{rid}": None,
    "/v2/api/admin/users": None,
    "/v2/api/admin/users/{uid}": None,
    "/v2/api/admin/jobs": None,
}


def feature_for_path(path: str):
    """The feature a route needs, or None if it is free.

    An UNMAPPED path returns None — free — on purpose. Denying it would mean a
    newly added route breaks for every user until someone notices, which is a
    self-inflicted outage; a test enumerates the live router and fails when a
    path is missing from the table, so the gap is caught in CI instead of in
    production. Fail open here, catch it there.
    """
    return ROUTE_FEATURES.get(path)


def normalize(value) -> str:
    """Map whatever is stored to a real tier.

    Unknown values fall to the DEFAULT rather than raising: a typo in the admin
    panel should cost a user features, not lock them out of a page with a 500.
    """
    plan = str(value or "").strip().lower()
    plan = LEGACY_ALIASES.get(plan, plan)
    return plan if plan in TIERS else DEFAULT_TIER


def rank(plan) -> int:
    return TIERS.index(normalize(plan))


def allows(plan, feature: str) -> bool:
    """True if `plan` reaches `feature`.

    An UNKNOWN feature is denied. Fail closed: a feature key that is misspelled
    at the call site would otherwise be free for everyone, which is the silent
    hole this module exists to prevent.
    """
    needed = FEATURES.get(feature)
    if needed is None:
        return False
    return rank(plan) >= rank(needed)


def features_for(plan) -> dict:
    """Everything this plan may reach, for the UI to hide what it cannot use."""
    return {key: allows(plan, key) for key in FEATURES}
