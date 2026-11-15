"""Byte-exact materialization of a REMOTE candidate, with a hash-back proof (#171).

A candidate is a commit id. :func:`materialize` writes the tree that commit
names into a fresh private directory and proves, before handing the
directory out, that what is on disk hashes back to that tree (ADR 0006
§2.6). The merge gate's export (:func:`autoforge.premerge.export_commit_tree`)
uses it, and candidate validation (#172) will.

- **Source (D6.1).** The commit, and every blob its tree names, must be in
  the store the controller's own git reads: the operator's shared object
  store, never an issue's private object directory, whose objects reach the
  shared store only through the strict import. A missing commit or blob is
  :class:`~autoforge.errors.CandidateNotMaterializableError`. The tree id is
  read from the commit's own bytes, re-hashed to its id (ADR 0004 D7.5), so
  a commit object rewritten in the store cannot name another tree.
- **Writer (D6.2).** ``git ls-tree -r -t -z --full-tree`` lists the tree;
  the blobs are streamed with ``git cat-file --batch``. Nothing else of git
  takes part: no index, no ``checkout-index``, no attribute, eol conversion,
  ``ident``, filter driver, ``core.autocrlf``, ``info/attributes`` or
  ``core.attributesFile``, so the bytes on disk are the committed bytes and
  no program the operator configured runs on candidate content. Names stay
  bytes; blob content reaches the disk as the executor reads it, through
  :attr:`~autoforge.executor.ExecutionRequest.on_chunk`, so it is never
  decoded, never cut at a capture bound and never held whole in memory, and
  the git process stays the executor's (ADR 0002). Every entry is created
  exclusively, relative to the descriptor of the directory created for its
  parent tree, never through a link: a directory for a tree, a ``0644`` or
  ``0755`` file, a symlink holding its link text, and an empty directory
  for a gitlink (counted; the submodule commit is never read). There is no
  ``.git``. A name that is ``.``, ``..`` or ``.git`` in any case, empty, or
  not below a tree the listing holds (a name carrying ``/``) is refused
  before anything is written.
- **Proof (D6.3).** The controller's own walk, with no git process: every
  entry is ``lstat``-ed relative to its directory's descriptor, never
  followed; blob ids are recomputed from the bytes on disk (a symlink's
  from its link text), the mode from the file type and the owner's execute
  bit, and tree ids bottom up in git's entry order, in the repository's
  object format. A gitlink path must be an empty directory, and its entry
  is the one the listing recorded. Every directory must hash to the tree
  the listing names for it and the root to the commit's tree, with no entry
  extra or missing. A mismatch is a
  :class:`~autoforge.errors.MaterializationError` naming the entry.
- **Comparison (PR #444 R3-F1, R4-F1; #488).** :func:`tree_differences`
  compares a working tree the controller did not write (an agent worktree)
  with a commit's tree the same way, blob by blob, and lists what differs
  instead of raising: what the directory holds besides the tree's entries
  is not looked at, and no git process reads it, so no attribute, filter
  or index can make other bytes read as the commit's. A gitlink's path
  matches only as the empty directory a checkout leaves a submodule it
  does not populate: a populated submodule's content is a repository of
  its own, never proven the commit's. On request, a file may also hold the
  bytes a checkout writes for it under the ``eol=crlf`` the commit's own
  ``.gitattributes`` give it (#488): those attributes are read from the
  commit's tree alone, in a git directory of the controller's own whose
  object directory holds only the ``.gitattributes`` blobs the controller
  re-hashed, each bound to the commit by trees re-hashed from the listing
  (PR #503 R1-F1), and the conversion is applied here to the blob's bytes,
  so no other attribute, conversion, configuration or object of the store
  takes part.

Every git process is the hardened local request
(:func:`~autoforge.git_transport.local_git_request`, ADR 0004 D7.3, D7.5):
no replacement objects, no commit-graph, no hooks, an allow-listed
environment. The directory is removed when the context exits, whatever
happened; a killed process leaves only that directory, which is never read
again.
"""

from __future__ import annotations

import errno
import hashlib
import os
import re
import shutil
import stat
import tempfile
import threading
import zlib
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .errors import CandidateNotMaterializableError, MaterializationError
from .executor import ExecutionRequest, ExecutionResult
from .git_transport import local_git_request
from .redaction import redact

Runner = Callable[[ExecutionRequest], ExecutionResult]

# The method identifier a validation policy digest names (ADR 0006 D8.1).
METHOD = "byte-exact-v1"

# Listing and streaming a large tree are bounded by time, as the export was.
_GIT_TIMEOUT_SECONDS = 600
# What the executor keeps of a streamed process's output: the stream goes
# to disk through ``on_chunk``, so the capture serves only the error tail.
_STREAM_CAPTURE_BYTES = 64 * 1024
# A ``cat-file --batch`` header is ``<id> <type> <size>``.
_MAX_HEADER_BYTES = 256
# A symlink's target is held in memory until it is created; the kernel
# refuses one of PATH_MAX or more anyway.
_MAX_LINK_BYTES = 4096
_READ_CHUNK_BYTES = 1024 * 1024

_HEX_LENGTH = {"sha1": 40, "sha256": 64}
_HEX_RE = re.compile(rb"[0-9a-f]+")

# Modes as a tree object writes them, and the type ``ls-tree`` prints.
_FILE = b"100644"
_EXECUTABLE = b"100755"
_SYMLINK = b"120000"
_TREE = b"40000"
_GITLINK = b"160000"
_TYPES = {
    _FILE: b"blob",
    _EXECUTABLE: b"blob",
    _SYMLINK: b"blob",
    _TREE: b"tree",
    _GITLINK: b"commit",
}

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_NEW_FILE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
# Non-blocking, so a FIFO swapped in after the ``lstat`` cannot stall the proof.
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK


@dataclass(frozen=True)
class MaterializedTree:
    """A candidate written out under ``root`` and proven to be its tree.

    ``files`` counts the blobs written (regular files and symlinks);
    ``gitlinks`` the unpopulated submodule directories. ``root`` is removed
    when the context that produced it exits.
    """

    commit: str
    tree: str
    object_format: str
    root: Path
    files: int
    gitlinks: int
    method: str = METHOD


@dataclass(frozen=True)
class _Entry:
    """One ``ls-tree`` record: ``path`` is ``parent`` and ``name`` joined by ``/``.

    ``size``: a blob's size in bytes when the listing was read with sizes,
    else -1.
    """

    path: bytes
    parent: bytes
    name: bytes
    mode: bytes
    oid: str
    size: int = -1


@dataclass(frozen=True)
class _Listing:
    """A tree's entries in ``ls-tree`` order: every tree before what it holds."""

    tree: str
    entries: tuple[_Entry, ...]


def _shown(path: bytes) -> str:
    """``path`` for a message: escaped, bounded and redacted, never decoded raw."""
    text = path.decode("utf-8", "backslashreplace")
    text = "".join(c if c.isprintable() else f"\\x{ord(c):02x}" for c in text)
    if len(text) > 200:
        text = text[:200] + "..."
    return redact(text) or "(root)"


def _request(repo: str, args: list[str]) -> ExecutionRequest:
    return local_git_request(["-C", repo, *args], timeout_seconds=_GIT_TIMEOUT_SECONDS)


