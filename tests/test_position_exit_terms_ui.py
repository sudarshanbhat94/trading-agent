"""The position row's exit terms, executed as real JavaScript.

The row used to say only `exit at <price>` in small grey text, so a trailing
stop was indistinguishable from a fixed one and the profit target was not shown
at all. The operator could not tell how a position would be closed. These
assertions run the shipped JS against the shipped Python payload shape, which
is the only thing that catches a field-name mismatch between the two.
"""

from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
import tempfile
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
V2_WEB = REPO_ROOT / "app" / "v2_web.py"


def _function_source(source: str, name: str) -> str:
    """Slice a JS function out of the ACTIVE template only.

    v2_web.py contains three SPA_HTML definitions and only the LAST is served;
    the earlier ones are dead. Searching the whole file finds a function in dead
    code and passes while the shipped page throws ReferenceError — which is
    exactly how a blank Account tab reached production. Anchor to the active
    template so the tests exercise what is actually served.
    """
    active = source.rindex('SPA_HTML = r"""')
    start = source.index(f"function {name}(", active)
    end = source.index("\nfunction ", start)
    return source[start:end] + "\n"


def _run(payload: dict) -> str:
    source = V2_WEB.read_text(encoding="utf-8")
    js = _function_source(source, "exitTerms")
    script = js + "\nconsole.log(exitTerms(" + json.dumps(payload) + "));\n"
    with tempfile.TemporaryDirectory() as tmp:
        path = pathlib.Path(tmp) / "t.js"
        path.write_text(script, encoding="utf-8")
        out = subprocess.run([shutil.which("node") or "node", str(path)],
                             capture_output=True, text=True, timeout=30)
    if out.returncode != 0:
        raise AssertionError(out.stderr)
    return out.stdout


TRAILING = dict(market="IN", stop=118.5, stop_kind="trailing", stop_base=96.0,
                trail_pct=2.5, peak=121.5, target=0.0, stop_away=-2.5,
                target_away=0.0, stop_from_entry=+18.5, target_from_entry=0.0)
FIXED = dict(market="IN", stop=96.0, stop_kind="fixed", stop_base=96.0,
             trail_pct=0.0, peak=104.0, target=132.0, stop_away=-7.7,
             target_away=+26.9, stop_from_entry=-4.0, target_from_entry=+32.0)
# The position from the screenshot: bought 112.90, now 139.50. A stop set at
# -35% OF ENTRY displayed as "-47.4%" because the only percentage shown was
# distance from the live price. Same setting, two positions, two numbers.
OPTION = dict(market="IN", stop=73.39, stop_kind="fixed", stop_base=73.39,
              trail_pct=0.0, peak=139.5, target=180.64, stop_away=-47.4,
              target_away=+29.5, stop_from_entry=-35.0, target_from_entry=+60.0)


@unittest.skipIf(shutil.which("node") is None, "node not available")
class ExitTermsTest(unittest.TestCase):
    def test_a_trailing_stop_says_it_rises(self) -> None:
        """The whole point: the operator read 'no target' as 'never exits'."""
        html = _run(TRAILING)
        self.assertIn("trailing stop", html)
        self.assertIn("rises with price", html)
        self.assertIn("118.5", html)

    def test_a_trailing_stop_shows_the_peak_it_follows(self) -> None:
        html = _run(TRAILING)
        self.assertIn("121.5", html)
        self.assertIn("2.5%", html)

    def test_no_target_is_stated_explicitly_not_left_blank(self) -> None:
        html = _run(TRAILING)
        self.assertIn("no fixed target", html)

    def test_a_fixed_stop_is_not_described_as_trailing(self) -> None:
        html = _run(FIXED)
        self.assertIn("<b>stop</b>", html)
        self.assertNotIn("rises with price", html)

    def test_a_target_is_shown_with_its_distance(self) -> None:
        html = _run(FIXED)
        self.assertIn("132", html)
        self.assertIn("26.9% away", html)
        self.assertIn("+32.0% from entry", html)

    def test_the_from_entry_figure_carries_a_sign(self) -> None:
        """A stop below entry and a target above it must be readable at a
        glance. The DISTANCE deliberately does not carry one — it is a gap, and
        a signed gap beside a signed return is what looked contradictory."""
        self.assertIn("+18.5% from entry", _run(TRAILING))
        self.assertIn("-4.0% from entry", _run(FIXED))
        self.assertIn("+32.0% from entry", _run(FIXED))

    def test_us_positions_render_in_dollars(self) -> None:
        payload = dict(FIXED, market="US")
        html = _run(payload)
        self.assertIn("$", html)
        self.assertNotIn("₹", html)


if __name__ == "__main__":
    unittest.main()

    def test_both_percentages_are_shown(self) -> None:
        """The confusion this fixes: a -35% stop rendering as -47.4% once the
        position was up 23%. Distance-from-here and the terms the trade was
        opened on are different questions; showing only one reads as an error."""
        html = _run("exitTerms", OPTION)
        self.assertIn("-35.0% from entry", html)
        self.assertIn("47.4% away", html)
        self.assertIn("+60.0% from entry", html)
        self.assertIn("29.5% away", html)

    def test_the_configured_terms_come_first(self) -> None:
        """What the settings configure is the primary number; how far away it
        happens to be right now is context."""
        html = _run("exitTerms", OPTION)
        self.assertLess(html.index("from entry"), html.index("away"))

    def test_the_away_figure_is_unsigned_so_it_cannot_be_misread(self) -> None:
        """It is a distance, not a return. A signed one sitting beside a signed
        from-entry figure is what made two numbers look contradictory."""
        html = _run("exitTerms", OPTION)
        self.assertNotIn("-47.4% away", html)
        self.assertNotIn("+29.5% away", html)
