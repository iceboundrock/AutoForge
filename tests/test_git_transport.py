"""#160: the controller's own git transport (ADR 0004 D6.1-D6.4, D7.1-D7.5).

Every test runs against local repositories under ``tmp_path``: a bare
repository is the remote (``file://``) and a non-bare one is the shared
repository whose object store the transport reads and fills. Nothing touches
the network or GitHub. ``in_base_history`` is answered from the bare
remote's authentic history, as GitHub answers it from its own.

The test's own git commands scrub every ``GIT_*`` variable and read no
global or system configuration, because some tests plant such variables in
the controller's environment on purpose.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import tempfile
import zlib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from autoforge.errors import ExecutionError, GitHubUnavailableError, GitTransportError
from autoforge.executor import ExecutionRequest, ExecutionResult, execute
from autoforge.git_transport import (
    LOCAL_GIT_ENV_ALLOWLIST,
    MAX_RANGE_COMMITS,
    NETWORK_GIT_ENV_ALLOWLIST,
    CommitRecord,
    GitRemote,
    GitTransport,
    PushOutcome,
    RangeProof,
    published_range_problem,
)

IDENT = ("-c", "user.name=t", "-c", "user.email=t@example.com")
CREDENTIAL = "fake-credential-0123456789"


def _clean_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    return env


def _run_git(*args: str, input: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", *args], env=_clean_env(), input=input, capture_output=True, check=False
    )


def git(*args: str, input: bytes | None = None) -> str:
    res = _run_git(*args, input=input)
    assert res.returncode == 0, res.stderr.decode(errors="replace")
    return res.stdout.decode().strip()


def git_ok(*args: str) -> bool:
    return _run_git(*args).returncode == 0


def write_loose(objects: Path, sha: str, kind: str, content: bytes) -> None:
    """Overwrite the loose object file named ``sha`` with other content."""
    path = objects / sha[:2] / sha[2:]
    if path.exists():
        path.chmod(0o644)
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(zlib.compress(b"%s %d\0" % (kind.encode(), len(content)) + content))


@dataclass
class World:
    tmp: Path
    shared: Path
    remote: Path
    gh: Path
    gh_log: Path
    questions: list[tuple[str, str]] = field(default_factory=list)
    requests: list[ExecutionRequest] = field(default_factory=list)
    answer: Callable[[str, str], bool] | None = None

    @property
    def objects(self) -> Path:
        return self.shared / ".git" / "objects"

    @property
    def url(self) -> str:
        return f"file://{self.remote}"

    def g(self, *args: str, input: bytes | None = None) -> str:
        return git("-C", str(self.shared), *IDENT, *args, input=input)

    def r(self, *args: str) -> str:
        return git(f"--git-dir={self.remote}", *args)

    def tree(self, files: dict[str, str] | None = None) -> str:
        lines = []
        for name, content in sorted((files or {}).items()):
            blob = self.g("hash-object", "-w", "--stdin", input=content.encode())
            lines.append(f"100644 blob {blob}\t{name}\n")
        return self.g("mktree", input="".join(lines).encode())

    def commit(self, message: str, *parents: str, tree: str | None = None) -> str:
        argv = ["commit-tree", tree or self.tree()]
        for parent in parents:
            argv += ["-p", parent]
        return self.g(*argv, "-m", message)

    def raw(self, sha: str) -> bytes:
        res = _run_git("-C", str(self.shared), "cat-file", "commit", sha)
        assert res.returncode == 0
        return res.stdout

    def publish(self, sha: str, ref: str) -> None:
        self.g("push", "-q", self.url, f"{sha}:{ref}")

    def remote_ref(self, ref: str) -> str | None:
        res = _run_git(f"--git-dir={self.remote}", "rev-parse", "-q", "--verify", ref)
        return res.stdout.decode().strip() if res.returncode == 0 else None

    def drop(self, sha: str) -> None:
        (self.objects / sha[:2] / sha[2:]).unlink()

    def local_refs(self) -> str:
        return self.g("for-each-ref", "--format=%(refname) %(objectname)")

    def in_base_history(self, base: str, sha: str) -> bool:
        self.questions.append((base, sha))
        if self.answer is not None:
            return self.answer(base, sha)
        return git_ok(f"--git-dir={self.remote}", "merge-base", "--is-ancestor", sha, base)

    def runner(self, request: ExecutionRequest) -> ExecutionResult:
        self.requests.append(request)
        return execute(request)

    def transport(self, **kwargs) -> GitTransport:
        kwargs.setdefault("gh_command", str(self.gh))
        kwargs.setdefault("runner", self.runner)
        return GitTransport(
            object_directory=self.objects,
            remote=GitRemote(url=self.url),
            in_base_history=self.in_base_history,
            **kwargs,
        )

    def pushes(self) -> list[ExecutionRequest]:
        return [r for r in self.requests if "push" in r.command]

    def reads_of(self, sha: str) -> list[ExecutionRequest]:
        return [r for r in self.requests if r.stdin_data and sha.encode() in r.stdin_data]


def make_world(tmp_path: Path, object_format: str = "sha1") -> World:
    shared = tmp_path / "shared"
    remote = tmp_path / "remote.git"
    git("init", "-q", "-b", "main", f"--object-format={object_format}", str(shared))
    git("init", "-q", "--bare", "-b", "main", f"--object-format={object_format}", str(remote))
    git("-C", str(shared), "config", "gc.auto", "0")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    gh_log = tmp_path / "gh.log"
    gh = bindir / "gh"
    gh.write_text(
        "#!/bin/sh\n"
        f'echo "$@" >> "{gh_log}"\n'
        "cat >/dev/null\n"
        f"printf 'username=x-access-token\\npassword={CREDENTIAL}\\n'\n"
    )
    gh.chmod(0o755)
    return World(tmp=tmp_path, shared=shared, remote=remote, gh=gh, gh_log=gh_log)


@pytest.fixture
def world(tmp_path, monkeypatch) -> World:
    # Every private directory is made here, so a leftover is visible.
    private = tmp_path / "private-tmp"
    private.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(private))
    return make_world(tmp_path)


def private_leftovers(world: World) -> list[str]:
    return sorted(p.name for p in (world.tmp / "private-tmp").iterdir())


CLOSES_ANOTHER_ISSUE = re.compile(
    r"(?i)\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(?!42\b)\d+\b"
)


def message_problem(message: str) -> str | None:
    """Issue #42's policy, as D8.5 runs it: no closing keyword for another issue."""
    if CLOSES_ANOTHER_ISSUE.search(message):
        return "the message closes another issue"
    return None


