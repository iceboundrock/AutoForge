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


def test_git_ignores_the_operator_config_but_not_the_example():
    """`cp autoforge.example.yaml autoforge.yaml` must not leave a file to commit."""
    for candidate, ignored in (
        ("autoforge.yaml", True),
        ("autoforge.local.yaml", True),
        ("autoforge.example.yaml", False),
    ):
        res = subprocess.run(
            ["git", "check-ignore", "-q", candidate],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        assert (res.returncode == 0) is ignored, f"{candidate}: unexpected ignore status"
