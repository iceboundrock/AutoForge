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

import dataclasses
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
from autoforge.executor_duplex import (
    DEFAULT_MAX_RECORD_BYTES,
    Chunk,
    DuplexChild,
    DuplexRequest,
    Eof,
    Overflow,
    StdoutMode,
    Timeout,
    start_duplex,
)
from autoforge.git_transport import (
    LOCAL_GIT_ENV_ALLOWLIST,
    LOCAL_TIMEOUT_SECONDS,
    MAX_RANGE_COMMITS,
    MAX_READER_BYTES,
    MAX_READER_CHUNKS,
    MAX_READER_PROCESSES,
    NETWORK_GIT_ENV_ALLOWLIST,
    READER_HEADER_BYTES,
    CommitRecord,
    GitRemote,
    GitTransport,
    PushOutcome,
    RangeProof,
    published_range_problem,
)
from autoforge.locking import SharedGitLock

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
        kwargs.setdefault("git_lock", lambda op: SharedGitLock(self.objects.parent, operation=op))
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
    assert record == CommitRecord(
        sha=merge, parents=(p1, p2), message="subject\n\nbody\n", tree=world.tree()
    )
    root = world.transport().read_commit(p1)
    assert root.parents == ()


def test_read_local_commit_reads_the_tree_from_the_store_and_never_fetches(world):
    """#172: candidate validation reads its candidate's tree from the shared
    store's authenticated bytes; a commit that is not there is None, never fetched."""
    tree = world.tree({"app.txt": "pristine\n"})
    sha = world.commit("candidate", tree=tree)
    record = world.transport().read_local_commit(sha)
    assert record is not None and record.tree == tree and record.sha == sha
    world.publish(sha, "refs/heads/side")
    world.drop(sha)
    assert world.transport().read_local_commit(sha) is None
    assert not [r for r in world.requests if "fetch" in r.command]


def test_a_commit_whose_tree_is_malformed_fails_closed(world):
    content = (
        "tree " + "z" * 40 + "\nauthor t <t@e> 0 +0000\ncommitter t <t@e> 0 +0000\n\nbad\n"
    ).encode()
    sha = _literal_commit(world, content)
    with pytest.raises(GitTransportError, match="malformed tree"):
        world.transport().read_local_commit(sha)


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


@pytest.mark.parametrize("misplaced", [False, True], ids=["in-place", "misplaced"])
def test_a_header_of_many_short_lines_is_parsed_as_git_reads_it(world, misplaced):
    """R3-F1 of PR #586: the header is searched whole past its tree and parent
    lines, not split into lines, and still refuses a parent line out of place
    however far below them it is."""
    parent = world.commit("parent")
    tail = f"\nparent {parent}" if misplaced else ""
    content = (
        f"tree {world.tree()}\nparent {parent}\nauthor t <t@e> 0 +0000\ncommitter t <t@e> 0 +0000\n"
    ).encode()
    content += b"x v\n" * 200_000 + f"x v{tail}\n\nsubject\n".encode()
    sha = _literal_commit(world, content)
    if misplaced:
        with pytest.raises(GitTransportError, match="out of place"):
            world.transport().read_commit(sha)
    else:
        record = world.transport().read_commit(sha)
        assert (record.parents, record.message) == ((parent,), "subject\n")


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


# -- many local reads through one git process (#541) ----------------------------------------
@dataclass
class Sessions:
    """A :class:`LocalCommitReader` process starter that keeps each request and
    child, and the SHAs each child was asked for, in order: the reader's
    shared process and its one-shot reads alike."""

    requests: list[DuplexRequest] = field(default_factory=list)
    children: list[DuplexChild] = field(default_factory=list)
    asked: list[list[str]] = field(default_factory=list)

    def __call__(self, request: DuplexRequest) -> DuplexChild:
        self.requests.append(request)
        child = start_duplex(request)
        self.children.append(child)
        self.asked.append([])
        return _Asked(child, self.asked[-1])  # type: ignore[return-value]