@dataclass
class Merge:
    """ADR 0004 §6.1's graph: R <- A <- B (main), H (side), candidate C = B + H."""

    r: str
    a: str
    b: str
    h: str
    c: str


def merge_graph(world: World, *, h_message: str, h_parent: str = "r") -> Merge:
    r = world.commit("root")
    a = world.commit("Fixes #7", r)
    b = world.commit("base", a)
    h = world.commit(h_message, {"r": r, "a": a}[h_parent])
    c = world.commit("merge side into the issue branch", b, h)
    world.publish(b, "refs/heads/main")
    world.publish(h, "refs/heads/side")
    return Merge(r=r, a=a, b=b, h=h, c=c)


# -- GitRemote --------------------------------------------------------------------------
def test_https_remote_is_built_from_owner_and_repo():
    assert GitRemote.https("octo-org/repo.name_1").url == (
        "https://github.com/octo-org/repo.name_1.git"
    )


@pytest.mark.parametrize(
    "repository",
    ["octo", "octo/repo/x", "../repo", "octo/..", "./repo", "octo/re po", "octo/repo\n", "", "/"],
)
def test_https_remote_refuses_what_is_not_owner_slash_repo(repository):
    with pytest.raises(GitTransportError):
        GitRemote.https(repository)


@pytest.mark.parametrize(
    "url",
    [
        "origin",
        "https://x-access-token:secret@github.com/o/r.git",
        "https://token@github.com/o/r.git",
        "https://github.com/o/r.git?x=1",
        "https://github.com/o/r.git#frag",
        "https://github.com:443/o/r.git",
        "http://github.com/o/r.git",
        "ssh://git@github.com/o/r.git",
        "git@github.com:o/r.git",
        "ext::sh -c touch% /tmp/x",
        "file://relative/path",
        "https://github.com/o/r .git",
        "--upload-pack=touch /tmp/x",
    ],
)
def test_remote_url_refuses_credentials_options_and_other_transports(url):
    with pytest.raises(GitTransportError) as info:
        GitRemote(url=url)
    assert "secret" not in str(info.value)


# -- fetch ------------------------------------------------------------------------------
def test_fetch_by_pull_ref_and_by_sha_lands_objects_and_creates_no_ref(world):
    base = world.commit("base")
    pr_head = world.commit("pr head", base)
    side = world.commit("side", base)
    world.publish(base, "refs/heads/main")
    world.publish(pr_head, "refs/pull/7/head")
    world.publish(side, "refs/heads/side")
    world.drop(pr_head)
    world.drop(side)
    refs_before = world.local_refs()
    transport = world.transport()
    assert not transport.has_commit(pr_head)
    assert not transport.has_commit(side)

    transport.fetch(["refs/pull/7/head"])
    assert transport.has_commit(pr_head)
    transport.fetch([side])
    assert transport.has_commit(side)

    assert world.local_refs() == refs_before
    assert not (world.shared / ".git" / "FETCH_HEAD").exists()
    assert world.remote_ref("refs/pull/7/head") == pr_head  # the remote is only read
    assert private_leftovers(world) == []
    fetches = [r for r in world.requests if "fetch" in r.command]
    assert len(fetches) == 2
    for request, wanted in zip(fetches, (["refs/pull/7/head"], [side]), strict=True):
        # No destination: the revisions alone follow the explicit URL.
        assert request.command[request.command.index(world.url) + 1 :] == wanted
        for flag in ("--no-tags", "--no-write-fetch-head", "--no-recurse-submodules"):
            assert flag in request.command


def test_a_failed_fetch_is_a_transport_error_and_leaves_no_private_directory(world):
    world.publish(world.commit("base"), "refs/heads/main")
    with pytest.raises(GitTransportError, match="refs/pull/8/head"):
        world.transport().fetch(["refs/pull/8/head"])
    assert private_leftovers(world) == []


@pytest.mark.parametrize(
    "revision",
    [
        "main",
        "HEAD",
        "refs/heads/a:refs/heads/b",
        "+refs/heads/a",
        "refs/heads/../x",
        "refs/heads/a*",
        "refs/heads/a^",
        "refs/heads/a~1",
        "-c",
        "--upload-pack=touch x",
        "abc123",
        "A" * 40,
        "a" * 41,
    ],
)
def test_fetch_refuses_anything_but_a_full_sha_or_ref_before_any_process(world, revision):
    with pytest.raises(GitTransportError):
        world.transport().fetch([revision])
    assert world.requests == []


