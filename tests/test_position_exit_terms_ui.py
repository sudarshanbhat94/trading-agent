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
    start = source.index(f"function {name}(")
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
                target_away=0.0)
FIXED = dict(market="IN", stop=96.0, stop_kind="fixed", stop_base=96.0,
             trail_pct=0.0, peak=104.0, target=132.0, stop_away=-7.7,
             target_away=+26.9)


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
        self.assertIn("+26.9%", html)

    def test_distances_carry_a_sign(self) -> None:
        """-2.5% below and +26.9% above must be readable at a glance."""
        self.assertIn("-2.5%", _run(TRAILING))
        self.assertIn("+26.9%", _run(FIXED))

    def test_us_positions_render_in_dollars(self) -> None:
        payload = dict(FIXED, market="US")
        html = _run(payload)
        self.assertIn("$", html)
        self.assertNotIn("₹", html)


if __name__ == "__main__":
    unittest.main()
