"""Ideas must be sized for the money that would actually buy them.

Ideas are published against a Rs 1,00,000 reference account. The owner of a
connected broker has a real balance — Rs 9,115 at the time of writing — so
showing them 15 shares of a Rs 783 stock is worse than useless: it is a number
they cannot act on presented as a recommendation.

The published LEVELS are never rewritten (see PublishTest); only the displayed
quantity is recomputed per viewer.
"""
from __future__ import annotations

import inspect
import unittest

from app import v2_web


class SizedForTheBrokerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.src = inspect.getsource(v2_web.api_ideas)

    def test_it_sizes_against_the_broker_sleeve(self) -> None:
        self.assertIn("size_for_sleeve", self.src)
        self.assertIn("broker_qty", self.src)

    def test_the_sleeve_is_the_lower_of_budget_and_real_margin(self) -> None:
        """A configured budget the account cannot fund is not a budget."""
        self.assertIn('min(float(bst.get("budget") or 0), margin)', self.src)

    def test_published_levels_are_not_rewritten(self) -> None:
        """Only qty/cost are added. Entry, stop and targets must survive
        untouched or a subscriber's screenshot stops matching the page."""
        for field in ('r["entry"] =', 'r["stop"] =', 'r["t1"] =',
                      'r["t2"] =', 'r["t3"] ='):
            with self.subTest(field=field):
                self.assertNotIn(field, self.src)

    def test_buying_requires_ownership_and_a_live_sleeve(self) -> None:
        self.assertIn('bst.get("live_ready")', self.src)
        self.assertIn('bst.get("owner_user_id") == user.get("id")', self.src)

    def test_a_resolved_idea_is_not_buyable(self) -> None:
        """Buying a stop-hit idea at today's price is not the idea."""
        self.assertIn('r["status"] == _ideas.STATUS_OPEN', self.src)

    def test_an_unaffordable_idea_is_not_buyable(self) -> None:
        self.assertIn('r["broker_qty"] > 0', self.src)


class BuyButtonTest(unittest.TestCase):
    def setUp(self) -> None:
        import pathlib
        src = pathlib.Path(inspect.getfile(v2_web)).read_text(encoding="utf-8")
        self.spa = src[src.rindex('SPA_HTML = r"""'):]

    def test_the_button_only_renders_when_buyable(self) -> None:
        self.assertIn("r.buyable?'<div class=ig-buy>", self.spa)

    def test_it_confirms_before_spending_real_money(self) -> None:
        block = self.spa[self.spa.index("function ideaBuy("):]
        block = block[:block.index("\nfunction ")]
        self.assertIn("confirm(", block)
        self.assertIn("REAL ORDER", block)

    def test_it_posts_to_the_buy_endpoint_that_mirrors(self) -> None:
        """/api/buy routes through record_entry, which fires the live mirror.
        Any other endpoint would place a paper position and no real order."""
        block = self.spa[self.spa.index("function ideaBuy("):]
        block = block[:block.index("\nfunction ")]
        self.assertIn("'/v2/api/buy'", block)

    def test_the_card_shows_the_broker_size_when_there_is_one(self) -> None:
        self.assertIn("r.broker_qty!=null", self.spa)
        self.assertIn("(your broker)", self.spa)


if __name__ == "__main__":
    unittest.main()
