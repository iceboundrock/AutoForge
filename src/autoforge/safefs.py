"""One filesystem capability boundary for every write the controller performs.

Why this module exists
----------------------

AutoForge runs coding and review agents as *the same operating-system user*,
against the same checkout, with no sandbox.  Every artifact the controller
writes therefore lands in a directory tree another process can rearrange
between one syscall and the next.  The historical failure mode was not that
any single check was wrong, it was that each write site grew its *own* set of
checks: one module refused a symlinked final component, another refused a
FIFO, a third remembered hard links, none of them covered a *parent*
directory that had been replaced by a symbolic link after the check ran.
That is an open-ended matrix (entry kind x path position x operation) and it
cannot be closed by adding cases to it.

The closed formulation is a *capability*: a directory is opened once, and
from then on the only way to name anything beneath it is relative to that
open descriptor.

``SafeRoot`` holds an open descriptor on a directory.  Every operation walks
to the target one component at a time with ``O_DIRECTORY | O_NOFOLLOW`` and
``dir_fd=``, so:

* no component of the path -- not the last one, not any parent -- can be a
  symbolic link;
* renaming or replacing a directory after the walk started cannot redirect
  the write, because the descriptors already resolved are the destination;
* the final open adds ``O_NOFOLLOW`` (a link fails the open), ``O_NONBLOCK``
  and ``O_NOCTTY`` (a FIFO or a device cannot stall or steal a terminal), and
  an ``fstat`` on the descriptor rejects every non-regular kind;
* a write open requires ``st_nlink == 1``: more is a hard link, a second
  name for an ordinary file that passes every other test, and none is an
  inode unlinked between the open and the inspection (or a filesystem that
  reports no link count), which a write would reach and report success;
* ``O_TRUNC`` is never handed to ``os.open`` -- the kernel applies it during
  the open, which is before the descriptor can be inspected -- so a file is
  opened intact, validated, and only then truncated through ``ftruncate`` on
  the very descriptor that was validated.

Whole-file writes do not truncate at all: they write and fsync a fresh
temporary inode in the target's own directory and ``os.replace`` it over the
target (or, for an exclusive create, ``link(2)`` it to the final name) with
both sides named by descriptor.  That replaces a *name*, never an inode, so
a hard link planted at the target survives untouched, and a reader either
sees the whole old file or the whole new one.  On Linux the temporary is an
``O_TMPFILE`` inode: it has *no name at all* while the bytes are written and
receives its first one when it is published, through the process's own
``/proc/self/fd`` entry.  Where that is unavailable (macOS, a filesystem
without ``O_TMPFILE``, no procfs) the temporary is created with
``O_CREAT | O_EXCL`` instead.  Either way the inode is one this process
created, so the bytes can only ever land on something the controller made --
but a named temporary sits in a directory another process can list, and that
process can give the inode a second name (``link(2)``) between the create and
the write.  The descriptor is therefore re-inspected after the write and
before the publish, on both paths: an inode that has acquired a name it was
not given by the controller is emptied through the still-open descriptor,
unlinked and reported, never renamed or linked over the target.

The one write that is not a whole-file replacement is the append
(``open_append`` then ``append_to``): a journal line is written through an
inode opened with ``O_APPEND`` and inspected as a regular file with exactly
one name, so the cost of an append is the line rather than the file.  The
descriptor is *held* between the open and the append, for as long as the
caller likes (the run logger holds it across an agent invocation), and the
bytes go through that descriptor and no other: a held descriptor keeps its
inode allocated, so no rename, link, unlink or inode-number reuse by another
process can make the append reach an inode other than the one that was
inspected.  Immediately before the write the append proves again, without
reading a byte, that the name still resolves under the root to that inode,
that the inode still has one name, and that it is within the caller's size
limit; a file renamed over the name, a link given to the inode, or the name
unlinked or moved away in between is refused, and nothing is written to
either file.

What this does and does not guarantee
-------------------------------------

Guaranteed: a controller write always lands on an inode the controller itself
created (or opened as a regular file with one name), reached through the
directory descriptor that was opened as the root, or it fails; and a name the
controller *publishes* -- the final rename or link -- names an inode that had
exactly one name at the last inspection before the publish.  No sequence of
symlink, hard link, FIFO, device, directory-replacement or rename operations
performed by another process under the same user can make a controller write
reach an inode other than that one: never an object the controller did not
create or inspect, and never through a name it did not resolve descriptor by
descriptor.  This is a property of descriptor-relative addressing, not of a
list of rejected shapes, so a shape nobody has thought of yet is covered too.
The guarantee is about the *inode*; where that inode's name is by the time
the bytes land is the next paragraph.

Not guaranteed everywhere: that a same-user process cannot *observe* a
controller write through a name it planted.  On the named-temporary path a
temporary can be hard-linked out of the root in the window between its
creation and the write, and the bytes reach that link before the post-write
inspection detects it; the write is then refused and the inode emptied, but
the other process may already have read it.  Every byte the controller
writes is already readable by that process (same user, same files), so this
discloses nothing, and it can never make a controller write *replace*
anything: the inode was created by this process, empty, an instant earlier.
On the Linux path there is no name to link while the bytes are written.  An
exclusive create has no window at all: the inode's first name is the final
one.  A whole-file replacement must still pass through a temporary name,
because ``linkat`` cannot replace, and holds it for the two syscalls between
the link and the rename with the re-inspection in between; a link planted
there finds the bytes complete and is detected before the rename, which is
the same observe-but-never-replace outcome as the named path in a window
that no longer spans the write.  What remains on every platform, and is
inherent, is that a same-user process which can open this process's
``/proc/<pid>/fd`` (that is a ptrace-scope question, not a filesystem one)
can name the unnamed inode the way the controller itself does; that is why
the pre-publish inspection requires *zero* names on the Linux path, and why
such a temporary is emptied and refused exactly like a named one.  The
append's window is one syscall wide and has
the same shape: between the re-inspection that immediately precedes the
``write(2)`` and the write itself, the other process can still rename the
journal's inode out of the root (or link it out and unlink the root name),
and the line then lands in the controller's own journal under its new name
with the append reporting success; or unlink the name outright, and the
line is written into an inode nothing names and lost with it.  That is the
controller's *own* bytes moved or dropped by a process that could already
read, copy or delete every one of them.  What it can never be is a write
into a foreign file: the bytes go through the descriptor that was
inspected, so a file put at the journal's name -- renamed there, linked
there, planted after an unlink -- receives nothing in any window, and is
refused by the re-inspection in every window but that one syscall.  The
move itself is not checked for beyond that, since it can follow any check;
it is stated here and pinned by a test.  ``O_TMPFILE`` does not help the
append, which must extend a named file.

Also not guaranteed: the *initial* resolution of the root path itself.
``SafeRoot.open`` resolves an ordinary pathname, and a process that can
redirect that pathname in the instant before the open can point the
controller at a different directory.  That is equivalent to being able to
redirect the repository checkout itself and no pathname-based defence can
close it; it is stated here rather than papered over.  Everything *below* the
root is closed.
"""

