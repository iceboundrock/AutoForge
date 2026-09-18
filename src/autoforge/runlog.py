"""Run logging: one directory per agent invocation, everything redacted.

Layout::

    <state_dir>/logs/<run-id>/
        events.jsonl                          # one JSON line per invocation, appended
                                              # in place; never read by the controller
        <seq>-<phase>-<attempt>/
            request.json      # phase, profile, provider, model, effort, prompt version,
                              # cwd, timeout, redacted argv (no environment dump)
            prompt.md         # rendered prompt sent to the agent (redacted)
            execution.json    # timestamps, exit code, timed_out, stdout/stderr sizes
                              # and whether either stream was truncated at the capture bound
            stdout.log        # redacted stdout (head + omission marker + tail when truncated)
            stderr.log        # redacted stderr (same)
            control-result.json   # parsed CONTROL_RESULT when one was accepted
            error.txt         # controller-side error, when the step failed

Logs never pollute state.json.

The step sequence is recovered from the step directory names, not from the
journal: a step directory is published (and fsynced) before its journal line
is appended, so the highest directory number is never below the highest
``seq`` in the journal, and listing the run's own directory is bounded
(``MAX_RUN_LOG_ENTRIES``) while the journal is not. The journal is
write-only for the controller -- appended to in place through a descriptor
that has been proved a single-named regular file, and refused on its size
(``MAX_EVENT_JOURNAL_BYTES``) without being read -- so the cost of recording
an invocation is the line, however long the run. Nothing inspects the tail
before a line is appended either, so a last line torn by a crash is glued
to the line appended after it: a reader of the journal must resync on its
own and cannot assume at most one bad line (ADR 0001 §5.5).
"""

from __future__ import annotations

import json
import os
import re
import secrets
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .errors import StateError
from .redaction import redact, redact_argv, redact_dict
from .safefs import ReadLimitExceeded, SafeRoot, UnreadableEntryError, WalkBudgetExceeded

# A run identifier is a *file name*: it names the directory this run's logs
# live in.  `generate_run_id` produces "af-<UTC stamp>-<hex>", but the value
# actually used comes back from `state.json`, so it is validated wherever it
# is turned into a path rather than trusted because the controller once
# generated it.
RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

# The largest an event journal may be before the controller refuses to
# extend it. The controller never reads the journal (the sequence comes from
# the step directory names), so this bounds no read; it is the size past
# which the file at that name is not one the controller wrote -- a real
# journal is a few kilobytes to a few hundred kilobytes per line (the record
# carries the redacted argv, which holds the rendered prompt, and the
# accepted CONTROL_RESULT, itself capped) and at most a few thousand lines
# (one per invocation, bounded by the run's step budget and the correction
# attempts). It is checked on the opened descriptor's ``st_size``, which is
# O(1) whatever the file holds, once before the agent is launched (so a
# journal a same-user agent planted refuses before any work is done, #57)
# and once at the append after it returned (the one moment the agent has
# had to enlarge it, #55). The budget bounds what the controller is willing
# to append to, not the file's final size: a journal at exactly the budget
# is still appended to, and the file that results is refused by the next
# check.
MAX_EVENT_JOURNAL_BYTES = 64 * 1024 * 1024
# The most entries the run's own log directory may hold before the listing
# that recovers the step sequence refuses instead of listing without end
# (#56). A run publishes one step directory per invocation, and a run's
# invocations number in the thousands at most (the step budget times the
# correction attempts); the journal itself, and the copies an operator moved
# aside as a refusal told them to, are a handful of names against this. Only
# the run's own directory is listed: sibling runs under ``logs/`` never
# count, so a state directory with a long history of runs cannot exhaust
# this for a reason unrelated to the run being resumed. It is a budget on the
# listing work, checked when the logger is opened, not a ceiling on what the
# directory may come to hold: a logger opened at the budget still publishes
# its step directory, and the next open refuses.
MAX_RUN_LOG_ENTRIES = 200_000


def validate_run_id(run_id: str) -> str:
    """Return ``run_id`` if it is a safe single path component, else raise.

    ``logs/<run_id>` is a controller write path, so a ``run_id`` of
    ``"../.."`` (or ``"/etc"``, or one holding a NUL) would move every log
    artifact of the run out of the state directory.  A state file carrying
    one is corruption and must fail loudly; :func:`validate_run_id` is the
    last line before the path is built, and
    :meth:`autoforge.state.AutoForgeState.from_dict` applies the same rule on
    load so the failure names the state file rather than the first log write.
    """
    if not isinstance(run_id, str) or not RUN_ID_RE.match(run_id) or run_id in (".", ".."):
        raise StateError(
            f"invalid run_id {run_id!r}: it names the run's log directory, so it must be a "
            "single path component of letters, digits, '.', '_' or '-' (at most 128 "
            "characters, not starting with '.'), never a path, an absolute name or '..'"
        )
    return run_id


