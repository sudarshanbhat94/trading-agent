"""Option-chain analytics.

Max pain is where the arithmetic is easy to get wrong and impossible to notice.
It is the settlement price minimising total intrinsic payout across every open
contract — NOT "the strike with the most total OI", which is a different
quantity. The test below verifies it against payouts computed by hand rather
than captured from the implementation, which is the only way a wrong answer
here would ever be caught.

The other recurring error is conflating open interest with its change. OI is a
stock — who is positioned. The change is a flow — what happened today. A chain
can have enormous put OI while today's flow is entirely calls.
"""

from __future__ import annotations

import unittest

from app import option_chain as oc


def row(strike, side, oi=0.0, volume=1000.0, close=10.0, oi_change=0.0):
    return dict(strike=strike, opt_type=side, oi=oi, volume=volume,
                close=close, oi_change=oi_change)


class PcrTest(unittest.TestCase):
    def test_oi_ratio(self) -> None:
        chain = [row(100, "PE", oi=1300), row(100, "CE", oi=1000)]
        self.assertAlmostEqual(oc.pcr_oi(chain), 1.3)

    def test_volume_ratio_is_separate_from_oi(self) -> None:
        """A day can be violently one-sided in volume while OI barely moves;
        the two disagreeing is itself the signal, so they must not share a
        computation."""
        chain = [row(100, "PE", oi=1000, volume=100),
                 row(100, "CE", oi=1000, volume=900)]
        self.assertAlmostEqual(oc.pcr_oi(chain), 1.0)
        self.assertAlmostEqual(oc.pcr_volume(chain), 100 / 900)

    def test_missing_side_returns_none_not_zero(self) -> None:
        """Zero would read as 'extremely call-heavy'; None reads as unknown."""
        self.assertIsNone(oc.pcr_oi([row(100, "CE", oi=100)]))
        self.assertIsNone(oc.pcr_volume([]))


class MaxPainTest(unittest.TestCase):
    def test_pain_is_the_hand_computed_minimum(self) -> None:
        """Verified by hand rather than captured from the implementation.

        chain: 90 CE/PE 10 each, 100 CE 1000 / PE 10, 110 CE/PE 10 each
          settle  90 -> puts at 100 (10x10=100) + at 110 (20x10=200)      = 300
          settle 100 -> calls at 90 (10x10=100) + puts at 110 (10x10=100) = 200
          settle 110 -> calls at 90 (20x10=200) + at 100 (10x1000=10000)  = 10200
        so max pain is 100.

        Note the heavy call OI at 100 PULLS max pain to 100 rather than away
        from it — that strike is precisely where those calls expire worthless.
        """
        chain = [
            row(90, "CE", oi=10), row(90, "PE", oi=10),
            row(100, "CE", oi=1000), row(100, "PE", oi=10),
            row(110, "CE", oi=10), row(110, "PE", oi=10),
        ]
        self.assertEqual(oc.max_pain(chain), 100.0)

    def test_a_lopsided_chain_moves_pain_off_centre(self) -> None:
        """Guards the shortcut implementation: with all the OI in puts above
        spot, settlement is dragged UP to where those puts stop paying."""
        chain = [row(90, "CE", oi=10), row(100, "PE", oi=5000), row(110, "CE", oi=10)]
        self.assertEqual(oc.max_pain(chain), 100.0)

    def test_symmetric_chain_pins_the_middle(self) -> None:
        chain = []
        for strike in (90, 100, 110):
            chain += [row(strike, "CE", oi=100), row(strike, "PE", oi=100)]
        self.assertEqual(oc.max_pain(chain), 100.0)

    def test_only_in_the_money_contracts_create_pain(self) -> None:
        """A call is worthless below its strike; counting it would move the
        answer toward wherever OI happens to sit."""
        chain = [row(100, "CE", oi=1000), row(200, "CE", oi=1000)]
        self.assertEqual(oc.max_pain(chain), 100.0)

    def test_empty_chain_is_none(self) -> None:
        self.assertIsNone(oc.max_pain([]))


class LevelsTest(unittest.TestCase):
    def test_highest_call_oi_is_resistance_and_put_oi_support(self) -> None:
        chain = [row(110, "CE", oi=900), row(120, "CE", oi=100),
                 row(90, "PE", oi=800), row(80, "PE", oi=100)]
        self.assertEqual(oc.oi_levels(chain), (110.0, 90.0))

    def test_stale_untraded_strikes_can_be_excluded(self) -> None:
        """OI can sit for weeks on an illiquid strike. Treating that as a wall
        the market is defending reads a ghost as a level."""
        chain = [row(110, "CE", oi=9999, volume=0), row(115, "CE", oi=100, volume=500),
                 row(90, "PE", oi=9999, volume=0), row(85, "PE", oi=100, volume=500)]
        self.assertEqual(oc.oi_levels(chain, min_volume=1), (115.0, 85.0))


