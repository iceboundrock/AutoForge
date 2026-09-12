"""Independent verification of the local working tree (LOCAL mode).

In REMOTE mode GitHub is the source of truth: a `CONTROL_RESULT` claiming a
PR exists is checked against `gh`. LOCAL mode has no GitHub, so the working
tree itself is the source of truth and this module is the only place that
reads it. The engine never shells out to `git` directly.

Everything here is a *read*. The controller never commits, stages, resets,
checks out, stashes or discards anything: a local run leaves the operator's
work exactly where it found it, and an implementation may legitimately
consist of uncommitted (even untracked) files.

Workspace identity
------------------
A review is only meaningful if the controller can say *which bytes* were
reviewed and prove they did not change afterwards. That is the workspace
snapshot, and it is defined by one rule:

    The snapshot covers **every entry in the working tree**, found by the
    controller walking the tree itself, and the only entries it does not
    cover are the ones an explicitly enumerated, fingerprinted exclusion rule
    removes.  There is no third category.

This is deliberately *not* built on ``git status``. Git reports what is
interesting to git, which is a moving target: an ignored file, a file marked
``assume-unchanged`` or ``skip-worktree``, a mode change under
``core.fileMode=false``, a clean tracked file, the contents of a submodule —
each is invisible to some git invocation while being perfectly able to change
what a reviewer concluded. Asking git "what changed?" and fingerprinting the
answer makes the snapshot's completeness a property of git's configuration,
which the agent under review can edit. The controller walks the tree instead,
so no git setting, index flag or ignore rule can subtract from the snapshot.

Git keeps exactly one job in LOCAL mode: telling the controller where the
repository is (its working tree and its git directory) and what its *anchor*
is (HEAD and the checked-out branch). Those are separate facts, checked
separately; no decision about which bytes are bound depends on git.

Classification is total. Every entry the walk meets is exactly one of:

===================  =========================================================
regular file         included: permission bits + SHA-256 of its bytes
directory            included: permission bits; the walk descends into it
symbolic link        included: SHA-256 of its *link text*, and the target must
                     resolve inside the working tree and outside every
                     excluded region — a link to bytes the snapshot does not
                     cover would let those bytes change without the
                     fingerprint moving
excluded             an enumerated rule matched: the rule and the path are
                     recorded *in* the fingerprint, so what is not covered is
                     itself part of the workspace's identity
anything else        refused (fail closed): FIFOs, sockets, devices,
                     unreadable entries, and nested git repositories
===================  =========================================================

There is no "git did not report it, so the controller did not see it" state.

Exclusion rules
---------------
Exactly two kinds of rule exist, and both are recorded in the fingerprint:

``gitdir``
    This repository's own git directory, matched by ``(st_dev, st_ino)``
    rather than by the name ``.git`` — a name is not evidence. (A linked
    worktree's ``.git`` *file* at the top level is matched too: it is the
    pointer git itself put there.) Any **other** entry named ``.git`` is
    refused: a submodule or a nested repository holds a whole second tree
    whose contents git will not show through the outer one, and LOCAL v1
    does not support binding it. Fail closed, not silently unbound.

``exclude:<pattern>``
    An operator-declared pattern from ``local.exclude``. This exists because
    a complete walk really is complete: ``.venv/``, ``node_modules/`` and
    ``__pycache__/`` are in the working tree, the test suite the controller
    itself runs rewrites them, and no amount of cleverness makes "the
    reviewer changed nothing" true over bytes that a build tool rewrites.
    The honest answer is an explicit, operator-visible list that is hashed
    into the fingerprint and named to the reviewer in its prompt — not a
    built-in default that quietly decides which bytes do not count.

A LOCAL state directory is **not** an exclusion rule, because it is not
inside the reviewed tree: see :meth:`LocalWorkspace.check_state_dir_location`.

Cost
----
A complete walk is bounded by ``local.max_workspace_entries`` and
``local.max_workspace_bytes``. Exceeding either is a *refusal* naming the
largest subtrees seen so far and the exact YAML to paste — never a silent
downgrade to metadata-only hashing, which would accept an equal-sized
replacement with a restored mtime.
"""

from __future__ import annotations

import hashlib
import os
import posixpath
import re
import stat
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path

from .errors import ConfigurationError, VerificationError
from .executor import ExecutionRequest, ExecutionResult, execute
from .prompts import escape_inline
from .run_contract import WorkspacePolicy
from .safefs import (
    ReadLimitExceeded,
    SafeRoot,
    UnreadableEntryError,
    WalkBudgetExceeded,
    entry_kind,
    open_regular_at,
    readlink_at,
)

Runner = Callable[[ExecutionRequest], ExecutionResult]

GIT_TIMEOUT_SECONDS = 60

# Bytes read per file when hashing working-tree content.
_CHUNK = 1024 * 1024

FEATURE_SPEC_SUFFIXES = (".md", ".markdown")

