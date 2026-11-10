"""An exported candidate artifact and its strict, bounded import (ADR 0013 D5, D6; #269).

Real git on real repositories in ``tmp_path``, no runtime: ``store`` stands in
for the shared object store, and ``agent`` for a container's private
repository, seeded from it. An artifact is what an exporter would leave:
``pack-objects`` of the agent's commits above the seed, and a manifest. Every
refusal is an :class:`ObjectImportError` that leaves the store as it was.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tracemalloc
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

import autoforge.candidate_artifact as candidate_artifact
from autoforge.agent_worktree import ObjectImportError
from autoforge.candidate_artifact import (
    ARTIFACT_MANIFEST,
    ARTIFACT_PACK,
    MAX_ARTIFACT_PACK_BYTES,
    MAX_MANIFEST_BYTES,
    ArtifactLimits,
    CandidateManifest,
    bound_revision_problem,
    import_artifact,
    manifest_disagreement,
    parse_manifest,
)
from autoforge.errors import GitLockTimeout, GitTransportError
from autoforge.executor import ExecutionRequest, execute
from autoforge.git_input import LaunchGitInput
from autoforge.git_transport import GitRemote, GitTransport
from tests.conftest import GIT_IDENT, isolated_git, isolated_git_run


def git(repo: Path, *args: str, input: bytes | None = None) -> str:
    return isolated_git("-C", str(repo), *args, input=input).decode().strip()


def commit(repo: Path, name: str, content: str = "", message: str = "") -> str:
    (repo / name).write_text(content or name)
    git(repo, "add", name)
    git(repo, *GIT_IDENT, "commit", "-q", "-m", message or f"Add {name} (#269)")
    return git(repo, "rev-parse", "HEAD")


@dataclass
class Repos:
    store: Path  # the shared store's repository; its common dir is store/.git
    agent: Path  # the container's private repository
    seed: str

    @property
    def common(self) -> Path:
        return self.store / ".git"

    def packs(self) -> set[str]:
        return set(os.listdir(self.common / "objects" / "pack"))

    def refs(self) -> str:
        return git(self.store, "for-each-ref")

    def has(self, sha: str) -> bool:
        res = isolated_git_run(f"--git-dir={self.common}", "cat-file", "-e", sha)
        return res.returncode == 0


def make_repos(tmp_path: Path) -> Repos:
    """A store with one seed commit, and the agent's repository seeded from it."""
    store = tmp_path / "store"
    store.mkdir()
    git(store, "init", "-q", "-b", "main")
    seed = commit(store, "README.md", "seed")
    # The container's repository, seeded from the store: a copy, with no
    # transport between them (#292 builds the real seed).
    agent = tmp_path / "agent"
    shutil.copytree(store, agent, symlinks=True)
    git(agent, "checkout", "-q", "--detach", seed)
    return Repos(store, agent, seed)


@pytest.fixture
def repos(tmp_path: Path) -> Repos:
    return make_repos(tmp_path)


def publish_to_store(repos: Repos, sha: str) -> None:
    """Put the agent's ``sha`` and what it reaches into the store, as a push would."""
    pack = isolated_git(
        "-C", str(repos.agent), "pack-objects", "--revs", "--stdout", "-q",
        input=f"{sha}\n".encode(),
    )  # fmt: skip
    isolated_git(f"--git-dir={repos.common}", "unpack-objects", "-q", input=pack)


def _input(repos: Repos, **changes) -> LaunchGitInput:
    fields = {
        "phase": "ANALYZE_EXECUTE",
        "run": "run-269",
        "issue": 269,
        "launch": "ANALYZE_EXECUTE-1",
        "repository": "owner/repo",
        "object_format": "sha1",
        "seed": repos.seed,
        "lower": repos.seed,
    }
    fields.update(changes)
    return LaunchGitInput(**fields)


def pack_of(repos: Repos, lines: list[str]) -> bytes:
    """``pack-objects --revs`` in the agent's repository: what an exporter would pack."""
    return isolated_git(
        "-C", str(repos.agent), "pack-objects", "--revs", "--stdout", "-q",
        input=("\n".join(lines) + "\n").encode(),
    )  # fmt: skip


def pack_objects(repos: Repos, ids: list[str]) -> bytes:
    """A pack of exactly ``ids``, from the agent's repository."""
    return isolated_git(
        "-C", str(repos.agent), "pack-objects", "--stdout", "-q",
        input=("\n".join(ids) + "\n").encode(),
    )  # fmt: skip


def count_of(pack: bytes) -> int:
    return int.from_bytes(pack[8:12], "big")


def write_artifact(
    where: Path,
    git_input: LaunchGitInput,
    candidate: str,
    pack: bytes,
    **manifest_changes,
) -> Path:
    where.mkdir(parents=True, exist_ok=True)
    manifest = CandidateManifest.for_input(
        git_input,
        candidate=candidate,
        pack_bytes=len(pack),
        pack_objects=count_of(pack),
        pack_sha256=hashlib.sha256(pack).hexdigest(),
    )
    manifest = replace(manifest, **manifest_changes)
    (where / ARTIFACT_PACK).write_bytes(pack)
    (where / ARTIFACT_MANIFEST).write_bytes(manifest.render())
    return where


