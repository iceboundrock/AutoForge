"""Fast feedback targets: the test modules a change's paths map to (ADR 0010, #342).

No test: the tool behind ``make test-fast``, for an agent or a human who
wants feedback on a change in a minute or two rather than after the full
suite's nine to sixteen minutes. It reads which paths changed, maps each
one to the test modules that cover it, says what it selected and why, and
with ``--run`` hands exactly those modules to pytest::

    uv run python -m tests.fast_targets --base main --run
    uv run python -m tests.fast_targets --run src/autoforge/evidence.py
    uv run python -m tests.fast_targets --base main --run -- -n 4 -x

**A FAST run is feedback, never evidence** (ADR 0010 D1, D5). It is no
gate: ``make test``, ``make check``, every CI job and the controller's
``validation.commands`` run what they ran before, and nothing reads what
this tool selected or how its run ended. An agent that relays the result
relays a claim.

**The selection**, for each changed path:

- a test module (``tests/test_*.py``) selects itself, and a removed one
  selects nothing;
- a path with an entry in :data:`COVERAGE` (its own, or else the longest
  directory prefix that holds it) selects the entry's test modules and
  every test module that imports the path's module directly; an entry of
  :data:`FULL` selects the full suite;
- any other path selects the full suite: the mapping never narrows what it
  does not know.

The run's selection is the union of the paths' selections, and the full
suite as soon as one path selects it. Without paths on the command line,
the changed paths are the worktree's differences from its merge base with
``--base`` (committed, staged and unstaged, both sides of a rename) and
the untracked files git does not ignore.

**The mapping** is explicit: every file under ``src/autoforge/`` and every
helper module under ``tests/`` has an entry, and ``tests/test_fast_targets.py``
fails when one has none, when an entry names a path or a test module that
does not exist, or when an entry would select nothing. It records where a
regression is likely to show first, not every test that can observe a
module: a test that reaches a module through the engine (conftest's
``make_engine``) does not import it, so a change can still break a test
outside its selection. A module the engine reaches on every scripted flow
adds that flow's own tests (:data:`LOOP`), and one that almost every test
runs through is :data:`FULL`. A mistake here costs feedback, never safety,
since nothing reads a FAST result.
"""

from __future__ import annotations

import argparse
import ast
import enum
import subprocess
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

REPO_ROOT = Path(__file__).resolve().parent.parent


class Full(enum.Enum):
    """An entry only the full suite can check."""

    FULL = "the full suite"


FULL = Full.FULL
Entry = tuple[str, ...] | Literal[Full.FULL]

# The scripted end-to-end loop (docs/agent-guides/testing.md, "Integration")
# and the engine's own verification, gate and recovery tests: what a module
# the engine calls on every flow breaks first.
LOOP = ("test_engine", "test_integration")

# Test modules whose every test is deselected by the default options
# (``-m 'not live'``): an entry naming one would select nothing that runs.
LIVE_ONLY = frozenset({"test_live_pi"})