#: Bumped whenever the *meaning* of a fingerprint changes, so a fingerprint
#: persisted by an older controller can never compare equal to a new one.
SNAPSHOT_TAG = "autoforge-workspace-v4"

DEFAULT_MAX_ENTRIES = 50_000
DEFAULT_MAX_BYTES = 512 * 1024 * 1024

# A full object name as `git rev-parse` prints it (40 hex for SHA-1, 64 for a
# SHA-256 repository). Anything else from a successful read is not an anchor.
_OBJECT_NAME_RE = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")


def hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# -- exclusion rules ---------------------------------------------------------
def normalize_exclude_pattern(pattern: str) -> str:
    """Canonical text of one ``local.exclude`` pattern (ConfigurationError if unusable).

    The canonical form is what gets hashed into the fingerprint, so two
    spellings of the same rule must not produce two different workspace
    identities.
    """
    if not isinstance(pattern, str):
        raise ConfigurationError(f"local.exclude entry must be a string, got {pattern!r}")
    text = pattern.strip().replace(os.sep, "/").strip("/")
    if not text:
        raise ConfigurationError("local.exclude entry must not be empty")
    if "\0" in text:
        raise ConfigurationError(f"local.exclude entry {pattern!r} contains a NUL byte")
    parts = [p for p in text.split("/") if p != ""]
    if any(p in (".", "..") for p in parts):
        raise ConfigurationError(
            f"local.exclude entry {pattern!r} may not contain '.' or '..': patterns are "
            "matched against paths relative to the repository root"
        )
    return "/".join(parts)


def _match_components(pattern: tuple[str, ...], path: tuple[str, ...]) -> bool:
    """Component-wise glob match with ``**`` spanning zero or more components."""
    if not pattern:
        return not path
    head, rest = pattern[0], pattern[1:]
    if head == "**":
        # Zero components, or one component consumed and try again.
        if _match_components(rest, path):
            return True
        return bool(path) and _match_components(pattern, path[1:])
    if not path:
        return False
    if not fnmatchcase(path[0], head):
        return False
    return _match_components(rest, path[1:])


@dataclass(frozen=True)
class Exclusion:
    """One reason a path is not covered by the snapshot."""

    path: str  # repository-relative, "/"-separated
    rule: str  # canonical rule text, e.g. "gitdir" or "exclude:.venv"


# -- snapshot ----------------------------------------------------------------
KIND_FILE = "file"
KIND_DIR = "dir"
KIND_LINK = "link"
KIND_EXCLUDED = "excluded"


@dataclass(frozen=True)
class WorkspaceEntry:
    """One classified entry of the working tree.

    ``digest`` is the SHA-256 of a regular file's bytes, or of a symbolic
    link's target text; it is empty for a directory and for an excluded path.
    ``detail`` carries a directory's permission bits and, for an excluded
    path, the rule that excluded it.
    """

    path: str
    kind: str
    mode: str
    digest: str
    detail: str = ""


@dataclass(frozen=True)
class WorkspaceSnapshot:
    """Exactly which bytes the working tree held when this was taken."""

    root: Path
    head_sha: str
    branch: str
    entries: tuple[WorkspaceEntry, ...]
    rules: tuple[str, ...]
    fingerprint: str
    total_bytes: int

    @property
    def anchor(self) -> str:
        """HEAD + branch, the git fact bound independently of the tree bytes."""
        head = self.head_sha or "(unborn)"
        branch = self.branch or "(detached)"
        return f"{head}@{branch}"

    @property
    def file_count(self) -> int:
        return sum(1 for e in self.entries if e.kind == KIND_FILE)

    @property
    def exclusions(self) -> tuple[Exclusion, ...]:
        return tuple(
            Exclusion(path=e.path, rule=e.detail) for e in self.entries if e.kind == KIND_EXCLUDED
        )

    def describe(self, limit: int = 12) -> str:
        """One line for a prompt or a block reason."""
        files = self.file_count
        dirs = sum(1 for e in self.entries if e.kind == KIND_DIR)
        links = sum(1 for e in self.entries if e.kind == KIND_LINK)
        excluded = self.exclusions
        parts = [
            f"{files} file(s), {dirs} director(ies), {links} symlink(s), "
            f"{self.total_bytes} byte(s) hashed"
        ]
        if excluded:
            shown = ", ".join(
                f"{escape_inline(x.path)} [{escape_inline(x.rule)}]" for x in excluded[:limit]
            )
            more = "" if len(excluded) <= limit else f" and {len(excluded) - limit} more"
            parts.append(f"excluded: {shown}{more}")
        return " | ".join(parts)

    def describe_exclusions(self) -> str:
        """What the reviewer must be told is *not* bound by the fingerprint."""
        excluded = self.exclusions
        if not excluded:
            return "(none — every entry in the working tree is covered by the fingerprint)"
        return "; ".join(f"{escape_inline(x.path)} [{escape_inline(x.rule)}]" for x in excluded)


