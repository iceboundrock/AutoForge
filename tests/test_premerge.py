"""#42: the controller's own pre-merge evidence (definition comparison, tree export).

The export tests use a real local git repository under ``tmp_path``; nothing
touches the network or any GitHub API. The fetch goes through the
controller's git transport to a ``file://`` remote (#160).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import zlib
from pathlib import Path

import pytest

from autoforge.errors import VerificationError
from autoforge.executor import ExecutionRequest, ExecutionResult, execute
from autoforge.git_transport import (
    LOCAL_GIT_ENV_ALLOWLIST,
    LOCAL_GIT_SWITCHES,
    GitRemote,
    GitTransport,
)
from autoforge.github import WorkflowJob, WorkflowRunJobs
from autoforge.premerge import (
    commit_is_local,
    describe_definition_difference,
    export_commit_tree,
    fetch_pr_head,
)
from tests.conftest import git_repo


def _jobs(*jobs: tuple[str, tuple[str, ...]]) -> WorkflowRunJobs:
    return WorkflowRunJobs(
        jobs=tuple(WorkflowJob(name, steps) for name, steps in jobs), total=len(jobs)
    )


BASE = _jobs(("lint", ("Set up job", "Run ruff")), ("test", ("Set up job", "Run pytest")))


def test_same_structure_in_any_job_order_is_no_difference():
    pr = _jobs(("test", ("Set up job", "Run pytest")), ("lint", ("Set up job", "Run ruff")))
    assert describe_definition_difference(pr, BASE) == ""


@pytest.mark.parametrize(
    ("pr", "expected"),
    [
        (
            _jobs(("lint", ("Set up job", "Run ruff"))),
            "job 'test' of the base branch's run is missing",
        ),
        (
            _jobs(
                ("lint", ("Set up job", "Run ruff")),
                ("test", ("Set up job", "Run pytest")),
                ("extra", ()),
            ),
            "job 'extra' of the PR's run does not exist",
        ),
        (
            _jobs(
                ("lint", ("Set up job", "Run ruff")), ("test", ("Set up job", "Run pytest -k x"))
            ),
            "job 'test' step 2 is 'Run pytest -k x' in the PR's run but 'Run pytest'",
        ),
        (
            _jobs(("lint", ("Set up job", "Run ruff")), ("test", ("Set up job",))),
            "job 'test' lacks step 'Run pytest'",
        ),
        (
            _jobs(
                ("lint", ("Set up job", "Run ruff")),
                ("test", ("Set up job", "Run pytest", "Run rm")),
            ),
            "job 'test' has an extra step 'Run rm'",
        ),
        (
            _jobs(("lint", ("Set up job", "Run ruff")), ("test", ("Run pytest", "Set up job"))),
            "job 'test' step 1 is 'Run pytest' in the PR's run but 'Set up job'",
        ),
    ],
)
def test_first_difference_is_named(pr, expected):
    assert describe_definition_difference(pr, BASE).startswith(expected)


# Two jobs of one run may share a display name (`name:` without the matrix
# interpolated, two job ids with one name). The comparison is a multiset, so
# an extra job hiding behind a kept job's name is a named difference (#63 R1-F1).
DUPLICATED = _jobs(("test", ("Set up job", "Run pytest")), ("test", ("Set up job", "Run pytest")))


def test_duplicate_job_names_compare_equal_as_a_multiset():
    assert describe_definition_difference(DUPLICATED, DUPLICATED) == ""
    swapped = _jobs(("test", ("Run pytest",)), ("test", ("Run ruff",)))
    assert (
        describe_definition_difference(
            swapped, _jobs(("test", ("Run ruff",)), ("test", ("Run pytest",)))
        )
        == ""
    )


@pytest.mark.parametrize(
    ("pr", "base", "expected"),
    [
        (
            _jobs(("test", ("Set up job", "Run pytest")), ("test", ("Set up job", "Run rm"))),
            _jobs(("test", ("Set up job", "Run pytest"))),
            "the PR's run has 2 jobs named 'test', the base branch's run has 1",
        ),
        (
            _jobs(("test", ("Set up job", "Run pytest"))),
            DUPLICATED,
            "the base branch's run has 2 jobs named 'test', the PR's run has 1",
        ),
        (
            _jobs(("test", ("Set up job", "Run pytest")), ("test", ("Set up job", "Run rm"))),
            DUPLICATED,
            "job 'test' step 2 is 'Run rm' in the PR's run but 'Run pytest'",
        ),
        (
            _jobs(("test", ("Set up job", "Run pytest")), ("test", ("Set up job",))),
            DUPLICATED,
            "job 'test' lacks step 'Run pytest'",
        ),
        (
            _jobs(
                ("test", ("Set up job", "Run pytest", "Run rm")),
                ("test", ("Set up job", "Run pytest")),
            ),
            DUPLICATED,
            "job 'test' has an extra step 'Run rm'",
        ),
    ],
)
def test_duplicate_job_names_never_hide_a_difference(pr, base, expected):
    assert describe_definition_difference(pr, base).startswith(expected)


# -- tree export ------------------------------------------------------------------------
def _git(repo: Path, *args: str) -> str:
    # Some tests plant GIT_* variables in the controller's environment; the
    # test's own git must not see them.
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env=env
    ).stdout.strip()


def _commit_file(repo: Path, name: str, content: str, message: str) -> str:
    (repo / name).parent.mkdir(parents=True, exist_ok=True)
    (repo / name).write_text(content)
    _git(repo, "add", name)
    _git(
        repo,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@example.com",
        "commit",
        "-q",
        "-m",
        message,
    )
    return _git(repo, "rev-parse", "HEAD")


def test_export_writes_exactly_the_commit_and_leaves_the_checkout_alone(tmp_path):
    repo = git_repo(tmp_path / "repo")
    first = _commit_file(repo, "pkg/a.txt", "one\n", "first")
    second = _commit_file(repo, "b.txt", "two\n", "second")
    # Operator state that must survive untouched: a staged change and a dirty file.
    (repo / "pkg" / "a.txt").write_text("dirty\n")
    (repo / "staged.txt").write_text("staged\n")
    _git(repo, "add", "staged.txt")
    status_before = _git(repo, "status", "--porcelain")
    index_before = (repo / ".git" / "index").read_bytes()

    with export_commit_tree(execute, str(repo), first) as exported:
        root = exported.root
        assert exported.sha == first
        assert root.is_dir() and not str(root).startswith(str(repo))
        assert sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()) == [
            "pkg/a.txt"
        ]
        assert (root / "pkg" / "a.txt").read_text() == "one\n"  # the commit, not the dirty tree
        assert not (root / ".git").exists()
        assert not (root / "b.txt").exists()  # `second` is HEAD, but `first` was asked for
    assert not root.exists()  # cleaned up

    assert _git(repo, "rev-parse", "HEAD") == second
    assert _git(repo, "status", "--porcelain") == status_before
    assert (repo / ".git" / "index").read_bytes() == index_before
    assert (repo / "pkg" / "a.txt").read_text() == "dirty\n"


def test_export_cleans_up_when_the_body_raises(tmp_path):
    repo = git_repo(tmp_path / "repo")
    sha = _commit_file(repo, "a.txt", "x", "c")
    with pytest.raises(RuntimeError, match="boom"):
        with export_commit_tree(execute, str(repo), sha) as exported:
            root = exported.root
            raise RuntimeError("boom")
    assert not root.exists()


def test_export_of_an_unknown_commit_is_a_verification_error(tmp_path):
    repo = git_repo(tmp_path / "repo")
    _commit_file(repo, "a.txt", "x", "c")
    missing = "f" * 40
    assert not commit_is_local(execute, str(repo), missing)
    with pytest.raises(VerificationError, match="read-tree"):
        with export_commit_tree(execute, str(repo), missing):
            pytest.fail("the body must not run")
    assert not list(Path(tmp_path).glob("autoforge-premerge-*"))


def _transport(tmp_path: Path, local: Path, origin: Path) -> GitTransport:
    gh = tmp_path / "bin" / "gh"  # a file:// remote never asks it for a credential
    gh.parent.mkdir(exist_ok=True)
    gh.write_text("#!/bin/sh\nexit 1\n")
    gh.chmod(0o755)

    def no_walk(base: str, sha: str) -> bool:
        raise AssertionError("fetching walks no range")

    return GitTransport(
        object_directory=local / ".git" / "objects",
        remote=GitRemote(url=f"file://{origin}"),
        gh_command=str(gh),
        in_base_history=no_walk,
    )


def test_fetch_pr_head_brings_the_commit_without_creating_a_local_ref(tmp_path):
    origin = git_repo(tmp_path / "origin")
    base = _commit_file(origin, "a.txt", "x", "base")
    pr_head = _commit_file(origin, "b.txt", "y", "pr")
    _git(origin, "update-ref", "refs/pull/7/head", pr_head)
    _git(origin, "reset", "-q", "--hard", base)

    local = git_repo(tmp_path / "local")
    _git(local, "remote", "add", "origin", str(tmp_path / "elsewhere"))  # never used
    _git(local, "fetch", "-q", str(origin), "HEAD")
    assert commit_is_local(execute, str(local), base)
    assert not commit_is_local(execute, str(local), pr_head)

    refs_before = _git(local, "for-each-ref", "--format=%(refname)")
    transport = _transport(tmp_path, local, origin)
    fetch_pr_head(transport, 7)
    assert commit_is_local(execute, str(local), pr_head)
    refs = _git(local, "for-each-ref", "--format=%(refname)")
    assert refs == refs_before  # objects only: no refs/pull, no new refs/heads
    assert "refs/pull" not in refs
    with export_commit_tree(execute, str(local), pr_head) as exported:
        assert (exported.root / "b.txt").read_text() == "y"

    with pytest.raises(VerificationError, match="fetch"):
        fetch_pr_head(transport, 8)


def test_git_plumbing_refuses_truncated_output():
    """A git reply past the capture bound is not the command's output (#53)."""

    def truncated(req):
        return ExecutionResult(req.command, req.cwd, 0, "", "", "t", "t", stdout_truncated=True)

    with pytest.raises(VerificationError, match="truncated"):
        with export_commit_tree(truncated, "/nonexistent", "a" * 40):
            pytest.fail("the body must not run")


