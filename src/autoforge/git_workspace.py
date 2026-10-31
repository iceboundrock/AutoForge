"""The controller's read-only view of a git working tree (mode-independent).

What the engine reads from the checkout it runs in, and from an agent
worktree: the working tree's root, its git directories, ``HEAD``, the
checked-out branch and ``git status``'s records. The engine uses them for
the agent worktree's root and its creation, the sandbox's facts and denied
roots, the candidate read, the checkout anchor around an invocation and the
uncommitted-work note; LOCAL's workspace reader
(:class:`~autoforge.local_workspace.LocalWorkspace`) builds its walk on
them. Nothing here reads the configuration (ADR 0009 C1).

Everything here is a *read*: nothing is committed, staged, reset, checked
out, stashed or discarded. Every read is the controller's own hardened git
process (:func:`~autoforge.git_transport.local_git_request`, ADR 0004 D7.3,
D7.5) and takes no optional lock, so a ``git status`` never refreshes, or
waits on, the index of a checkout it only inspects. A read that fails, times
out or is truncated raises :class:`~autoforge.errors.VerificationError`; an
answer is never guessed.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .errors import VerificationError
from .executor import ExecutionRequest, ExecutionResult, execute
from .git_transport import local_git_request

Runner = Callable[[ExecutionRequest], ExecutionResult]

GIT_TIMEOUT_SECONDS = 60

# A full object name as `git rev-parse` prints it (40 hex for SHA-1, 64 for a
# SHA-256 repository). Anything else from a successful read is not an anchor.
_OBJECT_NAME_RE = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")


@dataclass(frozen=True)
class StatusEntry:
    """One record of ``git status --porcelain=v1 -z``.

    ``code`` is git's two-letter status (``" M"``, ``"A "``, ``"??"``, ...),
    ``path`` the path the record names and ``orig_path`` the path a rename or
    copy came from (``""`` for every other record). Paths are as git prints
    them, relative to the working tree's root, and are not verified to exist.
    """

    code: str
    path: str
    orig_path: str = ""


class GitWorkspace:
    """Read-only view of the git working tree containing ``workdir``."""

    def __init__(
        self,
        workdir: str | Path = ".",
        runner: Runner | None = None,
        timeout_seconds: int = GIT_TIMEOUT_SECONDS,
    ) -> None:
        self.workdir = Path(workdir)
        self._runner: Runner = runner or execute
        self.timeout = timeout_seconds
        self._root: Path | None = None
        self._git_dirs: tuple[Path, ...] | None = None

    def _git(self, argv: list[str], *, allow_failure: bool = False) -> ExecutionResult:
        res = self._runner(
            # --no-optional-locks: every call here is a read, and a
            # `git status` that refreshes the index would write
            # `.git/index` — a side effect a `--dry-run` must not have.
            # Hardened like every controller git process (ADR 0004 D7.3):
            # the repository's hooks and file-system monitor are the agent's
            # to write, so neither runs, and nothing git starts holds a
            # credential. None of this changes what a read reports.
            local_git_request(
                ["--no-optional-locks", *argv],
                cwd=str(self.workdir),
                timeout_seconds=self.timeout,
            )
        )
        if res.timed_out:
            raise VerificationError(f"`git {' '.join(argv)}` timed out in {self.workdir}")
        if res.truncated:
            # Head + marker + tail of a listing is not the working tree.
            raise VerificationError(
                f"`git {' '.join(argv)}` output in {self.workdir} was truncated at the "
                "executor's capture bound"
            )
        if res.exit_code != 0 and not allow_failure:
            detail = (res.stderr or res.stdout).strip().splitlines()
            raise VerificationError(
                f"`git {' '.join(argv)}` failed in {self.workdir}: "
                f"{detail[-1] if detail else f'exit {res.exit_code}'}"
            )
        return res

    def root(self) -> Path:
        """Absolute path of the repository working tree containing ``workdir``."""
        if self._root is None:
            res = self._git(["rev-parse", "--show-toplevel"], allow_failure=True)
            if res.exit_code != 0:
                detail = (res.stderr or res.stdout).strip().splitlines()
                raise VerificationError(
                    f"{Path(self.workdir).resolve()} is not inside a git repository "
                    f"({detail[-1] if detail else 'git rev-parse failed'}); "
                    "the controller operates on a git working tree"
                )
            top = (res.stdout or "").strip()
            if not top:
                raise VerificationError(
                    f"git did not report a working tree for {Path(self.workdir).resolve()}"
                )
            self._root = Path(os.path.realpath(top))
        return self._root

    def git_dirs(self) -> tuple[Path, ...]:
        """This repository's git directories (per-worktree and common), resolved.

        Both are needed: in a linked worktree ``--git-dir`` is
        ``<main>/.git/worktrees/<name>`` while ``--git-common-dir`` is
        ``<main>/.git``. The last entry is the common directory, which holds
        the worktree registry and ``autoforge/``.
        """
        if self._git_dirs is None:
            found: list[Path] = []
            for flag in ("--git-dir", "--git-common-dir"):
                res = self._git(["rev-parse", flag], allow_failure=True)
                if res.exit_code != 0:
                    raise VerificationError(
                        f"cannot locate the git directory of {self.workdir} "
                        f"(`git rev-parse {flag}` exited {res.exit_code}); the controller "
                        "will not act on a working tree whose git directory it cannot "
                        "identify, because it could not then tell the repository's own "
                        "metadata apart from the tree's content"
                    )
                text = (res.stdout or "").strip()
                if not text:
                    raise VerificationError(
                        f"`git rev-parse {flag}` returned nothing for {self.workdir}"
                    )
                found.append(Path(os.path.realpath(Path(self.workdir) / text)))
            self._git_dirs = tuple(dict.fromkeys(found))
        return self._git_dirs

    def head_sha(self) -> str:
        """Current HEAD, or "" when the repository has no commit yet.

        ``rev-parse --verify --quiet`` rather than plain ``rev-parse``: it
        separates "this ref does not resolve" (exit 1, the unborn HEAD of a
        repository with no commit) from "the read failed" (exit 128: a damaged
        repository, a permissions change, a missing object store).  Both used
        to collapse into ``""``, which is also the legitimate value for an
        unborn HEAD — so a failing read looked exactly like "HEAD has not
        moved", and a run whose git identity could no longer be established
        continued instead of failing closed.
        """
        res = self._git(["rev-parse", "--verify", "--quiet", "HEAD"], allow_failure=True)
        if res.exit_code == 1:
            return ""  # unborn HEAD: the ref does not resolve, and that is a fact
        if res.exit_code != 0:
            raise VerificationError(self._anchor_read_failure("HEAD", res))
        sha = (res.stdout or "").strip().lower()
        if not _OBJECT_NAME_RE.match(sha):
            raise VerificationError(
                f"`git rev-parse --verify HEAD` succeeded in {self.workdir} but returned "
                f"{sha!r}, which is not an object name; the run's git anchor cannot be "
                "established and the controller will not continue without it"
            )
        return sha

    def branch(self) -> str:
        """Checked-out branch name, or "" when HEAD is detached.

        ``symbolic-ref`` rather than ``rev-parse --abbrev-ref HEAD``: the
        latter reports the literal string "HEAD" for a detached HEAD, which is
        indistinguishable from a branch actually named ``HEAD``. An unborn
        HEAD still has a symbolic ref, so a repository with no commit reports
        its branch normally.

        ``--quiet`` makes a detached HEAD exit 1 silently, which is the one
        non-zero status that means "not a symbolic ref" rather than "the read
        failed"; every other status fails closed, for the reason given in
        :meth:`head_sha`.
        """
        res = self._git(["symbolic-ref", "--quiet", "--short", "HEAD"], allow_failure=True)
        if res.exit_code == 1:
            return ""  # detached HEAD: not a symbolic ref, and that is a fact
        if res.exit_code != 0:
            raise VerificationError(self._anchor_read_failure("the checked-out branch", res))
        name = (res.stdout or "").strip()
        if not name:
            raise VerificationError(
                f"`git symbolic-ref HEAD` succeeded in {self.workdir} but named no branch; "
                "the run's git anchor cannot be established"
            )
        return name

    def _anchor_read_failure(self, what: str, res: ExecutionResult) -> str:
        detail = (res.stderr or res.stdout or "").strip().splitlines()
        return (
            f"cannot read {what} in {self.workdir} (git exited {res.exit_code}: "
            f"{detail[-1] if detail else 'no output'}). A run is pinned to the HEAD "
            "and branch it started on, and a failed read is not evidence that they are "
            "unchanged — treating it as one would let the run continue over a repository "
            "whose identity the controller can no longer establish."
        )

    def dirty_paths(self) -> list[str]:
        """Every path :meth:`status_entries` names, a rename's or copy's source included, sorted.

        The operator's notion of uncommitted work (LOCAL's start-up
        dirty-tree policy, :func:`uncommitted_paths`), never an identity.
        """
        paths: list[str] = []
        for entry in self.status_entries():
            if entry.orig_path:
                paths.append(entry.orig_path)
            paths.append(entry.path)
        return sorted(dict.fromkeys(paths))

    def status_entries(self) -> list[StatusEntry]:
        """The records behind :meth:`dirty_paths`, in git's order, with their codes.

        The same single ``git status`` read, kept as git reports it rather
        than reduced to a set of paths, for a caller that has to say *how*
        each path is uncommitted (#187). It is the operator's notion of
        uncommitted work, never an identity.
        """
        res = self._git(["status", "--porcelain=v1", "-z", "--untracked-files=all"])
        out = res.stdout or ""
        fields = out.split("\0")
        entries: list[StatusEntry] = []
        index = 0
        while index < len(fields):
            record = fields[index]
            index += 1
            if not record:
                continue
            if len(record) < 4:
                raise VerificationError(f"cannot parse `git status` record {record!r}")
            code, path, old = record[:2], record[3:], ""
            if code[0] in "RC" or code[1] in "RC":
                # A rename or a copy (`status.renames=copies`) is two
                # NUL-separated fields: "<code> <new>\0<old>". Read as a
                # record of its own, the source path would be cut into a
                # made-up code and path.
                if index < len(fields):
                    old = fields[index]
                    index += 1
            entries.append(StatusEntry(code=code, path=path, orig_path=old))
        return entries


def uncommitted_paths(
    workdir: str | Path,
    runner: Runner | None = None,
    *,
    timeout_seconds: int = GIT_TIMEOUT_SECONDS,
) -> list[str]:
    """What ``git status`` reports as changed or untracked in ``workdir``, sorted.

    The one read of "what uncommitted work is here" (:meth:`GitWorkspace.dirty_paths`,
    which it is): a single ``git status --porcelain=v1 -z
    --untracked-files=all``, with no optional locks, so the index is not
    refreshed, and with hooks and the file-system monitor off. An agent
    failure that leaves the state unchanged reports its count for the REMOTE
    agent worktree (#189), and a write phase's prompt lists the same read's
    records, codes included (:meth:`GitWorkspace.status_entries`, #187). It
    is the operator's notion of uncommitted work, never an identity. A read
    that fails, times out or is truncated raises VerificationError; a caller
    that cannot use the answer omits it rather than guessing.
    """
    return GitWorkspace(workdir, runner=runner, timeout_seconds=timeout_seconds).dirty_paths()
