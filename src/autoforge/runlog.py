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
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .errors import StateError
from .redaction import redact, redact_argv, redact_dict
from .safeio import append_text, ensure_directory, read_text, write_text

# A run identifier is a *file name*: it names the directory this run's logs
# live in.  `generate_run_id` produces "af-<UTC stamp>-<hex>", but the value
# actually used comes back from `state.json`, so it is validated wherever it
# is turned into a path rather than trusted because the controller once
# generated it.
RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


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

    The log tree usually lives inside the operator's checkout, where the
    agents the controller launches also write.  So no path here is trusted
    by name: ``run_id`` must be a single safe path component (it comes from
    ``state.json``, which a hand edit or a truncated write can corrupt), and
    every directory and artifact is created through :mod:`autoforge.safeio`,
    which refuses to follow a symbolic link or to open a FIFO, socket or
    device.  Without that a ``logs`` symlink would put controller artifacts
    outside the working tree and a FIFO at ``events.jsonl`` would hang the
    controller on startup instead of failing it.
    """

    def __init__(self, logs_dir: str | Path, run_id: str) -> None:
        logs_root = ensure_directory(logs_dir)
        self.run_dir = logs_root / validate_run_id(run_id)
        ensure_directory(self.run_dir)
        self.events_path = self.run_dir / "events.jsonl"
        # Sequence counter resumes across process restarts.
        self._seq = self._existing_event_count()

    def _existing_event_count(self) -> int:
        content = read_text(self.events_path)
        if content is None:
            return 0
        return sum(1 for line in content.splitlines() if line.strip())

    def _step_dir(self, seq: int, phase: str, attempt: int) -> Path:
        safe_phase = re.sub(r"[^a-z0-9._-]", "-", phase.lower()) or "unknown"
        return ensure_directory(self.run_dir / f"{seq:03d}-{safe_phase}-{attempt}")

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
        step_dir = self._step_dir(self._seq, record.phase, record.attempt)
        record.log_dir = str(step_dir)
        record.command = redact_argv(record.command)
        # Metadata is caller-supplied and can quote untrusted text (a
        # validation command's argv, a feature path). It is written to both
        # request.json and events.jsonl, so it passes the same boundary the
        # command and the parsed result do.
        record.metadata = redact_dict(record.metadata)
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
            "error": redact(record.error),
        }
        write_text(step_dir / "request.json", json.dumps(request, indent=2, sort_keys=True) + "\n")
        write_text(step_dir / "prompt.md", redact(prompt or ""))
        write_text(
            step_dir / "execution.json", json.dumps(execution, indent=2, sort_keys=True) + "\n"
        )
        write_text(step_dir / "stdout.log", redact(stdout))
        write_text(step_dir / "stderr.log", redact(stderr))
        if isinstance(record.parsed_result, dict):
            write_text(
                step_dir / "control-result.json",
                json.dumps(record.parsed_result, indent=2, sort_keys=True) + "\n",
            )
        if record.error:
            write_text(step_dir / "error.txt", redact(record.error) + "\n")
        append_text(self.events_path, json.dumps(asdict(record), sort_keys=True) + "\n")
        return step_dir