# -- object interpretation (#160, ADR 0004 D7.5) ------------------------------------------
def test_a_missing_commit_is_missing_even_with_a_replacement_for_it(tmp_path):
    repo = git_repo(tmp_path / "repo")
    present = _commit_file(repo, "a.txt", "x", "present")
    missing = "f" * 40
    _git(repo, "update-ref", f"refs/replace/{missing}", present)
    blob = _git(repo, "rev-parse", "HEAD:a.txt")
    _git(repo, "update-ref", f"refs/replace/{blob}", present)
    # The attack: the repository's own git reads both names as the commit.
    assert _git(repo, "rev-parse", "--verify", f"{missing}^{{commit}}") == missing
    assert _git(repo, "cat-file", "-t", blob) == "commit"

    assert not commit_is_local(execute, str(repo), missing)
    assert not commit_is_local(execute, str(repo), blob)
    assert commit_is_local(execute, str(repo), present)


def test_a_missing_commit_is_missing_even_when_the_commit_graph_lists_it(tmp_path):
    repo = git_repo(tmp_path / "repo")
    sha = _commit_file(repo, "a.txt", "x", "in the graph")
    _git(repo, "commit-graph", "write", "--reachable")
    _git(repo, "reset", "-q", "--hard", "HEAD~1")
    (repo / ".git" / "objects" / sha[:2] / sha[2:]).unlink()
    _git(repo, "rev-list", "-1", sha)  # the graph answers for it

    assert not commit_is_local(execute, str(repo), sha)


