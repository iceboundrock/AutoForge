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
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .redaction import redact, redact_argv, redact_dict


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
    def __init__(self, logs_dir: str | Path, run_id: str) -> None:
        self.run_dir = Path(logs_dir) / run_id
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.events_path = self.run_dir / "events.jsonl"
        # Sequence counter resumes across process restarts.
        self._seq = self._existing_event_count()

    def _existing_event_count(self) -> int:
        if not self.events_path.exists():
            return 0
        try:
            with open(self.events_path, encoding="utf-8") as fh:
                return sum(1 for line in fh if line.strip())
        except OSError:
            return 0

    def _step_dir(self, seq: int, phase: str, attempt: int) -> Path:
        d = self.run_dir / f"{seq:03d}-{phase.lower()}-{attempt}"
        d.mkdir(parents=True, exist_ok=True)
        return d

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
        (step_dir / "request.json").write_text(
            json.dumps(request, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        (step_dir / "prompt.md").write_text(redact(prompt or ""), encoding="utf-8")
        (step_dir / "execution.json").write_text(
            json.dumps(execution, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        (step_dir / "stdout.log").write_text(redact(stdout), encoding="utf-8")
        (step_dir / "stderr.log").write_text(redact(stderr), encoding="utf-8")
        if isinstance(record.parsed_result, dict):
            (step_dir / "control-result.json").write_text(
                json.dumps(record.parsed_result, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        if record.error:
            (step_dir / "error.txt").write_text(redact(record.error) + "\n", encoding="utf-8")
        with open(self.events_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(asdict(record), sort_keys=True) + "\n")
        return step_dir