class _Asked:
    def __init__(self, child: DuplexChild, asked: list[str]) -> None:
        self._child, self._asked = child, asked

    def send_line(self, data: bytes) -> None:
        self._asked.append(data.decode())
        self._child.send_line(data)

    def __getattr__(self, name: str) -> object:
        return getattr(self._child, name)


def test_a_local_commit_reader_answers_as_single_reads_through_one_process(world):
    """Every read answers as :meth:`GitTransport.read_local_commit` does (a
    merge, a root, messages with CRs and blank lines, a commit the store does
    not hold although a replacement names it) through one git process,
    started on the first read and ended with the ``with`` block: no single
    read, no fetch, and no private directory left."""
    root = world.commit("root")
    side = world.commit("side\r\n\r\nends in CR\r", root)
    merge = world.g(
        "commit-tree", world.tree(), "-p", root, "-p", side, "-m", "subject", "-m", "body\r"
    )
    gone = world.commit("dropped", merge)
    world.drop(gone)
    world.g("update-ref", f"refs/replace/{gone}", root)
    shas = [merge, side, root, gone, merge]
    sessions = Sessions()
    transport = world.transport(duplex=sessions)
    with transport.local_commit_reader() as reader:
        assert sessions.requests == [] and private_leftovers(world) == []
        got = [reader.read_local_commit(sha) for sha in shas]
        assert len(private_leftovers(world)) == 1  # the process's directory, while it runs
    assert world.requests == []
    (child,) = sessions.children
    assert child.exited() and child.finish().exit_code == 0
    assert private_leftovers(world) == []
    assert got == [transport.read_local_commit(sha) for sha in shas]
    assert got[0] is not None and got[0].parents == (root, side)
    assert got[1] is not None and "\r" in got[1].message
    assert got[3] is None


def test_a_reader_authenticates_a_commit_a_single_read_cannot_capture(world):
    """The reader hashes the bytes git wrote, not a text capture, so a commit
    that is not valid UTF-8 is read as its own bytes, where the single read
    refuses it for want of them."""
    tree = world.tree()
    content = (
        f"tree {tree}\nauthor t <t@example.com> 0 +0000\n"
        "committer t <t@example.com> 0 +0000\nencoding ISO-8859-1\n\n"
    ).encode() + b"caf\xe9\n"
    sha = _literal_commit(world, content)
    with world.transport().local_commit_reader() as reader:
        record = reader.read_local_commit(sha)
    assert record is not None and (record.sha, record.tree, record.parents) == (sha, tree, ())
    with pytest.raises(GitTransportError, match="not valid UTF-8"):
        world.transport().read_local_commit(sha)


def test_a_reader_refuses_what_a_single_read_refuses_and_its_process_reads_on(world):
    """A rewritten commit, an object that is no commit (a blob holding CRs, LFs
    and a NUL, a tree) and a malformed commit raise as a single read does, and
    the same process serves the next read; a malformed SHA is refused before
    anything is read."""
    good = world.commit("good")
    rewritten = world.commit("original message")
    write_loose(
        world.objects, rewritten, "commit", world.raw(rewritten).replace(b"original", b"forged")
    )
    blob = world.g("hash-object", "-w", "--stdin", input=b"data\r\n\x00\n\n")
    tree = world.tree({"a.txt": "a\n"})
    malformed = _literal_commit(world, b"author t <t@e> 0 +0000\n\nno tree\n")
    sessions = Sessions()
    with world.transport(duplex=sessions).local_commit_reader() as reader:
        for sha, reason in (
            (rewritten, "do not hash"),
            (blob, "is a blob"),
            (tree, "is a tree"),
            (malformed, "tree line"),
        ):
            with pytest.raises(GitTransportError, match=reason):
                reader.read_local_commit(sha)
            record = reader.read_local_commit(good)
            assert record is not None and record.message == "good\n"
        with pytest.raises(GitTransportError, match="not a full"):
            reader.read_local_commit("A" * 40)
    assert len(sessions.children) == 1 and world.requests == []


