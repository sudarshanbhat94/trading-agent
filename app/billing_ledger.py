"""Owned immutable payment receipts. Entitlements commit with confirmation.

Manual bank confirmation is explicitly attributed to the reviewing operator.
It is not an automatic payment verification or a tax invoice. Reused payment
references cannot grant a second account or extend a subscription twice.
"""
from datetime import datetime,timezone
import hashlib
import json
from .paper_ledger import minor


def ensure_schema(con):
    con.execute('''CREATE TABLE IF NOT EXISTS subscription_receipts(
      id INTEGER PRIMARY KEY,user_id INTEGER NOT NULL,request_id INTEGER NOT NULL UNIQUE,
      provider TEXT NOT NULL,payment_reference TEXT NOT NULL,amount_minor INTEGER NOT NULL,
      currency TEXT NOT NULL,plan TEXT NOT NULL,starts_at TEXT NOT NULL,ends_at TEXT NOT NULL,
      confirmed_by TEXT NOT NULL,confirmed_at TEXT NOT NULL,fingerprint TEXT NOT NULL,
      UNIQUE(provider,payment_reference))''')
    for action in ('UPDATE','DELETE'):
        con.execute(f"CREATE TRIGGER IF NOT EXISTS immutable_subscription_receipt_{action.lower()} BEFORE {action} "
                    "ON subscription_receipts BEGIN SELECT RAISE(ABORT,'immutable payment receipt'); END")


def confirm(con,request,*,payment_reference,by,starts_at,ends_at):
    if not con.in_transaction:raise RuntimeError('Receipt and entitlement must share a serialized transaction')
    if not isinstance(payment_reference,str) or not payment_reference.strip() or not isinstance(by,str) or not by.strip():
        raise ValueError('Verified payment reference and named reviewer required')
    payment_reference=payment_reference.strip()
    if len(payment_reference)>80:raise ValueError('Payment reference too long')
    from .plans import PACKAGES
    if request['requested_plan'] not in PACKAGES or minor(request['amount'])<=0:raise ValueError('Invalid paid plan request')
    payload=[request['user_id'],request['id'],'manual-bank-confirmation',payment_reference,
             minor(request['amount']),'INR',request['requested_plan'],starts_at,ends_at,by]
    digest=hashlib.sha256(json.dumps(payload,sort_keys=True).encode()).hexdigest()
    prior=con.execute('SELECT request_id,fingerprint FROM subscription_receipts WHERE provider=? AND payment_reference=?',
                      ('manual-bank-confirmation',payment_reference)).fetchone()
    if prior:raise ValueError('Payment reference already used; do not grant another entitlement')
    cur=con.execute('INSERT INTO subscription_receipts(user_id,request_id,provider,payment_reference,amount_minor,currency,plan,starts_at,ends_at,confirmed_by,confirmed_at,fingerprint) '
                    'VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',(*payload,datetime.now(timezone.utc).isoformat(),digest))
    return cur.lastrowid


def report(con,uid):
    rows=con.execute('SELECT id,request_id,provider,payment_reference,amount_minor,currency,plan,starts_at,ends_at,confirmed_at '
                     'FROM subscription_receipts WHERE user_id=? ORDER BY id DESC LIMIT 50',(uid,)).fetchall()
    fields=('id','request_id','provider','payment_reference','amount_minor','currency','plan','starts_at','ends_at','confirmed_at')
    return dict(scope='owned-payment-receipts',receipts=[dict(zip(fields,r)) for r in rows],
                automatic_renewal=False,tax_invoice=False,
                note='Manual receipts record a bank reconciliation attestation; no payment gateway or refund transfer is implied')
