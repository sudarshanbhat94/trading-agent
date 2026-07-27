"""Guard the deploy contract: everything app/ imports must be declared.

requirements.txt is what a fresh box installs. numpy and pandas were missing
from it while app/v2_engine.py and app/v2_live.py imported them at module
level, and app/main.py caught the resulting ImportError and logged a warning —
so the app booted, served pages, and had no trading engine at all. These tests
make that class of drift fail loudly instead of silently.
"""

from __future__ import annotations

import ast
import re
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
APP_DIR = REPO_ROOT / "app"
REQUIREMENTS = REPO_ROOT / "requirements.txt"

# Import name -> distribution name on PyPI, where they differ.
IMPORT_TO_DISTRIBUTION = {
    "dotenv": "python-dotenv",
    "yaml": "pyyaml",
    "PIL": "pillow",
    "sklearn": "scikit-learn",
}

# Imported directly but guaranteed as a pinned transitive dependency. Declaring
# starlette separately invites a version fight with fastapi's own pin.
ALLOWED_TRANSITIVE = {"starlette"}


def _declared_distributions() -> set[str]:
    declared: set[str] = set()
    for raw in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # "uvicorn[standard]>=0.30,<1" -> "uvicorn"
        name = re.split(r"[\[<>=!~;]", line, maxsplit=1)[0].strip()
        if name:
            declared.add(name.lower().replace("_", "-"))
    return declared


def _first_party_modules() -> set[str]:
    return {"app"} | {p.stem for p in APP_DIR.rglob("*.py")}


def _imported_third_party() -> dict[str, set[str]]:
    """Map third-party import name -> set of app/ files importing it.

    Walks the whole AST, not just module level: app/v2_live.py imports numpy
    inside functions, and a function-level import is still a hard dependency.
    """
    stdlib = sys.stdlib_module_names
    first_party = _first_party_modules()
    found: dict[str, set[str]] = {}

    for path in sorted(APP_DIR.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"), str(path))
        except SyntaxError:  # pragma: no cover - would fail elsewhere first
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                # level > 0 is a relative (first-party) import
                modules = [(node.module or "").split(".")[0]] if node.level == 0 else []
            else:
                continue
            for module in modules:
                if module and module not in stdlib and module not in first_party:
                    found.setdefault(module, set()).add(
                        str(path.relative_to(REPO_ROOT))
                    )
    return found


class DependencyContractTest(unittest.TestCase):
    def test_requirements_file_exists(self) -> None:
        self.assertTrue(REQUIREMENTS.is_file(), "requirements.txt is missing")

    def test_every_app_import_is_declared(self) -> None:
        declared = _declared_distributions()
        missing: list[str] = []
        for module, files in sorted(_imported_third_party().items()):
            if module in ALLOWED_TRANSITIVE:
                continue
            distribution = IMPORT_TO_DISTRIBUTION.get(module, module)
            if distribution.lower().replace("_", "-") not in declared:
                sample = ", ".join(sorted(files)[:3])
                missing.append(f"{module} (imported by {sample})")
        self.assertEqual(
            missing,
            [],
            "app/ imports packages that requirements.txt does not declare. A "
            "fresh deploy would fail to import them:\n  " + "\n  ".join(missing),
        )

    def test_trading_engine_modules_are_importable(self) -> None:
        """The v2 engine must import, not be silently skipped.

        If this errors with ModuleNotFoundError the deploy contract is broken —
        that is the failure being guarded against, so do not convert it to a
        skip.
        """
        import importlib

        for module in ("app.v2_engine", "app.v2_live", "app.v2_web", "app.meta_filter"):
            with self.subTest(module=module):
                try:
                    importlib.import_module(module)
                except ModuleNotFoundError as exc:
                    self.fail(
                        f"{module} is not importable ({exc}). Install "
                        f"requirements.txt: the trading engine is missing."
                    )


if __name__ == "__main__":
    unittest.main()