@dataclass
class Interrupting(Sessions):
    """A starter whose first process, asked for ``sha``, reports
    ``outcome(sha)`` in place of the reply, as the duplex handle reports a
    queue that overflowed, an exit or a time limit, or as a process that
    writes other than it was asked would; every later process is left as it
    is."""

    sha: str = ""
    outcome: Callable[[str], object] = lambda sha: None

    def __call__(self, request: DuplexRequest) -> DuplexChild:
        child = super().__call__(request)
        if len(self.children) > 1:
            return child
        return _Interrupted(child, self.sha.encode(), self.outcome(self.sha))  # type: ignore[return-value]


class _Interrupted:
    def __init__(self, child: DuplexChild, name: bytes, outcome: object) -> None:
        self._child, self._name, self._outcome, self._armed = child, name, outcome, False

    def send_line(self, data: bytes) -> None:
        self._armed = data == self._name
        self._child.send_line(data)

    def read_chunk(self, timeout: float | None = None) -> object:
        if self._armed:
            self._armed = False
            return self._outcome
        return self._child.read_chunk(timeout)

    def __getattr__(self, name: str) -> object:
        return getattr(self._child, name)


@dataclass
class Rechunking(Sessions):
    """A starter whose processes hand their stdout on ``size`` bytes at a
    time, so a reply's header, body and closing LF each span chunks."""

    size: int = 1

    def __call__(self, request: DuplexRequest) -> DuplexChild:
        return _Rechunked(super().__call__(request), self.size)  # type: ignore[return-value]


class _Rechunked:
    def __init__(self, child: DuplexChild, size: int) -> None:
        self._child, self._size, self._held = child, size, b""

    def read_chunk(self, timeout: float | None = None) -> object:
        if not self._held:
            item = self._child.read_chunk(timeout)
            if not isinstance(item, Chunk):
                return item
            self._held = item.data
        piece, self._held = self._held[: self._size], self._held[self._size :]
        return Chunk(piece)

    def __getattr__(self, name: str) -> object:
        return getattr(self._child, name)


@pytest.mark.parametrize("size", [1, 7, 41])
def test_a_reader_takes_each_reply_by_its_declared_size_wherever_its_chunks_end(world, size):
    """R2-F1 of PR #586: the reader's stdout is not framed into lines; each
    reply is its header line and then the bytes it declares, read across
    however many chunks the pipe gives them in, so a reply whose header, body
    and closing LF are split anywhere, a commit of many short lines and CRs
    included, answers as a single read does, through the one process."""
    root = world.commit("root")
    lines = world.commit("subject\n\n" + "ab\r\n" * 300, root)
    tip = world.commit("tip\r", lines)
    shas = [tip, lines, root, "f" * 40, tip]
    sessions = Rechunking(size=size)
    with world.transport(duplex=sessions).local_commit_reader() as reader:
        got = [reader.read_local_commit(sha) for sha in shas]
    assert got == [world.transport().read_local_commit(sha) for sha in shas]
    assert got[3] is None and got[1] is not None and got[1].message.count("\r\n") == 300
    assert len(sessions.children) == 1 and len(world.requests) == len(shas)


def test_a_reader_reads_a_line_past_the_duplex_default_record_bound_in_its_one_process(world):
    """R1-F1 of PR #586: a commit whose message holds a line longer than the
    duplex handle's default record bound (4 MiB) cost the reader its process,
    and every read after it a process of its own. The reader's stdout is no
    longer framed into lines, so the commit is read whole by its declared
    size, re-hashed, by the one process, which serves the reads after it
    too: no single read."""
    root = world.commit("root")
    message = world.tmp / "long-message"
    message.write_bytes(b"subject\n\n" + b"x" * (DEFAULT_MAX_RECORD_BYTES + 1) + b"\r\n")
    long = world.g("commit-tree", world.tree(), "-p", root, "-F", str(message))
    tip = world.commit("tip", long)
    sessions = Sessions()
    with world.transport(duplex=sessions).local_commit_reader() as reader:
        got = [reader.read_local_commit(sha) for sha in (tip, long, root)]
    assert world.requests == [] and len(sessions.children) == 1
    assert [r.parents for r in got if r is not None] == [(long,), (root,), ()]
    assert got[1] == world.transport().read_local_commit(long)
    assert got[1] is not None and len(got[1].message) > DEFAULT_MAX_RECORD_BYTES


