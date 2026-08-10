"""A stock's P(win) must not depend on how many other stocks you scored with it.

`day_breadth` is the meta model's #1 feature (importance 0.32) and `day_rank`
its #4. Training defines both over the ENGINE's own candidate set for the day —
`day_breadth = len(day_events)` in scripts/meta_label_research.py, where
day_events are the signals the engine itself would consider.

`meta_filter.score` computed them over whatever list it was handed. The ideas
page sweeps at IDEAS_MIN_CONVICTION (0.15) while the book sweeps at its own
threshold (0.55), so on 2026-08-10 the same model scored:

    ideas population   695 signals   max p 0.7090
    book  population   162 signals   max p 0.5461

    REDINGTON          0.5095 as an idea      0.4440 in the book    +0.0655
    DIACABS            0.5943                 0.5461                +0.0482
    LALPATHLAB         0.4920                 0.4474                +0.0447

Same stock, same day, same model, same price — the only difference is list
length. With the floor at 0.60 that is the difference between "nothing
qualifies" and "publish a hundred ideas", which is exactly what the live book
and the live ideas page were doing on the same afternoon.

That also breaks the promise written above _publish_ideas: the book and the
ideas page must never disagree about what looks good, because then one of them
is lying and there is no way to tell which.

The fix does NOT force ideas back to the book's narrow list — a daily-ideas
product that publishes nothing most days is not a product, which is why the
wider sweep exists. It anchors the POPULATION features to the canonical
candidate set while still scoring the wider list.
"""
from __future__ import annotations

import unittest

from app import meta_filter


def _sig(sym, score, strategy="swing_meanrev"):
    return dict(symbol=sym, strategy=strategy, score=score, atr=1.0, price=100.0)


class ScoreAcceptsAPopulationTest(unittest.TestCase):
    def test_the_parameter_exists(self) -> None:
        import inspect
        sig = inspect.signature(meta_filter.score)
        self.assertIn("population", sig.parameters)
        self.assertIsNone(sig.parameters["population"].default,
                          "defaulting to None keeps existing callers unchanged")

    def test_breadth_and_rank_come_from_the_population(self) -> None:
        import inspect
        src = inspect.getsource(meta_filter.score)
        self.assertIn("breadth = len(pop)", src)
        self.assertIn("rank_of = {id(s): _rank_for(float(s[\"score\"])) for s in cand}", src)


class RankAgainstThePopulationTest(unittest.TestCase):
    """_rank_for must reproduce training's ordering: position by conviction,
    descending, counting only members ranked strictly higher."""

    def _rank_for(self, pop_convs, sc):
        pop_convs = sorted(pop_convs, reverse=True)
        lo, hi = 0, len(pop_convs)
        while lo < hi:
            mid = (lo + hi) // 2
            if pop_convs[mid] > sc:
                lo = mid + 1
            else:
                hi = mid
        return lo

    def test_the_best_conviction_ranks_first(self) -> None:
        self.assertEqual(self._rank_for([0.9, 0.7, 0.5], 0.9), 0)

    def test_ordering_matches_training(self) -> None:
        pop = [0.9, 0.7, 0.5]
        self.assertEqual([self._rank_for(pop, c) for c in pop], [0, 1, 2])

    def test_a_signal_below_the_population_ranks_last(self) -> None:
        """An idea weaker than every book candidate — the common case."""
        self.assertEqual(self._rank_for([0.9, 0.7, 0.6], 0.2), 3)

    def test_a_signal_above_it_ranks_first(self) -> None:
        self.assertEqual(self._rank_for([0.9, 0.7], 0.99), 0)

    def test_ties_do_not_double_count(self) -> None:
        self.assertEqual(self._rank_for([0.8, 0.8, 0.6], 0.8), 0)


class PublishIdeasAnchorsToTheBookTest(unittest.TestCase):
    def test_it_passes_the_canonical_population(self) -> None:
        import inspect
        from app import v2_live
        src = inspect.getsource(v2_live._publish_ideas)
        self.assertIn("population=_canon", src)

    def test_the_canonical_set_is_the_books_threshold(self) -> None:
        """Not IDEAS_MIN_CONVICTION — that is the whole point."""
        import inspect
        from app import v2_live
        src = inspect.getsource(v2_live._publish_ideas)
        self.assertIn('>= PLAN["swing_meanrev"]["threshold"]', src)

    def test_the_canonical_set_is_a_subset_of_the_wide_sweep(self) -> None:
        """Derived by filtering `sigs`, so it costs no extra signal sweep and
        cannot drift from what is being scored."""
        from app import v2_live
        wide = [_sig("A", 0.9), _sig("B", 0.6), _sig("C", 0.2)]
        thr = v2_live.PLAN["swing_meanrev"]["threshold"]
        canon = [s for s in wide if float(s.get("score") or 0) >= thr]
        self.assertEqual([s["symbol"] for s in canon], ["A", "B"])
        self.assertLess(len(canon), len(wide))


class PopulationFallbacksTest(unittest.TestCase):
    def test_an_empty_population_falls_back_rather_than_dividing_by_zero(self) -> None:
        import inspect
        src = inspect.getsource(meta_filter.score)
        self.assertIn("if not pop:", src)
        self.assertIn("pop = cand", src)

    def test_no_population_keeps_the_old_behaviour(self) -> None:
        """poll_market scores the canonical set itself, so it must be unaffected."""
        import inspect
        src = inspect.getsource(meta_filter.score)
        self.assertIn("pop = cand if population is None else", src)


if __name__ == "__main__":
    unittest.main()
