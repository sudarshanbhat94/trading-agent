"""Guard against committing credentials or production host addresses.

This repository is public. A deploy host address or an API token that lands in a
commit is exposed the moment it is pushed, and rewriting published history is
disruptive enough that prevention is the only cheap fix. These tests scan the
files git is actually tracking, so they fail in CI before a leak ships.
"""

from __future__ import annotations

import ipaddress
import re
import subprocess
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Files that legitimately document the *shape* of a secret without holding one.
ALLOWED_PATHS = {
    ".env.example",
    "README.md",
    "HANDOFF.md",
    "tests/test_repo_secret_hygiene.py",
}

# Live credential formats. Deliberately narrow: each has a fixed prefix and
# length, so a placeholder like "your_api_key" cannot trip them.
SECRET_PATTERNS = {
    "OpenAI/DeepSeek-style API key": re.compile(r"\bsk-[A-Za-z0-9]{20,}"),
    "Telegram bot token": re.compile(r"\b[0-9]{9,10}:AA[A-Za-z0-9_-]{30,}"),
    "AWS access key id": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "GitHub personal access token": re.compile(r"\bghp_[A-Za-z0-9]{30,}"),
    "Private key block": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
}

# `user@1.2.3.4` — a shell-ready login to a real machine.
SSH_TARGET_RE = re.compile(r"\b[a-z_][a-z0-9_-]{0,30}@(\d{1,3}(?:\.\d{1,3}){3})\b")

IPV4_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")

TEXT_SUFFIXES = {
    ".py", ".md", ".txt", ".yml", ".yaml", ".json", ".toml", ".ini", ".cfg",
    ".sh", ".service", ".timer", ".html", ".js", ".css", ".env", ".example",
    ".csv", ".sql", "",
}


def _tracked_files() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return []
    return [line for line in result.stdout.splitlines() if line.strip()]


def _is_routable(address: str) -> bool:
    """True for addresses that identify a real internet-reachable host."""
    try:
        parsed = ipaddress.IPv4Address(address)
    except ValueError:
        return False
    return parsed.is_global


def _readable_tracked_files():
    for rel_path in _tracked_files():
        if rel_path in ALLOWED_PATHS:
            continue
        path = REPO_ROOT / rel_path
        if path.suffix.lower() not in TEXT_SUFFIXES or not path.is_file():
            continue
        try:
            yield rel_path, path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue


class RepoSecretHygieneTest(unittest.TestCase):
    def setUp(self) -> None:
        if not _tracked_files():
            self.skipTest("not a git checkout; nothing to scan")

    def test_no_live_credentials_in_tracked_files(self) -> None:
        findings: list[str] = []
        for rel_path, content in _readable_tracked_files():
            for label, pattern in SECRET_PATTERNS.items():
                match = pattern.search(content)
                if match:
                    line_no = content[: match.start()].count("\n") + 1
                    findings.append(f"{rel_path}:{line_no} looks like a {label}")
        self.assertEqual(
            findings,
            [],
            "Credential-shaped strings found in tracked files. Remove them and "
            "rotate the credential — this repo is public:\n  "
            + "\n  ".join(findings),
        )

    def test_no_production_ssh_targets_in_tracked_files(self) -> None:
        findings: list[str] = []
        for rel_path, content in _readable_tracked_files():
            for match in SSH_TARGET_RE.finditer(content):
                if _is_routable(match.group(1)):
                    line_no = content[: match.start()].count("\n") + 1
                    findings.append(f"{rel_path}:{line_no} -> {match.group(0)}")
        self.assertEqual(
            findings,
            [],
            "Public SSH login targets found in tracked files. Describe the host "
            "generically instead of naming it:\n  " + "\n  ".join(findings),
        )

    def test_env_file_is_ignored(self) -> None:
        """The real .env must never become a tracked file."""
        self.assertNotIn(".env", _tracked_files())
        gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn(".env", gitignore.split())

    def test_example_env_has_no_filled_in_values(self) -> None:
        """.env.example documents keys; every secret slot must be blank."""
        example = REPO_ROOT / ".env.example"
        if not example.is_file():
            self.skipTest(".env.example not present")
        secretish = ("API_KEY", "ACCESS_TOKEN", "API_SECRET", "PASSWORD",
                     "SECRET", "MPIN", "SESSION_TOKEN")
        filled: list[str] = []
        for line_no, line in enumerate(example.read_text(encoding="utf-8").splitlines(), 1):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            if any(token in key.upper() for token in secretish) and value.strip():
                filled.append(f"{line_no}: {key}")
        self.assertEqual(
            filled,
            [],
            ".env.example must ship with empty secret values:\n  " + "\n  ".join(filled),
        )


if __name__ == "__main__":
    unittest.main()