# Each entry lists the test modules a change to its path is checked by
# besides those that import its module, which the selection adds itself.
# Keys are paths relative to the repository root; one ending in "/" covers
# every path below it that has no entry of its own.
COVERAGE: Mapping[str, Entry] = {
    # -- the package: what almost every test runs through ---------------------
    "src/autoforge/__init__.py": FULL,  # the version and protocol numbers
    "src/autoforge/config.py": FULL,
    "src/autoforge/engine.py": FULL,
    "src/autoforge/errors.py": FULL,
    "src/autoforge/executor.py": FULL,  # every git call and launch
    "src/autoforge/github.py": FULL,  # the typed values conftest's FakeGitHub returns
    "src/autoforge/prompts/": FULL,  # every launch renders the templates
    "src/autoforge/providers.py": FULL,  # every scripted agent
    "src/autoforge/state.py": FULL,
    "src/autoforge/transitions.py": FULL,
    # -- the package: on every scripted flow ----------------------------------
    "src/autoforge/agent_worktree.py": (*LOOP, "test_validation_gates"),
    "src/autoforge/certificate.py": (*LOOP, "test_unblock"),
    "src/autoforge/child_env.py": LOOP,
    "src/autoforge/claims.py": LOOP,
    "src/autoforge/effect_ops.py": (*LOOP, "test_replan", "test_merge_branch"),
    "src/autoforge/effects.py": LOOP,
    "src/autoforge/evidence.py": (*LOOP, "test_unblock"),
    "src/autoforge/git_transport.py": (*LOOP, "test_replan", "test_validation_gates"),
    "src/autoforge/git_workspace.py": LOOP,
    "src/autoforge/interaction.py": LOOP,
    "src/autoforge/locking.py": LOOP,
    "src/autoforge/loop_guard.py": (*LOOP, "test_routing"),
    "src/autoforge/profiles.py": LOOP,
    "src/autoforge/progress.py": LOOP,
    "src/autoforge/read_context.py": LOOP,
    "src/autoforge/redaction.py": (*LOOP, "test_runlog", "test_progress", "test_evidence"),
    "src/autoforge/replan.py": LOOP,
    "src/autoforge/result_parser.py": (*LOOP, "test_validation_gates", "test_routing"),
    "src/autoforge/runlog.py": LOOP,
    "src/autoforge/safefs.py": LOOP,
    "src/autoforge/validation.py": LOOP,  # every issue, PR and comment URL
    # -- the package: one feature each ----------------------------------------
    "src/autoforge/AGENTS.md": ("test_packaging",),  # package data or deliberately not
    "src/autoforge/CLAUDE.md": ("test_packaging",),
    "src/autoforge/claude_stream.py": ("test_providers",),
    "src/autoforge/cli.py": (),
    "src/autoforge/config_fingerprint.py": ("test_cli",),
    "src/autoforge/doctor.py": ("test_cli",),
    "src/autoforge/doctor_sandbox.py": ("test_doctor",),
    "src/autoforge/epic_commands.py": ("test_epic_recovery", "test_cli"),
    "src/autoforge/epic_graph.py": ("test_epic_commands", "test_epic_recovery"),
    "src/autoforge/executor_duplex.py": ("test_providers",),
    "src/autoforge/executor_supervised.py": ("test_scheduler", "test_worker"),
    "src/autoforge/local_workspace.py": ("test_validation_gates",),
    "src/autoforge/loop_detect.py": ("test_claude_stream", "test_engine"),
    "src/autoforge/materialize.py": (
        "test_premerge",
        "test_validation_gates",
        "test_merge_certificate",
        "test_engine",
    ),
    "src/autoforge/ownership.py": ("test_cli", "test_engine"),
    "src/autoforge/pi_rpc.py": ("test_providers", "test_live_pi_support"),
    "src/autoforge/premerge.py": ("test_engine", "test_merge_certificate"),
    "src/autoforge/replan_txn.py": ("test_cli",),
    "src/autoforge/roadmap.py": ("test_engine",),
    "src/autoforge/run_contract.py": ("test_local", "test_validation_evidence"),
    "src/autoforge/run_namespace.py": ("test_engine",),
    "src/autoforge/sandbox.py": ("test_sandbox_gate", "test_engine"),
    "src/autoforge/sandbox_bwrap.py": ("test_engine_sandbox",),
    "src/autoforge/sandbox_pin.py": ("test_cli", "test_state", "test_worker", "test_engine"),
    "src/autoforge/sandbox_policy.py": ("test_engine_sandbox", "test_engine"),
    "src/autoforge/scheduler.py": ("test_worker",),
    "src/autoforge/scheduler_state.py": (),
    "src/autoforge/scheduler_stop.py": (),
    "src/autoforge/sdk_policy.py": ("test_cli",),
    "src/autoforge/worker.py": ("test_scheduler", "test_epic_commands", "test_epic_recovery"),
    # -- the tests' own helpers -----------------------------------------------
    "tests/__init__.py": FULL,
    "tests/conftest.py": FULL,
    "tests/tmp_path_retention.py": FULL,  # every test's tmp_path
    "tests/claude_fake.py": (),
    "tests/dist_check.py": (),
    "tests/epic_cli.py": ("test_epic_recovery",),
    "tests/fast_targets.py": (),
    "tests/live_pi.py": (),
    "tests/local_helpers.py": (),
    "tests/operator_comment_fidelity.py": (),
    "tests/parallel_bench.py": (),
    "tests/pi_fake.py": (),
    "tests/sandbox_host.py": (),
    "tests/scheduler_worker.py": ("test_scheduler", "test_epic_commands", "test_epic_recovery"),
}