def test_a_reader_takes_no_more_than_its_bytes_across_its_reads_then_reads_no_more(world):
    """R3-F1 of PR #586. Each reply's bytes are counted from its header before
    a byte of it is taken, across every read of the reader: a commit past
    what is left is never read, the process is killed at once rather than
    drained, and that read and every later one (a small commit, a missing
    one) is refused, with no single read. What was read before stands."""
    small = world.commit("small")
    large = world.commit("large " + "x" * 500, small)
    after = world.commit("after", large)
    budget = len(world.raw(after)) + len(world.raw(large)) - 1
    sessions = Sessions()
    with world.transport(duplex=sessions).local_commit_reader(max_bytes=budget) as reader:
        first = reader.read_local_commit(after)
        assert first is not None and first.parents == (large,)
        left = reader.bytes_left
        assert left == budget - len(world.raw(after))
        with pytest.raises(GitTransportError, match="a local commit reader may take"):
            reader.read_local_commit(large)
        (child,) = sessions.children
        assert child.exited() and child.finish().exit_code != 0
        for sha in (small, "f" * 40):
            with pytest.raises(GitTransportError, match="spent"):
                reader.read_local_commit(sha)
    assert reader.bytes_left == left and reader.processes_left == MAX_READER_PROCESSES - 1
    assert world.requests == [] and len(sessions.requests) == 1
    assert private_leftovers(world) == []


@pytest.mark.parametrize("short", [0, 1], ids=["all-that-is-left", "one-byte-past"])
def test_a_one_shot_read_counts_its_bytes_from_its_header_and_is_killed_past_them(world, short):
    """R4-F1 of PR #586. A one-shot read counts against the reader's bytes as
    its shared process does, from the reply's header before a byte of the
    object is taken: a commit of all that is left is taken, and one a byte
    past it is refused at its header, its process stopped by the reader
    rather than left to write the commit out, and the reader is spent. The
    shared process failed at its first reply here, so every later read is a
    one-shot read: a ``git cat-file --batch`` process of its own, never a
    single read."""
    root = world.commit("root")
    fits = world.commit("fits", root)
    past = world.commit("past " + "x" * 2000, fits)
    budget = len(world.raw(fits)) + len(world.raw(past)) - short
    sessions = Interrupting(sha=fits, outcome=lambda sha: Eof())
    with world.transport(duplex=sessions).local_commit_reader(max_bytes=budget) as reader:
        record = reader.read_local_commit(fits)
        assert record is not None and record.parents == (root,)
        left = reader.bytes_left
        assert left == len(world.raw(past)) - short
        if short:
            with pytest.raises(GitTransportError, match="a local commit reader may take"):
                reader.read_local_commit(past)
            with pytest.raises(GitTransportError, match="spent"):
                reader.read_local_commit(root)
        else:
            record = reader.read_local_commit(past)
            assert record is not None and record.parents == (fits,)
    assert sessions.asked == [[fits], [fits], [past]]
    assert reader.bytes_left == (left if short else 0)
    assert reader.processes_left == MAX_READER_PROCESSES - 3
    answered, last = (child.finish() for child in sessions.children[1:])
    assert answered.exit_code == 0 and not answered.stopped
    assert last.stopped is bool(short) and (last.exit_code == 0) is not bool(short)
    assert world.requests == [] and private_leftovers(world) == []


def test_a_reader_starts_no_more_than_its_processes_one_shot_reads_included(world):
    """R3-F1 of PR #586. Every git process the reader starts counts, its
    shared one and each one-shot read's: past a shared process that failed at
    its first reply, every read is a one-shot read until the reader has
    started ``max_processes``, and the next read is refused, as is every one
    after it, with no process started for either."""
    shas = [world.commit("root")]
    for n in range(4):
        shas.insert(0, world.commit(f"{n}", shas[0]))
    sessions = Interrupting(sha=shas[0], outcome=lambda sha: Eof())
    with world.transport(duplex=sessions).local_commit_reader(max_processes=3) as reader:
        read = [reader.read_local_commit(sha) for sha in shas[:2]]
        assert [r.parents for r in read if r is not None] == [(shas[1],), (shas[2],)]
        assert reader.processes_left == 0
        with pytest.raises(GitTransportError, match="3 git processes"):
            reader.read_local_commit(shas[2])
        with pytest.raises(GitTransportError, match="spent"):
            reader.read_local_commit(shas[3])
    assert sessions.asked == [[shas[0]], [shas[0]], [shas[1]]]
    assert world.requests == []
    assert all(child.exited() for child in sessions.children)
    assert private_leftovers(world) == []


