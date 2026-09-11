"""Independent verification of the local working tree (LOCAL mode).

In REMOTE mode GitHub is the source of truth: a `CONTROL_RESULT` claiming a
PR exists is checked against `gh`. LOCAL mode has no GitHub, so the working
tree itself is the source of truth and this module is the only place that
reads it. The engine never shells out to `git` directly.

Everything here is a *read*. The controller never commits, stages, resets,
checks out, stashes or discards anything: a local run leaves the operator's
work exactly where it found it, and an implementation may legitimately
consist of uncommitted (even untracked) files.

Every invocation goes through :mod:`autoforge.executor` with an argv list.
``shell=True`` is never used, so a path containing a space, a quote, ``;``
or ``$(...)`` is one opaque argument.

Workspace fingerprint
---------------------
:meth:`LocalWorkspace.status` returns a SHA-256 fingerprint that binds a
review to exactly the code that was reviewed. It covers:

- the current HEAD (or the fact that HEAD is unborn),
- the checked-out branch (or the fact that HEAD is detached),
- every path ``git status --porcelain=v1 -z --untracked-files=all`` reports
  (tracked modifications, staged modifications, deletions, renames and
  *untracked* files, which is where an implementation may well live), and
- the SHA-256 of each of those paths' current bytes.

Every reported path is hashed by *content*, whatever its size: a fingerprint
that fell back to ``(size, mtime)`` for large files would accept an
equal-sized replacement whose mtime was restored, and "the reviewer saw
exactly these bytes" is the one question this function exists to answer.

``git diff HEAD`` alone would miss untracked files entirely, so it is not
used. Paths under the configured state directory are excluded: AutoForge's
own logs and ``state.json`` change on every step and must not invalidate the
fingerprint. A state directory that *is* the repository root is rejected
rather than excluded, since excluding it would exclude the whole tree. The
feature specification is *not* excluded — it is covered by the
fingerprint and, independently, by its own SHA-256 (see
:func:`hash_bytes` and ``AutoForgeState.feature_spec_sha256``).

The fingerprint is deliberately not a general VCS abstraction: it answers
one question, "is this the same workspace the reviewer saw?".
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .errors import ConfigurationError, VerificationError
from .executor import ExecutionRequest, ExecutionResult, execute
from .state import is_runtime_artifact

Runner = Callable[[ExecutionRequest], ExecutionResult]

GIT_TIMEOUT_SECONDS = 60

# Bytes read per file when hashing working-tree content.
_CHUNK = 1024 * 1024

FEATURE_SPEC_SUFFIXES = (".md", ".markdown")

# A full object name as `git rev-parse` prints it (40 hex for SHA-1, 64 for a
# SHA-256 repository). Anything else from a successful read is not an anchor.
_OBJECT_NAME_RE = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")


def hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _unbindable(rel: str, why: str) -> str:
    """Message for a changed path the workspace fingerprint cannot bind."""
    return (
        f"the working-tree path {rel!r} changed but {why}. The workspace fingerprint binds "
        "a review to the exact bytes it saw, so a path it cannot hash makes the whole "
        "binding unprovable and the run stops here rather than reviewing a tree it can "
        "only partly describe."
    )


@dataclass(frozen=True)
class WorkspaceEntry:
    """One path `git status` reported, with the digest of its current bytes."""

    path: str
    code: str  # the two-character porcelain XY status code
    digest: str  # sha256 of the content, or a "<kind>:..." marker

    @property
    def is_untracked(self) -> bool:
        return self.code == "??"


@dataclass(frozen=True)
class WorkspaceStatus:
    root: Path
    head_sha: str  # "" when HEAD is unborn (a repository with no commit yet)
    # Checked-out branch, or "" when HEAD is detached. Together with
    # ``head_sha`` this is the *git anchor* a LOCAL run is pinned to: the
    # controller refuses to continue when either moves (see
    # ``AutoForgeState.base_head_sha`` / ``base_branch``).
    branch: str = ""
    entries: tuple[WorkspaceEntry, ...] = field(default_factory=tuple)
    fingerprint: str = ""

    @property
    def anchor(self) -> str:
        """Human-readable HEAD + branch, for error messages."""
        head = self.head_sha[:12] if self.head_sha else "(unborn)"
        return f"{head} on {self.branch or '(detached HEAD)'}"

    @property
    def is_clean(self) -> bool:
        return not self.entries

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(e.path for e in self.entries)

    @property
    def untracked_paths(self) -> tuple[str, ...]:
        return tuple(e.path for e in self.entries if e.is_untracked)

    @property
    def tracked_paths(self) -> tuple[str, ...]:
        return tuple(e.path for e in self.entries if not e.is_untracked)

    def describe(self, limit: int = 12) -> str:
        if not self.entries:
            return "(clean working tree)"
        shown = [f"{e.code} {e.path}" for e in self.entries[:limit]]
        if len(self.entries) > limit:
            shown.append(f"... and {len(self.entries) - limit} more")
        return ", ".join(shown)


class LocalWorkspace:
    """Read-only view of the git working tree a LOCAL run operates on."""

    def __init__(
        self,
        workdir: str | Path = ".",
        state_dir: str | Path = ".autoforge",
        runner: Runner | None = None,
        timeout_seconds: int = GIT_TIMEOUT_SECONDS,
    ) -> None:
        self.workdir = Path(workdir)
        self.state_dir = Path(state_dir)
        self._runner: Runner = runner or execute
        self.timeout = timeout_seconds
        self._root: Path | None = None

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

    def head_sha(self) -> str:
        """Current HEAD, or "" when the repository has no commit yet.

        ``rev-parse --verify --quiet`` rather than plain ``rev-parse``: it
        separates "this ref does not resolve" (exit 1, the unborn HEAD of a
        repository with no commit) from "the read failed" (exit 128: a damaged
        repository, a permissions change, a missing object store).  Both used
        to collapse into ``""``, which is also the legitimate value for an
        unborn HEAD — so a failing read looked exactly like "HEAD has not
        moved" to :meth:`ControllerEngine._git_anchor_drift`, and a run whose
        git identity could no longer be established continued instead of
        failing closed.
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

    # -- state-directory exclusion ----------------------------------------
    def state_dir_relpath(self) -> str | None:
        """Repository-relative path of the state directory, or None if outside.

        ``""`` means it resolves to the repository root itself, which is not a
        usable exclusion (see :meth:`check_state_dir`).
        """
        root = self.root()
        candidate = self.state_dir
        if not candidate.is_absolute():
            candidate = Path(self.workdir) / candidate
        try:
            rel = os.path.relpath(os.path.realpath(candidate), root)
        except ValueError:  # different drive (Windows); not inside the repo
            return None
        if rel.startswith("..") or os.path.isabs(rel):
            return None
        rel = os.path.normpath(rel).replace(os.sep, "/").strip("/")
        return "" if rel in ("", ".") else rel

    def check_state_dir(self) -> None:
        """Require an in-repository state directory to hold *only* runtime state.

        The fingerprint excludes everything under the state directory so
        AutoForge's own ``state.json`` and ``logs/`` cannot invalidate it.
        That exclusion is only safe while the directory holds nothing else,
        because an excluded path is a path no review is bound to.

        Two ways it can stop being safe, both refused here:

        *The state directory is the repository root.* There is no prefix to
        exclude — every path in the tree would have to be — so the
        controller's own writes start showing up as workspace changes: a
        read-only REVIEW appears to have modified the tree and the reviewer is
        rejected because the controller logged its invocation.

        *The state directory holds project content.* ``--state-dir src`` was
        accepted, and then every path under ``src/`` was excluded from the
        fingerprint: editing ``src/app.py`` left the fingerprint unchanged, so
        the controller would bind a review to a workspace digest that said
        nothing about the implementation, and accept code no reviewer ever
        saw. The directory must therefore contain only entries AutoForge
        itself writes (:func:`autoforge.state.is_runtime_artifact`).

        Excluding the individual runtime entries instead of the directory
        would not fix this: it would silently hide any ``state.json`` or
        ``logs/`` the *project* legitimately has, which is the same hole one
        level down. Refusing the configuration is the only answer that leaves
        nothing excluded that a reviewer needed to see.
        """
        rel = self.state_dir_relpath()
        if rel is None:
            return  # outside the repository: nothing is excluded at all
        root = self.root()
        if rel == "":
            raise ConfigurationError(
                f"the state directory resolves to the repository root ({root}); "
                "local mode fingerprints the working tree and excludes the state "
                "directory from it, which is impossible when the two are the same "
                "directory. Use a subdirectory such as '.autoforge' (the default), or a "
                "path outside the repository."
            )
        directory = root / rel
        try:
            names = sorted(entry.name for entry in os.scandir(directory))
        except FileNotFoundError:
            return  # not created yet: the run will create it and own it
        except NotADirectoryError:
            raise ConfigurationError(
                f"the state directory {directory} is not a directory; local mode needs a "
                "directory it owns for 'state.json' and 'logs/'"
            ) from None
        except OSError as exc:
            raise ConfigurationError(
                f"cannot inspect the state directory {directory}: {exc}"
            ) from exc
        foreign = [n for n in names if not is_runtime_artifact(n)]
        if foreign:
            listed = ", ".join(foreign[:10]) + ("..." if len(foreign) > 10 else "")
            raise ConfigurationError(
                f"the state directory {directory} is inside the repository and holds "
                f"content AutoForge did not write ({listed}); local mode excludes "
                f"everything under {rel!r} from the workspace fingerprint, so a state "
                "directory that also holds project content would hide exactly the changes "
                "a review must be bound to — an edit under it would leave the fingerprint "
                "unchanged and the controller would accept code no reviewer ever saw. Use "
                "a dedicated runtime directory such as '.autoforge' (the default), or a "
                "path outside the repository."
            )

    def _excluded_prefixes(self) -> tuple[str, ...]:
        """Repository-relative prefixes whose contents never affect the fingerprint."""
        self.check_state_dir()
        rel = self.state_dir_relpath()
        return (f"{rel}/",) if rel else ()

    # -- status + fingerprint ---------------------------------------------
    def status(self) -> WorkspaceStatus:
        """Read HEAD + working-tree status and compute the fingerprint."""
        root = self.root()
        head = self.head_sha()
        branch = self.branch()
        excluded = self._excluded_prefixes()
        entries: list[WorkspaceEntry] = []
        for code, path in self._porcelain():
            if any(path.startswith(prefix) for prefix in excluded):
                continue
            entries.append(
                WorkspaceEntry(path=path, code=code, digest=self._digest(root / path, path))
            )
        entries.sort(key=lambda e: e.path)
        return WorkspaceStatus(
            root=root,
            head_sha=head,
            branch=branch,
            entries=tuple(entries),
            fingerprint=self._fingerprint(head, branch, entries),
        )

    def _porcelain(self) -> list[tuple[str, str]]:
        """`git status --porcelain=v1 -z -uall` as (XY code, path) pairs.

        NUL-delimited output is parsed rather than the line-based form: a
        path containing a newline, a quote or a backslash is returned raw by
        ``-z`` and would otherwise be C-quoted and ambiguous. A rename entry
        ('R'/'C') is followed by a second record holding the *original* path;
        both ends are recorded, so moving a file cannot hide from the
        fingerprint.
        """
        res = self._git(["status", "--porcelain=v1", "-z", "--untracked-files=all"])
        raw = res.stdout or ""
        fields = raw.split("\0")
        out: list[tuple[str, str]] = []
        i = 0
        while i < len(fields):
            entry = fields[i]
            i += 1
            if not entry:
                continue
            if len(entry) < 4 or entry[2] != " ":
                # Not a status record we understand; recording it verbatim
                # keeps the fingerprint sensitive to it instead of dropping it.
                out.append(("??", entry))
                continue
            code, path = entry[:2], entry[3:]
            out.append((code, path))
            if code[0] in ("R", "C") and i < len(fields):
                original = fields[i]
                i += 1
                if original:
                    out.append((f"{code[0]}~", original))
        return out

    @staticmethod
    def _digest(path: Path, rel: str) -> str:
        """SHA-256 of a changed working-tree path. Fails closed, never guesses.

        Content is hashed regardless of file size. An earlier revision fell
        back to ``(size, mtime_ns)`` above a threshold to keep the cost
        bounded; that made the fingerprint metadata-bound rather than
        content-bound for exactly the files where a silent swap is easiest to
        hide, so a review could be accepted for bytes it never saw. The set of
        hashed paths is bounded by what ``git status`` reports as changed.

        The same argument rules out *stable markers* for paths that cannot be
        hashed. Returning ``"unreadable:PermissionError"`` or ``"dir"`` reads
        as a digest and compares equal to itself, so replacing an unreadable
        file's bytes — or changing everything inside a dirty submodule — left
        the fingerprint identical and the review still counted as bound. A
        marker that says "I could not look" is not evidence that nothing
        changed, so anything that cannot be content-bound raises instead.

        Absence is the one non-hash value kept: a deleted or renamed-away path
        is a fact ``git status`` reported and ``lstat`` confirms, not a failure
        to observe one.
        """
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            # A deleted (or renamed-away) path: absence is part of the state.
            return "absent"
        except OSError as exc:
            raise VerificationError(_unbindable(rel, f"cannot be inspected ({exc})")) from exc
        if stat.S_ISLNK(st.st_mode):
            try:
                target = os.readlink(path)
            except OSError as exc:
                raise VerificationError(
                    _unbindable(rel, f"is a symbolic link whose target cannot be read ({exc})")
                ) from exc
            return "symlink:" + hash_bytes(target.encode("utf-8", "surrogateescape"))
        if stat.S_ISDIR(st.st_mode):
            raise VerificationError(
                _unbindable(
                    rel,
                    "is a directory, whose contents no single digest can bind. `git status` "
                    "reports a bare directory for a dirty submodule and for an untracked "
                    "nested repository, and its contents can then change freely without the "
                    "workspace fingerprint moving. Commit or remove the nested repository, "
                    "or run the feature in a checkout without it: LOCAL mode reviews one "
                    "working tree and does not descend into submodules",
                )
            )
        if not stat.S_ISREG(st.st_mode):
            raise VerificationError(
                _unbindable(
                    rel,
                    f"is not a regular file (mode {st.st_mode:o}); a FIFO, socket or device "
                    "has no stable content for a review to be bound to",
                )
            )
        h = hashlib.sha256()
        try:
            with open(path, "rb") as fh:
                while chunk := fh.read(_CHUNK):
                    h.update(chunk)
        except OSError as exc:
            raise VerificationError(_unbindable(rel, f"cannot be read ({exc})")) from exc
        return h.hexdigest()

    @staticmethod
    def _fingerprint(head_sha: str, branch: str, entries: list[WorkspaceEntry]) -> str:
        h = hashlib.sha256()
        h.update(b"autoforge-workspace-v2\n")
        h.update(f"HEAD:{head_sha or '(unborn)'}\n".encode())
        h.update(f"BRANCH:{branch or '(detached)'}\n".encode("utf-8", "surrogateescape"))
        for e in sorted(entries, key=lambda x: x.path):
            h.update(e.code.encode("utf-8", "surrogateescape"))
            h.update(b"\0")
            h.update(e.path.encode("utf-8", "surrogateescape"))
            h.update(b"\0")
            h.update(e.digest.encode())
            h.update(b"\n")
        return h.hexdigest()


