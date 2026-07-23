"""Per-user Telegram alerts — fully self-serve, no server-wide token / admin.

Each user brings their OWN bot (created via @BotFather) and pastes its token in
Account settings. Linking:
  1. user pastes their bot token  -> we validate it with getMe
  2. user opens their bot and presses Start
  3. user taps "Verify" -> we read getUpdates on THEIR token, capture their
     chat_id, and store it -> linked
On every buy/sell the engine sends the alert via each linked user's own
token + chat_id. No public webhook, no long-poll loop.

Storage: telegram_accounts(user_id, bot_token, bot_username, chat_id,
alerts_buy, alerts_sell, linked_at) in the v2 paper DB.
"""
from __future__ import annotations

import json
import os
import sqlite3
import urllib.parse
import urllib.request

V2_DB = os.environ.get("V2_PAPER_DB", "/opt/opentrade/var/v2_paper.db")
_API = "https://api.telegram.org/bot%s/%s"


def _db():
    c = sqlite3.connect(V2_DB, timeout=20)
    c.execute("PRAGMA busy_timeout=6000")
    c.execute("PRAGMA journal_mode=WAL")
    return c


def ensure_schema():
    c = _db()
    c.execute("""CREATE TABLE IF NOT EXISTS telegram_accounts(
        user_id INTEGER PRIMARY KEY, bot_token TEXT, bot_username TEXT, chat_id TEXT,
        alerts_buy INTEGER DEFAULT 1, alerts_sell INTEGER DEFAULT 1, linked_at TEXT)""")
    # additive migrations (older installs / new alert types)
    for col, decl in (("bot_token", "TEXT"), ("bot_username", "TEXT"),
                      ("alerts_radar", "INTEGER DEFAULT 1"), ("alerts_summary", "INTEGER DEFAULT 1"),
                      ("alerts_price", "INTEGER DEFAULT 1")):
        try:
            c.execute("ALTER TABLE telegram_accounts ADD COLUMN %s %s" % (col, decl))
        except Exception:
            pass
    c.commit()
    c.close()


def _api(token, method, params=None, timeout=12):
    if not token:
        return None
    url = _API % (token, method)
    data = urllib.parse.urlencode(params or {}).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception:
        return None


def validate_token(token):
    r = _api(token, "getMe", {}, timeout=10)
    if r and r.get("ok"):
        return r["result"].get("username")
    return None


def send(token, chat_id, text):
    return _api(token, "sendMessage", {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                                       "disable_web_page_preview": "true"}, timeout=10)


def _money(v):
    try:
        return "{:,.2f}".format(float(v)).rstrip("0").rstrip(".")
    except Exception:
        return str(v)


def save_token(user_id: int, token: str):
    """Validate the user's bot token and store it. Returns bot username or None."""
    ensure_schema()
    token = (token or "").strip()
    uname = validate_token(token)
    if not uname:
        return None
    c = _db()
    c.execute("INSERT INTO telegram_accounts(user_id, bot_token, bot_username, chat_id) VALUES(?,?,?,NULL) "
              "ON CONFLICT(user_id) DO UPDATE SET bot_token=excluded.bot_token, "
              "bot_username=excluded.bot_username, chat_id=NULL", (user_id, token, uname))
    c.commit()
    c.close()
    return uname


def verify(user_id: int):
    """Read the user's bot getUpdates, capture their chat_id from a recent message."""
    ensure_schema()
    c = _db()
    row = c.execute("SELECT bot_token FROM telegram_accounts WHERE user_id=?", (user_id,)).fetchone()
    c.close()
    if not row or not row[0]:
        return None
    token = row[0]
    r = _api(token, "getUpdates", {"limit": 10, "timeout": 0}, timeout=10)
    if not r or not r.get("ok"):
        return None
    chat_id = None
    for upd in reversed(r.get("result") or []):
        msg = upd.get("message") or upd.get("edited_message") or {}
        chat = msg.get("chat") or {}
        if chat.get("id") is not None:
            chat_id = str(chat["id"])
            break
    if not chat_id:
        return None
    c = _db()
    c.execute("UPDATE telegram_accounts SET chat_id=?, linked_at=datetime('now') WHERE user_id=?", (chat_id, user_id))
    c.commit()
    c.close()
    send(token, chat_id, "✅ <b>OpenStocks connected!</b>\nYou'll get an alert here whenever the AI "
                         "buys or sells. Manage buy/sell alerts in Account settings.")
    return chat_id


def status(user_id: int) -> dict:
    ensure_schema()
    c = _db()
    row = c.execute("SELECT bot_token, bot_username, chat_id, alerts_buy, alerts_sell, alerts_radar, alerts_summary, alerts_price "
                    "FROM telegram_accounts WHERE user_id=?", (user_id,)).fetchone()
    c.close()
    has_token = bool(row and row[0])
    bot = row[1] if row else None
    return dict(has_token=has_token, bot=bot, linked=bool(row and row[2]),
                deep_link=("https://t.me/%s" % bot) if bot else None,
                alerts_buy=(bool(row[3]) if row else True),
                alerts_sell=(bool(row[4]) if row else True),
                alerts_radar=(bool(row[5]) if row else True),
                alerts_summary=(bool(row[6]) if row else True),
                alerts_price=(bool(row[7]) if row else True))


