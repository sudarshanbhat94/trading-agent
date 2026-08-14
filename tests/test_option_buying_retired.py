"""Buying index option premium is retired, on measurement.

Not paused, not pending better parameters. Measured on 1,300 sessions of real
F&O bhavcopy — every index, ATM straddle, marked close-to-close on the SAME
contracts:

    LONG premium    -1.09%/session, wins 35% of days, median -3.08%
    SHORT premium   +1.09%/session, wins 65% of days

    BANKNIFTY -0.86%   FINNIFTY -0.85%   MIDCPNIFTY -0.26%   NIFTY -3.04%

Every index. The direction of the trade is wrong, and no gate fixes that.

The other side is not available either. Naked short has the edge and a -217%
day (PF 1.22). Defined-risk condors cap the tail at -35% but net -Rs 286 per
lot per day, because eight legs of NFO friction cost 1,026% of a +Rs 31 gross
edge — and only Rs 160 of that is fixed brokerage, so size does not rescue it.

The live book agreed all along: 139 option trades in August, 18% win, -Rs 9,230.

And it was foreseeable from work already in the file. INDEX_PREMIUM_WARNING has
said since the 404-session study that the straddle "implies 2.20% vs 1.13%
realised", and its own comment admits that was "shown next to the tick-box
rather than enforced behind it".
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest import mock

from app import v2_live


class RetiredTest(unittest.TestCase):
    def test_the_flag_is_set(self) -> None:
        self.assertTrue(v2_live.OPTION_BUYING_RETIRED)

    def test_the_pass_returns_before_doing_anything(self) -> None:
        """The guard must be the first statement, ahead of any config read,
        quote fetch or budget calculation."""
        import inspect
        src = inspect.getsource(v2_live.index_options_pass)
        body = src[src.index('"""', src.index('"""') + 3) + 3:]
        first = [ln.strip() for ln in body.splitlines() if ln.strip()][0]
        self.assertEqual(first, "if OPTION_BUYING_RETIRED:")

    def test_it_reports_why(self) -> None:
        import inspect
        src = inspect.getsource(v2_live.index_options_pass)
        self.assertIn("RETIRED", src)
        self.assertIn("1,300 sessions", src)

    def test_the_pass_does_not_trade(self) -> None:
        with mock.patch.object(v2_live, "_rw") as rw:
            v2_live.index_options_pass("IN")
            rw.assert_not_called()
        self.assertIn("RETIRED", v2_live._status.get("IN", ""))


class SavedSettingsCannotReArmItTest(unittest.TestCase):
    """var/index_options.json on the box holds `auto_trade: true`. A persisted
    UI toggle must not be able to restart a strategy retired on evidence."""

    def test_a_saved_auto_trade_true_is_overridden(self) -> None:
        saved = {"enabled": True, "auto_trade": True,
                 "instruments": ["NIFTY", "BANKNIFTY"], "expiry": "weekly"}
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump(saved, fh)
            path = fh.name
        before = dict(v2_live.INDEX_OPTIONS)
        try:
            with mock.patch.object(v2_live, "INDEX_SETTINGS_FILE", path):
                v2_live.index_settings_load()
            self.assertFalse(v2_live.INDEX_OPTIONS["auto_trade"],
                             "a saved toggle re-armed a retired strategy")
        finally:
            v2_live.INDEX_OPTIONS.clear(); v2_live.INDEX_OPTIONS.update(before)
            os.unlink(path)

    def test_the_loader_forces_it_off(self) -> None:
        import inspect
        src = inspect.getsource(v2_live.index_settings_load)
        self.assertIn("OPTION_BUYING_RETIRED", src)
        self.assertIn('INDEX_OPTIONS["auto_trade"] = False', src)


class EverythingElseIsAlreadyOffTest(unittest.TestCase):
    def test_the_losing_equity_lanes_are_quarantined(self) -> None:
        for lane in ("volume_surge", "gap_momentum", "btst"):
            with self.subTest(lane=lane):
                self.assertIn(lane, v2_live.DISABLED_LANES)

    def test_the_drawdown_brake_covers_every_lane(self) -> None:
        self.assertIn("maxdd_total", v2_live.RISK)

    def test_exits_still_run(self) -> None:
        """Retiring a strategy must never strand what it already holds."""
        import inspect
        src = inspect.getsource(v2_live.exit_monitor)
        self.assertNotIn("OPTION_BUYING_RETIRED", src)
        self.assertNotIn("DISABLED_LANES", src)


if __name__ == "__main__":
    unittest.main()