def transport(repos: Repos) -> GitTransport:
    return GitTransport(
        object_directory=repos.common / "objects",
        remote=GitRemote("file:///nonexistent"),
        in_base_history=lambda base, sha: False,
        git_lock=lambda operation: contextlib.nullcontext(),
    )


class Lock:
    """L1 as a recording context manager; ``fail`` makes it time out."""

    def __init__(self, fail: BaseException | None = None) -> None:
        self.held = False
        self.taken: list[str] = []
        self.fail = fail

    @contextlib.contextmanager
    def __call__(self, operation: str):
        if self.fail is not None:
            raise self.fail
        self.taken.append(operation)
        self.held = True
        try:
            yield
        finally:
            self.held = False


class Runner:
    """The executor, recording each git process and whether L1 was held around it."""

    def __init__(self, lock: Lock) -> None:
        self.lock = lock
        self.calls: list[tuple[list[str], bool]] = []
        self.after = None  # called with each request once it ran

    def __call__(self, request: ExecutionRequest):
        args = request.command[request.command.index("--git-dir") + 2 :]
        self.calls.append((args, self.lock.held))
        result = execute(request)
        if self.after is not None:
            self.after(request, result)
        return result

    def writes(self) -> list[tuple[list[str], bool]]:
        return [(args, held) for args, held in self.calls if "--stdin" in args]


def run_import(repos: Repos, directory: Path, git_input: LaunchGitInput, **kwargs):
    lock = kwargs.pop("lock", None) or Lock()
    runner = kwargs.pop("runner", None) or Runner(lock)
    read_commit = kwargs.pop("read_commit", None) or transport(repos).read_local_commit
    return import_artifact(
        repos.common, directory, git_input, runner, read_commit=read_commit, git_lock=lock,
        **kwargs,
    )  # fmt: skip


def make_work(repos: Repos) -> tuple[str, bytes]:
    """Two commits of the agent above the seed, and their pack."""
    commit(repos.agent, "feature.py", "print('feature')")
    head = commit(repos.agent, "tests.py", "assert True")
    return head, pack_of(repos, [head, f"^{repos.seed}"])


@pytest.fixture
def work(repos: Repos) -> tuple[str, bytes]:
    return make_work(repos)


# -- a valid artifact -------------------------------------------------------------------


def test_a_valid_artifact_is_imported_strictly_under_l1_and_rehashed(repos, work, tmp_path):
    head, pack = work
    git_input = _input(repos)
    directory = write_artifact(tmp_path / "out", git_input, head, pack)
    before_refs, before_packs = repos.refs(), repos.packs()
    lock = Lock()
    runner = Runner(lock)
    read: list[str] = []
    reader = transport(repos)

    def read_commit(sha: str):
        read.append(sha)
        return reader.read_local_commit(sha)

    imported = run_import(
        repos, directory, git_input, lock=lock, runner=runner, read_commit=read_commit
    )
    assert imported.candidate == head and imported.published
    assert imported.objects == count_of(pack)
    assert repos.has(head) and git(repos.store, "rev-parse", f"{head}~2") == repos.seed
    # D7.5: the candidate was read back from the shared store as its own bytes.
    assert read == [head]
    # L1 was taken once, around the one write and nothing else.
    assert lock.taken == ["import an exported candidate into the shared store"]
    assert runner.writes() == [
        (["-c", "gc.auto=0", "-c", "maintenance.auto=false", "index-pack", "--stdin", "--strict"],
         True)
    ]  # fmt: skip
    assert [held for _, held in runner.calls].count(True) == 1
    # Objects only: no ref was created or moved anywhere.
    assert repos.refs() == before_refs and len(repos.packs() - before_packs) >= 2


def test_reimporting_the_same_artifact_publishes_nothing_and_gives_the_same_candidate(
    repos, work, tmp_path
):
    head, pack = work
    git_input = _input(repos)
    directory = write_artifact(tmp_path / "out", git_input, head, pack)
    first = run_import(repos, directory, git_input)
    packs = repos.packs()
    lock = Lock()
    again = run_import(repos, directory, git_input, lock=lock)
    assert again.candidate == first.candidate == head
    assert first.published and not again.published
    assert lock.taken == [] and repos.packs() == packs


def test_a_pack_is_streamed_into_index_pack_never_read_whole(repos, work, tmp_path):
    """The publication's stdin is the quarantined file, never bytes in memory."""
    head, pack = work
    git_input = _input(repos)
    directory = write_artifact(tmp_path / "out", git_input, head, pack)
    seen: list[ExecutionRequest] = []
    lock = Lock()
    runner = Runner(lock)
    runner.after = lambda request, result: seen.append(request)
    run_import(repos, directory, git_input, lock=lock, runner=runner)
    (publication,) = [r for r in seen if "--stdin" in r.command]
    assert publication.stdin_data is None and publication.stdin_path
    assert not publication.stdin_path.startswith(str(directory))
    # The quarantine is gone once the import returned.
    assert not os.path.exists(publication.stdin_path)


