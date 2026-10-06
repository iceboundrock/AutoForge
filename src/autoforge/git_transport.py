"""The controller's own git transport (ADR 0004 D6.1-D6.4, D7.1-D7.5, D12.3).

In Wave 1 the agent can write everything the shared repository holds: its
configuration (``remote.origin.pushurl``, ``url.*.insteadOf``,
``core.sshCommand``, ``core.hooksPath``, ``include.path``), its hooks, its
refs (``refs/replace/*``), its graft and shallow files, the commit-graph in
the object store, and the object files themselves. This module is how the
controller still fetches, reads and pushes commits without any of that
steering it.

- **A controller-owned git directory per operation (D7.1).** Every fetch,
  push and commit read runs with ``GIT_DIR`` set to a fresh private
  directory (``tempfile.mkdtemp``, mode 0700, outside any repository) that
  this module writes itself (``HEAD``, ``refs/``, ``objects/`` and a
  ``config`` that turns off gc, maintenance and the commit-graph) and
  removes afterwards, whatever happened. ``GIT_OBJECT_DIRECTORY`` points at
  the shared object store, so fetched objects land where the worktrees read
  them and candidates are read from there. The object format comes from the
  SHAs involved (40 or 64 hex digits), never from the shared configuration.
  The directory has no hooks, no refs, no graft and no shallow file, so none
  of the four substitution mechanisms of D7.5 is reachable from it except
  the commit-graph, which ``core.commitGraph=false`` turns off.
- **No other configuration.** ``GIT_CONFIG_NOSYSTEM=1`` and
  ``GIT_CONFIG_GLOBAL=/dev/null``; the child starts from an explicit
  allow-list of the controller's environment that names no ``GIT_*``
  variable, so a ``GIT_CONFIG_COUNT``/``GIT_CONFIG_PARAMETERS``/``GIT_DIR``
  in the operator's shell never reaches it. Hooks, the file-system monitor,
  replacement objects, the commit-graph, automatic gc and maintenance and
  every transport but the remote's own scheme are switched off again at
  command-line precedence, so a key added to the private config by mistake
  could not turn one of them back on.
- **An explicit URL and the credential from ``gh`` (D7.1).** The remote is a
  :class:`GitRemote` URL (``https://github.com/<owner>/<repo>.git`` in
  production), never ``origin``; no ``pushurl``, ``insteadOf`` or SSH
  setting can apply because no repository configuration is read. The
  credential comes from the operator's existing ``gh`` login through
  ``gh auth git-credential``, named at command-line precedence after the
  helper list is reset, so it never appears in argv, a URL, a log or state.
- **A SHA reads as its own bytes (D7.5).** :meth:`GitTransport.read_commit`
  re-hashes what git returns in the SHA's own object format and refuses a
  mismatch; parents and messages come only from those authenticated bytes.
  :meth:`GitTransport.prove_range` computes the published range itself: it
  walks authenticated parent links from the candidate and stops only where
  GitHub's answer (``in_base_history``) says a commit is in the base's
  history, so nothing below the range is read and a rewritten ancestor of
  the base cannot shrink it. Every failure fails closed with
  :class:`GitTransportError`; nothing is ever read as "allowed".
- **Pushes are compare-and-swap fast-forwards only (D6.1-D6.3).** An exact
  SHA to an exact ``refs/heads/<branch>``, never the default branch, with a
  lease on the expected old value ("absent" or a full SHA) and the
  fast-forward proven here first, because a lease that holds still permits
  a non-fast-forward. No ``--force``, no ``+``, no delete.

Fetches write objects only (D6.4): no ref is created or moved anywhere, the
private directory included, so AutoForge owns no ref lifecycle.

Every process runs through the executor (``runner``); this module has no
workflow semantics beyond the rules above, and it never talks to the GitHub
API itself: GitHub's ancestry answers arrive through ``in_base_history``,
which the engine binds to a typed read of the GitHub client.
"""

from __future__ import annotations

import hashlib
import os
import re
import shlex
import shutil
import tempfile
from collections import deque
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from .errors import ExecutionError, GitTransportError
from .executor import ExecutionRequest, ExecutionResult, execute
from .redaction import redact

Runner = Callable[[ExecutionRequest], ExecutionResult]