from __future__ import annotations

import errno
import os
import secrets
import stat
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from .errors import StateError

# Absent on Windows; 0 makes the flag a no-op rather than an AttributeError.
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_O_NOCTTY = getattr(os, "O_NOCTTY", 0)
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_O_ACCMODE = getattr(os, "O_ACCMODE", 3)

_DIR_OPEN_FLAGS = os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC

#: Linux only: an inode created with no directory entry, which receives its
#: first name when it is published (``SafeRoot._unnamed_tmp_at``).  0 where
#: the platform has no such thing, and the named temporary is used instead.
_O_TMPFILE = getattr(os, "O_TMPFILE", 0)

#: Directory-relative syscalls this module cannot work without.  They exist on
#: Linux and modern BSD/macOS; refusing early beats silently degrading to
#: pathname operations that do not hold the invariant.
_REQUIRED_DIR_FD = ("open", "mkdir", "rename", "unlink", "stat", "link", "readlink")


def entry_kind(mode: int) -> str | None:
    """Human name of a non-regular entry kind, or ``None`` for a regular file."""
    if stat.S_ISREG(mode):
        return None
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISFIFO(mode):
        return "FIFO"
    if stat.S_ISSOCK(mode):
        return "socket"
    if stat.S_ISCHR(mode):
        return "character device"
    if stat.S_ISBLK(mode):
        return "block device"
    if stat.S_ISLNK(mode):
        return "symbolic link"
    return "special file"


class UnsafePathError(StateError):
    """A controller path resolves to something the controller may not write.

    A subclass of :class:`~autoforge.errors.StateError` so existing callers
    and the CLI's error mapping keep working, but distinguishable for tests
    and for callers that want to report the filesystem cause specifically.
    """


def _denied(exc: OSError) -> bool:
    """True when the kernel refused for lack of permission, not for shape."""
    return exc.errno in (errno.EACCES, errno.EPERM)


def _denied_text(exc: OSError) -> str:
    """The kernel's own wording, without the filename it repeats back."""
    return exc.strerror or os.strerror(exc.errno or errno.EACCES)


class ReadLimitExceeded(StateError):
    """A bounded operation met a file larger than the limit the caller set.

    Raised by :meth:`SafeRoot.read_bytes` when a read would have to hold
    more than the limit, and by :meth:`SafeRoot.open_append` and
    :meth:`SafeRoot.append_to` when the file is already larger than it: in
    every case the file is refused before it is read or written.
    """

    def __init__(self, relpath: str, limit: int) -> None:
        super().__init__(f"{relpath} is larger than {limit} bytes")
        self.relpath = relpath
        self.limit = limit


class WalkBudgetExceeded(StateError):
    """A bounded :meth:`SafeRoot.walk` listed more entries than its budget."""

    def __init__(self, max_entries: int, where: str) -> None:
        super().__init__(
            f"the walk listed more than {max_entries} directory entries (while listing {where})"
        )
        self.max_entries = max_entries
        self.where = where


class UnreadableEntryError(StateError):
    """An entry exists, is of a kind the controller accepts, and cannot be read.

    This is *not* an unsafe path: nothing about it could redirect a write.  It
    is an access fact -- ``EACCES``/``EPERM`` on an open or a listing -- and
    what it means is the caller's decision, so it is reported as its own type
    carrying the path rather than folded into a generic failure.  A controller
    write path treats it as a plain failure; workspace identity translates it
    into a refusal to bind, because a file that cannot be read is a file that
    cannot be proven unchanged.
    """

    def __init__(self, path: str, message: str) -> None:
        super().__init__(message)
        self.path = path


def _hard_link_text(nlink: int) -> str:
    return (
        f"a hard link: {nlink} directory entries name this file, so writing here would "
        "alter a file AutoForge did not create"
    )


def _unsafe(where: str, what: str) -> UnsafePathError:
    return UnsafePathError(
        f"refusing to use {where}: it is {what}. AutoForge only reads and writes its own "
        "regular files and directories there; an entry it did not create could redirect a "
        "controller write outside its root or block the controller indefinitely. "
        "Move the entry aside and re-run."
    )


def _empty_and_refuse(fd: int, where: str, what: str) -> UnsafePathError:
    """Empty the controller's own temporary through ``fd`` and return the
    refusal to publish it, for the caller to raise.

    Every temporary is inspected once its bytes are complete, and one that
    another process has given a name of its own is not published: it is
    emptied first, so that the other name keeps an empty file and not the
    controller's bytes, and then refused.  The emptying is a syscall that can
    fail too, and a failure of it is *this write's* failure -- the bytes
    still sit behind the foreign name -- reported through the controller's
    error type with both facts in it, never as a raw ``OSError`` that the
    CLI's error boundary does not catch.
    """
    try:
        os.ftruncate(fd, 0)
    except OSError as exc:
        raise StateError(
            f"cannot write {where}: it is {what}, and the controller's bytes could not "
            f"be emptied from that temporary: {exc}"
        ) from exc
    return _unsafe(where, what)


def split_relpath(relpath: str) -> tuple[str, ...]:
    """Validate ``relpath`` as a root-relative path and return its components.

    Rejects absolute paths, empty components, ``.``, ``..`` and NUL.  This is
    the only place a caller-supplied (and therefore possibly state-file- or
    config-supplied) name becomes a sequence of directory-relative lookups,
    so it is the only place the rule has to hold.
    """
    if not isinstance(relpath, str) or not relpath:
        raise UnsafePathError(f"invalid controller path {relpath!r}: it must be a non-empty string")
    if "\0" in relpath:
        raise UnsafePathError(f"invalid controller path {relpath!r}: it contains a NUL byte")
    if relpath.startswith("/") or (os.sep != "/" and relpath.startswith(os.sep)):
        raise UnsafePathError(
            f"invalid controller path {relpath!r}: it must be relative to the controller root"
        )
    parts = tuple(p for p in relpath.replace(os.sep, "/").split("/") if p != "")
    if not parts:
        raise UnsafePathError(f"invalid controller path {relpath!r}: it names no entry")
    for part in parts:
        if part in (".", ".."):
            raise UnsafePathError(
                f"invalid controller path {relpath!r}: {part!r} is not a name the controller "
                "may traverse; every component must be a plain entry name"
            )
    return parts


