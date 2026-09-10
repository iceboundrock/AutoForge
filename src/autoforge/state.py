"""Persistent controller state with atomic file persistence.

State file:  ``<state_dir>/state.json``      (default ``.autoforge/state.json``)
Run logs:    ``<state_dir>/logs/<run-id>/``
Lock file:   ``<git common dir>/autoforge/controller.lock`` -- keyed by the
             repository, not by ``state_dir``; see :mod:`autoforge.locking`

Saves are atomic (temp file in the same directory + fsync + os.replace) so
a crash mid-write never leaves a half-written JSON file.  A corrupted state
file (unparseable, wrong protocol, invalid UTF-8, a dangling symlink, or a
non-regular entry such as a FIFO, socket, device or directory) raises
StateError with a meaningful message and is never silently overwritten with
a fresh state: ``run`` refuses (exit 2) unless ``--force`` is given, and
even then the unreadable entry is moved aside as
``state.json.corrupt-<timestamp>`` by :func:`quarantine_state_file` rather
than deleted (a directory entry cannot be archived automatically and stays
put with an error).  The entry is inspected with ``lstat``/``fstat`` and
opened non-blocking before it is read, so a FIFO without a writer fails
loudly instead of hanging the command.  ``run`` inspects, decides, quarantines, writes the first
state and executes it under one continuous controller lock (``step`` and
``resume`` load and execute under it likewise), so the verdict on an
existing entry is never taken from a view another controller may have
changed since, and no second controller can take over between the first
save and the execution.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from . import __prompt_version__, __protocol_version__, __version__
from .errors import StateError
from .loop_guard import validate_review_history
from .transitions import Phase

STATE_FILENAME = "state.json"
LOGS_DIRNAME = "logs"


def utcnow_iso() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class AutoForgeState:
    protocol_version: str = __protocol_version__
    controller_version: str = __version__
    prompt_version: str = __prompt_version__

    run_id: str = ""
    repository: str = ""
    epic_url: str = ""

    current_issue_url: str = ""
    current_pr_url: str = ""
    current_branch: str = ""

    phase: Phase = Phase.INITIALIZING

    # Completed review rounds for the current PR; the upcoming round is +1.
    review_round: int = 0

    # SHA bound to the last completed review (what the reviewer actually saw).
    reviewed_head_sha: str = ""
    # Latest PR HEAD the controller observed through `gh`.
    current_head_sha: str = ""

    last_review_comment_url: str = ""
    last_review_result: str = ""  # "needs_fix" | "clean" | "stale" | ""
    last_review_needs_fix: bool | None = None
    # Findings from the last review that still require a FIX round
    # (each: {"id","classification","required_resolution",...}).
    open_findings: list[dict] = field(default_factory=list)
    # Resolutions reported by the last FIX round (verified by the controller).
    last_fix_resolutions: list[dict] = field(default_factory=list)
    # One entry per completed review round of the current PR (see
    # loop_guard.review_record): round, reviewed_head_sha, result
    # (needs_fix | clean | stale), finding_count, fingerprint (digest of the
    # round's normalised required_resolution texts), resolutions (one digest
    # per distinct non-empty normalised required_resolution, clipped to
    # loop_guard.MAX_PERSISTED_RESOLUTION_DIGESTS) and resolutions_truncated
    # (True when that clip dropped digests). No review text is persisted.
    # An entry written before per-finding digests existed has no
    # 'resolutions' key at all and keeps the count-only stagnation rule; a
    # present but malformed field is corruption and fails loudly on load.
    # Drives the review-round cap and stagnation detection; cleared per PR.
    review_history: list[dict] = field(default_factory=list)

    merged_since_epic_update: int = 0
    counted_merged_prs: list[str] = field(default_factory=list)
    # Reasons the controller rejected the UPDATE_EPIC agent's next_issue_url
    # (bounded; the last one is rendered into the retry prompt). Cleared once
    # a next issue verified or the EPIC completed.
    next_issue_rejections: list[str] = field(default_factory=list)

    # Agent invocations attempted for the current phase (reset on transition).
    attempt: int = 0
    # Total executed steps across the run (never reset).
    step_count: int = 0
    # Human-readable reason when phase is BLOCKED/FAILED.
    block_reason: str = ""

    created_at: str = ""
    updated_at: str = ""

    # -- serialization -------------------------------------------------
    def to_dict(self) -> dict:
        d = asdict(self)
        d["phase"] = self.phase.value
        return d

    @classmethod
    def from_dict(cls, data: dict) -> AutoForgeState:
        if not isinstance(data, dict):
            raise StateError("state file must contain a JSON object")
        try:
            phase = Phase(data["phase"])
        except KeyError:
            raise StateError("state file missing required field 'phase'") from None
        except ValueError:
            raise StateError(f"state file has unknown phase {data.get('phase')!r}") from None
        kwargs = dict(data)
        kwargs["phase"] = phase
        # Drop unknown future fields defensively? No — fail loudly on wrong
        # types but ignore nothing: keep only known dataclass fields so that
        # hand-edited files with typos surface via required-field checks.
        known = {f for f in cls.__dataclass_fields__}
        unknown = set(kwargs) - known
        for u in unknown:
            del kwargs[u]
        try:
            state = cls(**{k: v for k, v in kwargs.items() if k in known})
        except TypeError as exc:
            raise StateError(f"state file has invalid fields: {exc}") from exc
        # Required-field sanity.
        for req in ("run_id", "repository", "epic_url", "created_at", "updated_at"):
            if not getattr(state, req, None):
                raise StateError(f"state file missing required field {req!r}")
        if not isinstance(state.counted_merged_prs, list):
            raise StateError("state field 'counted_merged_prs' must be a list")
        if not isinstance(state.open_findings, list):
            raise StateError("state field 'open_findings' must be a list")
        if not isinstance(state.last_fix_resolutions, list):
            raise StateError("state field 'last_fix_resolutions' must be a list")
        if not isinstance(state.next_issue_rejections, list):
            raise StateError("state field 'next_issue_rejections' must be a list")
        try:
            validate_review_history(state.review_history)
        except StateError as exc:
            raise StateError(f"state field {exc}") from None
        if not isinstance(state.step_count, int) or isinstance(state.step_count, bool):
            raise StateError("state field 'step_count' must be an integer")
        if not isinstance(state.review_round, int) or isinstance(state.review_round, bool):
            raise StateError("state field 'review_round' must be an integer")
        if state.last_review_needs_fix is not None and not isinstance(
            state.last_review_needs_fix, bool
        ):
            raise StateError("state field 'last_review_needs_fix' must be a boolean or null")
        if state.protocol_version != __protocol_version__:
            raise StateError(
                f"unsupported protocol_version {state.protocol_version!r} "
                f"(controller speaks {__protocol_version__!r})"
            )
        return state

    def touch(self) -> None:
        self.updated_at = utcnow_iso()

    # -- merge accounting (idempotent) ----------------------------------
    def record_merge(self, pr_url: str) -> bool:
        """Record a merged PR. Returns True if newly counted.

        Idempotent: recording the same ``pr_url`` twice only counts once, so
        a retried MERGE step after a crash can never double-count.
        """
        if pr_url and pr_url not in self.counted_merged_prs:
            self.counted_merged_prs.append(pr_url)
            self.merged_since_epic_update += 1
            return True
        return False

    def record_epic_update(self) -> None:
        self.merged_since_epic_update = 0

    # -- per-issue bookkeeping ------------------------------------------
    def reset_for_new_issue(self, issue_url: str) -> None:
        """Clear all PR/review bookkeeping when moving to another issue.

        ``step_count`` is deliberately kept: it is the run's cumulative step
        budget and must survive issue switches and ``resume`` alike.
        """
        self.current_issue_url = issue_url
        self.current_pr_url = ""
        self.current_branch = ""
        self.current_head_sha = ""
        self.reviewed_head_sha = ""
        self.review_round = 0
        self.last_review_result = ""
        self.last_review_needs_fix = None
        self.last_review_comment_url = ""
        self.open_findings = []
        self.last_fix_resolutions = []
        self.review_history = []
        self.next_issue_rejections = []
        self.attempt = 0

    @property
    def last_review_round(self) -> int:
        return self.review_round


# -- paths ---------------------------------------------------------------
@dataclass(frozen=True)
class StatePaths:
    """Where a run's state and logs live.

    The controller lock is *not* here: it is keyed by the repository
    identity, not by the caller-selectable state directory (see
    :func:`autoforge.locking.repository_lock_path`).
    """

    state_dir: Path
    state_file: Path
    logs_dir: Path

    @classmethod
    def from_state_dir(cls, state_dir: str | Path) -> StatePaths:
        d = Path(state_dir)
        return cls(
            state_dir=d,
            state_file=d / STATE_FILENAME,
            logs_dir=d / LOGS_DIRNAME,
        )


# -- persistence ----------------------------------------------------------
def save_state(state: AutoForgeState, path: str | Path) -> None:
    """Atomically persist state: temp file + fsync + atomic replace."""
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    state.touch()
    payload = json.dumps(state.to_dict(), indent=2, sort_keys=True) + "\n"
    fd, tmp_name = tempfile.mkstemp(prefix=".state-", suffix=".tmp", dir=str(dest.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, dest)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


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
    return "special file"


def _not_regular(p: Path, kind: str, is_link: bool) -> StateError:
    what = f"symbolic link to a {kind}" if is_link else f"a {kind}, not a regular file"
    # A directory cannot be archived by quarantine_state_file (no hard links
    # to directories), so 'run --force' is no way out of it.
    hint = (
        "move it out of the way by hand"
        if kind == "directory" and not is_link
        else ("move it aside or use 'run --force'")
    )
    return StateError(f"corrupted state file {p}: {what}; refusing to overwrite — {hint}")


def _read_regular_file(p: Path) -> bytes:
    """Read ``p`` only if it is a regular file (or a symlink to one).

    A FIFO, socket, device or directory is refused before it is opened: a
    plain ``open()`` on a FIFO without a writer blocks forever, so ``run``
    could never reach the fail-loud / quarantine path.  The check is
    repeated on the open descriptor (``O_NONBLOCK`` keeps a FIFO open from
    blocking), so an entry swapped between the two inspections is still
    caught.  Raises StateError for a non-regular entry, OSError otherwise.
    """
    st = os.lstat(p)
    is_link = stat.S_ISLNK(st.st_mode)
    if is_link:
        st = os.stat(p)  # follows the link; dangling links were rejected earlier
    kind = entry_kind(st.st_mode)
    if kind is not None:
        raise _not_regular(p, kind, is_link)
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOCTTY", 0)
    fd = os.open(p, flags)
    try:
        kind = entry_kind(os.fstat(fd).st_mode)
        if kind is not None:
            raise _not_regular(p, kind, is_link)
        with os.fdopen(fd, "rb") as fh:
            fd = -1
            return fh.read()
    finally:
        if fd != -1:
            os.close(fd)


def load_state(path: str | Path) -> AutoForgeState:
    """Load state; raises StateError (never silently re-inits) on problems."""
    p = Path(path)
    # lexists: a dangling symlink is still a state-directory entry (Path.exists
    # follows the link and would report it as absent, which lets a fresh run
    # replace it silently).
    if not os.path.lexists(p):
        raise StateError(
            f"no state file at {p} — run 'autoforge run --epic ... --issue ...' first; "
            "'resume' never creates a new run silently"
        )
    if p.is_symlink() and not p.exists():
        raise StateError(
            f"corrupted state file {p}: dangling symbolic link to {os.readlink(p)!r}; "
            "refusing to overwrite — restore from backup or re-run"
        )
    try:
        raw_bytes = _read_regular_file(p)
    except OSError as exc:
        raise StateError(f"cannot read state file {p}: {exc}") from exc
    try:
        raw = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        # Invalid UTF-8 is a corrupt file, not a read failure: it must take
        # the same fail-loud / quarantine path as unparseable JSON.
        raise StateError(
            f"corrupted state file {p}: not valid UTF-8 ({exc}); "
            "refusing to overwrite — restore from backup or re-run"
        ) from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise StateError(
            f"corrupted state file {p}: invalid JSON ({exc}); "
            "refusing to overwrite — restore from backup or re-run"
        ) from exc
    return AutoForgeState.from_dict(data)


CORRUPT_SUFFIX = ".corrupt-"
_QUARANTINE_MAX_ATTEMPTS = 1000


def quarantine_state_file(path: str | Path) -> Path:
    """Move an unreadable state file aside instead of deleting it.

    Renames ``<path>`` to ``<path>.corrupt-<UTC timestamp>`` (a numeric
    suffix is appended if that name is already taken) and returns the new
    path.  Never overwrites an existing file: the destination is reserved
    with :func:`os.link`, which fails atomically with ``EEXIST`` when the
    name is already taken (a plain ``rename`` would silently replace a file
    created between the existence check and the move).  On a collision the
    next numeric suffix is tried.  The directory entry itself is moved: a
    symbolic link (dangling or not) is archived as a link and the file it
    points to is never followed, modified or removed; a FIFO, socket or
    device entry is archived as that entry without being opened.  A
    directory cannot be hard-linked and is refused: it stays untouched and
    must be moved aside by hand.  Raises StateError when the move fails; the
    original entry is left untouched in that case.

    The caller must hold the controller lock (the CLI does, via
    ``ControllerEngine.locked()``): link and unlink are two syscalls, and a
    writer replacing ``path`` in between would see the replacement removed.
    """
    src = Path(path)
    try:
        src_mode = os.lstat(src).st_mode
    except OSError as exc:
        raise StateError(f"cannot move corrupted state file {src} aside: {exc}") from exc
    if stat.S_ISDIR(src_mode):
        raise StateError(
            f"cannot move corrupted state file {src} aside: it is a directory; "
            "move it out of the way by hand and re-run"
        )
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    base = src.with_name(f"{src.name}{CORRUPT_SUFFIX}{stamp}")
    candidate = base
    for n in range(1, _QUARANTINE_MAX_ATTEMPTS + 1):
        try:
            # Atomic no-replace reservation: link() never clobbers a
            # destination that appeared after we picked the candidate.
            # follow_symlinks=False links the entry itself, so a (dangling)
            # symlink is preserved as such instead of failing on its target.
            os.link(src, candidate, follow_symlinks=False)
        except FileExistsError:
            candidate = base.with_name(f"{base.name}.{n}")
            continue
        except OSError as exc:
            raise StateError(f"cannot move corrupted state file {src} aside: {exc}") from exc
        try:
            os.unlink(src)
        except OSError as exc:
            # Drop the reservation so the original is the only copy again.
            try:
                os.unlink(candidate)
            except OSError:
                pass
            raise StateError(f"cannot move corrupted state file {src} aside: {exc}") from exc
        return candidate
    raise StateError(
        f"cannot move corrupted state file {src} aside: "
        f"no free name after {_QUARANTINE_MAX_ATTEMPTS} attempts (last tried {candidate})"
    )
