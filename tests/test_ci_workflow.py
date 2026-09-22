"""Drift guards for the hosted CI workflow (issue #31).

The workflow itself is only really verified by running on GitHub. What these
tests protect is the part that can rot silently: the matrix must keep
covering the Python floor the project declares, the hosted commands must stay
the same ones `make check` runs locally (and vice versa), the local
`check-matrix` must cover the same interpreters as the hosted matrix, and the
one check name marked required on `main` must keep aggregating every job. If
they diverge, a green CI run stops meaning "the local checks pass", and the
controller's MERGE gate — which requires every check on the PR to have
succeeded — would be verifying something weaker than it looks.

These are text guards, not a YAML parser (the project has no YAML dependency
outside the optional `yaml` extra). They read the workflow with whole-line
comments removed and address individual blocks, so prose in a comment cannot
satisfy them; ``test_comments_cannot_satisfy_the_guards`` pins that. The one
exception is the Makefile's matrix target, whose expansion is read from
``make --dry-run`` (which executes no recipe) rather than re-implemented here.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from autoforge.config import default_config  # noqa: E402

# Local recipes whose command lines must also run in CI. Together with
# `lock-check` (the local form of CI's `uv sync --locked`) they are exactly
# what `make check` runs.
MIRRORED_MAKE_TARGETS = ("test", "lint", "fmt-check", "typecheck")
LOCKFILE_MAKE_TARGET = "lock-check"
# The Makefile variable that mirrors the workflow's `python-version:` matrix.
MATRIX_MAKE_VARIABLE = "CI_PYTHON_VERSIONS"


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


@dataclass
class _MakeRule:
    prerequisites: list[str] = field(default_factory=list)
    recipe: list[str] = field(default_factory=list)


_MAKE_ASSIGNMENT = re.compile(r"^([A-Za-z_]\w*)\s*[:+?]?=\s*(.*)$")


def _make_text() -> str:
    return (REPO_ROOT / "Makefile").read_text(encoding="utf-8")


def _make_rules() -> dict[str, _MakeRule]:
    """Explicit rules only: a pattern rule like ``test-py%`` is a target too, unexpanded."""
    rules: dict[str, _MakeRule] = {}
    target: str | None = None
    for line in _make_text().splitlines():
        if line.startswith("\t"):
            if target is not None:
                rules[target].recipe.append(line.strip())
        elif _MAKE_ASSIGNMENT.match(line):
            target = None
        elif ":" in line and not line.startswith((".", "#", " ")):
            name, prerequisites = line.split(":", 1)
            target = name.strip()
            rules.setdefault(target, _MakeRule()).prerequisites.extend(prerequisites.split())
        elif not line.strip():
            target = None
    return rules


def _make_recipes() -> dict[str, list[str]]:
    return {target: rule.recipe for target, rule in _make_rules().items()}


def _make_variable(name: str) -> list[str]:
    for line in _make_text().splitlines():
        match = _MAKE_ASSIGNMENT.match(line)
        if match and match.group(1) == name:
            return match.group(2).split()
    raise AssertionError(f"Makefile defines no {name} variable")


def _make_dry_run(target: str) -> list[str]:
    """The commands ``make <target>`` would run, with variables and pattern rules expanded."""
    make = shutil.which("make")
    if make is None:
        pytest.skip("GNU make is not installed; the Makefile cannot be expanded here")
    result = subprocess.run(
        [make, "--dry-run", "--silent", "-C", str(REPO_ROOT), target],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"make -n {target} failed:\n{result.stderr}"
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


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


def test_make_check_runs_exactly_what_ci_runs():
    """The other direction (issue #75): `make check` neither drops nor adds a CI step.

    Every `run:` in the workflow is either the locked install, mirrored locally
    by `lock-check`, or a command of a mirrored target; and `check` depends on
    exactly those targets, so a new local step that CI does not run (or a CI
    step no local target runs) fails here.
    """
    rules = _make_rules()
    assert "check" in rules, "Makefile has no 'check' target"
    assert sorted(rules["check"].prerequisites) == sorted(
        (*MIRRORED_MAKE_TARGETS, LOCKFILE_MAKE_TARGET)
    ), "`make check` must run the lockfile check plus the mirrored targets and nothing else"
    mirrored = {command for target in MIRRORED_MAKE_TARGETS for command in rules[target].recipe}
    for command in re.findall(r"^\s*(?:-\s*)?run:\s*(.+?)\s*$", _code(), flags=re.MULTILINE):
        if command in ("|", ">"):
            continue  # a block scalar: the aggregate job's `exit 1`, not a check command
        assert command.startswith("uv sync --locked") or command in mirrored, (
            f"CI runs {command!r} but no target of `make check` does"
        )


def test_ci_installs_from_the_lockfile():
    """A stale uv.lock must fail CI rather than be silently re-resolved."""
    assert "uv sync --locked" in _code()


def test_make_check_verifies_the_lockfile_like_ci():
    """`uv lock --check` is the local form of `uv sync --locked` (issue #75).

    Without it a stale lockfile passes `make check` and fails only once pushed.
    """
    rules = _make_rules()
    assert LOCKFILE_MAKE_TARGET in rules, f"Makefile has no '{LOCKFILE_MAKE_TARGET}' target"
    assert rules[LOCKFILE_MAKE_TARGET].recipe == ["uv lock --check"]
    assert LOCKFILE_MAKE_TARGET in rules["check"].prerequisites, (
        "`make check` does not verify uv.lock, so a stale lock passes locally and fails in CI"
    )


def test_make_check_matrix_runs_pytest_on_every_ci_python():
    """The CI matrix is maintained once (issue #75).

    The Makefile's CI_PYTHON_VERSIONS must equal the workflow's matrix, and
    `make check-matrix` must actually run `pytest` on each of them. The
    expansion is read from `make --dry-run`, so this pins what would run, not
    how the Makefile spells it. Isolated, so the matrix never replaces the
    project's own .venv.
    """
    versions = _matrix_python_versions(_code())
    assert _make_variable(MATRIX_MAKE_VARIABLE) == versions, (
        f"Makefile {MATRIX_MAKE_VARIABLE} and the CI python-version matrix differ"
    )
    commands = _make_dry_run("check-matrix")
    assert len(commands) == len(versions), f"check-matrix expands to {commands}"
    for version, command in zip(versions, commands, strict=True):
        assert re.fullmatch(rf"uv run (--\S+ )*--python {re.escape(version)} pytest", command), (
            f"check-matrix does not run pytest on Python {version}: {command!r}"
        )
        assert "--isolated" in command.split(), f"{command!r} would replace the project's .venv"
        assert "--locked" in command.split(), f"{command!r} does not install from the lockfile"


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
