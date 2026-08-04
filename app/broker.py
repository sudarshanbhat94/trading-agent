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
#
# ONE FILE PER USER. This was a single global broker.json with an owner_user_id
# field, which made "whose account is this" a value rather than a boundary:
# every read went through the same state, so a bug anywhere in the gating chain
# exposed one person's brokerage to another. A user id in the PATH cannot be
# got wrong by a missing WHERE clause.
STATE_DIR = os.getenv("BROKER_STATE_DIR", os.path.join(_ROOT, "var", "brokers"))
# Legacy single-account file, migrated on first read.
LEGACY_PATH = os.getenv("BROKER_STATE_PATH", os.path.join(_ROOT, "var", "broker.json"))


def _path(user_id):
    return os.path.join(STATE_DIR, f"{int(user_id)}.json")
API_BASE = os.getenv("UPSTOX_API_BASE_URL", "https://api.upstox.com/v2").rstrip("/")
# Orders go to the HFT host, which is what the place-order docs specify. Both
# hosts answered a probe identically, so this is not a bug fix — it is using the
# documented route for the one call that spends money, rather than assuming the
# general host will keep accepting it.
ORDER_BASE = os.getenv("UPSTOX_ORDER_BASE_URL", "https://api-hft.upstox.com/v2").rstrip("/")

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


def _migrate_legacy():
    """Move the old single-account file to its owner's per-user file, once.

    Without this the operator's working connection would silently vanish on
    deploy — and a broker that looks disconnected while a token still exists on
    disk is the worst of both.
    """
    try:
        if not os.path.exists(LEGACY_PATH):
            return
        with open(LEGACY_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        uid = data.get("owner_user_id")
        if uid is None:
            return
        dest = _path(uid)
        if not os.path.exists(dest):
            os.makedirs(STATE_DIR, exist_ok=True)
            with open(dest, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=1)
            os.chmod(dest, 0o600)
        os.replace(LEGACY_PATH, LEGACY_PATH + ".migrated")
    except (OSError, ValueError):
        pass


def _read(user_id) -> dict:
    _migrate_legacy()
    try:
        with open(_path(user_id), encoding="utf-8") as fh:
            data = json.load(fh)
        return {**DEFAULT, **(data if isinstance(data, dict) else {})}
    except (OSError, ValueError):
        return dict(DEFAULT)


def _write(user_id, state: dict) -> dict:
    os.makedirs(STATE_DIR, exist_ok=True)
    dest = _path(user_id)
    tmp = dest + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=1)
    os.chmod(tmp, 0o600)         # before the rename, so it is never briefly world-readable
    os.replace(tmp, dest)
    return state


def linked_users():
    """Every user id with a broker file. Used by the engine to fan orders out."""
    out = []
    try:
        for name in os.listdir(STATE_DIR):
            if name.endswith(".json"):
                try:
                    out.append(int(name[:-5]))
                except ValueError:
                    pass
    except OSError:
        pass
    return sorted(out)


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


def state(user_id, now=None) -> dict:
    """Redacted status for the UI. NEVER returns the token itself."""
    s = _read(user_id)
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


def configure(user_id, api_key=None, redirect_uri=None, budget=None,
              armed=None, kill_switch=None, allow_options=None) -> dict:
    s = _read(user_id)
    s["owner_user_id"] = int(user_id)   # the file IS the owner; kept for display
    if api_key is not None:
        s["api_key"] = str(api_key).strip()
    if redirect_uri is not None:
        s["redirect_uri"] = str(redirect_uri).strip()
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
    _write(user_id, s)
    return state(user_id)


def save_token(user_id, access_token: str, now=None) -> dict:
    """Store a token the USER obtained. Nothing here mints credentials."""
    token = str(access_token or "").strip()
    if not token:
        raise ValueError("empty access token")
    s = _read(user_id)
    now = now or datetime.now(IST)
    s["access_token"] = token
    s["token_saved_at"] = now.isoformat()
    s["expires_at"] = token_expiry(now=now).isoformat()
    s["connected"] = True
    _write(user_id, s)
    return state(user_id, now)


def disconnect(user_id) -> dict:
    """Drop the token AND disarm. Disconnecting must never leave a sleeve armed
    against a token that is about to be replaced."""
    s = _read(user_id)
    s.update(access_token="", connected=False, expires_at="", token_saved_at="",
             armed=False, kill_switch=True)
    _write(user_id, s)
    return state(user_id)