def test_a_reader_takes_a_commit_of_all_its_bytes_that_a_single_read_cannot_capture(world):
    """R3-F2, R4-F1 of PR #586. What each of the reader's processes takes is
    the object's bytes, up to :data:`MAX_READER_BYTES`, its shared process's
    and a one-shot read's alike; what a single read captures is its stdout,
    header and LFs included, up to the same bound. So a commit of exactly
    that many bytes is read whole by the reader, either way, which then has
    no byte left, and is refused by a single read as cut at its capture."""
    head = (f"tree {world.tree()}\nauthor t <t@e> 0 +0000\ncommitter t <t@e> 0 +0000\n\n").encode()
    content = head + b"x" * (MAX_READER_BYTES - len(head) - 1) + b"\n"
    assert len(content) == MAX_READER_BYTES
    sha = _literal_commit(world, content)
    for sessions, asked in (
        (Sessions(), [[sha]]),
        (Interrupting(sha=sha, outcome=lambda sha: Eof()), [[sha], [sha]]),
    ):
        with world.transport(duplex=sessions).local_commit_reader() as reader:
            record = reader.read_local_commit(sha)
            assert record is not None and record.parents == () and reader.bytes_left == 0
        assert sessions.asked == asked
    with pytest.raises(GitTransportError, match="truncated at the capture bound"):
        world.transport().read_local_commit(sha)


def test_a_reader_is_given_no_more_bytes_than_a_reply_may_hold(world):
    for max_bytes, max_processes in ((MAX_READER_BYTES + 1, 1), (-1, 1), (0, -1)):
        with pytest.raises(GitTransportError, match="must be"):
            world.transport().local_commit_reader(max_bytes=max_bytes, max_processes=max_processes)


@pytest.mark.parametrize(
    "outcome, restarts",
    [
        (lambda sha: Overflow(), True),
        (lambda sha: Eof(), False),
        (lambda sha: Timeout(deadline_exceeded=True), False),
        (lambda sha: Chunk(b"x" * (READER_HEADER_BYTES + 1)), False),
        (lambda sha: Chunk(f"{'0' * len(sha)} commit 1\n".encode()), False),
        (lambda sha: Chunk(f"{sha} missing\nunasked".encode()), False),
    ],
    ids=["overflow", "eof", "timeout", "overlong-header", "other-object", "more-than-asked"],
)
def test_only_an_overflowed_reply_leaves_later_reads_to_a_new_process(world, outcome, restarts):
    """R1-F1 of PR #586. More of a reply waiting than the queue holds is a
    reply git gave right: that read is a one-shot read and the next read
    starts a new shared process, which serves the read after it too. The end
    of stdout, a time limit, a header longer than any git writes, a reply
    for another object or more than the reply asked for is the process
    failing: that read and every later one is a one-shot read. Each answer
    is a single read's either way, no single read is made (R4-F1), and the
    reader has started three processes of its budget, or four (R3-F1)."""
    one = world.commit("one")
    two = world.commit("two", one)
    three = world.commit("three", two)
    shas = (three, two, one, three)
    sessions = Interrupting(sha=two, outcome=outcome)
    with world.transport(duplex=sessions).local_commit_reader() as reader:
        read = [reader.read_local_commit(sha) for sha in shas]
    assert read == [world.transport().read_local_commit(sha) for sha in shas]
    assert [r.parents for r in read if r is not None] == [(two,), (one,), (), (two,)]
    assert len(world.requests) == len(shas)  # the single reads compared with
    if restarts:
        assert sessions.asked == [[three, two], [two], [one, three]]
    else:
        assert sessions.asked == [[three, two], [two], [one], [three]]
    assert reader.processes_left == MAX_READER_PROCESSES - len(sessions.children)
    assert all(child.exited() for child in sessions.children)
    assert private_leftovers(world) == []