# How many commits one range walk may read before it fails closed. A Wave 1
# published range is one issue's work plus whatever default-branch commits
# the candidate merged since the recorded base; a thousand is far above
# that. Each commit read costs one git process and one GitHub ancestry
# question, so the bound is also what bounds the walk's time and API use.
MAX_RANGE_COMMITS = 1000

# A network operation may move a large pack; a local object read is small.
NETWORK_TIMEOUT_SECONDS = 600
LOCAL_TIMEOUT_SECONDS = 120

# Hooks and the file-system monitor are off at command-line precedence for
# every controller git process (D7.3); replacement objects and the
# commit-graph too, so an object read by SHA reads that SHA (D7.5).
# ``premerge.py`` applies the same switches to its processes in the
# operator's repository.
LOCAL_GIT_SWITCHES: tuple[str, ...] = (
    "--no-replace-objects",
    "-c",
    "core.commitGraph=false",
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "core.fsmonitor=false",
)

# The environment a controller git process that holds no credential starts
# from (D7.3): ``PATH`` to find git, the locale, ``TMPDIR`` for git's
# temporary files, and ``HOME`` / ``XDG_CONFIG_HOME`` so a process in the
# operator's repository still reads the operator's global configuration
# (``safe.directory`` lives there). No ``GIT_*`` variable and no token: what
# such a process runs (a filter driver during the export) has no more
# authority than the agent.
LOCAL_GIT_ENV_ALLOWLIST: tuple[str, ...] = (
    "PATH",
    "LANG",
    "LANGUAGE",
    "LC_*",
    "TMPDIR",
    "HOME",
    "XDG_CONFIG_HOME",
)

# The environment of a network operation (D7.1): ``PATH`` and the locale;
# the standard proxy variables (curl honours both spellings) and CA
# locations, so the operator's network setup still works; and exactly what
# ``gh auth git-credential`` needs to find the operator's existing ``gh``
# login: ``HOME`` / ``XDG_CONFIG_HOME`` / ``GH_CONFIG_DIR`` for its
# configuration, ``XDG_RUNTIME_DIR`` / ``DBUS_SESSION_BUS_ADDRESS`` for the
# system keyring, and the token variables ``gh`` reads when the operator
# authenticates it that way. No ``GIT_*`` variable: git's own configuration
# and object locations are set by this module alone.
NETWORK_GIT_ENV_ALLOWLIST: tuple[str, ...] = (
    "PATH",
    "LANG",
    "LANGUAGE",
    "LC_*",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
    "all_proxy",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "CURL_CA_BUNDLE",
    "HOME",
    "XDG_CONFIG_HOME",
    "XDG_RUNTIME_DIR",
    "DBUS_SESSION_BUS_ADDRESS",
    "GH_CONFIG_DIR",
    "GH_HOST",
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "GH_ENTERPRISE_TOKEN",
    "GITHUB_ENTERPRISE_TOKEN",
)

_SHA1_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
# GitHub owner and repository names: letters, digits, ``_``, ``.``, ``-``.
_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
# One ref path component this module accepts: a deliberately small subset of
# what ``git check-ref-format`` allows. A name outside it is refused (fails
# closed), never normalised.
_REF_COMPONENT_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]*$")
_MAX_REF_LENGTH = 255
# Parsing ``https://host/path`` and ``file:///path``. No userinfo, port,
# query or fragment: a URL carries no credential and nothing git could read
# as an option.
_HTTPS_URL_RE = re.compile(r"^https://[A-Za-z0-9.-]+(?:/[A-Za-z0-9_.~-]+)+$")
_FILE_URL_RE = re.compile(r"^file:///[^\s?#@]*$")
_GITHUB_HOST = "github.com"

_DETAIL_CHARS = 500


def _object_format(sha: str) -> str:
    """``sha1`` or ``sha256`` for a full lowercase hex id; anything else is refused."""
    if _SHA1_RE.match(sha):
        return "sha1"
    if _SHA256_RE.match(sha):
        return "sha256"
    raise GitTransportError(f"{sha!r} is not a full lowercase hexadecimal object id")


def _format_of(shas: Sequence[str], default: str) -> str:
    """The one object format the SHAs share, or ``default`` when there are none."""
    formats = {_object_format(sha) for sha in shas}
    if len(formats) > 1:
        raise GitTransportError("object ids of two different formats in one operation")
    return formats.pop() if formats else default