@dataclass
class ExecutionRecord:
    run_id: str
    seq: int
    phase: str
    attempt: int = 1
    correction: bool = False
    issue_url: str = ""
    pr_url: str = ""
    review_round: int = 0
    profile: str = ""
    provider: str = ""
    model: str = ""
    effort: str = ""
    prompt_version: str = ""
    command: list[str] = field(default_factory=list)
    cwd: str = ""
    timeout_seconds: int = 0
    started_at: str = ""
    finished_at: str = ""
    exit_code: int = 0
    timed_out: bool = False
    # The executor kept head + marker + tail of the stream (see
    # ``executor.DEFAULT_MAX_OUTPUT_BYTES``); ``stdout.log`` shows the cut.
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    dry_run: bool = False
    parsed_result: dict | None = None
    error: str = ""
    log_dir: str = ""
    metadata: dict[str, object] = field(default_factory=dict)


class RunLogger:
    """Writes one directory per agent invocation under ``<logs_dir>/<run_id>``.

    The log tree lives in the controller's state directory, on a machine
    where the agents the controller launches run as the same user.  So no
    path here is trusted by name: ``run_id`` must be a single safe path
    component (it comes from ``state.json``, which a hand edit or a truncated
    write can corrupt), and every directory and artifact is reached through a
    :class:`~autoforge.safefs.SafeRoot` -- descriptor-relative, so neither
    ``logs`` nor ``logs/<run_id>`` nor any step directory can be swapped for a
    symbolic link that puts controller artifacts somewhere else, and no
    artifact write can land on a FIFO, a device or a second name for someone
    else's file.

    The engine passes ``open_state_root`` -- :meth:`autoforge.state
    .StatePaths.open_root`, which walks from the one directory AutoForge did
    not create down to the state directory -- so every component from that
    anchor to a step artifact is checked. A caller with only a pathname gets
    an anchor at the state directory's parent instead, which still reaches
    ``logs`` and everything below it without following a link.
    """

    def __init__(
        self,
        logs_dir: str | Path,
        run_id: str,
        *,
        open_state_root: Callable[[], SafeRoot] | None = None,
    ) -> None:
        self.run_id = validate_run_id(run_id)
        self._open_state_root = open_state_root
        absolute = Path(os.path.abspath(logs_dir))
        self._anchor = absolute.parent
        self._logs_name = absolute.name
        self.run_dir = Path(logs_dir) / self.run_id
        self.events_path = self.run_dir / "events.jsonl"
        with self._logs_root() as logs:
            logs.ensure_dir(self.run_id)
            self._verify_publishable(logs)
            # Sequence counter resumes across process restarts.
            self._seq = self._recover_sequence(logs)

    @contextmanager
    def _logs_root(self) -> Iterator[SafeRoot]:
        """The ``logs`` directory as a capability, for the duration of one call."""
        base = (
            self._open_state_root()
            if self._open_state_root is not None
            else SafeRoot.open(self._anchor, create=True)
        )
        try:
            root = base.subroot(self._logs_name, create=True)
        finally:
            base.close()
        try:
            yield root
        finally:
            root.close()

    def _journal_remedy(self, step: str) -> str:
        """The way to resume that a refused journal leaves the operator."""
        return (
            f"The controller only appends to the journal and never reads it, so {step} to "
            "resume; the step directories are kept and the sequence continues from their "
            "names"
        )

    @contextmanager
    def _refusing_journal(self, unrecorded: str = "") -> Iterator[None]:
        """Word a refusal of the journal so that it names the manual step.

        The journal is opened twice per invocation, before the launch
        (:meth:`_recover_sequence`) and at the append after the agent
        returned (:meth:`log_execution`), and both opens refuse for the same
        causes. Every refusal must leave the operator a way to resume, and
        the cause decides what that is: a file past the size budget is not
        a controller journal and is moved aside; a journal the controller
        may not open for writing (``EACCES``: the in-place append needs a
        writable file where a whole-file replacement needed only a writable
        directory, and the controller creates the journal ``0600``, so this
        is a file planted or ``chmod``-ed by someone else) is made writable
        or moved aside; a link, a FIFO or a directory already says to move
        the entry aside. The error keeps its type, so the filesystem cause
        stays distinguishable; only the message grows. ``unrecorded`` says
        where an invocation's artifacts are when the refusal is the append's.
        """
        journal = f"logs/{self.run_id}/events.jsonl"
        tail = f"; {unrecorded}" if unrecorded else ""
        try:
            yield
        except ReadLimitExceeded:
            raise StateError(
                f"corrupted event journal for run {self.run_id}: larger than "
                f"{MAX_EVENT_JOURNAL_BYTES} bytes, which no controller journal can be. "
                f"{self._journal_remedy(f'move {journal} aside')}{tail}"
            ) from None
        except UnreadableEntryError as exc:
            remedy = self._journal_remedy(f"make {journal} writable, or move it aside,")
            exc.args = (f"{exc}. {remedy}{tail}",)
            raise
        except StateError as exc:
            if tail:
                exc.args = (f"{exc}{tail}",)
            raise

    def _refuse_run_dir(self, why: str) -> StateError:
        return StateError(
            f"corrupted run log directory for run {self.run_id}: {why}. The directory is "
            "only listed to continue the step sequence from the step directories it "
            f"holds, so move the entries AutoForge did not create out of "
            f"logs/{self.run_id}/ to resume"
        )

    def _verify_publishable(self, logs: SafeRoot) -> None:
        """Prove, before the launch, what :meth:`log_execution` will need after it.

        The logger is opened before the agent is launched so that a refusal
        of the run log lands then, charging no launch (#57), rather than
        after a write-capable agent has returned with work that would go
        unlogged. That refusal is only as good as what is proved here, and
        the post-agent record needs two things of the run directory: that
        it takes a new entry (the step directory's ``mkdir``, then the
        artifacts and, for a first invocation, the journal itself), and
        that the journal at its name can be appended to.

        The first is proved the way ``doctor`` proves the state directory:
        an entry is created in the run directory through the same capability
        and removed again. ``logs/<run>`` is created ``0700`` by this
        controller, but an existing one is whatever is there -- a ``0500``
        directory, a read-only mount, a filesystem without ``link(2)`` --
        and it passes every other check while the journal is absent. The
        second is proved by opening the journal exactly as the append will
        open it, writing nothing: a link, a FIFO, a second name, a file the
        controller may not write or one past ``MAX_EVENT_JOURNAL_BYTES``
        refuses here. Neither proof outlives the launch, since the agent
        runs as the same user and can undo either while it runs; they are
        the refusals that can land before it does. A crash between the
        probe's create and its unlink leaves one ``.af-probe-*`` entry,
        which the sequence listing ignores like any other non-step name.
        """
        run_dir = f"logs/{self.run_id}/"
        probe = f"{self.run_id}/.af-probe-{os.getpid():x}-{secrets.token_hex(8)}"
        try:
            logs.create_exclusive(probe, b"")
            logs.unlink(probe)
        except UnreadableEntryError as exc:
            exc.args = (
                f"cannot publish into {run_dir}: {exc}. A step directory and its artifacts "
                f"are published there after every agent invocation, so make {run_dir} "
                "writable to resume; the step directories are kept and the sequence "
                "continues from their names",
            )
            raise
        except StateError as exc:
            exc.args = (
                f"cannot publish into {run_dir}: {exc}. A step directory and its artifacts "
                "are published there after every agent invocation",
            )
            raise
        with self._refusing_journal():
            logs.verify_appendable(f"{self.run_id}/events.jsonl", limit=MAX_EVENT_JOURNAL_BYTES)

    def _recover_sequence(self, logs: SafeRoot) -> int:
        """The highest step number this run has published, from the directory names.

        The journal is not consulted. A step directory is published before
        its journal line, so the directory names are never behind the
        journal, and they are what a crash between the two leaves behind;
        reading the journal as well would only add a cost that grows with
        the run (#51).
        """
        # Only the run's own directory and its immediate children matter, so
        # the walk starts *at* the run directory (a sub-root, so sibling runs
        # under ``logs/`` are never listed -- #56) and descends no further:
        # every entry is skipped, so the step directories' contents are not
        # listed either. The listing is budgeted (``MAX_RUN_LOG_ENTRIES``):
        # more entries than a run can publish is not a run directory the
        # controller wrote, and the walk refuses while listing rather than
        # after holding a planted million names.
        highest = 0
        with logs.subroot(self.run_id) as run:
            try:
                for entry in run.walk(max_entries=MAX_RUN_LOG_ENTRIES):
                    entry.skip = True
                    match = re.match(r"^(\d+)-", entry.name)
                    if match:
                        highest = max(highest, int(match.group(1)))
            except WalkBudgetExceeded:
                raise self._refuse_run_dir(
                    f"more than {MAX_RUN_LOG_ENTRIES} entries, which no controller run "
                    "directory can hold (at most one step directory per invocation)"
                ) from None
        return highest

    def _step_name(self, seq: int, phase: str, attempt: int) -> str:
        safe_phase = re.sub(r"[^a-z0-9._-]", "-", phase.lower()) or "unknown"
        return f"{seq:03d}-{safe_phase}-{attempt}"

    def log_execution(
        self,
        record: ExecutionRecord,
        prompt: str = "",
        stdout: str = "",
        stderr: str = "",
    ) -> Path:
        """Persist one execution record + artifacts. Returns the step dir."""
        self._seq += 1
        record.seq = self._seq
        step = self._step_name(self._seq, record.phase, record.attempt)
        step_dir = self.run_dir / step
        record.log_dir = str(step_dir)
        record.command = redact_argv(record.command)
        # Metadata is caller-supplied and can quote untrusted text (a
        # validation command's argv, a feature path). It is written to both
        # request.json and events.jsonl, so it passes the same boundary the
        # command and the parsed result do.
        record.metadata = redact_dict(record.metadata)
        # Redacted on the record itself, not at each write site: `error` is
        # persisted three times (execution.json, error.txt and the whole
        # record in events.jsonl) and a controller-side error quotes agent
        # stdout, a failing command line and the occasional environment
        # value. Redacting per call site left the journal -- which serialises
        # the record wholesale -- with the only unredacted copy.
        record.error = redact(record.error)
        if isinstance(record.parsed_result, dict):
            record.parsed_result = json.loads(redact(json.dumps(record.parsed_result)))
        request = {
            "run_id": record.run_id,
            "seq": record.seq,
            "phase": record.phase,
            "attempt": record.attempt,
            "correction": record.correction,
            "issue_url": record.issue_url,
            "pr_url": record.pr_url,
            "review_round": record.review_round,
            "profile": record.profile,
            "provider": record.provider,
            "model": record.model,
            "effort": record.effort,
            "prompt_version": record.prompt_version,
            "cwd": record.cwd,
            "timeout_seconds": record.timeout_seconds,
            "command": record.command,
            "metadata": record.metadata,
        }
        execution = {
            "started_at": record.started_at,
            "finished_at": record.finished_at,
            "exit_code": record.exit_code,
            "timed_out": record.timed_out,
            "stdout_truncated": record.stdout_truncated,
            "stderr_truncated": record.stderr_truncated,
            "stdout_chars": len(stdout or ""),
            "stderr_chars": len(stderr or ""),
            "error": record.error,
        }
        base = f"{self.run_id}/{step}"
        with self._logs_root() as logs:
            logs.ensure_dir(base)
            logs.write_text(
                f"{base}/request.json", json.dumps(request, indent=2, sort_keys=True) + "\n"
            )
            logs.write_text(f"{base}/prompt.md", redact(prompt or ""))
            logs.write_text(
                f"{base}/execution.json", json.dumps(execution, indent=2, sort_keys=True) + "\n"
            )
            logs.write_text(f"{base}/stdout.log", redact(stdout))
            logs.write_text(f"{base}/stderr.log", redact(stderr))
            if isinstance(record.parsed_result, dict):
                logs.write_text(
                    f"{base}/control-result.json",
                    json.dumps(record.parsed_result, indent=2, sort_keys=True) + "\n",
                )
            if record.error:
                logs.write_text(f"{base}/error.txt", record.error + "\n")
            # The step directory and its artifacts are published above,
            # before the journal line, so a refusal here leaves the sequence
            # recoverable from the directory names. The append happens after
            # the agent returned, which is the one moment an agent has had
            # to replace or enlarge the journal; the open proves the file
            # again and the size check refuses without reading (#55). A
            # refusal of any cause says where the invocation's record is.
            unrecorded = (
                f"the invocation's artifacts are in logs/{self.run_id}/{step}/ but its "
                "journal line was not written"
            )
            with self._refusing_journal(unrecorded):
                logs.append_text(
                    f"{self.run_id}/events.jsonl",
                    json.dumps(asdict(record), sort_keys=True) + "\n",
                    limit=MAX_EVENT_JOURNAL_BYTES,
                )
        return step_dir