def _rewrite_loose_commit(objects: Path, sha: str, content: bytes) -> None:
    path = objects / sha[:2] / sha[2:]
    path.chmod(0o644)
    path.write_bytes(zlib.compress(b"commit %d\0" % len(content) + content))


@pytest.mark.parametrize("mechanism", ["replace", "commit-graph"])
def test_the_export_writes_the_named_commits_own_tree(tmp_path, mechanism):
    repo = git_repo(tmp_path / "repo")
    named = _commit_file(repo, "a.txt", "named\n", "named")
    other = _commit_file(repo, "a.txt", "other\n", "other")
    other_tree = _git(repo, "rev-parse", f"{other}^{{tree}}")
    if mechanism == "replace":
        _git(repo, "update-ref", f"refs/replace/{named}", other)
        # The attack: the repository's own git derives the other tree.
        assert _git(repo, "rev-parse", f"{named}^{{tree}}") == other_tree
    else:
        copy = tmp_path / "forge.git"
        shutil.copytree(repo / ".git", copy)
        content = subprocess.run(
            ["git", f"--git-dir={copy}", "cat-file", "commit", named],
            check=True,
            capture_output=True,
            env={k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
        ).stdout
        old_tree = _git(repo, "rev-parse", f"{named}^{{tree}}")
        _rewrite_loose_commit(
            copy / "objects", named, content.replace(old_tree.encode(), other_tree.encode())
        )
        subprocess.run(
            ["git", f"--git-dir={copy}", "commit-graph", "write", "--reachable"],
            check=True,
            capture_output=True,
            env={k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
        )
        shutil.copy(copy / "objects" / "info" / "commit-graph", repo / ".git" / "objects" / "info")

    with export_commit_tree(execute, str(repo), named) as exported:
        assert (exported.root / "a.txt").read_text() == "named\n"


def test_premerge_git_is_hardened_and_runs_filters_without_credentials(tmp_path, monkeypatch):
    repo = git_repo(tmp_path / "repo")
    sha = _commit_file(repo, "a.txt", "content\n", "c")
    decoy = git_repo(tmp_path / "decoy")
    dump = tmp_path / "filter-env"
    sentinel = tmp_path / "fsmonitor-ran"
    monitor = tmp_path / "fsmonitor"
    monitor.write_text(f'#!/bin/sh\ntouch "{sentinel}"\n')
    monitor.chmod(0o755)
    (repo / ".git" / "info").mkdir(exist_ok=True)
    (repo / ".git" / "info" / "attributes").write_text("* filter=leak\n")
    _git(repo, "config", "filter.leak.smudge", f"env > {dump}; cat")
    _git(repo, "config", "core.fsmonitor", str(monitor))
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_controllertokenvalue")
    monkeypatch.setenv("GH_TOKEN", "ghp_controllertokenvalue")
    monkeypatch.setenv("GIT_DIR", str(decoy / ".git"))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.hooksPath")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(tmp_path))
    requests: list[ExecutionRequest] = []

    def runner(request: ExecutionRequest) -> ExecutionResult:
        requests.append(request)
        return execute(request)

    assert commit_is_local(runner, str(repo), sha)
    with export_commit_tree(runner, str(repo), sha) as exported:
        assert (exported.root / "a.txt").read_text() == "content\n"

    assert dump.exists()  # the filter ran, and saw no credential and no planted GIT_*
    seen = dump.read_text()
    assert "ghp_controllertokenvalue" not in seen
    assert str(decoy) not in seen and "GIT_CONFIG_COUNT" not in seen
    assert not sentinel.exists()
    assert len(requests) == 3
    for request in requests:
        assert request.command[: 1 + len(LOCAL_GIT_SWITCHES)] == ["git", *LOCAL_GIT_SWITCHES]
        assert request.env_allowlist == LOCAL_GIT_ENV_ALLOWLIST
        assert not any(name.startswith("GIT_") for name in request.env_allowlist)
        assert (request.env or {})["GIT_NO_REPLACE_OBJECTS"] == "1"
