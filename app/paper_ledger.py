"""Immutable, balanced currency postings for the personal paper book.

Existing book calculations remain authoritative. A first-use opening snapshot
is labelled as a snapshot, never fabricated historical fills. All later cash
movements commit in the position/trade transaction. Live contract notes and
derivative settlement require their own ledger; this does not certify either.
"""
from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime, timezone
import hashlib
import json


def minor(value):
    amount = Decimal(str(value))
    if not amount.is_finite():
        raise ValueError("ledger amount must be finite")
    return int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def ensure_schema(con):
    con.execute("CREATE TABLE IF NOT EXISTS paper_ledger_events("
                "id INTEGER PRIMARY KEY,user_id INTEGER NOT NULL,market TEXT NOT NULL,"
                "epoch TEXT NOT NULL,event_key TEXT NOT NULL,kind TEXT NOT NULL,"
                "reference TEXT NOT NULL,created_at TEXT NOT NULL,fingerprint TEXT NOT NULL,"
                "provenance TEXT NOT NULL,UNIQUE(user_id,market,epoch,event_key))")
    con.execute("CREATE TABLE IF NOT EXISTS paper_ledger_postings("
                "event_id INTEGER NOT NULL,account TEXT NOT NULL,amount_minor INTEGER NOT NULL,"
                "PRIMARY KEY(event_id,account),FOREIGN KEY(event_id) REFERENCES paper_ledger_events(id))")
    for table in ("paper_ledger_events", "paper_ledger_postings"):
        for action in ("UPDATE", "DELETE"):
            con.execute(f"CREATE TRIGGER IF NOT EXISTS immutable_{table}_{action.lower()} "
                        f"BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT,'immutable paper ledger'); END")


def post(con, uid, market, epoch, event_key, kind, reference, postings, provenance="runtime-paper"):
    if not con.in_transaction:
        raise RuntimeError("ledger postings require the book transaction")
    entries = sorted(postings.items())
    if len(entries) < 2 or any(isinstance(v, bool) or not isinstance(v, int) for _, v in entries) or sum(v for _, v in entries):
        raise ValueError("currency postings must balance exactly")
    signature = hashlib.sha256(json.dumps([kind,str(reference),provenance,entries],sort_keys=True).encode()).hexdigest()
    old = con.execute("SELECT id,fingerprint FROM paper_ledger_events WHERE user_id=? AND market=? "
                      "AND epoch=? AND event_key=?",(uid,market,epoch,event_key)).fetchone()
    if old:
        if old[1] != signature:
            raise ValueError("ledger identity cannot be rebound")
        return old[0]
    cursor = con.execute("INSERT INTO paper_ledger_events(user_id,market,epoch,event_key,kind,reference,"
                         "created_at,fingerprint,provenance) VALUES(?,?,?,?,?,?,?,?,?)",
                         (uid,market,epoch,event_key,kind,str(reference),datetime.now(timezone.utc).isoformat(),signature,provenance))
    con.executemany("INSERT INTO paper_ledger_postings VALUES(?,?,?)",[(cursor.lastrowid,k,v) for k,v in entries])
    return cursor.lastrowid


def anchor(con, uid, market, epoch, cash, inventory):
    if con.execute("SELECT 1 FROM paper_ledger_events WHERE user_id=? AND market=? AND epoch=?",
                   (uid,market,epoch)).fetchone():
        return
    free, held = minor(cash), minor(inventory)
    post(con,uid,market,epoch,"opening","OPENING_SNAPSHOT",epoch,
         {"cash":free,"inventory":held,"opening_equity":-free-held},"observed-opening-snapshot")


def entry(con, uid, market, epoch, pid, notional, fee):
    principal, charge = minor(notional), minor(fee)
    return post(con,uid,market,epoch,f"position:{pid}:entry","ENTRY",pid,
                {"cash":-principal-charge,"inventory":principal,"charges":charge})


def exit(con, uid, market, epoch, pid, notional, proceeds, fee):
    principal, gross, charge = minor(notional), minor(proceeds), minor(fee)
    return post(con,uid,market,epoch,f"position:{pid}:exit","EXIT",pid,
                {"cash":gross-charge,"inventory":-principal,"charges":charge,"price_pnl":principal-gross})


def report(con, uid, market, epoch, expected_cash=None):
    rows = con.execute("SELECT p.account,SUM(p.amount_minor) FROM paper_ledger_postings p "
                       "JOIN paper_ledger_events e ON e.id=p.event_id WHERE e.user_id=? AND e.market=? "
                       "AND e.epoch=? GROUP BY p.account",(uid,market,epoch)).fetchall()
    balances = dict(rows)
    count = con.execute("SELECT COUNT(*) FROM paper_ledger_events WHERE user_id=? AND market=? AND epoch=?",
                        (uid,market,epoch)).fetchone()[0]
    gap = balances.get("cash",0)-minor(expected_cash) if count and expected_cash is not None else None
    return dict(status="uninitialised" if not count else "mismatch" if gap not in (None,0) else "ok",
                events=count,balances_minor=balances,cash_difference_minor=gap,
                balanced=sum(balances.values())==0,scope="personal-paper",currency_scale=100,
                market=market,currency={"IN":"INR","US":"USD"}.get(market),
                note="Opening snapshot is not reconstructed trade history; live settlement is not certified")
