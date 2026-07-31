"""The intraday index chart, executed as real JavaScript.

There was no index chart because there was no index price series — the feed
carries equities and option contracts, and every index level came from the
previous session's bhavcopy close.

These run the SHIPPED JS against the SHIPPED payload in node. That matters more
than it sounds: v2_web.py holds three SPA_HTML definitions and only the last is
served, and a helper that exists only in a dead one throws ReferenceError in
the browser and blanks the whole section. That is exactly how the Account tab
broke, and how `wrn` was caught during this same piece of work.
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
    active = source.rindex('SPA_HTML = r"""')
    start = source.index(f"function {name}(", active)
    end = source.index("\nfunction ", start)
    return source[start:end] + "\n"


def _run(fn: str, arg) -> str:
    source = V2_WEB.read_text(encoding="utf-8")
    js = "".join(_function_source(source, n)
                 for n in ("esc", "fmtn", "sgn", "pill", "col", "idxCandleSVG", "idxChartHtml"))
    script = js + f"\nconsole.log({fn}(" + json.dumps(arg) + "));\n"
    with tempfile.TemporaryDirectory() as tmp:
        path = pathlib.Path(tmp) / "t.js"
        path.write_text(script, encoding="utf-8")
        out = subprocess.run([shutil.which("node") or "node", str(path)],
                             capture_output=True, text=True, timeout=30)
    if out.returncode != 0:
        raise AssertionError(out.stderr)
    return out.stdout


def bars(*rows):
    return [dict(ts=t, open=o, high=h, low=lo, close=c) for t, o, h, lo, c in rows]


SESSION = bars(("2026-07-31T09:15", 24250.0, 24300.0, 24240.0, 24290.0),
               ("2026-07-31T09:20", 24290.0, 24340.0, 24285.0, 24330.0),
               ("2026-07-31T09:25", 24330.0, 24400.0, 24320.0, 24388.0))
PAYLOAD = dict(symbol="NIFTY", candles=SESSION, price=24388.5, prev_close=24250.2,
               chg=0.57, estimate=True, pairs=12, atm_distance_pct=0.36)


@unittest.skipIf(shutil.which("node") is None, "node not available")
class IndexChartRenderTest(unittest.TestCase):
    def test_a_candle_is_drawn_for_every_bar(self) -> None:
        svg = _run("idxCandleSVG", SESSION)
        self.assertEqual(svg.count("<rect"), len(SESSION))   # one body per bar
        self.assertEqual(svg.count("<line x1"), len(SESSION) + 3)  # wicks + 3 gridlines

    def test_an_up_bar_and_a_down_bar_are_coloured_differently(self) -> None:
        mixed = bars(("2026-07-31T09:15", 100.0, 110.0, 95.0, 105.0),    # up
                     ("2026-07-31T09:20", 105.0, 106.0, 90.0, 92.0))     # down
        svg = _run("idxCandleSVG", mixed)
        self.assertIn("#3fa45b", svg)
        self.assertIn("#e34d3f", svg)

    def test_the_time_axis_shows_clock_times_not_dates(self) -> None:
        """These are five-minute bars — a date label on every one says nothing."""
        svg = _run("idxCandleSVG", SESSION)
        self.assertIn(">09:15<", svg)
        self.assertIn(">09:25<", svg)
        self.assertNotIn("2026-07-31T", svg)

    def test_an_empty_series_says_so_instead_of_drawing_nothing(self) -> None:
        """A blank box reads as a broken chart. It has to name the reason."""
        for empty in ([], None, bars(("2026-07-31T09:15", 1.0, 1.0, 1.0, 1.0))):
            out = _run("idxCandleSVG", empty)
            self.assertIn("no candles yet", out)
            self.assertNotIn("<svg", out)

    def test_a_flat_series_does_not_divide_by_zero(self) -> None:
        flat = bars(("2026-07-31T09:15", 100.0, 100.0, 100.0, 100.0),
                    ("2026-07-31T09:20", 100.0, 100.0, 100.0, 100.0))
        svg = _run("idxCandleSVG", flat)
        self.assertIn("<svg", svg)
        self.assertNotIn("NaN", svg)

    def test_the_header_shows_the_level_and_the_change(self) -> None:
        html = _run("idxChartHtml", PAYLOAD)
        self.assertIn("NIFTY", html)
        self.assertIn("24,389", html.replace("&nbsp;", " "))   # fmtn rounds
        self.assertIn("0.57", html)

    def test_the_estimate_is_disclosed_not_dressed_up_as_a_quote(self) -> None:
        """No live index quote reaches this system; the level is derived from
        the option chain. Presenting it as a tick would be a lie."""
        html = _run("idxChartHtml", PAYLOAD)
        self.assertIn("put-call parity", html)
        self.assertIn("12 strike pairs", html)

    def test_a_missing_price_renders_a_dash_not_nan(self) -> None:
        html = _run("idxChartHtml", dict(symbol="NIFTY", candles=[], price=None, chg=None))
        self.assertNotIn("NaN", html)
        self.assertNotIn("undefined", html)
        self.assertIn("—", html)

    def test_an_empty_payload_does_not_throw(self) -> None:
        html = _run("idxChartHtml", {})
        self.assertNotIn("undefined", html)


if __name__ == "__main__":
    unittest.main()
