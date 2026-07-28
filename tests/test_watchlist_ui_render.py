"""Watchlist grouping, executed as real JavaScript.

The property that matters most is the *absence* of change: a watchlist where
nothing has been filed must render exactly as it did before, with no folder
headers. Grouping that imposes an "unfiled" heading on every existing user is
a regression dressed as a feature.

Run against the real payload shape from /v2/api/watchlist.
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

PRELUDE = (
    "function esc(x){if(x==null)return '';return String(x)"
    ".replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')"
    ".replace(/\"/g,'&quot;').replace(/'/g,'&#39;');}\n"
    "var INR={format:function(x){return String(x)}},USD=INR;\n"
    "function stock(){};function delWL(){};function fileWL(){};\n"
)


def _extract_js() -> str:
    source = V2_WEB.read_text(encoding="utf-8")
    start = source.index("function wlRow(w){")
    end = source.index("function fileWL(")
    return PRELUDE + source[start:end]


def _render(items) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        data = pathlib.Path(tmp) / "w.json"
        data.write_text(json.dumps(items), encoding="utf-8")
        script = _extract_js() + (
            "\nconst items=JSON.parse(require('fs').readFileSync(%r,'utf8'));"
            "\nprocess.stdout.write(wlGrouped(items));" % str(data)
        )
        path = pathlib.Path(tmp) / "r.js"
        path.write_text(script, encoding="utf-8")
        out = subprocess.run(["node", str(path)], capture_output=True, text=True, timeout=30)
        if out.returncode != 0:
            raise AssertionError(f"wlGrouped threw: {out.stderr[:500]}")
        return out.stdout


def _row(symbol, folder="", tags=None, chg=1.2, price=100.0):
    return {"symbol": symbol, "market": "IN", "ccy": "₹", "price": price,
            "chg": chg, "folder": folder, "tags": tags or []}


@unittest.skipUnless(shutil.which("node"), "node is required to execute the template")
class UngroupedTest(unittest.TestCase):
    """An unfiled watchlist must look untouched."""

    def test_no_headers_when_nothing_is_filed(self) -> None:
        html = _render([_row("CGPOWER"), _row("GRSE")])
        self.assertIn("CGPOWER", html)
        self.assertIn("GRSE", html)
        self.assertNotIn("unfiled", html)
        self.assertNotIn("text-transform:uppercase", html)

    def test_empty_watchlist_renders_nothing(self) -> None:
        self.assertEqual(_render([]).strip(), "")


@unittest.skipUnless(shutil.which("node"), "node is required to execute the template")
class GroupedTest(unittest.TestCase):
    def test_folder_headers_appear_once_something_is_filed(self) -> None:
        html = _render([_row("TCS", folder="core"), _row("GRSE")])
        self.assertIn("core", html)
        self.assertIn("unfiled", html)

    def test_unfiled_group_sorts_last(self) -> None:
        html = _render([_row("GRSE"), _row("TCS", folder="core")])
        self.assertLess(html.index(">core "), html.index(">unfiled "))

    def test_folders_are_alphabetical(self) -> None:
        html = _render([_row("Z", folder="zeta"), _row("A", folder="alpha")])
        self.assertLess(html.index("alpha"), html.index("zeta"))

    def test_header_shows_a_count(self) -> None:
        html = _render([_row("A", folder="core"), _row("B", folder="core")])
        self.assertIn(">2</span>", html)

    def test_every_symbol_survives_grouping(self) -> None:
        items = [_row("A", folder="core"), _row("B"), _row("C", folder="banks")]
        html = _render(items)
        for item in items:
            self.assertIn(item["symbol"], html)


@unittest.skipUnless(shutil.which("node"), "node is required to execute the template")
class TagTest(unittest.TestCase):
    def test_tags_render_as_chips(self) -> None:
        html = _render([_row("TCS", tags=["it", "largecap"])])
        self.assertIn("it", html)
        self.assertIn("largecap", html)

    def test_no_tag_markup_without_tags(self) -> None:
        html = _render([_row("TCS")])
        self.assertNotIn("border-radius:6px", html)

    def test_tags_are_escaped(self) -> None:
        html = _render([_row("TCS", tags=["<img src=x onerror=alert(1)>"])])
        self.assertNotIn("<img src=x", html)
        self.assertIn("&lt;img", html)

    def test_folder_name_is_escaped_in_the_header(self) -> None:
        html = _render([_row("TCS", folder="<script>")])
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)


@unittest.skipUnless(shutil.which("node"), "node is required to execute the template")
class RowContentTest(unittest.TestCase):
    def test_price_and_change_are_shown(self) -> None:
        html = _render([_row("TCS", chg=2.5, price=3500.0)])
        self.assertIn("3500", html)
        self.assertIn("2.5", html)

    def test_missing_change_renders_a_dash(self) -> None:
        self.assertIn("—", _render([_row("TCS", chg=None)]))

    def test_both_file_and_remove_controls_exist(self) -> None:
        html = _render([_row("TCS")])
        self.assertIn("fileWL", html)
        self.assertIn("delWL", html)

    def test_no_undefined_leaks(self) -> None:
        self.assertNotIn("undefined", _render([_row("TCS", folder="core", tags=["it"])]))


if __name__ == "__main__":
    unittest.main()