# -- feature specification ------------------------------------------------
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
    try:
        data = resolved.read_bytes()
    except OSError as exc:
        raise ConfigurationError(f"cannot read feature specification {resolved}: {exc}") from exc
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
    rel = os.path.relpath(resolved, workspace.root()).replace(os.sep, "/")
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


def _resolve_existing_parent(directory: Path) -> Path:
    """Resolve ``directory`` by resolving every component that already exists.

    ``realpath`` was previously applied to the target's parent only when that
    exact directory already existed. With ``features`` a symlink to somewhere
    outside the repository and a target of ``features/new/spec.md``,
    ``features/new`` does not exist, so the lexical (in-repository looking)
    path survived the repository-boundary check and ``mkdir(parents=True)``
    then followed the link and created the file outside the checkout.

    Walking up to the deepest component that *does* exist and resolving that
    removes the gap: every symlink on the path is resolved, and the components
    below it cannot be symlinks because they do not exist yet. The caller
    re-checks the parent after creating it, and writes with ``O_NOFOLLOW``.
    """
    missing: list[str] = []
    probe = directory
    while not os.path.lexists(probe):
        parent = probe.parent
        if parent == probe:  # reached the filesystem root
            break
        missing.append(probe.name)
        probe = parent
    resolved = Path(os.path.realpath(probe))
    for name in reversed(missing):
        resolved = resolved / name
    return resolved