def _failure(res: ExecutionResult, args: list[str]) -> str:
    """Why a git process did not answer, or "" when it did (exit 0, whole)."""
    shown = " ".join(["git", *args])
    if res.timed_out:
        return f"`{shown}` timed out after {_GIT_TIMEOUT_SECONDS}s"
    if res.exit_code != 0:
        tail = redact((res.stderr or res.stdout or "").strip()[-500:])
        return f"`{shown}` failed (exit {res.exit_code}): {tail}"
    if res.capture_abandoned:
        return f"`{shown}` left its output open past its exit"
    return ""


def _git(runner: Runner, repo: str, args: list[str], stdin: bytes | None = None) -> str:
    """One git plumbing command whose whole, short output is the answer."""
    req = _request(repo, args)
    req.stdin_data = stdin
    res = runner(req)
    problem = _failure(res, args)
    if not problem and res.stdout_truncated:
        problem = f"`git {' '.join(args)}` output was truncated at the executor's capture bound"
    if problem:
        raise MaterializationError(problem)
    return res.stdout or ""


def read_object_format(runner: Runner, repo: str) -> str:
    """The object format of ``repo``'s store: ``sha1`` or ``sha256``."""
    found = _git(runner, repo, ["rev-parse", "--show-object-format"]).strip()
    if found not in _HEX_LENGTH:
        raise MaterializationError(f"{repo} uses the object format {found!r}, which is not known")
    return found


def _is_oid(value: bytes, object_format: str) -> bool:
    return len(value) == _HEX_LENGTH[object_format] and _HEX_RE.fullmatch(value) is not None


def _commit_tree(runner: Runner, repo: str, commit: str, object_format: str) -> str:
    """The tree ``commit`` names, after it is found to be a commit in the store.

    The tree id is read from the commit's own bytes, which must hash to
    ``commit`` in ``object_format`` (ADR 0004 D7.5): git re-hashes only
    some of the objects it reads, which ones depending on the command and
    its version (``cat-file`` re-hashes none), so the proof that a loose
    object rewritten in the store names no other tree under the same id is
    the controller's own (PR #503 R1-F1).
    """
    if object_format not in _HEX_LENGTH:
        raise MaterializationError(f"unknown object format {object_format!r}")
    if not _is_oid(commit.encode("ascii", "replace"), object_format):
        raise CandidateNotMaterializableError(
            f"{commit!r} is not a full {object_format} commit id of {repo}"
        )
    answer = _git(runner, repo, ["cat-file", "--batch-check"], f"{commit}\n".encode()).split()
    if answer == [commit, "missing"]:
        raise CandidateNotMaterializableError(
            f"commit {commit} is not in the shared object store of {repo}"
        )
    if len(answer) != 3 or answer[0] != commit or answer[1] != "commit":
        kind = answer[1] if len(answer) == 3 and answer[0] == commit else "unreadable"
        raise CandidateNotMaterializableError(
            f"{commit} in {repo} is a {kind} object, not a commit"
        )
    args = ["cat-file", "commit", commit]
    collector = _Collector()
    req = _request(repo, args)
    req.on_chunk = collector
    req.max_output_bytes = _STREAM_CAPTURE_BYTES
    problem = _failure(runner(req), args)
    if problem:
        raise MaterializationError(f"commit {commit}: reading it failed: {problem}")
    raw = bytes(collector.data)
    if hashlib.new(object_format, b"commit %d\0" % len(raw) + raw).hexdigest() != commit:
        raise MaterializationError(
            f"commit {commit}: the bytes {repo}'s store holds for it do not hash to its id "
            "(a rewritten or substituted object); it is not used"
        )
    line = raw.partition(b"\n")[0]
    if not line.startswith(b"tree ") or not _is_oid(line[5:], object_format):
        raise MaterializationError(f"commit {commit} names no readable tree: {_shown(line[:100])}")
    return line[5:].decode("ascii")


class _Collector:
    """An ``on_chunk`` hook keeping a process's stdout bytes as they were written."""

    def __init__(self) -> None:
        self.data = bytearray()

    def __call__(self, stream: str, chunk: bytes) -> None:
        if stream == "stdout":
            self.data += chunk


def _check_name(name: bytes, path: bytes, commit: str) -> None:
    """Refuse a name no tree entry may carry into the export (D6.2)."""
    problem = ""
    if not name:
        problem = "an empty name"
    elif name in (b".", b".."):
        problem = f"the name {name.decode()!r}"
    elif name.lower() == b".git":
        problem = "a .git entry"
    elif b"\0" in name:
        problem = "a NUL in its name"
    if problem:
        raise MaterializationError(
            f"commit {commit}: the entry {_shown(path)} is refused: {problem}"
        )


def _list_tree(
    runner: Runner, repo: str, commit: str, tree: str, object_format: str, *, sizes: bool = False
) -> _Listing:
    """Every entry of ``tree``, validated before anything is written.

    ``sizes``: read each blob's size too (``ls-tree -l``), which reads the
    header of every blob from the store.
    """
    args = ["ls-tree", "-r", "-t", *(["-l"] if sizes else []), "-z", "--full-tree", tree]
    collector = _Collector()
    req = _request(repo, args)
    req.on_chunk = collector
    req.max_output_bytes = _STREAM_CAPTURE_BYTES
    problem = _failure(runner(req), args)
    if problem:
        raise MaterializationError(f"commit {commit}: listing its tree failed: {problem}")
    raw = bytes(collector.data)
    if raw and not raw.endswith(b"\0"):
        raise MaterializationError(f"commit {commit}: the listing of {tree} ends mid-record")
    trees: set[bytes] = {b""}
    entries: list[_Entry] = []
    seen: set[bytes] = set()
    for record in raw.split(b"\0")[:-1]:
        meta, tab, path = record.partition(b"\t")
        # ``-l`` right-aligns the size after one or more spaces: "-" for a
        # tree or a gitlink, a decimal for a blob.
        fields = meta.split(b" ", 3) if sizes else meta.split(b" ")
        size = -1
        if sizes:
            shown_size = fields.pop().lstrip(b" ") if len(fields) == 4 else b""
            if shown_size.isdigit() and fields[1] == b"blob":
                size = int(shown_size)
            elif shown_size != b"-" or fields[1] == b"blob":
                fields = []
        if not tab or len(fields) != 3:
            raise MaterializationError(
                f"commit {commit}: unreadable listing record {_shown(record)}"
            )
        mode, kind, oid = fields
        if mode == b"040000":
            mode = _TREE
        if _TYPES.get(mode) != kind or not _is_oid(oid, object_format):
            raise MaterializationError(
                f"commit {commit}: the entry {_shown(path)} has the unsupported mode and type "
                f"{_shown(mode)} {_shown(kind)}"
            )
        parent, slash, name = path.rpartition(b"/")
        _check_name(name, path, commit)
        if parent not in trees or (slash and not parent):
            # ``ls-tree`` joins names with ``/``: a path whose parent was not
            # listed as a tree, or that starts with ``/``, came from a name
            # holding ``/`` (a tree below a gitlink is never listed).
            raise MaterializationError(
                f"commit {commit}: the entry {_shown(path)} is refused: it is not below a tree "
                "of the listing (a name holding '/')"
            )
        if path in seen:
            raise MaterializationError(f"commit {commit}: the entry {_shown(path)} is listed twice")
        seen.add(path)
        if mode == _TREE:
            trees.add(path)
        entries.append(_Entry(path, parent, name, mode, oid.decode("ascii"), size))
    return _Listing(tree=tree, entries=tuple(entries))