def test_an_unchanged_candidate_is_the_seed_with_an_empty_pack(repos, tmp_path):
    pack = pack_of(repos, [repos.seed, f"^{repos.seed}"])
    assert count_of(pack) == 0
    git_input = _input(repos)
    directory = write_artifact(tmp_path / "out", git_input, repos.seed, pack)
    lock = Lock()
    imported = run_import(repos, directory, git_input, lock=lock)
    assert imported.candidate == repos.seed and not imported.published and lock.taken == []


def test_a_relaunch_with_a_carried_commit_imports_what_it_added(repos, tmp_path):
    carried = commit(repos.agent, "first.py")
    first = _input(repos)
    run_import(
        repos,
        write_artifact(
            tmp_path / "one", first, carried, pack_of(repos, [carried, f"^{repos.seed}"])
        ),
        first,
    )
    head = commit(repos.agent, "second.py")
    relaunch = _input(repos, launch="ANALYZE_EXECUTE-2", carried=carried)
    assert bound_revision_problem(repos.common, relaunch) == ""
    pack = pack_of(repos, [head, f"^{carried}"])
    imported = run_import(repos, write_artifact(tmp_path / "two", relaunch, head, pack), relaunch)
    assert imported.candidate == head and repos.has(head)


# -- refusals that publish nothing ------------------------------------------------------


def assert_refused(repos: Repos, directory: Path, git_input: LaunchGitInput, match: str, **kw):
    before_refs, before_packs = repos.refs(), repos.packs()
    lock = kw.pop("lock", None) or Lock()
    with pytest.raises(ObjectImportError, match=match) as raised:
        run_import(repos, directory, git_input, lock=lock, **kw)
    assert "nothing was published" in str(raised.value)
    assert repos.refs() == before_refs and repos.packs() == before_packs
    assert lock.taken == []
    return raised.value


def test_an_extra_commit_that_does_not_descend_from_the_seed_is_refused(repos, work, tmp_path):
    head, _ = work
    git(repos.agent, "checkout", "-q", "--orphan", "stray")
    stray = commit(repos.agent, "stray.txt", "unrelated")
    pack = pack_of(repos, [head, stray, f"^{repos.seed}"])
    git_input = _input(repos)
    directory = write_artifact(tmp_path / "out", git_input, head, pack)
    assert_refused(repos, directory, git_input, f"holds commit {stray}, which is not between")
    assert not repos.has(stray) and not repos.has(head)


def test_a_merged_commit_that_does_not_descend_from_the_seed_is_refused(repos, work, tmp_path):
    head, _ = work
    git(repos.agent, "checkout", "-q", "--orphan", "stray")
    stray = commit(repos.agent, "stray.txt", "unrelated")
    git(repos.agent, "checkout", "-q", "--detach", head)
    git(repos.agent, *GIT_IDENT, "merge", "-q", "--allow-unrelated-histories", "-m", "m", stray)
    merged = git(repos.agent, "rev-parse", "HEAD")
    pack = pack_of(repos, [merged, f"^{repos.seed}"])
    git_input = _input(repos)
    directory = write_artifact(tmp_path / "out", git_input, merged, pack)
    assert_refused(repos, directory, git_input, f"holds commit {stray}, which is not between")


def test_an_object_the_candidate_does_not_reach_is_refused(repos, work, tmp_path):
    head, _ = work
    blob = git(repos.agent, "hash-object", "-w", "--stdin", input=b"planted\n")
    commits = git(repos.agent, "rev-list", "--objects", head, "--not", repos.seed).split("\n")
    ids = [line.split(" ", 1)[0] for line in commits] + [blob]
    pack = pack_objects(repos, ids)
    git_input = _input(repos)
    directory = write_artifact(tmp_path / "out", git_input, head, pack)
    assert_refused(repos, directory, git_input, f"holds blob {blob}, which its HEAD does not reach")
    assert not repos.has(blob)


AUTHOR = "author A U Thor <author@example.com> 1700000000 +0000"
COMMITTER = "committer A U Thor <author@example.com> 1700000000 +0000"


def raw_commit(repo: Path, tree: str, parents: list[str], message: str) -> str:
    """A commit object written into ``repo`` alone, whatever its parents are or where."""
    lines = [f"tree {tree}", *(f"parent {parent}" for parent in parents), AUTHOR, COMMITTER]
    body = "\n".join(lines) + f"\n\n{message}\n"
    return git(repo, "hash-object", "-t", "commit", "-w", "--stdin", input=body.encode())


def host_only_blob(repos: Repos, text: bytes = b"another branch's secret\n") -> str:
    """A blob only the shared store holds: no launch was given it."""
    blob = git(repos.store, "hash-object", "-w", "--stdin", input=text)
    assert not isolated_git_run("-C", str(repos.agent), "cat-file", "-e", blob).returncode == 0
    return blob


