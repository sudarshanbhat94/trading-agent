"""Published ideas: sizing, levels, tiering, tracking, and the honest scoreboard.

The thing being sold here is a number a subscriber will act on with their own
money, so the tests care most about the places where an optimistic reading would
flatter the product: ties inside a bar, expired ideas, win rates off tiny
samples, and levels moving after publication.
"""
from __future__ import annotations

import sqlite3
import unittest

from app import ideas, v2_live


def _db():
    con = sqlite3.connect(":memory:")
    v2_live.ensure_schema(con)
    return con


# Ideas are published by SLEEVES now; `visible()` filters anything else out as
# a retired-lane leftover. A fixture publishing under a legacy lane name would
# be testing a path the product no longer has.
def _cand(symbol="WABAG", price=1000.0, atr=20.0, strategy="mean_reversion", **kw):
    return dict(symbol=symbol, price=price, atr=atr, strategy=strategy, **kw)


class LevelsTest(unittest.TestCase):
    """R-multiples of the lane's OWN measured stop, not flat percentages."""

    def test_targets_are_one_two_and_three_R(self) -> None:
        lv = ideas.levels(1000.0, 20.0, 3.0)      # R = 60
        self.assertEqual(lv["stop"], 940.0)
        self.assertEqual((lv["t1"], lv["t2"], lv["t3"]), (1060.0, 1120.0, 1180.0))

    def test_a_wider_lane_stop_widens_the_targets(self) -> None:
        """mom_breakout runs a 2 ATR stop and swing_meanrev 3 ATR, so the same
        stock must not get the same ladder from both lanes."""
        narrow = ideas.levels(1000.0, 20.0, 2.0)
        wide = ideas.levels(1000.0, 20.0, 3.0)
        self.assertGreater(wide["t1"] - 1000, narrow["t1"] - 1000)
        self.assertLess(wide["stop"], narrow["stop"])

    def test_the_ladder_scales_with_the_stock_not_the_price(self) -> None:
        """A Rs 27 stock and a Rs 5,500 stock get proportionate ladders because
        ATR is per-stock. This is the whole reason for not using percentages."""
        cheap = ideas.levels(27.1, 0.9, 3.0)
        dear = ideas.levels(5529.5, 121.0, 3.0)
        self.assertAlmostEqual((27.1 - cheap["stop"]) / 27.1, 0.0996, places=3)
        self.assertAlmostEqual((5529.5 - dear["stop"]) / 5529.5, 0.0656, places=3)

    def test_a_missing_atr_publishes_nothing(self) -> None:
        """An idea without a real stop distance cannot be sized, and a guessed
        stop is worse than no idea."""
        for bad in (0, None, -1):
            with self.subTest(atr=bad):
                self.assertEqual(ideas.levels(1000.0, bad, 3.0), {})

    def test_a_stop_below_zero_is_refused(self) -> None:
        """A very high ATR against a low price would otherwise produce a
        negative stop, which prices a risk of more than 100%."""
        self.assertEqual(ideas.levels(10.0, 5.0, 3.0), {})


class SizingTest(unittest.TestCase):
    def test_every_idea_risks_the_same_rupees(self) -> None:
        """The point of risk-based sizing: a Rs 27 stock and a Rs 5,500 stock
        both put ~1% of the account at risk, so one cannot quietly dominate."""
        for entry, stop in ((27.1, 24.4), (563.0, 508.4), (5529.5, 5287.5)):
            with self.subTest(entry=entry):
                qty = ideas.size(entry, stop)
                risk = qty * (entry - stop)
                self.assertLessEqual(risk, 1000.0)
                self.assertGreater(risk, 900.0)

    def test_a_tight_stop_cannot_ask_for_more_than_the_account(self) -> None:
        """Rs 2 of risk on a Rs 900 stock is 500 shares = Rs 450,000 of a
        Rs 100,000 account. The notional cap is what stops that."""
        qty = ideas.size(900.0, 898.0)
        self.assertLessEqual(qty * 900.0, ideas.CAPITAL * ideas.MAX_NOTIONAL_PCT + 900)

    def test_an_unsizeable_idea_returns_zero_not_one(self) -> None:
        self.assertEqual(ideas.size(1000.0, 1000.0), 0)
        self.assertEqual(ideas.size(1000.0, 1100.0), 0)
        self.assertEqual(ideas.size(0, 0), 0)

    def test_build_refuses_rather_than_publishing_a_zero_quantity(self) -> None:
        self.assertEqual(ideas.build(_cand(price=5.0, atr=4.0), 3.0, 1), {})