def open_regular_at(
    dir_fd: int,
    name: str,
    flags: int,
    *,
    mode: int = 0o600,
    where: str | None = None,
) -> int:
    """``os.open`` beneath ``dir_fd``, restricted to a regular file with one name.

    Returns a descriptor the caller owns.  See the module docstring for why
    ``O_TRUNC`` is applied afterwards with ``ftruncate`` rather than passed to
    the open, and why a write open insists on a link count of one.
    """
    label = where or name
    truncate = bool(flags & os.O_TRUNC)
    writing = (flags & _O_ACCMODE) in (os.O_WRONLY, os.O_RDWR)
    open_flags = (flags & ~os.O_TRUNC) | _O_NOFOLLOW | _O_NONBLOCK | _O_NOCTTY | _O_CLOEXEC
    try:
        fd = os.open(name, open_flags, mode, dir_fd=dir_fd)
    except FileNotFoundError:
        # Absence is the caller's business (see SafeRoot.read_bytes).
        raise
    except FileExistsError:
        # O_EXCL: also the caller's business.
        raise
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.EMLINK):
            raise _unsafe(label, "a symbolic link") from exc
        if exc.errno == errno.ENXIO:
            # O_WRONLY|O_NONBLOCK on a FIFO with no reader.
            raise _unsafe(label, "a FIFO") from exc
        if exc.errno == errno.EISDIR:
            # The kernel refuses a writable open before fstat can report it.
            raise _unsafe(label, "a directory, not a regular file") from exc
        if _denied(exc):
            raise UnreadableEntryError(label, f"cannot open {label}: {_denied_text(exc)}") from exc
        raise StateError(f"cannot open {label}: {exc}") from exc
    try:
        st = os.fstat(fd)
        kind = entry_kind(st.st_mode)
        if kind is not None:
            raise _unsafe(label, f"a {kind}, not a regular file")
        # A write open requires exactly one name. More than one is a hard
        # link, and the wording is mode-neutral: a truncating open would
        # replace the shared inode's contents, an appending one would extend
        # them, and either way the change shows at the other name. None at
        # all means the name was unlinked between the open and this
        # inspection (the descriptor still holds the inode, so a write would
        # go into a file nothing names and report success), or that the
        # filesystem reports no link count, in which case a second name can
        # never be ruled out; both are refused rather than written to.
        if writing and st.st_nlink > 1:
            raise _unsafe(label, _hard_link_text(st.st_nlink))
        if writing and st.st_nlink < 1:
            raise _unsafe(
                label,
                "a file no directory entry names: it was unlinked after it was opened, or "
                "this filesystem reports no link count, so a write here could not be "
                "proved to reach a single-named file",
            )
        if truncate:
            os.ftruncate(fd, 0)
    except BaseException:
        os.close(fd)
        raise
    return fd


def readlink_at(dir_fd: int, name: str) -> str:
    """Read a symbolic link's target text beneath ``dir_fd``."""
    return os.readlink(name, dir_fd=dir_fd)


@dataclass
class WalkEntry:
    """One entry produced by :meth:`SafeRoot.walk`.

    ``dir_fd`` is the descriptor of the entry's *parent* directory and is
    valid only until the walk advances; use it with :func:`open_regular_at` or
    :func:`readlink_at` while handling the entry, never afterwards.  Set
    :attr:`skip` on a directory entry to stop the walk descending into it.
    """

    relpath: str
    name: str
    dir_fd: int
    st: os.stat_result
    skip: bool = False

    @property
    def is_dir(self) -> bool:
        return stat.S_ISDIR(self.st.st_mode)

    @property
    def is_symlink(self) -> bool:
        return stat.S_ISLNK(self.st.st_mode)

    @property
    def is_regular(self) -> bool:
        return stat.S_ISREG(self.st.st_mode)


class AppendHandle:
    """A regular file held open for appending, bound to the inode it was proved on.

    Made by :meth:`SafeRoot.open_append` and written through by
    :meth:`SafeRoot.append_to`.  The handle *is* the proof: when it was
    opened, the descriptor was inspected as a single-named regular file at
    ``relpath`` under its root, within ``limit``; holding it keeps that inode
    allocated, so its number cannot be reused by a file another process
    creates, and every append through it can reach that inode and no other.
    The instance owns the descriptor and must be closed (it is a context
    manager); dropping it closes it too, as descriptor hygiene rather than
    as anything safety depends on.
    """

    __slots__ = ("_fd", "_relpath", "_dev", "_ino", "_limit", "_closed")

    def __init__(self, fd: int, relpath: str, st: os.stat_result, limit: int | None) -> None:
        self._fd = fd
        self._relpath = relpath
        self._dev = st.st_dev
        self._ino = st.st_ino
        self._limit = limit
        self._closed = False

    @property
    def fd(self) -> int:
        if self._closed:
            raise StateError(f"append handle on {self._relpath} is already closed")
        return self._fd

    @property
    def relpath(self) -> str:
        """The root-relative name the file was opened at."""
        return self._relpath

    @property
    def identity(self) -> tuple[int, int]:
        """``(st_dev, st_ino)`` of the inode the handle holds."""
        return (self._dev, self._ino)

    @property
    def limit(self) -> int | None:
        """The size past which an append through this handle is refused."""
        return self._limit

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            os.close(self._fd)

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:  # pragma: no cover - interpreter shutdown
            pass

    def __enter__(self) -> AppendHandle:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"AppendHandle({self._relpath!r})"


