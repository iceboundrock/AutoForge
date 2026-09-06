"""R1-F1: the whole runtime directory must be git-ignored, not just known files."""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_gitignore_excludes_runtime_dir_wholesale():
    rules = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ".autoforge/" in rules


def test_git_ignores_arbitrary_runtime_files():
    """Any new file under .autoforge/ (e.g. doctor crash leftovers) is ignored."""
    for candidate in (".autoforge/newfile.json", ".autoforge/.doctor-xyz", ".autoforge/x/y"):
        res = subprocess.run(
            ["git", "check-ignore", "-q", candidate],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        assert res.returncode == 0, f"{candidate} is not git-ignored"