def test_a_tree_naming_a_blob_only_the_host_store_holds_is_refused(repos, tmp_path):
    """The pack holds a new commit and its tree; the tree names a blob only the shared
    store has. ``--strict`` resolves the link there, and the closure refuses it."""
    secret = host_only_blob(repos)
    readme = git(repos.agent, "rev-parse", f"{repos.seed}:README.md")
    tree = git(
        repos.agent, "mktree", "--missing",
        input=f"100644 blob {readme}\tREADME.md\n100644 blob {secret}\tsecret.txt\n".encode(),
    )  # fmt: skip
    head = raw_commit(repos.agent, tree, [repos.seed], "Name a blob I was never given (#269)")
    pack = pack_objects(repos, [head, tree])
    assert count_of(pack) == 2
    git_input = _input(repos)
    directory = write_artifact(tmp_path / "out", git_input, head, pack)
    assert_refused(
        repos, directory, git_input, f"reaches object {secret}, which neither its pack carries"
    )
    assert not repos.has(head) and not repos.has(tree)


def test_an_unrelated_merge_parent_only_the_host_store_holds_is_refused(repos, tmp_path):
    """The candidate merges a commit of another branch of the shared store, which
    the pack omits: the parent and its tree are not the launch's to name."""
    secret = host_only_blob(repos)
    tree = git(repos.store, "mktree", input=f"100644 blob {secret}\tsecret.txt\n".encode())
    other = raw_commit(repos.store, tree, [], "Another branch's root (#9)")
    seed_tree = git(repos.agent, "rev-parse", f"{repos.seed}^{{tree}}")
    merged = raw_commit(repos.agent, seed_tree, [repos.seed, other], "Merge it in (#269)")
    pack = pack_objects(repos, [merged])
    git_input = _input(repos)
    directory = write_artifact(tmp_path / "out", git_input, merged, pack)
    assert_refused(repos, directory, git_input, "which neither its pack carries nor the seed")
    assert not repos.has(merged)


def test_a_host_only_commit_claimed_with_an_empty_pack_is_refused(repos, tmp_path):
    """Another run's commit, descending from the seed, is in the shared store; an
    artifact claiming it with no pack never makes it this launch's candidate."""
    seed_tree = git(repos.store, "rev-parse", f"{repos.seed}^{{tree}}")
    other = raw_commit(repos.store, seed_tree, [repos.seed], "Another run's work (#9)")
    pack = pack_of(repos, [repos.seed, f"^{repos.seed}"])
    assert count_of(pack) == 0
    git_input = _input(repos)
    directory = write_artifact(tmp_path / "out", git_input, other, pack)
    assert_refused(repos, directory, git_input, f"reaches object {other}, which neither")


def test_a_relaunch_may_omit_what_its_carried_commit_already_holds(repos, tmp_path):
    """The carried commit was given to the container: its objects need no re-sending,
    and an object only the store holds still does."""
    carried = commit(repos.agent, "first.py")
    publish_to_store(repos, carried)
    relaunch = _input(repos, launch="ANALYZE_EXECUTE-2", carried=carried)
    head = commit(repos.agent, "second.py")
    imported = run_import(
        repos,
        write_artifact(tmp_path / "ok", relaunch, head, pack_of(repos, [head, f"^{carried}"])),
        relaunch,
    )
    assert imported.candidate == head
    secret = host_only_blob(repos)
    tree = git(
        repos.agent, "mktree", "--missing",
        input=f"100644 blob {secret}\tsecret.txt\n".encode(),
    )  # fmt: skip
    evil = raw_commit(repos.agent, tree, [carried], "Name a blob I was never given (#269)")
    directory = write_artifact(tmp_path / "evil", relaunch, evil, pack_objects(repos, [evil, tree]))
    assert_refused(repos, directory, relaunch, "nor the seed and the carried commit reaches")


def test_a_claimed_head_that_is_not_a_commit_is_refused(repos, work, tmp_path):
    head, pack = work
    tree = git(repos.agent, "rev-parse", f"{head}^{{tree}}")
    git_input = _input(repos)
    directory = write_artifact(tmp_path / "out", git_input, tree, pack)
    assert_refused(repos, directory, git_input, f"claimed HEAD {tree} is not a commit")


def test_a_claimed_head_that_is_nowhere_is_refused(repos, work, tmp_path):
    _, pack = work
    git_input = _input(repos)
    directory = write_artifact(tmp_path / "out", git_input, "e" * 40, pack)
    assert_refused(repos, directory, git_input, "is not a commit")


def test_a_claimed_head_below_the_seed_is_refused(repos, tmp_path):
    later = commit(repos.store, "later.md")
    git_input = _input(repos, seed=later, lower=later)
    pack = pack_of(repos, [repos.seed, f"^{repos.seed}"])
    directory = write_artifact(tmp_path / "out", git_input, repos.seed, pack)
    assert_refused(repos, directory, git_input, f"does not descend from the seed {later}")


def test_a_pack_that_fails_strict_is_refused(repos, tmp_path):
    tree = git(repos.agent, "rev-parse", f"{repos.seed}^{{tree}}")
    raw = f"tree {tree}\nparent {repos.seed}\nauthor x\ncommitter x\n\nbroken (#269)\n"
    bad = git(repos.agent, "hash-object", "-t", "commit", "-w", "--literally", "--stdin",
              input=raw.encode())  # fmt: skip
    pack = pack_objects(repos, [bad])
    git_input = _input(repos)
    directory = write_artifact(tmp_path / "out", git_input, bad, pack)
    assert_refused(repos, directory, git_input, "indexing its pack strictly exited")
    assert not repos.has(bad)