def test_fetch_refuses_a_bare_string_and_an_empty_list(world):
    transport = world.transport()
    with pytest.raises(GitTransportError):
        transport.fetch("refs/pull/1/head")
    with pytest.raises(GitTransportError):
        transport.fetch([])
    assert world.requests == []


def test_a_missing_gh_is_refused_before_any_process(world):
    transport = world.transport(gh_command="autoforge-no-such-gh-binary")
    with pytest.raises(GitTransportError, match="not found"):
        transport.fetch(["refs/heads/main"])
    sha = world.commit("x")
    with pytest.raises(GitTransportError, match="not found"):
        transport.push(sha=sha, branch="feature", expected_old=None, default_branch="main")
    assert world.requests == []


# -- reading a commit -------------------------------------------------------------------
def test_read_commit_returns_the_authenticated_parents_and_message(world):
    p1 = world.commit("one")
    p2 = world.commit("two")
    merge = world.g("commit-tree", world.tree(), "-p", p1, "-p", p2, "-m", "subject", "-m", "body")
    record = world.transport().read_commit(merge)
    assert record == CommitRecord(sha=merge, parents=(p1, p2), message="subject\n\nbody\n")
    root = world.transport().read_commit(p1)
    assert root.parents == ()


def test_a_missing_commit_is_fetched_once_and_then_read(world):
    sha = world.commit("published")
    world.publish(sha, "refs/heads/side")
    world.drop(sha)
    assert world.transport().read_commit(sha).message == "published\n"
    assert len([r for r in world.requests if "fetch" in r.command]) == 1


def test_a_commit_still_missing_after_the_refetch_fails_closed(world):
    world.publish(world.commit("base"), "refs/heads/main")
    sha = world.commit("never published")
    world.drop(sha)
    with pytest.raises(GitTransportError, match=sha):
        world.transport().read_commit(sha)
    assert len([r for r in world.requests if "fetch" in r.command]) == 1


def test_a_rewritten_loose_commit_fails_closed_on_its_id_without_a_refetch(world):
    sha = world.commit("original message")
    forged = world.raw(sha).replace(b"original message", b"forged message")
    write_loose(world.objects, sha, "commit", forged)
    assert b"forged" in world.raw(sha)  # git itself reads the rewritten bytes
    with pytest.raises(GitTransportError, match=f"{sha}.*do not hash"):
        world.transport().read_commit(sha)
    assert not [r for r in world.requests if "fetch" in r.command]


def _literal_commit(world: World, content: bytes) -> str:
    return world.g("hash-object", "-t", "commit", "-w", "--literally", "--stdin", input=content)


def test_a_commit_that_is_not_utf8_fails_closed(world):
    tree = world.tree()
    content = (
        f"tree {tree}\nauthor t <t@example.com> 0 +0000\n"
        "committer t <t@example.com> 0 +0000\nencoding ISO-8859-1\n\n"
    ).encode() + b"caf\xe9\n"
    sha = _literal_commit(world, content)
    with pytest.raises(GitTransportError, match="not valid UTF-8"):
        world.transport().read_commit(sha)


@pytest.mark.parametrize(
    ("make", "reason"),
    [
        (lambda t, p: b"author t <t@e> 0 +0000\n\nno tree\n", "tree line"),
        (lambda t, p: f"tree {t}\nparent {p[:10]}\n\nshort parent\n".encode(), "malformed parent"),
        (
            lambda t, p: (
                f"tree {t}\nauthor t <t@e> 0 +0000\nparent {p}\n\nparent out of place\n"
            ).encode(),
            "out of place",
        ),
    ],
)
def test_commit_headers_are_parsed_as_strictly_as_git_reads_them(world, make, reason):
    parent = world.commit("parent")
    sha = _literal_commit(world, make(world.tree(), parent))
    with pytest.raises(GitTransportError, match=reason):
        world.transport().read_commit(sha)


def test_a_parent_line_inside_the_message_is_message_text(world):
    parent = world.commit("parent")
    sha = world.commit(f"subject\n\nparent {parent}", world.commit("real"))
    record = world.transport().read_commit(sha)
    assert parent not in record.parents
    assert f"parent {parent}" in record.message


def test_an_object_that_is_not_a_commit_is_refused(world):
    blob = world.g("hash-object", "-w", "--stdin", input=b"data\n")
    transport = world.transport()
    assert not transport.has_commit(blob)
    with pytest.raises(GitTransportError, match="blob"):
        transport.read_commit(blob)


@pytest.mark.parametrize("sha", ["", "abc", "A" * 40, "g" * 40, "a" * 50, " " + "a" * 40])
def test_a_malformed_sha_is_refused_before_any_process(world, sha):
    transport = world.transport()
    with pytest.raises(GitTransportError):
        transport.read_commit(sha)
    with pytest.raises(GitTransportError):
        transport.has_commit(sha)
    assert world.requests == []


