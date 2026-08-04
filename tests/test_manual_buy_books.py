"""Manual buy must not touch the engine's book at all.

Originally this file guarded a narrower bug: /api/buy counted index-option
positions against the equity book's slots and cash, so every Buy returned 400
("book full 7/6", cash -Rs 5,539).

The endpoint has since been moved off the house book entirely, which makes that
class of bug unreachable — there is no equity/options mixing to get wrong
because manual buy no longer reads v2_positions. The reason it moved was worse
than a 400: record_entry fires the live-broker mirror, and the mirror asserts
the OWNER's user id, so ANY Pro or Elite subscriber pressing Buy placed a real
order in the operator's Upstox account.
"""
from __future__ import annotations

import inspect
import unittest

from app import v2_web


def _body(fn):
    """Source with the docstring stripped.

    These endpoints EXPLAIN in prose why they no longer touch the engine's
    tables, so matching raw source finds the table name inside the very comment
    saying it is not used.
    """
    src = inspect.getsource(fn)
    marker = chr(34) * 3
    if src.count(marker) >= 2:
        first = src.index(marker)
        second = src.index(marker, first + 3)
        return src[:first] + src[second + 3:]
    return src


class ManualBuyIsOffTheHouseBookTest(unittest.TestCase):
    def setUp(self) -> None:
        self.src = _body(v2_web.api_buy)

    def test_it_writes_the_callers_own_book(self) -> None:
        self.assertIn("books.buy(", self.src)

    def test_it_never_reads_or_writes_the_engines_tables(self) -> None:
        for table in ("v2_positions", "v2_trades", "v2_book"):
            with self.subTest(table=table):
                self.assertNotIn(table, self.src)

    def test_it_does_not_call_the_house_writer(self) -> None:
        """record_entry writes v2_positions AND fires both engine mirrors."""
        self.assertNotIn("record_entry(", self.src)

    def test_only_the_sleeve_owner_reaches_the_broker(self) -> None:
        """THE fix. Anyone else's manual buy is paper, full stop."""
        self.assertIn('int(bst.get("owner_user_id") or -1) == uid', self.src)

    def test_it_takes_the_session_user(self) -> None:
        self.assertIn("user", inspect.signature(v2_web.api_buy).parameters)


class ManualSellIsOffTheHouseBookTest(unittest.TestCase):
    def setUp(self) -> None:
        self.src = _body(v2_web.api_sell)

    def test_it_sells_from_the_callers_own_book(self) -> None:
        self.assertIn("books.sell(", self.src)
        self.assertNotIn("v2_positions", self.src)

    def test_it_does_not_call_the_house_writer(self) -> None:
        self.assertNotIn("record_exit(", self.src)

    def test_only_the_owner_reaches_the_broker(self) -> None:
        self.assertIn('int(bst.get("owner_user_id") or -1) == uid', self.src)


class HouseExitIsOperatorOnlyTest(unittest.TestCase):
    def test_closing_an_engine_position_needs_admin(self) -> None:
        """/positions/{pid}/exit acts on v2_positions and fires the broker
        mirror. It was reachable by every Pro subscriber."""
        src = inspect.getsource(v2_web.api_exit)
        self.assertIn('(user.get("role") or "").lower() != "admin"', src)
        self.assertIn("the engine's book", src)


if __name__ == "__main__":
    unittest.main()