def test_a_corrupt_pack_is_refused(repos, work, tmp_path):
    head, pack = work
    corrupt = bytearray(pack)
    corrupt[40] ^= 0xFF
    git_input = _input(repos)
    # The manifest describes the corrupt bytes exactly, so only git can refuse it.
    directory = write_artifact(tmp_path / "out", git_input, head, bytes(corrupt))
    assert_refused(repos, directory, git_input, "indexing its pack strictly exited")


def test_a_pack_missing_an_object_its_commits_need_is_refused(repos, work, tmp_path):
    head, _ = work
    pack = pack_objects(repos, [head])  # the commit alone: its tree is nowhere
    git_input = _input(repos)
    directory = write_artifact(tmp_path / "out", git_input, head, pack)
    assert_refused(repos, directory, git_input, "indexing its pack strictly exited")


def test_a_thin_pack_is_refused(repos, work, tmp_path):
    git(repos.agent, "checkout", "-q", "--detach", repos.seed)
    base = commit(repos.agent, "feature.py", "print('feature')\n" * 50)
    publish_to_store(repos, base)
    head = commit(repos.agent, "feature.py", "print('feature')\n" * 50 + "print('more')\n")
    thin = isolated_git(
        "-C", str(repos.agent), "pack-objects", "--revs", "--thin", "--stdout", "-q",
        input=f"{head}\n^{base}\n".encode(),
    )  # fmt: skip
    git_input = _input(repos, seed=base, lower=base)
    directory = write_artifact(tmp_path / "out", git_input, head, thin)
    assert_refused(repos, directory, git_input, "indexing its pack strictly exited")


def test_an_oversized_pack_is_refused_before_a_byte_of_it_is_read(repos, monkeypatch, tmp_path):
    """A pack over the export bound, sparse on disk: refused from its size alone,
    with no quarantine, no copy and no allocation anywhere near its size."""
    git_input = _input(repos)
    directory = write_artifact(tmp_path / "out", git_input, repos.seed, b"")
    with open(directory / ARTIFACT_PACK, "wb") as handle:
        handle.truncate(MAX_ARTIFACT_PACK_BYTES + 1)
    manifest = parse_manifest((directory / ARTIFACT_MANIFEST).read_bytes())
    (directory / ARTIFACT_MANIFEST).write_bytes(
        replace(manifest, pack_bytes=MAX_ARTIFACT_PACK_BYTES + 1).render()
    )

    def never(*args, **kwargs):
        raise AssertionError("the oversized pack was read")

    monkeypatch.setattr(candidate_artifact, "_quarantine", never)
    monkeypatch.setattr(candidate_artifact, "_copy_pack", never)
    monkeypatch.setattr(candidate_artifact.os, "pread", never)
    tracemalloc.start()
    try:
        assert_refused(repos, directory, git_input, "over the export bound")
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 4 * 1024 * 1024


def test_a_pack_over_a_smaller_bound_is_refused(repos, work, tmp_path):
    head, pack = work
    git_input = _input(repos)
    directory = write_artifact(tmp_path / "out", git_input, head, pack)
    limits = ArtifactLimits(max_pack_bytes=len(pack) - 1)
    assert_refused(repos, directory, git_input, "over the export bound", limits=limits)


def test_a_pack_with_more_objects_than_the_bound_is_refused(repos, work, tmp_path):
    head, pack = work
    git_input = _input(repos)
    directory = write_artifact(tmp_path / "out", git_input, head, pack)
    limits = ArtifactLimits(max_objects=count_of(pack) - 1)
    assert_refused(repos, directory, git_input, "objects, over the export bound", limits=limits)


@pytest.mark.parametrize(
    ("changes", "match"),
    [
        ({"pack_bytes": 1}, "and its manifest says 1"),
        ({"pack_objects": 99}, "and its manifest says 99"),
        ({"pack_sha256": "0" * 64}, "SHA-256 is not the one its manifest names"),
    ],
)
def test_a_pack_that_is_not_the_one_its_manifest_describes_is_refused(
    repos, work, tmp_path, changes, match
):
    head, pack = work
    git_input = _input(repos)
    directory = write_artifact(tmp_path / "out", git_input, head, pack, **changes)
    assert_refused(repos, directory, git_input, match)