def test_a_reader_whose_process_cannot_start_reads_each_commit_by_one_of_its_own(world):
    """A shared process that cannot start leaves each read to a one-shot
    read, within the reader's processes; a one-shot read that cannot start
    either fails that read, which no single read takes over (R4-F1 of PR
    #586), and a read past the processes is refused."""
    one = world.commit("one")
    two = world.commit("two", one)
    refusals: list[str] = []
    sessions = Sessions()

    def refused(request: DuplexRequest) -> DuplexChild:
        assert Path(request.env["GIT_DIR"]).is_dir()
        refusals.append(request.env["GIT_DIR"])
        raise ExecutionError("spawn failed")

    def first_refused(request: DuplexRequest) -> DuplexChild:
        return sessions(request) if refusals else refused(request)

    with world.transport(duplex=first_refused).local_commit_reader() as reader:
        read = [reader.read_local_commit(sha) for sha in (two, one)]
    assert [r.parents for r in read if r is not None] == [(one,), ()]
    assert len(refusals) == 1 and sessions.asked == [[two], [one]]
    assert reader.processes_left == MAX_READER_PROCESSES - 3
    with world.transport(duplex=refused).local_commit_reader() as reader:
        for sha in (two, one, two):
            with pytest.raises(GitTransportError, match="spawn failed"):
                reader.read_local_commit(sha)
        with pytest.raises(GitTransportError, match="4 git processes"):
            reader.read_local_commit(one)
    assert len(refusals) == 1 + MAX_READER_PROCESSES
    assert world.requests == [] and private_leftovers(world) == []


def test_a_reader_starts_nothing_unread_ends_its_process_on_an_error_and_refuses_once_closed(
    world,
):
    sha = world.commit("one")
    sessions = Sessions()
    reader = world.transport(duplex=sessions).local_commit_reader()
    with reader:
        pass
    assert sessions.requests == [] and private_leftovers(world) == []
    with pytest.raises(GitTransportError, match="closed"):
        reader.read_local_commit(sha)
    reader.close()
    with pytest.raises(RuntimeError, match="interrupted"):
        with world.transport(duplex=sessions).local_commit_reader() as reader:
            assert reader.read_local_commit(sha) is not None
            raise RuntimeError("interrupted")
    (child,) = sessions.children
    assert child.exited() and private_leftovers(world) == []


def test_the_readers_process_is_hardened_as_a_single_read_is(world):
    """The reader's git process gets what a single read gets: the same argv
    (``cat-file --batch`` behind the same switches), allow-list and
    environment, a private 0700 directory without hooks and with the same
    configuration, no sandbox and no orphan containment, as for the
    controller's other git plumbing, and one local read's time limit for the
    whole process. Its stdout is read unframed, every byte kept."""
    sha = world.commit("one")
    observed: list[tuple[int, bool, str]] = []
    sessions = Sessions()

    def inspecting(request: DuplexRequest) -> DuplexChild:
        git_dir = Path(request.env["GIT_DIR"])
        observed.append(
            (
                stat.S_IMODE(git_dir.stat().st_mode),
                (git_dir / "hooks").exists(),
                (git_dir / "config").read_text(),
            )
        )
        return sessions(request)

    transport = world.transport(duplex=inspecting)
    with transport.local_commit_reader() as reader:
        reader.read_local_commit(sha)
    transport.read_local_commit(sha)
    (single,) = world.requests
    (session,) = sessions.requests
    assert session.command == single.command
    assert session.command[-2:] == ["cat-file", "--batch"]
    assert session.env_allowlist == single.env_allowlist == LOCAL_GIT_ENV_ALLOWLIST

    def without_dir(env: dict[str, str] | None) -> dict[str, str]:
        return {k: v for k, v in (env or {}).items() if k != "GIT_DIR"}

    assert without_dir(session.env) == without_dir(single.env)
    assert session.cwd == session.env["GIT_DIR"]
    assert session.max_runtime_seconds == single.timeout_seconds == LOCAL_TIMEOUT_SECONDS
    assert session.idle_timeout_seconds is None
    assert session.stdout_mode is StdoutMode.CHUNKS  # R2-F1 of PR #586
    assert session.max_pending_records == MAX_READER_CHUNKS
    assert session.max_pending_bytes == MAX_READER_BYTES + READER_HEADER_BYTES
    assert session.sandbox is None and not session.contain_orphans
    ((mode, has_hooks, config),) = observed
    assert mode == 0o700 and not has_hooks
    for line in ("bare = true", "commitGraph = false", "auto = 0", "writeCommitGraph = false"):
        assert line in config


