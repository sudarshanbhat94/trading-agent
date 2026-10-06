#!/usr/bin/env python3
"""Deterministic isolated session: restart/replay/ownership/risk/after-cost close.

This is a software rehearsal, not exchange fills or profitable-strategy proof.
No credential, network, production database or strategy parameter is used.
"""
import json
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app import books, broker, execution_outbox as outbox, v2_live


def run():
    stamp=datetime.now(timezone.utc).isoformat()
    quote={"TEST":dict(price=100,ts=stamp)}
    with tempfile.TemporaryDirectory() as tmp, patch.object(books,"subscribers",return_value=[1,2]), \
            patch.object(broker,"linked_users",return_value=[]), patch.object(v2_live,"_live",return_value=quote), \
            patch.object(broker,"place_order",side_effect=AssertionError("real orders forbidden")):
        path=Path(tmp)/"paper.db"
        con=sqlite3.connect(path);v2_live.ensure_schema(con)
        for uid in (1,2):books.ensure_book(con,uid,"IN")
        # Fixture signal accepted earlier by a strategy; no strategy is selected
        # or promoted here. Outbox publication cannot write another account yet.
        accepted=v2_live.record_entry(con,"IN","mean_reversion","TEST",stamp[:10],100,20,99,110,0,.5,None)
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
        assert books.buy(con,2,"IN","manual","TEST",100,20,99,110,quotes=quote)>0
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
        result=dict(status="passed",capital=10000,house_fixture_net=round(house_net,2),
                    delivery_duplicates=0,ownership_errors=0,negative_cash=False,
                    actual_broker_orders=0,fixture_signal=True,profitability_evidence=False)
        con.close()
    return result


if __name__=="__main__":
    print(json.dumps(run(),sort_keys=True))
