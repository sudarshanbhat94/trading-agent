#!/usr/bin/env python3
"""Deterministic isolated session: restart/replay/ownership/risk/after-cost close.

This is a software rehearsal, not exchange fills or profitable-strategy proof.
No credential, network, production database or strategy parameter is used.
"""
import json
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone,timedelta
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app import books, broker, execution_outbox as outbox, v2_live, paper_ledger, personal_performance


def fixture_catalogue():
    from app.instrument_catalog import Instrument,import_snapshot
    from app import execution_contracts
    con=sqlite3.connect(':memory:');now=datetime.now(timezone.utc);stamp=now.isoformat()
    start=now.replace(hour=0,minute=0,second=0,microsecond=0);end=start+timedelta(days=1)
    spec=Instrument('NSE','NSE_EQ','EQUITY','TEST','INR','TEST','EQ')
    import_snapshot(con,[(spec,'NSE_EQ|TEST')],provider='upstox',source='SYNTHETIC REHEARSAL',source_day=stamp[:10],observed_at=stamp)
    attrs=dict(source='SYNTHETIC REHEARSAL; NOT EXCHANGE EVIDENCE',observed_at=stamp,effective_from=start.isoformat(),effective_until=end.isoformat())
    execution_contracts.record(con,'rules',spec.id,dict(lot_size=1,tick_size='0.05',freeze_quantity=10000,settlement='T+1',
        lower_circuit=80,upper_circuit=120,calendar='NSE:FIXTURE',banned=False,corporate_action_pending=False,actions_reviewed_at=stamp),**attrs)
    execution_contracts.record(con,'session','NSE:FIXTURE',dict(open=True,opens_at=start.isoformat(),closes_at=end.isoformat()),**attrs)
    return con


def run():
    from app import entry_contracts
    stamp=datetime.now(timezone.utc).isoformat()
    quote={"TEST":dict(price=100,ts=stamp)}
    with closing(fixture_catalogue()) as catalogue,entry_contracts.using(catalogue),tempfile.TemporaryDirectory() as tmp, patch.object(books,"subscribers",return_value=[1,2]), \
            patch.object(broker,"linked_users",return_value=[]), patch.object(v2_live,"_live",return_value=quote), \
            patch.object(broker,"place_order",side_effect=AssertionError("real orders forbidden")):
        path=Path(tmp)/"paper.db"
        con=sqlite3.connect(path);v2_live.ensure_schema(con)
        for uid in (1,2):books.ensure_book(con,uid,"IN")
        # Fixture signal accepted earlier by a strategy; no strategy is selected
        # or promoted here. Outbox publication cannot write another account yet.
        accepted=v2_live.record_entry(con,"IN","mean_reversion","TEST",stamp[:10],100,20,99,110,0,.5,None,regime='ON')
        assert accepted
        pid=con.execute("SELECT id FROM v2_positions").fetchone()[0]
        assert books.positions(con,1)==[]
        con.close()  # Hard boundary between publication and account delivery.
        con=sqlite3.connect(path)
        assert outbox.drain(con)==1
        assert outbox.drain(con)==0
        assert len(books.positions(con,1))==len(books.positions(con,2))==1
        assert books.positions(con,1)[0]["shares"]==20
        assert books.cash(con,1)>=0
        # User 2 reset is local. An unrelated manual holding is not the mirror.
        books.reset_book(con,2,"IN")
        assert books.buy(con,2,"IN","manual","TEST",100,20,99,110,quotes=quote,regime='ON')>0
        house_net,_=v2_live.record_exit(con,"IN",pid,stamp[:10],110,20,"target")
        con.close()  # Restart again before close delivery.
        con=sqlite3.connect(path)
        assert outbox.drain(con)==1
        assert outbox.drain(con)==0
        assert books.positions(con,1)==[]
        assert len(books.positions(con,2))==1
        assert con.execute("SELECT COUNT(*) FROM v2_trades").fetchone()[0]==1
        assert con.execute("SELECT COUNT(*) FROM user_trades WHERE user_id=1").fetchone()[0]==1
        assert con.execute("SELECT COUNT(*) FROM user_trades WHERE user_id=2").fetchone()[0]==0
        ledger=paper_ledger.report(con,1,"IN",books.current_epoch(con,1),books.cash(con,1))
        assert ledger['status']=='ok' and ledger['balanced']
        performance=personal_performance.report(con,1,quotes=quote)
        assert performance['current_epoch']['trades']==1
        assert performance['current_epoch']['r_observations']==1
        result=dict(status="passed",capital=10000,house_fixture_net=round(house_net,2),
                    delivery_duplicates=0,ownership_errors=0,negative_cash=False,
                    actual_broker_orders=0,fixture_signal=True,profitability_evidence=False,
                    ledger_balanced=True,ledger_cash_difference_minor=ledger['cash_difference_minor'],
                    personal_closed_trades=performance['current_epoch']['trades'])
        con.close()
    return result


if __name__=="__main__":
    print(json.dumps(run(),sort_keys=True))
