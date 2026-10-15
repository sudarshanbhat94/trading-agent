"""Mock account endpoints for decision tests; no remote calls or risk mocks.

Dedicated reconciliation tests use independent broker snapshots and divergence
cases. This fixture just supplies the expanded port to older decision tests.
"""
def install(broker, con):
    def positions(uid):
        return [dict(instrument_token=key,product=product,quantity=int(q),overnight_quantity=0)
                for key,product,q in con.execute("SELECT instrument_key,product,SUM(CASE WHEN side='BUY' "
                                                "THEN filled_qty ELSE -filled_qty END) FROM v2_live_orders "
                                                "WHERE user_id=? GROUP BY instrument_key,product HAVING SUM(CASE "
                                                "WHEN side='BUY' THEN filled_qty ELSE -filled_qty END)!=0",(uid,))]
    def trades(uid):
        return [dict(trade_id=f"fixture-{rid}",order_id=oid,instrument_token=key,
                     transaction_type=side,product=product,quantity=int(qty),average_price=float(price),executed_at=ts)
                for rid,oid,key,side,product,qty,price,ts in con.execute(
                    "SELECT id,broker_order_id,instrument_key,side,product,filled_qty,average_price,ts FROM v2_live_orders "
                    "WHERE user_id=? AND filled_qty>0",(uid,))]
    broker.positions=positions
    broker.holdings=lambda uid: []
    broker.funds=lambda uid: dict(data=dict(equity=dict(available_margin=10000)))
    broker.trades=trades
