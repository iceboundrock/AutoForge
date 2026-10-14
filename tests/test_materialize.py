"""#171: byte-exact materialization of a REMOTE candidate and its hash-back proof.

Every test builds its repositories under ``tmp_path`` with git plumbing
(``hash-object``, ``mktree``, ``commit-tree``), so each blob holds exactly
the bytes the test chose, and hostile trees are written as objects directly.
Nothing touches the network. The exports are created under a temporary
directory of the test's own, so a leftover is visible.
"""

from __future__ import annotations

import errno
import os
import random
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import pytest

from autoforge import materialize as materialize_mod
from autoforge.agent_worktree import import_private_objects
from autoforge.errors import (
    CandidateNotMaterializableError,
    MaterializationError,
    VerificationError,
)
from autoforge.executor import ExecutionRequest, ExecutionResult, execute
from autoforge.materialize import METHOD, MaterializedTree, materialize, read_object_format

from .conftest import GIT_IDENT

FORMATS = ["sha1", "sha256"]


# -- fixtures ------------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def exports(tmp_path, monkeypatch) -> Path:
    """Where ``tempfile.mkdtemp`` creates the exports during a test."""
    where = tmp_path / "exports"
    where.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(where))
    return where


def _git(repo: Path, *args: str, data: bytes | None = None, env: dict | None = None) -> bytes:
    clean = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        input=data,
        env={**clean, **(env or {})},
    ).stdout


def _out(repo: Path, *args: str, data: bytes | None = None) -> str:
    return _git(repo, *args, data=data).decode().strip()


def _init(path: Path, object_format: str = "sha1") -> Path:
    subprocess.run(
        ["git", "init", "-q", "-b", "main", f"--object-format={object_format}", str(path)],
        check=True,
    )
    return path


@dataclass(frozen=True)
class Exe:
    content: bytes


@dataclass(frozen=True)
class Link:
    target: bytes


@dataclass(frozen=True)
class Gitlink:
    commit: str


def _blob(repo: Path, content: bytes) -> str:
    return _out(repo, "hash-object", "-w", "--no-filters", "--stdin", data=content)


def _mktree(repo: Path, entries: list[tuple[str, str, str, bytes]]) -> str:
    """A tree of ``(mode, type, id, name)`` entries; ``mktree`` sorts them."""
    data = b"".join(f"{m} {t} {o}\t".encode() + name + b"\0" for m, t, o, name in entries)
    return _out(repo, "mktree", "-z", "--missing", data=data)


def _literal_tree(repo: Path, entries: list[tuple[bytes, bytes, str]]) -> str:
    """A tree object of ``(mode, name, id)`` entries written as given, unchecked."""
    body = b"".join(mode + b" " + name + b"\0" + bytes.fromhex(oid) for mode, name, oid in entries)
    return _out(repo, "hash-object", "-t", "tree", "--literally", "-w", "--stdin", data=body)


def _tree(repo: Path, spec: dict) -> str:
    entries = []
    for key, value in spec.items():
        name = key if isinstance(key, bytes) else key.encode()
        if isinstance(value, dict):
            entries.append(("040000", "tree", _tree(repo, value), name))
        elif isinstance(value, Exe):
            entries.append(("100755", "blob", _blob(repo, value.content), name))
        elif isinstance(value, Link):
            entries.append(("120000", "blob", _blob(repo, value.target), name))
        elif isinstance(value, Gitlink):
            entries.append(("160000", "commit", value.commit, name))
        else:
            entries.append(("100644", "blob", _blob(repo, value), name))
    return _mktree(repo, entries)


def _commit(repo: Path, tree: str) -> str:
    return _out(repo, *GIT_IDENT, "commit-tree", "-m", "candidate", tree)


def _export(repo: Path, commit: str, runner=execute):
    fmt = read_object_format(runner, str(repo))
    return materialize(runner, str(repo), commit, fmt, prefix="autoforge-validate-")


def _assert_exact(repo: Path, root: Path, commit: str) -> None:
    """Every blob on disk is ``git cat-file blob`` byte for byte (link text for a link)."""
    listing = _git(repo, "ls-tree", "-r", "-z", "--full-tree", commit)
    for record in listing.split(b"\0")[:-1]:
        meta, _, path = record.partition(b"\t")
        mode, kind, oid = meta.split(b" ")
        on_disk = os.path.join(os.fsencode(root), path)
        if kind == b"commit":
            assert os.path.isdir(on_disk) and not os.listdir(on_disk)
            continue
        committed = _git(repo, "cat-file", "blob", oid.decode())
        if mode == b"120000":
            assert os.readlink(on_disk) == committed
        else:
            with open(on_disk, "rb") as handle:
                assert handle.read() == committed, path


