"""Opening an index shows the index, not "not in liquid universe".

An index is not an equity. It has no row in the liquid-universe panel, so
/api/stock/{symbol} answered "not in liquid universe" and the page rendered
blank — for the one instrument the engine actually trades options on.

Every piece needed to render it properly already existed: the parity level, the
15-minute candles, the CE/PE call with its five readings. Nothing routed to
them. These tests pin the routing so an index symbol can never fall back into
the equity path again.
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


def _run(expr, fns=("isIndexSym",)):
    src = V2_WEB.read_text(encoding="utf-8")
    js = "".join(_function_source(src, n) for n in fns)
    with tempfile.TemporaryDirectory() as tmp:
        path = pathlib.Path(tmp) / "t.js"
        path.write_text(js + f"\nconsole.log(JSON.stringify({expr}));\n", encoding="utf-8")
        out = subprocess.run([shutil.which("node") or "node", str(path)],
                             capture_output=True, text=True, timeout=30)
    if out.returncode != 0:
        raise AssertionError(out.stderr)
    return json.loads(out.stdout.strip())


@unittest.skipIf(shutil.which("node") is None, "node not available")
class IndexRoutingTest(unittest.TestCase):
    def test_the_tradeable_indices_are_recognised(self) -> None:
        for sym in ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"):
            with self.subTest(sym=sym):
                self.assertTrue(_run(f"isIndexSym({json.dumps(sym)})"))

    def test_it_is_case_insensitive(self) -> None:
        self.assertTrue(_run('isIndexSym("banknifty")'))

    def test_an_equity_is_not_an_index(self) -> None:
        """The routing must not swallow ordinary symbols — RELIANCE has to keep
        reaching the stock panel."""
        for sym in ("RELIANCE", "ITC", "TCS", "NIFTYBEES"):
            with self.subTest(sym=sym):
                self.assertFalse(_run(f"isIndexSym({json.dumps(sym)})"))

    def test_sensex_is_not_routed(self) -> None:
        """It is a BSE index and the F&O feed carries nothing for it. Sending
        it to the index view would swap one blank page for another."""
        self.assertFalse(_run('isIndexSym("SENSEX")'))

    def test_junk_does_not_throw(self) -> None:
        for bad in ("", None):
            self.assertFalse(_run(f"isIndexSym({json.dumps(bad)})"))


class RoutingWiringTest(unittest.TestCase):
    def test_render_stock_hands_indices_off_first(self) -> None:
        """Before any equity lookup — otherwise the index falls into the panel
        that has never heard of it and answers 'not in liquid universe'."""
        src = V2_WEB.read_text(encoding="utf-8")
        active = src[src.rindex('SPA_HTML = r"""'):]
        body = active[active.index("function renderStock("):]
        self.assertIn("if(isIndexSym(sym))return renderIndex(", body[:200])

    def test_the_index_view_uses_the_endpoints_that_already_existed(self) -> None:
        src = V2_WEB.read_text(encoding="utf-8")
        active = src[src.rindex('SPA_HTML = r"""'):]
        view = active[active.index("function renderIndex("):active.index("function renderStock(")]
        self.assertIn("/v2/api/index-candles", view)
        self.assertIn("/v2/api/index-call", view)
        self.assertIn("idxCandleSVG(", view)

    def test_the_ticker_carries_an_engine_key(self) -> None:
        """Deriving it from the display name by stripping non-letters is the
        kind of thing that breaks the day somebody renames a label."""
        src = V2_WEB.read_text(encoding="utf-8")
        self.assertIn('("^NSEI", "Nifty 50", "NIFTY")', src)
        self.assertIn('("^BSESN", "Sensex", "")', src)
        self.assertIn("key=key", src)



class SpaCacheHeaderTest(unittest.TestCase):
    """The page must not be served from a browser cache after a deploy.

    The whole app is ONE html document with the JS inline, and it went out with
    no cache headers at all — so browsers applied heuristic caching and kept
    running the previous build. Every UI fix looked like it had not shipped,
    because for that browser it genuinely had not, and the same bug got
    reported again after it was fixed.
    """

    def setUp(self) -> None:
        import os
        import tempfile
        os.environ["OPENSTOCKS_DISABLE_ENGINE"] = "1"
        os.environ["DATABASE_PATH"] = os.path.join(tempfile.mkdtemp(), "a.db")
        from fastapi.testclient import TestClient
        from app import main as m
        self.client = TestClient(m.app)

    def test_the_spa_is_not_cacheable(self) -> None:
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        cache = r.headers.get("cache-control", "").lower()
        self.assertIn("no-store", cache)
        self.assertIn("no-cache", cache)

    def test_it_carries_a_build_marker(self) -> None:
        """So "which version is this browser running" has an answer instead of
        being guessed at from behaviour."""
        r = self.client.get("/")
        build = r.headers.get("x-openstocks-build", "")
        self.assertTrue(build)
        self.assertEqual(len(build), 12)

    def test_the_marker_tracks_the_content(self) -> None:
        from app import v2_web
        import hashlib
        expected = hashlib.sha256(v2_web.SPA_HTML.encode("utf-8")).hexdigest()[:12]
        self.assertEqual(self.client.get("/").headers.get("x-openstocks-build"), expected)

if __name__ == "__main__":
    unittest.main()
