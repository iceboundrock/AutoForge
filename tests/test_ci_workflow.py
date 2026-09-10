"""Drift guards for the hosted CI workflow (issue #31).

The workflow itself is only really verified by running on GitHub. What these
tests protect is the part that can rot silently: the matrix must keep
covering the Python floor the project declares, the hosted commands must stay
the same ones `make` runs locally, and the one check name marked required on
`main` must keep aggregating every job. If they diverge, a green CI run stops
meaning "the local checks pass", and the controller's MERGE gate — which
requires every check on the PR to have succeeded — would be verifying
something weaker than it looks.

These are text guards, not a YAML parser (the project has no YAML dependency
outside the optional `yaml` extra). They read the workflow with whole-line
comments removed and address individual blocks, so prose in a comment cannot
satisfy them; ``test_comments_cannot_satisfy_the_guards`` pins that.
"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from autoforge.config import default_config  # noqa: E402

# Local recipes whose command lines must also run in CI.
MIRRORED_MAKE_TARGETS = ("test", "lint", "fmt-check", "typecheck")


def _workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def _strip_comments(text: str) -> str:
    """Drop whole-line comments only: a '#' inside a `run:` line is a command."""
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def _code() -> str:
    return _strip_comments(_workflow_text())


def _block(text: str, key: str, indent: int = 0) -> str:
    """``key:`` at ``indent`` spaces plus everything nested under it."""
    prefix = " " * indent
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.startswith(f"{prefix}{key}:") and not line[:indent].strip():
            body = [line]
            for nxt in lines[i + 1 :]:
                if nxt.strip() and len(nxt) - len(nxt.lstrip(" ")) <= indent:
                    break
                body.append(nxt)
            return "\n".join(body)
    raise AssertionError(f"the CI workflow declares no {key!r} block at indent {indent}")


def _job_names(text: str) -> list[str]:
    return re.findall(r"^  (\w[\w-]*):$", _block(text, "jobs"), flags=re.MULTILINE)


def _matrix_python_versions(text: str) -> list[str]:
    match = re.search(r"python-version:\s*\[([^\]]*)\]", text)
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
    triggers = _block(_code(), "on")
    assert "pull_request:" in triggers, "CI must run on pull requests to gate a PR merge"
    assert "branches: [main]" in _block(triggers, "push", indent=2), (
        "CI must also run on pushes to the default branch"
    )


def test_matrix_covers_the_declared_python_floor():
    floor = _requires_python_floor()
    versions = _matrix_python_versions(_code())
    assert floor in versions, (
        f"requires-python is >={floor} but the CI matrix tests {versions}; "
        "the oldest supported interpreter must be tested"
    )


def test_ci_runs_the_same_commands_as_the_local_make_targets():
    text = _code()
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
    assert "uv sync --locked" in _code()


def test_the_required_check_aggregates_every_other_job():
    """`ci` is the single name marked required on `main`: it must cover them all.

    A job that is skipped or cancelled must fail it, because the controller
    maps a SKIPPED check run to passing — an aggregate that can skip its way
    to green would hand the merge gate a vacuous success.
    """
    code = _code()
    others = [job for job in _job_names(code) if job != "ci"]
    assert others, "the workflow has no jobs for `ci` to aggregate"
    ci = _block(_block(code, "jobs"), "ci", indent=2)
    assert "name: ci" in ci, "the required check must keep the stable name `ci`"
    needs = re.search(r"needs:\s*\[([^\]]*)\]", ci)
    assert needs, "the `ci` job declares no needs:"
    assert sorted(n.strip() for n in needs.group(1).split(",")) == sorted(others), (
        f"`ci` needs {needs.group(1)!r} but the workflow's other jobs are {others}"
    )
    assert "if: always()" in ci, "`ci` must run even when a needed job failed"
    guard = next(
        (line for line in ci.splitlines() if "needs.*.result" in line),
        "",
    )
    for result in ("failure", "cancelled", "skipped"):
        assert result in guard, f"`ci` does not treat a {result} job as a failure: {guard!r}"
    assert "exit 1" in ci, "`ci` never fails: nothing makes the aggregate job exit non-zero"


def test_workflow_is_granted_no_write_permissions():
    assert _block(_code(), "permissions").split(":", 1)[1].strip() == "contents: read"


def test_the_merge_gate_refuses_to_merge_a_pr_that_edits_this_workflow():
    """The controller-side half of the trust boundary (PR #38 review R2-F1).

    GitHub runs the PR's own copy of this file, so the check it produces is
    only as trustworthy as the PR. `safety.protected_merge_paths` is what
    keeps an unattended merge from clearing itself.
    """
    relative = WORKFLOW.relative_to(REPO_ROOT).as_posix()
    assert default_config().safety.protects(relative)


def test_comments_cannot_satisfy_the_guards():
    """The guards read code, not prose: a comment claiming CI runs X does not count."""
    commented = "# on:\n#   pull_request:\n#     run: uv run pytest\njobs:\n  ci:\n"
    assert _strip_comments(commented) == "jobs:\n  ci:"
    assert "pull_request" not in _strip_comments(commented)