def _valid_ref_path(name: str) -> bool:
    """Whether ``name`` is ``component(/component)*`` in the accepted subset.

    No ``..``, no component starting with ``.`` or ending with ``.`` or
    ``.lock``, no empty component, no character outside
    ``[A-Za-z0-9._-]``, at most :data:`_MAX_REF_LENGTH` characters.
    """
    if not name or len(name) > _MAX_REF_LENGTH or ".." in name:
        return False
    for component in name.split("/"):
        if not _REF_COMPONENT_RE.match(component):
            return False
        if component.endswith(".") or component.endswith(".lock"):
            return False
    return True


def valid_branch_name(branch: str) -> bool:
    """Whether ``branch`` is a plain branch name this transport will push to.

    A short name (``autoforge/160``), never a full ref (``refs/...``), never
    ``HEAD``; see :func:`_valid_ref_path` for the character rules.
    """
    if branch.startswith("refs/") or branch == "HEAD":
        return False
    return _valid_ref_path(branch)


def _valid_fetch_revision(revision: str) -> bool:
    """A full object id, or a full ref name (``refs/pull/7/head``).

    Never a refspec: no ``:`` (a destination), no leading ``+`` (a force),
    no ``^``/``~``/glob, nothing that starts with ``-``.
    """
    if _SHA1_RE.match(revision) or _SHA256_RE.match(revision):
        return True
    return revision.startswith("refs/") and _valid_ref_path(revision)


def _tail(result: ExecutionResult) -> str:
    """The end of a failed process's output, redacted, for an error message."""
    text = (result.stderr or result.stdout or "").strip()
    return redact(text[-_DETAIL_CHARS:])


@dataclass(frozen=True)
class GitRemote:
    """The one URL a network operation talks to; never a remote name.

    ``https://`` in production (built with :meth:`https`); ``file://`` for a
    local bare repository (tests). No userinfo is accepted, so a credential
    can never travel in the URL; the credential comes from ``gh``.
    """

    url: str

    def __post_init__(self) -> None:
        if not (_HTTPS_URL_RE.match(self.url) or _FILE_URL_RE.match(self.url)):
            raise GitTransportError(
                "a git remote must be an https:// URL with no credential, port, query or "
                f"fragment, or a file:/// URL; got {redact(self.url)!r}"
            )

    @property
    def scheme(self) -> str:
        """The URL's scheme, which is the one transport git is allowed to use."""
        return self.url.split("://", 1)[0]

    @classmethod
    def https(cls, repository: str) -> GitRemote:
        """``https://github.com/<owner>/<repo>.git`` for a verified ``owner/repo``."""
        parts = repository.split("/")
        if len(parts) != 2 or not all(
            _NAME_RE.match(part) and part not in (".", "..") for part in parts
        ):
            raise GitTransportError(f"{repository!r} is not an owner/repo repository name")
        owner, repo = parts
        return cls(url=f"https://{_GITHUB_HOST}/{owner}/{repo}.git")


@dataclass(frozen=True)
class CommitRecord:
    """A commit read from bytes that hash to its id (D7.5)."""

    sha: str
    parents: tuple[str, ...]
    # Decoded from the authenticated bytes (UTF-8, invalid bytes replaced).
    message: str


@dataclass(frozen=True)
class RangeProof:
    """The published range ``base..candidate`` as the walk of D7.5 proved it."""

    base: str
    candidate: str
    # Every commit the walk included, in walk (breadth-first) order: those
    # reachable from the candidate through authenticated parent links that
    # are neither the base nor reported by GitHub in the base's history.
    commits: tuple[CommitRecord, ...]
    # True iff the walk reached ``base`` itself: the candidate descends from
    # (or is) the base. A candidate equal to the base has an empty range.
    reached_base: bool


class PushOutcome(Enum):
    """What one push attempt established about the remote ref.

    Only the read-back through the GitHub client is authoritative for the
    effect record; this is what git reported, classified.
    """

    # The remote accepted the update: a new branch, or a fast-forward.
    PUSHED = "pushed"
    # The remote ref already pointed at the candidate; nothing was sent. Git
    # reports this before it checks the lease, so it is the outcome of a
    # push that already landed (a save lost after the push) whatever the
    # expected old value was.
    UP_TO_DATE = "up_to_date"
    # Definitive: the remote ref was not at the expected old value (a human
    # pushed meanwhile, or the branch exists when it was expected absent),
    # so git refused before updating anything.
    LEASE_REJECTED = "lease_rejected"
    # Definitive: the remote refused the update for its own reason (branch
    # protection, a server-side hook); ``detail`` carries git's reason.
    REMOTE_REJECTED = "remote_rejected"
    # Ambiguous: a timeout, a transport failure, a failed capture or output
    # this module cannot classify. The push may or may not have landed. The
    # caller reconciles by reading the remote ref, never by pushing again
    # blindly.
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class PushResult:
    outcome: PushOutcome
    branch: str
    sha: str
    expected_old: str | None
    # One redacted, bounded line for a log or a BLOCKED reason; never
    # authoritative and never parsed.
    detail: str