class _Writer:
    """Creates a listing's entries below ``root``, in listing order.

    Trees and gitlinks are created as they are reached; a blob is created
    when its content arrives (:meth:`open_blob`, :meth:`write`,
    :meth:`close_blob`). The descriptors of the directories on the current
    path are held open, so every entry is created relative to the one made
    for its parent, never through a path a link could redirect.
    """

    def __init__(self, root: Path, listing: _Listing, commit: str) -> None:
        self._entries = listing.entries
        self._commit = commit
        self._next = 0
        self._stack: list[tuple[bytes, int]] = [(b"", os.open(root, _DIR_FLAGS))]
        self._blob: _Entry | None = None
        self._fd: int | None = None
        self._link: bytearray | None = None

    def _fail(self, entry: _Entry, what: str, exc: OSError | None = None) -> MaterializationError:
        reason = f": {exc.strerror or exc}" if exc is not None else ""
        return MaterializationError(
            f"commit {self._commit}: {what} {_shown(entry.path)} failed{reason}"
        )

    def _parent_fd(self, entry: _Entry) -> int:
        while self._stack and self._stack[-1][0] != entry.parent:
            os.close(self._stack.pop()[1])
        if not self._stack:
            raise MaterializationError(
                f"commit {self._commit}: the entry {_shown(entry.path)} is listed after its "
                "parent tree was complete"
            )
        return self._stack[-1][1]

    def _directory(self, entry: _Entry) -> None:
        parent = self._parent_fd(entry)
        try:
            os.mkdir(entry.name, 0o755, dir_fd=parent)
            fd = os.open(entry.name, _DIR_FLAGS, dir_fd=parent)
        except OSError as exc:
            raise self._fail(entry, "creating the directory", exc) from exc
        try:
            os.fchmod(fd, 0o755)
        except OSError as exc:
            os.close(fd)
            raise self._fail(entry, "creating the directory", exc) from exc
        if entry.mode == _TREE:
            self._stack.append((entry.path, fd))
        else:
            os.close(fd)

    def next_blob(self) -> _Entry | None:
        """Create every tree and gitlink up to the next blob, and return that blob."""
        while self._next < len(self._entries):
            entry = self._entries[self._next]
            self._next += 1
            if entry.mode in (_TREE, _GITLINK):
                self._directory(entry)
            else:
                return entry
        return None

    def open_blob(self, entry: _Entry) -> None:
        parent = self._parent_fd(entry)
        self._blob = entry
        if entry.mode == _SYMLINK:
            self._link = bytearray()
            return
        try:
            self._fd = os.open(entry.name, _NEW_FILE_FLAGS, 0o600, dir_fd=parent)
        except OSError as exc:
            raise self._fail(entry, "creating the file", exc) from exc

    def write(self, data: memoryview) -> None:
        assert self._blob is not None
        if self._link is not None:
            self._link += data
            if len(self._link) > _MAX_LINK_BYTES:
                raise MaterializationError(
                    f"commit {self._commit}: the symlink {_shown(self._blob.path)} holds more "
                    f"than {_MAX_LINK_BYTES} bytes of link text"
                )
            return
        assert self._fd is not None
        try:
            while data:
                written = os.write(self._fd, data)
                data = data[written:]
        except OSError as exc:
            raise self._fail(self._blob, "writing", exc) from exc

    def close_blob(self) -> None:
        entry = self._blob
        assert entry is not None
        try:
            if self._link is not None:
                os.symlink(bytes(self._link), entry.name, dir_fd=self._parent_fd(entry))
            else:
                assert self._fd is not None
                os.fchmod(self._fd, 0o755 if entry.mode == _EXECUTABLE else 0o644)
        except OSError as exc:
            raise self._fail(entry, "creating", exc) from exc
        finally:
            self._close_file()
            self._link = None
            self._blob = None

    def _close_file(self) -> None:
        if self._fd is not None:
            fd, self._fd = self._fd, None
            os.close(fd)

    def finish(self) -> None:
        """Create what follows the last blob; every entry must have been reached."""
        if self.next_blob() is not None:
            raise MaterializationError(
                f"commit {self._commit}: git sent fewer blobs than the tree lists"
            )

    def close(self) -> None:
        self._close_file()
        while self._stack:
            os.close(self._stack.pop()[1])


class _BlobSink(Protocol):
    """Where :class:`_BlobStream` hands each blob: :class:`_Writer`, :class:`_CrlfForms`,
    :class:`_LooseObjects`.
    """

    def next_blob(self) -> _Entry | None: ...

    def open_blob(self, entry: _Entry) -> None: ...

    def write(self, data: memoryview) -> None: ...

    def close_blob(self) -> None: ...


class _BlobStream:
    """An ``on_chunk`` hook parsing ``git cat-file --batch`` into a sink.

    Each blob arrives as ``<id> blob <size>\\n<content>\\n``, in the order
    the ids were asked for. The first problem is kept and ``stop`` is set,
    so the executor ends the process; nothing is read after it, or after
    :meth:`close`.
    """

    def __init__(self, writer: _BlobSink, commit: str) -> None:
        self._writer = writer
        self._commit = commit
        self._header = bytearray()
        self._blob: _Entry | None = None
        self._remaining = 0
        self._trailer = False
        self._lock = threading.Lock()
        self._closed = False
        self.error: Exception | None = None
        self.stop = threading.Event()
        self.done = False
        self._expect()

    def _expect(self) -> None:
        self._blob = self._writer.next_blob()
        self.done = self._blob is None

    def __call__(self, stream: str, chunk: bytes) -> None:
        if stream != "stdout":
            return
        with self._lock:
            if self._closed or self.error is not None:
                return
            try:
                self._feed(memoryview(chunk))
            except (MaterializationError, CandidateNotMaterializableError, OSError) as exc:
                self.error = exc
                self.stop.set()

    def _feed(self, chunk: memoryview) -> None:
        while chunk:
            if self.done:
                raise MaterializationError(
                    f"commit {self._commit}: git sent more blobs than the tree lists"
                )
            if self._trailer:
                if chunk[0] != 0x0A:
                    raise MaterializationError(
                        f"commit {self._commit}: unexpected git output after a blob's content"
                    )
                chunk = chunk[1:]
                self._trailer = False
                self._writer.close_blob()
                self._expect()
            elif self._remaining:
                part = chunk[: self._remaining]
                self._writer.write(part)
                self._remaining -= len(part)
                chunk = chunk[len(part) :]
                self._trailer = not self._remaining
            else:
                room = _MAX_HEADER_BYTES - len(self._header)
                end = bytes(chunk[: room + 1]).find(b"\n")
                if end < 0:
                    if len(chunk) > room:
                        raise MaterializationError(
                            f"commit {self._commit}: an unreadable `git cat-file` header"
                        )
                    self._header += chunk
                    return
                self._header += chunk[:end]
                chunk = chunk[end + 1 :]
                header, self._header = bytes(self._header), bytearray()
                self._start(header)

    def _start(self, header: bytes) -> None:
        blob = self._blob
        assert blob is not None
        oid = blob.oid.encode("ascii")
        fields = header.split(b" ")
        if fields == [oid, b"missing"]:
            raise CandidateNotMaterializableError(
                f"commit {self._commit}: the blob {blob.oid} of {_shown(blob.path)} is not in "
                "the shared object store"
            )
        if len(fields) != 3 or fields[0] != oid or fields[1] != b"blob" or not fields[2].isdigit():
            raise MaterializationError(
                f"commit {self._commit}: git answered {_shown(header)} for the blob of "
                f"{_shown(blob.path)}"
            )
        self._writer.open_blob(blob)
        self._remaining = int(fields[2])
        self._trailer = not self._remaining

    def close(self) -> None:
        with self._lock:
            self._closed = True