def auth_url(user_id, api_key=None, redirect_uri=None) -> str:
    """The Upstox login URL the OPERATOR opens in their own browser."""
    from urllib.parse import urlencode
    s = _read(user_id)
    key = str(api_key or s.get("api_key") or "").strip()
    redir = str(redirect_uri or s.get("redirect_uri") or "").strip()
    if not key:
        raise ValueError("save your Upstox API key first")
    if not redir:
        raise ValueError("save the exact redirect URI registered in your Upstox app")
    return (f"{API_BASE}/login/authorization/dialog?"
            + urlencode({"response_type": "code", "client_id": key, "redirect_uri": redir}))


def extract_code(pasted: str) -> str:
    """Accept the whole redirect URL, or just the code.

    The operator is copying out of a browser address bar on a page that shows a
    404, because nothing is listening at the redirect URI. Demanding they
    isolate the `code=` value by hand is a needless way to burn a single-use
    credential that expires in minutes — so take either form.
    """
    from urllib.parse import parse_qs, urlparse
    raw = str(pasted or "").strip().strip('"').strip("'")
    if not raw:
        return ""
    if "code=" in raw:
        # works for a full URL, a bare query string, or "code=xyz" on its own
        query = urlparse(raw).query or raw
        found = parse_qs(query).get("code") or []
        if found and found[0].strip():
            return found[0].strip()
    if "://" in raw or "?" in raw or "&" in raw:
        return ""                       # a URL that carried no code at all
    return raw


def exchange_code(user_id, code: str, api_secret: str, api_key=None,
                  redirect_uri=None, now=None) -> dict:
    """Swap the operator's one-time login code for a token.

    The secret is used for this call and NOT stored: it is only needed at
    exchange time, and a long-lived secret sitting in a file is a bigger prize
    than the daily token it produces.
    """
    import httpx
    s = _read(user_id)
    key = str(api_key or s.get("api_key") or "").strip()
    redir = str(redirect_uri or s.get("redirect_uri") or "").strip()
    code, secret = extract_code(code), str(api_secret or "").strip()
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
    return save_token(user_id, token, now=now)


def _token(user_id) -> str:
    return _read(user_id).get("access_token") or ""


def _headers(user_id) -> dict:
    return {"accept": "application/json",
            "Authorization": f"Bearer {_token(user_id)}"}


def funds(user_id) -> dict:
    import httpx
    r = httpx.get(f"{API_BASE}/user/get-funds-and-margin",
                  headers=_headers(user_id), params={"segment": "SEC"}, timeout=20)
    r.raise_for_status()
    return r.json() or {}


def positions(user_id) -> list:
    import httpx
    r = httpx.get(f"{API_BASE}/portfolio/short-term-positions",
                  headers=_headers(user_id), timeout=20)
    r.raise_for_status()
    return (r.json() or {}).get("data") or []


def holdings(user_id) -> list:
    """DELIVERY holdings. Separate endpoint from positions, and both are needed:
    orders placed with product='D' settle into holdings, while intraday and F&O
    sit in positions. Showing only one of them under-reports the account."""
    import httpx
    r = httpx.get(f"{API_BASE}/portfolio/long-term-holdings",
                  headers=_headers(user_id), timeout=20)
    r.raise_for_status()
    return (r.json() or {}).get("data") or []


def account_snapshot(user_id):
    """The REAL account: cash, what is held, and what it is worth right now.

    Returns None when the broker cannot be reached, which the caller must treat
    as "unknown" rather than "empty" — rendering a connected account as Rs 0
    because a request timed out would read as a wiped-out account.
    """
    try:
        eq = ((funds(user_id) or {}).get("data") or {}).get("equity") or {}
        cash = float(eq.get("available_margin") or 0.0)
        used = float(eq.get("used_margin") or 0.0)
        rows = []
        value = day_pnl = invested = 0.0
        for h in (holdings(user_id) or []):
            qty = float(h.get("quantity") or 0)
            if qty <= 0:
                continue
            ltp = float(h.get("last_price") or 0)
            avg = float(h.get("average_price") or 0)
            rows.append(dict(symbol=h.get("trading_symbol") or h.get("tradingsymbol") or "",
                             qty=qty, avg=round(avg, 2), ltp=round(ltp, 2),
                             value=round(qty * ltp, 2),
                             pnl=round(qty * (ltp - avg), 2),
                             pnl_pct=(round((ltp / avg - 1) * 100, 2) if avg else 0.0),
                             kind="holding"))
            value += qty * ltp
            invested += qty * avg
            day_pnl += float(h.get("day_change") or 0) * qty
        for p in (positions(user_id) or []):
            qty = float(p.get("quantity") or 0)
            if qty == 0:
                continue
            ltp = float(p.get("last_price") or 0)
            avg = float(p.get("average_price") or p.get("buy_price") or 0)
            rows.append(dict(symbol=p.get("trading_symbol") or p.get("tradingsymbol") or "",
                             qty=qty, avg=round(avg, 2), ltp=round(ltp, 2),
                             value=round(qty * ltp, 2),
                             pnl=round(float(p.get("pnl") or qty * (ltp - avg)), 2),
                             pnl_pct=(round((ltp / avg - 1) * 100, 2) if avg else 0.0),
                             kind="position"))
            value += qty * ltp
            invested += qty * avg
            day_pnl += float(p.get("day_buy_value") or 0) * 0  # day P&L comes from pnl below
        return dict(cash=round(cash, 2), used_margin=round(used, 2),
                    holdings_value=round(value, 2), invested=round(invested, 2),
                    equity=round(cash + value, 2),
                    unrealised=round(value - invested, 2),
                    day_pnl=round(day_pnl, 2),
                    positions=rows, n_positions=len(rows))
    except Exception:
        return None