def _fingerprint(
    entries: Iterable[WorkspaceEntry],
    rules: Iterable[str],
) -> str:
    """SHA-256 over the classified tree and the rules that shaped it.

    Every field is length-prefixed before it is fed to the hash, because a
    path may contain any byte except NUL and ``/`` — including a newline —
    and a separator-delimited encoding would let two different trees produce
    one digest.

    The git anchor is deliberately *not* part of this: "which bytes are in the
    tree" and "which commit is checked out" are two facts with two different
    failure modes, and mixing them would report a plain ``git commit`` (which
    changes HEAD and nothing else) as "the reviewer modified the working
    tree".
    """
    h = hashlib.sha256()

    def feed(value: str) -> None:
        raw = value.encode("utf-8", errors="surrogateescape")
        h.update(str(len(raw)).encode("ascii"))
        h.update(b"\0")
        h.update(raw)
        h.update(b"\0")

    feed(SNAPSHOT_TAG)
    ordered_rules = sorted(rules)
    feed(str(len(ordered_rules)))
    for rule in ordered_rules:
        feed(rule)
    ordered = sorted(entries, key=lambda e: e.path.encode("utf-8", errors="surrogateescape"))
    feed(str(len(ordered)))
    for entry in ordered:
        feed(entry.kind)
        feed(entry.mode)
        feed(entry.digest)
        feed(entry.detail)
        feed(entry.path)
    return h.hexdigest()


def _mode_text(st: os.stat_result) -> str:
    return f"{stat.S_IMODE(st.st_mode):04o}"


def _refuse(path: str, what: str, remedy: str) -> VerificationError:
    return VerificationError(
        f"cannot bind the working tree: {path} is {what}. A LOCAL run must be able to say "
        f"exactly which bytes the reviewer reviewed, and it cannot for this entry. {remedy}"
    )


