"""Live broker (Upstox) — real money, one account, many locks.

EVERYTHING HERE DEFAULTS TO OFF. Connecting a broker and arming it to trade are
two separate decisions, and neither is implied by deploying this file. A fresh
install has no token, `armed=False`, and a kill switch that must be explicitly
released.

WHY THE LOCKS ARE THIS HEAVY
The paper book's measured record is a realised loss: -Rs 1,443 across 22 closed
equity trades at a 27% win rate, and the options book's +Rs 27,301 is six trades
dominated by one session. The exit study says the live exits give up 42% of the
entry edge. This module is therefore written on the assumption that it will lose
money, and its job is to bound how fast — not to get orders out quickly.

THE LOCKS, in the order can_trade checks them:
  1. kill switch engaged            -> refuse (default state on a new install)
  2. not armed                      -> refuse
  3. caller is not the OWNER user   -> refuse (one numeric user id, not a role)
  4. no token / token expired       -> refuse
  5. market closed                  -> refuse
  6. instrument class not enabled   -> refuse (options OFF: see LOT_COSTS)
  7. order notional over cap        -> refuse
  8. day's notional over cap        -> refuse
  9. budget already committed       -> refuse

TOKENS ARE NEVER WRITTEN TO THE REPO. They live in var/, which is gitignored,
with 0600 permissions. This repository is public.

UPSTOX TOKENS EXPIRE DAILY at ~03:30 IST and the standard plan issues no refresh
token, so an unattended bot is not possible: somebody must complete an
interactive login each morning. `state()["stale"]` is how the UI says so, and
rule 4 is what stops the engine placing orders against a dead token and
reporting success.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# var/ is gitignored. If this ever moves, it must move somewhere that stays out
# of git — the repository is public and a committed token is a funded account
# handed to whoever reads it first.
STATE_PATH = os.getenv("BROKER_STATE_PATH", os.path.join(_ROOT, "var", "broker.json"))
API_BASE = os.getenv("UPSTOX_API_BASE_URL", "https://api.upstox.com/v2").rstrip("/")

# The live sleeve's own money. Deliberately NOT read from BUDGET: the paper book
# is Rs 1,00,000 of imaginary capital and this is Rs 10,000 of real capital, and
# the two numbers must never be able to drift into each other.
LIVE_BUDGET = float(os.getenv("LIVE_BUDGET", "10000"))
MAX_ORDER_PCT = 0.35            # no single order over 35% of the sleeve
MAX_DAY_PCT = 1.0               # total notional placed in one day, as a multiple
MAX_OPEN_POSITIONS = 3

# Index options are OFF and this is arithmetic, not caution. Measured from the
# operator's own book, one lot costs:
#     NIFTY 24300 CE       65 x Rs 87.75  = Rs  5,704
#     BANKNIFTY 57500 CE   30 x Rs 691.00 = Rs 20,730
#     FINNIFTY 26100 CE    60 x Rs 461.15 = Rs 27,669
# Two of those exceed the entire sleeve, and the cheapest is 57% of it in a
# single position. There is no position size that makes this sensible at
# Rs 10,000, so the switch stays off until the budget can afford a lot.
LOT_COSTS = {"NIFTY": 5704, "BANKNIFTY": 20730, "FINNIFTY": 27669}
OPTIONS_MIN_BUDGET = 50000.0

DEFAULT = dict(
    connected=False, access_token="", token_saved_at="", expires_at="",
    api_key="", redirect_uri="",
    owner_user_id=None,          # ONE numeric user id. A role is not enough:
                                 # "admin" is a set that can grow by accident.
    armed=False,                 # arming is a separate act from connecting
    kill_switch=True,            # engaged by default; release is deliberate
    allow_options=False,
    budget=LIVE_BUDGET,
)


def _read() -> dict:
    try:
        with open(STATE_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        return {**DEFAULT, **(data if isinstance(data, dict) else {})}
    except (OSError, ValueError):
        return dict(DEFAULT)


def _write(state: dict) -> dict:
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=1)
    os.chmod(tmp, 0o600)         # before the rename, so it is never briefly world-readable
    os.replace(tmp, STATE_PATH)
    return state


def token_expiry(saved_at=None, now=None):
    """Upstox tokens die at 03:30 IST, not N hours after issue.

    Computing it from the clock rather than trusting an `expires_in` is what
    makes a token issued at 23:00 correctly expire four hours later instead of
    appearing valid all of the next day.
    """
    now = now or datetime.now(IST)
    cut = now.replace(hour=3, minute=30, second=0, microsecond=0)
    if now >= cut:
        cut += timedelta(days=1)
    return cut


def state(now=None) -> dict:
    """Redacted status for the UI. NEVER returns the token itself."""
    s = _read()
    now = now or datetime.now(IST)
    expires = s.get("expires_at") or ""
    stale = True
    if s.get("access_token") and expires:
        try:
            stale = datetime.fromisoformat(expires) <= now
        except ValueError:
            stale = True
    elif s.get("access_token"):
        stale = False
    return dict(
        connected=bool(s.get("access_token")), stale=stale,
        expires_at=expires, token_saved_at=s.get("token_saved_at") or "",
        # a fingerprint, so the UI can show WHICH key is configured without
        # ever putting the key on a screen or in a log
        api_key_hint=(s["api_key"][:4] + "…" + s["api_key"][-4:]) if s.get("api_key") else "",
        redirect_uri=s.get("redirect_uri") or "",
        owner_user_id=s.get("owner_user_id"), armed=bool(s.get("armed")),
        kill_switch=bool(s.get("kill_switch", True)),
        allow_options=bool(s.get("allow_options")),
        budget=float(s.get("budget") or LIVE_BUDGET),
        max_order=round(float(s.get("budget") or LIVE_BUDGET) * MAX_ORDER_PCT, 2),
        options_blocked_reason=(
            "" if float(s.get("budget") or LIVE_BUDGET) >= OPTIONS_MIN_BUDGET
            else f"one index-option lot costs Rs {min(LOT_COSTS.values()):,}–"
                 f"Rs {max(LOT_COSTS.values()):,}; this sleeve holds "
                 f"Rs {float(s.get('budget') or LIVE_BUDGET):,.0f}"),
        live_ready=bool(s.get("access_token")) and not stale
        and bool(s.get("armed")) and not bool(s.get("kill_switch", True)),
    )


def configure(api_key=None, redirect_uri=None, owner_user_id=None, budget=None,
              armed=None, kill_switch=None, allow_options=None) -> dict:
    s = _read()
    if api_key is not None:
        s["api_key"] = str(api_key).strip()
    if redirect_uri is not None:
        s["redirect_uri"] = str(redirect_uri).strip()
    if owner_user_id is not None:
        s["owner_user_id"] = int(owner_user_id)
    if budget is not None:
        s["budget"] = max(0.0, float(budget))
    if armed is not None:
        s["armed"] = bool(armed)
    if kill_switch is not None:
        s["kill_switch"] = bool(kill_switch)
    if allow_options is not None:
        # Refused rather than accepted-and-ignored: a switch that silently does
        # nothing is worse than one that says no.
        s["allow_options"] = bool(allow_options) and float(s.get("budget") or 0) >= OPTIONS_MIN_BUDGET
    _write(s)
    return state()


def save_token(access_token: str, now=None) -> dict:
    """Store a token the USER obtained. Nothing here mints credentials."""
    token = str(access_token or "").strip()
    if not token:
        raise ValueError("empty access token")
    s = _read()
    now = now or datetime.now(IST)
    s["access_token"] = token
    s["token_saved_at"] = now.isoformat()
    s["expires_at"] = token_expiry(now=now).isoformat()
    s["connected"] = True
    _write(s)
    return state(now)


def disconnect() -> dict:
    """Drop the token AND disarm. Disconnecting must never leave a sleeve armed
    against a token that is about to be replaced."""
    s = _read()
    s.update(access_token="", connected=False, expires_at="", token_saved_at="",
             armed=False, kill_switch=True)
    _write(s)
    return state()


def auth_url(api_key=None, redirect_uri=None) -> str:
    """The Upstox login URL the OPERATOR opens in their own browser."""
    from urllib.parse import urlencode
    s = _read()
    key = str(api_key or s.get("api_key") or "").strip()
    redir = str(redirect_uri or s.get("redirect_uri") or "").strip()
    if not key:
        raise ValueError("save your Upstox API key first")
    if not redir:
        raise ValueError("save the exact redirect URI registered in your Upstox app")
    return (f"{API_BASE}/login/authorization/dialog?"
            + urlencode({"response_type": "code", "client_id": key, "redirect_uri": redir}))


def exchange_code(code: str, api_secret: str, api_key=None, redirect_uri=None,
                  now=None) -> dict:
    """Swap the operator's one-time login code for a token.

    The secret is used for this call and NOT stored: it is only needed at
    exchange time, and a long-lived secret sitting in a file is a bigger prize
    than the daily token it produces.
    """
    import httpx
    s = _read()
    key = str(api_key or s.get("api_key") or "").strip()
    redir = str(redirect_uri or s.get("redirect_uri") or "").strip()
    code, secret = str(code or "").strip(), str(api_secret or "").strip()
    if not (key and redir and code and secret):
        raise ValueError("api key, secret, redirect URI and code are all required")
    r = httpx.post(f"{API_BASE}/login/authorization/token",
                   headers={"accept": "application/json",
                            "Content-Type": "application/x-www-form-urlencoded"},
                   data={"code": code, "client_id": key, "client_secret": secret,
                         "redirect_uri": redir, "grant_type": "authorization_code"},
                   timeout=20)
    if r.status_code >= 400:
        raise RuntimeError(f"Upstox refused the code ({r.status_code}): {r.text[:300]}")
    token = (r.json() or {}).get("access_token")
    if not token:
        raise RuntimeError("Upstox returned no access_token")
    return save_token(token, now=now)


def _token() -> str:
    return _read().get("access_token") or ""


def _headers() -> dict:
    return {"accept": "application/json", "Authorization": f"Bearer {_token()}"}


def funds() -> dict:
    import httpx
    r = httpx.get(f"{API_BASE}/user/get-funds-and-margin",
                  headers=_headers(), params={"segment": "SEC"}, timeout=20)
    r.raise_for_status()
    return r.json() or {}


def positions() -> list:
    import httpx
    r = httpx.get(f"{API_BASE}/portfolio/short-term-positions",
                  headers=_headers(), timeout=20)
    r.raise_for_status()
    return (r.json() or {}).get("data") or []


def is_option(symbol: str) -> bool:
    """A STRIKE followed by CE/PE — not merely a symbol ending in those letters.

    RELIANCE ends in "CE". A naive suffix test classifies the most traded stock
    on the exchange as an option and blocks it while options are off; invert the
    switch and it would size a Rs 2,900 equity as if it were a lot. The digit is
    what distinguishes NIFTY26...24300CE from a company name.
    """
    import re
    return bool(re.search(r"\d(CE|PE)$", str(symbol or "").upper()))


def can_trade(order, st=None, user_id=None, market_is_open=True,
              day_notional=0.0, open_positions=0):
    """(ok, reason). Every lock, in order, with the reason named.

    Pure: takes the redacted state and returns a decision, so every refusal
    below is testable without a broker, a token or a network.
    """
    st = st or state()
    notional = float(order.get("qty") or 0) * float(order.get("price") or 0)
    if st.get("kill_switch", True):
        return False, "kill switch engaged"
    if not st.get("armed"):
        return False, "live trading is not armed"
    owner = st.get("owner_user_id")
    if owner is None or user_id is None or int(owner) != int(user_id):
        return False, "this account is not the live-trading owner"
    if not st.get("connected"):
        return False, "no broker token — connect Upstox"
    if st.get("stale"):
        return False, "broker token expired — log in to Upstox again"
    if not market_is_open:
        return False, "market is closed"
    if is_option(order.get("symbol")) and not st.get("allow_options"):
        return False, st.get("options_blocked_reason") or "options are disabled"
    if notional <= 0:
        return False, "order has no value"
    if notional > float(st.get("max_order") or 0):
        return False, (f"order Rs {notional:,.0f} exceeds the per-order cap "
                       f"Rs {float(st.get('max_order') or 0):,.0f}")
    budget = float(st.get("budget") or 0)
    if day_notional + notional > budget * MAX_DAY_PCT:
        return False, f"daily notional cap Rs {budget * MAX_DAY_PCT:,.0f} reached"
    if open_positions >= MAX_OPEN_POSITIONS:
        return False, f"already holding {open_positions} live positions (max {MAX_OPEN_POSITIONS})"
    return True, "ok"


def place_order(instrument_key, qty, side="BUY", price=0.0, product="D",
                order_type="MARKET", tag="openstocks"):
    """Send ONE order. Callers must have passed can_trade first.

    This function does not re-check the locks — it is deliberately dumb, so the
    gate lives in exactly one place and cannot be half-applied by a caller that
    forgot a parameter.
    """
    import httpx
    payload = dict(quantity=int(qty), product=product, validity="DAY",
                   price=float(price or 0), tag=tag, instrument_token=instrument_key,
                   order_type=order_type, transaction_type=side.upper(),
                   disclosed_quantity=0, trigger_price=0, is_amo=False)
    r = httpx.post(f"{API_BASE}/order/place", headers={**_headers(),
                   "Content-Type": "application/json"}, json=payload, timeout=25)
    ok = r.status_code < 400
    body = {}
    try:
        body = r.json() or {}
    except ValueError:
        body = {"raw": r.text[:400]}
    return dict(ok=ok, status=r.status_code, order_id=(body.get("data") or {}).get("order_id"),
                response=body)
