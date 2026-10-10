#!/usr/bin/env python3
"""Read-only authenticated portfolio collector; never submits broker orders.

Run separately from the execution writer. Existing private per-user tokens
authorize the outbound socket; the URI and profile identity are never logged.
This does not connect/arm accounts or modify the reviewed execution policy.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sqlite3
import sys
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app import broker,portfolio_stream


async def collect(uid,path,*,duration=None):
    from websockets.asyncio.client import connect
    connection=None
    # The execution service must have migrated this database before collection.
    # mode=rw refuses accidental creation of an empty substitute paper book.
    con=sqlite3.connect('file:'+str(Path(path).resolve())+'?mode=rw',uri=True,timeout=10)
    try:
        if not con.execute("SELECT 1 FROM sqlite_master WHERE name='broker_stream_accounts'").fetchone():
            raise ValueError('Execution stream schema is not migrated')
        account,uri=await asyncio.to_thread(broker.portfolio_stream_access,uid)
        connection=portfolio_stream.open_connection(con,uid,account)
        started=time.monotonic()
        async with connect(uri,open_timeout=15,close_timeout=5,ping_interval=20,ping_timeout=20,max_size=65536) as socket:
            while duration is None or time.monotonic()-started<duration:
                remaining=15 if duration is None else min(15,max(.01,duration-(time.monotonic()-started)))
                try:message=await asyncio.wait_for(socket.recv(),timeout=remaining)
                except asyncio.TimeoutError:
                    portfolio_stream.heartbeat(con,connection)
                    continue
                payload=json.loads(message)
                portfolio_stream.ingest(con,connection,payload)
                portfolio_stream.heartbeat(con,connection)
    finally:
        if connection:portfolio_stream.close_connection(con,connection)
        con.close()


async def run(path,once=False,duration=20):
    tasks={}
    try:
        while True:
            users={uid for uid in broker.linked_users() if broker.state(uid).get('exit_ready')}
            for uid,task in list(tasks.items()):
                if task.done():
                    try:task.result()
                    except Exception as exc:
                        # Transport exception text can contain the signed URI.
                        print('portfolio collector failed: '+type(exc).__name__,flush=True)
                    del tasks[uid]
                elif uid not in users:
                    task.cancel();await asyncio.gather(task,return_exceptions=True);del tasks[uid]
            for uid in users-tasks.keys():
                tasks[uid]=asyncio.create_task(collect(uid,path,duration=duration if once else None))
            if once:
                results=await asyncio.gather(*tasks.values(),return_exceptions=True)
                errors=[type(result).__name__ for result in results if isinstance(result,BaseException)]
                print(json.dumps(dict(accounts=len(tasks),failures=errors,orders_submitted=0)))
                return 1 if errors else 0
            await asyncio.sleep(30)
    finally:
        for task in tasks.values():task.cancel()
        await asyncio.gather(*tasks.values(),return_exceptions=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database',default=os.environ.get('V2_PAPER_DB','/opt/opentrade/var/v2_paper.db'))
    mode=parser.add_mutually_exclusive_group(required=True);mode.add_argument('--loop',action='store_true');mode.add_argument('--once',action='store_true')
    parser.add_argument('--duration',type=int,default=20)
    args=parser.parse_args()
    if not 1<=args.duration<=60:parser.error('duration must be 1–60 seconds')
    return asyncio.run(run(args.database,args.once,args.duration))


if __name__=='__main__':sys.exit(main())