class TierTest(unittest.TestCase):
    def test_the_top_idea_goes_to_every_paid_tier(self) -> None:
        """Starter gets the SAME rank-1 idea Elite does. Selling a better first
        pick would mean deliberately publishing a worse one to everyone else."""
        self.assertEqual(ideas.tier_for_rank(1), "watch")

    def test_the_paid_tiers_add_breadth(self) -> None:
        self.assertEqual([ideas.tier_for_rank(r) for r in (1, 2, 3, 4, 5)],
                         ["watch", "paper", "paper", "auto", "auto"])

    def test_free_sees_none(self) -> None:
        self.assertEqual(ideas.allowance("free"), 0)

    def test_the_tiers_and_the_allowance_cannot_disagree(self) -> None:
        """tier_for_rank is derived FROM PER_DAY, so the gate and the pricing
        page are the same fact."""
        for tier, n in ideas.PER_DAY.items():
            if not n:
                continue
            with self.subTest(tier=tier):
                self.assertEqual(ideas.tier_for_rank(n), tier)


class PublishTest(unittest.TestCase):
    def setUp(self) -> None:
        self.con = _db()
        self.atr_stop = lambda s: v2_live.PLAN.get(s, {}).get("atr_stop") or 3.0

    def _pub(self, cands, date="2026-08-04", ts="2026-08-04T09:20:00+05:30"):
        return ideas.publish(self.con, "IN", cands, self.atr_stop, date, ts)

    def test_it_writes_the_days_ideas(self) -> None:
        self.assertEqual(self._pub([_cand("A"), _cand("B")]), 2)

    def test_republishing_the_same_day_changes_nothing(self) -> None:
        """poll_market runs every five minutes. A second pass must not create a
        duplicate."""
        self._pub([_cand("A")])
        self.assertEqual(self._pub([_cand("A")]), 0)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM v2_ideas").fetchone()[0], 1)

    def test_a_published_level_is_never_rewritten(self) -> None:
        """THE important one. The candidate list moves with the tape all day;
        walking the entry or the stop after a subscriber acted on it would make
        their screenshot stop matching the page."""
        self._pub([_cand("A", price=1000.0, atr=20.0)])
        before = self.con.execute("SELECT entry,stop,t1 FROM v2_ideas").fetchone()
        self._pub([_cand("A", price=1400.0, atr=55.0)])     # same day, moved a lot
        self.assertEqual(self.con.execute("SELECT entry,stop,t1 FROM v2_ideas").fetchone(),
                         before)

    def test_the_same_symbol_can_be_published_again_another_day(self) -> None:
        self._pub([_cand("A")])
        self.assertEqual(self._pub([_cand("A")], date="2026-08-05"), 1)

    def test_it_publishes_at_most_five(self) -> None:
        self.assertEqual(self._pub([_cand(f"S{i}") for i in range(9)]), 5)

    def test_engine_order_is_preserved(self) -> None:
        """Ideas come out in the engine's own ranked order, after the meta
        filter — not re-sorted by something the page prefers."""
        self._pub([_cand("FIRST"), _cand("SECOND"), _cand("THIRD")])
        rows = self.con.execute("SELECT symbol FROM v2_ideas ORDER BY rank").fetchall()
        self.assertEqual([r[0] for r in rows], ["FIRST", "SECOND", "THIRD"])

    def test_an_unsizeable_candidate_is_skipped_not_published_blank(self) -> None:
        self._pub([_cand("GOOD"), _cand("BAD", price=5.0, atr=4.0)])
        self.assertEqual([r[0] for r in self.con.execute("SELECT symbol FROM v2_ideas")],
                         ["GOOD"])

    def test_visible_respects_the_plan(self) -> None:
        self._pub([_cand(f"S{i}") for i in range(5)])
        for plan, n in (("free", 0), ("watch", 1), ("paper", 3), ("auto", 5)):
            with self.subTest(plan=plan):
                self.assertEqual(len(ideas.visible(self.con, "IN", plan)), n)

    def test_conviction_prefers_the_meta_model(self) -> None:
        """meta_p is the number the engine ranks on when it has one."""
        self._pub([_cand("A", score=0.9, meta_p=0.44)])
        self.assertAlmostEqual(
            self.con.execute("SELECT conviction FROM v2_ideas").fetchone()[0], 0.44)