def _leftovers(exports: Path) -> list[Path]:
    return list(exports.iterdir())


# -- fidelity ------------------------------------------------------------------------------
HOSTILE_ATTRIBUTES = b"* text eol=crlf ident filter=x\n"


@pytest.mark.parametrize("object_format", FORMATS)
def test_the_export_holds_the_committed_bytes_whatever_attributes_and_configuration_say(
    tmp_path, monkeypatch, exports, object_format
):
    repo = _init(tmp_path / "repo", object_format)
    sentinel = tmp_path / "smudge-ran"
    smudge = f"touch {sentinel}; cat"
    # The operator's global configuration (HOME reaches the controller's git) ...
    home = tmp_path / "home"
    home.mkdir()
    (home / ".gitconfig").write_text(
        f'[filter "x"]\n\tsmudge = {smudge}\n\trequired = true\n[core]\n\tautocrlf = true\n'
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    # ... the repository's own, $GIT_DIR/info/attributes and core.attributesFile ...
    _git(repo, "config", "core.autocrlf", "true")
    _git(repo, "config", "filter.x.smudge", smudge)
    attributes_file = tmp_path / "attributes"
    attributes_file.write_bytes(HOSTILE_ATTRIBUTES)
    _git(repo, "config", "core.attributesFile", str(attributes_file))
    (repo / ".git" / "info").mkdir(exist_ok=True)
    (repo / ".git" / "info" / "attributes").write_bytes(HOSTILE_ATTRIBUTES)
    # ... and the candidate's own .gitattributes.
    commit = _commit(
        repo,
        _tree(
            repo,
            {
                ".gitattributes": HOSTILE_ATTRIBUTES + b"*.txt export-ignore\n",
                "a.txt": b"one\ntwo\n$Id$\n",
                "pointer.bin": b"version https://git-lfs.github.com/spec/v1\nsize 12\n",
                "binary.dat": bytes(range(256)) * 4 + b"\xff\xfe\x00 \xc3\x28 not utf-8",
                "sub": {"mixed.txt": b"crlf\r\nlf\n"},
            },
        ),
    )

    # Control: git's own checkout of the same commit converts and runs the filter.
    index = tmp_path / "control-index"
    control = tmp_path / "control"
    _git(repo, "read-tree", commit, env={"GIT_INDEX_FILE": str(index)})
    _git(repo, "checkout-index", "-a", f"--prefix={control}/", env={"GIT_INDEX_FILE": str(index)})
    assert (control / "a.txt").read_bytes() != b"one\ntwo\n$Id$\n"
    assert sentinel.exists()
    sentinel.unlink()

    with _export(repo, commit) as tree:
        _assert_exact(repo, tree.root, commit)
        assert (tree.root / "a.txt").read_bytes() == b"one\ntwo\n$Id$\n"
        assert (tree.root / "pointer.bin").read_bytes().startswith(b"version https://git-lfs")
        assert tree.object_format == object_format
    assert not sentinel.exists()  # no filter program ran on the candidate's content
    assert not _leftovers(exports)


@pytest.mark.parametrize("object_format", FORMATS)
def test_modes_links_gitlinks_and_empty_trees_are_written_as_the_tree_lists_them(
    tmp_path, exports, object_format
):
    repo = _init(tmp_path / "repo", object_format)
    absent = "e" * (40 if object_format == "sha1" else 64)  # never in the store
    latin = b"caf\xe9-\xff.bin"  # not valid UTF-8
    commit = _commit(
        repo,
        _tree(
            repo,
            {
                "run.sh": Exe(b"#!/bin/sh\necho hi\n"),
                "plain.txt": b"plain\n",
                "empty.txt": b"",
                "relative": Link(b"plain.txt"),
                "escaping": Link(b"../../outside/the/export"),
                "absolute": Link(b"/etc/passwd"),
                "dangling": Link(b"does/not/exist"),
                latin: b"a name that is not UTF-8\n",
                "vendor": {"lib": Gitlink(absent)},
                "nothing": {},
                "a.b": b"sorts before a/\n",
                "a": {"x": b"x\n", "deeper": {"y": Exe(b"y\n")}},
            },
        ),
    )
    with _export(repo, commit) as tree:
        root = tree.root
        assert isinstance(tree, MaterializedTree)
        assert tree.commit == commit
        assert tree.tree == _out(repo, "rev-parse", f"{commit}^{{tree}}")
        assert tree.method == METHOD == "byte-exact-v1"
        assert (tree.files, tree.gitlinks) == (11, 1)
        assert root.parent == exports and root.name.startswith("autoforge-validate-")
        assert stat.S_IMODE(root.stat().st_mode) == 0o700
        assert stat.S_IMODE((root / "run.sh").stat().st_mode) == 0o755
        assert stat.S_IMODE((root / "a" / "deeper" / "y").stat().st_mode) == 0o755
        assert stat.S_IMODE((root / "plain.txt").stat().st_mode) == 0o644
        assert stat.S_IMODE((root / "empty.txt").stat().st_mode) == 0o644
        for name, target in [
            ("relative", "plain.txt"),
            ("escaping", "../../outside/the/export"),
            ("absolute", "/etc/passwd"),
            ("dangling", "does/not/exist"),
        ]:
            assert (root / name).is_symlink() and os.readlink(root / name) == target
        assert (root / "vendor" / "lib").is_dir() and not list((root / "vendor" / "lib").iterdir())
        assert (root / "nothing").is_dir() and not list((root / "nothing").iterdir())
        assert os.path.isfile(os.path.join(os.fsencode(root), latin))
        assert not any(p.name.lower() == ".git" for p in root.rglob("*"))
        _assert_exact(repo, root, commit)
    assert not root.exists()
    assert not _leftovers(exports)


def test_a_blob_larger_than_any_capture_bound_is_streamed_exactly(tmp_path):
    repo = _init(tmp_path / "repo")
    big = random.Random(171).randbytes(3 * 1024 * 1024 + 7)
    many = {f"f{i:03d}": random.Random(i).randbytes(i * 37) for i in range(60)}
    commit = _commit(repo, _tree(repo, {"big.bin": big, "small": many}))
    with _export(repo, commit) as tree:
        assert (tree.root / "big.bin").read_bytes() == big
        _assert_exact(repo, tree.root, commit)
        assert tree.files == 61


def _rechunking(size: int):
    """A runner handing every ``on_chunk`` hook its stream in ``size``-byte pieces."""

    def runner(req: ExecutionRequest) -> ExecutionResult:
        hook = req.on_chunk
        if hook is not None:

            def split(stream: str, chunk: bytes) -> None:
                for start in range(0, len(chunk), size):
                    hook(stream, chunk[start : start + size])

            req.on_chunk = split
        return execute(req)

    return runner


@pytest.mark.parametrize("size", [1, 3, 41])
def test_the_stream_is_parsed_whatever_its_chunk_boundaries(tmp_path, size):
    repo = _init(tmp_path / "repo")
    commit = _commit(
        repo,
        _tree(
            repo,
            {
                "empty": b"",
                "newline": b"\n",
                "header-like": b"0123 blob 7\n",
                "link": Link(b"empty"),
                "dir": {"x": Exe(b"x" * 300)},
            },
        ),
    )
    with _export(repo, commit, runner=_rechunking(size)) as tree:
        _assert_exact(repo, tree.root, commit)


# -- the proof -----------------------------------------------------------------------------
def _relink(path: Path, target: str) -> None:
    path.unlink()
    path.symlink_to(target)


def _to_file(path: Path, content: bytes) -> None:
    path.unlink() if path.is_symlink() or path.is_file() else path.rmdir()
    path.write_bytes(content)


TAMPERS = {
    "a changed byte": (lambda r: (r / "plain.txt").write_bytes(b"plain!\n"), "plain.txt"),
    "a changed byte below a tree": (lambda r: (r / "a" / "x").write_bytes(b"y\n"), "a/x"),
    "an added file": (lambda r: (r / "extra.txt").write_bytes(b""), "extra.txt"),
    "an added directory": (lambda r: (r / "extra").mkdir(), "extra"),
    "a removed file": (lambda r: (r / "plain.txt").unlink(), "plain.txt"),
    "a removed empty tree": (lambda r: (r / "nothing").rmdir(), "nothing"),
    "an executable bit set": (lambda r: (r / "plain.txt").chmod(0o755), "plain.txt"),
    "an executable bit cleared": (lambda r: (r / "run.sh").chmod(0o644), "run.sh"),
    "a symlink retargeted": (lambda r: _relink(r / "link", "run.sh"), "link"),
    # The same bytes as the link text: only the mode tells them apart.
    "a symlink replaced by a file": (lambda r: _to_file(r / "link", b"plain.txt"), "link"),
    "a file replaced by a symlink": (lambda r: _relink(r / "plain.txt", "plain\n"), "plain.txt"),
    "a file written into a gitlink": (
        lambda r: (r / "vendor" / "lib" / "x").write_bytes(b""),
        "vendor/lib",
    ),
    "a gitlink replaced by a file": (lambda r: _to_file(r / "vendor" / "lib", b""), "vendor/lib"),
    "a fifo": (lambda r: os.mkfifo(r / "fifo"), "fifo"),
    "a fifo in place of a file": (
        lambda r: ((r / "plain.txt").unlink(), os.mkfifo(r / "plain.txt")),
        "plain.txt",
    ),
}


def _proof_fixture(repo: Path, object_format: str) -> str:
    absent = "d" * (40 if object_format == "sha1" else 64)
    return _commit(
        repo,
        _tree(
            repo,
            {
                "plain.txt": b"plain\n",
                "run.sh": Exe(b"#!/bin/sh\n"),
                "link": Link(b"plain.txt"),
                "a": {"x": b"x\n"},
                "nothing": {},
                "vendor": {"lib": Gitlink(absent)},
            },
        ),
    )


@pytest.mark.parametrize("object_format", FORMATS)
@pytest.mark.parametrize("tamper", sorted(TAMPERS))
def test_tampering_between_the_write_and_the_proof_is_refused(
    tmp_path, monkeypatch, exports, object_format, tamper
):
    repo = _init(tmp_path / "repo", object_format)
    commit = _proof_fixture(repo, object_format)
    change, named = TAMPERS[tamper]
    write = materialize_mod._write

    def write_then_tamper(runner, repo_, commit_, listing, root):
        write(runner, repo_, commit_, listing, root)
        change(root)

    monkeypatch.setattr(materialize_mod, "_write", write_then_tamper)
    with pytest.raises(MaterializationError, match="does not hash back to its tree") as info:
        with _export(repo, commit):
            pytest.fail("an export that fails its proof is never handed out")
    assert f"{named} " in str(info.value)
    assert commit in str(info.value)
    assert not _leftovers(exports)


@pytest.mark.parametrize("object_format", FORMATS)
def test_a_faithful_export_passes_the_proof_unchanged(tmp_path, object_format):
    """The control for the tamper cases: the same fixture, untouched, is proven."""
    repo = _init(tmp_path / "repo", object_format)
    commit = _proof_fixture(repo, object_format)
    with _export(repo, commit) as tree:
        _assert_exact(repo, tree.root, commit)
        assert (tree.files, tree.gitlinks) == (4, 1)


@pytest.mark.parametrize("object_format", FORMATS)
def test_a_legacy_entry_mode_fails_the_proof(tmp_path, exports, object_format):
    """``ls-tree`` prints ``100664`` as ``100644``; the stored tree id cannot be reproduced."""
    repo = _init(tmp_path / "repo", object_format)
    blob = _blob(repo, b"group-writable, once\n")
    legacy = _literal_tree(repo, [(b"100664", b"old.txt", blob)])
    commit = _commit(repo, _mktree(repo, [("040000", "tree", legacy, b"legacy")]))
    assert b"100644 blob" in _git(repo, "ls-tree", "-r", commit)
    with pytest.raises(MaterializationError, match="legacy hashes to the tree"):
        with _export(repo, commit):
            pytest.fail("the body must not run")
    assert not _leftovers(exports)


class _OsProxy:
    """The ``os`` module, as :mod:`autoforge.materialize` alone sees it."""

    def __getattr__(self, name):
        return getattr(os, name)


def _fold(path):
    return path.lower() if isinstance(path, bytes) else path


class _CaseFoldingOs(_OsProxy):
    """A case-insensitive filesystem: every name the writer uses is folded."""

    @staticmethod
    def open(path, flags, mode=0o777, *, dir_fd=None):
        return os.open(_fold(path), flags, mode, dir_fd=dir_fd)

    @staticmethod
    def mkdir(path, mode=0o777, *, dir_fd=None):
        return os.mkdir(_fold(path), mode, dir_fd=dir_fd)

    @staticmethod
    def symlink(src, dst, target_is_directory=False, *, dir_fd=None):
        return os.symlink(src, _fold(dst), target_is_directory, dir_fd=dir_fd)


@pytest.mark.parametrize(
    ("first", "second"),
    [
        (b"Makefile", b"makefile"),
        ({"x": b"1\n"}, {"y": b"2\n"}),
        (Link(b"Makefile"), b"makefile"),
    ],
    ids=["two-files", "two-trees", "a-link-and-a-file"],
)
def test_names_a_case_folding_filesystem_would_merge_are_refused(
    tmp_path, monkeypatch, exports, first, second
):
    repo = _init(tmp_path / "repo")
    commit = _commit(repo, _tree(repo, {"Makefile": first, "makefile": second}))
    monkeypatch.setattr(materialize_mod, "os", _CaseFoldingOs())
    with pytest.raises(MaterializationError, match="makefile failed: File exists"):
        with _export(repo, commit):
            pytest.fail("the body must not run")
    assert not _leftovers(exports)


class _FullDiskOs(_OsProxy):
    @staticmethod
    def write(fd, data):
        raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))


