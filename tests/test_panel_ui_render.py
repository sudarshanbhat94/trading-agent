"""The analyst panel card, executed as real JavaScript.

The panel exists to make disagreement visible, so the assertions that matter
are that dissent actually reaches the markup and that an abstaining analyst
renders as "abstained" rather than as a neutral vote — the two things that
would silently undo the module's whole point.

Run against real `analysts.analyse()` output, so a field-name mismatch across
the Python/JS boundary fails the build.
"""

from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
import tempfile
import unittest

from app import analysts

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
V2_WEB = REPO_ROOT / "app" / "v2_web.py"


def _function_source(source: str, name: str) -> str:
    start = source.index(f"function {name}(")
    end = source.index("\nfunction ", start)
    return source[start:end] + "\n"


def _extract_js() -> str:
    source = V2_WEB.read_text(encoding="utf-8")
    return (_function_source(source, "esc")
            + _function_source(source, "stanceBar")
            + source[source.index("function panelHtml("):source.index("function recBadge(score){")])


def _render(payload) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        data = pathlib.Path(tmp) / "p.json"
        data.write_text(json.dumps(payload), encoding="utf-8")
        script = _extract_js() + (
            "\nconst p=JSON.parse(require('fs').readFileSync(%r,'utf8'));"
            "\nprocess.stdout.write(panelHtml(p,'\\u20b9'));" % str(data)
        )
        path = pathlib.Path(tmp) / "r.js"
        path.write_text(script, encoding="utf-8")
        out = subprocess.run(["node", str(path)], capture_output=True, text=True, timeout=30)
        if out.returncode != 0:
            raise AssertionError(f"panelHtml threw: {out.stderr[:500]}")
        return out.stdout


BULL = {
    "price": 105.0, "close": 105.0, "sma20": 100.0, "sma50": 95.0,
    "rvol": 1.4, "atr_pct": 0.02, "regime_on": True, "news_score": 0.6,
    "catalysts": [{"headline": "Q1 results beat", "type": "results"}],
    "technicals": {"supertrend": {"direction": "up", "value": 98.0},
                   "ichimoku": {"kijun": 100.0}, "stale": False},
}

CONFLICTED = dict(BULL, news_score=-0.8, regime_on=False, atr_pct=0.07, rvol=0.4,
                  catalysts=[{"headline": "Order cancelled", "type": "order"}])


@unittest.skipUnless(shutil.which("node"), "node is required to execute the template")
class PanelRenderTest(unittest.TestCase):
    def test_renders_consensus_and_participation(self) -> None:
        html = _render(analysts.analyse(BULL))
        self.assertIn("analyst panel", html)
        self.assertIn("reporting", html)
        self.assertIn("confidence", html)

    def test_every_analyst_appears(self) -> None:
        html = _render(analysts.analyse(BULL))
        for agent in ("technical", "catalyst", "risk", "position"):
            self.assertIn(agent, html)

    def test_abstainers_render_as_abstained_not_neutral(self) -> None:
        """The module's core distinction must survive into the UI."""
        panel = analysts.analyse(BULL)
        self.assertTrue(any(o["abstained"] for o in panel["opinions"]))
        self.assertIn("abstained", _render(panel))

    def test_dissent_is_shown_prominently(self) -> None:
        panel = analysts.analyse(CONFLICTED)
        self.assertTrue(panel["cio"]["dissent"], "fixture must actually conflict")
        html = _render(panel)
        self.assertIn("analysts disagree", html)
        self.assertIn("var(--warn)", html)

    def test_no_dissent_block_when_analysts_agree(self) -> None:
        panel = analysts.analyse(BULL)
        self.assertEqual(panel["cio"]["dissent"], [])
        self.assertNotIn("analysts disagree", _render(panel))

    def test_rationales_reach_the_markup(self) -> None:
        panel = analysts.analyse(BULL)
        html = _render(panel)
        for opinion in panel["opinions"]:
            self.assertIn(opinion["rationale"][:30], html)

    def test_evidence_is_collapsible(self) -> None:
        self.assertIn("<details", _render(analysts.analyse(BULL)))

    def test_no_undefined_leaks(self) -> None:
        self.assertNotIn("undefined", _render(analysts.analyse(BULL)))

    def test_all_abstaining_still_renders(self) -> None:
        html = _render(analysts.analyse({}))
        self.assertIn("no view", html)
        self.assertNotIn("undefined", html)

    def test_missing_panel_renders_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            script = _extract_js() + "\nprocess.stdout.write(panelHtml(null,'x'));"
            path = pathlib.Path(tmp) / "s.js"
            path.write_text(script, encoding="utf-8")
            out = subprocess.run(["node", str(path)], capture_output=True, text=True, timeout=30)
            self.assertEqual(out.returncode, 0)
            self.assertEqual(out.stdout.strip(), "")


@unittest.skipUnless(shutil.which("node"), "node is required to execute the template")
class StanceBarTest(unittest.TestCase):
    """The bar is centred at zero, so direction must be visually unambiguous."""

    def _bar(self, value: float) -> str:
        with tempfile.TemporaryDirectory() as tmp:
            script = _extract_js() + f"\nprocess.stdout.write(stanceBar({value}));"
            path = pathlib.Path(tmp) / "s.js"
            path.write_text(script, encoding="utf-8")
            return subprocess.run(["node", str(path)], capture_output=True,
                                  text=True, timeout=30).stdout

    def test_bullish_is_green_and_starts_at_the_centre(self) -> None:
        bar = self._bar(0.8)
        self.assertIn("var(--up)", bar)
        self.assertIn("left:50%", bar)

    def test_bearish_is_red_and_ends_at_the_centre(self) -> None:
        bar = self._bar(-0.8)
        self.assertIn("var(--dn)", bar)
        self.assertIn("left:10%", bar)      # 50 - 40

    def test_neutral_is_muted(self) -> None:
        self.assertIn("var(--mut)", self._bar(0.0))

    def test_width_scales_with_magnitude(self) -> None:
        self.assertIn("width:50%", self._bar(1.0))
        self.assertIn("width:25%", self._bar(0.5))


class EscapingTest(unittest.TestCase):
    def test_rationale_text_is_escaped(self) -> None:
        if not shutil.which("node"):
            self.skipTest("node required")
        panel = analysts.analyse(BULL)
        panel["opinions"][0]["rationale"] = "<img src=x onerror=alert(1)>"
        html = _render(panel)
        self.assertNotIn("<img src=x", html)
        self.assertIn("&lt;img", html)


if __name__ == "__main__":
    unittest.main()
