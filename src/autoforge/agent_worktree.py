"""The agent worktree's git surface, as the controller reads it (ADR 0005 D6.3-D6.5).

Mode-independent: the redirect these close needs no sandbox, only a
writable ``.git`` file, which every agent has. The engine calls these for a
REMOTE run's issue worktree, whatever ``sandbox.mode`` says (#168).

- **Binding (D6.4).** :func:`bound_admin_dir` finds the worktree's admin
  dir through the repository's registry: exactly one
  ``<common>/worktrees/<id>/gitdir`` names ``<worktree>/.git``, and the
  worktree's ``.git`` is a regular file naming that admin dir, absolute or
  relative (``worktree.useRelativePaths``). It replaces asking ``git -C
  <worktree>`` where its repository is, which follows the agent-writable
  ``.git`` file. :func:`registered_worktree_roots` is the registry's list of
  working-tree roots, what ``git worktree list`` reports, read from the same
  entries without git and without following a link: ``git worktree list``
  resolves every listed worktree's ``HEAD``, through a link the agent
  planted in its admin dir (#223). Both read the registry through a
  descriptor opened ``O_NOFOLLOW`` (:func:`_registry_entries`): a registry
  that is itself a link is refused, never listed.
- **Candidate (D6.3).** :func:`read_candidate` reads the worktree's
  ``HEAD`` as ``worktrees/<id>/HEAD`` with ``--git-dir=<git common dir>``,
  after ``lstat`` found a regular file there, reached from the common dir
  through real directories (:func:`head_problem`): never
  through the worktree's ``.git`` file, the admin dir's ``commondir`` or
  ``gitdir``, or ``git -C <worktree>``. An attached ``HEAD`` is told by
  ``symbolic-ref --no-recurse``, which never opens the reference it names
  (#223).
- **Strict import (D6.5).** :func:`import_private_objects` copies the
  objects reachable from that ``HEAD`` which the shared store lacks out of
  the issue's private object directory (an enforced launch's overlay upper
  layer), through ``index-pack --strict``, which writes each object under
  the id it computes from its bytes. It reads the commit ``HEAD`` holds
  itself, after the candidate read's ``lstat``: a link there fails the
  import before anything is read through it, and an attached ``HEAD``
  imports nothing. :func:`private_objects_problem` walks the directory
  first without following a link: a root that is not a real directory
  reached from the common dir through real directories, an alternates
  file, or an entry that is neither a directory, a regular file nor an
  overlay whiteout, is tampering
  (:class:`~autoforge.errors.SandboxTamperError`). Its ``index-pack`` is
  the one step that writes the shared store, so it alone runs under the
  shared-Git lock (L1, ADR 0008 D12.1) and with automatic maintenance off
  (D12.3).

Every git process is the controller's own hardened local request
(:func:`~autoforge.git_transport.local_git_request`, ADR 0004 D7.3, D7.5);
the private directory is named through ``GIT_ALTERNATE_OBJECT_DIRECTORIES``
for the processes that read it, as one double-quoted entry
(:func:`alternate_entry`) so that a ``:`` in its path does not split it,
and never added to the shared store's alternates.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import tempfile
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path

from .errors import GitLockTimeout, LockError, SandboxTamperError, VerificationError
from .executor import ExecutionRequest, ExecutionResult, execute
from .git_transport import NO_AUTO_MAINTENANCE, local_git_request
from .locking import SharedGitLock
from .redaction import redact

Runner = Callable[[ExecutionRequest], ExecutionResult]

GIT_TIMEOUT_SECONDS = 120
# An import moves at most one issue's new objects; a large one is a pack.
IMPORT_TIMEOUT_SECONDS = 600
# A pointer file holds one path.
_MAX_POINTER_BYTES = 4096
# A registry directory: a link there fails the open instead of being followed.
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
# The registry entries and private-directory entries a walk visits before
# it fails closed: a store bigger than this is not one issue's work.
MAX_REGISTRY_ENTRIES = 10_000
MAX_PRIVATE_ENTRIES = 1_000_000
_SHA_RE = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
# The files that make git read objects from somewhere else (D6.5).
ALTERNATES_FILES = ("info/alternates", "info/http-alternates")


def _run(runner: Runner | None, request: ExecutionRequest) -> ExecutionResult:
    return (runner or execute)(request)


def _detail(res: ExecutionResult) -> str:
    lines = (res.stderr or res.stdout or "").strip().splitlines()
    return f": {redact(lines[-1])[:200]}" if lines else ""


def _ended(res: ExecutionResult) -> str:
    return "timed out" if res.timed_out else f"exited {res.exit_code}"


def _read_pointer(path: Path | str, dir_fd: int | None = None) -> str | None:
    """A pointer file's single line, or ``None`` unless it is a regular file.

    Opened without following a link and without blocking, and checked again
    on the descriptor: a file swapped for a FIFO after the ``lstat`` neither
    blocks the read nor is read. ``dir_fd``: ``path`` is a name in that
    directory, reached through no path at all.
    """
    try:
        if not stat.S_ISREG(os.lstat(path, dir_fd=dir_fd).st_mode):
            return None
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
    except OSError:
        return None
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            return None
        data = handle.read(_MAX_POINTER_BYTES + 1)
    if len(data) > _MAX_POINTER_BYTES or b"\0" in data:
        return None
    text = data.decode("utf-8", "replace").strip()
    return text if text and "\n" not in text else None


def _names(value: str, base: Path, expected: Path) -> bool:
    """Whether the pointer ``value`` (absolute, or relative to ``base``) names ``expected``."""
    named = os.path.normpath(value if os.path.isabs(value) else os.path.join(base, value))
    return named in (str(expected), os.path.realpath(expected))


@dataclass(frozen=True)
class _RegistryEntry:
    """A registry entry ``<common>/worktrees/<id>`` with a regular ``gitdir`` file."""

    admin: Path
    gitdir: str
    locked: bool


def _registry_entries(
    registry: Path, refuse: Callable[[str], VerificationError]
) -> list[_RegistryEntry] | None:
    """The registry's entries, read without following a link (D6.4, #223).

    ``None`` when ``registry`` is absent. The registry itself is ``lstat``-ed
    and opened ``O_NOFOLLOW``, and each entry and its files are reached from
    that descriptor, never by path: a registry that is a link (an agent
    without an enforced sandbox can replace it) is refused before the
    directory it names is opened, and so is one that is not a directory,
    cannot be read, or holds more than :data:`MAX_REGISTRY_ENTRIES`
    entries. An entry that is not a real directory, or whose ``gitdir`` is
    not a regular file (:func:`_read_pointer`), is left out.
    """
    try:
        mode = os.lstat(registry).st_mode
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise refuse(f"{registry} cannot be checked ({exc.strerror})") from None
    if stat.S_ISLNK(mode):
        raise refuse(f"{registry} is a link: the controller lists no registry through one")
    if not stat.S_ISDIR(mode):
        raise refuse(f"{registry} is not a directory")
    found: list[_RegistryEntry] = []
    try:
        # A link swapped in since the lstat fails the open.
        fd = os.open(registry, _DIR_FLAGS)
    except OSError as exc:
        raise refuse(f"{registry} cannot be read ({exc.strerror})") from None
    try:
        with os.scandir(fd) as entries:
            for count, entry in enumerate(entries):
                if count >= MAX_REGISTRY_ENTRIES:
                    raise refuse(f"{registry} holds more than {MAX_REGISTRY_ENTRIES} entries")
                if not entry.is_dir(follow_symlinks=False):
                    continue
                try:
                    admin_fd = os.open(entry.name, _DIR_FLAGS, dir_fd=fd)
                except OSError:
                    continue
                try:
                    value = _read_pointer("gitdir", admin_fd)
                    if value is None:
                        continue
                    try:
                        os.lstat("locked", dir_fd=admin_fd)
                        locked = True
                    except OSError:
                        locked = False
                finally:
                    os.close(admin_fd)
                found.append(_RegistryEntry(registry / entry.name, value, locked))
    except OSError as exc:
        raise refuse(f"{registry} cannot be read ({exc.strerror})") from None
    finally:
        os.close(fd)
    return found


def bound_admin_dir(common: Path, worktree: Path) -> Path:
    """The admin dir ``<common>/worktrees/<id>`` the registry binds ``worktree`` to (D6.4).

    ``worktree`` is the literal path the controller derived (its link and
    location checks are the caller's). Refused, with a
    :class:`VerificationError` naming the inconsistency and ``git worktree
    repair``, unless exactly one registry entry's ``gitdir`` names
    ``<worktree>/.git`` and the worktree's ``.git`` is a regular file
    naming that entry. The registry is read without following a link
    (:func:`_registry_entries`).
    """
    dotgit = worktree / ".git"
    registry = common / "worktrees"

    def refuse(what: str) -> VerificationError:
        return VerificationError(
            f"the agent worktree {worktree} is not bound to the repository at {common}: "
            f"{what}. The controller reads a worktree only through the registry entry that "
            "names it; if the worktree or the repository was moved, run `git worktree repair` "
            f"from the repository (or `git worktree repair {worktree}`), then 'resume'"
        )

    entries = _registry_entries(registry, refuse)
    if entries is None:
        raise refuse(f"the repository registers no worktree ({registry} is absent)")
    matches = [e.admin for e in entries if _names(e.gitdir, e.admin, dotgit)]
    if not matches:
        raise refuse(f"no entry of {registry} has a gitdir naming {dotgit}")
    if len(matches) > 1:
        names = ", ".join(sorted(m.name for m in matches))
        raise refuse(f"more than one entry of {registry} ({names}) has a gitdir naming {dotgit}")
    admin = matches[0]
    value = _read_pointer(dotgit)
    if value is None or not value.startswith("gitdir: "):
        raise refuse(f"{dotgit} is not a regular file of the form 'gitdir: <path>'")
    if not _names(value[len("gitdir: ") :].strip(), worktree, admin):
        raise refuse(f"{dotgit} does not name the registry entry {admin}")
    return admin


def _less_dotgit(path: str) -> str:
    """``path`` less a trailing ``/.git``: how git names a worktree after its ``.git``."""
    return path[: -len("/.git")] if path.endswith("/.git") else path


def registered_worktree_roots(common: Path) -> set[Path]:
    """The working-tree roots the repository's registry names, resolved (D6.4, #223).

    What ``git worktree list`` reports, read without git: git also resolves
    every listed worktree's ``HEAD``, following a link an agent planted
    there to any file on the host (a FIFO stalls it). Nothing here reads a
    ``HEAD`` or a reference.

    - The main worktree, named as git names it: the common dir, less a
      trailing ``/.git``.
    - Each entry ``<common>/worktrees/<id>`` that is a real directory with
      a regular ``gitdir`` file (:func:`_read_pointer`), at the path that
      file names less a trailing ``/.git``, absolute or relative to the
      entry.
    - Left out, as git marks it ``prunable``: an entry whose ``gitdir``
      names a ``.git`` that is not there, unless the entry is ``locked``.
      Both are ``lstat``-ed, so a link there counts as there and is not
      followed.

    An entry that is a link, or whose ``gitdir`` is not a regular file,
    names no root, since git would read through it; the binding
    (:func:`bound_admin_dir`) refuses such a worktree too. A registry that
    is a link, is not a directory, cannot be listed, or holds more than
    :data:`MAX_REGISTRY_ENTRIES` entries, is a :class:`VerificationError`
    (:func:`_registry_entries`).
    """
    roots = {Path(os.path.realpath(_less_dotgit(os.path.realpath(common)) or "/"))}
    registry = common / "worktrees"

    def refuse(what: str) -> VerificationError:
        return VerificationError(
            f"cannot list the repository's worktrees: {what}. The controller neither reuses an "
            "agent worktree nor launches in a sandbox it cannot check against the registry"
        )

    for entry in _registry_entries(registry, refuse) or ():  # None: no linked worktree
        dotgit = os.path.join(entry.admin, entry.gitdir)  # an absolute value stands alone
        if not (os.path.lexists(dotgit) or entry.locked):
            continue  # prunable: nothing at its path is a worktree
        roots.add(Path(os.path.realpath(os.path.join(entry.admin, _less_dotgit(entry.gitdir)))))
    return roots


@dataclass(frozen=True)
class Candidate:
    """The worktree's ``HEAD`` as the controller reads it (D6.3).

    ``attached``: ``HEAD`` is a symbolic reference. ``head``: the commit it
    names, "" when it is attached or names none the shared store holds.
    ``problem``: why there is no ``HEAD`` to read at all (not a regular
    file), else "".
    """

    attached: bool
    head: str
    problem: str = ""


def _admin_git(
    common: Path, args: list[str], runner: Runner | None, what: str, worktree: str
) -> tuple[int, str]:
    res = _run(
        runner,
        local_git_request(["--git-dir", str(common), *args], timeout_seconds=GIT_TIMEOUT_SECONDS),
    )
    if res.timed_out or res.truncated or res.exit_code not in (0, 1):
        raise VerificationError(
            f"cannot read {what} of the agent worktree {worktree} (`git {' '.join(args)}` "
            f"{_ended(res)}{_detail(res)}); nothing was pushed. 'resume' reads it again"
        )
    return res.exit_code, res.stdout.strip()


def head_problem(common: Path, admin: Path) -> str | None:
    """Why git may not read the admin dir's ``HEAD``, or ``None`` (D6.3).

    ``lstat``, no link followed: anything but a regular file is named,
    since git follows a link there (to a file the agent wrote outside the
    store, invisible inside) and reads the commit it names. So is a link or
    another file type at the admin dir or the registry above it
    (:func:`_root_problem`), which git would read ``worktrees/<id>/HEAD``
    through (#223). Checked before ``HEAD`` is read at all, by the
    candidate read and by the import.
    """
    problem = _root_problem(common, admin)
    if problem:
        return problem
    head_file = admin / "HEAD"
    try:
        mode = os.lstat(head_file).st_mode
    except OSError as exc:
        return f"{head_file} cannot be read ({exc.strerror})"
    if not stat.S_ISREG(mode):
        return f"{head_file} is not a regular file"
    return None


def read_candidate(common: Path, admin: Path, worktree: str, runner: Runner | None) -> Candidate:
    """The candidate: ``worktrees/<id>/HEAD`` addressed through the common dir (D6.3).

    Whether ``HEAD`` is attached is asked with ``symbolic-ref --no-recurse``,
    which reads ``HEAD`` alone: without it git resolves the reference
    ``HEAD`` names, and so a link the agent planted at
    ``worktrees/<id>/refs/...`` to any file on the host (#223). An
    inconclusive git read is a :class:`VerificationError`, never a guess.
    """
    problem = head_problem(common, admin)
    if problem:
        return Candidate(False, "", problem)
    ref = f"worktrees/{admin.name}/HEAD"
    attached, _ = _admin_git(
        common,
        ["symbolic-ref", "-q", "--no-recurse", ref],
        runner,
        "whether HEAD is detached",
        worktree,
    )
    if attached == 0:
        # Never a candidate: the reference it names (one the agent may have
        # written in its admin dir) is neither opened nor resolved.
        return Candidate(True, "")
    found, head = _admin_git(
        common,
        ["rev-parse", "--verify", "-q", f"{ref}^{{commit}}"],
        runner,
        "the HEAD commit",
        worktree,
    )
    if found != 0 or not _SHA_RE.fullmatch(head):
        head = ""
    return Candidate(False, head)


def _root_problem(common: Path, directory: Path) -> str | None:
    """Why ``directory`` is not a real directory below ``common``, or ``None``.

    Every component from ``common`` down to ``directory`` is ``lstat``-ed:
    one that is a link, not a directory, or cannot be checked is named, so
    nothing is read through it.
    """
    try:
        parts = directory.relative_to(common).parts
    except ValueError:
        parts = ()
    if not parts or ".." in parts:
        return f"{directory} is not a directory below the git common dir {common}"
    path = common
    for part in parts:
        path = path / part
        try:
            mode = os.lstat(path).st_mode
        except OSError as exc:
            return f"{path} cannot be checked ({exc.strerror})"
        if stat.S_ISLNK(mode):
            return f"{path} is a link"
        if not stat.S_ISDIR(mode):
            return f"{path} is not a directory"
    return None


def private_objects_problem(common: Path, private: Path) -> str | None:
    """Why the private object directory may not be read, or ``None`` (D6.5).

    Walked without following a link. The directory itself, and each one
    between the git common dir ``common`` and it, must be a real directory:
    a link, another file type or one that cannot be checked is named, never
    taken for an empty store. Inside it, an alternates file, a link, a
    socket, a FIFO, a block device or a character device other than an
    overlay whiteout (0:0, skipped) is named; nothing is opened.
    """
    problem = _root_problem(common, private)
    if problem:
        return problem
    for name in ALTERNATES_FILES:
        try:
            os.lstat(private / name)
        except FileNotFoundError:
            continue
        except OSError as exc:
            return f"{private / name} cannot be checked ({exc.strerror})"
        return f"{private / name} exists: it would make git read another object store"
    stack = [private]
    seen = 0
    while stack:
        directory = stack.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    seen += 1
                    if seen > MAX_PRIVATE_ENTRIES:
                        return f"{private} holds more than {MAX_PRIVATE_ENTRIES} entries"
                    st = entry.stat(follow_symlinks=False)
                    if stat.S_ISDIR(st.st_mode):
                        stack.append(Path(entry.path))
                    elif stat.S_ISREG(st.st_mode):
                        continue
                    elif stat.S_ISCHR(st.st_mode) and st.st_rdev == 0:
                        continue  # an overlay whiteout: a shared object hidden inside
                    else:
                        return f"{entry.path} is neither a directory, a regular file nor a whiteout"
        except FileNotFoundError:
            continue
        except OSError as exc:
            return f"{directory} cannot be walked ({exc.strerror})"
    return None


def holds_objects(private: Path) -> bool:
    """Whether the private object directory has anything to import or refuse.

    ``False`` only when it is absent, or a real directory (``lstat``, no
    link followed) without an entry. A link, another file type, or a
    directory that cannot be checked or listed is ``True``, so that the
    import's walk (:func:`private_objects_problem`) refuses it rather than
    taking it for an empty store.
    """
    try:
        if not stat.S_ISDIR(os.lstat(private).st_mode):
            return True
        with os.scandir(private) as entries:
            return any(True for _ in entries)
    except FileNotFoundError:
        return False
    except OSError:
        return True


def alternate_entry(directory: Path) -> str:
    """``directory`` as the one entry of ``GIT_ALTERNATE_OBJECT_DIRECTORIES`` (#224).

    Git splits that variable on ``:`` unless an entry starts with ``"``,
    which it then unquotes C-style. So the entry is always double-quoted,
    with ``\\`` and ``"`` escaped; every other byte, ``:`` included, passes
    verbatim. Unquoted, a path with a ``:`` names two directories that do
    not exist, git reads no object through either, and the import would
    take the agent's commit for none.
    """
    escaped = str(directory).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


class ObjectImportError(VerificationError):
    """The strict import did not complete; nothing was published."""


def import_private_objects(
    common: Path,
    admin: Path,
    private: Path,
    runner: Runner | None,
    *,
    exclude: tuple[str, ...] = (),
    git_lock: Callable[[str], AbstractContextManager[object]] | None = None,
) -> str:
    """Import the objects ``HEAD`` reaches that the shared store lacks (D6.5).

    Returns the ``HEAD`` commit the import covered ("" when the directory
    holds nothing, or ``HEAD`` names no commit even with it). The walk of
    :func:`private_objects_problem` comes first: tampering is a
    :class:`SandboxTamperError`. A ``HEAD`` that is not a regular file
    (:func:`head_problem`, the check :func:`read_candidate` makes) is an
    :class:`ObjectImportError` before git reads anything: nothing is packed
    or imported, and the candidate read refuses the candidate with it. The
    commit is the object id ``HEAD`` holds, read without following a link;
    an attached ``HEAD`` imports nothing (it is never a candidate). Then
    ``rev-list --objects <head> --not --branches --tags --remotes
    <exclude>`` with the private directory as an alternate, the objects the
    shared store does not hold, ``pack-objects`` of exactly those, and
    ``index-pack --stdin --fix-thin --strict`` into the shared store.
    Idempotent; a failure is :class:`ObjectImportError` and is not retried
    here. ``exclude`` are commits the shared store holds (the phase's
    recorded base), so the walk stops there.

    Only ``index-pack`` writes the shared store: it runs under the
    shared-Git lock (L1, ADR 0008 D12.1) that ``git_lock(operation)``
    returns, by default :class:`~autoforge.locking.SharedGitLock` on
    ``common``, and with automatic maintenance off (D12.3). The walk,
    ``rev-list`` and ``pack-objects`` read the store and write only a
    private scratch directory. A lock not obtained within its bound is this
    import failing (:class:`ObjectImportError`, D11.3).
    """
    if not holds_objects(private):
        return ""
    problem = private_objects_problem(common, private)
    if problem:
        raise SandboxTamperError(
            f"the private object directory {private} of the agent worktree has been "
            f"tampered with: {problem}. Nothing was read from it and nothing was published; "
            "inspect it, and remove what does not belong there before unblocking the run"
        )
    problem = head_problem(common, admin)
    if problem:
        raise ObjectImportError(
            f"the strict import of the agent's commits from {private} did not start: "
            f"{problem}, and git would follow it; nothing was read or published"
        )
    alternate = {"GIT_ALTERNATE_OBJECT_DIRECTORIES": alternate_entry(private)}
    git_dir = ["--git-dir", str(common)]

    def git(
        args: list[str],
        what: str,
        *,
        env: dict[str, str] | None = None,
        stdin: bytes | None = None,
        allow: tuple[int, ...] = (0,),
    ) -> ExecutionResult:
        request = local_git_request(
            [*git_dir, *args], env=env, timeout_seconds=IMPORT_TIMEOUT_SECONDS
        )
        request.stdin_data = stdin
        res = _run(runner, request)
        if res.timed_out or res.truncated or res.exit_code not in allow:
            raise ObjectImportError(
                f"the strict import of the agent's commits from {private} failed: {what} "
                f"{_ended(res)}{_detail(res)}; nothing was published"
            )
        return res

    detached = _read_pointer(admin / "HEAD")
    if detached is None or not _SHA_RE.fullmatch(detached):
        # Attached (``ref: ...``), or no object id: no candidate to cover,
        # and no reference the agent wrote is resolved. The candidate read
        # says why there is none.
        return ""
    found = git(
        ["rev-parse", "--verify", "-q", f"{detached}^{{commit}}"],
        "reading HEAD",
        env=alternate,
        allow=(0, 1),
    )
    head = found.stdout.strip()
    if found.exit_code != 0 or not _SHA_RE.fullmatch(head):
        return ""
    present = _present(git, [head, *exclude])
    if head in present:
        return head
    listed = git(
        [
            "rev-list",
            "--objects",
            head,
            "--not",
            "--branches",
            "--tags",
            "--remotes",
            *[sha for sha in exclude if sha in present],
        ],
        "listing the objects HEAD reaches",
        env=alternate,
    )
    lines = [line for line in listed.stdout.splitlines() if line.strip()]
    ids = [line.split(" ", 1)[0] for line in lines]
    missing = set(ids) - _present(git, ids)
    wanted = [line for line in lines if line.split(" ", 1)[0] in missing]
    if not wanted:
        return head
    scratch = tempfile.mkdtemp(prefix="autoforge-import-")
    try:
        base = os.path.join(scratch, "import")
        git(
            ["pack-objects", "-q", base],
            "packing the agent's objects",
            env=alternate,
            stdin=("\n".join(wanted) + "\n").encode(),
        )
        packs = [name for name in os.listdir(scratch) if name.endswith(".pack")]
        if len(packs) != 1:
            raise ObjectImportError(
                f"the strict import of the agent's commits from {private} failed: "
                f"pack-objects wrote {len(packs)} packs; nothing was published"
            )
        with open(os.path.join(scratch, packs[0]), "rb") as handle:
            data = handle.read()
        lock = git_lock or (lambda operation: SharedGitLock(common, operation=operation))
        try:
            with lock("import the agent's objects into the shared store"):
                git(
                    [*NO_AUTO_MAINTENANCE, "index-pack", "--stdin", "--fix-thin", "--strict"],
                    "indexing the pack strictly into the shared store",
                    stdin=data,
                )
        except (GitLockTimeout, LockError) as exc:
            raise ObjectImportError(
                f"the strict import of the agent's commits from {private} failed: {exc}; "
                "nothing was published"
            ) from exc
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    if head not in _present(git, [head]):
        raise ObjectImportError(
            f"the strict import of the agent's commits from {private} left HEAD {head} "
            "outside the shared object store; nothing was published"
        )
    return head


def _present(git: Callable[..., ExecutionResult], ids: list[str]) -> set[str]:
    """Which of ``ids`` the shared store itself holds (no alternate)."""
    unique = list(dict.fromkeys(i for i in ids if _SHA_RE.fullmatch(i)))
    if not unique:
        return set()
    res = git(
        ["cat-file", "--batch-check=%(objectname)"],
        "checking which objects the shared store holds",
        stdin=("\n".join(unique) + "\n").encode(),
    )
    return {line.strip() for line in res.stdout.splitlines() if _SHA_RE.fullmatch(line.strip())}
