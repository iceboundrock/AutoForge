"""Filesystem primitives that never follow a link and never block on a special file.

AutoForge writes its runtime artifacts — ``state.json``, the run logs —
into a directory that normally lives *inside* the operator's checkout, which
is also where the coding and review agents it launches do their work.  An
entry under that directory is therefore not automatically one the controller
created: ``logs`` can be a symbolic link pointing anywhere on the machine,
``events.jsonl`` can be a FIFO, a step artifact can be a device node.

Following such an entry writes controller artifacts outside the working tree
(a LOCAL run promises to touch nothing else), and opening a FIFO without a
writer blocks the controller forever instead of failing it.  So every
directory the controller creates is verified to be a real directory, and
every artifact is opened with ``O_NOFOLLOW`` (a symbolic link fails the open
outright), ``O_NONBLOCK`` (a FIFO cannot stall it) and an ``fstat`` check on
the resulting descriptor (a FIFO, socket or device fails loudly).

A *hard* link needs the same treatment and is caught by neither: it is a
second name for an ordinary regular file, indistinguishable from the first.
``logs/<run>/stdout.log`` hard-linked to a file elsewhere on the machine
passes ``O_NOFOLLOW`` and passes the ``fstat`` kind check, and the run-log
write then replaces that file's contents in place.  So a write open
additionally requires a link count of one, and -- because the kernel applies
``O_TRUNC`` during ``os.open``, before any of this can be checked --
truncation is never requested of ``open``: the descriptor is opened intact,
validated, and only then truncated with ``ftruncate``.

These are the same rules :func:`autoforge.state.load_state` already applies
to the state file; they live here so the state file, the lock file and the
run logs share one implementation instead of three.

This is fail-closed hygiene for a single-operator tool, not a defence
against an attacker racing the controller: the entry is inspected and then
opened, and only the open is atomic.  What it does guarantee is that a
controller write never *lands* on a link or a special file.
"""

from __future__ import annotations

import errno
import os
import stat
from pathlib import Path

from .errors import StateError

# Absent on Windows; 0 makes the flag a no-op there rather than an AttributeError.
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_O_NOCTTY = getattr(os, "O_NOCTTY", 0)
# POSIX; 3 is its value everywhere CPython defines the open flags.
_O_ACCMODE = getattr(os, "O_ACCMODE", 3)


def entry_kind(mode: int) -> str | None:
    """Human name of a non-regular entry kind, or None for a regular file."""
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


def _unsafe(path: Path, what: str) -> StateError:
    return StateError(
        f"refusing to use runtime path {path}: it is {what}. AutoForge only writes its own "
        "regular files and directories there; an entry it did not create could redirect "
        "controller writes outside the working tree or block the controller indefinitely. "
        "Move the entry aside and re-run."
    )


def ensure_directory(path: str | Path) -> Path:
    """Create ``path`` (with parents) and prove it is a real directory.

    A symbolic link is refused even when it points at a directory: the whole
    point is that the controller's writes land where it thinks they do.
    ``mkdir`` is attempted before the entry is inspected so the common case
    costs one syscall, and an existing entry is then checked with ``lstat``
    (never ``stat``, which would follow the link being looked for).
    """
    p = Path(path)
    try:
        os.mkdir(p, 0o700)
    except FileExistsError:
        pass
    except FileNotFoundError:
        if p.parent == p:  # pragma: no cover - reached only for a root that cannot exist
            raise
        ensure_directory(p.parent)
        try:
            os.mkdir(p, 0o700)
        except FileExistsError:
            pass
        except OSError as exc:
            raise StateError(f"cannot create runtime directory {p}: {exc}") from exc
    except OSError as exc:
        raise StateError(f"cannot create runtime directory {p}: {exc}") from exc
    try:
        st = os.lstat(p)
    except OSError as exc:  # pragma: no cover - the mkdir above just succeeded
        raise StateError(f"cannot inspect runtime directory {p}: {exc}") from exc
    if stat.S_ISLNK(st.st_mode):
        raise _unsafe(p, "a symbolic link, not a directory AutoForge created")
    if not stat.S_ISDIR(st.st_mode):
        raise _unsafe(p, f"a {entry_kind(st.st_mode)}, not a directory")
    return p


def open_regular(path: str | Path, flags: int, *, mode: int = 0o600) -> int:
    """``os.open`` restricted to a regular file with one name: nothing is followed.

    Returns an open descriptor the caller owns.  ``O_NOFOLLOW`` fails the
    open (``ELOOP``) when the final component is a symbolic link, so the
    check cannot be raced; ``O_NONBLOCK`` keeps a FIFO or a device from
    stalling the call, and the ``fstat`` that follows rejects it.

    ``O_TRUNC`` is honoured but never handed to ``os.open``: the kernel
    applies it while opening, which is *before* the descriptor can be
    inspected, so a run-log write would already have emptied whatever it
    landed on by the time the check that rejects it ran.  The file is opened
    intact, validated, and then truncated with ``ftruncate`` on the very
    descriptor that was validated.

    A write open also requires ``st_nlink == 1``.  A hard link is a regular
    file by every test above, so without this an extra name for some other
    file of the same user -- planted under the log tree -- would have its
    contents replaced by a run-log write instead of the controller writing
    its own artifact.  A read is left alone: reading through a second name
    changes nothing.
    """
    p = Path(path)
    truncate = bool(flags & os.O_TRUNC)
    writing = (flags & _O_ACCMODE) in (os.O_WRONLY, os.O_RDWR)
    try:
        fd = os.open(p, (flags & ~os.O_TRUNC) | _O_NOFOLLOW | _O_NONBLOCK | _O_NOCTTY, mode)
    except FileNotFoundError:
        # Absence is the caller's business (see read_text); it is not unsafe.
        raise
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.EMLINK):
            raise _unsafe(p, "a symbolic link") from exc
        if exc.errno == errno.ENXIO:
            # O_WRONLY|O_NONBLOCK on a FIFO with no reader.
            raise _unsafe(p, "a FIFO") from exc
        if exc.errno == errno.EISDIR:
            # The kernel refuses a writable open before fstat can report it.
            raise _unsafe(p, "a directory, not a regular file") from exc
        raise StateError(f"cannot open runtime file {p}: {exc}") from exc
    try:
        st = os.fstat(fd)
        kind = entry_kind(st.st_mode)
        if kind is not None:
            raise _unsafe(p, f"a {kind}, not a regular file")
        # st_nlink is 0 where a platform does not report link counts; only a
        # count that positively proves a second name is a refusal.
        if writing and st.st_nlink > 1:
            raise _unsafe(
                p,
                f"a hard link: {st.st_nlink} directory entries name this file, so writing "
                "here would replace the contents of a file AutoForge did not create",
            )
        if truncate:
            os.ftruncate(fd, 0)
    except BaseException:
        os.close(fd)
        raise
    return fd


def write_text(path: str | Path, text: str) -> None:
    """Replace ``path``'s contents, creating it if needed. Never follows a link."""
    fd = open_regular(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)


def append_text(path: str | Path, text: str) -> None:
    """Append to ``path``, creating it if needed. Never follows a link."""
    fd = open_regular(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND)
    with os.fdopen(fd, "a", encoding="utf-8") as fh:
        fh.write(text)


def read_text(path: str | Path) -> str | None:
    """Read ``path``; None when it does not exist. Never follows a link."""
    try:
        fd = open_regular(path, os.O_RDONLY)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, encoding="utf-8", errors="replace") as fh:
        return fh.read()
