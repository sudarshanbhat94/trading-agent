"""Timestamped inputs at the production boundary, independent of strategy rules."""
import math
from datetime import datetime, timezone


def fresh_quotes(quotes, now, max_age_seconds=120):
    out = {}
    for symbol, quote in quotes.items():
        try:
            ts = datetime.fromisoformat(str(quote["ts"]).replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            age = (now - ts).total_seconds()
            price = float(quote["price"])
            if -5 <= age <= max_age_seconds and math.isfinite(price) and price > 0:
                out[symbol] = quote
        except (KeyError, TypeError, ValueError):
            continue
    return out


def delivery_reader(con, asof):
    """Use published completed-session delivery, never today's partial data."""
    def read(symbol):
        rows = con.execute(
            "SELECT date,delivery_pct FROM delivery_data WHERE symbol=? AND date<=? "
            "AND delivery_pct IS NOT NULL ORDER BY date DESC LIMIT 21",
            (symbol, str(asof)[:10])).fetchall()
        if len(rows) < 2 or rows[0][0][:10] != str(asof)[:10]:
            return None, None
        return float(rows[0][1]), sum(float(r[1]) for r in rows[1:]) / (len(rows)-1)
    return read
