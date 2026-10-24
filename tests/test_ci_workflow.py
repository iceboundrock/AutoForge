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

Two hosted jobs run outside `make check`. ``sandbox`` (#169) is the outer
sandbox's real backend on the runner, which measures what the runner provides
and requires the real-backend tests wherever it can enforce. It runs exactly
one Makefile target, ``test-sandbox-measured``, and
``test_the_sandbox_job_measures_the_runner_and_requires_the_backend_where_it_can``
pins both sides. ``dist`` (#256) builds the sdist and the wheel, installs the
wheel outside the checkout and runs it, on every interpreter of the matrix; it
runs exactly ``make dist-check`` with that interpreter, and
``test_the_dist_job_checks_the_installed_artifacts_on_every_ci_python`` pins
both sides.

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
# The hosted job that runs the outer sandbox's real backend (#169), what it
# runs, and the local target that requires the backend outright.
SANDBOX_JOB = "sandbox"
SANDBOX_JOB_COMMANDS = [
    "sudo apt-get update --quiet && sudo apt-get install --yes --quiet "
    "--no-install-recommends bubblewrap",
    "uv sync --locked --group dev",
    "make test-sandbox-measured",
]
SANDBOX_MEASURED_TARGET = "test-sandbox-measured"
SANDBOX_REQUIRED_TARGET = "test-sandbox"
SANDBOX_MEASUREMENT = "uv run python -m tests.sandbox_host"
# The hosted job that builds, installs and runs the distributions (#256), the
# one command it runs, and what that command expands to for an interpreter.
DIST_JOB = "dist"
DIST_TARGET = "dist-check"
DIST_JOB_COMMANDS = ["make dist-check DIST_PYTHON=${{ matrix.python-version }}"]
DIST_EXPANSION = "uv run --no-project --python {version} python -I tests/dist_check.py"
_RUN_LINE = re.compile(r"^\s*(?:-\s*)?run:\s*(.+?)\s*$", flags=re.MULTILINE)


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


def _job(text: str, name: str) -> str:
    return _block(_block(text, "jobs"), name, indent=2)


def _sandbox_job(text: str) -> str:
    return _job(text, SANDBOX_JOB)


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


def _make_dry_run(
    target: str, directory: Path = REPO_ROOT, assignments: tuple[str, ...] = ()
) -> list[str]:
    """The commands ``make <target>`` would run, with variables and pattern rules expanded.

    ``directory`` is where make runs (and reads its Makefile from); ``--silent``
    also drops the "Nothing to be done" notice, so a target that expands to
    nothing yields an empty list rather than prose. ``assignments`` are
    ``NAME=value`` overrides on make's command line, as a CI step passes them.
    """
    make = shutil.which("make")
    if make is None:
        pytest.skip("GNU make is not installed; the Makefile cannot be expanded here")
    result = subprocess.run(
        [make, "--dry-run", "--silent", "-C", str(directory), target, *assignments],
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
    # The sandbox and dist jobs are the hosted jobs outside `make check`; their
    # commands are pinned by test_the_sandbox_job_measures_the_runner_and_...
    # and test_the_dist_job_checks_the_installed_artifacts_on_every_ci_python.
    code = _code()
    for job in (SANDBOX_JOB, DIST_JOB):
        code = code.replace(_job(code, job), "")
    for command in _RUN_LINE.findall(code):
        if command in ("|", ">"):
            continue  # a block scalar: the aggregate job's `exit 1`, not a check command
        assert command.startswith("uv sync --locked") or command in mirrored, (
            f"CI runs {command!r} but no target of `make check` does"
        )


# A fixture that hands its module the real backend: `return real_sandbox_backend(...)`.
_REAL_BACKEND_FIXTURE = re.compile(r"^\s+return real_sandbox_backend\(", flags=re.MULTILINE)


def _real_backend_modules() -> list[str]:
    """Every test module that launches the real backend: those whose fixture asks for it."""
    return sorted(
        path.relative_to(REPO_ROOT).as_posix()
        for path in (REPO_ROOT / "tests").glob("test_*.py")
        if _REAL_BACKEND_FIXTURE.search(path.read_text(encoding="utf-8"))
    )


def test_the_sandbox_job_measures_the_runner_and_requires_the_backend_where_it_can():
    """Hosted CI runs the real backend wherever the runner can enforce (#169, PR #231 R1-F1).

    Whether GitHub's runner can enforce is measured on every run, never
    assumed. The `sandbox` job installs bubblewrap and runs exactly
    `make test-sandbox-measured`, which records the measurement first and then
    runs every real-backend module with AUTOFORGE_REQUIRE_SANDBOX_BACKEND=auto:
    a runner measured capable fails a test that would skip instead of passing
    without coverage. The modules are the ones `make test-sandbox` requires
    outright, and every module that launches the real backend is one of them.
    """
    code = _code()
    assert SANDBOX_JOB in _job_names(code), "the CI workflow has no job for the real backend"
    job = _sandbox_job(code)
    assert "runs-on: ubuntu-latest" in job
    assert _RUN_LINE.findall(job) == SANDBOX_JOB_COMMANDS

    measured = _make_dry_run(SANDBOX_MEASURED_TARGET)
    (required,) = _make_dry_run(SANDBOX_REQUIRED_TARGET)
    assert len(measured) == 2 and measured[0] == SANDBOX_MEASUREMENT, (
        f"`make {SANDBOX_MEASURED_TARGET}` must record the measurement before the tests: {measured}"
    )
    auto, required_words = measured[1].split(), required.split()
    assert auto[0] == "AUTOFORGE_REQUIRE_SANDBOX_BACKEND=auto", measured[1]
    assert required_words[0] == "AUTOFORGE_REQUIRE_SANDBOX_BACKEND=1", required
    assert auto[1:] == required_words[1:], (
        f"`make {SANDBOX_MEASURED_TARGET}` and `make {SANDBOX_REQUIRED_TARGET}` run different tests"
    )
    assert auto[1:5] == ["uv", "run", "pytest", "-rs"], measured[1]
    assert sorted(auto[5:]) == _real_backend_modules(), (
        "a module that launches the real backend is not run by the sandbox targets"
    )
    assert SANDBOX_MEASURED_TARGET not in _make_rules()["check"].prerequisites


def test_the_dist_job_checks_the_installed_artifacts_on_every_ci_python():
    """The built artifacts are installed and run on every claimed interpreter (#256).

    The `dist` job runs exactly `make dist-check` with the matrix entry's
    interpreter, over the same interpreters as the `test` matrix and the
    Makefile's CI_PYTHON_VERSIONS (the ones docs/compatibility.md claims), and
    the target runs tests/dist_check.py on that bare interpreter in isolated
    mode: no `uv sync`, so the check imports only what the wheel installed.
    It is not part of `make check`, and its checkout keeps no token.
    """
    code = _code()
    assert DIST_JOB in _job_names(code), "the CI workflow has no job for the distributions"
    job = _job(code, DIST_JOB)
    assert "runs-on: ubuntu-latest" in job
    assert _RUN_LINE.findall(job) == DIST_JOB_COMMANDS
    versions = _make_variable(MATRIX_MAKE_VARIABLE)
    assert _matrix_python_versions(job) == versions == _matrix_python_versions(code), (
        "the dist job must cover exactly the interpreters the test matrix covers"
    )
    assert "persist-credentials: false" in job, "the dist job's checkout must keep no token"
    assert "uv sync" not in job, "the dist job must not install the project from the tree"
    for version in versions:
        assert _make_dry_run(DIST_TARGET, assignments=(f"DIST_PYTHON={version}",)) == [
            DIST_EXPANSION.format(version=version)
        ]
    (default,) = _make_dry_run(DIST_TARGET)
    assert default.split()[4] in versions, (
        f"`make {DIST_TARGET}` without DIST_PYTHON must use a CI interpreter: {default!r}"
    )
    assert DIST_TARGET not in _make_rules()["check"].prerequisites


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


def test_make_check_matrix_runs_every_interpreter_despite_a_colliding_file(tmp_path: Path):
    """The matrix targets are phony (PR #122 review R1-F1).

    A file named like a matrix target (``test-py3.11``) must not let make
    consider that interpreter's run up to date and skip it, or `check-matrix`
    reports success having tested fewer interpreters than CI does. A phony
    target is also never matched against a pattern rule, so the naive
    ``.PHONY: test-py3.11`` with a ``test-py%`` recipe expands to nothing at
    all; the length check below catches that too. Run in a copy of the
    Makefile so the colliding files never touch the checkout.
    """
    versions = _make_variable(MATRIX_MAKE_VARIABLE)
    (tmp_path / "Makefile").write_text(_make_text(), encoding="utf-8")
    for version in versions:
        (tmp_path / f"test-py{version}").touch()
    commands = _make_dry_run("check-matrix", directory=tmp_path)
    assert len(commands) == len(versions), (
        f"with a file per matrix target present, check-matrix expands to {commands}"
    )
    for version, command in zip(versions, commands, strict=True):
        assert f"--python {version} pytest" in command, (
            f"a file named test-py{version} made check-matrix skip that interpreter: {command!r}"
        )


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
