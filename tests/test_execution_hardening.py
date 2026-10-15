"""Independent fault/replay fixtures; no broker credentials or real orders."""
import json
import os
import sqlite3
import tempfile
import unittest
from tests.contract_storage_fixtures import ContractStorageCase
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi import Request, Response
from app import auth, books, broker, broker_reconciliation as recon, execution_outbox as outbox, v2_live, worker_fencing as fencing
from app.config import Settings
from app.credential_vault import seal, unseal
from app.instrument_catalog import Instrument, InstrumentError, import_snapshot, resolve, upstox_contract
from app.execution_ports import route_for


def quotes(price=100):
    return {"TEST":dict(price=price,ts=datetime.now(timezone.utc).isoformat())}


class CatalogueTest(ContractStorageCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:"); self.addCleanup(self.con.close)
        self.now = datetime.now(timezone.utc)
        self.spec = Instrument("NSE","NSE_EQ","EQUITY","TEST","INR","ISIN_TEST","EQ",
                               tick_size="0.05",freeze_quantity=10000,settlement="T+1")

    def load(self,items,now=None):
        at=now or self.now
        return import_snapshot(self.con,items,provider="upstox",source="independent-fixture",
                               source_day=at.date().isoformat(),observed_at=at.isoformat(),now=self.now)

    def test_same_symbol_requires_venue_and_series(self):
        other=replace(self.spec,venue="BSE",segment="BSE_EQ")
        self.assertNotEqual(other.id,self.spec.id)
        self.load([(self.spec,"NSE_EQ|ISIN_TEST"),(other,"BSE_EQ|ISIN_TEST")])
        with self.assertRaises(InstrumentError):resolve(self.con,symbol="TEST",now=self.now)
        actual,key=resolve(self.con,symbol="TEST",venue="BSE",segment="BSE_EQ",now=self.now)
        self.assertEqual(actual,other);self.assertEqual(key,"BSE_EQ|ISIN_TEST")
        self.assertNotEqual(self.spec.id,replace(self.spec,series="BE").id)
        with self.assertRaises(InstrumentError):route_for("upstox",other)

    def test_symbol_lot_change_does_not_change_identity(self):
        self.assertEqual(self.spec.id,replace(self.spec,symbol="RENAMED",lot_size=10).id)

    def test_expiry_strike_right_are_separate_contracts(self):
        option=Instrument("NSE","NSE_FO","OPTION","SAME","INR","NIFTY","FO",
                          "2026-10-29","25000","CE","NSE_INDEX|Nifty 50",50,"0.05",1800,"CASH")
        self.assertNotEqual(option.id,replace(option,right="PE").id)
        self.assertNotEqual(option.id,replace(option,strike="25100").id)
        self.assertNotEqual(option.id,replace(option,expiry="2026-11-26").id)
        for qty,px in [(1,10),(1850,10),(50,10.03)]:
            with self.subTest(qty=qty),self.assertRaises(InstrumentError):
                option.validate_order(qty,px,now=self.now,expiry_cutoff=self.now+timedelta(days=1))
        option.validate_order(50,10,now=self.now,expiry_cutoff=self.now+timedelta(days=1))
        with self.assertRaises(InstrumentError):option.validate_order(50,10,now=self.now)
        with self.assertRaises(InstrumentError):route_for("upstox",option)

    def test_stale_future_duplicate_source_refused_without_partial_import(self):
        self.load([(self.spec,"key")],self.now-timedelta(days=3))
        with self.assertRaises(InstrumentError):resolve(self.con,symbol="TEST",now=self.now)
        with self.assertRaises(InstrumentError):self.load([(self.spec,"key")],self.now+timedelta(seconds=1))
        with self.assertRaises(InstrumentError):self.load([(self.spec,"one"),(self.spec,"two")])
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM instrument_snapshots").fetchone()[0],1)

    def test_raw_provider_metadata_cannot_certify_order_units(self):
        row=dict(exchange="NSE",segment="NSE_EQ",instrument_type="EQ",isin="ISIN_TEST",
                 instrument_key="NSE_EQ|ISIN_TEST",trading_symbol="TEST",lot_size=1,tick_size=5,freeze_quantity=100000)
        spec,_=upstox_contract(row)
        self.assertIsNone(spec.tick_size);self.assertEqual(spec.series,"UNKNOWN")
        with self.assertRaises(InstrumentError):spec.validate_order(1,100)
        with self.assertRaises(InstrumentError):upstox_contract(dict(row,lot_size=1.5))

    def test_sync_retains_all_venues_and_reports_quarantine(self):
        from scripts.sync_instrument_catalog import sync
        row=dict(exchange="NSE",segment="NSE_EQ",instrument_type="EQ",isin="ISIN_TEST",
                 instrument_key="NSE_EQ|ISIN_TEST",trading_symbol="TEST",lot_size=1)
        data=[row,dict(row,exchange="BSE",segment="BSE_EQ",instrument_key="BSE_EQ|ISIN_TEST"),{}]
        result=sync(self.con,json.dumps(data).encode(),self.now.date().isoformat(),self.now.isoformat())
        self.assertEqual((result["imported"],result["quarantined"]),(2,1))
        self.assertFalse(result["execution_enabled"])


class DeliveryTest(ContractStorageCase):
    def setUp(self):
        self.con=sqlite3.connect(":memory:");v2_live.ensure_schema(self.con);self.addCleanup(self.con.close)
        self.addCleanup(fencing.ACTIVE.set,None)
        self.addCleanup(patch.stopall)
        patch.object(books,"subscribers",return_value=[]).start()
        patch.object(broker,"linked_users",return_value=[]).start()

    def enter(self):
        return v2_live.record_entry(self.con,"IN","mean_reversion","TEST",
                                   datetime.now().date().isoformat(),100,20,99,110,0,.5,None)

    def test_entry_and_outbox_rollback_together(self):
        with patch.object(outbox,"enqueue",side_effect=RuntimeError("disk fault")),self.assertRaises(RuntimeError):self.enter()
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM v2_positions").fetchone()[0],0)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM execution_outbox").fetchone()[0],0)

    def test_close_is_atomic_once_and_no_network_inside_transaction(self):
        with patch.object(v2_live,"_live_mirror_entry",side_effect=AssertionError("network")),patch.object(v2_live,"_live_mirror_exit",side_effect=AssertionError("network")):
            self.assertTrue(self.enter())
            pid=self.con.execute("SELECT id FROM v2_positions").fetchone()[0]
            pnl=v2_live.record_exit(self.con,"IN",pid,datetime.now().date().isoformat(),110,20,"target")
            self.assertEqual(v2_live.record_exit(self.con,"IN",pid,datetime.now().date().isoformat(),90,20,"stop"),pnl)
        self.assertFalse(self.con.in_transaction)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM v2_positions").fetchone()[0],0)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM v2_trades").fetchone()[0],1)
        self.assertEqual(outbox.drain(self.con),2)

    def test_retry_replays_same_event_and_retains_failure(self):
        outbox.enqueue(self.con,"stable","test",dict(intent="same"));self.con.commit()
        def fail(con,topic,payload):
            self.assertFalse(con.in_transaction);raise TimeoutError("private upstream response must not be retained")
        self.assertEqual(outbox.drain(self.con,fail,now=1000),0)
        self.assertEqual(outbox.drain(self.con,lambda *a:None,now=1029),0)
        self.assertEqual(outbox.drain(self.con,lambda *a:None,now=1031),1)
        row=self.con.execute("SELECT attempts,status,last_error FROM execution_outbox").fetchone()
        self.assertEqual(row,(2,"done",None))
        self.assertNotIn("private",self.con.execute("SELECT detail FROM execution_incidents").fetchone()[0])

    def test_delivery_refuses_uncommitted_book_transaction(self):
        outbox.enqueue(self.con,"not-committed","test",{})
        with self.assertRaises(RuntimeError):outbox.drain(self.con,lambda *a:None)
        self.con.rollback()

    def test_delivery_lease_takeover_does_not_report_old_success(self):
        outbox.enqueue(self.con,"takeover","test",{});self.con.commit()
        def take_over(con,*a):
            con.execute("UPDATE execution_outbox SET lease_token='new-worker',lease_until=1500");con.commit()
        self.assertEqual(outbox.drain(self.con,take_over,now=1000),0)
        self.assertEqual(self.con.execute("SELECT status FROM execution_outbox").fetchone()[0],'pending')
        with self.assertRaises(ValueError):outbox.enqueue(self.con,"takeover","test",dict(different=True))

    def test_source_closed_before_delivery_never_resurrects(self):
        self.enter();pid=self.con.execute("SELECT id FROM v2_positions").fetchone()[0]
        v2_live.record_exit(self.con,"IN",pid,datetime.now().date().isoformat(),110,20,"target")
        with patch.object(books,"subscribers",return_value=[2]),patch.object(books,"buy",side_effect=AssertionError("late entry")):
            self.assertEqual(outbox.drain(self.con),2)

    def test_personal_partial_failure_retries_without_duplicate_close(self):
        for uid in (1,2):self.assertGreater(books.buy(self.con,uid,"IN","manual","TEST",100,20,99,110,src_id=7,quotes=quotes()),0)
        sell=books.sell
        def flaky(con,uid,*args,**kw):
            if uid==2:raise sqlite3.OperationalError("busy")
            return sell(con,uid,*args,**kw)
        outbox.enqueue(self.con,"exit-seven","house_exit",dict(src_id=7,market="IN",symbol="TEST",price=110,reason="target"));self.con.commit()
        with patch.object(books,"sell",side_effect=flaky):self.assertEqual(outbox.drain(self.con,now=1000),0)
        self.assertEqual(outbox.drain(self.con,now=1031),1)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM user_trades").fetchone()[0],2)

    def test_paper_retry_returns_original_fill_after_close_and_rejects_key_rebinding(self):
        kw=dict(stop=99,target=110,request_key="paper-identity",quotes=quotes())
        qty=books.buy(self.con,1,"IN","manual","TEST",100,20,**kw)
        self.assertEqual(qty,20);books.sell(self.con,1,"IN","TEST",110)
        self.assertEqual(books.buy(self.con,1,"IN","manual","TEST",101,20,**kw),20)
        self.assertEqual(books.entry_receipt(self.con,1,"IN","paper-identity")["entry"],100)
        self.assertEqual(books.positions(self.con,1),[])
        with self.assertRaises(ValueError):books.buy(self.con,1,"IN","manual","OTHER",100,20,**kw)

    def test_worker_takeover_fences_old_writer_and_outbox(self):
        token=fencing.acquire(self.con,"worker-one",now=1000)
        self.assertIsNone(fencing.acquire(self.con,"worker-two",now=1010))
        replacement=fencing.acquire(self.con,"worker-two",now=1201)
        self.assertGreater(replacement[1],token[1])
        fencing.ACTIVE.set(token)
        with self.assertRaises(RuntimeError):self.enter()
        with self.assertRaises(RuntimeError):books.buy(self.con,1,"IN","manual","TEST",100,20,99,110)
        with self.assertRaises(RuntimeError):outbox.drain(self.con)