class SafeRoot:
    """An open directory descriptor plus descriptor-relative operations on it.

    Construct with :meth:`open`.  The instance owns a descriptor and must be
    closed (it is a context manager).  Every method takes a *root-relative*
    path and reaches it one component at a time; nothing below the root is
    ever addressed by pathname.
    """

    __slots__ = ("_fd", "_path", "_dev", "_ino", "_closed")

    def __init__(self, fd: int, path: Path, st: os.stat_result) -> None:
        self._fd = fd
        self._path = path
        self._dev = st.st_dev
        self._ino = st.st_ino
        self._closed = False

    # -- construction -------------------------------------------------------
    @classmethod
    def open(cls, path: str | Path, *, create: bool = False) -> SafeRoot:
        """Open ``path`` as a capability root.

        ``create`` makes the root (and its parents) if absent.  The root path
        itself is an ordinary pathname resolution -- see the module docstring
        for exactly what that does and does not promise.
        """
        missing = [name for name in _REQUIRED_DIR_FD if name not in _supports_dir_fd()]
        if missing:
            raise StateError(
                "this platform does not support directory-relative "
                f"{', '.join(missing)}; AutoForge cannot guarantee that a controller write "
                "stays inside its root here and refuses to run rather than pretend"
            )
        p = Path(path)
        if create:
            _mkdir_tree(p)
        try:
            fd = os.open(p, _DIR_OPEN_FLAGS & ~_O_NOFOLLOW)
        except NotADirectoryError as exc:
            raise _unsafe(str(p), "not a directory") from exc
        except FileNotFoundError:
            raise
        except OSError as exc:
            if exc.errno == errno.ENOTDIR:
                raise _unsafe(str(p), "not a directory") from exc
            raise StateError(f"cannot open controller root {p}: {exc}") from exc
        try:
            st = os.fstat(fd)
            if not stat.S_ISDIR(st.st_mode):  # pragma: no cover - O_DIRECTORY covers it
                raise _unsafe(str(p), f"a {entry_kind(st.st_mode)}, not a directory")
        except BaseException:
            os.close(fd)
            raise
        return cls(fd, p, st)

    def subroot(self, relpath: str, *, create: bool = False) -> SafeRoot:
        """A :class:`SafeRoot` on a directory beneath this one.

        Reached by the same component-at-a-time walk, so the sub-root is a
        directory that was actually found inside this root: it cannot be a
        symbolic link, and it stays the directory it was even if its name is
        later reused for something else.
        """
        parts = split_relpath(relpath)
        if create:
            self.ensure_dir(relpath)
        fd = self._walk(parts, create=False)
        try:
            st = os.fstat(fd)
        except BaseException:  # pragma: no cover - the open just succeeded
            os.close(fd)
            raise
        return SafeRoot(fd, self._path / Path(*parts), st)

    # -- identity -----------------------------------------------------------
    @property
    def path(self) -> Path:
        """The pathname the root was opened through (for messages only)."""
        return self._path

    @property
    def fd(self) -> int:
        if self._closed:
            raise StateError(f"controller root {self._path} is already closed")
        return self._fd

    @property
    def identity(self) -> tuple[int, int]:
        """``(st_dev, st_ino)`` of the directory this root is bound to."""
        return (self._dev, self._ino)

    def verify_identity(self, *, role: str = "controller root") -> None:
        """Fail if the root's pathname no longer names the directory we hold.

        The controller's writes are safe either way -- they go to the
        descriptor -- but a root that has been renamed or replaced means the
        operator is no longer looking at the files the controller is writing,
        so continuing silently would be the wrong kind of correct. The check
        is by ``lstat``: a symbolic link planted at the pathname is reported
        as what it is rather than followed to wherever it points.
        ``role`` names the root in the message (``"state directory"``).
        """
        if self._closed:
            raise StateError(f"{role} {self._path} is already closed")
        try:
            st = os.lstat(self._path)
        except OSError as exc:
            raise StateError(
                f"{role} {self._path} is no longer readable: {exc}. "
                "It was moved or replaced while the controller was running."
            ) from exc
        if stat.S_ISLNK(st.st_mode):
            raise _unsafe(
                f"{role} {self._path}",
                "a symbolic link now, planted while the controller was running",
            )
        if (st.st_dev, st.st_ino) != (self._dev, self._ino):
            raise StateError(
                f"{role} {self._path} was replaced while the controller was running: "
                "it now names a different directory. Refusing to continue against a root that "
                "is no longer the one this run started in."
            )

    def dup(self) -> SafeRoot:
        """An independent handle on the same directory (closed separately).

        For handing the held capability to a component that closes what it
        is given: the copy shares the inode binding, not the lifetime.
        """
        fd = os.dup(self.fd)
        try:
            st = os.fstat(fd)
        except BaseException:  # pragma: no cover - the dup just succeeded
            os.close(fd)
            raise
        return SafeRoot(fd, self._path, st)

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            os.close(self._fd)

    def __del__(self) -> None:
        # Descriptor hygiene only: a root dropped without ``close`` must not
        # leak its descriptor for the rest of the process. Nothing about
        # safety depends on finalisation running.
        try:
            self.close()
        except Exception:  # pragma: no cover - interpreter shutdown
            pass

    def __enter__(self) -> SafeRoot:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"SafeRoot({str(self._path)!r})"

    # -- traversal ----------------------------------------------------------
    def _walk(self, parts: tuple[str, ...], *, create: bool) -> int:
        """Return a descriptor on the directory named by ``parts``.

        Every component is opened with ``O_DIRECTORY | O_NOFOLLOW`` relative
        to the previous one.  The caller owns the returned descriptor.
        """
        fd = os.dup(self.fd)
        try:
            for depth, part in enumerate(parts):
                if create:
                    made = False
                    try:
                        os.mkdir(part, 0o700, dir_fd=fd)
                        made = True
                    except FileExistsError:
                        pass
                    except OSError as exc:
                        where = self._describe(parts[: depth + 1])
                        raise StateError(
                            f"cannot create controller directory {where}: {exc}"
                        ) from exc
                    if made:
                        _fsync_fd(fd)
                try:
                    nxt = os.open(part, _DIR_OPEN_FLAGS, dir_fd=fd)
                except FileNotFoundError:
                    raise
                except OSError as exc:
                    where = self._describe(parts[: depth + 1])
                    if exc.errno in (errno.ELOOP, errno.EMLINK, errno.ENOTDIR):
                        raise _unsafe(where, _kind_at(fd, part, "not a directory")) from exc
                    raise StateError(f"cannot open controller directory {where}: {exc}") from exc
                os.close(fd)
                fd = nxt
            return fd
        except BaseException:
            os.close(fd)
            raise

    def _describe(self, parts: tuple[str, ...]) -> str:
        return str(self._path / Path(*parts)) if parts else str(self._path)

    def _parent_of(self, parts: tuple[str, ...], *, create: bool) -> int:
        return self._walk(parts[:-1], create=create)

    # -- directories --------------------------------------------------------
    def ensure_dir(self, relpath: str) -> None:
        """Create ``relpath`` and every parent, proving each is a real directory."""
        parts = split_relpath(relpath)
        os.close(self._walk(parts, create=True))

    # -- reads --------------------------------------------------------------
    def lstat(self, relpath: str) -> os.stat_result | None:
        """``lstat`` of ``relpath``; ``None`` when it does not exist."""
        parts = split_relpath(relpath)
        try:
            parent = self._parent_of(parts, create=False)
        except FileNotFoundError:
            return None
        try:
            return os.lstat(parts[-1], dir_fd=parent)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise StateError(f"cannot inspect {self._describe(parts)}: {exc}") from exc
        finally:
            os.close(parent)

    def read_bytes(self, relpath: str, *, limit: int | None = None) -> bytes | None:
        """Read ``relpath``; ``None`` when it (or a parent) does not exist.

        With a ``limit`` the read stops one byte past it and raises
        :class:`ReadLimitExceeded`: a file the caller has decided is too
        large to hold is never materialised, whatever ``st_size`` claimed
        before the file was opened.
        """
        parts = split_relpath(relpath)
        try:
            parent = self._parent_of(parts, create=False)
        except FileNotFoundError:
            return None
        try:
            fd = open_regular_at(parent, parts[-1], os.O_RDONLY, where=self._describe(parts))
        except FileNotFoundError:
            return None
        finally:
            os.close(parent)
        with os.fdopen(fd, "rb") as fh:
            return _read_within(fh, relpath, limit)

    def read_text(self, relpath: str, *, limit: int | None = None) -> str | None:
        data = self.read_bytes(relpath, limit=limit)
        if data is None:
            return None
        return data.decode("utf-8", errors="replace")

    # -- writes -------------------------------------------------------------
    def write_bytes(self, relpath: str, data: bytes, *, mode: int = 0o600) -> None:
        """Atomically make ``relpath`` hold exactly ``data``.

        A fresh temporary is created in the target's own directory, written,
        fsynced and renamed over the target, with both sides named relative to
        the same directory descriptor.  Nothing is ever truncated in place, so
        an existing hard link, and any reader of the old contents, is left
        intact.
        """
        parts = split_relpath(relpath)
        parent = self._parent_of(parts, create=True)
        try:
            self._replace_at(parent, parts[-1], data, mode=mode, where=self._describe(parts))
        finally:
            os.close(parent)

    def write_text(self, relpath: str, text: str, *, mode: int = 0o600) -> None:
        self.write_bytes(relpath, text.encode("utf-8"), mode=mode)

    def create_exclusive(self, relpath: str, data: bytes, *, mode: int = 0o600) -> None:
        """Create ``relpath`` holding exactly ``data``, or fail because an
        entry of that name already exists.

        The name is published only after the bytes are durable: ``data`` is
        written and fsynced into a fresh temporary in the same directory and
        then *linked* to ``relpath``.  ``link(2)`` fails with ``EEXIST`` on any
        existing entry -- a symbolic link included -- so the exclusivity is
        the same as ``O_CREAT | O_EXCL`` would give, but a crash can no longer
        leave a half-written file behind under the final name for a later
        invocation to find and trust.
        """
        parts = split_relpath(relpath)
        parent = self._parent_of(parts, create=True)
        try:
            self._publish_new_at(parent, parts[-1], data, mode=mode, where=self._describe(parts))
        finally:
            os.close(parent)

    def open_append(
        self, relpath: str, *, mode: int = 0o600, limit: int | None = None
    ) -> AppendHandle:
        """Open ``relpath`` for appending, creating it if needed, and hold it.

        The one write that is not a whole-file replacement. Replacing the
        name would mean reading the existing bytes and publishing them again
        with the new ones, which costs the whole file on every append; a
        journal appended to N times would be rewritten N times. The append
        therefore goes through an opened inode: ``O_WRONLY | O_CREAT |
        O_APPEND`` with the same ``O_NOFOLLOW | O_NONBLOCK | O_NOCTTY`` open
        as every other controller open, and the descriptor is inspected
        before it is handed back -- a regular file (no link, FIFO, device or
        directory) with exactly one name, or :class:`UnsafePathError`; with
        a ``limit``, one no larger than it, or :class:`ReadLimitExceeded`.
        Nothing is read, so the cost is the open whatever the file's size.

        The descriptor is returned rather than closed so that the proof
        outlives this call: :meth:`append_to` writes through it, and only
        through it, so a caller that opens the file at one moment (before an
        agent is launched) and appends at another (after it returned) is
        guaranteed that the bytes reach the inode inspected at the first
        moment or no inode at all. The caller owns the handle and closes it;
        until the handle exists, this call does, and a failure anywhere
        after the open -- the size check, or the directory ``fsync`` that
        makes a name this open created durable -- closes the descriptor
        before it raises, so a refused open never leaks it. An ``OSError``
        there is a :class:`~autoforge.errors.StateError`, as one at the
        open itself is; the file, if the open created it, is left where it
        is, since an empty journal is what the next open finds or makes.
        """
        parts = split_relpath(relpath)
        where = self._describe(parts)
        parent = self._parent_of(parts, create=True)
        try:
            fd = open_regular_at(
                parent, parts[-1], os.O_WRONLY | os.O_CREAT | os.O_APPEND, mode=mode, where=where
            )
            try:
                st = os.fstat(fd)
                if limit is not None and st.st_size > limit:
                    raise ReadLimitExceeded(relpath, limit)
                # The name may have been created by this open; its directory
                # entry is made durable the way a published temporary's is.
                _fsync_fd(parent)
            except OSError as exc:
                os.close(fd)
                raise StateError(f"cannot open {where}: {exc}") from exc
            except BaseException:
                os.close(fd)
                raise
        finally:
            os.close(parent)
        return AppendHandle(fd, relpath, st, limit)

    def append_to(self, handle: AppendHandle, text: str) -> None:
        """Append ``text`` through ``handle``, once the file is proved still what was opened.

        The bytes go through the held descriptor, so they can only ever land
        in the inode :meth:`open_append` inspected; that is the guarantee,
        and it holds whatever another process did to the name in between.
        What is proved immediately before the write, each in O(1) and none
        of it reading a byte, is that the file is still the one the caller
        means: the name resolves, under *this* root (walked descriptor by
        descriptor, so a root re-verified since the open is the one that
        answers), to the held inode by ``lstat`` -- a file renamed over the
        name, a symbolic link planted at it, or the name unlinked or moved
        away is refused and nothing is written to whatever is there; the
        inode still has exactly one name -- a hard link given to it since
        the open is refused, as it would have been at the open; and the
        inode is within the handle's limit. Then the line is written and
        fsynced.

        What remains is the window between that re-inspection and the
        ``write(2)``: a move of the inode in it puts the line in the
        caller's own file under its new name, an unlink loses the line with
        the inode, and the append reports success either way. A file the
        other process put at the name receives nothing in any window
        (module docstring, "Not guaranteed").
        """
        parts = split_relpath(handle.relpath)
        where = self._describe(parts)
        fd = handle.fd
        gone = UnsafePathError(
            f"refusing to append to {where}: no entry of that name is left, so the file that "
            "was opened and inspected there was unlinked or moved away since. Nothing was "
            "written to it."
        )
        try:
            parent = self._parent_of(parts, create=False)
        except FileNotFoundError:
            raise gone from None
        try:
            try:
                now = os.lstat(parts[-1], dir_fd=parent)
            except FileNotFoundError:
                raise gone from None
            except OSError as exc:
                raise StateError(f"cannot inspect {where}: {exc}") from exc
        finally:
            os.close(parent)
        if (now.st_dev, now.st_ino) != handle.identity:
            kind = entry_kind(now.st_mode) or "regular file"
            raise UnsafePathError(
                f"refusing to append to {where}: the entry at that name is a different {kind} "
                "now, not the file that was opened and inspected there, so it was replaced "
                "since. Neither file was written to; move the entry aside and re-run."
            )
        st = os.fstat(fd)
        if st.st_nlink > 1:
            raise _unsafe(where, _hard_link_text(st.st_nlink))
        if st.st_nlink < 1:  # pragma: no cover - the name resolved to it an instant ago
            raise gone
        if handle.limit is not None and st.st_size > handle.limit:
            raise ReadLimitExceeded(handle.relpath, handle.limit)
        try:
            view = memoryview(text.encode("utf-8"))
            while view:
                view = view[os.write(fd, view) :]
            os.fsync(fd)
        except OSError as exc:
            raise StateError(f"cannot append to {where}: {exc}") from exc

    def unlink(self, relpath: str) -> None:
        parts = split_relpath(relpath)
        try:
            parent = self._parent_of(parts, create=False)
        except FileNotFoundError:
            return
        try:
            os.unlink(parts[-1], dir_fd=parent)
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise StateError(f"cannot remove {self._describe(parts)}: {exc}") from exc
        finally:
            os.close(parent)

    def link(self, src: str, dst: str) -> None:
        """Hard-link ``src`` to ``dst`` within this root, never following a link.

        Used to *reserve* a destination name: ``link`` fails atomically with
        ``EEXIST`` when the name is taken, which a rename would not.
        ``follow_symlinks=False`` links the directory entry itself, so a
        symbolic link is archived as a link rather than resolved.
        """
        src_parts = split_relpath(src)
        dst_parts = split_relpath(dst)
        src_parent = self._parent_of(src_parts, create=False)
        try:
            dst_parent = self._parent_of(dst_parts, create=True)
            try:
                os.link(
                    src_parts[-1],
                    dst_parts[-1],
                    src_dir_fd=src_parent,
                    dst_dir_fd=dst_parent,
                    follow_symlinks=False,
                )
            except FileExistsError:
                raise
            except OSError as exc:
                raise StateError(
                    f"cannot link {self._describe(src_parts)} to {self._describe(dst_parts)}: {exc}"
                ) from exc
            finally:
                os.close(dst_parent)
        finally:
            os.close(src_parent)

    def rename(self, src: str, dst: str) -> None:
        """Rename within this root, both sides named relative to a descriptor."""
        src_parts = split_relpath(src)
        dst_parts = split_relpath(dst)
        src_parent = self._parent_of(src_parts, create=False)
        try:
            dst_parent = self._parent_of(dst_parts, create=True)
            try:
                os.rename(
                    src_parts[-1],
                    dst_parts[-1],
                    src_dir_fd=src_parent,
                    dst_dir_fd=dst_parent,
                )
                _fsync_fd(dst_parent)
            except OSError as exc:
                raise StateError(
                    f"cannot move {self._describe(src_parts)} to {self._describe(dst_parts)}: {exc}"
                ) from exc
            finally:
                os.close(dst_parent)
        finally:
            os.close(src_parent)

    def _unnamed_tmp_at(self, parent: int, data: bytes, *, mode: int, where: str) -> int | None:
        """Write ``data``, fsynced, into an inode of ``parent``'s filesystem
        that has no name (Linux ``O_TMPFILE``).

        Returns a descriptor the caller owns and publishes through
        :meth:`_link_unnamed_at`, or ``None`` where the mechanism is not
        available, in which case the caller falls back to
        :meth:`_durable_tmp_at`.  Unavailable means: the platform has no
        ``O_TMPFILE`` (macOS); the kernel refuses the open (a filesystem
        without it reports ``EOPNOTSUPP``, a kernel too old to know the flag
        sees the ``O_DIRECTORY`` bit it is built on and reports ``EISDIR``,
        an emulation layer may say ``EINVAL``); or the process's own
        ``/proc/self/fd`` entry does not name the inode just opened (no
        procfs, or something else mounted there).  *Every* failure of the
        open falls back rather than being classified, because the fallback
        opens with ``O_CREAT | O_EXCL`` in the same directory an instant
        later: a real refusal -- permission, space, a read-only mount --
        recurs there and is reported by the path that has always reported
        it, so nothing is hidden and the fallback costs one syscall.

        A write failure *after* the open is this write's failure and is
        raised; the inode has no name, so closing the descriptor is the whole
        cleanup.
        """
        if not _O_TMPFILE:
            return None
        try:
            fd = os.open(".", os.O_WRONLY | _O_TMPFILE | _O_CLOEXEC, mode, dir_fd=parent)
        except OSError:
            return None
        try:
            st = os.fstat(fd)
            try:
                seen = os.stat(_proc_fd_path(fd))
            except OSError:
                seen = None
            if (
                seen is None
                or (seen.st_dev, seen.st_ino) != (st.st_dev, st.st_ino)
                or entry_kind(st.st_mode) is not None
                or st.st_nlink != 0
            ):
                # The publish below needs the procfs entry to denote exactly
                # this inode; without that proof the named path is used.
                os.close(fd)
                return None
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view) :]
            os.fsync(fd)
        except OSError as exc:
            os.close(fd)
            raise StateError(f"cannot write {where}: {exc}") from exc
        except BaseException:
            os.close(fd)
            raise
        return fd

    def _link_unnamed_at(self, fd: int, parent: int, name: str, *, where: str) -> None:
        """Give the unnamed inode behind ``fd`` its first name, ``name`` under
        ``parent``, through the process's own ``/proc/self/fd`` entry.

        The inode is inspected immediately before: it must still have *no*
        name.  A same-user process that can open this process's
        ``/proc/<pid>/fd`` can name the inode the same way the controller is
        about to, so an inode found with any name is emptied through this
        descriptor and refused rather than published -- the same treatment a
        named temporary gets for a second name.  ``linkat`` never replaces,
        so ``EEXIST`` on any existing entry (a symbolic link included) is
        surfaced as :class:`FileExistsError` for the caller to interpret.
        """
        try:
            nlink = os.fstat(fd).st_nlink
        except OSError as exc:
            raise StateError(f"cannot write {where}: {exc}") from exc
        if nlink != 0:
            raise _empty_and_refuse(
                fd,
                where,
                f"being written through a temporary that acquired {nlink} directory "
                "entr(ies) before it was published; another process linked the "
                "controller's temporary file elsewhere",
            )
        try:
            os.link(_proc_fd_path(fd), name, dst_dir_fd=parent, follow_symlinks=True)
        except OSError as exc:
            if exc.errno == errno.EEXIST:
                raise FileExistsError(errno.EEXIST, f"{where} already exists") from exc
            raise StateError(f"cannot create {where}: {exc}") from exc

    def _name_unnamed_tmp_at(self, fd: int, parent: int, *, where: str) -> str:
        """Give the unnamed inode behind ``fd`` a temporary name so that a
        rename can publish it, and return that name.

        ``linkat`` cannot replace an existing entry, so a whole-file
        replacement needs a named step after all: link, then rename.  The
        name exists for exactly those two syscalls, with the bytes already
        complete, and the inode is re-inspected between them: it must have
        exactly the one name the controller just gave it.  One that gained
        another is emptied through this descriptor, unlinked and refused.

        Once the name exists, every way out of this method other than
        returning it unlinks it: the re-inspection failing, the emptying
        failing, and the refusal itself.  The caller never sees a name it
        does not receive, so a failure here leaves no ``.af-tmp-*`` behind.
        """
        tmp = _tmp_name()
        try:
            self._link_unnamed_at(fd, parent, tmp, where=where)
        except FileExistsError as exc:
            # The random name collided; a fact about the directory, not the
            # target, so it is a plain failure of this write.
            raise StateError(f"cannot write {where}: temporary name {tmp} is taken") from exc
        try:
            nlink = os.fstat(fd).st_nlink
            if nlink != 1:
                raise _empty_and_refuse(
                    fd,
                    where,
                    f"being written through a temporary that acquired {nlink - 1} extra "
                    "directory entr(ies) before it was published; another process linked "
                    "the controller's temporary file elsewhere",
                )
        except OSError as exc:
            _quiet_unlink(parent, tmp)
            raise StateError(f"cannot write {where}: {exc}") from exc
        except BaseException:
            _quiet_unlink(parent, tmp)
            raise
        return tmp

    def _durable_tmp_at(self, parent: int, data: bytes, *, mode: int, where: str) -> str:
        """Write ``data`` into a fresh, fsynced *named* temporary beside
        ``where`` -- the path taken where :meth:`_unnamed_tmp_at` is not
        available.

        Returns the temporary's name; the caller publishes it under the final
        name (or unlinks it).  The name carries the pid and a random token, so
        two controllers and two attempts by one controller never collide.

        The temporary is created ``O_EXCL`` -- it is this process's inode --
        but it is *named*, and a same-user process can hard-link that name
        elsewhere between the create and the write.  So the descriptor is
        inspected again once the bytes are durable and before the name is
        handed to the caller: a temporary that has gained a second name is
        emptied through the descriptor it was written through, unlinked, and
        refused (see the module docstring for exactly what that closes and
        what it cannot).
        """
        tmp = _tmp_name()
        fd = open_regular_at(
            parent, tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode=mode, where=where
        )
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
                nlink = os.fstat(fh.fileno()).st_nlink
                if nlink > 1:
                    # Our inode, emptied through our descriptor: the second
                    # name keeps an empty file, and nothing is published.
                    raise _empty_and_refuse(
                        fh.fileno(),
                        where,
                        f"being written through a temporary that acquired {nlink - 1} extra "
                        "directory entr(ies) while it was being written; another process "
                        "linked the controller's temporary file elsewhere",
                    )
        except OSError as exc:
            # The temporary is never published, so a failed write leaves
            # neither a partial final name nor a stray temporary behind.
            _quiet_unlink(parent, tmp)
            raise StateError(f"cannot write {where}: {exc}") from exc
        except BaseException:
            # A refusal, a failed emptying, or an interrupt: the name goes
            # the same way.
            _quiet_unlink(parent, tmp)
            raise
        return tmp

    def _replace_at(self, parent: int, name: str, data: bytes, *, mode: int, where: str) -> None:
        fd = self._unnamed_tmp_at(parent, data, mode=mode, where=where)
        if fd is None:
            tmp = self._durable_tmp_at(parent, data, mode=mode, where=where)
        else:
            try:
                tmp = self._name_unnamed_tmp_at(fd, parent, where=where)
            finally:
                os.close(fd)
        try:
            os.replace(tmp, name, src_dir_fd=parent, dst_dir_fd=parent)
        except OSError as exc:
            _quiet_unlink(parent, tmp)
            if exc.errno == errno.EISDIR:
                raise _unsafe(where, "a directory, not a regular file") from exc
            raise StateError(f"cannot write {where}: {exc}") from exc
        self._fsync_published(parent, where)

    def _publish_new_at(
        self, parent: int, name: str, data: bytes, *, mode: int, where: str
    ) -> None:
        fd = self._unnamed_tmp_at(parent, data, mode=mode, where=where)
        if fd is not None:
            # The inode's first name is the final one: no temporary name at
            # any point, so nothing to unlink and nothing to observe.
            try:
                self._link_unnamed_at(fd, parent, name, where=where)
            finally:
                os.close(fd)
            self._fsync_published(parent, where)
            return
        tmp = self._durable_tmp_at(parent, data, mode=mode, where=where)
        try:
            os.link(tmp, name, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
        except OSError as exc:
            _quiet_unlink(parent, tmp)
            if exc.errno == errno.EEXIST:
                raise FileExistsError(errno.EEXIST, f"{where} already exists") from exc
            raise StateError(f"cannot create {where}: {exc}") from exc
        _quiet_unlink(parent, tmp)
        self._fsync_published(parent, where)

    @staticmethod
    def _fsync_published(parent: int, where: str) -> None:
        """Make the directory entry just published at ``where`` durable.

        The last step of every write that publishes a name. A filesystem
        that cannot fsync a directory is tolerated (:func:`_fsync_fd`); a
        fatal failure is reported through the controller's error type, as
        every other failure of the write is, rather than as a raw
        ``OSError`` that the CLI's error boundary does not catch. The name
        is left in place: its bytes are whole and fsynced, and it is the
        entry's durability across a crash, not its contents, that could not
        be proved.
        """
        try:
            _fsync_fd(parent)
        except OSError as exc:
            raise StateError(f"cannot durably publish {where}: {exc}") from exc

    # -- walking ------------------------------------------------------------
    def walk(self, *, max_entries: int | None = None) -> Iterator[WalkEntry]:
        """Yield every entry beneath the root, parents before children.

        The walk descends only through descriptors it opened itself with
        ``O_NOFOLLOW``, so it can neither be redirected out of the root by a
        symbolic link nor be made to follow one.  Set ``entry.skip = True`` on
        a directory entry to stop the walk descending into it.

        ``max_entries`` bounds the *work*, not just the result: directory
        entries are counted as they are listed, before any of them is sorted,
        stat'ed or yielded, and the walk raises :class:`WalkBudgetExceeded`
        the moment the count passes the bound -- so one directory holding a
        million names costs a million ``readdir`` records and nothing more.
        The walk is iterative (an explicit stack, one descriptor per open
        level), so the tree's depth is bounded by the same budget rather than
        by the interpreter's recursion limit: a nest of ten thousand
        directories is ten thousand entries, not a ``RecursionError``.
        """
        listed = 0
        # (directory descriptor, relpath prefix, names still to visit)
        stack: list[tuple[int, str, list[str]]] = []
        names = self._list_dir(self.fd, "", listed=0, max_entries=max_entries)
        listed += len(names)
        stack.append((self.fd, "", names))
        try:
            while stack:
                dir_fd, prefix, names = stack[-1]
                if not names:
                    stack.pop()
                    if dir_fd != self.fd:
                        os.close(dir_fd)
                    continue
                name = names.pop()
                relpath = f"{prefix}/{name}" if prefix else name
                try:
                    st = os.lstat(name, dir_fd=dir_fd)
                except FileNotFoundError:
                    # Vanished between the listing and the stat.  A file that
                    # is not there when we look is not part of the snapshot;
                    # it cannot be, and pretending otherwise would be a lie
                    # about what was read.
                    continue
                except OSError as exc:
                    if _denied(exc):
                        raise UnreadableEntryError(
                            relpath, f"cannot inspect {self._path / relpath}: {_denied_text(exc)}"
                        ) from exc
                    raise StateError(f"cannot inspect {self._path / relpath}: {exc}") from exc
                entry = WalkEntry(relpath=relpath, name=name, dir_fd=dir_fd, st=st)
                yield entry
                if entry.skip or not entry.is_dir:
                    continue
                try:
                    child = os.open(name, _DIR_OPEN_FLAGS, dir_fd=dir_fd)
                except FileNotFoundError:
                    continue
                except OSError as exc:
                    if exc.errno in (errno.ELOOP, errno.EMLINK, errno.ENOTDIR):
                        # lstat said directory, the O_NOFOLLOW open disagrees:
                        # the entry was swapped underneath us.
                        raise _unsafe(
                            str(self._path / relpath), "not the directory it just was"
                        ) from exc
                    if _denied(exc):
                        raise UnreadableEntryError(
                            relpath, f"cannot open {self._path / relpath}: {_denied_text(exc)}"
                        ) from exc
                    raise StateError(f"cannot open {self._path / relpath}: {exc}") from exc
                try:
                    children = self._list_dir(
                        child, relpath, listed=listed, max_entries=max_entries
                    )
                except BaseException:
                    os.close(child)
                    raise
                listed += len(children)
                stack.append((child, relpath, children))
        finally:
            for dir_fd, _, _ in stack:
                if dir_fd != self.fd:
                    os.close(dir_fd)

    def _list_dir(
        self, dir_fd: int, prefix: str, *, listed: int, max_entries: int | None
    ) -> list[str]:
        """The names in ``dir_fd``, sorted for iteration in reverse (``pop``).

        ``listed`` is how many entries the walk has already read elsewhere;
        with a ``max_entries`` the listing raises as soon as the total would
        pass it, without materialising the rest of the directory.
        """
        names: list[str] = []
        try:
            with os.scandir(dir_fd) as it:
                for entry in it:
                    if max_entries is not None and listed + len(names) >= max_entries:
                        raise WalkBudgetExceeded(max_entries, prefix or ".")
                    names.append(entry.name)
        except OSError as exc:
            where = self._path / prefix if prefix else self._path
            if _denied(exc):
                raise UnreadableEntryError(
                    prefix or ".", f"cannot list {where}: {_denied_text(exc)}"
                ) from exc
            raise StateError(f"cannot list {where}: {exc}") from exc
        names.sort(reverse=True)
        return names


def _read_within(fh: BinaryIO, relpath: str, limit: int | None) -> bytes:
    """Read all of ``fh``, or with a ``limit`` at most one byte past it.

    A file the caller has decided is too large to hold is never
    materialised, whatever ``st_size`` claimed before it was opened: the
    read asks for ``limit + 1`` bytes and raises :class:`ReadLimitExceeded`
    when it gets them all.
    """
    if limit is None:
        return fh.read()
    data = fh.read(limit + 1)
    if len(data) > limit:
        raise ReadLimitExceeded(relpath, limit)
    return data


def _kind_at(dir_fd: int, name: str, fallback: str) -> str:
    """Name what ``name`` actually is, for a refusal message.

    Linux reports ``O_DIRECTORY | O_NOFOLLOW`` on a symbolic link as
    ``ENOTDIR``, which would otherwise be reported as the unhelpful "not a
    directory".  This is used only to word an error that has already been
    decided; it never influences whether the operation is refused.
    """
    try:
        st = os.lstat(name, dir_fd=dir_fd)
    except OSError:  # pragma: no cover - best effort wording
        return fallback
    kind = entry_kind(st.st_mode)
    if kind is None:
        return "a regular file, not a directory"
    if kind == "directory":  # pragma: no cover - then the open would have worked
        return fallback
    return f"a {kind}, not a directory"


def _supports_dir_fd() -> set[str]:
    return {fn.__name__ for fn in os.supports_dir_fd}


def _mkdir_tree(p: Path) -> None:
    """Create ``p`` and its parents by pathname (root creation only).

    Only used to bring the *root* into existence; everything beneath a root
    is created through :meth:`SafeRoot.ensure_dir`, which is
    descriptor-relative.
    """
    missing: list[Path] = []
    current = p
    while not current.exists():
        missing.append(current)
        parent = current.parent
        if parent == current:
            break
        current = parent
    try:
        os.makedirs(p, 0o700, exist_ok=True)
        for created in missing:
            _fsync_path(created)
            _fsync_path(created.parent)
    except OSError as exc:
        raise StateError(f"cannot create or durably publish controller root {p}: {exc}") from exc


def _fsync_path(path: Path) -> None:
    fd = os.open(path, _DIR_OPEN_FLAGS & ~_O_NOFOLLOW)
    try:
        _fsync_fd(fd)
    finally:
        os.close(fd)


def _fsync_fd(fd: int) -> None:
    try:
        os.fsync(fd)
    except OSError as exc:
        # Some filesystems reject fsync on directories. That is a weaker
        # durability guarantee, not a reason to fail a write. I/O, space and
        # permission failures are different: reporting success after one of
        # those would lose a checkpoint while the caller believes it durable.
        unsupported = {
            errno.EINVAL,
            errno.ENOSYS,
            getattr(errno, "ENOTSUP", errno.EINVAL),
            getattr(errno, "EOPNOTSUPP", errno.EINVAL),
        }
        if exc.errno in unsupported:
            return
        raise


def _tmp_name() -> str:
    """A temporary name that two controllers, and two attempts by one, never share."""
    return f".af-tmp-{os.getpid():x}-{secrets.token_hex(8)}"


def _proc_fd_path(fd: int) -> str:
    """The procfs name of this process's descriptor ``fd``: the only pathname
    that can denote an unnamed inode, and one only this process (and whatever
    may open its ``/proc/<pid>/fd``) can resolve."""
    return f"/proc/self/fd/{fd}"


def _quiet_unlink(dir_fd: int, name: str) -> None:
    try:
        os.unlink(name, dir_fd=dir_fd)
    except OSError:  # pragma: no cover - cleanup best effort
        pass