def init_feature_file(
    workspace: LocalWorkspace,
    slug: str,
    feature_dir: str = "features",
    overwrite: bool = False,
) -> Path:
    """Create ``<feature_dir>/<slug>.md`` from the template and return its path.

    Refuses to overwrite an existing file unless ``overwrite`` is set: the
    feature specification is the operator's own work, and clobbering it on a
    mistyped slug would destroy exactly the input a run depends on. The file
    is deliberately *not* placed under the state directory — ``.autoforge/``
    stays runtime state, while a feature specification is project content the
    operator edits and may commit.
    """
    if not SLUG_RE.match(slug):
        raise ConfigurationError(
            f"invalid feature slug {slug!r}: use lowercase letters, digits, '-', '_' or '.' "
            "(no path separators), e.g. 'add-transaction-filter'"
        )
    root = workspace.root()
    directory = Path(feature_dir)
    target = (directory if directory.is_absolute() else root / directory) / f"{slug}.md"
    resolved = _resolve_existing_parent(target.parent) / target.name
    rel = os.path.relpath(resolved, root)
    if rel.startswith("..") or os.path.isabs(rel):
        raise ConfigurationError(
            f"feature directory {feature_dir!r} resolves to {resolved.parent}, which is "
            f"outside the repository {root}"
        )
    if os.path.lexists(resolved):
        if not overwrite:
            raise ConfigurationError(
                f"feature specification already exists: {resolved} — edit it, choose another "
                "slug, or pass --force to overwrite it"
            )
        if os.path.islink(resolved) or not resolved.is_file():
            raise ConfigurationError(f"refusing to overwrite {resolved}: it is not a regular file")
    try:
        resolved.parent.mkdir(parents=True, exist_ok=True)
        # Re-check what the parent really is now that it exists: the check
        # above described the path as it was, and the directories were created
        # since. Together with O_NOFOLLOW on the final component this closes
        # the window in which a symlink appears between the two.
        created = Path(os.path.realpath(resolved.parent))
        rel_after = os.path.relpath(created, root)
        if rel_after.startswith("..") or os.path.isabs(rel_after):
            raise ConfigurationError(
                f"feature directory {feature_dir!r} resolves to {created}, which is outside "
                f"the repository {root}; refusing to write there"
            )
        final = created / target.name
        # O_NOFOLLOW: never write *through* a symbolic link, whoever created
        # it. O_EXCL without --force makes "already exists" atomic rather than
        # a check that a racing writer can slip past.
        flags = os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        flags |= os.O_TRUNC if overwrite else os.O_EXCL
        fd = os.open(final, flags, 0o644)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(feature_template(slug))
    except FileExistsError:
        raise ConfigurationError(
            f"feature specification already exists: {resolved} — edit it, choose another "
            "slug, or pass --force to overwrite it"
        ) from None
    except OSError as exc:
        raise ConfigurationError(f"cannot create feature specification {resolved}: {exc}") from exc
    return final