def _stream_blobs(
    runner: Runner, repo: str, commit: str, sink: _BlobSink, blobs: Sequence[str]
) -> None:
    """Stream the blobs ``blobs`` names from ``repo``'s store into ``sink``, in that order.

    ``sink.next_blob`` yields the entry of each id in turn; anything but
    exactly those blobs is a :class:`MaterializationError`.
    """
    stream = _BlobStream(sink, commit)
    args = ["cat-file", "--batch", "--buffer"]
    req = _request(repo, args)
    req.stdin_data = "".join(f"{oid}\n" for oid in blobs).encode("ascii")
    req.on_chunk = stream
    req.stop = stream.stop
    req.max_output_bytes = _STREAM_CAPTURE_BYTES
    try:
        res = runner(req)
    finally:
        stream.close()
    if stream.error is not None:
        raise stream.error
    problem = _failure(res, args)
    if problem:
        raise MaterializationError(f"commit {commit}: streaming its blobs failed: {problem}")
    if not stream.done:
        raise MaterializationError(f"commit {commit}: git sent fewer blobs than the tree lists")


def _write(runner: Runner, repo: str, commit: str, listing: _Listing, root: Path) -> None:
    """Write ``listing`` below ``root``, the blobs streamed from the store."""
    writer = _Writer(root, listing, commit)
    try:
        blobs = [e.oid for e in listing.entries if e.mode not in (_TREE, _GITLINK)]
        if blobs:
            _stream_blobs(runner, repo, commit, writer, blobs)
        writer.finish()
    finally:
        writer.close()


def _blob_id(algorithm: str, content: bytes) -> str:
    return hashlib.new(algorithm, b"blob %d\0" % len(content) + content).hexdigest()


def _file_digest(
    dir_fd: int, name: bytes, seen: os.stat_result, start: Callable[[int], hashlib._Hash]
) -> tuple[os.stat_result, str]:
    """The ``fstat`` of the regular file ``name`` and the digest of its bytes on disk.

    ``seen`` is its ``lstat``: the file opened, never through a link, must
    be that one, and hold as many bytes as it says; ``start`` makes the
    digest from that size.
    """
    fd = os.open(name, _READ_FLAGS, dir_fd=dir_fd)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or (st.st_dev, st.st_ino) != (seen.st_dev, seen.st_ino):
            raise OSError(0, "replaced while it was proved")
        digest = start(st.st_size)
        total = 0
        while chunk := os.read(fd, _READ_CHUNK_BYTES):
            digest.update(chunk)
            total += len(chunk)
        if total != st.st_size:
            raise OSError(0, "changed size while it was proved")
    finally:
        os.close(fd)
    return st, digest.hexdigest()


def _file_id(algorithm: str, dir_fd: int, name: bytes, seen: os.stat_result) -> tuple[bytes, str]:
    """The mode and blob id of the regular file ``name``, from its bytes on disk."""
    st, oid = _file_digest(
        dir_fd, name, seen, lambda size: hashlib.new(algorithm, b"blob %d\0" % size)
    )
    return (_EXECUTABLE if st.st_mode & stat.S_IXUSR else _FILE), oid


def _tree_id(algorithm: str, entries: list[tuple[bytes, bytes, str]]) -> str:
    """The id of a tree holding ``entries`` (name, mode, id), in git's order.

    A tree entry sorts as its name followed by ``/``; every other entry,
    a gitlink included, as its name.
    """
    entries.sort(key=lambda e: e[0] + b"/" if e[1] == _TREE else e[0])
    body = b"".join(mode + b" " + name + b"\0" + bytes.fromhex(oid) for name, mode, oid in entries)
    return hashlib.new(algorithm, b"tree %d\0" % len(body) + body).hexdigest()


@dataclass
class _Frame:
    """One directory of the proof's walk: its names still to visit, its entries so far."""

    path: bytes
    fd: int
    pending: list[bytes]
    entries: list[tuple[bytes, bytes, str]]


def _join(parent: bytes, name: bytes) -> bytes:
    return parent + b"/" + name if parent else name


def _prove(root: Path, listing: _Listing, commit: str, object_format: str) -> None:
    """Prove that ``root`` holds exactly ``listing``'s tree (D6.3), or raise."""
    expected = {listed.path: listed for listed in listing.entries}
    children: dict[bytes, set[bytes]] = {b"": set()}
    for listed in listing.entries:
        children.setdefault(listed.parent, set()).add(listed.name)
        if listed.mode == _TREE:
            children.setdefault(listed.path, set())

    def fail(path: bytes, what: str) -> MaterializationError:
        return MaterializationError(
            f"commit {commit}: the export does not hash back to its tree {listing.tree}: "
            f"{_shown(path)} {what}"
        )

    frames: list[_Frame] = []

    def enter(path: bytes, fd: int) -> None:
        # Held by a frame at once, so the ``finally`` below closes it.
        frames.append(_Frame(path, fd, [], []))
        names = [os.fsencode(name) for name in os.listdir(fd)]
        missing = sorted(children.get(path, set()) - set(names))
        if missing:
            raise fail(_join(path, missing[0]), "is missing")
        frames[-1].pending = sorted(names, reverse=True)

    path = b""
    try:
        enter(b"", os.open(root, _DIR_FLAGS))
        while frames:
            top = frames[-1]
            if not top.pending:
                path = top.path
                oid = _tree_id(object_format, top.entries)
                want = listing.tree if not top.path else expected[top.path].oid
                if oid != want:
                    raise fail(top.path, f"hashes to the tree {oid}, the commit holds {want}")
                frames.pop()
                os.close(top.fd)
                if frames:
                    frames[-1].entries.append((top.path.rpartition(b"/")[2], _TREE, oid))
                continue
            name = top.pending.pop()
            path = _join(top.path, name)
            entry = expected.get(path)
            if entry is None or entry.parent != top.path:
                raise fail(path, "is not in the commit")
            st = os.lstat(name, dir_fd=top.fd)
            if entry.mode == _GITLINK:
                if not stat.S_ISDIR(st.st_mode):
                    raise fail(path, "is a gitlink, but not a directory")
                fd = os.open(name, _DIR_FLAGS, dir_fd=top.fd)
                try:
                    if os.listdir(fd):
                        raise fail(path, "is a gitlink, but its directory is not empty")
                finally:
                    os.close(fd)
                top.entries.append((name, _GITLINK, entry.oid))
                continue
            if stat.S_ISDIR(st.st_mode):
                if entry.mode != _TREE:
                    raise fail(path, f"is a directory, the commit holds mode {entry.mode.decode()}")
                enter(path, os.open(name, _DIR_FLAGS, dir_fd=top.fd))
                continue
            if stat.S_ISREG(st.st_mode):
                mode, oid = _file_id(object_format, top.fd, name, st)
            elif stat.S_ISLNK(st.st_mode):
                mode, oid = _SYMLINK, _blob_id(object_format, os.readlink(name, dir_fd=top.fd))
            else:
                raise fail(path, "is neither a regular file, a symlink nor a directory")
            if (mode, oid) != (entry.mode, entry.oid):
                raise fail(
                    path,
                    f"is {mode.decode()} {oid} on disk, the commit holds "
                    f"{entry.mode.decode()} {entry.oid}",
                )
            top.entries.append((name, mode, oid))
    except OSError as exc:
        raise fail(path, f"could not be proved: {exc.strerror or exc}") from exc
    finally:
        for open_frame in frames:
            os.close(open_frame.fd)