class LocalWorkspace:
    """Read-only view of the git working tree a LOCAL run operates on."""

    def __init__(
        self,
        workdir: str | Path = ".",
        runner: Runner | None = None,
        timeout_seconds: int = GIT_TIMEOUT_SECONDS,
        exclude: Iterable[str] = (),
        max_entries: int = DEFAULT_MAX_ENTRIES,
        max_bytes: int = DEFAULT_MAX_BYTES,
    ) -> None:
        self.workdir = Path(workdir)
        self._runner: Runner = runner or execute
        self.timeout = timeout_seconds
        self.exclude: tuple[str, ...] = tuple(
            sorted({normalize_exclude_pattern(p) for p in exclude})
        )
        self.max_entries = max_entries
        self.max_bytes = max_bytes
        self._root: Path | None = None
        self._git_dirs: tuple[Path, ...] | None = None
        # Content-digest memo, keyed by identity *and* change indicators. The
        # key is a cache key only: it never becomes part of the fingerprint,
        # so a forged mtime can at worst cost a re-hash, never bind the wrong
        # bytes... and ctime cannot be set backwards without root at all.
        self._digests: dict[tuple[int, int, int, int, int, int], str] = {}

    # -- reader policy ----------------------------------------------------
    def policy(self) -> WorkspacePolicy:
        """Everything about this reader that shapes a snapshot, as contract data.

        A fingerprint binds the working tree *as this reader classifies it*,
        so the reader's configuration is part of what a review means (see
        :class:`autoforge.run_contract.WorkspacePolicy`). The snapshot tag
        is included because the walk's classification rules are part of the
        reader too.
        """
        return WorkspacePolicy(
            exclude=self.exclude,
            max_entries=self.max_entries,
            max_bytes=self.max_bytes,
            snapshot_tag=SNAPSHOT_TAG,
        )

    # -- git plumbing -----------------------------------------------------
    def _git(self, argv: list[str], *, allow_failure: bool = False) -> ExecutionResult:
        res = self._runner(
            ExecutionRequest(
                # --no-optional-locks: every call here is a read, and a
                # `git status` that refreshes the index would write
                # `.git/index` — a side effect a `--dry-run` must not have.
                command=["git", "--no-optional-locks", *argv],
                cwd=str(self.workdir),
                timeout_seconds=self.timeout,
            )
        )
        if res.timed_out:
            raise VerificationError(f"`git {' '.join(argv)}` timed out in {self.workdir}")
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
                    "local mode operates on a git working tree"
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
        ``<main>/.git``, and either can be the directory that physically sits
        inside the tree being walked.
        """
        if self._git_dirs is None:
            found: list[Path] = []
            for flag in ("--git-dir", "--git-common-dir"):
                res = self._git(["rev-parse", flag], allow_failure=True)
                if res.exit_code != 0:
                    raise VerificationError(
                        f"cannot locate the git directory of {self.workdir} "
                        f"(`git rev-parse {flag}` exited {res.exit_code}); the controller "
                        "will not walk a working tree whose git directory it cannot identify, "
                        "because it could not then tell the repository's own metadata apart "
                        "from reviewable content"
                    )
                text = (res.stdout or "").strip()
                if not text:
                    raise VerificationError(
                        f"`git rev-parse {flag}` returned nothing for {self.workdir}"
                    )
                found.append(Path(os.path.realpath(Path(self.workdir) / text)))
            self._git_dirs = tuple(dict.fromkeys(found))
        return self._git_dirs

    def local_state_dir(self) -> Path:
        """Where a LOCAL run keeps its runtime state by default.

        Inside the git directory, which is *outside* the reviewed tree by
        construction. That is the whole point: a state directory in the
        working tree would have to be carved out of the workspace snapshot by
        name, and "these particular file names do not count" is exactly the
        kind of rule an agent writing into the tree can exploit.
        """
        return self.git_dirs()[0] / "autoforge" / "state"

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
            f"{detail[-1] if detail else 'no output'}). A local run is pinned to the HEAD "
            "and branch it started on, and a failed read is not evidence that they are "
            "unchanged — treating it as one would let the run continue over a repository "
            "whose identity the controller can no longer establish."
        )

    def dirty_paths(self) -> list[str]:
        """Paths git considers changed, for the *start-up dirty-tree policy* only.

        This is a policy question ("is the operator's work in the way?"), not
        an identity question: nothing derived from it enters the fingerprint.
        Git's own notion of "changed" is exactly right here — it is the
        operator's mental model of their uncommitted work — and it is exactly
        wrong for identity, which is why the two are separate methods.
        """
        res = self._git(["status", "--porcelain=v1", "-z", "--untracked-files=all"])
        out = res.stdout or ""
        fields = out.split("\0")
        paths: list[str] = []
        index = 0
        while index < len(fields):
            record = fields[index]
            index += 1
            if not record:
                continue
            if len(record) < 4:
                raise VerificationError(f"cannot parse `git status` record {record!r}")
            code, path = record[:2], record[3:]
            if code[0] == "R" or code[1] == "R":
                # A rename is two NUL-separated fields: "<code> <new>\0<old>".
                if index < len(fields):
                    old = fields[index]
                    index += 1
                    if old:
                        paths.append(old)
            if path:
                paths.append(path)
        return sorted(dict.fromkeys(paths))

    # -- state directory --------------------------------------------------
    def check_state_dir_location(self, state_dir: str | Path) -> None:
        """Require the state directory to sit outside the reviewed tree.

        LOCAL mode's workspace snapshot covers the *whole* working tree. A
        state directory inside it would be rewritten by the controller on
        every step, so it would have to be excluded — and an exclusion carved
        out by path is a region of the tree an agent can write into knowing
        the fingerprint will not move. Putting runtime state in the git
        directory removes the problem instead of managing it: the git
        directory is already excluded, by inode identity, for reasons that
        have nothing to do with AutoForge.
        """
        root = self.root()
        resolved = Path(os.path.realpath(state_dir))
        if not _is_within(resolved, root):
            return
        for git_dir in self.git_dirs():
            if resolved == git_dir or _is_within(resolved, git_dir):
                return
        raise ConfigurationError(
            f"state directory {resolved} is inside the LOCAL working tree {root}. A LOCAL "
            "run fingerprints every byte of the working tree, so runtime state kept there "
            "would either invalidate its own fingerprint on every step or have to be carved "
            "out of it by path — a region an agent could then write into unnoticed. Leave "
            f"--state-dir unset (the default is {self.local_state_dir()}) or point it "
            "outside the repository."
        )

    # -- the snapshot -----------------------------------------------------
    def snapshot(self) -> WorkspaceSnapshot:
        """Classify every entry of the working tree and fingerprint the result."""
        root = self.root()
        head = self.head_sha()
        branch = self.branch()
        git_ids = self._git_dir_identities()
        entries: list[WorkspaceEntry] = []
        total_bytes = 0
        by_top: dict[str, int] = {}

        # One translation point for "the controller may not read this".
        # Whether it is a file it cannot open or a directory it cannot list,
        # unreadable means unbindable, and the answer is the same refusal.
        # The entry budget is the walk's own: it is charged as directory
        # entries are *listed*, before they are sorted, stat'ed or handed
        # here, so a directory of a million names or a nest ten thousand deep
        # costs at most the budget in work and never a RecursionError.
        try:
            with SafeRoot.open(root) as tree:
                for entry in tree.walk(max_entries=self.max_entries):
                    top = entry.relpath.split("/", 1)[0]
                    by_top[top] = by_top.get(top, 0) + 1
                    rule = self._exclusion_rule(entry.relpath, entry.st, git_ids)
                    if rule is not None:
                        entries.append(
                            WorkspaceEntry(
                                path=entry.relpath,
                                kind=KIND_EXCLUDED,
                                mode="",
                                digest="",
                                detail=rule,
                            )
                        )
                        entry.skip = True
                        continue
                    if entry.name == ".git":
                        raise _refuse(
                            entry.relpath,
                            "a nested git repository or submodule",
                            "LOCAL mode binds one working tree and cannot see inside a second one, "
                            "so it refuses rather than reviewing code it cannot pin. Remove it, or "
                            f"declare it unreviewed by adding '{posixpath.dirname(entry.relpath)}' "
                            "to local.exclude.",
                        )
                    if entry.is_dir:
                        entries.append(
                            WorkspaceEntry(
                                path=entry.relpath,
                                kind=KIND_DIR,
                                mode=_mode_text(entry.st),
                                digest="",
                            )
                        )
                    elif entry.is_symlink:
                        target = readlink_at(entry.dir_fd, entry.name)
                        self._check_link_target(entry.relpath, target, root, git_ids)
                        entries.append(
                            WorkspaceEntry(
                                path=entry.relpath,
                                kind=KIND_LINK,
                                mode="",
                                digest=hash_bytes(target.encode("utf-8", errors="surrogateescape")),
                            )
                        )
                    elif entry.is_regular:
                        total_bytes += entry.st.st_size
                        if total_bytes > self.max_bytes:
                            raise self._too_big(
                                f"the working tree holds more than {self.max_bytes} bytes of "
                                "regular-file content",
                                "local.max_workspace_bytes",
                                by_top,
                            )
                        entries.append(
                            WorkspaceEntry(
                                path=entry.relpath,
                                kind=KIND_FILE,
                                mode=_mode_text(entry.st),
                                digest=self._digest(
                                    entry.dir_fd, entry.name, entry.relpath, entry.st
                                ),
                            )
                        )
                    else:
                        raise _refuse(
                            entry.relpath,
                            f"a {entry_kind(entry.st.st_mode)}",
                            "Its contents are not a sequence of bytes the controller can hash, so "
                            "it cannot be part of a reviewed snapshot. Move it out of the working "
                            "tree, or add it to local.exclude to declare it unreviewed.",
                        )
        except WalkBudgetExceeded as exc:
            raise self._too_big(
                f"the working tree has more than {self.max_entries} entries "
                f"(the budget ran out while listing {exc.where})",
                "local.max_workspace_entries",
                by_top,
            ) from exc
        except UnreadableEntryError as exc:
            raise _refuse(
                exc.path,
                "not readable by the controller",
                "An entry the controller cannot read is an entry it cannot prove "
                "unchanged. Fix its permissions, or add it to local.exclude to declare "
                "it unreviewed.",
            ) from exc

        rules = ("gitdir",) + tuple(f"exclude:{p}" for p in self.exclude)
        return WorkspaceSnapshot(
            root=root,
            head_sha=head,
            branch=branch,
            entries=tuple(entries),
            rules=rules,
            fingerprint=_fingerprint(entries, rules),
            total_bytes=total_bytes,
        )

    def _git_dir_identities(self) -> set[tuple[int, int]]:
        ids: set[tuple[int, int]] = set()
        for git_dir in self.git_dirs():
            try:
                st = os.lstat(git_dir)
            except OSError as exc:
                raise VerificationError(
                    f"cannot inspect the git directory {git_dir}: {exc}. The controller "
                    "identifies it by inode, not by name, and will not walk the working tree "
                    "without that identity."
                ) from exc
            ids.add((st.st_dev, st.st_ino))
        return ids

    def _exclusion_rule(
        self, relpath: str, st: os.stat_result | None, git_ids: set[tuple[int, int]]
    ) -> str | None:
        """The rule excluding ``relpath``, or ``None`` when it is covered.

        This is the *only* definition of "excluded from the snapshot": the
        walk asks it about every entry, and the link-target check asks it
        about every ancestor of a resolved target, so the two can never
        disagree about what a reviewer may read unbound. ``st`` is the
        entry's ``lstat``, or ``None`` for a path that does not exist (a
        dangling link's target), for which only the path rules apply.

        Identity first: the git directory is recognised by ``(st_dev,
        st_ino)``, so a decoy named ``.git`` is not excluded and a git
        directory under any other name still is.
        """
        if st is not None:
            if stat.S_ISDIR(st.st_mode) and (st.st_dev, st.st_ino) in git_ids:
                return "gitdir"
            if relpath == ".git" and stat.S_ISREG(st.st_mode):
                # A linked worktree's pointer file. It is git's own metadata
                # and it names a directory the walk never enters.
                return "gitdir"
        return self._config_rule(relpath)

    def _config_rule(self, relpath: str) -> str | None:
        """The ``local.exclude`` rule covering ``relpath`` or any ancestor of it."""
        parts = tuple(p for p in relpath.split("/") if p)
        for pattern in self.exclude:
            pat = tuple(pattern.split("/"))
            for depth in range(1, len(parts) + 1):
                if _match_components(pat, parts[:depth]):
                    return f"exclude:{pattern}"
        return None

    def _check_link_target(
        self, relpath: str, target: str, root: Path, git_ids: set[tuple[int, int]]
    ) -> None:
        """A symbolic link may only point at bytes this snapshot also covers.

        The link *text* is what the snapshot binds, so the link itself cannot
        change without moving the fingerprint. What the link resolves to is a
        different question: if it leaves the working tree, or lands in an
        excluded region, the reviewer read bytes that can be replaced with the
        fingerprint standing still.

        The resolution uses ``realpath``, which follows other links — and that
        is sound here rather than a hole, because *every* link in the tree
        gets this check: if any link on the resolved path escaped, that link
        is itself refused. The conjunction over the whole walk is the
        invariant, not each check in isolation.

        "Excluded" is decided by :meth:`_exclusion_rule`, asked about every
        ancestor of the resolved target exactly as the walk asks it about
        every entry: a target the walk would have skipped (a git directory
        by inode, a linked worktree's ``.git`` pointer file, a
        ``local.exclude`` region) is a target whose bytes are unbound,
        whichever of those rules would have skipped it.
        """
        parent = root / posixpath.dirname(relpath) if posixpath.dirname(relpath) else root
        resolved = Path(os.path.realpath(os.path.join(str(parent), target)))
        if resolved != root and not _is_within(resolved, root):
            raise _refuse(
                relpath,
                f"a symbolic link to {resolved}, which is outside the working tree",
                "The reviewer would read bytes the snapshot cannot bind, so they could be "
                "replaced without the fingerprint changing. Point it inside the repository, "
                "or add it to local.exclude to declare it unreviewed.",
            )
        rel = os.path.relpath(resolved, root).replace(os.sep, "/")
        if rel in (".", ""):
            return
        parts = rel.split("/")
        for depth in range(1, len(parts) + 1):
            prefix = "/".join(parts[:depth])
            try:
                st: os.stat_result | None = os.lstat(root.joinpath(*parts[:depth]))
            except OSError:
                # Nothing exists there (yet): only the path rules can apply.
                st = None
            rule = self._exclusion_rule(prefix, st, git_ids)
            if rule is not None:
                raise _refuse(
                    relpath,
                    f"a symbolic link to {prefix}, which '{rule}' excludes from the snapshot",
                    "Its bytes are not bound, so they could change without the fingerprint "
                    "moving. Remove the link, or exclude the link itself as well.",
                )

    def _digest(self, dir_fd: int, name: str, relpath: str, st: os.stat_result) -> str:
        key = (
            st.st_dev,
            st.st_ino,
            st.st_size,
            st.st_mtime_ns,
            st.st_ctime_ns,
            stat.S_IMODE(st.st_mode),
        )
        cached = self._digests.get(key)
        if cached is not None:
            return cached
        try:
            fd = open_regular_at(dir_fd, name, os.O_RDONLY, where=relpath)
        except FileNotFoundError as exc:
            raise _refuse(
                relpath,
                "gone between being listed and being read",
                "Something is writing the working tree while the controller is binding it; "
                "stop it and re-run.",
            ) from exc
        h = hashlib.sha256()
        try:
            with os.fdopen(fd, "rb") as fh:
                remaining = st.st_size
                while remaining:
                    chunk = fh.read(min(_CHUNK, remaining))
                    if not chunk:
                        break
                    h.update(chunk)
                    remaining -= len(chunk)
                if remaining or fh.read(1):
                    raise _refuse(
                        relpath,
                        "grew or shrank while it was being read",
                        "Something is writing the working tree while the controller is binding it; "
                        "stop it and re-run.",
                    )
                final = os.fstat(fh.fileno())
                final_key = (
                    final.st_dev,
                    final.st_ino,
                    final.st_size,
                    final.st_mtime_ns,
                    final.st_ctime_ns,
                    stat.S_IMODE(final.st_mode),
                )
                if final_key != key:
                    raise _refuse(
                        relpath,
                        "changed while it was being read",
                        "Something is writing the working tree while the controller is binding it; "
                        "stop it and re-run.",
                    )
        except OSError as exc:
            raise _refuse(
                relpath, f"unreadable ({exc})", "Fix it, or add it to local.exclude."
            ) from exc
        digest = h.hexdigest()
        self._digests[key] = digest
        return digest

    def _too_big(self, what: str, setting: str, by_top: dict[str, int]) -> VerificationError:
        biggest = sorted(by_top.items(), key=lambda kv: -kv[1])[:8]
        listing = "\n".join(f"    - {name}   ({n} entries seen so far)" for name, n in biggest)
        return VerificationError(
            f"refusing to fingerprint the working tree: {what}.\n"
            "A LOCAL run binds the review to every byte of the tree, and it will not fall "
            "back to hashing metadata instead — an equal-sized replacement with a restored "
            "mtime would then pass. Either exclude what does not need reviewing:\n"
            "  local:\n"
            "    exclude:\n"
            "      - .venv\n"
            "      - '**/__pycache__'\n"
            f"or raise {setting} deliberately.\n"
            f"Largest top-level entries seen so far:\n{listing}"
        )

    # -- writes into the working tree -------------------------------------
    def tree_root(self) -> SafeRoot:
        """A write capability on the working tree (caller closes it).

        The only controller writes into the working tree are the ones
        ``autoforge local init`` makes, and they go through the same boundary
        as every other controller write.
        """
        return SafeRoot.open(self.root())


def _is_within(path: Path, base: Path) -> bool:
    """True when ``path`` is ``base`` or lies beneath it (both already resolved).

    Compared component-wise: a textual prefix test would say ``/repo-backup``
    is inside ``/repo``.
    """
    try:
        path.relative_to(base)
    except ValueError:
        return False
    return True


# -- feature specifications ---------------------------------------------------
@dataclass(frozen=True)
class FeatureSpec:
    """A resolved, frozen feature specification."""

    path: Path  # absolute, real path
    relative_path: str  # repository-relative, "/"-separated
    content: str
    sha256: str


def resolve_feature_spec(workspace: LocalWorkspace, spec_path: str | Path) -> Path:
    """Resolve ``spec_path`` to a regular Markdown file inside the repository.

    Rejects (with ConfigurationError): a path that escapes the repository via
    ``..`` or an absolute path elsewhere, a symbolic link, a directory, a
    FIFO/socket/device, a non-Markdown suffix, and anything unreadable. The
    feature specification is *untrusted project data*; the controller only
    guarantees where it came from, never what it says.
    """
    root = workspace.root()
    raw = Path(spec_path)
    candidate = raw if raw.is_absolute() else Path(workspace.workdir) / raw
    # realpath on the *parent* only: the file itself must not be a symlink,
    # and resolving it would silently accept one pointing outside the repo.
    parent = Path(os.path.realpath(candidate.parent))
    resolved = parent / candidate.name
    try:
        rel = os.path.relpath(resolved, root)
    except ValueError:
        raise ConfigurationError(
            f"feature specification {spec_path!r} is not inside the repository {root}"
        ) from None
    if rel.startswith("..") or os.path.isabs(rel):
        raise ConfigurationError(
            f"feature specification {spec_path!r} resolves to {resolved}, which is outside "
            f"the repository {root}; local runs only accept a specification inside the "
            "working repository"
        )
    if resolved.suffix.lower() not in FEATURE_SPEC_SUFFIXES:
        raise ConfigurationError(
            f"feature specification {resolved} must be a Markdown file "
            f"({' or '.join(FEATURE_SPEC_SUFFIXES)})"
        )
    try:
        st = os.lstat(resolved)
    except FileNotFoundError:
        raise ConfigurationError(f"feature specification not found: {resolved}") from None
    except OSError as exc:
        raise ConfigurationError(f"cannot read feature specification {resolved}: {exc}") from exc
    if stat.S_ISLNK(st.st_mode):
        raise ConfigurationError(
            f"feature specification {resolved} is a symbolic link; local runs require a "
            "regular file inside the repository so the frozen specification cannot be "
            "redirected during the run"
        )
    if not stat.S_ISREG(st.st_mode):
        raise ConfigurationError(
            f"feature specification {resolved} is not a regular file "
            f"(mode {st.st_mode:o}); local runs require a regular Markdown file"
        )
    return resolved


def read_feature_spec(workspace: LocalWorkspace, spec_path: str | Path) -> FeatureSpec:
    """Resolve + read + hash a feature specification (ConfigurationError on any problem)."""
    resolved = resolve_feature_spec(workspace, spec_path)
    rel = os.path.relpath(resolved, workspace.root()).replace(os.sep, "/")
    # The specification is bounded by the same budget as the tree it lives
    # in: a run's prompts carry it verbatim, and a spec the snapshot would
    # refuse to fingerprint is not one the controller should read whole.
    with workspace.tree_root() as tree:
        try:
            data = tree.read_bytes(rel, limit=workspace.max_bytes)
        except ReadLimitExceeded as exc:
            raise ConfigurationError(
                f"feature specification {resolved} is larger than {exc.limit} bytes "
                "(local.max_workspace_bytes); a specification is a short Markdown "
                "document, not a corpus"
            ) from exc
        except OSError as exc:
            raise ConfigurationError(
                f"cannot read feature specification {resolved}: {exc}"
            ) from exc
    if data is None:
        raise ConfigurationError(f"feature specification not found: {resolved}")
    try:
        content = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ConfigurationError(
            f"feature specification {resolved} is not valid UTF-8 ({exc})"
        ) from exc
    if not content.strip():
        raise ConfigurationError(
            f"feature specification {resolved} is empty; describe the feature first "
            "(see 'autoforge local init')"
        )
    return FeatureSpec(
        path=resolved,
        relative_path=rel,
        content=content,
        sha256=hash_bytes(data),
    )


def verify_feature_spec_unchanged(
    workspace: LocalWorkspace, relative_path: str, expected_sha256: str, *, when: str
) -> FeatureSpec:
    """Re-read the frozen specification and require its hash to be unchanged.

    ``when`` names the moment for the error message ("before REVIEW", "after
    ANALYZE_EXECUTE", ...). Raises :class:`VerificationError` — an agent that
    rewrote its own acceptance criteria is a controller-invariant violation,
    not a configuration mistake.
    """
    target = workspace.root() / relative_path
    try:
        spec = read_feature_spec(workspace, target)
    except ConfigurationError as exc:
        raise VerificationError(
            f"feature specification {relative_path} is no longer usable {when}: {exc}"
        ) from exc
    if spec.sha256 != expected_sha256:
        raise VerificationError(
            f"feature specification {relative_path} changed {when}: expected SHA-256 "
            f"{expected_sha256[:16]}..., found {spec.sha256[:16]}.... The specification is "
            "frozen for the whole run so an agent cannot rewrite its own acceptance "
            "criteria. Restore the file, or start a new run for the changed requirements."
        )
    return spec


# -- `autoforge local init` ---------------------------------------------------
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")

FEATURE_TEMPLATE = """# Feature: {title}

## Problem

<!-- What is wrong or missing today, and for whom? One short paragraph. -->

## Requirements

- [ ] <!-- Concrete, checkable behaviour the implementation must provide. -->

## Acceptance Criteria

- [ ] <!-- How a reviewer decides this is done. One line per criterion. -->

## Non-goals

- <!-- Explicitly out of scope, so the agent does not invent work. -->

## Notes / Decisions

<!-- Constraints, prior art, files worth reading first, decisions already made. -->
"""


def slug_to_title(slug: str) -> str:
    """``add-transaction-filter`` -> ``Add Transaction Filter``."""
    words = [w for w in re.split(r"[-_.]+", slug) if w]
    return " ".join(w[:1].upper() + w[1:] for w in words) or slug


def feature_template(slug: str) -> str:
    """The starting Markdown for ``autoforge local init <slug>``."""
    return FEATURE_TEMPLATE.format(title=slug_to_title(slug))


def init_feature_file(
    workspace: LocalWorkspace,
    slug: str,
    feature_dir: str = "features",
    overwrite: bool = False,
) -> Path:
    """Create ``<feature_dir>/<slug>.md`` from the template and return its path.

    Refuses to overwrite an existing file unless ``overwrite`` is set: the
    feature specification is the operator's own work, and clobbering it on a
    mistyped slug would destroy exactly the input a run depends on.

    The write goes through the working tree's :class:`~autoforge.safefs
    .SafeRoot`, which is what makes this safe rather than a fourth hand-rolled
    set of checks: no component of ``features/<slug>.md`` can be a symbolic
    link, ``--force`` *replaces the name* instead of truncating an inode (so a
    hard link planted at the target keeps its contents), and without
    ``--force`` the create is ``O_EXCL``, which is atomic rather than a
    check a racing writer can slip past.
    """
    if not SLUG_RE.match(slug):
        raise ConfigurationError(
            f"invalid feature slug {slug!r}: use lowercase letters, digits, '-', '_' or '.' "
            "(no path separators), e.g. 'add-transaction-filter'"
        )
    root = workspace.root()
    directory = Path(feature_dir)
    if directory.is_absolute():
        try:
            rel_dir = directory.relative_to(root)
        except ValueError:
            raise ConfigurationError(
                f"feature directory {feature_dir!r} is outside the repository {root}"
            ) from None
    else:
        rel_dir = directory
    rel = f"{rel_dir.as_posix()}/{slug}.md".lstrip("/")
    if rel.startswith("..") or "/../" in f"/{rel}":
        raise ConfigurationError(f"feature directory {feature_dir!r} escapes the repository {root}")
    content = feature_template(slug).encode("utf-8")
    with workspace.tree_root() as tree:
        if overwrite:
            existing = tree.lstat(rel)
            if existing is not None and not stat.S_ISREG(existing.st_mode):
                raise ConfigurationError(
                    f"refusing to overwrite {root / rel}: it is a "
                    f"{entry_kind(existing.st_mode)}, not a regular file. --force replaces "
                    "a specification AutoForge could have written; it does not write through "
                    "an entry the operator put there. Move it aside, or choose another slug."
                )
            tree.write_bytes(rel, content, mode=0o644)
        else:
            try:
                tree.create_exclusive(rel, content, mode=0o644)
            except FileExistsError:
                raise ConfigurationError(
                    f"feature specification already exists: {root / rel} — edit it, choose "
                    "another slug, or pass --force to overwrite it"
                ) from None
    return root / rel