def test_a_full_disk_is_a_materialization_failure(tmp_path, monkeypatch, exports):
    repo = _init(tmp_path / "repo")
    commit = _commit(repo, _tree(repo, {"a": b"a" * 1000, "b": b"b" * 1000}))
    monkeypatch.setattr(materialize_mod, "os", _FullDiskOs())
    with pytest.raises(MaterializationError, match="writing a failed: No space left on device"):
        with _export(repo, commit):
            pytest.fail("the body must not run")
    assert not _leftovers(exports)


def _cut_stream(keep: int):
    """A runner whose ``git cat-file --batch`` stream ends after ``keep`` bytes."""

    def runner(req: ExecutionRequest) -> ExecutionResult:
        hook = req.on_chunk
        if hook is not None and "--batch" in req.command:
            sent = 0

            def cut(stream: str, chunk: bytes) -> None:
                nonlocal sent
                if stream == "stdout":
                    chunk = chunk[: max(0, keep - sent)]
                    sent += len(chunk)
                if chunk:
                    hook(stream, chunk)

            req.on_chunk = cut
        return execute(req)

    return runner


@pytest.mark.parametrize("keep", [0, 10, 60, 100])
def test_a_stream_that_ends_early_is_a_materialization_failure(tmp_path, exports, keep):
    repo = _init(tmp_path / "repo")
    commit = _commit(repo, _tree(repo, {"a": b"a" * 50, "b": b"b" * 50}))
    with pytest.raises(MaterializationError, match="fewer blobs"):
        with _export(repo, commit, runner=_cut_stream(keep)):
            pytest.fail("the body must not run")
    assert not _leftovers(exports)


