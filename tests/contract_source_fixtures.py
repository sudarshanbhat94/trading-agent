"""Dated synthetic rules/calendar for integration tests; never production input."""
from datetime import datetime,timedelta,timezone
import sqlite3
from app import execution_contracts
from app.instrument_catalog import Instrument,import_snapshot


def catalogue(symbol='TEST',kind='EQUITY',now=None):
    now=now or datetime.now(timezone.utc);start=now.replace(hour=0,minute=0,second=0,microsecond=0);end=start+timedelta(days=1)
    con=sqlite3.connect(':memory:');spec=Instrument('NSE','NSE_EQ',kind,symbol,'INR','FIXTURE_'+symbol,'EQ')
    import_snapshot(con,[(spec,'NSE_EQ|'+symbol)],provider='upstox',source='SYNTHETIC CONTRACT FIXTURE',source_day=now.date().isoformat(),observed_at=now.isoformat(),now=now)
    attrs=dict(source='SYNTHETIC ALL-DAY CALENDAR; NOT EXCHANGE RULES',observed_at=now.isoformat(),effective_from=start.isoformat(),effective_until=end.isoformat(),now=now)
    execution_contracts.record(con,'rules',spec.id,dict(lot_size=1,tick_size='0.05',freeze_quantity=10000,settlement='T+1',lower_circuit=50,upper_circuit=200,
        calendar='NSE:FIXTURE',banned=False,corporate_action_pending=False,actions_reviewed_at=now.isoformat()),**attrs)
    execution_contracts.record(con,'session','NSE:FIXTURE',dict(open=True,opens_at=start.isoformat(),closes_at=end.isoformat()),**attrs)
    return con
