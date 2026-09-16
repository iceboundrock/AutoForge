"""Pre-merge evidence the controller produces itself (issue #42).

A green required check proves that the workflow defining it ran to
completion on the PR. It does not prove *which* definition ran (GitHub runs
the PR's own workflow files and reports the result under the same name) and
it does not prove what the tests in the PR *assert* (the commands execute
the PR's own code). This module holds the two controller-side answers the
merge gate adds on top of the hosted check:

- :func:`describe_definition_difference` compares the job/step structure
  of the run that produced the PR's check with the base branch's own run of
  the same workflow, so a redefined workflow is a named difference rather
  than a green check.
- :func:`export_commit_tree` materialises the exact reviewed commit into a
  private temporary directory -- no worktree, no branch, no ``.git`` -- so
  ``merge.verification_commands`` can run the reviewed code on the
  operator's machine without touching the operator's checkout.

Both are pure with respect to workflow state; the engine decides what a
difference or a failing command means for the phase.
"""

from __future__ import annotations

import shutil
import tempfile
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .errors import VerificationError
from .executor import ExecutionRequest, ExecutionResult
from .github import WorkflowRunJobs

Runner = Callable[[ExecutionRequest], ExecutionResult]

# The export is plumbing, never a checkout: a private index file is filled
# from the commit's tree and written out with a path prefix. The operator's
# index, HEAD, working tree and stash are not read or written.
_GIT_TIMEOUT_SECONDS = 600


def describe_definition_difference(pr_jobs: WorkflowRunJobs, base_jobs: WorkflowRunJobs) -> str:
    """Name the first way the PR run's structure differs from the base run's.

    Returns "" only when both runs have the same jobs and, per job, the same
    step names in the same order. Job order is irrelevant (matrix legs start
    in any order); step order is part of the definition and is compared.

    Jobs are compared as a multiset: two jobs of one run may share a name
    (``name:`` set without interpolating the matrix, two job ids with the
    same display name), and a job added under a name the run already has
    must be an extra job, not a duplicate that collapses into the one kept.
    """
    pr_shape = pr_jobs.structure()
    base_shape = base_jobs.structure()
    if pr_shape == base_shape:
        return ""
    pr_names = Counter(name for name, _ in pr_shape)
    base_names = Counter(name for name, _ in base_shape)
    for name in sorted(base_names):
        if pr_names[name] < base_names[name]:
            if base_names[name] == 1:
                return f"job {name!r} of the base branch's run is missing from the PR's run"
            return (
                f"the base branch's run has {base_names[name]} jobs named {name!r}, the PR's "
                f"run has {pr_names[name]}"
            )
    for name in sorted(pr_names):
        if pr_names[name] > base_names[name]:
            if base_names[name] == 0:
                return f"job {name!r} of the PR's run does not exist in the base branch's run"
            return (
                f"the PR's run has {pr_names[name]} jobs named {name!r}, the base branch's "
                f"run has {base_names[name]}"
            )
    # Same job names with the same multiplicities; the sorted shapes pair
    # same-named jobs up by their step lists, so the first pair that differs
    # is a real difference even when the name is shared.
    for (name, base_steps), (_, pr_steps) in zip(base_shape, pr_shape, strict=True):
        if base_steps == pr_steps:
            continue
        for index, (base_step, pr_step) in enumerate(zip(base_steps, pr_steps, strict=False)):
            if base_step != pr_step:
                return (
                    f"job {name!r} step {index + 1} is {pr_step!r} in the PR's run but "
                    f"{base_step!r} in the base branch's run"
                )
        if len(pr_steps) < len(base_steps):
            return f"job {name!r} lacks step {base_steps[len(pr_steps)]!r} of the base branch's run"
        return f"job {name!r} has an extra step {pr_steps[len(base_steps)]!r} in the PR's run"
    # Unreachable when the shapes differ, but a difference must never be
    # reported as "" -- the gate fails closed on what it could not name.
    return "the PR's run and the base branch's run differ in structure"


@dataclass(frozen=True)
class ExportedTree:
    """A commit's tree written out under ``root``; ``root`` is deleted afterwards."""

    sha: str
    root: Path


def _git(runner: Runner, repo: str, args: list[str], env: dict[str, str] | None = None) -> str:
    """Run one git plumbing command against ``repo``; non-zero exit is a failure."""
    req = ExecutionRequest(
        command=["git", "-C", repo, *args],
        env=env,
        timeout_seconds=_GIT_TIMEOUT_SECONDS,
    )
    res = runner(req)
    shown = " ".join(["git", *args])
    if res.timed_out:
        raise VerificationError(f"`{shown}` timed out after {_GIT_TIMEOUT_SECONDS}s")
    if res.exit_code != 0:
        tail = (res.stderr or res.stdout or "").strip()[-500:]
        raise VerificationError(f"`{shown}` failed (exit {res.exit_code}): {tail}")
    if res.truncated:
        raise VerificationError(f"`{shown}` output was truncated at the executor's capture bound")
    return res.stdout or ""


def commit_is_local(runner: Runner, repo: str, sha: str) -> bool:
    res = runner(
        ExecutionRequest(
            command=["git", "-C", repo, "cat-file", "-e", f"{sha}^{{commit}}"],
            timeout_seconds=_GIT_TIMEOUT_SECONDS,
        )
    )
    return not res.timed_out and res.exit_code == 0


def fetch_pr_head(runner: Runner, repo: str, pr_number: int) -> None:
    """Fetch ``refs/pull/<n>/head`` from ``origin`` so the reviewed commit is local.

    Only objects are fetched: no local ref is created or moved (``--no-tags``,
    no destination refspec), so the operator's branches are untouched.
    """
    _git(runner, repo, ["fetch", "--quiet", "--no-tags", "origin", f"refs/pull/{pr_number}/head"])


@contextmanager
def export_commit_tree(runner: Runner, repo: str, sha: str) -> Iterator[ExportedTree]:
    """Write the tree of ``sha`` into a fresh temporary directory, then delete it.

    ``git read-tree`` into a private index (``GIT_INDEX_FILE``) followed by
    ``git checkout-index --prefix`` exports every tracked file of exactly
    that commit and nothing else: no ``.git`` (the exported code cannot
    reach the repository through git), no attribute filtering (unlike
    ``git archive``, whose ``export-ignore`` would silently drop files), no
    worktree or branch (nothing for the operator to clean up, nothing that
    collides with their checkout). Submodules are not populated.

    Raises VerificationError when the commit cannot be materialised; the
    caller decides whether that is inconclusive or conclusive.
    """
    root = Path(tempfile.mkdtemp(prefix="autoforge-premerge-"))
    try:
        index = root / "index"
        tree = root / "tree"
        tree.mkdir()
        env = {"GIT_INDEX_FILE": str(index)}
        _git(runner, repo, ["read-tree", f"{sha}^{{tree}}"], env)
        # The trailing separator is what makes --prefix a directory.
        _git(runner, repo, ["checkout-index", "-a", "-f", f"--prefix={tree}/"], env)
        yield ExportedTree(sha=sha, root=tree)
    finally:
        shutil.rmtree(root, ignore_errors=True)