def published_range_problem(
    proof: RangeProof, message_problem: Callable[[str], str | None]
) -> str | None:
    """Run the commit-message policy over every commit of a proven range (D8.5).

    ``message_problem`` is the caller's policy (closing keywords naming
    another issue, credential-shaped strings); it returns a reason or None.
    The first refusal is returned naming the commit's SHA, never quoting the
    message, so the reason must not quote it either. Whether the candidate
    descends from the base is :attr:`RangeProof.reached_base`, which the
    caller checks separately.
    """
    for commit in proof.commits:
        problem = message_problem(commit.message)
        if problem:
            return (
                f"commit {commit.sha} in the published range {proof.base}..{proof.candidate}: "
                f"{problem}"
            )
    return None


class GitTransport:
    """Fetch, read and push commits in a controller-owned git context.

    ``object_directory`` is the shared object store (``<git common
    dir>/objects``). ``in_base_history(base, sha)`` is GitHub's answer to
    whether ``sha`` is in ``base``'s history (``behind`` or ``identical``
    when comparing ``base...sha``; False for ``ahead``, ``diverged`` and a
    commit GitHub does not hold); any exception it raises fails the walk
    closed. ``object_format`` is used only when an operation names no SHA
    (a fetch of refs alone); otherwise the SHAs' own format is used.
    """

    def __init__(
        self,
        *,
        object_directory: Path,
        remote: GitRemote,
        gh_command: str = "gh",
        in_base_history: Callable[[str, str], bool],
        runner: Runner | None = None,
        max_range_commits: int = MAX_RANGE_COMMITS,
        object_format: str = "sha1",
    ) -> None:
        if max_range_commits < 1:
            raise GitTransportError(f"max_range_commits must be >= 1, got {max_range_commits}")
        if object_format not in ("sha1", "sha256"):
            raise GitTransportError(f"unknown object format {object_format!r}")
        self._objects = Path(object_directory).resolve()
        self._remote = remote
        self._gh_command = gh_command
        self._in_base_history = in_base_history
        self._runner: Runner = runner or execute
        self._max_range_commits = max_range_commits
        self._default_format = object_format

    @property
    def remote(self) -> GitRemote:
        return self._remote

    # -- the per-operation context ------------------------------------------------------
    @contextmanager
    def _git_dir(self, object_format: str) -> Iterator[Path]:
        """A fresh minimal git directory for one operation, removed afterwards.

        Written here, not by ``git init``, so no template (and no template
        hook) is copied in; it has no hooks directory at all.
        """
        try:
            root = Path(tempfile.mkdtemp(prefix="autoforge-git-"))
        except OSError as exc:
            raise GitTransportError(f"cannot create a private git directory: {exc}") from exc
        try:
            (root / "refs").mkdir()
            (root / "objects").mkdir()
            (root / "HEAD").write_text("ref: refs/heads/autoforge-unborn\n", encoding="ascii")
            (root / "config").write_text(_private_config(object_format), encoding="ascii")
            yield root
        except OSError as exc:
            raise GitTransportError(f"cannot prepare a private git directory: {exc}") from exc
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def _gh_path(self) -> str:
        """The absolute path of the controller's ``gh``; refused when absent."""
        found = shutil.which(self._gh_command)
        if found is None:
            raise GitTransportError(
                f"the GitHub CLI {self._gh_command!r} was not found; the controller's git "
                "transport takes its credential from gh"
            )
        return os.path.abspath(found)

    def _request(
        self,
        git_dir: Path,
        args: Sequence[str],
        *,
        gh_path: str | None,
        stdin: bytes | None = None,
    ) -> ExecutionRequest:
        """One git process in ``git_dir``; a network one when ``gh_path`` is given."""
        # No promisor remote can be configured in the private directory, so a
        # missing object is never fetched lazily behind this module's back.
        switches = [*LOCAL_GIT_SWITCHES, "-c", "gc.auto=0", "-c", "maintenance.auto=false"]
        switches += ["-c", "protocol.allow=never"]
        if gh_path is not None:
            switches += ["-c", f"protocol.{self._remote.scheme}.allow=always"]
            # Reset the helper list, then name gh's helper. Git runs a ``!``
            # helper through the shell, hence the quoting of the path.
            switches += [
                "-c",
                "credential.helper=",
                "-c",
                f"credential.helper=!{shlex.quote(gh_path)} auth git-credential",
                "-c",
                "credential.interactive=false",
            ]
        env = {
            "GIT_DIR": str(git_dir),
            "GIT_OBJECT_DIRECTORY": str(self._objects),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_TERMINAL_PROMPT": "0",
        }
        return ExecutionRequest(
            command=["git", *switches, *args],
            cwd=str(git_dir),
            env=env,
            env_allowlist=(
                NETWORK_GIT_ENV_ALLOWLIST if gh_path is not None else LOCAL_GIT_ENV_ALLOWLIST
            ),
            timeout_seconds=NETWORK_TIMEOUT_SECONDS
            if gh_path is not None
            else LOCAL_TIMEOUT_SECONDS,
            stdin_data=stdin,
        )

    def _run(self, request: ExecutionRequest, what: str) -> ExecutionResult:
        try:
            return self._runner(request)
        except ExecutionError as exc:
            raise GitTransportError(f"{what}: {redact(str(exc))}") from exc

    # -- fetch ---------------------------------------------------------------------------
    def fetch(self, revisions: Sequence[str]) -> None:
        """Fetch the objects of ``revisions`` into the shared store; no ref anywhere.

        Each revision is a full SHA or a full ref name (``refs/pull/7/head``,
        ``refs/heads/main``). No destination is given, tags are not followed,
        ``FETCH_HEAD`` is not written and submodules are not touched, so no
        ref is created or moved, the private directory's included (D6.4).
        """
        if isinstance(revisions, str):
            raise GitTransportError("fetch takes a sequence of revisions, not one string")
        wanted = list(revisions)
        if not wanted:
            raise GitTransportError("nothing to fetch")
        for revision in wanted:
            if not _valid_fetch_revision(revision):
                raise GitTransportError(f"{revision!r} is not a full object id or ref name")
        shas = [r for r in wanted if not r.startswith("refs/")]
        object_format = _format_of(shas, self._default_format)
        gh_path = self._gh_path()
        shown = ", ".join(wanted)
        with self._git_dir(object_format) as git_dir:
            request = self._request(
                git_dir,
                [
                    "fetch",
                    "--no-tags",
                    "--no-write-fetch-head",
                    "--no-recurse-submodules",
                    "--no-auto-gc",
                    "--no-write-commit-graph",
                    "--quiet",
                    self._remote.url,
                    *wanted,
                ],
                gh_path=gh_path,
            )
            result = self._run(request, f"fetching {shown} from {self._remote.url} failed")
        if result.timed_out:
            raise GitTransportError(
                f"fetching {shown} from {self._remote.url} timed out after "
                f"{request.timeout_seconds}s"
            )
        if result.exit_code != 0:
            raise GitTransportError(
                f"fetching {shown} from {self._remote.url} failed (exit {result.exit_code}): "
                f"{_tail(result)}"
            )
        if result.truncated:
            raise GitTransportError(
                f"fetching {shown}: git's output was truncated at the executor's capture bound"
            )

    # -- object reads --------------------------------------------------------------------
    def _batch(self, sha: str, mode: str) -> ExecutionResult:
        """``git cat-file --batch`` / ``--batch-check`` for one SHA, in its own directory."""
        with self._git_dir(_object_format(sha)) as git_dir:
            request = self._request(
                git_dir, ["cat-file", mode], gh_path=None, stdin=f"{sha}\n".encode("ascii")
            )
            result = self._run(request, f"reading object {sha} failed")
        if result.timed_out:
            raise GitTransportError(f"reading object {sha} timed out")
        if result.truncated:
            raise GitTransportError(
                f"reading object {sha}: git's output was truncated at the capture bound"
            )
        return result

    def has_commit(self, sha: str) -> bool:
        """Whether the shared store holds a commit under ``sha``, with no substitution.

        No replacement ref, graft or commit-graph can make a missing commit
        present: the check runs in a private directory with replacement
        objects and the commit-graph off. Existence only; the bytes are
        authenticated by :meth:`read_commit`.
        """
        result = self._batch(sha, "--batch-check")
        if result.exit_code != 0:
            raise GitTransportError(
                f"checking for commit {sha} failed (exit {result.exit_code}): {_tail(result)}"
            )
        header = result.stdout.rstrip("\n")
        if header == f"{sha} missing":
            return False
        parts = header.split(" ")
        if len(parts) != 3 or parts[0] != sha:
            raise GitTransportError(f"unexpected reply checking for commit {sha}")
        return parts[1] == "commit"

    def _read_once(self, sha: str) -> CommitRecord | None:
        """The authenticated commit, or None when the store does not hold ``sha``."""
        result = self._batch(sha, "--batch")
        if result.exit_code != 0:
            # A loose object git cannot even inflate, for example. Not
            # "missing": it is not re-fetched, and it never reads as allowed.
            raise GitTransportError(
                f"reading commit {sha} failed (exit {result.exit_code}): {_tail(result)}"
            )
        header, newline, body = result.stdout.partition("\n")
        if header == f"{sha} missing":
            return None
        parts = header.split(" ")
        if not newline or len(parts) != 3 or parts[0] != sha or not parts[2].isdigit():
            raise GitTransportError(f"unexpected reply reading commit {sha}")
        if parts[1] != "commit":
            raise GitTransportError(f"object {sha} is a {parts[1]}, not a commit")
        size = int(parts[2])
        # The executor captures text (UTF-8, invalid bytes replaced). Valid
        # UTF-8 round-trips exactly; anything else changes length or hash and
        # is refused below, never authenticated from a lossy copy.
        data = body.encode("utf-8")
        raw = data[:size]
        lossy = "�" in body
        if len(data) != size + 1 or data[size:] != b"\n":
            if lossy:
                raise GitTransportError(_lossy(sha))
            raise GitTransportError(f"commit {sha}: git returned {len(data) - 1} bytes, not {size}")
        if _hash_commit(raw, _object_format(sha)) != sha:
            if lossy:
                raise GitTransportError(_lossy(sha))
            raise GitTransportError(
                f"commit {sha}: the stored bytes do not hash to its id (a rewritten or "
                "substituted object); it is not used"
            )
        return _parse_commit(sha, raw)

    def read_commit(self, sha: str) -> CommitRecord:
        """Read ``sha`` as its own bytes: re-hashed, parsed, never substituted.

        A commit the store does not hold is fetched once by SHA (D6.4) and
        read again; still missing is a :class:`GitTransportError`, never
        "not an ancestor, so allowed". An id mismatch is not re-fetched.
        """
        _object_format(sha)
        record = self._read_once(sha)
        if record is not None:
            return record
        try:
            self.fetch([sha])
        except GitTransportError as exc:
            raise GitTransportError(
                f"commit {sha} is not in the object store and fetching it again failed: {exc}"
            ) from exc
        record = self._read_once(sha)
        if record is None:
            raise GitTransportError(f"commit {sha} is still missing after fetching it again")
        return record

    # -- the range walk ------------------------------------------------------------------
    def _ask_in_base_history(self, base: str, sha: str) -> bool:
        try:
            answer = self._in_base_history(base, sha)
        except Exception as exc:  # any failure of the answer fails closed
            raise GitTransportError(
                f"GitHub's answer to whether {sha} is in the history of {base} is unavailable "
                f"({type(exc).__name__}: {redact(str(exc))[:_DETAIL_CHARS]}); the range is not "
                "proven"
            ) from exc
        if answer is True or answer is False:
            return answer
        raise GitTransportError(
            f"GitHub's answer to whether {sha} is in the history of {base} is not a boolean"
        )

    def prove_range(self, base: str, candidate: str) -> RangeProof:
        """Prove the published range ``base..candidate`` (D7.5).

        Breadth-first from the candidate. A commit reached is the base (stop
        there, leave it out, :attr:`RangeProof.reached_base`), or one GitHub
        reports in the base's history (stop there, leave it out, do not read
        it or its parents), or else part of the range: it is read as its own
        bytes and its authenticated parents are walked. Nothing below an
        excluded commit is read, so no rewritten ancestor of the base can
        hide a commit from the range. Reading more than ``max_range_commits``
        commits, an id mismatch, a commit still missing after a re-fetch and
        an unavailable GitHub answer each raise :class:`GitTransportError`.
        """
        if _object_format(base) != _object_format(candidate):
            raise GitTransportError(
                f"the base {base} and the candidate {candidate} use different object formats"
            )
        commits: list[CommitRecord] = []
        reached_base = False
        seen = {candidate}
        pending = deque([candidate])
        while pending:
            sha = pending.popleft()
            if sha == base:
                reached_base = True
                continue
            if self._ask_in_base_history(base, sha):
                continue
            if len(commits) >= self._max_range_commits:
                raise GitTransportError(
                    f"the range {base}..{candidate} holds more than {self._max_range_commits} "
                    "commits; it is not proven"
                )
            record = self.read_commit(sha)
            commits.append(record)
            for parent in record.parents:
                if parent not in seen:
                    seen.add(parent)
                    pending.append(parent)
        return RangeProof(
            base=base, candidate=candidate, commits=tuple(commits), reached_base=reached_base
        )

    def descends_from(self, candidate: str, ancestor: str) -> bool:
        """Whether ``candidate`` is ``ancestor`` or descends from it (D7.5).

        True only when the walk for ``ancestor..candidate`` reaches
        ``ancestor``; a walk that cannot be completed raises instead.
        """
        return self.prove_range(ancestor, candidate).reached_base

    # -- push ----------------------------------------------------------------------------
    def push(
        self, *, sha: str, branch: str, expected_old: str | None, default_branch: str
    ) -> PushResult:
        """Compare-and-swap ``refs/heads/<branch>`` from ``expected_old`` to ``sha``.

        Refused with :class:`GitTransportError` before any process when the
        branch is the default branch (compared case-insensitively) or not a
        plain branch name, or a SHA is not a full id. Then the candidate is
        read as its own bytes and, when ``expected_old`` is given, proven to
        descend from it (D6.2): the lease alone would permit a
        non-fast-forward. ``expected_old=None`` means the branch must not
        exist. A raised :class:`GitTransportError` therefore means no push
        was attempted; once git runs, the outcome is returned, ambiguity
        included (:attr:`PushOutcome.UNKNOWN`).

        That the candidate descends from the recorded base and differs from
        it is the caller's precondition (:meth:`prove_range`).
        """
        _object_format(sha)
        if expected_old is not None and _object_format(expected_old) != _object_format(sha):
            raise GitTransportError(
                f"the expected old value {expected_old!r} and the candidate {sha} use "
                "different object formats"
            )
        if not default_branch:
            raise GitTransportError("the default branch must be named to refuse a push to it")
        if not valid_branch_name(branch):
            raise GitTransportError(f"{branch!r} is not a plain branch name this transport pushes")
        if branch.lower() == default_branch.lower():
            raise GitTransportError(
                f"refusing to push to {branch!r}: it is the default branch, which changes only "
                "through a reviewed PR"
            )
        gh_path = self._gh_path()
        self.read_commit(sha)
        if expected_old is not None and not self.descends_from(sha, expected_old):
            raise GitTransportError(
                f"refusing to push {sha} to {branch!r}: it does not descend from the branch's "
                f"expected head {expected_old}, so the update would not be a fast-forward"
            )
        ref = f"refs/heads/{branch}"
        lease = f"--force-with-lease={ref}:{expected_old or ''}"

        def result(outcome: PushOutcome, detail: str) -> PushResult:
            return PushResult(
                outcome=outcome,
                branch=branch,
                sha=sha,
                expected_old=expected_old,
                detail=redact(detail)[:_DETAIL_CHARS],
            )

        with self._git_dir(_object_format(sha)) as git_dir:
            request = self._request(
                git_dir,
                [
                    "push",
                    "--porcelain",
                    "--no-verify",
                    "--no-follow-tags",
                    "--recurse-submodules=no",
                    lease,
                    self._remote.url,
                    f"{sha}:{ref}",
                ],
                gh_path=gh_path,
            )
            try:
                res = self._runner(request)
            except ExecutionError as exc:
                # It may have failed after git sent the update.
                return result(PushOutcome.UNKNOWN, f"git push did not complete: {exc}")
        if res.timed_out:
            return result(
                PushOutcome.UNKNOWN,
                f"git push timed out after {request.timeout_seconds}s; whether the remote ref "
                "moved is unknown",
            )
        if res.truncated:
            return result(PushOutcome.UNKNOWN, "git push output was truncated")
        outcome, detail = _classify_push(res, ref)
        return result(outcome, detail)


