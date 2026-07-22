"""Telegram alerts bot for OpenStocks.

Lets a user self-link their Telegram (via a /start deep-link code) and receive
alerts when the engine buys or sells. The bot token comes from the
TELEGRAM_BOT_TOKEN env var (create a bot with @BotFather). If the token is
unset, every function is a safe no-op and the UI shows "not configured".

Storage: a telegram_accounts table in the v2 paper DB (small, WAL, low churn).
Linking: user clicks Connect -> we mint a code -> they open t.me/<bot>?start=CODE
and press Start -> a long-poll loop maps their chat_id to the code -> linked.
No public webhook needed.
"""
from __future__ import annotations

import json
import os
import secrets
import sqlite3
import threading
import time
import urllib.parse
import urllib.request

V2_DB = os.environ.get("V2_PAPER_DB", "/opt/opentrade/var/v2_paper.db")
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
_API = "https://api.telegram.org/bot%s/%s"
_uname = {"v": None, "t": 0.0}


def enabled() -> bool:
    return bool(TOKEN)


def _db():
    c = sqlite3.connect(V2_DB, timeout=20)
    c.execute("PRAGMA busy_timeout=6000")
    c.execute("PRAGMA journal_mode=WAL")
    return c


def ensure_schema():
    c = _db()
    c.execute("""CREATE TABLE IF NOT EXISTS telegram_accounts(
        user_id INTEGER PRIMARY KEY, chat_id TEXT, username TEXT, link_code TEXT,
        alerts_buy INTEGER DEFAULT 1, alerts_sell INTEGER DEFAULT 1, linked_at TEXT)""")
    c.commit()
    c.close()


def _call(method, params=None, timeout=35):
    if not TOKEN:
        return None
    url = _API % (TOKEN, method)
    data = urllib.parse.urlencode(params or {}).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception:
        return None


def bot_username():
    if not TOKEN:
        return None
    if _uname["v"] and time.time() - _uname["t"] < 3600:
        return _uname["v"]
    r = _call("getMe", {}, timeout=10)
    if r and r.get("ok"):
        _uname.update(v=r["result"].get("username"), t=time.time())
        return _uname["v"]
    return None


def send(chat_id, text):
    return _call("sendMessage", {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                                 "disable_web_page_preview": "true"}, timeout=10)


def start_link(user_id: int):
    """Mint a fresh link code for the user; return (code, deep_link)."""
    ensure_schema()
    code = secrets.token_urlsafe(8)
    c = _db()
    c.execute("INSERT INTO telegram_accounts(user_id, link_code) VALUES(?, ?) "
              "ON CONFLICT(user_id) DO UPDATE SET link_code=excluded.link_code", (user_id, code))
    c.commit()
    c.close()
    bu = bot_username()
    link = "https://t.me/%s?start=%s" % (bu, code) if bu else None
    return code, link


def status(user_id: int) -> dict:
    ensure_schema()
    c = _db()
    row = c.execute("SELECT chat_id, username, alerts_buy, alerts_sell FROM telegram_accounts WHERE user_id=?",
                    (user_id,)).fetchone()
    c.close()
    return dict(configured=enabled(), bot=bot_username(), linked=bool(row and row[0]),
                username=(row[1] if row else None),
                alerts_buy=(bool(row[2]) if row else True),
                alerts_sell=(bool(row[3]) if row else True))


def set_prefs(user_id: int, buy: bool, sell: bool):
    c = _db()
    c.execute("UPDATE telegram_accounts SET alerts_buy=?, alerts_sell=? WHERE user_id=?",
              (1 if buy else 0, 1 if sell else 0, user_id))
    c.commit()
    c.close()


def unlink(user_id: int):
    c = _db()
    c.execute("DELETE FROM telegram_accounts WHERE user_id=?", (user_id,))
    c.commit()
    c.close()


def _handle_start(code, chat_id, username):
    c = _db()
    row = c.execute("SELECT user_id FROM telegram_accounts WHERE link_code=?", (code,)).fetchone() if code else None
    if row:
        c.execute("UPDATE telegram_accounts SET chat_id=?, username=?, link_code=NULL, "
                  "linked_at=datetime('now') WHERE user_id=?", (str(chat_id), username, row[0]))
        c.commit()
        c.close()
        send(chat_id, "✅ <b>OpenStocks connected!</b>\nYou'll get an alert here whenever the AI "
                      "buys or sells. Turn buy/sell alerts on or off in Account → settings.")
    else:
        c.close()
        send(chat_id, "Hi! To link your account, open OpenStocks → Account → <b>Connect Telegram</b>, "
                      "then tap the link it gives you.")


def poll_loop():
    if not TOKEN:
        return
    ensure_schema()
    offset = 0
    while TOKEN:
        try:
            r = _call("getUpdates", {"offset": offset, "timeout": 30}, timeout=40)
            if r and r.get("ok"):
                for upd in r["result"]:
                    offset = upd["update_id"] + 1
                    msg = upd.get("message") or {}
                    text = (msg.get("text") or "").strip()
                    chat = msg.get("chat") or {}
                    frm = msg.get("from") or {}
                    if text.startswith("/start"):
                        parts = text.split(maxsplit=1)
                        code = parts[1].strip() if len(parts) > 1 else ""
                        uname = frm.get("username") or frm.get("first_name") or "user"
                        _handle_start(code, chat.get("id"), uname)
        except Exception:
            time.sleep(5)
        time.sleep(1)


def notify_trade(side, symbol, qty, price, market, pnl_pct=None, strategy=None):
    """Fan out a buy/sell alert to every linked user who enabled that side."""
    if not TOKEN:
        return
    try:
        c = _db()
        col = "alerts_buy" if str(side).upper() == "BUY" else "alerts_sell"
        rows = c.execute("SELECT chat_id FROM telegram_accounts "
                         "WHERE chat_id IS NOT NULL AND %s=1" % col).fetchall()
        c.close()
    except Exception:
        return
    if not rows:
        return
    ccy = "₹" if market == "IN" else "$"
    try:
        pr = ("%.2f" % float(price)).rstrip("0").rstrip(".")
    except Exception:
        pr = str(price)
    if str(side).upper() == "BUY":
        tag = ("  ·  " + strategy) if strategy else ""
        txt = "\U0001f7e2 <b>Bought %s</b>\n%s @ %s%s%s" % (symbol, qty, ccy, pr, tag)
    else:
        pnl = ("  ·  %s%.2f%%" % ("+" if (pnl_pct or 0) >= 0 else "", pnl_pct)) if pnl_pct is not None else ""
        txt = "\U0001f534 <b>Sold %s</b>\n%s @ %s%s%s" % (symbol, qty, ccy, pr, pnl)
    for (chat_id,) in rows:
        try:
            send(chat_id, txt)
        except Exception:
            pass


_started = False


def start_background():
    global _started
    if _started or not TOKEN:
        return
    _started = True
    threading.Thread(target=poll_loop, daemon=True, name="telegram-poll").start()
