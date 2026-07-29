"""Contract selection for the live option feed.

The feed carried no derivatives, so the options engine could open a position and
never see it again until the next day's bhavcopy — a weekly option moves 30-50%
on a 1% index move, so that is not a reporting gap, it means a position cannot
be exited.

The isolation assertions are the important ones. These rows must never reach the
`universe` table (get_universe() screens over it, so an option there becomes a
buy candidate for the equity lanes) and must be written under their own source
so _live("IN") cannot see them either.
"""
from __future__ import annotations

import unittest

from app import nfo_contracts as nfo

MASTER = []
for strike in range(23800, 24300, 50):
    for opt in ("CE", "PE"):
        MASTER.append({"exchange": "NSE_FO", "name": "NIFTY", "option_type": opt,
                       "expiry": "2026-08-04", "strike": str(float(strike)),
                       "lot_size": "65", "tradingsymbol": f"NIFTY26804{strike}{opt}",
                       "instrument_key": f"NSE_FO|{strike}{'1' if opt=='CE' else '2'}"})
# a later expiry that must not be selected
MASTER.append({"exchange": "NSE_FO", "name": "NIFTY", "option_type": "CE",
               "expiry": "2026-12-29", "strike": "24000.0", "lot_size": "65",
               "tradingsymbol": "NIFTY26DEC24000CE", "instrument_key": "NSE_FO|999"})


class SelectTest(unittest.TestCase):
    def test_picks_a_window_around_the_money(self) -> None:
        rows = nfo.select("NIFTY", 24000, rows=MASTER, each_side=2, today="2026-07-29")
        strikes = sorted({r["strike"] for r in rows})
        self.assertEqual(strikes, [23900.0, 23950.0, 24000.0, 24050.0, 24100.0])

    def test_both_sides_of_every_strike(self) -> None:
        rows = nfo.select("NIFTY", 24000, rows=MASTER, each_side=1, today="2026-07-29")
        self.assertEqual(sorted({r["option_type"] for r in rows}), ["CE", "PE"])

    def test_only_the_nearest_expiry(self) -> None:
        """A far expiry has its own liquidity and is not what gets traded."""
        rows = nfo.select("NIFTY", 24000, rows=MASTER, today="2026-07-29")
        self.assertEqual({r["expiry"] for r in rows}, {"2026-08-04"})

    def test_expired_contracts_are_never_selected(self) -> None:
        rows = nfo.select("NIFTY", 24000, rows=MASTER, today="2026-08-05")
        self.assertTrue(all(r["expiry"] >= "2026-08-05" for r in rows))

    def test_rows_carry_the_instrument_key_the_provider_needs(self) -> None:
        rows = nfo.select("NIFTY", 24000, rows=MASTER, today="2026-07-29")
        self.assertTrue(all(r["upstox_instrument_key"].startswith("NSE_FO|") for r in rows))

    def test_exchange_routes_to_the_india_provider(self) -> None:
        """The region router keys off exchange, and NSE_FO is not in
        INDIA_EXCHANGES — these would be routed to the US provider."""
        from app.market_regions import INDIA_EXCHANGES
        rows = nfo.select("NIFTY", 24000, rows=MASTER, today="2026-07-29")
        self.assertTrue(all(r["exchange"] in INDIA_EXCHANGES for r in rows))

    def test_lot_size_is_carried(self) -> None:
        """Sizing is impossible without it — a lot is the minimum tradeable unit."""
        rows = nfo.select("NIFTY", 24000, rows=MASTER, today="2026-07-29")
        self.assertTrue(all(r["lot_size"] == 65.0 for r in rows))

    def test_the_window_is_small(self) -> None:
        """Upstox rate-limits, and a 429 puts BOTH equity lanes into a 45s
        cooldown, so the option lane must stay tiny."""
        rows = nfo.select("NIFTY", 24000, rows=MASTER, today="2026-07-29")
        self.assertLessEqual(len(rows), 16)

    def test_no_spot_yields_nothing(self) -> None:
        self.assertEqual(nfo.select("NIFTY", 0, rows=MASTER), [])
        self.assertEqual(nfo.select("NIFTY", None, rows=MASTER), [])

    def test_unknown_index_yields_nothing(self) -> None:
        self.assertEqual(nfo.select("SENSEX", 80000, rows=MASTER, today="2026-07-29"), [])

    def test_empty_master_is_safe(self) -> None:
        self.assertEqual(nfo.select("NIFTY", 24000, rows=[], today="2026-07-29"), [])


class IsolationTest(unittest.TestCase):
    def test_selection_never_touches_the_universe_table(self) -> None:
        """An option in `universe` becomes a buy candidate for swing_meanrev."""
        import inspect
        src = inspect.getsource(nfo)
        self.assertNotIn("INSERT INTO universe", src)
        self.assertNotIn("insert into universe", src.lower())

    def test_feed_writes_options_under_their_own_source(self) -> None:
        import pathlib
        src = (pathlib.Path(__file__).resolve().parent.parent
               / "scripts" / "v2_quote_feed.py").read_text()
        self.assertIn('NFO_SOURCE = "upstox-nfo"', src)
        self.assertIn("nfo_quotes", src)
        # must NOT go through upsert_quotes, which would file them as equities
        nfo_write = src[src.index("def _nfo_write("):src.index("def _nfo_worker(")]
        self.assertNotIn("upsert_quotes", nfo_write)


if __name__ == "__main__":
    unittest.main()
