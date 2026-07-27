"""The recommendation card, executed as real JavaScript.

Unit-testing the Python payload and eyeballing the template is not enough: the
Stats tab was blank in production because a template dereferenced a field the
API never sent, and no Python test could see it. This runs the actual `recHtml`
from `v2_web.py` against an actual `build_recommendation()` payload, so a
field-name mismatch across the language boundary fails the build.

It also pins the XSS escaping. Catalyst headlines come from news and NSE
filings — external text going straight into innerHTML.
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

from app import narrative as nar
from app import recommendation as rec

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
V2_WEB = REPO_ROOT / "app" / "v2_web.py"


def _extract_js() -> str:
    """Pull the card's functions out of the Python source that embeds them."""
    source = V2_WEB.read_text(encoding="utf-8")
    start = source.index("function esc(x){")
    end = source.index("function newsHtml(n,s){")
    return source[start:end]


def _render(payload: dict) -> str:
    """Run recHtml in node and return the HTML it produces."""
    with tempfile.TemporaryDirectory() as tmp:
        data_path = pathlib.Path(tmp) / "payload.json"
        data_path.write_text(json.dumps(payload), encoding="utf-8")
        script = _extract_js() + (
            "\nconst r=JSON.parse(require('fs').readFileSync(%r,'utf8'));"
            "\nprocess.stdout.write(recHtml(r,'\\u20b9'));" % str(data_path)
        )
        script_path = pathlib.Path(tmp) / "render.js"
        script_path.write_text(script, encoding="utf-8")
        result = subprocess.run(["node", str(script_path)],
                                capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            raise AssertionError(f"recHtml threw: {result.stderr[:500]}")
        return result.stdout


def _recommendation(**overrides):
    facts = {
        "symbol": "TESTCO", "price": 105.0, "close": 105.0, "conviction": 0.70,
        "sma20": 100.0, "sma50": 95.0, "rs20": 0.04, "atr_pct": 0.02,
        "regime_on": True, "entry": 105.0, "stop": 99.0, "target": 111.0,
        "news": [{"title": "Q1 results beat estimates", "label": "results", "score": 0.6}],
        "news_score": 0.6,
        "technicals": {"supertrend": {"direction": "up", "value": 98.0},
                       "ichimoku": {"kijun": 100.0},
                       "pivot_points": {"s1": 102.0, "s2": 99.0, "r1": 108.0, "r2": 112.0},
                       "stale": False},
    }
    facts.update(overrides)
    result = rec.build_recommendation(facts)
    result["narrative"] = nar.narrate(result)
    return result


@unittest.skipUnless(shutil.which("node"), "node is required to execute the template")
class RecommendationCardTest(unittest.TestCase):
    def test_renders_the_core_fields(self) -> None:
        payload = _recommendation()
        html = _render(payload)
        self.assertIn(payload["rating"], html)
        self.assertIn("confidence", html)
        self.assertIn("bull case", html)
        self.assertIn("evidence", html)

    def test_renders_levels_and_targets(self) -> None:
        html = _render(_recommendation())
        self.assertIn("pivot S1", html)
        self.assertIn("108", html)      # pivot R1 resistance
        self.assertIn("111", html)      # engine target

    def test_narrative_text_is_shown(self) -> None:
        payload = _recommendation()
        html = _render(payload)
        # A distinctive fragment of the deterministic prose.
        self.assertIn("rates this", html)

    def test_no_undefined_leaks_into_the_markup(self) -> None:
        """The Stats-tab failure mode: a template reading a field the payload
        does not have."""
        self.assertNotIn("undefined", _render(_recommendation()))

    def test_insufficient_data_renders_a_refusal_not_a_rating(self) -> None:
        payload = rec.build_recommendation({})
        payload["narrative"] = nar.narrate(payload)
        html = _render(payload)
        self.assertIn("Not enough stored data", html)
        self.assertNotIn("confidence", html)

    def test_missing_recommendation_renders_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            script = _extract_js() + "\nprocess.stdout.write(recHtml(null,'x'));"
            path = pathlib.Path(tmp) / "s.js"
            path.write_text(script, encoding="utf-8")
            out = subprocess.run(["node", str(path)], capture_output=True, text=True, timeout=30)
            self.assertEqual(out.returncode, 0)
            self.assertEqual(out.stdout.strip(), "")

    def test_bearish_payload_renders_the_bear_case(self) -> None:
        payload = _recommendation(
            conviction=0.15, close=90.0, price=90.0, sma20=100.0, sma50=105.0,
            rs20=-0.06, news_score=-0.5,
            technicals={"supertrend": {"direction": "down", "value": 104.0},
                        "ichimoku": {"kijun": 101.0}, "pivot_points": {}, "stale": False},
        )
        html = _render(payload)
        self.assertIn("bear case", html)


@unittest.skipUnless(shutil.which("node"), "node is required to execute the template")
class EscapingTest(unittest.TestCase):
    """Catalyst headlines are external text rendered via innerHTML."""

    def test_script_tag_in_a_headline_is_escaped(self) -> None:
        payload = _recommendation(news=[
            {"title": "<img src=x onerror=alert(1)>Q1 beat", "label": "results", "score": 0.6},
        ])
        html = _render(payload)
        self.assertNotIn("<img src=x", html)
        self.assertIn("&lt;img", html)

    def test_quotes_in_a_headline_cannot_break_out_of_an_attribute(self) -> None:
        payload = _recommendation(news=[
            {"title": '" onmouseover="alert(1)', "label": "results", "score": 0.6},
        ])
        html = _render(payload)
        self.assertNotIn('onmouseover="alert(1)"', html)
        self.assertIn("&quot;", html)

    def test_escaper_handles_null_and_ampersands(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            script = _extract_js() + (
                "\nprocess.stdout.write(JSON.stringify("
                "[esc(null), esc('a & b'), esc('<b>'), esc(5)]));"
            )
            path = pathlib.Path(tmp) / "s.js"
            path.write_text(script, encoding="utf-8")
            out = subprocess.run(["node", str(path)], capture_output=True, text=True, timeout=30)
            self.assertEqual(json.loads(out.stdout), ["", "a &amp; b", "&lt;b&gt;", "5"])


if __name__ == "__main__":
    unittest.main()
