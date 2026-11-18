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
import re
import shutil
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
from autoforge.materialize import (
    METHOD,
    MaterializedTree,
    TreeDifference,
    materialize,
    materialize_into,
    read_object_format,
    tree_differences,
)
from autoforge.safefs import SafeRoot

from .conftest import GIT_IDENT, rewrite_loose_object, watched_fifo

FORMATS = ["sha1", "sha256"]


# -- fixtures ------------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def exports(tmp_path, monkeypatch) -> Path:
    """Where ``tempfile.mkdtemp`` creates the exports during a test."""
    where = tmp_path / "exports"
    where.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(where))
    return where


def _raw_name(directory: Path) -> bytes:
    """A file name that is not UTF-8 (``caf\\xe9-\\xff.bin``) where the file
    system under ``directory`` takes one, as Linux's do. APFS refuses it with
    ``EILSEQ`` (macOS, #501), so no tree holding it can be exported there;
    there the name is its UTF-8 spelling, still not ASCII."""
    latin = b"caf\xe9-\xff.bin"
    probe = os.path.join(os.fsencode(directory), latin)
    try:
        os.close(os.open(probe, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
    except OSError as exc:
        if exc.errno != errno.EILSEQ:
            raise
        return "café-ÿ.bin".encode()
    os.unlink(probe)
    return latin


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
    latin = _raw_name(exports)  # not valid UTF-8 where the file system takes one
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

    def write_then_tamper(runner, repo_, commit_, listing, root, *check):
        write(runner, repo_, commit_, listing, root, *check)
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


def test_the_callers_check_runs_on_each_directory_before_anything_is_written_in_it(tmp_path):
    """``materialize_into``'s check (#292, PR #504 R2-F1): every directory the writer uses, on
    its held descriptor, before its mode is set or anything is made in it; one refused stays
    as its ``mkdir`` left it, empty, and the writer stops there."""
    repo = _init(tmp_path / "repo")
    absent = "e" * 40
    commit = _commit(
        repo,
        _tree(
            repo,
            {
                "README": b"top\n",
                "src": {"a.py": b"a\n", "deep": {"b.py": b"b\n"}},
                "vendor": {"lib": Gitlink(absent)},
                "z.txt": b"last\n",
            },
        ),
    )
    seen: list[tuple[bytes, list[str], int]] = []

    def recording(refuse: bytes):
        def check(fd: int, path: bytes) -> str:
            info = os.fstat(fd)
            assert stat.S_ISDIR(info.st_mode)
            seen.append((path, sorted(os.listdir(fd)), stat.S_IMODE(info.st_mode)))
            return "refused by the test" if path == refuse else ""

        return check

    old = os.umask(0o077)
    try:
        whole = tmp_path / "whole"
        whole.mkdir(mode=0o700)
        with SafeRoot.open(whole) as root:
            tree = materialize_into(
                execute, str(repo), commit, "sha1", root, check_directory=recording(b"none")
            )
        assert tree.files == 4 and tree.gitlinks == 1
        # Each directory is checked empty, with its mkdir's mode (never set yet).
        assert seen == [
            (b"", [], 0o700),
            (b"src", [], 0o700),
            (b"src/deep", [], 0o700),
            (b"vendor", [], 0o700),
            (b"vendor/lib", [], 0o700),
        ]
        assert stat.S_IMODE((whole / "src" / "deep").stat().st_mode) == 0o755
        seen.clear()
        cut = tmp_path / "cut"
        cut.mkdir(mode=0o700)
        with SafeRoot.open(cut) as root:
            with pytest.raises(
                MaterializationError,
                match="the directory src/deep was refused before anything was written in it: "
                "refused by the test",
            ):
                materialize_into(
                    execute, str(repo), commit, "sha1", root, check_directory=recording(b"src/deep")
                )
    finally:
        os.umask(old)
    assert [path for path, _, _ in seen] == [b"", b"src", b"src/deep"]
    deep = cut / "src" / "deep"
    assert list(deep.iterdir()) == []
    assert stat.S_IMODE(deep.stat().st_mode) == 0o700  # its mode was never set
    assert not (cut / "src" / "deep" / "b.py").exists() and not (cut / "vendor").exists()


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


# -- comparison (PR #444 R3-F1) ------------------------------------------------------------
@pytest.mark.parametrize("object_format", FORMATS)
def test_a_directory_is_compared_with_the_tree_blob_by_blob(tmp_path, exports, object_format):
    """A proven export holds every blob as committed, and the empty directory
    of an unpopulated submodule at its gitlink: no difference, and an added
    file is not looked at. A file at the gitlink's path populates it (PR
    #444 R4-F1). A changed byte, a flipped execute bit, a retargeted link, a
    deletion and a file replaced by a directory are listed by their raw
    paths, in listing order."""
    repo = _init(tmp_path / "repo", object_format)
    absent = "e" * (40 if object_format == "sha1" else 64)
    latin = _raw_name(exports)
    commit = _commit(
        repo,
        _tree(
            repo,
            {
                "run.sh": Exe(b"#!/bin/sh\necho hi\n"),
                "plain.txt": b"plain\n",
                "empty.txt": b"",
                "relative": Link(b"plain.txt"),
                latin: b"a name that is not UTF-8\n",
                "vendor": {"lib": Gitlink(absent)},
                "a": {"x": b"x\n"},
            },
        ),
    )
    with _export(repo, commit) as tree:
        root = tree.root

        def differences() -> list[TreeDifference]:
            return tree_differences(execute, str(repo), commit, object_format, root)

        assert differences() == []
        (root / "untracked.txt").write_text("not in the tree\n")
        assert differences() == []
        (root / "vendor" / "lib" / "module.txt").write_text("the submodule's\n")
        assert differences() == [TreeDifference(b"vendor/lib", "populated", submodule=True)]
        (root / "vendor" / "lib" / "module.txt").unlink()
        (root / "run.sh").chmod(0o644)
        with open(os.path.join(os.fsencode(root), latin), "r+b") as handle:
            handle.write(b"A")
        (root / "relative").unlink()
        os.symlink("empty.txt", root / "relative")
        (root / "plain.txt").unlink()
        (root / "empty.txt").unlink()
        (root / "empty.txt").mkdir()
        assert differences() == [
            TreeDifference(latin, "modified"),
            TreeDifference(b"empty.txt", "retyped"),
            TreeDifference(b"plain.txt", "deleted"),
            TreeDifference(b"relative", "modified"),
            TreeDifference(b"run.sh", "modified"),
        ]


@pytest.mark.parametrize(
    ("what", "kind"),
    [
        ("empty", ""),
        ("a repository", "populated"),
        ("a file", "populated"),
        ("an empty subdirectory", "populated"),
        ("absent", "deleted"),
        ("a link to an empty directory", "retyped"),
        ("a regular file", "retyped"),
        ("a fifo", "retyped"),
        ("a link above it", "deleted"),
    ],
)
def test_a_gitlink_matches_only_as_an_empty_real_directory(tmp_path, exports, what, kind):
    """PR #444 R4-F1: a checkout leaves an empty directory at a submodule it
    does not populate. Anything in it, a repository or not, is content the
    commit's tree does not prove; anything but a real directory there, or
    a link on the way to it, is not the commit's either. Nothing at the
    path is opened but the directory itself, nothing through a link."""
    repo = _init(tmp_path / "repo")
    commit = _commit(
        repo,
        _tree(repo, {"third_party": {"vendor": Gitlink("e" * 40)}, "a.txt": b"a\n"}),
    )
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "vendor").mkdir(parents=True)
    with _export(repo, commit) as tree:
        root = tree.root
        gitlink = root / "third_party" / "vendor"
        assert gitlink.is_dir() and not any(gitlink.iterdir())
        opened: list[str] = []
        if what == "a repository":
            subprocess.run(["git", "init", "-q", str(gitlink)], check=True)
        elif what == "a file":
            (gitlink / "cache.py").write_text("TTL_SECONDS = 300\n")
        elif what == "an empty subdirectory":
            (gitlink / "pkg").mkdir()
        elif what == "absent":
            gitlink.rmdir()
        elif what == "a link to an empty directory":
            gitlink.rmdir()
            os.symlink(elsewhere / "vendor", gitlink)
        elif what == "a regular file":
            gitlink.rmdir()
            gitlink.write_text("not a directory\n")
        elif what == "a link above it":
            shutil.rmtree(root / "third_party")
            os.symlink(elsewhere, root / "third_party")
        expected = [TreeDifference(b"third_party/vendor", kind, submodule=True)] if kind else []
        if what == "a fifo":
            gitlink.rmdir()
            with watched_fifo(gitlink) as opened:
                assert tree_differences(execute, str(repo), commit, "sha1", root) == expected
        else:
            assert tree_differences(execute, str(repo), commit, "sha1", root) == expected
        assert opened == []


# -- the commit's own end-of-line checkout form (#488) --------------------------------------
def _checkout(repo: Path, commit: str, where: Path) -> Path:
    """Git's own checkout of ``commit`` into ``where``, by the commit's attributes alone.

    No configuration and no attributes file but the commit's: what a
    checkout of the commit writes on any machine.
    """
    index = where.parent / f"{where.name}-index"
    env = {
        "GIT_INDEX_FILE": str(index),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_ATTR_NOSYSTEM": "1",
    }
    pure = ("-c", f"core.attributesFile={os.devnull}")
    _git(repo, *pure, "read-tree", commit, env=env)
    _git(repo, *pure, "checkout-index", "-a", f"--prefix={where}/", env=env)
    index.unlink()
    return where


def _crlf(content: bytes) -> bytes:
    return re.sub(rb"(?<!\r)\n", b"\r\n", content)


EOL_ATTRIBUTES = (
    b"*.bat text eol=crlf\n"
    b"*.cmd eol=crlf\n"
    b"*.auto text=auto eol=crlf\n"
    b"*.legacy crlf eol=crlf\n"
    b"[attr]windows text eol=crlf\n"
    b"*.win windows\n"
    b"*.bin -text eol=crlf\n"
    b"*.dat binary eol=crlf\n"
)


@pytest.mark.parametrize("object_format", FORMATS)
@pytest.mark.parametrize("size", [None, 1, 3])
def test_a_checkout_under_the_commits_own_eol_crlf_is_no_difference(
    tmp_path, exports, object_format, size
):
    """#488: git's checkout of a commit whose own `.gitattributes` say
    `eol=crlf` writes CRLF where the blob holds LF. The raw comparison
    lists every such file; with `checkout_eol` none is a difference, for
    each way the attributes say it (`text eol=crlf`, `eol=crlf` alone, the
    legacy `crlf`, a macro, `text=auto` where git takes the blob as text, a
    deeper `.gitattributes`), whatever the chunks the blobs arrive in. The
    files git leaves as committed (`-text`, `binary`, a `text=auto` blob
    that holds a CRLF already or a NUL) need no second form."""
    repo = _init(tmp_path / "repo", object_format)
    commit = _commit(
        repo,
        _tree(
            repo,
            {
                ".gitattributes": EOL_ATTRIBUTES,
                "gradlew.bat": b"@echo off\necho hi\n",
                "run.bat": Exe(b"@echo off\r\nmixed\nendings\r\n"),
                "lone-cr.bat": b"a\rb\n",
                "plain.cmd": b"one\ntwo",
                "text.auto": b"a\nb\n",
                "crlf.auto": b"a\r\nb\n",
                "nul.auto": b"a\0b\n",
                "old.legacy": b"x\n",
                "macro.win": b"y\n",
                "raw.bin": b"z\n",
                "raw.dat": b"w\n",
                "plain.txt": b"untouched\n",
                "sub": {
                    ".gitattributes": b"*.bat -text\n",
                    "kept.bat": b"lf\n",
                    "deep.cmd": b"c\n",
                },
            },
        ),
    )
    root = _checkout(repo, commit, tmp_path / "checkout")
    assert (root / "gradlew.bat").read_bytes() == b"@echo off\r\necho hi\r\n"
    assert (root / "run.bat").read_bytes() == b"@echo off\r\nmixed\r\nendings\r\n"
    assert os.access(root / "run.bat", os.X_OK)
    assert (root / "nul.auto").read_bytes() == b"a\0b\n"
    assert (root / "sub" / "kept.bat").read_bytes() == b"lf\n"
    runner = execute if size is None else _rechunking(size)
    converted = [
        b"gradlew.bat",
        b"lone-cr.bat",
        b"macro.win",
        b"old.legacy",
        b"plain.cmd",
        b"run.bat",
        b"sub/deep.cmd",
        b"text.auto",
    ]
    raw = tree_differences(runner, str(repo), commit, object_format, root)
    assert raw == [TreeDifference(path, "modified") for path in converted]
    assert tree_differences(runner, str(repo), commit, object_format, root, checkout_eol=True) == []
    assert not _leftovers(exports)


def test_a_text_value_check_attr_prints_like_a_state_is_read_the_stricter_way(tmp_path, exports):
    """`check-attr` prints `text=set` as it prints `text`, and `text=unset` as
    `-text`, while git takes both values as unspecified and falls back to
    `crlf`. Read both ways, the stricter answer is kept: git's checkout of
    each is no difference or a refusal (`text=unset` with `eol=crlf`, which
    git converts), and the CRLF form of a file git left as committed is a
    difference, never taken as the commit's."""
    repo = _init(tmp_path / "repo")
    commit = _commit(
        repo,
        _tree(
            repo,
            {
                ".gitattributes": (
                    b"set-binary.x text=set -crlf eol=crlf\n"
                    b"unset.x text=unset eol=crlf\n"
                    b"set-auto.x text=set crlf=auto eol=crlf\n"
                    b"set.x text=set eol=crlf\n"
                ),
                "set-binary.x": b"a\nb\n",
                "unset.x": b"a\nb\n",
                "set-auto.x": b"a\nb\n",
                "set.x": b"a\nb\n",
            },
        ),
    )
    root = _checkout(repo, commit, tmp_path / "checkout")
    assert (root / "set-binary.x").read_bytes() == b"a\nb\n"
    for converted in ("unset.x", "set-auto.x", "set.x"):
        assert (root / converted).read_bytes() == b"a\r\nb\r\n"
    found = tree_differences(execute, str(repo), commit, "sha1", root, checkout_eol=True)
    assert found == [TreeDifference(b"unset.x", "modified")]
    (root / "set-binary.x").write_bytes(b"a\r\nb\r\n")
    found = tree_differences(execute, str(repo), commit, "sha1", root, checkout_eol=True)
    assert found == [
        TreeDifference(b"set-binary.x", "modified"),
        TreeDifference(b"unset.x", "modified"),
    ]


def _hostile_eol(tmp_path: Path, monkeypatch, repo: Path, root: Path, source: str) -> None:
    """``eol=crlf`` for every `.bat` file from ``source``, which a checkout reads too."""
    rule = b"*.bat text eol=crlf\n"
    if source == "info/attributes":
        (repo / ".git" / "info").mkdir(exist_ok=True)
        (repo / ".git" / "info" / "attributes").write_bytes(rule)
    elif source == "core.attributesFile":
        (tmp_path / "attributes").write_bytes(rule)
        _git(repo, "config", "core.attributesFile", str(tmp_path / "attributes"))
    elif source == "the global attributes file":
        (tmp_path / "xdg" / "git").mkdir(parents=True)
        (tmp_path / "xdg" / "git" / "attributes").write_bytes(rule)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    elif source == "core.autocrlf":
        _git(repo, "config", "core.autocrlf", "true")
    elif source == "core.eol":
        (repo / ".git" / "info").mkdir(exist_ok=True)
        (repo / ".git" / "info" / "attributes").write_bytes(b"*.bat text\n")
        _git(repo, "config", "core.eol", "crlf")
    else:  # an untracked .gitattributes in the file's directory of the checkout
        (root / "bin" / ".gitattributes").write_bytes(rule)


@pytest.mark.parametrize(
    "source",
    [
        "info/attributes",
        "core.attributesFile",
        "the global attributes file",
        "core.autocrlf",
        "core.eol",
        "an untracked .gitattributes",
    ],
)
def test_an_end_of_line_conversion_the_commit_does_not_name_is_a_difference(
    tmp_path, monkeypatch, exports, source
):
    """The attributes and configuration a checkout also reads are the
    agent's to write (#488): a CRLF file whose `eol=crlf` comes from any of
    them, and not from the commit's own `.gitattributes`, is still a
    difference. Git itself reads the file as unchanged there. The commit
    has a `.gitattributes` on the file's path that converts nothing, so
    `check-attr` runs, in the controller's own git directory, rather than
    nothing being read at all (PR #503 R1-F1)."""
    repo = _init(tmp_path / "repo")
    commit = _commit(
        repo,
        _tree(
            repo,
            {
                ".gitattributes": b"*.txt -text\n",
                "bin": {"gradlew.bat": b"@echo off\necho hi\n"},
                "a.txt": b"a\n",
            },
        ),
    )
    root = _checkout(repo, commit, tmp_path / "checkout")
    (root / "bin" / "gradlew.bat").write_bytes(b"@echo off\r\necho hi\r\n")
    _hostile_eol(tmp_path, monkeypatch, repo, root, source)
    if source != "an untracked .gitattributes":
        clean = _git(
            repo, "hash-object", "--path=bin/gradlew.bat", str(root / "bin" / "gradlew.bat")
        )
        assert clean.decode().strip() == _out(repo, "rev-parse", f"{commit}:bin/gradlew.bat")
    seen: list[ExecutionRequest] = []
    found = tree_differences(_recording(seen), str(repo), commit, "sha1", root, checkout_eol=True)
    assert found == [TreeDifference(b"bin/gradlew.bat", "modified")]
    assert any("check-attr" in req.command for req in seen)
    assert not _leftovers(exports)


def test_under_the_commits_own_eol_crlf_only_its_checkout_form_is_no_difference(tmp_path, exports):
    """The second form is exactly what git's checkout writes: an edit in
    CRLF form, a flipped execute bit, a link to a file holding the form, a
    file with only some of its LFs converted, the converted form of a
    `text=auto` blob git leaves alone (a NUL, a CRLF already), a deletion
    and a directory replaced by a link to a copy are all still differences."""
    repo = _init(tmp_path / "repo")
    commit = _commit(
        repo,
        _tree(
            repo,
            {
                ".gitattributes": b"*.bat text eol=crlf\n*.auto text=auto eol=crlf\n",
                "crlf.auto": b"a\r\nb\n",
                "dir": {"x.bat": b"x\n"},
                "edit.bat": b"echo a\n",
                "gone.bat": b"g\n",
                "half.bat": b"a\nb\n",
                "link.bat": b"echo c\n",
                "mode.bat": b"echo b\n",
                "nul.auto": b"a\0b\n",
            },
        ),
    )
    root = _checkout(repo, commit, tmp_path / "checkout")
    assert tree_differences(execute, str(repo), commit, "sha1", root, checkout_eol=True) == []
    (root / "edit.bat").write_bytes(b"echo EDITED\r\n")
    (root / "mode.bat").chmod(0o755)
    (tmp_path / "form.bat").write_bytes((root / "link.bat").read_bytes())
    (root / "link.bat").unlink()
    os.symlink(tmp_path / "form.bat", root / "link.bat")
    (root / "half.bat").write_bytes(b"a\r\nb\n")
    (root / "nul.auto").write_bytes(b"a\0b\r\n")
    (root / "crlf.auto").write_bytes(b"a\r\nb\r\n")
    (root / "gone.bat").unlink()
    shutil.copytree(root / "dir", tmp_path / "copy")
    shutil.rmtree(root / "dir")
    os.symlink(tmp_path / "copy", root / "dir")
    assert tree_differences(execute, str(repo), commit, "sha1", root, checkout_eol=True) == [
        TreeDifference(b"crlf.auto", "modified"),
        TreeDifference(b"dir/x.bat", "deleted"),
        TreeDifference(b"edit.bat", "modified"),
        TreeDifference(b"gone.bat", "deleted"),
        TreeDifference(b"half.bat", "modified"),
        TreeDifference(b"link.bat", "retyped"),
        TreeDifference(b"mode.bat", "modified"),
        TreeDifference(b"nul.auto", "modified"),
    ]


@pytest.mark.parametrize("size", [None, 1, 2, 7])
def test_the_checkout_form_is_gits_whatever_the_bytes(tmp_path, exports, size):
    """Git's checkout is the oracle: random blobs of LF, CR, CRLF, NUL, `^Z`,
    DEL and other bytes, and the edges of `text=auto`'s binary test (one
    non-printable byte in 128 printable ones is text, in 127 binary; a final
    `^Z` is not counted), each under `text eol=crlf` and `text=auto
    eol=crlf`, in chunks of any size. Every file git wrote is no difference;
    where git left a `text=auto` blob with a lone LF as it was, the
    converted form is one."""
    rng = random.Random(488)
    alphabet = b"ab\r\n\n\r\n\x00\x1a\x7f\x01\xc3\t"
    contents = [bytes(rng.choice(alphabet) for _ in range(rng.randrange(0, 80))) for _ in range(40)]
    contents += [
        b"a" * 128 + b"\x01\n",
        b"a" * 127 + b"\x01\n",
        b"a" * 10 + b"\n\x1a",
        b"a" * 10 + b"\x1a\n",
        b"\n" * 5,
        b"\r\n\n\r\r\n\n",
    ]
    files = {}
    for i, content in enumerate(contents):
        files[f"f{i:02d}.t"] = content
        files[f"f{i:02d}.a"] = content
    files[".gitattributes"] = b"*.t text eol=crlf\n*.a text=auto eol=crlf\n"
    repo = _init(tmp_path / "repo")
    commit = _commit(repo, _tree(repo, files))
    root = _checkout(repo, commit, tmp_path / "checkout")
    runner = execute if size is None else _rechunking(size)
    assert tree_differences(runner, str(repo), commit, "sha1", root, checkout_eol=True) == []
    left = []
    for i, content in enumerate(contents):
        name = f"f{i:02d}.a"
        if (root / name).read_bytes() == content and _crlf(content) != content:
            (root / name).write_bytes(_crlf(content))
            left.append(TreeDifference(name.encode(), "modified"))
    assert len(left) > 5
    assert tree_differences(runner, str(repo), commit, "sha1", root, checkout_eol=True) == left


def _recording(seen: list[ExecutionRequest]):
    def runner(req: ExecutionRequest) -> ExecutionResult:
        seen.append(req)
        return execute(req)

    return runner


def test_the_attributes_are_read_in_a_git_directory_of_the_controllers_own(tmp_path, exports):
    """No git process reads the checkout, and none runs for the attributes
    unless a file's bytes differ. Then `update-index` and `check-attr` run
    in a temporary git directory with no configuration, no global or system
    attributes and `core.attributesFile` at `/dev/null`, whose object
    directory is its own, not the repository's store (PR #503 R1-F1); it is
    removed afterwards, and nothing is written into the repository."""
    repo = _init(tmp_path / "repo")
    commit = _commit(
        repo, _tree(repo, {".gitattributes": b"*.bat eol=crlf\n", "a.bat": b"a\n", "b.txt": b"b\n"})
    )
    root = _checkout(repo, commit, tmp_path / "checkout")
    (root / "a.bat").write_bytes(b"a\n")
    before = sorted(p.relative_to(repo) for p in (repo / ".git").rglob("*"))
    seen: list[ExecutionRequest] = []
    assert (
        tree_differences(_recording(seen), str(repo), commit, "sha1", root, checkout_eol=True) == []
    )
    assert not any({"update-index", "check-attr"} & set(req.command) for req in seen)
    seen.clear()
    (root / "a.bat").write_bytes(b"a\r\n")
    (root / "b.txt").write_bytes(b"edited\n")
    found = tree_differences(_recording(seen), str(repo), commit, "sha1", root, checkout_eol=True)
    assert found == [TreeDifference(b"b.txt", "modified")]
    attributes = [req for req in seen if {"update-index", "check-attr"} & set(req.command)]
    assert len(attributes) == 2
    for req in seen:
        assert not any(str(root) in arg for arg in req.command)
        assert req.cwd is None or str(root) not in req.cwd
    for req in attributes:
        env = req.env or {}
        assert f"core.attributesFile={os.devnull}" in req.command
        assert env["GIT_CONFIG_GLOBAL"] == os.devnull
        assert env["GIT_CONFIG_NOSYSTEM"] == env["GIT_ATTR_NOSYSTEM"] == "1"
        assert env["GIT_OBJECT_DIRECTORY"] == str(Path(env["GIT_DIR"]) / "objects")
        assert Path(env["GIT_DIR"]).parent == Path(req.cwd or "")
        assert Path(env["GIT_DIR"]).is_relative_to(exports)
        assert Path(env["GIT_INDEX_FILE"]).is_relative_to(exports)
    assert not _leftovers(exports)
    assert sorted(p.relative_to(repo) for p in (repo / ".git").rglob("*")) == before


def _failing(command: str, stdout: str = "", code: int = 128):
    def runner(req: ExecutionRequest) -> ExecutionResult:
        if command in req.command:
            return ExecutionResult(req.command, req.cwd, code, stdout, "fatal: boom", "t", "t")
        return execute(req)

    return runner


def _rewriting_blobs(old: bytes, new: bytes):
    """A runner whose ``git cat-file --batch`` stream holds ``new`` where the store has ``old``."""

    def runner(req: ExecutionRequest) -> ExecutionResult:
        hook = req.on_chunk
        if hook is None or "--batch" not in req.command:
            return execute(req)
        held = bytearray()
        req.on_chunk = lambda stream, chunk: held.extend(chunk) if stream == "stdout" else None
        result = execute(req)
        hook("stdout", bytes(held).replace(old, new))
        return result

    return runner


@pytest.mark.parametrize(
    ("runner", "message"),
    [
        (_failing("check-attr"), "reading its end-of-line attributes failed.*boom"),
        (_failing("update-index"), "reading its end-of-line attributes failed.*boom"),
        (_failing("check-attr", code=0), "`git check-attr` did not answer"),
        (_rewriting_blobs(b"echo a", b"echo b"), "that are not its"),
        (_failing("--batch"), "streaming its blobs failed.*boom"),
    ],
    ids=["check-attr fails", "update-index fails", "no answer", "other bytes", "cat-file fails"],
)
def test_an_unreadable_checkout_form_is_a_failure_never_no_difference(
    tmp_path, exports, runner, message
):
    """Attributes git did not answer, or a blob git streamed as other bytes
    than its id's, prove nothing: the comparison fails, never takes the
    file as the commit's."""
    repo = _init(tmp_path / "repo")
    commit = _commit(
        repo, _tree(repo, {".gitattributes": b"*.bat eol=crlf\n", "a.bat": b"echo a\n"})
    )
    root = _checkout(repo, commit, tmp_path / "checkout")
    with pytest.raises(MaterializationError, match=message):
        tree_differences(runner, str(repo), commit, "sha1", root, checkout_eol=True)
    assert not _leftovers(exports)


# The reviewer's reproduction (PR #503 R1-F1): a committed rule that leaves
# `.txt` files as committed, and a rule of the same size that converts them,
# which an agent writes into the rule's loose object in the shared store.
COMMITTED_RULE = b"*.txt -text" + b" " * 8 + b"\n"
FORGED_RULE = b"*.txt text eol=crlf\n"


def _store_attributes(repo: Path, commit: str, path: str) -> bytes:
    """What git's attribute machinery reads for ``path`` of ``commit`` through ``repo``'s store."""
    env = {"GIT_INDEX_FILE": str(repo.parent / "store-index")}
    _git(repo, "read-tree", commit, env=env)
    found = _git(repo, "check-attr", "--cached", "text", "eol", "--", path, env=env)
    Path(env["GIT_INDEX_FILE"]).unlink()
    return found


def _tree_body(entries: list[tuple[bytes, bytes, str]]) -> bytes:
    """The bytes of a tree object of ``(mode, name, id)`` entries, in git's order."""
    entries = sorted(entries, key=lambda e: e[1] + b"/" if e[0] == b"40000" else e[1])
    return b"".join(mode + b" " + name + b"\0" + bytes.fromhex(oid) for mode, name, oid in entries)


@pytest.mark.parametrize("object_format", FORMATS)
def test_an_attributes_blob_rewritten_in_the_store_authorizes_no_crlf_form(
    tmp_path, exports, object_format
):
    """PR #503 R1-F1: git does not re-hash the `.gitattributes` blob it
    reads, so once its loose object holds a same-size rule that converts,
    git's own attribute read of the commit says `eol=crlf`, although the
    commit says `-text`. The controller reads the rule only from the bytes
    it re-hashed: they are not the blob's, and the comparison fails, never
    taking the CRLF file as the commit's."""
    assert len(COMMITTED_RULE) == len(FORGED_RULE)
    repo = _init(tmp_path / "repo", object_format)
    commit = _commit(repo, _tree(repo, {".gitattributes": COMMITTED_RULE, "notes.txt": b"a\nb\n"}))
    root = _checkout(repo, commit, tmp_path / "checkout")
    assert (root / "notes.txt").read_bytes() == b"a\nb\n"
    (root / "notes.txt").write_bytes(b"a\r\nb\r\n")
    rule = _out(repo, "rev-parse", f"{commit}:.gitattributes")
    rewrite_loose_object(repo / ".git" / "objects", rule, "blob", FORGED_RULE)
    assert _git(repo, "cat-file", "blob", rule) == FORGED_RULE
    assert b"eol: crlf" in _store_attributes(repo, commit, "notes.txt")
    assert tree_differences(execute, str(repo), commit, object_format, root) == [
        TreeDifference(b"notes.txt", "modified")
    ]
    forged = f"the blob of .gitattributes that are not its {rule}"
    with pytest.raises(MaterializationError, match=forged):
        tree_differences(execute, str(repo), commit, object_format, root, checkout_eol=True)
    assert not _leftovers(exports)


@pytest.mark.parametrize("forgery", ["swapped", "dropped"])
@pytest.mark.parametrize("depth", [1, 2])
def test_a_tree_rewritten_in_the_store_authorizes_no_crlf_form(tmp_path, exports, forgery, depth):
    """PR #503 R1-F1: git re-hashes a tree it is named, but not the trees
    below it it reads, so a subtree on a file's path rewritten under its
    id, its `.gitattributes` entry swapped for a blob that converts or
    dropped so that a parent's rule converts, makes git's attribute read of
    the commit say `eol=crlf`. Each directory on the path must hash back to
    its tree from the entries the store lists for it: the forged one does
    not, and the comparison fails."""
    repo = _init(tmp_path / "repo")
    leaf: dict = {".gitattributes": COMMITTED_RULE, "notes.txt": b"a\nb\n"}
    spec: dict = {".gitattributes": b"*.txt text eol=crlf\n", "docs": leaf}
    directory = "docs"
    if depth == 2:
        spec["docs"] = {"api": leaf, "index.txt": b"i"}
        directory = "docs/api"
    path = f"{directory}/notes.txt"
    commit = _commit(repo, _tree(repo, spec))
    root = _checkout(repo, commit, tmp_path / "checkout")
    assert (root / path).read_bytes() == b"a\nb\n"
    (root / path).write_bytes(b"a\r\nb\r\n")
    tree = _out(repo, "rev-parse", f"{commit}:{directory}")
    entries = []
    for record in _git(repo, "ls-tree", "-z", tree).split(b"\0")[:-1]:
        meta, _, name = record.partition(b"\t")
        mode, _, oid = meta.split(b" ")
        if name == b".gitattributes":
            if forgery == "dropped":
                continue
            oid = _blob(repo, FORGED_RULE).encode()
        entries.append((mode.lstrip(b"0"), name, oid.decode()))
    rewrite_loose_object(repo / ".git" / "objects", tree, "tree", _tree_body(entries))
    assert b"eol: crlf" in _store_attributes(repo, commit, path)
    listed_for = f"the entries the store lists for {directory} hash to the tree"
    with pytest.raises(MaterializationError, match=re.escape(listed_for)):
        tree_differences(execute, str(repo), commit, "sha1", root, checkout_eol=True)
    assert not _leftovers(exports)


@pytest.mark.parametrize("checkout_eol", [False, True])
def test_a_commit_rewritten_in_the_store_names_no_other_tree(tmp_path, exports, checkout_eol):
    """PR #503 R1-F1, ADR 0004 D7.5: the commit's loose object rewritten to
    name another tree, whose attributes convert and whose blob is the CRLF
    edit, is not read: `cat-file` reads it as the forged bytes, and the
    tree id comes from the commit's bytes only once they hash to its id,
    whichever git re-hashes what, so the comparison fails, as the export
    does."""
    repo = _init(tmp_path / "repo")
    commit = _commit(repo, _tree(repo, {".gitattributes": COMMITTED_RULE, "notes.txt": b"a\n"}))
    root = _checkout(repo, commit, tmp_path / "checkout")
    (root / "notes.txt").write_bytes(b"a\r\n")
    (root / ".gitattributes").write_bytes(FORGED_RULE)
    forged = _tree(repo, {".gitattributes": FORGED_RULE, "notes.txt": b"a\r\n"})
    raw = _git(repo, "cat-file", "commit", commit)
    tree = _out(repo, "rev-parse", f"{commit}^{{tree}}")
    rewrite_loose_object(
        repo / ".git" / "objects", commit, "commit", raw.replace(tree.encode(), forged.encode())
    )
    assert forged.encode() in _git(repo, "cat-file", "commit", commit)
    with pytest.raises(MaterializationError, match="do not hash to its id"):
        tree_differences(execute, str(repo), commit, "sha1", root, checkout_eol=checkout_eol)
    with pytest.raises(MaterializationError, match="do not hash to its id"):
        with _export(repo, commit):
            pytest.fail("the body must not run")
    assert not _leftovers(exports)


def test_a_store_rewritten_after_the_copy_cannot_change_the_attributes_read(tmp_path, exports):
    """PR #503 R1-F1: the attributes are copied out of the store as their
    bytes are hashed, and git reads them only from that copy. Rewritten in
    the store once copied, the rule that converts is never read: the CRLF
    file is still a difference. The object directory git reads them from
    holds exactly the `.gitattributes` blobs on the file's path, once each
    (two directories share one here), and nothing of the store."""
    repo = _init(tmp_path / "repo")
    commit = _commit(
        repo,
        _tree(
            repo,
            {
                ".gitattributes": COMMITTED_RULE,
                "docs": {".gitattributes": COMMITTED_RULE, "notes.txt": b"a\nb\n"},
                "other": {".gitattributes": b"*.bat eol=crlf\n", "x.bat": b"x"},
            },
        ),
    )
    root = _checkout(repo, commit, tmp_path / "checkout")
    (root / "docs" / "notes.txt").write_bytes(b"a\r\nb\r\n")
    rule = _out(repo, "rev-parse", f"{commit}:.gitattributes")
    read: list[set[str]] = []

    def runner(req: ExecutionRequest) -> ExecutionResult:
        objects = (req.env or {}).get("GIT_OBJECT_DIRECTORY")
        if "update-index" in req.command:
            rewrite_loose_object(repo / ".git" / "objects", rule, "blob", FORGED_RULE)
        if objects and "check-attr" in req.command:
            files = [p for p in Path(objects).rglob("*") if p.is_file()]
            read.append({p.parent.name + p.name for p in files})
        return execute(req)

    found = tree_differences(runner, str(repo), commit, "sha1", root, checkout_eol=True)
    assert found == [TreeDifference(b"docs/notes.txt", "modified")]
    assert read == [{rule}]
    assert _git(repo, "cat-file", "blob", rule) == FORGED_RULE
    assert not _leftovers(exports)


def test_a_symlinked_attributes_file_is_read_as_a_checkout_reads_it(tmp_path, exports):
    """A `.gitattributes` committed as a symlink is in a checkout's index
    with its link text as its blob, which git reads as attributes: the
    controller's index holds it the same way, so git's checkout is no
    difference, and where git does not convert, the CRLF form is one."""
    repo = _init(tmp_path / "repo")
    commit = _commit(
        repo, _tree(repo, {".gitattributes": Link(b"*.bat text eol=crlf"), "a.bat": b"a\n"})
    )
    root = _checkout(repo, commit, tmp_path / "checkout")
    written = (root / "a.bat").read_bytes()
    assert tree_differences(execute, str(repo), commit, "sha1", root, checkout_eol=True) == []
    if written == b"a\n":  # a git that reads no attributes from a link
        (root / "a.bat").write_bytes(b"a\r\n")
        assert tree_differences(execute, str(repo), commit, "sha1", root, checkout_eol=True) == [
            TreeDifference(b"a.bat", "modified")
        ]


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