NOT_EVIDENCE = (
    "a FAST run is feedback, not evidence: `make check` runs the full suite, and CI and "
    "the controller's validation decide (ADR 0010)"
)


@dataclass(frozen=True)
class Choice:
    """What one changed path selected, and why."""

    path: str
    modules: frozenset[str]
    full: bool
    reason: str


@dataclass(frozen=True)
class Selection:
    """What a set of changed paths selected from the default suite."""

    choices: tuple[Choice, ...]
    suite: tuple[str, ...]

    @property
    def full(self) -> bool:
        return any(c.full for c in self.choices)

    @property
    def modules(self) -> tuple[str, ...]:
        """The test modules to run: the whole suite's when :attr:`full`."""
        if self.full:
            return self.suite
        return tuple(sorted(set().union(*(c.modules for c in self.choices))))

    def targets(self) -> list[str]:
        """What to hand pytest: ``tests`` (its testpath) for the full suite."""
        if self.full:
            return ["tests"]
        return [f"tests/{m}.py" for m in self.modules]


def suite_modules(root: Path = REPO_ROOT) -> tuple[str, ...]:
    """Every test module the default suite collects (``testpaths = ["tests"]``)."""
    return tuple(sorted(p.stem for p in (root / "tests").glob("test_*.py")))


def module_name(path: str) -> str | None:
    """The module ``path`` imports as (``autoforge.x``, ``tests.x``), or ``None``."""
    p = PurePosixPath(path)
    if p.suffix != ".py":
        return None
    parts = p.with_suffix("").parts
    if parts[:2] == ("src", "autoforge"):
        parts = parts[1:]
    elif parts[:1] != ("tests",):
        return None
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _imported(tree: ast.AST) -> set[str]:
    """The ``autoforge`` and ``tests`` modules ``tree`` imports, anywhere in it."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                # Relative to the ``tests`` package every test module is in.
                base = "tests" + (f".{node.module}" if node.module else "")
            else:
                base = node.module or ""
            names.add(base)
            # ``from autoforge import evidence`` imports the module ``autoforge.evidence``.
            names.update(f"{base}.{alias.name}" for alias in node.names)
    return {n for n in names if n.split(".")[0] in ("autoforge", "tests")}


def importers(root: Path = REPO_ROOT) -> dict[str, frozenset[str]]:
    """For each module, the test modules that import it directly."""
    found: dict[str, set[str]] = {}
    for path in sorted((root / "tests").glob("test_*.py")):
        tree = ast.parse(path.read_bytes(), filename=str(path))
        for name in _imported(tree):
            found.setdefault(name, set()).add(path.stem)
    return {name: frozenset(modules) for name, modules in found.items()}


def entry_key(path: str, coverage: Mapping[str, Entry] = COVERAGE) -> str | None:
    """The key of ``path``'s entry: its own, else the longest prefix holding it."""
    if path in coverage:
        return path
    prefixes = [k for k in coverage if k.endswith("/") and path.startswith(k)]
    return max(prefixes, key=len) if prefixes else None


def choose(
    path: str,
    *,
    suite: Sequence[str],
    imported_by: Mapping[str, frozenset[str]],
    coverage: Mapping[str, Entry] = COVERAGE,
) -> Choice:
    """What one changed path selects (the module docstring's rule)."""
    p = PurePosixPath(path)
    if p.parent == PurePosixPath("tests") and p.name.startswith("test_") and p.suffix == ".py":
        if p.stem in suite:
            return Choice(path, frozenset({p.stem}), False, "a test module: itself")
        return Choice(path, frozenset(), False, "a removed test module: nothing to run")
    key = entry_key(path, coverage)
    if key is None:
        return Choice(path, frozenset(), True, "no entry: the full suite")
    entry = coverage[key]
    if entry is FULL:
        return Choice(path, frozenset(), True, f"{key} is FULL: the full suite")
    module = module_name(path)
    direct = imported_by.get(module, frozenset()) if module else frozenset()
    listed = frozenset(entry)
    reason = f"{len(listed)} listed for {key}"
    if direct - listed:
        reason += f", {len(direct - listed)} more importing {module}"
    return Choice(path, listed | direct, False, reason)