def test_sha256_repositories_are_read_fetched_and_walked_in_their_own_format(tmp_path, monkeypatch):
    private = tmp_path / "private-tmp"
    private.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(private))
    world = make_world(tmp_path, "sha256")
    base = world.commit("base")
    side = world.commit("side", base)
    candidate = world.commit("candidate", base, side)
    assert len(candidate) == 64
    world.publish(base, "refs/heads/main")
    world.publish(side, "refs/heads/side")
    world.drop(side)
    seen: list[str] = []

    def runner(request: ExecutionRequest) -> ExecutionResult:
        seen.append((Path(request.env["GIT_DIR"]) / "config").read_text())
        return execute(request)

    transport = world.transport(runner=runner)
    proof = transport.prove_range(base, candidate)
    assert [c.sha for c in proof.commits] == [candidate, side]
    assert proof.reached_base
    assert all("objectFormat = sha256" in config for config in seen)
    assert all("repositoryformatversion = 1" in config for config in seen)
    with pytest.raises(GitTransportError, match="different object formats"):
        transport.prove_range("a" * 40, candidate)


# -- existence ----------------------------------------------------------------------------
def test_a_missing_commit_is_missing_even_with_a_replacement_for_it(world):
    present = world.commit("present")
    missing = world.commit("missing")
    world.drop(missing)
    world.g("update-ref", f"refs/replace/{missing}", present)
    # The attack: the shared repository's own git now reports the commit.
    check = world.g("cat-file", "--batch-check", input=f"{missing}\n".encode())
    assert check.startswith(f"{missing} commit")
    assert world.g("rev-parse", "--verify", f"{missing}^{{commit}}") == missing

    assert not world.transport().has_commit(missing)


def test_a_missing_commit_is_missing_even_when_the_commit_graph_lists_it(world):
    sha = world.commit("in the graph", world.commit("root"))
    world.g("update-ref", "refs/heads/keep", sha)
    world.g("commit-graph", "write", "--reachable")
    world.g("update-ref", "-d", "refs/heads/keep")
    world.drop(sha)
    assert git_ok("-C", str(world.shared), "rev-list", "-1", sha)  # the graph answers for it

    assert not world.transport().has_commit(sha)


# -- the range walk -----------------------------------------------------------------------
def test_a_rewritten_ancestor_of_the_base_does_not_shrink_the_range(world):
    g = merge_graph(world, h_message="Fixes #99")
    forged = world.raw(g.a).replace(
        f"parent {g.r}\n".encode(), f"parent {g.r}\nparent {g.h}\n".encode()
    )
    write_loose(world.objects, g.a, "commit", forged)
    # The attack: git's own range now hides H.
    assert world.g("rev-list", f"{g.b}..{g.c}") == g.c

    proof = world.transport().prove_range(g.b, g.c)
    assert [c.sha for c in proof.commits] == [g.c, g.h]
    assert proof.reached_base
    assert all(sha != g.a for _, sha in world.questions)
    assert world.reads_of(g.a) == []
    problem = published_range_problem(proof, message_problem)
    assert problem is not None and g.h in problem
    assert "#99" not in problem and "Fixes" not in problem


def test_a_clean_range_publishes_and_the_base_history_is_never_read(world):
    g = merge_graph(world, h_message="add the feature", h_parent="a")
    proof = world.transport().prove_range(g.b, g.c)
    assert [c.sha for c in proof.commits] == [g.c, g.h]
    assert proof.reached_base
    # A ("Fixes #7") is in the base's history: left out on GitHub's word, unread.
    assert (g.b, g.a) in world.questions
    assert world.reads_of(g.a) == []
    assert published_range_problem(proof, message_problem) is None


def test_the_candidate_equal_to_the_base_is_an_empty_range_that_reaches_it(world):
    base = world.commit("base")
    proof = world.transport().prove_range(base, base)
    assert proof == RangeProof(base=base, candidate=base, commits=(), reached_base=True)
    assert world.questions == []
    assert world.transport().descends_from(base, base)


def test_an_unrelated_candidate_does_not_descend_from_the_base(world):
    base = world.commit("base")
    world.publish(base, "refs/heads/main")
    unrelated = world.commit("unrelated")
    transport = world.transport()
    proof = transport.prove_range(base, unrelated)
    assert not proof.reached_base
    assert [c.sha for c in proof.commits] == [unrelated]
    assert not transport.descends_from(unrelated, base)


def test_a_walk_over_the_bound_fails_closed(world):
    base = world.commit("base")
    world.publish(base, "refs/heads/main")
    tip = base
    for n in range(5):
        tip = world.commit(f"c{n}", tip)
    with pytest.raises(GitTransportError, match="more than 3"):
        world.transport(max_range_commits=3).prove_range(base, tip)
    assert world.transport(max_range_commits=5).descends_from(tip, base)
    assert MAX_RANGE_COMMITS >= 1000


def test_an_unavailable_ancestry_answer_fails_closed(world):
    base = world.commit("base")
    candidate = world.commit("candidate", base)

    def unavailable(base: str, sha: str) -> bool:
        raise GitHubUnavailableError("compare failed after retries: GITHUB_TOKEN=ghp_abcdefghijkl")

    world.answer = unavailable
    with pytest.raises(GitTransportError, match="unavailable") as info:
        world.transport().prove_range(base, candidate)
    assert "ghp_abcdefghijkl" not in str(info.value)


def test_an_ancestry_answer_that_is_not_a_boolean_fails_closed(world):
    base = world.commit("base")
    candidate = world.commit("candidate", base)
    world.answer = lambda base, sha: None  # type: ignore[assignment,return-value]
    with pytest.raises(GitTransportError, match="not a boolean"):
        world.transport().prove_range(base, candidate)