def test_a_stream_with_more_than_was_asked_is_a_materialization_failure(tmp_path):
    repo = _init(tmp_path / "repo")
    commit = _commit(repo, _tree(repo, {"a": b"a\n"}))

    def runner(req: ExecutionRequest) -> ExecutionResult:
        result = execute(req)
        if req.on_chunk is not None and "--batch" in req.command:
            req.on_chunk("stdout", b"x")
        return result

    with pytest.raises(MaterializationError, match="more blobs"):
        with _export(repo, commit, runner=runner):
            pass


def test_a_failing_git_process_is_a_materialization_failure(tmp_path, exports):
    repo = _init(tmp_path / "repo")
    commit = _commit(repo, _tree(repo, {"a": b"a\n"}))

    def runner(req: ExecutionRequest) -> ExecutionResult:
        if "--batch" in req.command:
            return ExecutionResult(req.command, req.cwd, 128, "", "fatal: boom", "t", "t")
        return execute(req)

    with pytest.raises(MaterializationError, match="streaming its blobs failed.*boom"):
        with _export(repo, commit, runner=runner):
            pytest.fail("the body must not run")
    assert not _leftovers(exports)


# -- identity ------------------------------------------------------------------------------
def test_a_commit_missing_from_the_store_is_not_materializable(tmp_path, monkeypatch, exports):
    repo = _init(tmp_path / "repo")
    _commit(repo, _tree(repo, {"a": b"a\n"}))
    writes = []
    monkeypatch.setattr(materialize_mod, "_write", lambda *a: writes.append(a))
    with pytest.raises(CandidateNotMaterializableError, match="not in the shared object store"):
        with _export(repo, "f" * 40):
            pytest.fail("the body must not run")
    assert not writes and not _leftovers(exports)
    assert issubclass(CandidateNotMaterializableError, VerificationError)
    assert issubclass(MaterializationError, VerificationError)