class ReconciliationTest(ContractStorageCase):
    def setUp(self):
        self.con=sqlite3.connect(":memory:");v2_live.ensure_schema(self.con);self.addCleanup(self.con.close)
        self.now=datetime.now(timezone.utc)
        self.con.execute("INSERT INTO v2_live_orders(ts,user_id,market,symbol,instrument_key,side,qty,price,product,status,broker_order_id,filled_qty,average_price) "
                         "VALUES(?,1,'IN','TEST','NSE_EQ|ISIN_TEST','BUY',20,100,'D','filled','order-one',20,100)",(self.now.isoformat(),));self.con.commit()
        self.data=dict(positions=[dict(instrument_token="NSE_EQ|ISIN_TEST",product="D",quantity=20,overnight_quantity=0)],holdings=[],
                       funds=dict(data=dict(equity=dict(available_margin=8000))),
                       trades=[dict(trade_id="one",order_id="order-one",instrument_token="NSE_EQ|ISIN_TEST",transaction_type="BUY",product="D",quantity=20)])

    def review(self,**overrides):return recon.reconcile(self.con,1,**dict(self.data,**overrides),checked_at=self.now.timestamp())

    def test_complete_actual_evidence_is_ready_only_for_its_account_and_interval(self):
        self.assertEqual(self.review()["status"],"ok")
        self.assertTrue(recon.ready(self.con,1,self.now.timestamp()+119))
        self.assertFalse(recon.ready(self.con,1,self.now.timestamp()+121))
        self.assertFalse(recon.ready(self.con,2,self.now.timestamp()))

    def test_missing_actual_trades_does_not_look_like_reconciled(self):
        self.assertEqual(self.review(trades=[])["status"],"mismatch")

    def test_external_holdings_never_adopted(self):
        result=self.review(holdings=[dict(instrument_token="NSE_EQ|EXTERNAL",quantity=5,t1_quantity=0)])
        self.assertEqual(result["status"],"mismatch");self.assertFalse(result["external_adopted"])
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM v2_live_orders").fetchone()[0],1)

    def test_carried_delivery_not_counted_twice(self):
        result=self.review(positions=[dict(instrument_token="NSE_EQ|ISIN_TEST",product="D",quantity=20,overnight_quantity=20)],
                           holdings=[dict(instrument_token="NSE_EQ|ISIN_TEST",quantity=20,t1_quantity=0)])
        self.assertEqual(result["status"],"ok")

    def test_incomplete_or_duplicate_inventory_and_funds_fail_closed(self):
        for overrides in [dict(positions=None),dict(funds={}),dict(funds=dict(data=dict(equity=dict(available_margin=float('nan'))))),
                          dict(positions=[dict(instrument_token="NSE_EQ|ISIN_TEST",product="D",quantity=20)]),
                          dict(trades=self.data['trades']*2)]:
            with self.subTest(overrides=overrides):self.assertEqual(self.review(**overrides)["status"],"unknown")
        for body in ({},{'data':None},{'data':{}},None):
            with self.assertRaises(ValueError):broker._inventory_rows(body)