@dataclass(frozen=True)
class TreeDifference:
    """An entry of a commit's tree that a directory does not hold as the commit does.

    For a blob, ``kind`` is ``"deleted"`` (nothing at the path, or a
    directory above it that is not a real one), ``"retyped"`` (anything but
    a regular file where the tree lists one, anything but a symlink where it
    lists a symlink) or ``"modified"`` (other bytes, or the owner's execute
    bit other than the mode says). For a gitlink (``submodule`` true), it is
    ``"deleted"`` as for a blob, ``"retyped"`` (anything but a real
    directory) or ``"populated"`` (a real directory holding any entry,
    a submodule's checkout or anything else).
    """

    path: bytes
    kind: str
    submodule: bool = False


# What opening a directory along a listed path finds when it is not a real
# directory there: nothing, a file, or a symlink (``O_NOFOLLOW``: ELOOP on
# Linux, ENOTDIR on some other systems).
_NOT_A_DIRECTORY = (errno.ENOENT, errno.ENOTDIR, errno.ELOOP)


def _open_listed_dir(parent: int | None, name: bytes) -> int | None:
    """The real directory ``name`` in ``parent``, opened without following a link, or ``None``."""
    if parent is None:
        return None
    try:
        return os.open(name, _DIR_FLAGS, dir_fd=parent)
    except OSError as exc:
        if exc.errno in _NOT_A_DIRECTORY:
            return None
        raise


def _blob_difference(object_format: str, parent: int | None, entry: _Entry) -> str:
    """How the blob ``entry`` differs in the directory ``parent``, or "" when it does not."""
    if parent is None:
        return "deleted"
    try:
        st = os.lstat(entry.name, dir_fd=parent)
    except FileNotFoundError:
        return "deleted"
    if entry.mode == _SYMLINK:
        if not stat.S_ISLNK(st.st_mode):
            return "retyped"
        target = os.readlink(entry.name, dir_fd=parent)
        return "" if _blob_id(object_format, target) == entry.oid else "modified"
    if not stat.S_ISREG(st.st_mode):
        return "retyped"
    mode = _EXECUTABLE if st.st_mode & stat.S_IXUSR else _FILE
    if mode != entry.mode or st.st_size != entry.size:
        return "modified"
    _, oid = _file_id(object_format, parent, entry.name, st)
    return "" if oid == entry.oid else "modified"


def _gitlink_difference(parent: int | None, entry: _Entry) -> str:
    """How the gitlink ``entry`` differs in the directory ``parent``, or "" when it does not.

    Only an empty real directory matches: what a checkout leaves at a
    submodule's path when it does not populate it. Nothing inside is read
    beyond its first name, and no repository there is opened.
    """
    if parent is None:
        return "deleted"
    try:
        st = os.lstat(entry.name, dir_fd=parent)
    except FileNotFoundError:
        return "deleted"
    if not stat.S_ISDIR(st.st_mode):
        return "retyped"
    fd = _open_listed_dir(parent, entry.name)
    if fd is None:
        return "deleted"
    try:
        with os.scandir(fd) as names:
            return "populated" if next(names, None) is not None else ""
    finally:
        os.close(fd)


# -- the commit's own end-of-line checkout form (#488) ---------------------------------------
#
# A checkout writes each blob through the conversions its attributes name,
# so a worktree git checked out itself holds other bytes than the blob
# wherever an end-of-line conversion applies (``*.bat text eol=crlf``). The
# one conversion :func:`tree_differences` takes as the commit's is
# ``eol=crlf`` as the commit's own ``.gitattributes`` give it, applied here
# as git's ``convert.c`` applies it on checkout.

# The attributes ``convert_attrs`` reads for it, in the order asked for.
_EOL_ATTRIBUTES = (b"text", b"crlf", b"eol")
# ``git_path_check_crlf``: what ``text``, else the legacy ``crlf``, makes of
# a path. ``check-attr`` prints a true attribute and the value ``set`` alike
# (``unset`` likewise), and git takes either value as unspecified, so
# :func:`_crlf_conversion` reads such a ``text`` both ways and keeps the
# stricter answer; a ``crlf`` read so already gives the stricter one.
_CRLF_STATES = {b"set": "text", b"input": "text", b"auto": "auto", b"unset": "binary"}
_LONE_LF = re.compile(rb"(?<!\r)\n")
# What ``gather_stats`` counts as non-printable: the control bytes but BS,
# HT, FF and ESC (LF and CR are counted apart), and DEL.
_NON_PRINTABLE = bytes(b for b in range(32) if b not in b"\b\t\n\f\r\x1b") + b"\x7f"


def _crlf_conversion(text: bytes, crlf: bytes, eol: bytes) -> str:
    """The checkout conversion to CRLF these attribute values give a path, or "".

    ``"text"``: a CR before every LF that does not follow one
    (``CRLF_TEXT_CRLF``); ``"auto"``: the same, only for a blob git takes as
    text with LF endings alone (``CRLF_AUTO_CRLF``). Only ``eol=crlf``
    converts, and not on a binary path (``-text``, the ``binary`` macro):
    ``core.autocrlf`` and ``core.eol``, which make a checkout write CRLF
    without it, are configuration, which the agent can write.

    A ``text`` that reads ``set`` or ``unset`` may be that value rather than
    the attribute set or unset, which git takes as unspecified, falling back
    to ``crlf``: both readings are made and the stricter kept (none over
    ``"auto"`` over ``"text"``, each converting a subset of what the next
    converts, identically), so a file is at worst refused where git would
    have converted it, never taken as a form git does not write.
    """
    if eol != b"crlf":
        return ""
    fallback = _CRLF_STATES.get(crlf, "")
    readings = {_CRLF_STATES.get(text) or fallback}
    if text in (b"set", b"unset"):
        readings.add(fallback)
    if "binary" in readings:
        return ""
    return "auto" if "auto" in readings else "text"


