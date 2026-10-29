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
from app import books, broker, execution_outbox as outbox, v2_live, paper_ledger, personal_performance, paper_exchange, executable_quotes


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
    from app.sleeves.base import Candidate
    from app.sleeves.risk import Allocation
    def quote(at=None,ask=100):
        # Reads return an already observed event, never a future timestamp
        # created after the outbox captured its decision time.
        at=at or datetime.now(timezone.utc)-timedelta(seconds=1)
        snapshot=executable_quotes.normalize_upstox(dict(instrument_token='NSE_EQ|TEST',symbol='TEST',timestamp=at.isoformat(),
            lower_circuit_limit=80,upper_circuit_limit=120,depth=dict(buy=[dict(price=round(ask-.05,2),quantity=1000)],sell=[dict(price=ask,quantity=1000)])),
            'NSE_EQ|TEST','TEST',observed_at=at.isoformat())
        return {'TEST':dict(price=ask,ts=at.isoformat(),execution=snapshot)}
    with closing(fixture_catalogue()) as catalogue,entry_contracts.using(catalogue),tempfile.TemporaryDirectory() as tmp, patch.object(books,"subscribers",return_value=[1,2]), \
            patch('app.sleeves.index_directional.SYMBOL','TEST'), \
            patch.object(broker,"linked_users",return_value=[]), patch.object(v2_live,"_live",side_effect=lambda *a,**kw:quote()), \
            patch.object(broker,"place_order",side_effect=AssertionError("real orders forbidden")):
        path=Path(tmp)/"paper.db"
        con=sqlite3.connect(path);v2_live.ensure_schema(con)
        for uid in (1,2):books.ensure_book(con,uid,"IN")
        # Synthetic ticker substitution tests execution, not a model promotion.
        now=datetime.now(timezone.utc)
        allocation=Allocation(Candidate('TEST','index_directional',.9,100,99,target=110,allocation_pct=.5),20,2000,100)
        submitted=paper_exchange.enqueue_house(con,catalogue,allocation,quote(now),regime='ON',now=now)
        assert submitted['status']=='pending'
        assert con.execute('SELECT COUNT(*) FROM v2_positions').fetchone()[0]==0
        assert books.positions(con,1)==[]
        con.close()  # Restart after reservation, before the later exchange event.
        con=sqlite3.connect(path)
        at=now+timedelta(seconds=1)
        fills=paper_exchange.service_house(con,catalogue,quote(at),regime='ON',now=at)
        assert len(fills)==1 and fills[0]['status']=='filled'
        assert paper_exchange.service_house(con,catalogue,quote(at),regime='ON',now=at)==[]
        pid=fills[0]['position_id']
        assert outbox.drain(con)==1
        assert outbox.drain(con)==0
        assert books.positions(con,1)==books.positions(con,2)==[]
        assert len(paper_exchange.pending(con))==2
        at=datetime.now(timezone.utc)+timedelta(seconds=2)
        fills=paper_exchange.service(con,catalogue,quote(at),regime='ON',now=at)
        assert len(fills)==2 and all(r['status']=='filled' for r in fills)
        assert len(books.positions(con,1))==len(books.positions(con,2))==1
        assert books.positions(con,1)[0]["shares"]==20
        assert books.cash(con,1)>=0
        # User 2 reset is local. An unrelated manual holding is not the mirror.
        books.reset_book(con,2,"IN")
        assert books.buy(con,2,"IN","manual","TEST",100,20,99,110,quotes=quote(),regime='ON')>0
        paper_exchange.queue_exit(con,0,pid,'target')
        assert con.execute('SELECT COUNT(*) FROM v2_trades').fetchone()[0]==0
        con.close()  # Restart again before the later bid can fill the exit.
        con=sqlite3.connect(path)
        at=datetime.now(timezone.utc)+timedelta(seconds=3)
        closed=paper_exchange.service_exits(con,quote(at,110.05),now=at)
        assert len(closed)==1 and closed[0]['side']=='SELL'
        house_net=closed[0]['pnl']
        assert outbox.drain(con)==1
        assert outbox.drain(con)==0
        assert len(books.positions(con,1))==1  # Exit delivery is not an exchange fill.
        at=datetime.now(timezone.utc)+timedelta(seconds=4)
        assert len(paper_exchange.service_exits(con,quote(at,110.05),now=at))==1
        assert books.positions(con,1)==[]
        assert len(books.positions(con,2))==1
        assert con.execute("SELECT COUNT(*) FROM v2_trades").fetchone()[0]==1
        assert con.execute("SELECT COUNT(*) FROM user_trades WHERE user_id=1").fetchone()[0]==1
        assert con.execute("SELECT COUNT(*) FROM user_trades WHERE user_id=2").fetchone()[0]==0
        ledger=paper_ledger.report(con,1,"IN",books.current_epoch(con,1),books.cash(con,1))
        assert ledger['status']=='ok' and ledger['balanced']
        performance=personal_performance.report(con,1,quotes=quote())
        assert performance['current_epoch']['trades']==1
        assert performance['current_epoch']['r_observations']==1
        result=dict(status="passed",capital=10000,house_fixture_net=round(house_net,2),
                    delivery_duplicates=0,ownership_errors=0,negative_cash=False,
                    actual_broker_orders=0,fixture_signal=True,profitability_evidence=False,
                    ledger_balanced=True,ledger_cash_difference_minor=ledger['cash_difference_minor'],
                    personal_closed_trades=performance['current_epoch']['trades'],
                    simulation_version=paper_exchange.SIMULATION_VERSION,partial_fills_supported=False)
        con.close()
    return result


if __name__=="__main__":
    print(json.dumps(run(),sort_keys=True))