def _private_config(object_format: str) -> str:
    """The whole configuration of a private git directory."""
    lines = [
        "[core]",
        f"\trepositoryformatversion = {1 if object_format == 'sha256' else 0}",
        "\tbare = true",
        "\tcommitGraph = false",
        "\tlogAllRefUpdates = false",
        "[gc]",
        "\tauto = 0",
        "[maintenance]",
        "\tauto = false",
        "[fetch]",
        "\twriteCommitGraph = false",
    ]
    if object_format == "sha256":
        lines += ["[extensions]", "\tobjectFormat = sha256"]
    return "\n".join(lines) + "\n"


def _hash_commit(raw: bytes, object_format: str) -> str:
    """The object id of a commit with content ``raw``."""
    algorithm = hashlib.sha256 if object_format == "sha256" else hashlib.sha1
    return algorithm(b"commit %d\0" % len(raw) + raw).hexdigest()


def _lossy(sha: str) -> str:
    return (
        f"commit {sha} is not valid UTF-8, so its bytes cannot be authenticated through the "
        "executor's text capture; it is not used"
    )


def _parse_commit(sha: str, raw: bytes) -> CommitRecord:
    """Parents and message from authenticated commit bytes, exactly as git reads them.

    Git takes the parents only from the ``parent`` lines directly after the
    ``tree`` line; a ``parent`` header anywhere else is ignored by git and
    GitHub, so it is refused here rather than believed (believing it could
    prove an ancestry no one else sees).
    """
    header, _, message = raw.partition(b"\n\n")
    lines = header.split(b"\n")
    width = len(sha)
    if not lines[0].startswith(b"tree ") or len(lines[0]) != 5 + width:
        raise GitTransportError(f"commit {sha} does not start with a tree line")
    parents: list[str] = []
    index = 1
    while index < len(lines) and lines[index].startswith(b"parent "):
        parent = lines[index][7:].decode("ascii", errors="replace")
        if len(parent) != width or _object_format(parent) != _object_format(sha):
            raise GitTransportError(f"commit {sha} names a malformed parent")
        parents.append(parent)
        index += 1
    if any(line.startswith(b"parent ") for line in lines[index:]):
        raise GitTransportError(f"commit {sha} has a parent header out of place; it is not used")
    return CommitRecord(
        sha=sha,
        parents=tuple(parents),
        message=message.decode("utf-8", errors="replace"),
    )