class _CrlfForm:
    """One blob streamed: its CRLF checkout form's size and digest, and git's stats.

    The form is the blob with a CR put before every LF that does not
    follow one, as both ``crlf_to_worktree`` and the streaming
    ``lf_to_crlf_filter`` write it. The stats are ``gather_stats``'s, which
    decide whether ``text=auto`` converts at all. The blob's own id is
    computed from the same bytes (:meth:`whole`), so a form is only ever
    the committed bytes' (ADR 0004 D7.5).
    """

    def __init__(self, entry: _Entry, object_format: str) -> None:
        self.entry = entry
        self._blob = hashlib.new(object_format, b"blob %d\0" % entry.size)
        self._form = hashlib.sha256()
        self.size = 0
        self._read = 0
        self._last = b""
        self._cr = self._lf = self._crlf = self._nul = self._non_printable = 0

    def feed(self, data: bytes) -> None:
        if not data:
            return
        pair = self._last == b"\r" and data[:1] == b"\n"
        self._blob.update(data)
        self._read += len(data)
        self._crlf += data.count(b"\r\n") + pair
        self._cr += data.count(b"\r")
        self._lf += data.count(b"\n")
        self._nul += data.count(b"\0")
        self._non_printable += len(data) - len(data.translate(None, _NON_PRINTABLE))
        # An LF that ends a CRLF split across two chunks keeps its CR alone.
        form = b"\n" + _LONE_LF.sub(b"\r\n", data[1:]) if pair else _LONE_LF.sub(b"\r\n", data)
        self._form.update(form)
        self.size += len(form)
        self._last = data[-1:]

    def whole(self) -> bool:
        """Whether the bytes fed are the blob the listing names, all of them."""
        return self._read == self.entry.size and self._blob.hexdigest() == self.entry.oid

    @property
    def digest(self) -> str:
        return self._form.hexdigest()

    def converts(self, conversion: str) -> bool:
        """Whether a checkout under ``conversion`` writes this form rather than the blob.

        ``will_convert_lf_to_crlf``: never without an LF to convert; under
        ``"auto"``, not when the blob holds a CR or a CRLF already, or reads
        as binary (``convert_is_binary``: a lone CR, a NUL, or more than one
        non-printable byte in 128 printable ones, a final ``^Z`` not counted).
        """
        lone_lf = self._lf - self._crlf
        if not conversion or not lone_lf:
            return False
        if conversion == "text":
            return True
        lone_cr = self._cr - self._crlf
        printable = self._read - self._cr - self._lf - self._non_printable
        non_printable = self._non_printable - (self._last == b"\x1a")
        binary = lone_cr or self._nul or (printable >> 7) < non_printable
        return not lone_cr and not self._crlf and not binary


class _CrlfForms:
    """A :class:`_BlobStream` sink: each listed blob's :class:`_CrlfForm`, by path."""

    def __init__(self, entries: Sequence[_Entry], object_format: str, commit: str) -> None:
        self._entries = list(entries)
        self._object_format = object_format
        self._commit = commit
        self._next = 0
        self._current: _CrlfForm | None = None
        self.forms: dict[bytes, _CrlfForm] = {}

    def next_blob(self) -> _Entry | None:
        if self._next == len(self._entries):
            return None
        self._next += 1
        return self._entries[self._next - 1]

    def open_blob(self, entry: _Entry) -> None:
        self._current = _CrlfForm(entry, self._object_format)

    def write(self, data: memoryview) -> None:
        assert self._current is not None
        self._current.feed(bytes(data))

    def close_blob(self) -> None:
        form, self._current = self._current, None
        assert form is not None
        if not form.whole():
            raise MaterializationError(
                f"commit {self._commit}: git sent bytes for the blob of "
                f"{_shown(form.entry.path)} that are not its {form.entry.oid}"
            )
        self.forms[form.entry.path] = form


def _attribute_dir_config(object_format: str) -> str:
    """The whole configuration of the git directory the attributes are read in."""
    lines = [
        "[core]",
        f"\trepositoryformatversion = {1 if object_format == 'sha256' else 0}",
        "\tbare = true",
    ]
    if object_format == "sha256":
        lines += ["[extensions]", "\tobjectFormat = sha256"]
    return "\n".join(lines) + "\n"


# The name of the attributes file git reads in each directory of a path,
# and the modes git reads one from: a blob's, a symlink's link text
# included. A tree or a gitlink of that name holds no attributes.
_ATTRIBUTES_FILE = b".gitattributes"
_BLOB_MODES = (_FILE, _EXECUTABLE, _SYMLINK)


def _attribute_files(
    listing: _Listing, commit: str, object_format: str, paths: Sequence[bytes]
) -> list[_Entry]:
    """The ``.gitattributes`` files git reads for ``paths``, from trees proven ``commit``'s.

    For a path, git reads the ``.gitattributes`` of each directory on it,
    from the root down, and nothing else of the tree. Each of those
    directories must hold exactly the entries the listing gives it: they
    are hashed as a tree, in git's order and in ``object_format``, and the
    id must be the one the listing names for the directory, the root's
    being the tree read from the commit's own bytes (:func:`_commit_tree`).
    So every file returned, and the entry of every path of ``paths``, is
    ``commit``'s by a chain of re-hashed ids, and a tree rewritten in the
    store to add, drop or swap one fails here (PR #503 R1-F1).
    """
    directories: set[bytes] = set()
    for path in paths:
        parent = path.rpartition(b"/")[0]
        while parent not in directories:
            directories.add(parent)
            if not parent:
                break
            parent = parent.rpartition(b"/")[0]
    held: dict[bytes, list[_Entry]] = {directory: [] for directory in directories}
    ids = {b"": listing.tree}
    for entry in listing.entries:
        if entry.parent in held:
            held[entry.parent].append(entry)
        if entry.mode == _TREE and entry.path in held:
            ids[entry.path] = entry.oid
    files: list[_Entry] = []
    for directory in sorted(directories):
        entries = held[directory]
        found = _tree_id(object_format, [(e.name, e.mode, e.oid) for e in entries])
        want = ids.get(directory)
        if found != want:
            raise MaterializationError(
                f"commit {commit}: the entries the store lists for {_shown(directory)} hash to "
                f"the tree {found}, not to its {want}: a tree object was rewritten or "
                "substituted, so its attributes are not read"
            )
        files += [e for e in entries if e.name == _ATTRIBUTES_FILE and e.mode in _BLOB_MODES]
    return files


class _LooseObjects:
    """A :class:`_BlobStream` sink: each blob put into ``objects`` as a loose object.

    Its bytes are compressed into a temporary file as they stream and
    moved to the blob's id only once they hash to it, so ``objects`` holds
    nothing but bytes the controller hashed itself (ADR 0004 D7.5).
    """

    def __init__(
        self, objects: Path, entries: Sequence[_Entry], object_format: str, commit: str
    ) -> None:
        self._objects = objects
        self._incoming = objects / "incoming"
        self._entries = list(entries)
        self._object_format = object_format
        self._commit = commit
        self._next = 0
        self._entry: _Entry | None = None
        self._fd: int | None = None
        self._digest = hashlib.new(object_format)
        self._zip = zlib.compressobj()
        self._read = 0

    def _fail(self, exc: OSError) -> MaterializationError:
        shown = _shown(self._entry.path) if self._entry is not None else ""
        return MaterializationError(
            f"commit {self._commit}: copying the blob of {shown} failed: {exc.strerror or exc}"
        )

    def _put(self, data: bytes) -> None:
        assert self._fd is not None
        view = memoryview(data)
        try:
            while view:
                view = view[os.write(self._fd, view) :]
        except OSError as exc:
            raise self._fail(exc) from exc

    def next_blob(self) -> _Entry | None:
        if self._next == len(self._entries):
            return None
        self._next += 1
        return self._entries[self._next - 1]

    def open_blob(self, entry: _Entry) -> None:
        header = b"blob %d\0" % entry.size
        self._entry = entry
        self._digest = hashlib.new(self._object_format, header)
        self._zip = zlib.compressobj()
        self._read = 0
        try:
            self._fd = os.open(self._incoming, _NEW_FILE_FLAGS, 0o600)
        except OSError as exc:
            raise self._fail(exc) from exc
        self._put(self._zip.compress(header))

    def write(self, data: memoryview) -> None:
        self._digest.update(data)
        self._read += len(data)
        self._put(self._zip.compress(data))

    def close_blob(self) -> None:
        entry = self._entry
        assert entry is not None
        try:
            self._put(self._zip.flush())
        finally:
            self.close()
        if self._read != entry.size or self._digest.hexdigest() != entry.oid:
            raise MaterializationError(
                f"commit {self._commit}: git sent bytes for the blob of {_shown(entry.path)} "
                f"that are not its {entry.oid}"
            )
        try:
            directory = self._objects / entry.oid[:2]
            directory.mkdir(exist_ok=True)
            os.rename(self._incoming, directory / entry.oid[2:])
        except OSError as exc:
            raise self._fail(exc) from exc

    def close(self) -> None:
        if self._fd is not None:
            fd, self._fd = self._fd, None
            os.close(fd)


