"""Run logging: one directory per agent invocation, everything redacted.

Layout::

    <state_dir>/logs/<run-id>/
        events.jsonl                          # one JSON line per invocation
        <seq>-<phase>-<attempt>/
            request.json      # phase, profile, provider, model, effort, prompt version,
                              # cwd, timeout, redacted argv (no environment dump)
            prompt.md         # rendered prompt sent to the agent (redacted)
            execution.json    # timestamps, exit code, timed_out, stdout/stderr sizes
            stdout.log        # redacted stdout
            stderr.log        # redacted stderr
            control-result.json   # parsed CONTROL_RESULT when one was accepted
            error.txt         # controller-side error, when the step failed

Logs never pollute state.json.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .errors import StateError
from .redaction import redact, redact_argv, redact_dict
from .safefs import ReadLimitExceeded, SafeRoot

# A run identifier is a *file name*: it names the directory this run's logs
# live in.  `generate_run_id` produces "af-<UTC stamp>-<hex>", but the value
# actually used comes back from `state.json`, so it is validated wherever it
# is turned into a path rather than trusted because the controller once
# generated it.
RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

# The most an event journal may be before recovery refuses it as corrupt
# without reading it. Recovery needs the journal only for the highest ``seq``
# it holds, and it lives where the agents the controller launches also write
# (the same OS user), so like ``state.json`` (``MAX_STATE_FILE_BYTES``) it is
# read through ``SafeRoot.read_bytes(limit=)``: one byte past the budget and
# no more, so a sparse or oversized file at that name costs at most the
# budget rather than the machine's memory. A real journal is a few kilobytes
# per line and at most a few thousand lines (one per invocation, bounded by
# the run's step budget and the correction attempts), so the budget is
# generous; the record bound is checked *before* the journal is split into
# lines, so a journal of millions of empty records cannot allocate its way
# around the byte budget either.
MAX_EVENT_JOURNAL_BYTES = 64 * 1024 * 1024
MAX_EVENT_JOURNAL_RECORDS = 100_000


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
            # Sequence counter resumes across process restarts.
            self._seq = self._existing_event_count(logs)

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

    def _refuse_journal(self, why: str) -> StateError:
        return StateError(
            f"corrupted event journal for run {self.run_id}: {why}. The journal is only "
            "read to continue the step sequence, so move "
            f"logs/{self.run_id}/events.jsonl aside to resume; the step directories are "
            "kept and the sequence continues from their names"
        )

    def _existing_event_count(self, logs: SafeRoot) -> int:
        path = f"{self.run_id}/events.jsonl"
        try:
            data = logs.read_bytes(path, limit=MAX_EVENT_JOURNAL_BYTES)
        except ReadLimitExceeded:
            # Refused before it is held: the bounded read stops one byte past
            # the budget, whatever st_size claimed.
            raise self._refuse_journal(
                f"larger than {MAX_EVENT_JOURNAL_BYTES} bytes, which no controller journal can be"
            ) from None
        highest = 0
        if data is not None:
            # Counted on the bytes, before any line is materialised.
            records = data.count(b"\n") + (0 if data.endswith(b"\n") or not data else 1)
            if records > MAX_EVENT_JOURNAL_RECORDS:
                raise self._refuse_journal(
                    f"more than {MAX_EVENT_JOURNAL_RECORDS} records, which no controller "
                    "journal can hold"
                )
            for line_number, line in enumerate(data.splitlines(), 1):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                    raise StateError(
                        f"corrupted event journal for run {self.run_id}: line {line_number} "
                        f"is not valid JSON ({exc})"
                    ) from exc
                seq = record.get("seq") if isinstance(record, dict) else None
                if not isinstance(seq, int) or isinstance(seq, bool) or seq < 1:
                    raise StateError(
                        f"corrupted event journal for run {self.run_id}: line {line_number} "
                        "has no positive integer seq"
                    )
                highest = max(highest, seq)

        # A crash can publish a step directory before its journal line. Use
        # those names too, or the next invocation could reuse the directory
        # and overwrite a completed execution's artifacts.
        # Only the run's own directory and its immediate children matter, so
        # the walk descends no further: sibling runs and the step directories'
        # contents are skipped, not listed.
        prefix = f"{self.run_id}/"
        for entry in logs.walk():
            if entry.relpath != self.run_id:
                entry.skip = True
            if not entry.relpath.startswith(prefix):
                continue
            step_name = entry.relpath[len(prefix) :]
            match = re.match(r"^(\d+)-", step_name)
            if match:
                highest = max(highest, int(match.group(1)))
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
            logs.append_text(
                f"{self.run_id}/events.jsonl", json.dumps(asdict(record), sort_keys=True) + "\n"
            )
        return step_dir