def test_a_blob_missing_from_the_store_is_not_materializable(tmp_path, exports):
    repo = _init(tmp_path / "repo")
    never_written = _out(repo, "hash-object", "--stdin", data=b"only hashed\n")
    tree = _mktree(repo, [("100644", "blob", never_written, b"ghost.txt")])
    commit = _commit(repo, tree)
    with pytest.raises(CandidateNotMaterializableError, match=f"{never_written} of ghost.txt"):
        with _export(repo, commit):
            pytest.fail("the body must not run")
    assert not _leftovers(exports)


@pytest.mark.parametrize("what", ["tag", "tree", "blob", "short", "upper"])
def test_only_a_full_commit_id_is_materializable(tmp_path, what):
    repo = _init(tmp_path / "repo")
    commit = _commit(repo, _tree(repo, {"a": b"a\n"}))
    named = {
        "tag": lambda: (
            _git(repo, *GIT_IDENT, "tag", "-a", "-m", "t", "v1", commit),
            _out(repo, "rev-parse", "v1"),
        )[1],
        "tree": lambda: _out(repo, "rev-parse", f"{commit}^{{tree}}"),
        "blob": lambda: _out(repo, "rev-parse", f"{commit}:a"),
        "short": lambda: commit[:12],
        "upper": lambda: commit.upper(),
    }[what]()
    with pytest.raises(CandidateNotMaterializableError):
        with _export(repo, named):
            pytest.fail("the body must not run")


