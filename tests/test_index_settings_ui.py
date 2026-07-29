"""The index-options settings control, executed as real JavaScript.

The operator asked for a way to choose what gets auto-traded and there was no
control at all — the config existed only in Python. These run the shipped JS
against the shipped payload, which is what catches a field-name mismatch
between the two (the Stats tab was blank in production for exactly that).

The assertion that matters most is that auto-trade renders DISABLED while the
feed cannot price an option. A switch that silently does nothing is worse than
one you cannot reach: the operator would believe positions were being managed.
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


def _run(fn: str, arg) -> str:
    source = V2_WEB.read_text(encoding="utf-8")
    js = _function_source(source, "esc") + _function_source(source, fn)
    script = js + f"\nconsole.log({fn}(" + json.dumps(arg) + "));\n"
    with tempfile.TemporaryDirectory() as tmp:
        path = pathlib.Path(tmp) / "t.js"
        path.write_text(script, encoding="utf-8")
        out = subprocess.run([shutil.which("node") or "node", str(path)],
                             capture_output=True, text=True, timeout=30)
    if out.returncode != 0:
        raise AssertionError(out.stderr)
    return out.stdout


READY = dict(enabled=True, auto_trade=False, instruments=["NIFTY"], expiry="weekly",
             min_confidence=0.6, available=["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"],
             live_quotes=True)
BLOCKED = dict(READY, live_quotes=False)


@unittest.skipIf(shutil.which("node") is None, "node not available")
class SettingsRenderTest(unittest.TestCase):
    def test_a_failed_load_says_so_instead_of_rendering_empty(self) -> None:
        """Blank checkboxes read as 'selection is broken'. The failure must be
        visible as a failure."""
        html = _run("idxHtml", dict(READY, available=[]))
        self.assertIn("could not load", html)

    def test_every_available_index_is_offered(self) -> None:
        html = _run("idxHtml", READY)
        for name in READY["available"]:
            self.assertIn(name, html)

    def test_the_selected_index_is_checked(self) -> None:
        html = _run("idxHtml", READY)
        self.assertIn('value="NIFTY" checked', html)

    def test_an_unselected_index_is_not_checked(self) -> None:
        html = _run("idxHtml", READY)
        self.assertIn('value="BANKNIFTY" ', html)
        self.assertNotIn('value="BANKNIFTY" checked', html)

    def test_auto_trade_is_disabled_without_live_quotes(self) -> None:
        """The important one. Without live option prices a position cannot be
        exited, so the switch must be unreachable AND say why."""
        html = _run("idxHtml", BLOCKED)
        self.assertIn("disabled", html)
        self.assertIn("no live option prices", html)

    def test_auto_trade_is_reachable_once_quotes_exist(self) -> None:
        html = _run("idxHtml", READY)
        self.assertNotIn("disabled", html)

    def test_expiry_choice_is_reflected(self) -> None:
        self.assertIn('id=idxWk class="on"', _run("idxHtml", READY))
        self.assertIn('id=idxMo class="on"', _run("idxHtml", dict(READY, expiry="monthly")))

    def test_the_long_only_limit_is_stated_to_the_operator(self) -> None:
        """Max loss = premium is the property that makes this safe to enable;
        it belongs on screen, not only in a commit message."""
        html = _run("idxHtml", READY)
        self.assertIn("premium", html.lower())


class ApiPathTest(unittest.TestCase):
    """The SPA serves TWO roots: /api/... for auth and account (main.py) and
    /v2/api/... for engine endpoints. Calling the wrong one returns 404, and
    api() turns that into an empty object — so the control rendered with no
    checkboxes and looked like selection was broken rather than like a failure."""

    def _active(self):
        source = V2_WEB.read_text(encoding="utf-8")
        return source[source.rindex('SPA_HTML = r\'\'\'' if False else 'SPA_HTML = r"""'):]

    def test_index_endpoints_are_called_on_the_v2_root(self) -> None:
        active = self._active()
        self.assertIn("api('/v2/api/index-settings'", active)
        self.assertIn("api('/v2/api/index-call'", active)

    def test_no_index_call_uses_the_bare_root(self) -> None:
        self.assertNotIn("api('/api/index-", self._active())


@unittest.skipIf(shutil.which("node") is None, "node not available")
class CallRenderTest(unittest.TestCase):
    def test_a_call_shows_its_side_and_reasons(self) -> None:
        html = _run("idxCallHtml", [dict(symbol="NIFTY", call="CE",
                                         reasons=["trend: uptrend", "volume: 2.1x"])])
        self.assertIn("CE", html)
        self.assertIn("trend: uptrend", html)

    def test_no_trade_is_shown_explicitly(self) -> None:
        """Blank would read as broken; 'no trade' is the engine working."""
        html = _run("idxCallHtml", [dict(symbol="NIFTY", call=None, reasons=["trend: mixed"])])
        self.assertIn("no trade", html)

    def test_reasons_are_escaped(self) -> None:
        html = _run("idxCallHtml", [dict(symbol="X", call=None,
                                         reasons=["<img src=x onerror=alert(1)>"])])
        self.assertNotIn("<img", html)

    def test_empty_selection_says_so(self) -> None:
        self.assertIn("no indices selected", _run("idxCallHtml", []))


if __name__ == "__main__":
    unittest.main()
