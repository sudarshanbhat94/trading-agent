"""The allocation & risk card, executed as real JavaScript.

Same reasoning as the recommendation card: the payload is produced in Python
and consumed in a template embedded in a Python string, and nothing else checks
that the field names line up. The Stats tab was blank in production for exactly
that reason.
"""

from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
import tempfile
import unittest

from app import portfolio

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
V2_WEB = REPO_ROOT / "app" / "v2_web.py"


def _function_source(source: str, name: str) -> str:
    """Slice one top-level JS function out of the Python file that embeds it.

    Functions in this bundle are written one per line, so the next newline that
    is followed by `function ` ends the definition.
    """
    start = source.index(f"function {name}(")
    end = source.index("\nfunction ", start)
    return source[start:end] + "\n"


def _extract_js() -> str:
    source = V2_WEB.read_text(encoding="utf-8")
    # esc() is the real escaper, taken verbatim so the XSS assertions below
    # test production behaviour rather than a stand-in.
    return _function_source(source, "esc") + _function_source(source, "pfTile") + \
        source[source.index("function pfHtml("):source.index("function recBadge(score){")]


def _render(payload: dict) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        data = pathlib.Path(tmp) / "p.json"
        data.write_text(json.dumps(payload), encoding="utf-8")
        script = _extract_js() + (
            "\nconst d=JSON.parse(require('fs').readFileSync(%r,'utf8'));"
            "\nprocess.stdout.write(pfHtml(d));" % str(data)
        )
        path = pathlib.Path(tmp) / "r.js"
        path.write_text(script, encoding="utf-8")
        out = subprocess.run(["node", str(path)], capture_output=True, text=True, timeout=30)
        if out.returncode != 0:
            raise AssertionError(f"pfHtml threw: {out.stderr[:500]}")
        return out.stdout


def _payload():
    result = portfolio.build(
        positions=[("KFINTECH", "btst", 10, 950.4, 950.4),
                   ("RKFORGE", "btst", 16, 624.0, 624.0),
                   ("RRKABEL", "btst", 3, 2571.4, 2571.4)],
        equity_curve=[("2026-07-25", 100_000.0), ("2026-07-26", 105_000.0),
                      ("2026-07-27", 99_000.0)],
        trades=[("volume_surge", "2026-07-27", 964.85)],
        budget=100_000.0,
    )
    result["ccy"] = "₹"
    return result


@unittest.skipUnless(shutil.which("node"), "node is required to execute the template")
class PortfolioCardTest(unittest.TestCase):
    def test_renders_the_risk_tiles(self) -> None:
        html = _render(_payload())
        for label in ("deployed", "largest position", "top 3", "positions",
                      "max drawdown", "below high-water"):
            self.assertIn(label, html)

    def test_renders_every_position(self) -> None:
        html = _render(_payload())
        for symbol in ("KFINTECH", "RKFORGE", "RRKABEL"):
            self.assertIn(symbol, html)

    def test_shows_the_computed_drawdown(self) -> None:
        """105000 -> 99000 is -5.71%, and it must reach the markup."""
        payload = _payload()
        self.assertAlmostEqual(payload["drawdown"]["max_drawdown_pct"], -5.71, places=2)
        self.assertIn("-5.71", _render(payload))

    def test_no_undefined_leaks(self) -> None:
        self.assertNotIn("undefined", _render(_payload()))

    def test_empty_book_says_so(self) -> None:
        payload = portfolio.build([], [], [], 100_000.0)
        payload["ccy"] = "₹"
        self.assertIn("no open positions", _render(payload))

    def test_error_payload_degrades(self) -> None:
        self.assertIn("unavailable", _render({"error": "boom"}))

    def test_concentrated_book_is_flagged_red(self) -> None:
        """A single position above 30% of equity should render in the danger
        colour, not silently look the same as a spread book."""
        payload = portfolio.build(
            positions=[("ONE", "swing_meanrev", 100, 400.0, 400.0)],
            equity_curve=[("d1", 100_000.0)], trades=[], budget=100_000.0,
        )
        payload["ccy"] = "₹"
        self.assertGreaterEqual(payload["concentration"]["largest_pct"], 30)
        self.assertIn("var(--dn)", _render(payload))

    def test_spread_book_is_not_flagged(self) -> None:
        html = _render(_payload())
        self.assertLess(_payload()["concentration"]["largest_pct"], 20)
        self.assertIn("var(--up)", html)

    def test_symbol_is_escaped(self) -> None:
        payload = portfolio.build(
            positions=[("<img src=x onerror=alert(1)>", "btst", 1, 100.0, 100.0)],
            equity_curve=[], trades=[], budget=100_000.0,
        )
        payload["ccy"] = "₹"
        html = _render(payload)
        self.assertNotIn("<img src=x", html)
        self.assertIn("&lt;img", html)


if __name__ == "__main__":
    unittest.main()