def _eol_conversions(
    runner: Runner,
    repo: str,
    commit: str,
    object_format: str,
    listing: _Listing,
    paths: Sequence[bytes],
) -> dict[bytes, str]:
    """The checkout conversion to CRLF ``commit``'s own attributes give each of ``paths``.

    Read by git's own attribute machinery, from ``commit``'s tree and nothing
    else, in a git directory written into a temporary directory of the
    controller's own. That directory's object directory is its own as well:
    it holds only the ``.gitattributes`` files git reads for ``paths``
    (:func:`_attribute_files`, bound to ``commit`` by re-hashed trees), each
    copied there from ``repo``'s store once its bytes hash to its id
    (:class:`_LooseObjects`). ``update-index --index-info`` writes an index
    of exactly those files, at their paths and with their modes, as a
    checkout's index holds them, and ``check-attr --cached`` reads them,
    macros included. Git re-hashes neither a blob ``check-attr`` reads nor
    a tree below the one it is named, but nothing of ``repo``'s store is
    read here: a blob or a tree rewritten there, before the copy or after
    it, cannot change the attributes (PR #503 R1-F1). Nor is any attribute
    a checkout also reads, and the agent can
    write: no ``.gitattributes`` of a working tree, no ``info/attributes``
    (the directory has none), no global attributes file
    (``core.attributesFile`` is ``/dev/null``), no system one
    (``GIT_ATTR_NOSYSTEM``), and no configuration but the directory's own
    (``GIT_CONFIG_NOSYSTEM``, ``GIT_CONFIG_GLOBAL``). With no
    ``.gitattributes`` in any directory of ``paths``, nothing converts and
    no git process runs. The directory is removed afterwards, whatever
    happened.
    """
    files = _attribute_files(listing, commit, object_format, paths)
    if not files:
        return dict.fromkeys(paths, "")
    failed = f"commit {commit}: reading its end-of-line attributes failed"
    try:
        scratch = Path(tempfile.mkdtemp(prefix="autoforge-attributes-"))
    except OSError as exc:
        raise MaterializationError(f"{failed}: {exc.strerror or exc}") from exc
    try:
        git_dir = scratch / "git"
        objects = git_dir / "objects"
        try:
            for directory in (git_dir, git_dir / "refs", objects):
                directory.mkdir()
            (git_dir / "HEAD").write_text("ref: refs/heads/autoforge-unborn\n", encoding="ascii")
            (git_dir / "config").write_text(_attribute_dir_config(object_format), encoding="ascii")
        except OSError as exc:
            raise MaterializationError(f"{failed}: {exc.strerror or exc}") from exc
        unique = list({entry.oid: entry for entry in files}.values())
        sink = _LooseObjects(objects, unique, object_format, commit)
        try:
            _stream_blobs(runner, repo, commit, sink, [entry.oid for entry in unique])
        finally:
            sink.close()
        env = {
            "GIT_DIR": str(git_dir),
            "GIT_INDEX_FILE": str(scratch / "index"),
            "GIT_OBJECT_DIRECTORY": str(objects),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_ATTR_NOSYSTEM": "1",
        }
        index = b"".join(
            b"%s %s\t%s\0" % (entry.mode, entry.oid.encode("ascii"), entry.path) for entry in files
        )
        collector = _Collector()
        names = [name.decode("ascii") for name in _EOL_ATTRIBUTES]
        steps = (
            # ``-z`` first: ``--index-info`` reads its input as soon as it is parsed.
            (["update-index", "-z", "--index-info"], index),
            (
                ["check-attr", "--cached", "--stdin", "-z", *names],
                b"".join(path + b"\0" for path in paths),
            ),
        )
        for args, data in steps:
            req = local_git_request(
                ["-c", f"core.attributesFile={os.devnull}", *args],
                cwd=str(scratch),
                env=env,
                timeout_seconds=_GIT_TIMEOUT_SECONDS,
            )
            req.stdin_data = data
            if args[0] == "check-attr":
                req.on_chunk = collector
                req.max_output_bytes = _STREAM_CAPTURE_BYTES
            problem = _failure(runner(req), args)
            if problem:
                raise MaterializationError(f"{failed}: {problem}")
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    fields = bytes(collector.data).split(b"\0")
    if fields[-1] or (len(fields) - 1) % 3:
        raise MaterializationError(f"{failed}: `git check-attr` ended mid-record")
    found: dict[bytes, dict[bytes, bytes]] = {}
    for start in range(0, len(fields) - 1, 3):
        path, name, value = fields[start : start + 3]
        found.setdefault(path, {})[name] = value
    conversions: dict[bytes, str] = {}
    for path in paths:
        values = found.get(path, {})
        if any(name not in values for name in _EOL_ATTRIBUTES):
            raise MaterializationError(f"{failed}: `git check-attr` did not answer {_shown(path)}")
        conversions[path] = _crlf_conversion(*(values[name] for name in _EOL_ATTRIBUTES))
    return conversions


def _holds_form(root_fd: int, entry: _Entry, form: _CrlfForm) -> bool:
    """Whether the directory ``root_fd`` holds ``entry`` as its mode's file of exactly ``form``.

    Every directory on the path is opened without following a link, and
    the file is read as :func:`tree_differences` reads one.
    """
    opened: list[int] = []
    try:
        parent = root_fd
        for name in entry.parent.split(b"/") if entry.parent else []:
            fd = _open_listed_dir(parent, name)
            if fd is None:
                return False
            opened.append(fd)
            parent = fd
        try:
            st = os.lstat(entry.name, dir_fd=parent)
        except FileNotFoundError:
            return False
        mode = _EXECUTABLE if st.st_mode & stat.S_IXUSR else _FILE
        if not stat.S_ISREG(st.st_mode) or mode != entry.mode or st.st_size != form.size:
            return False
        _, digest = _file_digest(parent, entry.name, st, lambda _size: hashlib.sha256())
        return digest == form.digest
    finally:
        for fd in opened:
            os.close(fd)


def _crlf_checkouts(
    runner: Runner,
    repo: str,
    commit: str,
    object_format: str,
    listing: _Listing,
    root: Path,
    candidates: Sequence[_Entry],
) -> set[bytes]:
    """The paths of ``candidates`` that ``root`` holds in ``commit``'s own CRLF checkout form.

    ``listing`` is ``commit``'s, as :func:`tree_differences` read it; the
    entries of ``candidates`` are its. Their blobs' ids, and the attributes,
    are taken from it only through trees re-hashed to ``commit``
    (:func:`_eol_conversions`), and each blob's bytes are hashed back to its
    id as its form is computed (:class:`_CrlfForms`).
    """
    conversions = _eol_conversions(
        runner, repo, commit, object_format, listing, [entry.path for entry in candidates]
    )
    converting = [entry for entry in candidates if conversions[entry.path]]
    if not converting:
        return set()
    sink = _CrlfForms(converting, object_format, commit)
    _stream_blobs(runner, repo, commit, sink, [entry.oid for entry in converting])
    held: set[bytes] = set()
    path = b""
    try:
        root_fd = os.open(root, _DIR_FLAGS)
        try:
            for entry in converting:
                path = entry.path
                form = sink.forms[path]
                if form.converts(conversions[path]) and _holds_form(root_fd, entry, form):
                    held.add(path)
        finally:
            os.close(root_fd)
    except OSError as exc:
        raise MaterializationError(
            f"commit {commit}: {_shown(path)} of {root} could not be compared with its "
            f"end-of-line checkout form: {exc.strerror or exc}"
        ) from exc
    return held