def test_replacement_objects_cannot_change_what_is_written(tmp_path):
    repo = _init(tmp_path / "repo")
    commit = _commit(repo, _tree(repo, {"a.txt": b"committed\n"}))
    blob = _out(repo, "rev-parse", f"{commit}:a.txt")
    _git(repo, "update-ref", f"refs/replace/{blob}", _blob(repo, b"replaced\n"))
    assert _git(repo, "cat-file", "blob", blob) == b"replaced\n"  # the repository's own git
    with _export(repo, commit) as tree:
        assert (tree.root / "a.txt").read_bytes() == b"committed\n"


@pytest.fixture
def agent_worktree(tmp_path) -> dict[str, Path]:
    """A shared repository with an issue worktree whose objects go to a private directory."""
    root = _init(Path(os.path.realpath(tmp_path)) / "repo")
    _git(root, *GIT_IDENT, "commit", "-q", "--allow-empty", "-m", "base")
    common = root / ".git"
    worktree = common / "autoforge" / "worktrees" / "9"
    _git(root, "worktree", "add", "-q", "--detach", str(worktree), "HEAD")
    private = common / "autoforge" / "sandbox" / "9" / "objects"
    private.mkdir(parents=True)
    return {"root": root, "common": common, "worktree": worktree, "private": private}


def test_a_commit_only_in_a_private_object_directory_is_refused_until_imported(
    agent_worktree, exports
):
    repo = agent_worktree
    env = {
        "GIT_OBJECT_DIRECTORY": str(repo["private"]),
        "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(repo["common"] / "objects"),
    }
    (repo["worktree"] / "b.txt").write_bytes(b"the agent's work\n")
    _git(repo["worktree"], "add", "b.txt", env=env)
    _git(repo["worktree"], *GIT_IDENT, "commit", "-q", "-m", "work", env=env)
    head = _git(repo["worktree"], "rev-parse", "HEAD", env=env).decode().strip()

    with pytest.raises(CandidateNotMaterializableError, match="not in the shared object store"):
        with _export(repo["root"], head):
            pytest.fail("the body must not run")
    assert not _leftovers(exports)

    admin = repo["common"] / "worktrees" / "9"
    assert import_private_objects(repo["common"], admin, repo["private"], None) == head
    with _export(repo["root"], head) as tree:
        assert (tree.root / "b.txt").read_bytes() == b"the agent's work\n"