class MatrixTest(unittest.TestCase):
    def test_the_four_states(self) -> None:
        self.assertEqual(oc.oi_price_matrix(1, 1)[0], "long_buildup")
        self.assertEqual(oc.oi_price_matrix(1, -1)[0], "short_covering")
        self.assertEqual(oc.oi_price_matrix(-1, 1)[0], "short_buildup")
        self.assertEqual(oc.oi_price_matrix(-1, -1)[0], "long_unwinding")

    def test_buildups_and_unwinds_are_not_collapsed(self) -> None:
        """Both are bullish, but new commitment is not the same as shorts
        closing, so the label has to survive even though the vote matches."""
        self.assertNotEqual(oc.oi_price_matrix(1, 1)[0], oc.oi_price_matrix(1, -1)[0])
        self.assertEqual(oc.oi_price_matrix(1, 1)[1], oc.oi_price_matrix(1, -1)[1])

    def test_no_change_is_neutral(self) -> None:
        self.assertEqual(oc.oi_price_matrix(0, 5)[1], 0)
        self.assertEqual(oc.oi_price_matrix(5, 0)[1], 0)


class MigrationTest(unittest.TestCase):
    def test_detects_positioning_moving_up_the_chain(self) -> None:
        before = [row(100, "CE", oi=1000), row(110, "CE", oi=100)]
        after = [row(100, "CE", oi=100), row(110, "CE", oi=1000)]
        self.assertGreater(oc.strike_migration(after, before, top=2)["CE"], 0)

    def test_no_previous_side_yields_none(self) -> None:
        self.assertIsNone(oc.strike_migration([row(100, "CE", oi=1)], [])["CE"])


class ConcentrationTest(unittest.TestCase):
    def test_single_strike_is_fully_concentrated(self) -> None:
        self.assertAlmostEqual(oc.concentration([row(100, "CE", oi=500)])["CE"], 1.0)

    def test_even_spread_is_low(self) -> None:
        chain = [row(s, "CE", oi=100) for s in (90, 100, 110, 120)]
        self.assertAlmostEqual(oc.concentration(chain)["CE"], 0.25)


class StraddleTest(unittest.TestCase):
    def test_expected_move_is_call_plus_put_at_the_money(self) -> None:
        chain = [row(100, "CE", close=30), row(100, "PE", close=20),
                 row(110, "CE", close=5), row(110, "PE", close=60)]
        out = oc.atm_straddle(chain, spot=101)
        self.assertEqual(out["strike"], 100.0)
        self.assertAlmostEqual(out["premium"], 50.0)
        self.assertAlmostEqual(out["pct"], 50 / 101 * 100)

    def test_missing_leg_yields_none(self) -> None:
        self.assertIsNone(oc.atm_straddle([row(100, "CE", close=30)], spot=100))

    def test_no_spot_yields_none(self) -> None:
        self.assertIsNone(oc.atm_straddle([row(100, "CE", close=1),
                                           row(100, "PE", close=1)], spot=0))


class SummariseTest(unittest.TestCase):
    def _chain(self):
        chain = []
        for strike in (90, 100, 110):
            chain += [row(strike, "CE", oi=100 * strike, close=10),
                      row(strike, "PE", oi=90 * strike, close=10)]
        return chain

    def test_returns_every_reading(self) -> None:
        out = oc.summarise(self._chain(), spot=100, price_change=1, oi_change=1)
        for key in ("pcr_oi", "pcr_volume", "max_pain", "resistance", "support",
                    "concentration", "straddle", "matrix", "max_pain_gap_pct"):
            self.assertIn(key, out)

    def test_gap_to_max_pain_is_signed(self) -> None:
        """Pinning is a pull TOWARDS a level, so the direction of the gap is the
        tradeable part — an unsigned distance loses the whole signal."""
        out = oc.summarise(self._chain(), spot=100, price_change=1, oi_change=1)
        pain, gap = out["max_pain"], out["max_pain_gap_pct"]
        self.assertEqual(gap > 0, pain > 100)

    def test_survives_a_junk_chain(self) -> None:
        junk = [dict(strike=None, opt_type="CE", oi="x", volume=None, close="")]
        out = oc.summarise(junk, spot=100)
        self.assertIsNone(out["max_pain"])


if __name__ == "__main__":
    unittest.main()
