"""Entitlement applies to new risk, never to an owner's safety controls."""
from . import plans


def may_open(user):
    if not user or not user.get("active", True):
        return False
    plan = plans.effective(user.get("account_plan"), user.get("trial_ends_at"),
                           plan_expires_at=user.get("plan_expires_at"))
    return plans.allows(plan, "broker_connect")


def read_user(con, uid):
    row = con.execute("SELECT active,account_plan,trial_ends_at,plan_expires_at "
                      "FROM users WHERE id=?", (uid,)).fetchone()
    return dict(zip(("active", "account_plan", "trial_ends_at", "plan_expires_at"), row)) if row else None