@pytest.mark.parametrize("mechanism", ["replace", "graft", "shallow", "commit-graph"])
def test_a_reader_reads_each_commit_as_its_own_bytes_whatever_is_planted(world, mechanism):
    """No substitution mechanism of D7.5 reaches the reader's process: the
    planted commit is read with its own parents, as a single read reads it."""
    base = world.commit("base")
    below = world.commit("unrelated root")
    tip = world.commit("unrelated", below)
    if mechanism == "shallow":
        _plant(world, mechanism, tip, ())
    else:
        _plant(world, mechanism, below, (base,))
    with world.transport().local_commit_reader() as reader:
        read = [reader.read_local_commit(sha) for sha in (tip, below)]
    assert [r.parents for r in read if r is not None] == [(below,), ()]
    assert world.requests == []


def test_a_reader_never_tries_to_fetch_a_commit_the_store_lacks(world):
    """As R4-F1 of PR #536 holds the single read: the shared repository names
    a promisor remote, and the reader's process, traced, reads a commit the
    store lacks as missing and tries no fetch, since its private directory
    names no remote."""
    sha = world.commit("present")
    for key, value in (
        ("remote.promised.url", str(world.tmp / "nowhere")),
        ("remote.promised.promisor", "true"),
        ("extensions.partialClone", "promised"),
    ):
        world.g("config", key, value)
    trace = world.tmp / "git.trace"

    def traced(request: DuplexRequest) -> DuplexChild:
        env = {**(request.env or {}), "GIT_TRACE": str(trace)}
        return start_duplex(dataclasses.replace(request, env=env))

    with world.transport(duplex=traced).local_commit_reader() as reader:
        assert reader.read_local_commit("e" * 40) is None
        assert reader.read_local_commit(sha) is not None
    logged = trace.read_text()
    assert "cat-file --batch" in logged and "fetch" not in logged


def test_a_reader_reads_sha256_commits_in_their_own_format(tmp_path, monkeypatch):
    private = tmp_path / "private-tmp"
    private.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(private))
    world = make_world(tmp_path, "sha256")
    base = world.commit("base")
    tip = world.commit("tip", base)
    configs: list[str] = []

    def inspecting(request: DuplexRequest) -> DuplexChild:
        configs.append((Path(request.env["GIT_DIR"]) / "config").read_text())
        return start_duplex(request)

    with world.transport(duplex=inspecting).local_commit_reader() as reader:
        record = reader.read_local_commit(tip)
        assert record is not None and record.parents == (base,)
        assert reader.read_local_commit(base) is not None
    assert len(configs) == 1 and "objectFormat = sha256" in configs[0]
    assert world.requests == []


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
    below = world.commit("unrelated root")
    unrelated = world.commit("unrelated", below)
    # Planted below the candidate, on the path a walk takes (#348): git 2.47's
    # ``merge-base`` reads the commits named on its command line from their
    # objects and consults the commit-graph only for the commits it walks to,
    # so a forged graph entry for the candidate itself fools 2.55 but not 2.47.
    _plant(world, mechanism, below, (base,))
    # The attack: the shared repository's own git now says it descends.
    assert git_ok("-C", str(world.shared), "merge-base", "--is-ancestor", base, unrelated)

    transport = world.transport()
    assert not transport.descends_from(unrelated, base)
    assert world.reads_of(below) != []  # the walk read the planted commit's own bytes
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
