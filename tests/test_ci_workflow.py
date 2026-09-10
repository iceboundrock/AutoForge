"""Drift guards for the hosted CI workflow (issue #31).

The workflow itself is only really verified by running on GitHub. What these
tests protect is the part that can rot silently: the matrix must keep
covering the Python floor the project declares, and the hosted commands must
stay the same ones `make` runs locally. If they diverge, a green CI run stops
meaning "the local checks pass", and the controller's MERGE gate — which
requires every check on the PR to have succeeded — would be verifying
something weaker than it looks.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

# Local recipes whose command lines must also run in CI.
MIRRORED_MAKE_TARGETS = ("test", "lint", "fmt-check", "typecheck")


def _workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def _matrix_python_versions() -> list[str]:
    match = re.search(r"python-version:\s*\[([^\]]*)\]", _workflow_text())
    assert match, "the CI workflow declares no python-version matrix"
    return re.findall(r"\d+\.\d+", match.group(1))


def _requires_python_floor() -> str:
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    spec = str(data["project"]["requires-python"]).strip()
    match = re.fullmatch(r">=\s*(\d+\.\d+)", spec)
    assert match, f"unexpected requires-python spec {spec!r}; update this test"
    return match.group(1)


def _make_recipes() -> dict[str, list[str]]:
    recipes: dict[str, list[str]] = {}
    target: str | None = None
    for line in (REPO_ROOT / "Makefile").read_text(encoding="utf-8").splitlines():
        if line.startswith("\t"):
            if target is not None:
                recipes[target].append(line.strip())
        elif ":" in line and not line.startswith((".", "#", " ")):
            target = line.split(":", 1)[0].strip()
            recipes.setdefault(target, [])
        elif not line.strip():
            target = None
    return recipes


def test_ci_workflow_exists():
    """Without a hosted check the pre-merge 'all checks succeeded' is vacuous."""
    assert WORKFLOW.is_file(), "no .github/workflows/ci.yml: the MERGE gate has nothing to verify"


def test_ci_runs_on_pull_requests_and_main_pushes():
    text = _workflow_text()
    assert "pull_request:" in text, "CI must run on pull requests to gate a PR merge"
    assert "branches: [main]" in text, "CI must also run on pushes to the default branch"


def test_matrix_covers_the_declared_python_floor():
    floor = _requires_python_floor()
    versions = _matrix_python_versions()
    assert floor in versions, (
        f"requires-python is >={floor} but the CI matrix tests {versions}; "
        "the oldest supported interpreter must be tested"
    )


def test_ci_runs_the_same_commands_as_the_local_make_targets():
    text = _workflow_text()
    recipes = _make_recipes()
    for target in MIRRORED_MAKE_TARGETS:
        assert target in recipes, f"Makefile has no '{target}' target"
        assert recipes[target], f"Makefile target '{target}' has no commands"
        for command in recipes[target]:
            assert f"run: {command}" in text, (
                f"'make {target}' runs {command!r} locally but CI does not"
            )


def test_ci_installs_from_the_lockfile():
    """A stale uv.lock must fail CI rather than be silently re-resolved."""
    assert "uv sync --locked" in _workflow_text()