def test_a_pack_with_a_bad_header_is_refused(repos, tmp_path):
    git_input = _input(repos)
    directory = write_artifact(tmp_path / "out", git_input, repos.seed, b"NOTAPACK" + b"\0" * 24)
    assert_refused(repos, directory, git_input, "does not start with a pack header")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("run", "run-other"),
        ("issue", 270),
        ("phase", "FIX"),
        ("launch", "ANALYZE_EXECUTE-9"),
        ("repository", "owner/other"),
        ("seed", "f" * 40),
        ("lower", "f" * 40),
        ("carried", "f" * 40),
        ("seed_manifest_digest", "sha256:" + "0" * 64),
        ("export_command", True),
        ("entry_label", "fix-round-9"),
    ],
)
def test_a_manifest_that_disagrees_with_the_git_input_is_refused(
    repos, work, tmp_path, field, value
):
    """Another launch, run, issue, seed, boundary, carried commit or seed: refused,
    before a byte of the pack is read."""
    head, pack = work
    git_input = _input(repos)
    directory = write_artifact(tmp_path / "out", git_input, head, pack, **{field: value})
    assert_refused(repos, directory, git_input, f"its manifest names {field} ")


def test_a_manifest_naming_another_object_format_is_refused(repos, work, tmp_path):
    """A sha256 manifest over a sha1 store: its ids are not the input's format."""
    head, pack = work
    git_input = _input(repos)
    directory = write_artifact(tmp_path / "out", git_input, head, pack, object_format="sha256")
    assert_refused(repos, directory, git_input, "candidate is not a full object id")
    directory = write_artifact(tmp_path / "out2", git_input, "a" * 64, pack, object_format="sha256")
    assert_refused(repos, directory, git_input, "its manifest names object_format 'sha256'")


def test_the_expected_values_are_the_inputs_never_the_artifacts(repos, work, tmp_path):
    """An artifact written for another launch's input is refused against this one's."""
    head, pack = work
    mine = _input(repos)
    theirs = _input(repos, launch="ANALYZE_EXECUTE-2")
    directory = write_artifact(tmp_path / "out", theirs, head, pack)
    assert manifest_disagreement(parse_manifest((directory / ARTIFACT_MANIFEST).read_bytes()), mine)
    assert_refused(repos, directory, mine, "launch")


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"not json",
        b"[]",
        b'{"schema": 2}',
        b"\xff\xfe",
    ],
)
def test_a_manifest_that_does_not_parse_is_refused(raw):
    with pytest.raises(ObjectImportError, match="manifest"):
        parse_manifest(raw)


@pytest.fixture
def digit_limit():
    """The interpreter's default integer digit limit, whatever the environment set."""
    before = sys.get_int_max_str_digits()
    sys.set_int_max_str_digits(4300)
    yield 4300
    sys.set_int_max_str_digits(before)


def test_a_manifest_with_an_integer_past_the_digit_limit_is_refused(
    repos, work, tmp_path, digit_limit
):
    """PR #474 R2-F2: a 5,000-digit integer, far below the manifest's size bound,
    makes the decoder raise ValueError: a candidate refusal, nothing published."""
    head, pack = work
    git_input = _input(repos)
    directory = write_artifact(tmp_path / "out", git_input, head, pack)
    path = directory / ARTIFACT_MANIFEST
    raw = path.read_bytes()
    hostile = raw.replace(b'"issue":269,', b'"issue":1' + b"0" * (digit_limit + 699) + b",")
    assert hostile != raw and len(hostile) < MAX_MANIFEST_BYTES
    path.write_bytes(hostile)
    assert_refused(repos, directory, git_input, r"its manifest does not parse \(ValueError\)")
    assert not repos.has(head)


def test_a_manifest_nested_as_deep_as_its_bound_allows_is_refused(repos, work, tmp_path):
    """R2-F2: the deepest nesting the size bound admits is refused, nothing
    published: by the decoder's RecursionError where it recurses that deep,
    by the schema check where it does not."""
    head, pack = work
    git_input = _input(repos)
    directory = write_artifact(tmp_path / "out", git_input, head, pack)
    depth = (MAX_MANIFEST_BYTES - 16) // 2
    hostile = b'{"schema":' + b"[" * depth + b"]" * depth + b"}"
    assert len(hostile) <= MAX_MANIFEST_BYTES
    (directory / ARTIFACT_MANIFEST).write_bytes(hostile)
    assert_refused(repos, directory, git_input, "its manifest")
    assert not repos.has(head)


@pytest.mark.parametrize(
    "raised",
    [RecursionError("maximum recursion depth exceeded"), ValueError("Exceeds the limit")],
)
def test_every_decoder_failure_is_a_refusal_never_a_raw_error(repos, monkeypatch, raised):
    """R2-F2: whatever the decoder raises on content, a nesting or digit limit
    included, is an ObjectImportError naming its type and none of its text."""

    def decoder_fails(text: str):
        raise raised

    monkeypatch.setattr(candidate_artifact.json, "loads", decoder_fails)
    with pytest.raises(ObjectImportError) as refused:
        parse_manifest(_manifest_with_bytes(repos))
    assert f"its manifest does not parse ({type(raised).__name__})" in str(refused.value)
    assert "nothing was published" in str(refused.value)
    assert str(raised) not in str(refused.value)


def _manifest_with_bytes(repos: Repos) -> bytes:
    return CandidateManifest.for_input(
        _input(repos), candidate=repos.seed, pack_bytes=0, pack_objects=0, pack_sha256="0" * 64
    ).render()


