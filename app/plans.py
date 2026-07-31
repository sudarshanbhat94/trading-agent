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
TIERS = ("watch", "paper", "auto")
DEFAULT_TIER = "watch"
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

LABELS = {"watch": "Watch", "paper": "Paper", "auto": "Auto"}


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