def select(
    paths: Iterable[str],
    *,
    root: Path = REPO_ROOT,
    coverage: Mapping[str, Entry] = COVERAGE,
) -> Selection:
    """The selection for ``paths``, each relative to ``root``."""
    suite = suite_modules(root)
    imported_by = importers(root)
    choices = tuple(
        choose(path, suite=suite, imported_by=imported_by, coverage=coverage)
        for path in sorted(set(paths))
    )
    return Selection(choices, suite)


def _git(root: Path, *args: str) -> str:
    res = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)
    if res.returncode != 0:
        raise SystemExit(f"fast_targets: git {' '.join(args)} failed: {res.stderr.strip()}")
    return res.stdout


def changed_paths(base: str, root: Path = REPO_ROOT) -> tuple[str, list[str]]:
    """The merge base of ``HEAD`` with ``base``, and every path changed since it.

    Committed, staged and unstaged changes to tracked files (``git diff``
    against the merge base compares the worktree), both sides of a rename,
    and the untracked files git does not ignore.
    """
    merge_base = _git(root, "merge-base", base, "HEAD").strip()
    tracked = _git(root, "diff", "--name-only", "--no-renames", "-z", merge_base, "--")
    untracked = _git(root, "ls-files", "--others", "--exclude-standard", "-z")
    return merge_base, sorted({p for p in (tracked + untracked).split("\0") if p})


def _normalized(path: str, root: Path) -> str:
    """``path`` relative to ``root`` when it is inside it, in posix form."""
    absolute = Path(path)
    if absolute.is_absolute() and absolute.is_relative_to(root):
        return absolute.relative_to(root).as_posix()
    return PurePosixPath(path.replace("\\", "/")).as_posix().removeprefix("./")


def report(selection: Selection, origin: str) -> list[str]:
    """The lines saying what was selected and why."""
    lines = [f"fast_targets: {len(selection.choices)} path(s) {origin}"]
    lines += [f"  {c.path}: {c.reason}" for c in selection.choices]
    if selection.full:
        lines.append("fast_targets: selected the full suite")
    else:
        lines.append(
            f"fast_targets: selected {len(selection.modules)} of {len(selection.suite)} test "
            f"modules: {' '.join(selection.modules) or '(none)'}"
        )
    lines.append(f"fast_targets: {NOT_EVIDENCE}")
    return lines


def main(
    argv: Sequence[str] | None = None,
    *,
    root: Path = REPO_ROOT,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    extra: list[str] = []
    if "--" in args:
        split = args.index("--")
        args, extra = args[:split], args[split + 1 :]
    parser = argparse.ArgumentParser(
        prog="python -m tests.fast_targets",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "paths",
        nargs="*",
        help="changed paths relative to the repository root (default: what changed since --base)",
    )
    parser.add_argument(
        "--base",
        default="main",
        help="the revision whose merge base with HEAD the change is read from (default: main)",
    )
    parser.add_argument(
        "--run",
        action="store_true",
        help="run pytest on the selection; arguments after -- are passed to it",
    )
    ns = parser.parse_args(args)
    if extra and not ns.run:
        parser.error("pytest arguments after -- need --run")
    if ns.paths:
        paths = [_normalized(p, root) for p in ns.paths]
        origin = "given"
    else:
        merge_base, paths = changed_paths(ns.base, root)
        origin = f"changed since {ns.base} (merge base {merge_base[:12]})"
    selection = select(paths, root=root)
    for line in report(selection, origin):
        print(line, file=sys.stderr)
    targets = selection.targets()
    if not ns.run:
        print("\n".join(targets))
        return 0
    if not targets:
        print("fast_targets: nothing to run", file=sys.stderr)
        return 0
    code = run([sys.executable, "-m", "pytest", *targets, *extra], cwd=root).returncode
    print(f"fast_targets: pytest exited {code}; {NOT_EVIDENCE}", file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