def _manifest_with(repos: Repos, **data) -> bytes:
    manifest = CandidateManifest.for_input(
        _input(repos), candidate=repos.seed, pack_bytes=0, pack_objects=0, pack_sha256="0" * 64
    )
    raw = json.loads(manifest.render())
    raw.update(data)
    return json.dumps(raw).encode()


@pytest.mark.parametrize(
    ("data", "match"),
    [
        ({"refs": {"refs/heads/main": "0" * 40}}, "unknown: refs"),
        ({"path": "/etc/passwd"}, "unknown: path"),
        ({"issue": "269"}, "issue is not a int"),
        ({"issue": True}, "issue is not a int"),
        ({"candidate": "../../x"}, "candidate is not a full object id"),
        ({"pack_bytes": -1}, "negative"),
        ({"repository": "../x"}, "not an owner/name"),
        ({"object_format": "md5"}, "unknown object format"),
    ],
)
def test_a_manifest_naming_refs_paths_or_malformed_values_is_refused(repos, data, match):
    with pytest.raises(ObjectImportError, match=match):
        parse_manifest(_manifest_with(repos, **data))


def test_a_manifest_missing_a_field_is_refused(repos):
    raw = json.loads(_manifest_with(repos))
    del raw["seed"]
    with pytest.raises(ObjectImportError, match="missing: seed"):
        parse_manifest(json.dumps(raw).encode())


def test_an_oversized_manifest_is_refused_before_it_is_read(repos, work, tmp_path):
    head, pack = work
    git_input = _input(repos)
    directory = write_artifact(tmp_path / "out", git_input, head, pack)
    with open(directory / ARTIFACT_MANIFEST, "ab") as handle:
        handle.truncate(64 * 1024 + 1)
    assert_refused(repos, directory, git_input, "manifest holds 65537 bytes, over the bound")


# -- the artifact's directory: links, traversal, planted files ----------------------------


def test_a_directory_reached_through_a_link_is_refused(repos, work, tmp_path):
    head, pack = work
    git_input = _input(repos)
    real = write_artifact(tmp_path / "real", git_input, head, pack)
    link = tmp_path / "out"
    link.symlink_to(real)
    assert_refused(repos, link, git_input, "without following a link")


@pytest.mark.parametrize("name", [ARTIFACT_PACK, ARTIFACT_MANIFEST])
def test_a_file_that_is_a_link_to_a_host_path_is_refused(repos, work, tmp_path, name):
    head, pack = work
    git_input = _input(repos)
    directory = write_artifact(tmp_path / "out", git_input, head, pack)
    host = tmp_path / "host-secret"
    host.write_bytes((directory / name).read_bytes())
    (directory / name).unlink()
    (directory / name).symlink_to(host)
    assert_refused(repos, directory, git_input, f"its {name} cannot be opened without following")


def test_a_pack_that_is_a_fifo_is_refused_without_blocking(repos, work, tmp_path):
    head, pack = work
    git_input = _input(repos)
    directory = write_artifact(tmp_path / "out", git_input, head, pack)
    (directory / ARTIFACT_PACK).unlink()
    os.mkfifo(directory / ARTIFACT_PACK)
    assert_refused(repos, directory, git_input, f"its {ARTIFACT_PACK} is not a regular file")


@pytest.mark.parametrize("planted", ["../escape", "extra.pack", ".hidden", "sub/"])
def test_a_directory_holding_anything_else_is_refused(repos, work, tmp_path, planted):
    head, pack = work
    git_input = _input(repos)
    directory = write_artifact(tmp_path / "out", git_input, head, pack)
    if planted.endswith("/"):
        (directory / planted.rstrip("/")).mkdir()
    elif planted.startswith(".."):
        (directory / "escape").symlink_to(tmp_path)
    else:
        (directory / planted).write_text("x")
    assert_refused(repos, directory, git_input, "which an artifact does not")


def test_a_directory_lacking_its_pack_is_refused(repos, work, tmp_path):
    head, pack = work
    git_input = _input(repos)
    directory = write_artifact(tmp_path / "out", git_input, head, pack)
    (directory / ARTIFACT_PACK).unlink()
    assert_refused(repos, directory, git_input, f"lacks {ARTIFACT_PACK}")


def test_a_candidate_that_does_not_read_as_its_own_bytes_is_refused(repos, work, tmp_path):
    head, pack = work
    git_input = _input(repos)
    directory = write_artifact(tmp_path / "out", git_input, head, pack)

    def substituted(sha: str):
        raise GitTransportError(f"commit {sha}: the stored bytes do not hash to its id")

    with pytest.raises(ObjectImportError, match="does not read as its own bytes"):
        run_import(repos, directory, git_input, read_commit=substituted)


def test_a_lock_not_obtained_refuses_the_import_and_writes_nothing(repos, work, tmp_path):
    head, pack = work
    git_input = _input(repos)
    directory = write_artifact(tmp_path / "out", git_input, head, pack)
    packs = repos.packs()
    lock = Lock(fail=GitLockTimeout("L1 was held past its bound"))
    with pytest.raises(ObjectImportError, match="shared-Git lock was not obtained"):
        run_import(repos, directory, git_input, lock=lock)
    assert repos.packs() == packs and not repos.has(head)