def test_a_commit_of_the_range_still_missing_after_the_refetch_fails_closed(world):
    base = world.commit("base")
    world.publish(base, "refs/heads/main")
    lost = world.commit("lost", base)
    candidate = world.commit("candidate", lost)
    world.drop(lost)
    with pytest.raises(GitTransportError, match=lost):
        world.transport().prove_range(base, candidate)


def test_published_range_problem_names_the_first_refused_commit_by_sha():
    commits = (
        CommitRecord("a" * 40, (), "clean"),
        CommitRecord("b" * 40, (), "Closes #5 with secret words"),
        CommitRecord("c" * 40, (), "Fixes #6"),
    )
    proof = RangeProof(base="d" * 40, candidate="a" * 40, commits=commits, reached_base=True)
    problem = published_range_problem(proof, message_problem)
    assert problem is not None
    assert "b" * 40 in problem and "c" * 40 not in problem
    assert "secret words" not in problem
    clean = RangeProof(base="d" * 40, candidate="a" * 40, commits=commits[:1], reached_base=True)
    assert published_range_problem(clean, message_problem) is None


# -- object interpretation: planted substitution state ------------------------------------
def _forge_commit_graph(world: World, sha: str, content: bytes) -> None:
    """Install a commit-graph that records ``sha`` as ``content`` says."""
    copy = world.tmp / "forge.git"
    shutil.copytree(world.shared / ".git", copy)
    git(f"--git-dir={copy}", "update-ref", "refs/heads/forge", sha)
    write_loose(copy / "objects", sha, "commit", content)
    git(f"--git-dir={copy}", "commit-graph", "write", "--reachable")
    shutil.copy(copy / "objects" / "info" / "commit-graph", world.objects / "info" / "commit-graph")
    shutil.rmtree(copy)


def _plant(world: World, mechanism: str, sha: str, parents: tuple[str, ...]) -> None:
    """Make the shared repository's own git read ``sha`` with ``parents``."""
    raw = world.raw(sha)
    header, _, message = raw.partition(b"\n\n")
    lines = [line for line in header.split(b"\n") if not line.startswith(b"parent ")]
    content = (
        b"\n".join([lines[0], *(f"parent {p}".encode() for p in parents), *lines[1:]])
        + b"\n\n"
        + message
    )
    if mechanism == "replace":
        replacement = _literal_commit(world, content)
        world.g("update-ref", f"refs/replace/{sha}", replacement)
    elif mechanism == "graft":
        (world.shared / ".git" / "info" / "grafts").write_text(" ".join((sha, *parents)) + "\n")
    elif mechanism == "shallow":
        assert parents == ()
        (world.shared / ".git" / "shallow").write_text(sha + "\n")
    elif mechanism == "commit-graph":
        _forge_commit_graph(world, sha, content)
    else:  # pragma: no cover
        raise AssertionError(mechanism)


@pytest.mark.parametrize("mechanism", ["replace", "graft", "commit-graph"])
def test_planted_ancestry_passes_neither_the_ancestry_nor_the_fast_forward_check(world, mechanism):
    base = world.commit("base")
    world.publish(base, "refs/heads/main")
    world.publish(base, "refs/heads/feature")
    unrelated = world.commit("unrelated")
    _plant(world, mechanism, unrelated, (base,))
    # The attack: the shared repository's own git now says it descends.
    assert git_ok("-C", str(world.shared), "merge-base", "--is-ancestor", base, unrelated)

    transport = world.transport()
    assert not transport.descends_from(unrelated, base)
    with pytest.raises(GitTransportError) as info:
        transport.push(sha=unrelated, branch="feature", expected_old=base, default_branch="main")
    assert unrelated in str(info.value) and base in str(info.value)
    assert world.pushes() == []
    assert world.remote_ref("refs/heads/feature") == base


@pytest.mark.parametrize("mechanism", ["replace", "graft", "shallow", "commit-graph"])
def test_planted_history_does_not_hide_a_commit_from_the_message_check(world, mechanism):
    g = merge_graph(world, h_message="Closes #99")
    _plant(world, mechanism, g.c, () if mechanism == "shallow" else (g.b,))
    # The attack: git's own range no longer holds H. (``C ^B``: with a
    # forged commit-graph, git 2.55 reads ``B..C`` from the objects but
    # ``C ^B`` from the graph.)
    assert world.g("rev-list", g.c, f"^{g.b}") == g.c

    proof = world.transport().prove_range(g.b, g.c)
    assert [c.sha for c in proof.commits] == [g.c, g.h]
    assert proof.reached_base
    problem = published_range_problem(proof, message_problem)
    assert problem is not None and g.h in problem


def test_a_rewritten_commit_in_the_range_fails_both_checks_closed(world):
    g = merge_graph(world, h_message="add the feature")
    world.publish(g.b, "refs/heads/feature")
    clean = world.raw(g.h)
    write_loose(world.objects, g.h, "commit", clean.replace(b"add the feature", b"Fixes #42"))
    transport = world.transport()
    with pytest.raises(GitTransportError, match=g.h):
        transport.prove_range(g.b, g.c)
    with pytest.raises(GitTransportError, match=g.h):
        transport.push(sha=g.c, branch="feature", expected_old=g.b, default_branch="main")
    assert world.pushes() == []


