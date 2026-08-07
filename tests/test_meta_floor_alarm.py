"""A probability floor above the model's output range kills the book silently.

WHAT HAPPENED. The meta model was retrained on 2026-08-02. Its full-sample
outputs came out compressed — across 163 live signals on 2026-08-06 the highest
P(win) it produced was 0.561. The shipped floor was 0.60, a hand-typed `--floor`
CLI argument (default 0.58) stored verbatim in the model bundle and never once
checked against what the model actually emits.

So every daily lane went to zero. Not throttled — zero:

    lane            signals  >= threshold  past meta   model's best p
    swing_meanrev       161           161          0            0.561
    mom_breakout         20            20          0            0.484
    gap_momentum          4             1          0            0.401

The last daily-lane entry in the live book is 2026-07-29. Five sessions passed
with nobody noticing, because a book taking no trades is indistinguishable from
a quiet market unless something says otherwise. Everything that DID trade in
that window — volume_surge, index_options, manual — bypasses this gate.

The floor is deliberately NOT lowered here; on a re-run over 27,731 labelled
events the gate adds no edge and the raw signal set is negative, so the daily
book taking nothing is the better of the two mistakes. What is fixed is the
SILENCE: this configuration must announce itself.
"""
from __future__ import annotations

import inspect
import unittest

from app import v2_live


class AlarmExistsTest(unittest.TestCase):
    def test_rejecting_everything_is_logged_as_an_error(self) -> None:
        src = inspect.getsource(v2_live.poll_market)
        self.assertIn("META GATE REJECTED ALL", src)
        self.assertIn("if cand and not kept:", src,
                      "the alarm must fire on total rejection, not on a partial cut")

    def test_the_alarm_reports_both_numbers(self) -> None:
        """"Nothing passed" is not actionable. "Floor 0.60, best 0.561" is."""
        src = inspect.getsource(v2_live.poll_market)
        start = src.index("META GATE REJECTED ALL")
        window = src[start:start + 700]
        self.assertIn("mfloor", window, "must report the floor it applied")
        self.assertIn("max(seen)", window, "must report what the model could reach")

    def test_it_does_not_fire_when_something_survives(self) -> None:
        src = inspect.getsource(v2_live.poll_market)
        self.assertNotIn("if not kept:", src.replace("if cand and not kept:", ""),
                         "an empty candidate list is a quiet market, not a mismatch")


class TheClaimIsHonestTest(unittest.TestCase):
    """The comment above the gate asserted an edge that re-measurement refuted.

    A false claim in a comment is worse than no comment: it is what stops the
    next person re-checking. This pins the corrected numbers so the old claim
    cannot quietly return."""

    def _src(self) -> str:
        return inspect.getsource(v2_live.poll_market)

    def test_the_claim_is_never_asserted_undressed(self) -> None:
        """The old wording is deliberately still quoted — you cannot tell the
        next reader a claim was wrong without repeating it. What must never
        return is the claim standing on its own as a statement of fact, so pin
        the refutation to it rather than banning the words."""
        src = self._src()
        idx = src.find("proven OOS on 15mo of real data")
        self.assertGreater(idx, -1, "if the quote goes, so should this test")
        context = src[max(0, idx - 400):idx + 400]
        self.assertIn("did not survive", context,
                      "the claim must be adjacent to its refutation, not free-standing")
        self.assertIn("THIS GATE DOES NOT CURRENTLY ADD EDGE", src)

    def test_the_measured_numbers_are_recorded(self) -> None:
        src = self._src()
        for fragment in ("BASELINE", "META", "PF 0.91", "PF 0.89", "27,731"):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, src)

    def test_the_warning_against_unblocking_is_present(self) -> None:
        """The obvious "fix" — drop the floor so the lane trades again — turns
        on a lane measured at -0.12%/trade over 26,147 events."""
        src = self._src()
        self.assertIn("-0.12%/trade", src)
        self.assertIn("26,147", src)


class FloorIsStillInPlaceTest(unittest.TestCase):
    def test_the_gate_still_applies_the_floor(self) -> None:
        """Guard against a future "cleanup" removing the gate entirely."""
        src = inspect.getsource(v2_live.poll_market)
        self.assertIn("if p is not None and p < thr:", src)
        self.assertIn("continue", src)


if __name__ == "__main__":
    unittest.main()