class VaultSessionTest(ContractStorageCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.state_dir=str(Path(self.tmp.name)/"brokers")
        self.env=patch.dict(os.environ,{"BROKER_VAULT_KEY_PATH":str(Path(self.tmp.name)/"private/key")})
        self.env.start();self.addCleanup(self.env.stop)

    def test_credentials_encrypted_account_bound_and_tamper_evident(self):
        data=dict(access_token="fixture-not-a-real-token",owner_user_id=1)
        encrypted=seal(self.state_dir,1,data)
        self.assertNotIn(data['access_token'],json.dumps(encrypted))
        self.assertEqual(unseal(self.state_dir,1,encrypted),data)
        with self.assertRaises(ValueError):unseal(self.state_dir,2,encrypted)
        from cryptography.fernet import InvalidToken
        with self.assertRaises(InvalidToken):unseal(self.state_dir,1,dict(encrypted,ciphertext="tampered"))
        self.assertEqual(Path(os.environ['BROKER_VAULT_KEY_PATH']).stat().st_mode&0o777,0o600)

    def test_concurrent_key_creation_never_overwrites_the_winner(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            envelopes=list(pool.map(lambda n:seal(self.state_dir,n,dict(owner_user_id=n)),range(8)))
        for n,envelope in enumerate(envelopes):self.assertEqual(unseal(self.state_dir,n,envelope)['owner_user_id'],n)

    def test_legacy_state_migration_is_private_and_bad_key_visible(self):
        Path(self.state_dir).mkdir()
        path=Path(self.state_dir)/'1.json';path.write_text(json.dumps(dict(owner_user_id=1,access_token='fixture-token')))
        with patch.object(broker,'STATE_DIR',self.state_dir),patch.object(broker,'LEGACY_PATH',str(Path(self.tmp.name)/'absent')):
            self.assertEqual(broker._read(1)['access_token'],'fixture-token')
            self.assertNotIn('fixture-token',path.read_text());self.assertEqual(path.stat().st_mode&0o777,0o600)
            path.write_text('{"format":"fernet-account-v1","ciphertext":"broken"}')
            self.assertIsNotNone(broker.state(1)['credential_error'])
            with self.assertRaises(ValueError):broker.configure(1,budget=10000)

    def test_logout_revokes_copied_cookie_but_not_other_session(self):
        class DB:
            @contextmanager
            def connect(inner):
                con=sqlite3.connect(Path(self.tmp.name)/'auth.db')
                try:
                    yield con;con.commit()
                finally:con.close()
        db=DB();settings=Settings(auth_session_secret='fixture-isolated-session-signing-key')
        user=dict(id=1,role='user')
        token=auth._make_token(user,settings,db);other=auth._make_token(user,settings,db)
        payload=auth._verify_token(token,settings,db);other_payload=auth._verify_token(other,settings,db)
        self.assertTrue(auth._active_session(db,payload))
        request=Request(dict(type='http',headers=[(b'cookie',('openstocks_session='+token).encode())],method='POST',path='/',server=('test',80),scheme='http'))
        auth.logout_user(Response(),request,settings,db)
        self.assertFalse(auth._active_session(db,payload));self.assertTrue(auth._active_session(db,other_payload))