# -- push ---------------------------------------------------------------------------------
def test_push_creates_then_fast_forwards_and_touches_no_local_ref(world):
    g = merge_graph(world, h_message="add the feature")
    refs_before = world.local_refs()
    transport = world.transport()

    created = transport.push(
        sha=g.c, branch="autoforge/160-x", expected_old=None, default_branch="main"
    )
    assert created.outcome is PushOutcome.PUSHED
    assert world.remote_ref("refs/heads/autoforge/160-x") == g.c

    nxt = world.commit("fix review findings", g.c)
    forward = transport.push(
        sha=nxt, branch="autoforge/160-x", expected_old=g.c, default_branch="main"
    )
    assert forward.outcome is PushOutcome.PUSHED
    assert (forward.sha, forward.branch, forward.expected_old) == (nxt, "autoforge/160-x", g.c)
    assert world.remote_ref("refs/heads/autoforge/160-x") == nxt

    again = transport.push(
        sha=nxt, branch="autoforge/160-x", expected_old=g.c, default_branch="main"
    )
    assert again.outcome is PushOutcome.UP_TO_DATE

    assert world.local_refs() == refs_before
    assert world.remote_ref("refs/heads/main") == g.b
    assert private_leftovers(world) == []
    for request in world.pushes():
        assert "--force" not in request.command
        assert not any(arg.startswith("+") for arg in request.command)
        assert "--no-verify" in request.command and "--porcelain" in request.command
        assert request.command[-1].endswith(":refs/heads/autoforge/160-x")


def test_a_lease_mismatch_is_rejected_and_leaves_the_remote_alone(world):
    base = world.commit("base")
    world.publish(base, "refs/heads/main")
    head = world.commit("controller head", base)
    world.publish(head, "refs/heads/feature")
    human = world.commit("a human pushed", head)
    world.publish(human, "refs/heads/feature")
    candidate = world.commit("controller fix", head)

    result = world.transport().push(
        sha=candidate, branch="feature", expected_old=head, default_branch="main"
    )
    assert result.outcome is PushOutcome.LEASE_REJECTED
    assert world.remote_ref("refs/heads/feature") == human


def test_an_absent_lease_is_rejected_when_the_branch_exists(world):
    base = world.commit("base")
    world.publish(base, "refs/heads/feature")
    candidate = world.commit("candidate", base)
    result = world.transport().push(
        sha=candidate, branch="feature", expected_old=None, default_branch="main"
    )
    assert result.outcome is PushOutcome.LEASE_REJECTED
    assert world.remote_ref("refs/heads/feature") == base


def test_a_non_fast_forward_is_refused_naming_both_shas_before_any_push(world):
    base = world.commit("base")
    world.publish(base, "refs/heads/main")
    head = world.commit("head", base)
    world.publish(head, "refs/heads/feature")
    sibling = world.commit("sibling", base)
    with pytest.raises(GitTransportError) as info:
        world.transport().push(
            sha=sibling, branch="feature", expected_old=head, default_branch="main"
        )
    assert sibling in str(info.value) and head in str(info.value)
    assert world.pushes() == []
    assert world.remote_ref("refs/heads/feature") == head


@pytest.mark.parametrize(("branch", "default"), [("main", "main"), ("Main", "main")])
def test_the_default_branch_is_refused_before_any_process(world, branch, default):
    sha = world.commit("x")
    with pytest.raises(GitTransportError, match="default branch"):
        world.transport().push(sha=sha, branch=branch, expected_old=None, default_branch=default)
    assert world.requests == []


@pytest.mark.parametrize(
    "branch",
    [
        "",
        "HEAD",
        "refs/heads/feature",
        "a..b",
        "-x",
        ".x",
        "x.lock",
        "x.",
        "x/",
        "/x",
        "a//b",
        "a b",
        "a:b",
        "+x",
        "x~1",
        "x^",
        "x*",
        "x@{1}",
        "x\\y",
        "a" * 256,
    ],
)
def test_an_invalid_branch_name_is_refused_before_any_process(world, branch):
    sha = world.commit("x")
    with pytest.raises(GitTransportError):
        world.transport().push(sha=sha, branch=branch, expected_old=None, default_branch="main")
    assert world.requests == []


@pytest.mark.parametrize(
    ("sha", "expected_old"),
    [("abc", None), ("A" * 40, None), ("a" * 40, ""), ("a" * 40, "HEAD"), ("a" * 40, "b" * 64)],
)
def test_an_invalid_sha_or_expected_old_is_refused_before_any_process(world, sha, expected_old):
    with pytest.raises(GitTransportError):
        world.transport().push(
            sha=sha, branch="feature", expected_old=expected_old, default_branch="main"
        )
    assert world.requests == []


def _canned_push(world: World, stdout: str = "", **result) -> Callable[..., ExecutionResult]:
    def runner(request: ExecutionRequest) -> ExecutionResult:
        if "push" not in request.command:
            return world.runner(request)
        world.requests.append(request)
        if result.pop("raise_", False):
            raise ExecutionError("the pipe broke")
        return ExecutionResult(
            request.command,
            request.cwd,
            result.get("exit_code", 0),
            stdout,
            result.get("stderr", ""),
            "t",
            "t",
            timed_out=result.get("timed_out", False),
            stdout_truncated=result.get("truncated", False),
        )

    return runner