# -- crash windows: a re-import gives the same candidate --------------------------------


class Crash(BaseException):
    """The controller dying at a boundary (a kill, Ctrl-C): nothing catches it."""


def test_a_crash_before_l1_leaves_nothing_and_a_reimport_succeeds(repos, work, tmp_path):
    head, pack = work
    git_input = _input(repos)
    directory = write_artifact(tmp_path / "out", git_input, head, pack)
    packs = repos.packs()
    with pytest.raises(Crash):
        run_import(repos, directory, git_input, lock=Lock(fail=Crash()))
    assert repos.packs() == packs and not repos.has(head)
    assert run_import(repos, directory, git_input).candidate == head


def test_a_crash_during_index_pack_publishes_nothing_and_a_reimport_succeeds(repos, work, tmp_path):
    """``index-pack`` killed half-way: only a temporary file it never renamed is
    left, no object is visible, and the import again gives the candidate."""
    head, pack = work
    git_input = _input(repos)
    directory = write_artifact(tmp_path / "out", git_input, head, pack)
    half = tmp_path / "half.pack"
    half.write_bytes(pack[: len(pack) // 2])
    lock = Lock()
    runner = Runner(lock)
    original = runner.__call__

    def crash_in_publication(request: ExecutionRequest):
        if "--stdin" in request.command:
            request.stdin_path = str(half)  # it reads half the pack, then dies
            execute(request)
            raise Crash()
        return original(request)

    with pytest.raises(Crash):
        run_import(repos, directory, git_input, lock=lock, runner=crash_in_publication)
    assert not repos.has(head)
    imported = run_import(repos, directory, git_input)
    assert imported.candidate == head and imported.published


def test_a_crash_after_publication_reimports_without_writing_again(repos, work, tmp_path):
    head, pack = work
    git_input = _input(repos)
    directory = write_artifact(tmp_path / "out", git_input, head, pack)
    lock = Lock()
    runner = Runner(lock)

    def crash_after(request, result):
        if "--stdin" in request.command:
            raise Crash()

    runner.after = crash_after
    with pytest.raises(Crash):
        run_import(repos, directory, git_input, lock=lock, runner=runner)
    assert repos.has(head)
    packs = repos.packs()
    again_lock = Lock()
    imported = run_import(repos, directory, git_input, lock=again_lock)
    assert imported.candidate == head and not imported.published
    assert again_lock.taken == [] and repos.packs() == packs


# -- the bound revision, before any journal entry ----------------------------------------


def test_a_bound_revision_the_store_holds_is_accepted(repos):
    assert bound_revision_problem(repos.common, _input(repos)) == ""


def test_a_bound_seed_missing_from_the_store_is_refused(repos):
    missing = "d" * 40
    problem = bound_revision_problem(repos.common, _input(repos, seed=missing, lower=missing))
    assert f"seed {missing} is not a commit the shared store holds" in problem


def test_a_merge_base_that_is_not_an_ancestor_of_the_head_is_refused(repos):
    later = commit(repos.store, "later.md")
    git_input = _input(repos, phase="REVIEW", seed=repos.seed, lower=later)
    problem = bound_revision_problem(repos.common, git_input)
    assert f"lower boundary {later} is not an ancestor of the seed" in problem


def test_a_merge_base_missing_from_the_store_is_refused(repos):
    git_input = _input(repos, phase="FIX", lower="d" * 40)
    assert "lower boundary" in bound_revision_problem(repos.common, git_input)


def test_a_carried_commit_that_does_not_descend_from_the_seed_is_refused(repos):
    later = commit(repos.store, "later.md")
    git(repos.store, "checkout", "-q", "--orphan", "other")
    other = commit(repos.store, "other.md")
    git_input = _input(repos, seed=later, lower=later, carried=other)
    assert "does not descend from the seed" in bound_revision_problem(repos.common, git_input)
    missing = _input(repos, carried="d" * 40)
    assert "carried commit" in bound_revision_problem(repos.common, missing)


def test_an_export_command_needs_only_its_commit(repos):
    git_input = _input(repos, phase="REVIEW", lower="", export_command=True)
    assert bound_revision_problem(repos.common, git_input) == ""


def test_the_store_is_read_without_the_containers_objects(repos, work, tmp_path):
    """A commit only the agent's repository holds is not in the shared store."""
    head, _ = work
    git_input = _input(repos, seed=head, lower=head)
    assert "is not a commit the shared store holds" in bound_revision_problem(
        repos.common, git_input
    )


def test_the_runner_defaults_to_the_executor(repos):
    assert subprocess.run(["git", "--version"], capture_output=True).returncode == 0
    assert bound_revision_problem(repos.common, _input(repos), None) == ""


def test_an_unknown_manifest_key_shaped_like_a_credential_is_never_echoed(repos):
    token = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
    with pytest.raises(ObjectImportError) as raised:
        parse_manifest(_manifest_with(repos, **{token: 1}))
    assert "unknown:" in str(raised.value) and token not in str(raised.value)