_PORCELAIN_REJECTED = "[rejected]"
_PORCELAIN_REMOTE_REJECTED = "[remote rejected]"


def _classify_push(res: ExecutionResult, ref: str) -> tuple[PushOutcome, str]:
    """Read git's ``--porcelain`` status line for ``ref``.

    ``<flag>\\t<from>:<to>\\t<summary>``: ``*`` new ref, `` `` fast-forward,
    ``+`` forced (cannot happen once the fast-forward is proven, and it would
    still mean the ref moved), ``=`` up to date, ``!`` rejected. The status
    words are not translated by git. A success needs exit 0 as well; any
    other combination is :attr:`PushOutcome.UNKNOWN`.
    """
    line = None
    for candidate in res.stdout.splitlines():
        fields = candidate.split("\t")
        if len(fields) >= 3 and fields[1].rpartition(":")[2] == ref:
            line = fields
            break
    if line is None:
        return PushOutcome.UNKNOWN, (
            f"git push reported no status for {ref} (exit {res.exit_code}): {_tail(res)}"
        )
    flag, summary = line[0], "\t".join(line[2:])
    if flag in ("*", " ", "+") and res.exit_code == 0:
        return PushOutcome.PUSHED, summary
    if flag == "=" and res.exit_code == 0:
        return PushOutcome.UP_TO_DATE, summary
    if flag == "!" and summary.startswith(_PORCELAIN_REJECTED):
        return PushOutcome.LEASE_REJECTED, summary
    if flag == "!" and summary.startswith(_PORCELAIN_REMOTE_REJECTED):
        return PushOutcome.REMOTE_REJECTED, summary
    return PushOutcome.UNKNOWN, f"{summary} (exit {res.exit_code})"
