"""Explicit account routing for a user action; paper never mirrors implicitly."""
from fastapi.responses import JSONResponse


def live_action(con, main, user, market, symbol, price, side):
    from . import broker, live_trade, order_journal
    from .broker_access import may_open
    uid = int(user['id'])
    state = broker.state(uid)
    if market != 'IN':
        return JSONResponse({'error':'This broker route supports NSE only'},status_code=400)
    if side == 'BUY' and not may_open(user):
        return JSONResponse({'error':'Elite is required for new live orders'},status_code=403)
    if not state.get('live_ready'):
        return JSONResponse({'error':'Broker is not ready; no paper order was substituted',
                             'mode':'live','paper_recorded':False},status_code=409)
    if side == 'BUY':
        status = live_trade.mirror_entry(con,main,uid,market,symbol,price,'manual',
                                         stop=price*.94,target=price*1.06)
    else:
        if live_trade.live_qty(con,uid,symbol)<=0 and not order_journal.unresolved(con,uid,symbol):
            return JSONResponse({'error':'No managed broker position for this symbol'},status_code=409)
        status = live_trade.mirror_exit(con,main,uid,market,symbol,price,'manual')
    accepted = status in ('submitted','partial','filled')
    return JSONResponse({'ok':accepted,'mode':'live','symbol':symbol,'broker_status':status,
                         'paper_recorded':False,'error':None if accepted else status},
                        status_code=200 if accepted else 409)