def send_test(user_id: int) -> bool:
    """Send a test alert to the user's linked Telegram."""
    ensure_schema()
    c = _db()
    row = c.execute("SELECT bot_token, chat_id FROM telegram_accounts WHERE user_id=?", (user_id,)).fetchone()
    c.close()
    if not row or not row[0] or not row[1]:
        return False
    r = send(row[0], row[1], "\U0001f514 <b>OpenStocks</b> · Test alert\n"
                             "Your Telegram is connected. You'll receive buy, sell, radar, "
                             "price-alert and daily-summary messages here.")
    return bool(r and r.get("ok"))


def set_prefs(user_id: int, buy: bool, sell: bool, radar: bool = True, summary: bool = True, price: bool = True):
    c = _db()
    c.execute("UPDATE telegram_accounts SET alerts_buy=?, alerts_sell=?, alerts_radar=?, alerts_summary=?, alerts_price=? WHERE user_id=?",
              (1 if buy else 0, 1 if sell else 0, 1 if radar else 0, 1 if summary else 0, 1 if price else 0, user_id))
    c.commit()
    c.close()


def notify_alert(symbol, market, kind, value, price):
    """A user-set watchlist price alert fired — fan out to opted-in users."""
    try:
        rows = _recipients("alerts_price")
    except Exception:
        return
    if not rows:
        return
    ccy = "₹" if market == "IN" else "$"
    nm = "India" if market == "IN" else "US"
    if kind == "pct":
        line = "<b>Moved:</b> %s%% or more\n<b>Now:</b> %s%s" % (value, ccy, _money(price))
    else:
        dirn = "Rose above" if kind == "above" else "Fell below"
        line = "<b>%s:</b> %s%s\n<b>Now:</b> %s%s" % (dirn, ccy, _money(value), ccy, _money(price))
    txt = ("\U0001f514 <b>OpenStocks</b> · Price alert\n"
           "<b>%s</b> · %s\n%s") % (symbol, nm, line)
    for token, chat_id in rows:
        try:
            send(token, chat_id, txt)
        except Exception:
            pass


def _recipients(pref_col):
    ensure_schema()
    c = _db()
    rows = c.execute("SELECT bot_token, chat_id FROM telegram_accounts "
                     "WHERE bot_token IS NOT NULL AND chat_id IS NOT NULL AND %s=1" % pref_col).fetchall()
    c.close()
    return rows


def notify_radar(items, market="IN"):
    """items: list of {'symbol','note'} the engine is watching to buy next."""
    try:
        rows = _recipients("alerts_radar")
    except Exception:
        return
    if not rows or not items:
        return
    lines = "\n".join("• <b>%s</b>%s" % (it["symbol"], (" — " + it["note"].title()) if it.get("note") else "")
                      for it in items)
    nm = "India" if market == "IN" else "US"
    txt = ("\U0001f440 <b>OpenStocks</b> · Radar — %s\n"
           "Stocks the AI is watching to buy next:\n%s") % (nm, lines)
    for token, chat_id in rows:
        try:
            send(token, chat_id, txt)
        except Exception:
            pass


def notify_summary(text):
    """text: pre-formatted daily progress summary (HTML)."""
    try:
        rows = _recipients("alerts_summary")
    except Exception:
        return
    for token, chat_id in rows:
        try:
            send(token, chat_id, text)
        except Exception:
            pass


def unlink(user_id: int):
    c = _db()
    c.execute("DELETE FROM telegram_accounts WHERE user_id=?", (user_id,))
    c.commit()
    c.close()


def notify_trade(side, symbol, qty, price, market, pnl_pct=None, strategy=None):
    """Fan a buy/sell alert out to every linked user via their own bot."""
    try:
        ensure_schema()
        c = _db()
        col = "alerts_buy" if str(side).upper() == "BUY" else "alerts_sell"
        rows = c.execute("SELECT bot_token, chat_id FROM telegram_accounts "
                         "WHERE bot_token IS NOT NULL AND chat_id IS NOT NULL AND %s=1" % col).fetchall()
        c.close()
    except Exception:
        return
    if not rows:
        return
    ccy = "₹" if market == "IN" else "$"
    nm = "India" if market == "IN" else "US"
    pr = _money(price)
    if str(side).upper() == "BUY":
        strat = ("\n<b>Strategy:</b> %s" % strategy.replace("_", " ").title()) if strategy else ""
        txt = ("\U0001f7e2 <b>OpenStocks</b> · Buy\n"
               "<b>%s</b> · %s\n"
               "<b>Qty:</b> %s @ %s%s%s") % (symbol, nm, qty, ccy, pr, strat)
    else:
        pnl = ("\n<b>Return:</b> %s%.2f%%" % ("+" if (pnl_pct or 0) >= 0 else "", pnl_pct)) if pnl_pct is not None else ""
        txt = ("\U0001f534 <b>OpenStocks</b> · Sell\n"
               "<b>%s</b> · %s\n"
               "<b>Qty:</b> %s @ %s%s%s") % (symbol, nm, qty, ccy, pr, pnl)
    for token, chat_id in rows:
        try:
            send(token, chat_id, txt)
        except Exception:
            pass


def start_background():
    """No background loop needed in the per-user model (chat_id captured on demand)."""
    return