def tree_differences(
    runner: Runner,
    repo: str,
    commit: str,
    object_format: str,
    root: Path,
    *,
    checkout_eol: bool = False,
) -> list[TreeDifference]:
    """The entries of ``commit``'s tree that ``root`` does not hold as committed (PR #444 R3-F1).

    ``root`` is a working tree someone else wrote (an agent worktree), not
    an export: what it holds besides the tree's entries (an untracked or
    ignored file, a ``.git``) is not looked at. The tree is listed, sizes
    included, from ``repo``'s store as :func:`materialize` lists it, every
    name refused that it refuses; no git process reads ``root``. Each blob
    is then compared as the proof (D6.3) compares an export: ``lstat``-ed
    relative to the descriptor of its directory, every directory on its
    path opened without following a link (a directory that is a link, a
    file or missing makes everything below it ``deleted``), a regular
    file's execute bit and size compared with the listing before its bytes
    are read and hashed, a symlink's link text hashed. So no attribute,
    filter driver, end-of-line, ``ident`` or ``working-tree-encoding``
    conversion, and no index, takes part: a file reads as the commit's only
    when its bytes are the blob's. A gitlink's path reads as the commit's
    only as an empty real directory, which is what a checkout leaves when
    it does not populate the submodule (PR #444 R4-F1): a populated
    submodule's content is the commit of a repository of its own, whose
    store and configuration are the worktree's writer's, so it is never
    proven the gitlink's, and anything else there (files without a
    repository, which ``git status`` does not list whatever its
    submodule settings) is not the commit's either. ``object_format`` names
    the store's format (:func:`read_object_format`). In listing order.

    ``checkout_eol`` (#488): a regular file of its mode whose bytes are not
    the blob's is compared once more, with the bytes a checkout writes for
    it under the end-of-line conversion ``commit``'s own ``.gitattributes``
    give it, and is not listed when it holds exactly those. That is
    ``eol=crlf`` alone (``text=auto eol=crlf`` only where git takes the
    blob as text): a CR put before every LF that does not follow one,
    computed here from the blob's bytes, hashed back to its id as they
    stream, with the attributes read from ``commit``'s tree by git in a git
    directory of the controller's own (:func:`_eol_conversions`), never
    from ``root``, ``info/attributes`` or any configuration. What decides
    that conversion is authenticated first (PR #503 R1-F1): the tree comes
    from the commit's re-hashed bytes, each directory on the file's path
    hashes back to its tree from the listing's entries, so the file's blob
    id and every ``.gitattributes`` read for it are the commit's, and git
    reads those attributes only from copies the controller hashed, in an
    object directory of its own, never from the store, where a rewritten
    object would read as the original. Both forms a file may then hold are
    functions of the commit alone, what a checkout of it writes with no
    configuration, so an edit a clean filter maps back to the blob holds
    neither (R3-F1), and any other conversion (a filter driver, Git LFS,
    ``ident``, ``working-tree-encoding``, ``core.autocrlf``) is still a
    difference. No git process reads ``root`` here either, and none runs at
    all unless a file's bytes differ.

    Raises :class:`~autoforge.errors.CandidateNotMaterializableError` when
    ``commit`` is not a commit of the store, and
    :class:`~autoforge.errors.MaterializationError` when the commit's bytes
    do not hash to its id, the tree cannot be listed or a file cannot be
    read (a directory without permission, a file replaced during the read),
    or, with ``checkout_eol``, when the attributes or a blob cannot be read
    or a tree, an attributes file or a blob on the way is not the bytes its
    id names: never taken as no difference.
    """
    tree = _commit_tree(runner, repo, commit, object_format)
    listing = _list_tree(runner, repo, commit, tree, object_format, sizes=True)
    found: list[TreeDifference] = []
    # The regular files listed as modified: another execute bit, size or bytes.
    rewritten: list[_Entry] = []
    stack: list[tuple[bytes, int | None]] = []
    path = b""
    try:
        stack.append((b"", os.open(root, _DIR_FLAGS)))
        for entry in listing.entries:
            path = entry.path
            while stack and stack[-1][0] != entry.parent:
                fd = stack.pop()[1]
                if fd is not None:
                    os.close(fd)
            if not stack:
                raise MaterializationError(
                    f"commit {commit}: the entry {_shown(path)} is listed after its parent tree "
                    "was complete"
                )
            parent = stack[-1][1]
            if entry.mode == _TREE:
                stack.append((entry.path, _open_listed_dir(parent, entry.name)))
            elif entry.mode == _GITLINK:
                kind = _gitlink_difference(parent, entry)
                if kind:
                    found.append(TreeDifference(entry.path, kind, submodule=True))
            else:
                kind = _blob_difference(object_format, parent, entry)
                if kind:
                    found.append(TreeDifference(entry.path, kind))
                    if kind == "modified" and entry.mode != _SYMLINK:
                        rewritten.append(entry)
    except OSError as exc:
        raise MaterializationError(
            f"commit {commit}: {_shown(path)} of {root} could not be compared with its tree "
            f"{listing.tree}: {exc.strerror or exc}"
        ) from exc
    finally:
        for _, fd in stack:
            if fd is not None:
                os.close(fd)
    if not checkout_eol or not rewritten:
        return found
    held = _crlf_checkouts(runner, repo, commit, object_format, listing, root, rewritten)
    return [difference for difference in found if difference.path not in held]


@contextmanager
def materialize(
    runner: Runner, repo: str, commit: str, object_format: str, *, prefix: str
) -> Iterator[MaterializedTree]:
    """Write ``commit``'s tree into a fresh private directory, prove it, then remove it.

    The directory is created by :func:`tempfile.mkdtemp` (mode ``0700``) in
    the system temporary directory, its name starting with ``prefix``; it is
    yielded only once proven, and removed when the context exits, on an
    exception too.

    Raises :class:`~autoforge.errors.CandidateNotMaterializableError` when
    the commit or a blob it names is not in ``repo``'s object store, and
    :class:`~autoforge.errors.MaterializationError` when the tree cannot be
    listed, written or proven.
    """
    root = Path(tempfile.mkdtemp(prefix=prefix))
    try:
        tree = _commit_tree(runner, repo, commit, object_format)
        listing = _list_tree(runner, repo, commit, tree, object_format)
        _write(runner, repo, commit, listing, root)
        _prove(root, listing, commit, object_format)
        modes = [entry.mode for entry in listing.entries]
        yield MaterializedTree(
            commit=commit,
            tree=tree,
            object_format=object_format,
            root=root,
            files=sum(mode not in (_TREE, _GITLINK) for mode in modes),
            gitlinks=modes.count(_GITLINK),
        )
    finally:
        shutil.rmtree(root, ignore_errors=True)