class TrackTest(unittest.TestCase):
    def setUp(self) -> None:
        self.con = _db()
        ideas.publish(self.con, "IN", [_cand("A", price=1000.0, atr=20.0)],
                      lambda s: 3.0, "2026-08-04", "2026-08-04T09:20+05:30")
        # stop 940 | t1 1060 | t2 1120 | t3 1180

    def _track(self, price, high, low, today="2026-08-04"):
        ideas.track(self.con, "IN", {"A": dict(price=price, high=high, low=low)},
                    "2026-08-04T14:00+05:30", today)
        return ideas.visible(self.con, "IN", "auto")[0]

    def test_an_untouched_idea_stays_open(self) -> None:
        r = self._track(1010, 1020, 995)
        self.assertEqual(r["status"], "open")

    def test_t1_closes_it(self) -> None:
        r = self._track(1065, 1065, 1000)
        self.assertEqual(r["status"], "t1")
        self.assertAlmostEqual(r["result_pct"], 6.0)

    def test_the_stop_closes_it(self) -> None:
        r = self._track(935, 1005, 935)
        self.assertEqual(r["status"], "stopped")
        self.assertAlmostEqual(r["result_pct"], -6.0)

    def test_a_bar_that_hits_both_is_recorded_as_a_LOSS(self) -> None:
        """We cannot know which came first inside one bar. Assuming the target
        did would manufacture wins out of ambiguity — the same optimism that
        made reported P&L disagree with equity."""
        r = self._track(1190, 1190, 935)
        self.assertEqual(r["status"], "stopped")
        self.assertLess(r["result_pct"], 0)

    def test_it_records_how_far_it_ran_even_when_stopped(self) -> None:
        """MFE/MAE on losers is what says whether a stop sits inside noise."""
        r = self._track(1190, 1190, 935)
        self.assertAlmostEqual(r["mfe"], 19.0)
        self.assertAlmostEqual(r["mae"], -6.5)

    def test_best_target_keeps_climbing_past_t1(self) -> None:
        self._track(1065, 1065, 1000)
        self.con.execute("UPDATE v2_ideas SET status='open'")   # re-open to observe t2
        r = self._track(1125, 1125, 1000)
        self.assertEqual(r["best_target"], "t2")

    def test_a_closed_idea_is_not_reopened_by_a_later_quote(self) -> None:
        self._track(935, 1005, 935)
        r = self._track(2000, 2000, 1900)
        self.assertEqual(r["status"], "stopped")

    def test_it_expires_after_the_horizon(self) -> None:
        r = self._track(1010, 1015, 1005, today="2026-08-20")
        self.assertEqual(r["status"], "expired")
        self.assertAlmostEqual(r["result_pct"], 1.0)

    def test_a_missing_quote_does_not_resolve_anything(self) -> None:
        """A stock with no live price must stay open rather than being marked
        against a stale or zero price."""
        ideas.track(self.con, "IN", {}, "2026-08-04T14:00+05:30", "2026-08-04")
        self.assertEqual(ideas.visible(self.con, "IN", "auto")[0]["status"], "open")

    def test_a_feed_without_ohlc_still_tracks_on_the_last_price(self) -> None:
        ideas.track(self.con, "IN", {"A": dict(price=935)},
                    "2026-08-04T14:00+05:30", "2026-08-04")
        self.assertEqual(ideas.visible(self.con, "IN", "auto")[0]["status"], "stopped")


class ScoreboardTest(unittest.TestCase):
    def test_losers_are_counted(self) -> None:
        """The operator asked for everything shown, including losers."""
        s = ideas.scoreboard([dict(status="t1", result_pct=6.0),
                              dict(status="stopped", result_pct=-6.0),
                              dict(status="stopped", result_pct=-6.0)])
        self.assertEqual(s["closed"], 3)
        self.assertEqual(s["win_pct"], 33)
        self.assertAlmostEqual(s["avg_pct"], -2.0)

    def test_expired_ideas_are_not_quietly_dropped(self) -> None:
        s = ideas.scoreboard([dict(status="expired", result_pct=-1.2)])
        self.assertEqual(s["closed"], 1)
        self.assertEqual(s["expired"], 1)
        self.assertEqual(s["win_pct"], 0)

    def test_open_ideas_are_excluded_from_the_rate_but_still_counted(self) -> None:
        s = ideas.scoreboard([dict(status="open"), dict(status="t1", result_pct=6.0)])
        self.assertEqual((s["open"], s["closed"], s["win_pct"]), (1, 1, 100))

    def test_nothing_closed_reports_no_rate_rather_than_zero(self) -> None:
        """0% off zero trades is a fabricated statistic."""
        s = ideas.scoreboard([dict(status="open")])
        self.assertIsNone(s["win_pct"])
        self.assertIsNone(s["avg_pct"])

    def test_target_counts_are_cumulative(self) -> None:
        s = ideas.scoreboard([dict(status="t1", best_target="t3", result_pct=6.0)])
        self.assertEqual((s["hit_t1"], s["hit_t2"], s["hit_t3"]), (1, 1, 1))


