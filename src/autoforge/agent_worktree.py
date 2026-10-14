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
  ``.git`` file.
- **Candidate (D6.3).** :func:`read_candidate` reads the worktree's
  ``HEAD`` as ``worktrees/<id>/HEAD`` with ``--git-dir=<git common dir>``,
  after ``lstat`` found a regular file there: never through the worktree's
  ``.git`` file, the admin dir's ``commondir`` or ``gitdir``, or ``git -C
  <worktree>``.
- **Strict import (D6.5).** :func:`import_private_objects` copies the
  objects reachable from that ``HEAD`` which the shared store lacks out of
  the issue's private object directory (an enforced launch's overlay upper
  layer), through ``index-pack --strict``, which writes each object under
  the id it computes from its bytes. :func:`private_objects_problem` walks
  the directory first without following a link: an alternates file, or an
  entry that is neither a directory, a regular file nor an overlay
  whiteout, is tampering (:class:`~autoforge.errors.SandboxTamperError`).

Every git process is the controller's own hardened local request
(:func:`~autoforge.git_transport.local_git_request`, ADR 0004 D7.3, D7.5);
the private directory is named through ``GIT_ALTERNATE_OBJECT_DIRECTORIES``
for the two processes that read it, and never added to the shared store's
alternates.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .errors import SandboxTamperError, VerificationError
from .executor import ExecutionRequest, ExecutionResult, execute
from .git_transport import local_git_request
from .redaction import redact

Runner = Callable[[ExecutionRequest], ExecutionResult]

GIT_TIMEOUT_SECONDS = 120
# An import moves at most one issue's new objects; a large one is a pack.
IMPORT_TIMEOUT_SECONDS = 600
# A pointer file holds one path.
_MAX_POINTER_BYTES = 4096
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


def _read_pointer(path: Path) -> str | None:
    """A pointer file's single line, or ``None`` unless it is a regular file."""
    try:
        if not stat.S_ISREG(os.lstat(path).st_mode):
            return None
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None
    with os.fdopen(fd, "rb") as handle:
        data = handle.read(_MAX_POINTER_BYTES + 1)
    if len(data) > _MAX_POINTER_BYTES or b"\0" in data:
        return None
    text = data.decode("utf-8", "replace").strip()
    return text if text and "\n" not in text else None


def _names(value: str, base: Path, expected: Path) -> bool:
    """Whether the pointer ``value`` (absolute, or relative to ``base``) names ``expected``."""
    named = os.path.normpath(value if os.path.isabs(value) else os.path.join(base, value))
    return named in (str(expected), os.path.realpath(expected))


def bound_admin_dir(common: Path, worktree: Path) -> Path:
    """The admin dir ``<common>/worktrees/<id>`` the registry binds ``worktree`` to (D6.4).

    ``worktree`` is the literal path the controller derived (its link and
    location checks are the caller's). Refused, with a
    :class:`VerificationError` naming the inconsistency and ``git worktree
    repair``, unless exactly one registry entry's ``gitdir`` names
    ``<worktree>/.git`` and the worktree's ``.git`` is a regular file
    naming that entry.
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

    matches: list[Path] = []
    try:
        with os.scandir(registry) as entries:
            for count, entry in enumerate(entries):
                if count >= MAX_REGISTRY_ENTRIES:
                    raise refuse(f"{registry} holds more than {MAX_REGISTRY_ENTRIES} entries")
                if not entry.is_dir(follow_symlinks=False):
                    continue
                admin = registry / entry.name
                value = _read_pointer(admin / "gitdir")
                if value is not None and _names(value, admin, dotgit):
                    matches.append(admin)
    except FileNotFoundError:
        raise refuse(f"the repository registers no worktree ({registry} is absent)") from None
    except OSError as exc:
        raise refuse(f"{registry} cannot be read ({exc.strerror})") from None
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


@dataclass(frozen=True)
class Candidate:
    """The worktree's ``HEAD`` as the controller reads it (D6.3).

    ``attached``: ``HEAD`` is a symbolic reference. ``head``: the commit it
    names, "" when it names none the shared store holds. ``problem``: why
    there is no ``HEAD`` to read at all (not a regular file), else "".
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


def read_candidate(common: Path, admin: Path, worktree: str, runner: Runner | None) -> Candidate:
    """The candidate: ``worktrees/<id>/HEAD`` addressed through the common dir (D6.3).

    An inconclusive git read is a :class:`VerificationError`, never a guess.
    """
    head_file = admin / "HEAD"
    try:
        mode = os.lstat(head_file).st_mode
    except OSError as exc:
        return Candidate(False, "", f"{head_file} cannot be read ({exc.strerror})")
    if not stat.S_ISREG(mode):
        return Candidate(False, "", f"{head_file} is not a regular file")
    ref = f"worktrees/{admin.name}/HEAD"
    attached, _ = _admin_git(
        common, ["symbolic-ref", "-q", ref], runner, "whether HEAD is detached", worktree
    )
    found, head = _admin_git(
        common,
        ["rev-parse", "--verify", "-q", f"{ref}^{{commit}}"],
        runner,
        "the HEAD commit",
        worktree,
    )
    if found != 0 or not _SHA_RE.fullmatch(head):
        head = ""
    return Candidate(attached == 0, head)


def private_objects_problem(private: Path) -> str | None:
    """Why the private object directory may not be read, or ``None`` (D6.5).

    Walked without following a link. An alternates file, a link, a socket,
    a FIFO, a block device or a character device other than an overlay
    whiteout (0:0, skipped) is named; nothing is opened.
    """
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
    """Whether the private object directory has any entry at all."""
    try:
        with os.scandir(private) as entries:
            return any(True for _ in entries)
    except OSError:
        return False


class ObjectImportError(VerificationError):
    """The strict import did not complete; nothing was published."""


def import_private_objects(
    common: Path,
    admin: Path,
    private: Path,
    runner: Runner | None,
    *,
    exclude: tuple[str, ...] = (),
) -> str:
    """Import the objects ``HEAD`` reaches that the shared store lacks (D6.5).

    Returns the ``HEAD`` commit the import covered ("" when the directory
    holds nothing, or ``HEAD`` names no commit even with it). The walk of
    :func:`private_objects_problem` comes first: tampering is a
    :class:`SandboxTamperError`. Then ``rev-list --objects <head> --not
    --branches --tags --remotes <exclude>`` with the private directory as an
    alternate, the objects the shared store does not hold, ``pack-objects``
    of exactly those, and ``index-pack --stdin --fix-thin --strict`` into the
    shared store. Idempotent; a failure is :class:`ObjectImportError` and is not
    retried here. ``exclude`` are commits the shared store holds (the
    phase's recorded base), so the walk stops there.
    """
    if not holds_objects(private):
        return ""
    problem = private_objects_problem(private)
    if problem:
        raise SandboxTamperError(
            f"the private object directory {private} of the agent worktree has been "
            f"tampered with: {problem}. Nothing was read from it and nothing was published; "
            "inspect it, and remove what does not belong there before unblocking the run"
        )
    alternate = {"GIT_ALTERNATE_OBJECT_DIRECTORIES": str(private)}
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

    ref = f"worktrees/{admin.name}/HEAD^{{commit}}"
    found = git(["rev-parse", "--verify", "-q", ref], "reading HEAD", env=alternate, allow=(0, 1))
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
        git(
            ["index-pack", "--stdin", "--fix-thin", "--strict"],
            "indexing the pack strictly into the shared store",
            stdin=data,
        )
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
