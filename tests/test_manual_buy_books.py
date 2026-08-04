"""Manual buy must not see the options book.

The options book is separately funded. /api/buy counted every position in the
market — including index_options — against the equity book's 6 slots, and
subtracted option premium from the equity budget.

Live state when this was found: 7 positions against max 6 ("book full"), cash
computed as -Rs 5,539, and every Buy returning 400. Three of those 7 were
option contracts that have their own Rs 1,00,000.

_market_stats was fixed for exactly this on 2026-08-03; this endpoint was
missed, which is the recurring shape — one copy of a rule gets corrected and
its siblings do not.
"""
from __future__ import annotations

import inspect
import unittest

from app import v2_web


class BuyIgnoresOptionsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.src = inspect.getsource(v2_web.api_buy)

    def test_the_slot_count_excludes_options(self) -> None:
        block = self.src[self.src.index("SELECT COUNT(*) FROM v2_positions"):]
        block = block[:200]
        self.assertIn("strategy NOT IN", block)

    def test_the_cash_math_excludes_options(self) -> None:
        for q in ("COALESCE(SUM(pnl),0) FROM v2_trades",
                  "COALESCE(SUM(shares*entry_price),0) FROM v2_positions"):
            with self.subTest(query=q):
                block = self.src[self.src.index(q):][:220]
                self.assertIn("strategy NOT IN", block)

    def test_it_uses_the_shared_exclusion_list(self) -> None:
        """Not a second hand-written literal — that is how the two copies
        drifted apart in the first place."""
        self.assertIn("EQUITY_EXCLUDED", self.src)
        self.assertIn("index_options", v2_web.EQUITY_EXCLUDED)

    def test_no_unscoped_position_query_remains(self) -> None:
        """Any surviving `FROM v2_positions WHERE market=?` without the strategy
        filter would reintroduce the bug for whatever it feeds.

        Adjacent string literals are joined first — these queries are written
        across two lines, so matching the raw source finds a false positive at
        every line break.
        """
        import re
        joined = re.sub(r'"\s*\n\s*f?"', "", self.src)
        bad = re.findall(r"FROM v2_positions WHERE market=\?(?! AND strategy NOT IN)"
                         r"(?! AND symbol=\?)", joined)
        self.assertEqual(bad, [], "unscoped position query in api_buy")


if __name__ == "__main__":
    unittest.main()
