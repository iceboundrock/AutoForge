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
  read from the commit itself.
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

Every git process is the hardened local request
(:func:`~autoforge.git_transport.local_git_request`, ADR 0004 D7.3, D7.5):
no replacement objects, no commit-graph, no hooks, an allow-listed
environment. The directory is removed when the context exits, whatever
happened; a killed process leaves only that directory, which is never read
again.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import tempfile
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

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
    """One ``ls-tree`` record: ``path`` is ``parent`` and ``name`` joined by ``/``."""

    path: bytes
    parent: bytes
    name: bytes
    mode: bytes
    oid: str


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
    """The tree ``commit`` names, after it is found to be a commit in the store."""
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
    tree = _git(runner, repo, ["rev-parse", "--verify", "--quiet", f"{commit}^{{tree}}"]).strip()
    if not _is_oid(tree.encode("ascii", "replace"), object_format):
        raise MaterializationError(f"commit {commit} names no readable tree: {tree[:100]!r}")
    return tree


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


def _list_tree(runner: Runner, repo: str, commit: str, tree: str, object_format: str) -> _Listing:
    """Every entry of ``tree``, validated before anything is written."""
    args = ["ls-tree", "-r", "-t", "-z", "--full-tree", tree]
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
        fields = meta.split(b" ")
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
        entries.append(_Entry(path, parent, name, mode, oid.decode("ascii")))
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


class _BlobStream:
    """An ``on_chunk`` hook parsing ``git cat-file --batch`` into the writer.

    Each blob arrives as ``<id> blob <size>\\n<content>\\n``, in the order
    the ids were asked for. The first problem is kept and ``stop`` is set,
    so the executor ends the process; nothing is read after it, or after
    :meth:`close`.
    """

    def __init__(self, writer: _Writer, commit: str) -> None:
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


def _write(runner: Runner, repo: str, commit: str, listing: _Listing, root: Path) -> None:
    """Write ``listing`` below ``root``, the blobs streamed from the store."""
    writer = _Writer(root, listing, commit)
    try:
        stream = _BlobStream(writer, commit)
        blobs = [e.oid for e in listing.entries if e.mode not in (_TREE, _GITLINK)]
        if blobs:
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
                raise MaterializationError(
                    f"commit {commit}: streaming its blobs failed: {problem}"
                )
            if not stream.done:
                raise MaterializationError(
                    f"commit {commit}: git sent fewer blobs than the tree lists"
                )
        writer.finish()
    finally:
        writer.close()


def _blob_id(algorithm: str, content: bytes) -> str:
    return hashlib.new(algorithm, b"blob %d\0" % len(content) + content).hexdigest()


def _file_id(algorithm: str, dir_fd: int, name: bytes, seen: os.stat_result) -> tuple[bytes, str]:
    """The mode and blob id of the regular file ``name``, from its bytes on disk."""
    fd = os.open(name, _READ_FLAGS, dir_fd=dir_fd)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or (st.st_dev, st.st_ino) != (seen.st_dev, seen.st_ino):
            raise OSError(0, "replaced while it was proved")
        digest = hashlib.new(algorithm, b"blob %d\0" % st.st_size)
        total = 0
        while chunk := os.read(fd, _READ_CHUNK_BYTES):
            digest.update(chunk)
            total += len(chunk)
        if total != st.st_size:
            raise OSError(0, "changed size while it was proved")
    finally:
        os.close(fd)
    return (_EXECUTABLE if st.st_mode & stat.S_IXUSR else _FILE), digest.hexdigest()


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