@pytest.mark.parametrize(
    ("line", "exit_code", "outcome"),
    [
        ("*\t{sha}:{ref}\t[new branch]", 0, PushOutcome.PUSHED),
        (" \t{sha}:{ref}\taaaaaaa..bbbbbbb", 0, PushOutcome.PUSHED),
        ("=\t{sha}:{ref}\t[up to date]", 0, PushOutcome.UP_TO_DATE),
        ("!\t{sha}:{ref}\t[rejected] (stale info)", 1, PushOutcome.LEASE_REJECTED),
        (
            "!\t{sha}:{ref}\t[remote rejected] (protected branch hook declined)",
            1,
            PushOutcome.REMOTE_REJECTED,
        ),
        (
            "!\t{sha}:{ref}\t[remote failure] (remote failed to report status)",
            1,
            PushOutcome.UNKNOWN,
        ),
        ("*\t{sha}:{ref}\t[new branch]", 1, PushOutcome.UNKNOWN),
        ("*\t{sha}:refs/heads/other\t[new branch]", 0, PushOutcome.UNKNOWN),
        ("", 128, PushOutcome.UNKNOWN),
        ("garbage", 0, PushOutcome.UNKNOWN),
    ],
)
def test_push_output_is_classified(world, line, exit_code, outcome):
    sha = world.commit("x")
    stdout = "To file:///r\n" + line.format(sha=sha, ref="refs/heads/feature") + "\nDone\n"
    transport = world.transport(runner=_canned_push(world, stdout, exit_code=exit_code))
    result = transport.push(sha=sha, branch="feature", expected_old=None, default_branch="main")
    assert result.outcome is outcome


@pytest.mark.parametrize("result", [{"timed_out": True}, {"truncated": True}, {"raise_": True}])
def test_an_incomplete_push_is_unknown_not_an_error(world, result):
    sha = world.commit("x")
    transport = world.transport(runner=_canned_push(world, **result))
    pushed = transport.push(sha=sha, branch="feature", expected_old=None, default_branch="main")
    assert pushed.outcome is PushOutcome.UNKNOWN


def test_push_detail_is_redacted(world):
    sha = world.commit("x")
    runner = _canned_push(world, "", exit_code=128, stderr="fatal: GH_TOKEN=ghp_abcdefghijklmnop")
    result = world.transport(runner=runner).push(
        sha=sha, branch="feature", expected_old=None, default_branch="main"
    )
    assert result.outcome is PushOutcome.UNKNOWN
    assert "ghp_abcdefghijklmnop" not in result.detail


# -- hardening ----------------------------------------------------------------------------
HOOKS = ("pre-push", "pre-auto-gc", "reference-transaction", "post-checkout", "post-update")


