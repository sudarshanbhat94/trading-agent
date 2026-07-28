"""Record 5-minute intraday bars from the live quote feed.

Why this exists: this system has NO intraday history. `market_ticks` is empty,
`latest_quotes` is a snapshot that overwrites itself, and `candles` holds daily
bars only. That single fact explains several things at once —

  * `swing_meanrev` can only be validated as "buy at the open", because a
    daily bar contains no other entry point;
  * `volume_surge` trades live all day and CANNOT be backtested at all, so
    nobody can say whether it works;
  * any question of the form "would entering later in the day have been
    better?" is unanswerable.

None of that is fixable retroactively. The data was never kept. So this keeps
it from now on, by folding the quote feed's own snapshots into 5-minute OHLCV
bars and writing them to `candles` under `intraday:5m`, where the existing
backtest tooling can already read them.

  python3 scripts/intraday_recorder.py --loop        # run as a service
  python3 scripts/intraday_recorder.py --once        # single fold, for checking

Cheap by construction: one read of latest_quotes per tick, one upsert per
symbol per bar. No network of its own — it rides the feed that already runs.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone

DB = os.environ.get("OPENSTOCKS_DB", "/opt/opentrade/var/trading_agent.db")
SOURCE = "intraday:5m"
BAR_SECONDS = 300
IST = timezone(timedelta(hours=5, minutes=30))


def bar_start(moment, bar_seconds=BAR_SECONDS):
    """The 5-minute bucket a moment belongs to, as an IST ISO timestamp.

    Buckets are aligned to the hour, so 09:17 lands in the 09:15 bar regardless
    of when the recorder happened to start.
    """
    moment = moment.astimezone(IST)
    seconds = moment.minute * 60 + moment.second
    floored = (seconds // bar_seconds) * bar_seconds
    return moment.replace(minute=0, second=0, microsecond=0) + timedelta(seconds=floored)


def fold_tick(bar, price, cumulative_volume):
    """Fold one observation into a bar. Returns the updated bar.

    `latest_quotes.volume` is the day's CUMULATIVE volume, so a bar's own
    volume is the growth across the bar, not the raw number. Storing the
    cumulative figure would make every bar look identical and enormous.
    """
    price = float(price)
    if bar is None:
        return {"open": price, "high": price, "low": price, "close": price,
                "volume_start": cumulative_volume, "volume_end": cumulative_volume}
    bar["high"] = max(bar["high"], price)
    bar["low"] = min(bar["low"], price)
    bar["close"] = price
    if cumulative_volume is not None:
        if bar["volume_start"] is None:
            bar["volume_start"] = cumulative_volume
        # Cumulative volume resets each session; a drop means a new day, so
        # rebase rather than recording a negative bar volume.
        if bar["volume_start"] is not None and cumulative_volume < bar["volume_start"]:
            bar["volume_start"] = cumulative_volume
        bar["volume_end"] = cumulative_volume
    return bar


def bar_volume(bar):
    start, end = bar.get("volume_start"), bar.get("volume_end")
    if start is None or end is None:
        return 0.0
    return max(0.0, float(end) - float(start))


def ensure_schema(con):
    con.execute("PRAGMA journal_mode=WAL")
    # Same table the daily candles live in, so existing tooling reads it for
    # free; only the `source` differs.
    con.execute("""CREATE TABLE IF NOT EXISTS candles(
        symbol TEXT, ts TEXT, open REAL, high REAL, low REAL, close REAL,
        volume REAL, source TEXT, PRIMARY KEY(symbol, ts, source))""")
    con.execute("CREATE INDEX IF NOT EXISTS idx_candles_src_ts ON candles(source, ts)")
    con.commit()


def read_quotes(con):
    """Current snapshot: symbol -> (price, cumulative volume)."""
    out = {}
    for symbol, price, volume in con.execute(
            "SELECT symbol, price, volume FROM latest_quotes WHERE price IS NOT NULL"):
        out[str(symbol).upper()] = (float(price), float(volume) if volume is not None else None)
    return out


def flush(con, bars, bucket):
    """Write the finished bucket's bars."""
    stamp = bucket.isoformat()
    rows = [(symbol, stamp, b["open"], b["high"], b["low"], b["close"],
             bar_volume(b), SOURCE) for symbol, b in bars.items()]
    if not rows:
        return 0
    con.executemany(
        "INSERT OR REPLACE INTO candles(symbol,ts,open,high,low,close,volume,source) "
        "VALUES(?,?,?,?,?,?,?,?)", rows)
    con.commit()
    return len(rows)


def run(loop=True, interval=20, verbose=True):
    con = sqlite3.connect(DB, timeout=30)
    con.execute("PRAGMA busy_timeout=10000")
    ensure_schema(con)
    bars: dict = {}
    bucket = None
    written = 0
    try:
        while True:
            now = datetime.now(timezone.utc)
            current = bar_start(now)
            if bucket is not None and current != bucket:
                written = flush(con, bars, bucket)
                if verbose:
                    print(f"[intraday] {bucket.strftime('%H:%M')} IST · wrote {written} bars",
                          flush=True)
                bars = {}
            bucket = current
            try:
                for symbol, (price, volume) in read_quotes(con).items():
                    bars[symbol] = fold_tick(bars.get(symbol), price, volume)
            except Exception as exc:
                print("[intraday] read failed:", str(exc)[:120], flush=True)
            if not loop:
                flush(con, bars, bucket)
                if verbose:
                    print(f"[intraday] single fold · {len(bars)} symbols in the "
                          f"{bucket.strftime('%H:%M')} bar", flush=True)
                break
            time.sleep(interval)
    finally:
        con.close()
    return written


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--loop", action="store_true", help="run continuously")
    parser.add_argument("--once", action="store_true", help="one fold, then exit")
    parser.add_argument("--interval", type=float, default=20, help="seconds between reads")
    args = parser.parse_args()
    run(loop=not args.once, interval=args.interval)


if __name__ == "__main__":
    main()