def is_option(symbol: str) -> bool:
    """A STRIKE followed by CE/PE — not merely a symbol ending in those letters.

    RELIANCE ends in "CE". A naive suffix test classifies the most traded stock
    on the exchange as an option and blocks it while options are off; invert the
    switch and it would size a Rs 2,900 equity as if it were a lot. The digit is
    what distinguishes NIFTY26...24300CE from a company name.
    """
    import re
    return bool(re.search(r"\d(CE|PE)$", str(symbol or "").upper()))


def can_trade(order, user_id, st=None, market_is_open=True,
              day_notional=0.0, open_positions=0):
    """(ok, reason). Every lock, in order, with the reason named.

    Pure: takes the redacted state and returns a decision, so every refusal
    below is testable without a broker, a token or a network.
    """
    st = st if st is not None else state(user_id)
    notional = float(order.get("qty") or 0) * float(order.get("price") or 0)
    if st.get("kill_switch", True):
        return False, "kill switch engaged"
    if not st.get("armed"):
        return False, "live trading is not armed"
    # The STATE ITSELF is per-user now — st came from that user's own file — so
    # this is a consistency check, not the boundary. The boundary is the path.
    # Kept because a caller passing a mismatched pair is a bug worth refusing
    # rather than trading through.
    owner = st.get("owner_user_id")
    if user_id is None or (owner is not None and int(owner) != int(user_id)):
        return False, "broker state does not belong to this account"
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


# Upstox refuses a MARKET order with market_protection=0:
#   UDAPI1158 "Market orders are not allowed. Try placing an order with market
#              protection."
# It is a PERCENTAGE band around LTP beyond which the order will not fill, so 0
# reads as "accept any price" and is rejected. 5% is wide enough that a normal
# equity fill goes through and narrow enough to stop a fat-finger or a thin book
# filling far away from the price the decision was made at.
MARKET_PROTECTION_PCT = float(os.getenv("UPSTOX_MARKET_PROTECTION", "5"))


def place_order(user_id, instrument_key, qty, side="BUY", price=0.0, product="D",
                order_type="MARKET", tag="openstocks"):
    """Send ONE order. Callers must have passed can_trade first.

    This function does not re-check the locks — it is deliberately dumb, so the
    gate lives in exactly one place and cannot be half-applied by a caller that
    forgot a parameter.
    """
    import httpx
    # market_protection was missing. Upstox accepted the payload without it, but
    # it is in the documented body and defaults are not a thing to inherit
    # silently on the call that spends money.
    payload = dict(quantity=int(qty), product=product, validity="DAY",
                   price=float(price or 0), tag=tag, instrument_token=instrument_key,
                   order_type=order_type, transaction_type=side.upper(),
                   disclosed_quantity=0, trigger_price=0, is_amo=False,
                   market_protection=(MARKET_PROTECTION_PCT
                                      if order_type == "MARKET" else 0))
    r = httpx.post(f"{ORDER_BASE}/order/place", headers={**_headers(user_id),
                   "Content-Type": "application/json"}, json=payload, timeout=25)
    ok = r.status_code < 400
    body = {}
    try:
        body = r.json() or {}
    except ValueError:
        body = {"raw": r.text[:400]}
    return dict(ok=ok, status=r.status_code, order_id=(body.get("data") or {}).get("order_id"),
                response=body)
