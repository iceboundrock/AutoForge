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
* a write open requires ``st_nlink == 1``, because a hard link is a second
  name for an ordinary file and passes every other test;
* ``O_TRUNC`` is never handed to ``os.open`` -- the kernel applies it during
  the open, which is before the descriptor can be inspected -- so a file is
  opened intact, validated, and only then truncated through ``ftruncate`` on
  the very descriptor that was validated.

Whole-file writes do not truncate at all: they create a fresh unlinked-name
temporary with ``O_CREAT | O_EXCL`` in the target's own directory, fsync it,
and ``os.replace`` it over the target with both sides named by descriptor.
That replaces a *name*, never an inode, so a hard link planted at the target
survives untouched, and a reader either sees the whole old file or the whole
new one.

What this does and does not guarantee
-------------------------------------

Guaranteed: a controller write always lands inside the inode that was opened
as the root, on a regular file with exactly one name, or it fails.  No
sequence of symlink, hard link, FIFO, device, directory-replacement or rename
operations performed by another process under the same user can move a
controller write outside that inode or onto an object the controller did not
create.  This is a property of descriptor-relative addressing, not of a list
of rejected shapes, so a shape nobody has thought of yet is covered too.

Not guaranteed: the *initial* resolution of the root path itself.  ``SafeRoot
.open`` resolves an ordinary pathname, and a process that can redirect that
pathname in the instant before the open can point the controller at a
different directory.  That is equivalent to being able to redirect the
repository checkout itself and no pathname-based defence can close it; it is
stated here rather than papered over.  Everything *below* the root is closed.
"""

from __future__ import annotations

import errno
import os
import secrets
import stat
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from .errors import StateError

# Absent on Windows; 0 makes the flag a no-op rather than an AttributeError.
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_O_NOCTTY = getattr(os, "O_NOCTTY", 0)
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_O_ACCMODE = getattr(os, "O_ACCMODE", 3)

_DIR_OPEN_FLAGS = os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC

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
    """A bounded :meth:`SafeRoot.read_bytes` met a file larger than its limit."""

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


def _unsafe(where: str, what: str) -> UnsafePathError:
    return UnsafePathError(
        f"refusing to use {where}: it is {what}. AutoForge only reads and writes its own "
        "regular files and directories there; an entry it did not create could redirect a "
        "controller write outside its root or block the controller indefinitely. "
        "Move the entry aside and re-run."
    )


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
        # st_nlink is 0 where a platform does not report link counts; only a
        # count that positively proves a second name is a refusal.
        if writing and st.st_nlink > 1:
            raise _unsafe(
                label,
                f"a hard link: {st.st_nlink} directory entries name this file, so writing "
                "here would replace the contents of a file AutoForge did not create",
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
            if limit is None:
                return fh.read()
            data = fh.read(limit + 1)
        if len(data) > limit:
            raise ReadLimitExceeded(relpath, limit)
        return data

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

    def append_text(self, relpath: str, text: str, *, mode: int = 0o600) -> None:
        """Append to ``relpath`` by replacing its name, creating it if needed.

        Appending through an opened inode would leave a hard-link race between
        the link-count check and the write. Reading the old bytes and publishing
        a fresh inode keeps a second name, planted at any point in the window,
        untouched.
        """
        parts = split_relpath(relpath)
        parent = self._parent_of(parts, create=True)
        try:
            existing = b""
            existing_mode = mode
            try:
                fd = open_regular_at(parent, parts[-1], os.O_RDONLY, where=self._describe(parts))
            except FileNotFoundError:
                pass
            else:
                try:
                    existing_mode = stat.S_IMODE(os.fstat(fd).st_mode)
                except BaseException:
                    os.close(fd)
                    raise
                with os.fdopen(fd, "rb") as fh:
                    existing = fh.read()
            self._replace_at(
                parent,
                parts[-1],
                existing + text.encode("utf-8"),
                mode=existing_mode,
                where=self._describe(parts),
            )
        finally:
            os.close(parent)

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

    def _durable_tmp_at(self, parent: int, data: bytes, *, mode: int, where: str) -> str:
        """Write ``data`` into a fresh, fsynced temporary beside ``where``.

        Returns the temporary's name; the caller publishes it under the final
        name (or unlinks it).  The name carries the pid and a random token, so
        two controllers and two attempts by one controller never collide.
        """
        tmp = f".af-tmp-{os.getpid():x}-{secrets.token_hex(8)}"
        fd = open_regular_at(
            parent, tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode=mode, where=where
        )
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
        except BaseException:
            _quiet_unlink(parent, tmp)
            raise
        return tmp

    def _replace_at(self, parent: int, name: str, data: bytes, *, mode: int, where: str) -> None:
        tmp = self._durable_tmp_at(parent, data, mode=mode, where=where)
        try:
            os.replace(tmp, name, src_dir_fd=parent, dst_dir_fd=parent)
        except OSError as exc:
            _quiet_unlink(parent, tmp)
            if exc.errno == errno.EISDIR:
                raise _unsafe(where, "a directory, not a regular file") from exc
            raise StateError(f"cannot write {where}: {exc}") from exc
        _fsync_fd(parent)

    def _publish_new_at(
        self, parent: int, name: str, data: bytes, *, mode: int, where: str
    ) -> None:
        tmp = self._durable_tmp_at(parent, data, mode=mode, where=where)
        try:
            os.link(tmp, name, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
        except OSError as exc:
            _quiet_unlink(parent, tmp)
            if exc.errno == errno.EEXIST:
                raise FileExistsError(errno.EEXIST, f"{where} already exists") from exc
            raise StateError(f"cannot create {where}: {exc}") from exc
        _quiet_unlink(parent, tmp)
        _fsync_fd(parent)

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


def _quiet_unlink(dir_fd: int, name: str) -> None:
    try:
        os.unlink(name, dir_fd=dir_fd)
    except OSError:  # pragma: no cover - cleanup best effort
        pass