# -- hostile names -------------------------------------------------------------------------
@pytest.mark.parametrize("nested", [False, True], ids=["at-the-root", "below-a-tree"])
@pytest.mark.parametrize(
    ("name", "kind"),
    [
        (b"..", "blob"),
        (b".", "blob"),
        (b".git", "blob"),
        (b".GIT", "blob"),
        (b".Git", "blob"),
        (b".git", "tree"),
        (b"..", "tree"),
    ],
)
def test_a_hostile_entry_name_is_refused_before_anything_is_written(
    tmp_path, monkeypatch, exports, name, kind, nested
):
    repo = _init(tmp_path / "repo")
    blob = _blob(repo, b"[core]\n\thooksPath = /tmp\n")
    if kind == "tree":
        entry = ("040000", "tree", _mktree(repo, [("100644", "blob", blob, b"config")]), name)
    else:
        entry = ("100644", "blob", blob, name)
    tree = _mktree(repo, [entry, ("100644", "blob", blob, b"ok.txt")])
    if nested:
        tree = _mktree(repo, [("040000", "tree", tree, b"sub")])
    commit = _commit(repo, tree)
    writes = []
    monkeypatch.setattr(materialize_mod, "_write", lambda *a: writes.append(a))
    with pytest.raises(MaterializationError, match="is refused") as info:
        with _export(repo, commit):
            pytest.fail("the body must not run")
    assert name.decode() in str(info.value)
    assert not writes and not _leftovers(exports)


@pytest.mark.parametrize("name", [b"a/b", b"../escape", b"x/../../escape", b"/abs"])
def test_a_name_holding_a_slash_is_refused_before_anything_is_written(
    tmp_path, monkeypatch, exports, name
):
    repo = _init(tmp_path / "repo")
    blob = _blob(repo, b"x\n")
    commit = _commit(repo, _literal_tree(repo, [(b"100644", name, blob)]))
    writes = []
    monkeypatch.setattr(materialize_mod, "_write", lambda *a: writes.append(a))
    with pytest.raises(MaterializationError, match="is refused"):
        with _export(repo, commit):
            pytest.fail("the body must not run")
    assert not writes and not _leftovers(exports)


def test_a_name_holding_a_slash_inside_a_listed_tree_fails_the_proof(tmp_path, exports):
    """``ls-tree`` prints ``a/b`` as if ``b`` were in the tree ``a``; the tree id says otherwise."""
    repo = _init(tmp_path / "repo")
    blob = _blob(repo, b"x\n")
    inner = _mktree(repo, [("100644", "blob", blob, b"a")])
    commit = _commit(
        repo, _literal_tree(repo, [(b"40000", b"a", inner), (b"100644", b"a/b", blob)])
    )
    with pytest.raises(MaterializationError, match="does not hash back"):
        with _export(repo, commit):
            pytest.fail("the body must not run")
    assert not _leftovers(exports)


def test_an_empty_name_is_refused(tmp_path, exports):
    repo = _init(tmp_path / "repo")
    commit = _commit(repo, _literal_tree(repo, [(b"100644", b"", _blob(repo, b"x\n"))]))
    with pytest.raises(MaterializationError):
        with _export(repo, commit):
            pytest.fail("the body must not run")
    assert not _leftovers(exports)


# -- cleanup -------------------------------------------------------------------------------
def test_the_directory_is_removed_after_success_and_after_an_exception(tmp_path, exports):
    repo = _init(tmp_path / "repo")
    commit = _commit(repo, _tree(repo, {"a": b"a\n", "d": {"b": b"b\n"}}))
    with _export(repo, commit) as tree:
        first = tree.root
        (first / "written-by-a-command").write_bytes(b"")
    assert not first.exists()
    with pytest.raises(RuntimeError, match="boom"):
        with _export(repo, commit) as tree:
            second = tree.root
            raise RuntimeError("boom")
    assert not second.exists() and second != first
    assert not _leftovers(exports)


def test_an_empty_tree_exports_as_an_empty_directory(tmp_path):
    repo = _init(tmp_path / "repo")
    commit = _commit(repo, _tree(repo, {}))
    with _export(repo, commit) as tree:
        assert not list(tree.root.iterdir())
        assert (tree.files, tree.gitlinks) == (0, 0)