def _sentinel_hook(directory: Path, name: str, sentinel: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    hook = directory / name
    hook.write_text(f'#!/bin/sh\ntouch "{sentinel}-{name}"\ncat >/dev/null\nexit 0\n')
    hook.chmod(0o755)


def test_planted_configuration_environment_and_hooks_neither_redirect_nor_run(world, monkeypatch):
    base = world.commit("base")
    world.publish(base, "refs/heads/main")
    side = world.commit("side", base)
    world.publish(side, "refs/pull/3/head")
    world.drop(side)
    candidate = world.commit("candidate", base)

    decoy = world.tmp / "decoy.git"
    git("init", "-q", "--bare", "-b", "main", str(decoy))
    decoy_url = f"file://{decoy}"
    sentinel = world.tmp / "sentinel"
    planted_hooks = world.tmp / "planted-hooks"
    for name in HOOKS:
        _sentinel_hook(world.shared / ".git" / "hooks", name, sentinel)
        _sentinel_hook(planted_hooks, name, sentinel)
    include = world.tmp / "included.gitconfig"
    include.write_text(f'[url "{decoy_url}"]\n\tpushInsteadOf = {world.url}\n')
    for key, value in (
        ("remote.origin.url", decoy_url),
        ("remote.origin.pushurl", decoy_url),
        (f"url.{decoy_url}.insteadOf", world.url),
        ("core.sshCommand", f"touch {sentinel}-ssh"),
        ("core.hooksPath", str(planted_hooks)),
        ("core.fsmonitor", str(planted_hooks / "pre-push")),
        ("gc.auto", "1"),
        ("include.path", str(include)),
        ("credential.helper", f"!touch {sentinel}-credential"),
    ):
        world.g("config", key, value)
    home = world.tmp / "home"
    (home / ".config" / "git").mkdir(parents=True)
    (home / ".gitconfig").write_text(f'[url "{decoy_url}"]\n\tinsteadOf = {world.url}\n')
    (home / ".config" / "git" / "config").write_text(f"[core]\n\thooksPath = {planted_hooks}\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", f"url.{decoy_url}.insteadOf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", world.url)
    monkeypatch.setenv("GIT_CONFIG_PARAMETERS", f"'core.hooksPath'='{planted_hooks}'")
    monkeypatch.setenv("GIT_DIR", str(decoy))
    monkeypatch.setenv("GIT_SSH_COMMAND", f"touch {sentinel}-ssh-env")
    refs_before = world.local_refs()

    transport = world.transport()
    transport.fetch(["refs/pull/3/head"])
    assert transport.has_commit(side)
    assert transport.read_commit(candidate).parents == (base,)
    assert transport.descends_from(candidate, base)
    pushed = transport.push(
        sha=candidate, branch="feature", expected_old=None, default_branch="main"
    )

    assert pushed.outcome is PushOutcome.PUSHED
    assert world.remote_ref("refs/heads/feature") == candidate
    assert git(f"--git-dir={decoy}", "for-each-ref") == ""
    assert not list(world.tmp.glob("sentinel*"))
    assert world.local_refs() == refs_before
    assert private_leftovers(world) == []


def test_every_request_is_hardened_and_carries_no_credential(world, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", CREDENTIAL)
    g = merge_graph(world, h_message="add the feature")
    world.drop(g.h)
    observed: list[tuple[int, bool, str]] = []

    def runner(request: ExecutionRequest) -> ExecutionResult:
        git_dir = Path(request.env["GIT_DIR"])
        observed.append(
            (
                stat.S_IMODE(git_dir.stat().st_mode),
                (git_dir / "hooks").exists(),
                (git_dir / "config").read_text(),
            )
        )
        return world.runner(request)

    transport = world.transport(runner=runner)
    transport.prove_range(g.b, g.c)  # reads, plus one re-fetch of H
    transport.push(sha=g.c, branch="feature", expected_old=None, default_branch="main")

    assert {"fetch", "push", "cat-file"} <= {
        next(a for a in r.command[1:] if not a.startswith("-") and "=" not in a)
        for r in world.requests
    }
    for mode, has_hooks, config in observed:
        assert mode == 0o700 and not has_hooks
        for line in ("bare = true", "commitGraph = false", "auto = 0", "writeCommitGraph = false"):
            assert line in config
    for request in world.requests:
        command = request.command
        env = request.env or {}
        assert command[0] == "git"
        assert command[1] == "--no-replace-objects"
        for setting in (
            "core.hooksPath=/dev/null",
            "core.fsmonitor=false",
            "core.commitGraph=false",
            "gc.auto=0",
            "maintenance.auto=false",
            "protocol.allow=never",
        ):
            assert setting in command
        assert "origin" not in command
        assert not Path(env["GIT_DIR"]).is_relative_to(world.tmp / "shared")
        assert Path(env["GIT_DIR"]).is_relative_to(world.tmp / "private-tmp")
        assert env["GIT_OBJECT_DIRECTORY"] == str(world.objects.resolve())
        assert env["GIT_CONFIG_NOSYSTEM"] == "1"
        assert env["GIT_CONFIG_GLOBAL"] == os.devnull
        assert env["GIT_NO_REPLACE_OBJECTS"] == "1"
        assert env["GIT_TERMINAL_PROMPT"] == "0"
        assert request.env_allowlist is not None
        assert not any(name.upper().startswith("GIT_") for name in request.env_allowlist)
        flat = " ".join([*command, *env.values()])
        assert CREDENTIAL not in flat
        if request.command[-1] == world.url or world.url in command:
            assert "protocol.file.allow=always" in command
            helpers = [command[i + 1] for i, a in enumerate(command[:-1]) if a == "-c"]
            reset = helpers.index("credential.helper=")
            assert helpers[reset + 1] == f"credential.helper=!{world.gh} auth git-credential"
            assert request.env_allowlist == NETWORK_GIT_ENV_ALLOWLIST
        else:
            assert not any(a.endswith(".allow=always") for a in command)
            assert not any(a.startswith("credential.helper") for a in command)
            assert request.env_allowlist == LOCAL_GIT_ENV_ALLOWLIST
    assert private_leftovers(world) == []


def test_the_credential_comes_from_gh_and_never_reaches_argv(world):
    """Drives git's credential machinery through the transport's own request.

    A ``file://`` remote never asks for a credential, so this is the one test
    that reaches into the transport's private request builder.
    """
    transport = world.transport()
    with transport._git_dir("sha1") as git_dir:
        request = transport._request(
            git_dir,
            ["credential", "fill"],
            gh_path=str(world.gh),
            stdin=b"protocol=https\nhost=github.com\npath=octo/repo.git\n\n",
        )
        result = execute(request)
    assert result.exit_code == 0, result.stderr
    assert f"password={CREDENTIAL}" in result.stdout
    assert world.gh_log.read_text().split() == ["auth", "git-credential", "get"]
    assert CREDENTIAL not in " ".join(request.command)
    assert not git_dir.exists()


def test_gh_paths_with_spaces_are_quoted_for_the_credential_helper(tmp_path, monkeypatch):
    private = tmp_path / "private-tmp"
    private.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(private))
    world = make_world(tmp_path)
    spaced = tmp_path / "a dir" / "gh tool"
    spaced.parent.mkdir()
    shutil.copy(world.gh, spaced)
    transport = world.transport(gh_command=str(spaced))
    with transport._git_dir("sha1") as git_dir:
        request = transport._request(
            git_dir,
            ["credential", "fill"],
            gh_path=str(spaced),
            stdin=b"protocol=https\nhost=github.com\n\n",
        )
        result = execute(request)
    assert f"password={CREDENTIAL}" in result.stdout


def test_the_private_directory_is_removed_when_the_operation_fails(world):
    def broken(request: ExecutionRequest) -> ExecutionResult:
        assert Path(request.env["GIT_DIR"]).is_dir()
        raise ExecutionError("spawn failed")

    transport = world.transport(runner=broken)
    with pytest.raises(GitTransportError, match="spawn failed"):
        transport.has_commit("a" * 40)
    with pytest.raises(GitTransportError):
        transport.fetch(["refs/heads/main"])
    assert private_leftovers(world) == []


def test_constructor_refuses_a_nonsensical_bound_or_format(world):
    with pytest.raises(GitTransportError):
        world.transport(max_range_commits=0)
    with pytest.raises(GitTransportError):
        world.transport(object_format="md5")