class IdeaPoolTest(unittest.TestCase):
    """Ideas are NOT the book's buy list.

    Tying them together was the original design mistake: the book's candidate
    list answers "would I risk MY capital right now, in this regime, with a slot
    free", which is empty in a NEUTRAL regime — so the page a subscriber pays
    for showed nothing on the days that matter most.
    """

    def test_cash_parking_etfs_are_excluded(self) -> None:
        """LIQUID scored 0.708 today, the highest p(win) of anything, because it
        drifts up and never falls. It is not a trade, and it would top every
        ideas list the moment the ranking stopped being the trading gate."""
        for sym in ("LIQUID", "LIQUIDETF", "LIQUID1", "LIQUIDIETF", "LIQUIDCASE",
                    "LIQUIDBEES"):
            with self.subTest(symbol=sym):
                self.assertTrue(v2_live._is_cash_etf(sym))

    def test_real_stocks_are_not_excluded(self) -> None:
        for sym in ("RELIANCE", "RBLBANK", "SAILIFE", "WELCORP", "ITC"):
            with self.subTest(symbol=sym):
                self.assertFalse(v2_live._is_cash_etf(sym))

    def test_the_publisher_runs_its_own_signal_sweep(self) -> None:
        """The signature is the fix. It takes the raw history and does its own
        sweep, because BOTH lists it was previously handed had already been
        filtered by the book's conviction threshold — first `cand`, then `sigs`
        from _signals_completed, which applies threshold 0.55 internally. That
        is why swapping one for the other changed nothing."""
        import inspect
        params = list(inspect.signature(v2_live._publish_ideas).parameters)
        for needed in ("tails", "mdf", "asof"):
            self.assertIn(needed, params)
        for gone in ("cand", "sigs", "mp"):
            self.assertNotIn(gone, params)

    def test_the_ideas_bar_is_lower_than_the_books(self) -> None:
        """0.55 is what the book demands before risking capital. Ideas rank on
        the confidence model instead, so the conviction bar only has to exclude
        noise — SONACOMS at 0.499 conviction scored p(win) 0.617."""
        self.assertLess(v2_live.IDEAS_MIN_CONVICTION,
                        v2_live.PLAN["swing_meanrev"]["threshold"])
        self.assertGreater(v2_live.IDEAS_MIN_CONVICTION, 0)

    def test_the_regime_gate_is_not_applied_to_ideas(self) -> None:
        """The regime is a fact about whether the BOOK should deploy capital
        today, not about whether a setup is worth showing. Applying it here is
        what produced an empty page in a NEUTRAL market."""
        import inspect
        src = inspect.getsource(v2_live._publish_ideas)
        body = src[src.index("pool = []"):src.index("pool.sort")]
        self.assertNotIn("regime", body)


class HonestyTest(unittest.TestCase):
    """The page must not sell a target the evidence says costs money."""

    def test_the_measured_lanes_are_flagged(self) -> None:
        for lane in ("swing_meanrev", "mom_breakout"):
            with self.subTest(lane=lane):
                self.assertTrue(ideas.t1_costs_edge(lane))

    def test_the_warning_reaches_the_page(self) -> None:
        import pathlib
        spa = pathlib.Path("app/v2_web.py").read_text(encoding="utf-8")
        spa = spa[spa.rindex('SPA_HTML = r"""'):]
        self.assertIn("gives up edge", spa)

    def test_the_sample_size_is_shown_next_to_the_win_rate(self) -> None:
        """Both moved into the credibility strip when the advisory layout
        landed. The RULE is unchanged — a win rate never appears without its n,
        and a thin record says so — only where it is rendered."""
        import pathlib
        spa = pathlib.Path("app/v2_web.py").read_text(encoding="utf-8")
        spa = spa[spa.rindex('SPA_HTML = r"""'):]
        block = spa[spa.index("function renderIdeas("):]
        block = block[:block.index("\nfunction ")]
        self.assertIn("too few to be a track record", block)
        self.assertIn("of '+st.closed", block)
        self.assertIn("not as evidence", block)


if __name__ == "__main__":
    unittest.main()
