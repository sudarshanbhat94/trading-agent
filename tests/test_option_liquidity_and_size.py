"""Which contract the lane buys, and how much of it.

Two gaps, both found by looking at what the lane had actually done rather than
at the code.

LIQUIDITY. _pick_contract filtered on price, strike and lot size — and never on
whether the contract trades. Measured live, median option volume by index:

    NIFTY  60,284,250 · BANKNIFTY 696,180 · MIDCPNIFTY 106,200 · FINNIFTY 3,360

FINNIFTY's minimum was ZERO, and that is where the lane put 28% of the options
book. A paper fill in a contract nobody trades is fiction, and it is the kind
that only surfaces as a loss once the book is real.

The test is RELATIVE — a share of the busiest strike in the same chain — because
volume accumulates through the session. An absolute floor would refuse every
trade at 09:20 and accept anything by 14:20.

SIZE. It bought exactly ONE lot regardless of budget. A Rs 4,397 NIFTY lot
against a Rs 10,000 cap left more than half the allowance unused on every
trade, which is a large part of why the options book sat a third deployed.
"""

from __future__ import annotations

import unittest

from app import v2_live


def q(strike, price, lot=65.0, vol=1_000_000.0, opt="CE", under="NIFTY",
      expiry="2026-08-04"):
    return dict(price=price, lot_size=lot, strike=strike, option_type=opt,
                underlying=under, expiry=expiry, vol=vol, high=price, low=price)


CHAIN = {
    "N24300CE": q(24300, 167.65, vol=5_000_000.0),
    "N24400CE": q(24400, 67.65, vol=1_000_000.0),
    "N24500CE": q(24500, 20.00, vol=500_000.0),     # 10% of the busiest — thin but real
    "N24600CE": q(24600, 5.00, vol=1_000.0),        # 0.02% of the busiest — dead
}
SPOT = 24388.0


class LiquidityTest(unittest.TestCase):
    def test_a_dead_strike_is_refused(self) -> None:
        """1,000 against a busiest of 5,000,000 is 0.02% of the chain."""
        picked = v2_live._pick_contract("NIFTY", "CE", SPOT, CHAIN,
                                        max_cost=400.0, min_vol_share=0.05)
        self.assertIsNone(picked, "the only affordable strike is untraded")

    def test_a_traded_strike_is_accepted(self) -> None:
        picked = v2_live._pick_contract("NIFTY", "CE", SPOT, CHAIN,
                                        max_cost=2000.0, min_vol_share=0.05)
        self.assertEqual(picked["symbol"], "N24500CE")

    def test_the_test_is_relative_so_it_holds_early_in_the_session(self) -> None:
        """Same chain, every volume scaled down by 1000 as it would be at the
        open. An absolute floor would reject all of these; the decision must
        not change."""
        early = {k: dict(v, vol=v["vol"] / 1000.0) for k, v in CHAIN.items()}
        picked = v2_live._pick_contract("NIFTY", "CE", SPOT, early,
                                        max_cost=2000.0, min_vol_share=0.05)
        self.assertEqual(picked["symbol"], "N24500CE")

    def test_no_share_given_means_no_liquidity_test(self) -> None:
        """Back-compat: callers that do not ask for it get the old behaviour."""
        picked = v2_live._pick_contract("NIFTY", "CE", SPOT, CHAIN, max_cost=400.0)
        self.assertEqual(picked["symbol"], "N24600CE")

    def test_the_nearest_affordable_strike_still_wins(self) -> None:
        """Liquidity is a filter, not a ranking — it must not quietly turn the
        selector into 'buy the busiest strike'."""
        picked = v2_live._pick_contract("NIFTY", "CE", SPOT, CHAIN,
                                        max_cost=20000.0, min_vol_share=0.05)
        # spot 24388: |24400-24388|=12 beats |24300-24388|=88, and it is NOT the
        # busiest strike — so the filter has not become a ranking
        self.assertEqual(picked["symbol"], "N24400CE")

    def test_the_configured_share_is_wired_in(self) -> None:
        import inspect
        src = inspect.getsource(v2_live.index_options_pass)
        self.assertIn('min_vol_share=cfg.get("min_volume_share"', src)
        self.assertIn("min_volume_share", v2_live.INDEX_OPTIONS)


class LotSizingTest(unittest.TestCase):
    """Options trade in whole lots, so this is floor division — never a
    fractional lot, and never a rupee over the cap."""

    def lots(self, budget, premium, lot):
        return max(1, int(budget // (premium * lot)))

    def test_the_budget_buys_more_than_one_lot_when_it_fits(self) -> None:
        # NIFTY at Rs 67.65 x 65 = Rs 4,397 a lot, against a Rs 10,000 cap
        self.assertEqual(self.lots(10000.0, 67.65, 65.0), 2)

    def test_an_expensive_lot_still_gives_one(self) -> None:
        # FINNIFTY at Rs 461.15 x 60 = Rs 27,669 against a Rs 30,000 cap
        self.assertEqual(self.lots(30000.0, 461.15, 60.0), 1)

    def test_sizing_is_wired_in_and_bounded_by_the_cap(self) -> None:
        import inspect
        src = inspect.getsource(v2_live.index_options_pass)
        self.assertIn("lots = max(1, int(budget_per_trade // (premium * lot)))", src)
        self.assertIn("shares = lot * lots", src)
        self.assertIn("cost = premium * shares", src)
        # and a single lot that already breaks the cap is refused rather than bought
        self.assertIn("if cost > budget_per_trade:", src)

    def test_the_position_is_written_with_the_sized_quantity(self) -> None:
        """The bug this would hide: sizing up but recording one lot, so the
        book under-states what was bought."""
        import inspect
        src = inspect.getsource(v2_live.index_options_pass)
        self.assertIn("today.isoformat(), premium, shares, stop, target", src)
        self.assertNotIn("today.isoformat(), premium, lot, stop, target", src)

    def test_the_spent_cash_reflects_every_lot(self) -> None:
        import inspect
        src = inspect.getsource(v2_live.index_options_pass)
        spend = src.index("options_cash -= cost")
        sized = src.index("cost = premium * shares")
        self.assertLess(sized, spend, "cost must be the sized cost before it is spent")


if __name__ == "__main__":
    unittest.main()
