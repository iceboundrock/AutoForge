"""How a set of AutoForge runs stopped, from their state and run logs, read only (#369).

No test: the corpus protocol of ADR 0011 (D7), which a maintainer runs by
hand over the state directories of past runs. It classifies each run's
saved state with :mod:`autoforge.stop_classification` and counts its run
log's launches and operator actions, from typed fields only, and prints a
summary (``--json``: the ``autoforge.stop-corpus/1`` document)::

    uv run python -m tests.stop_corpus ~/code/AutoForge/.autoforge
    uv run python -m tests.stop_corpus --json RUN_DIR [RUN_DIR ...] > corpus.json

A path is a state directory (it holds ``state.json``) or a directory whose
subdirectories, two levels down at most, are; each state directory is one
run. Nothing is written, locked, launched or sent: a state file is read by
the controller's own strict loader (:func:`autoforge.state.load_state`),
which writes nothing, and ``logs/<run id>/events.jsonl`` by a bounded read.

**Units, each counted once in its own denominator, never summed across:**

- a *run* is one state directory, classified by its saved state alone into
  one eligibility (:data:`ELIGIBILITY`): ``intentional`` (DONE,
  READY_FOR_MERGE, a scheduler's hand-off), ``deterministic`` (a phase
  ``resume`` re-enters), ``needs_human`` (BLOCKED, FAILED, a pause) or
  ``undecidable`` (a state that does not load, or an UNKNOWN outcome);
- a *launch* is one journal line of an agent or a controller-run command,
  failed when it timed out, exited non-zero or recorded an error; a failed
  agent launch's code comes from ``timed_out``, ``ended_by`` and
  ``exit_code``, and is ``unknown`` when only the error text says more;
- an *unblock* is one journal line of an operator's ``unblock``, applied or
  refused; each applied one ended one BLOCKED episode, which needed a human
  by the exit rule, whatever its reason.

**What it cannot say** (printed with every summary, :data:`LIMITATIONS`):
no reason code is persisted (ADR 0011 D5), so a saved BLOCKED or FAILED
classifies ``unknown`` and the free text of ``block_reason`` and of a
launch's ``error`` is never read; a run log that was moved, deleted or torn
leaves its run's history incomplete (each such run is flagged, and a torn or
foreign line is counted, not parsed around); and the corpus is the
directories given, nothing else: whatever selected them is its bias.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from autoforge.errors import StateError
from autoforge.runlog import MAX_EVENT_JOURNAL_BYTES
from autoforge.state import STATE_FILENAME, load_state
from autoforge.stop_classification import (
    StopClassification,
    StopOutcome,
    StopReasonCode,
    classify_exception,
    classify_state,
)

SCHEMA = "autoforge.stop-corpus/1"
# How deep below a given path a state directory is looked for.
MAX_DEPTH = 2

# The eligibility of each final outcome; total over StopOutcome (tested).
ELIGIBILITY: dict[StopOutcome, str] = {
    StopOutcome.DONE: "intentional",
    StopOutcome.READY_FOR_MERGE: "intentional",
    StopOutcome.HANDOFF: "intentional",
    StopOutcome.IN_PROGRESS: "deterministic",
    StopOutcome.RESUMABLE_ERROR: "deterministic",
    StopOutcome.BLOCKED: "needs_human",
    StopOutcome.FAILED: "needs_human",
    StopOutcome.AWAITING_INPUT: "needs_human",
    StopOutcome.REFUSED: "undecidable",
    StopOutcome.UNKNOWN: "undecidable",
}
ELIGIBILITIES = ("intentional", "deterministic", "needs_human", "undecidable")

LIMITATIONS = (
    "no reason code is persisted: a saved BLOCKED or FAILED classifies 'unknown', and no "
    "block_reason or error text is read",
    "a failed agent launch that exited 0 (a malformed result, a failed verification, a "
    "refused run log) has no typed cause in the journal: 'unknown'",
    "a run log that was moved, deleted or torn leaves its run's history incomplete; "
    "such runs are flagged and torn or foreign lines counted, never parsed around",
    "the corpus is the directories given and nothing else: what selected them is its "
    "selection bias, and no denominator reaches past them",
)


@dataclass
class RunReport:
    """One state directory: its final stop and its run log's counts."""

    state_dir: str
    run_id: str = ""
    final: StopClassification | None = None
    eligibility: str = "undecidable"
    launches: Counter[str] = field(default_factory=Counter)
    failed_launches: Counter[str] = field(default_factory=Counter)
    unblocks_applied: int = 0
    unblocks_refused: int = 0
    unblock_history: int = 0
    torn_lines: int = 0
    journal: str = "absent"  # absent | read | too-large | unreadable

    @property
    def incomplete(self) -> bool:
        """Whether the run's history cannot be read whole."""
        if self.final is None or self.final.outcome is StopOutcome.REFUSED:
            return True
        return (
            self.journal != "read"
            or self.torn_lines > 0
            or self.unblock_history != self.unblocks_applied
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "state_dir": self.state_dir,
            "run_id": self.run_id,
            "final": self.final.as_dict() if self.final else None,
            "eligibility": self.eligibility,
            "launches": dict(sorted(self.launches.items())),
            "failed_launches": dict(sorted(self.failed_launches.items())),
            "unblocks_applied": self.unblocks_applied,
            "unblocks_refused": self.unblocks_refused,
            "unblock_history": self.unblock_history,
            "journal": self.journal,
            "torn_lines": self.torn_lines,
            "incomplete": self.incomplete,
        }


def find_state_dirs(paths: Iterable[Path], max_depth: int = MAX_DEPTH) -> list[Path]:
    """Every state directory at or below each path, links unfollowed, each once, sorted."""
    found: set[Path] = set()
    for root in paths:
        level = [root]
        for _ in range(max_depth + 1):
            below: list[Path] = []
            for directory in level:
                if directory.is_symlink() or not directory.is_dir():
                    continue
                state = directory / STATE_FILENAME
                if state.is_file() and not state.is_symlink():
                    found.add(directory.resolve())
                    continue
                try:
                    below += sorted(p for p in directory.iterdir() if p.is_dir())
                except OSError:
                    continue
            level = below
    return sorted(found)


def launch_kind(record: dict) -> str:
    """``agent``, ``command`` or ``operator``: from the record's typed fields."""
    profile = str(record.get("profile", ""))
    if profile.startswith("(operator"):
        return "operator"
    if profile.startswith("(controller") or int(record.get("timeout_seconds") or 0) > 0:
        return "command"
    return "agent"


def failed(record: dict) -> bool:
    return (
        bool(record.get("timed_out"))
        or record.get("exit_code") not in (0, None)
        or bool(record.get("error"))
    )


def launch_code(record: dict) -> StopReasonCode:
    """A failed launch's code from ``timed_out``, ``ended_by`` and ``exit_code`` alone."""
    ended_by = str(record.get("ended_by") or "")
    if record.get("timed_out") or ended_by in ("idle", "max_runtime"):
        return StopReasonCode.EXECUTION_TIMEOUT
    if ended_by in ("loop", "provider_failure") or record.get("exit_code") not in (0, None):
        return StopReasonCode.EXECUTION_FAILED
    return StopReasonCode.UNKNOWN


def _journal_lines(path: Path) -> tuple[str, list[bytes]]:
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return "absent", []
    except OSError:
        return "unreadable", []
    if size > MAX_EVENT_JOURNAL_BYTES:
        return "too-large", []
    try:
        with path.open("rb") as handle:
            data = handle.read(MAX_EVENT_JOURNAL_BYTES + 1)
    except OSError:
        return "unreadable", []
    return "read", data.splitlines()


def read_run(state_dir: Path) -> RunReport:
    """One run, read only."""
    report = RunReport(state_dir=str(state_dir))
    try:
        state = load_state(state_dir / STATE_FILENAME)
    except StateError as exc:
        report.final = classify_exception(exc)
        report.eligibility = ELIGIBILITY[report.final.outcome]
        return report
    report.run_id = state.run_id
    report.final = classify_state(state)
    report.eligibility = ELIGIBILITY[report.final.outcome]
    report.unblock_history = len(state.unblock_history)
    report.journal, lines = _journal_lines(state_dir / "logs" / state.run_id / "events.jsonl")
    for line in lines:
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except (ValueError, RecursionError):
            report.torn_lines += 1
            continue
        if not isinstance(record, dict) or record.get("run_id") != state.run_id:
            report.torn_lines += 1
            continue
        kind = launch_kind(record)
        if kind == "operator":
            if str(record.get("phase", "")).endswith("-unblock"):
                metadata = record.get("metadata") or {}
                if isinstance(metadata, dict) and metadata.get("applied") is True:
                    report.unblocks_applied += 1
                else:
                    report.unblocks_refused += 1
            report.launches["operator"] += 1
            continue
        report.launches[kind] += 1
        if failed(record):
            code = launch_code(record) if kind == "agent" else StopReasonCode.UNKNOWN
            report.failed_launches[f"{kind}:{code.value}"] += 1
    return report


def summarize(reports: Sequence[RunReport]) -> dict[str, object]:
    """The corpus summary: each unit counted once in its own denominator."""
    eligibility = Counter(r.eligibility for r in reports)
    outcomes = Counter(r.final.outcome.value for r in reports if r.final is not None)
    codes = Counter(
        f"{r.final.outcome.value}:{r.final.code.value}" for r in reports if r.final is not None
    )
    launches: Counter[str] = Counter()
    failures: Counter[str] = Counter()
    for r in reports:
        launches.update(r.launches)
        failures.update(r.failed_launches)
    blocked_now = sum(
        1 for r in reports if r.final is not None and r.final.outcome is StopOutcome.BLOCKED
    )
    applied = sum(r.unblocks_applied for r in reports)
    return {
        "schema": SCHEMA,
        "runs": {
            "total": len(reports),
            "by_eligibility": {name: eligibility.get(name, 0) for name in ELIGIBILITIES},
            "by_outcome": dict(sorted(outcomes.items())),
            "by_outcome_and_code": dict(sorted(codes.items())),
            "incomplete_history": sum(1 for r in reports if r.incomplete),
        },
        "launches": {
            "total": sum(launches.values()),
            "by_kind": dict(sorted(launches.items())),
            "failed": sum(failures.values()),
            "failed_by_kind_and_code": dict(sorted(failures.items())),
        },
        "unblocks": {
            "applied": applied,
            "refused": sum(r.unblocks_refused for r in reports),
            "in_state_histories": sum(r.unblock_history for r in reports),
        },
        # Every BLOCKED episode needed a human (the exit rule): those an
        # applied unblock ended, and those still BLOCKED.
        "blocked_episodes": {"ended_by_unblock": applied, "still_blocked": blocked_now},
        "torn_lines": sum(r.torn_lines for r in reports),
        "limitations": list(LIMITATIONS),
        "run_reports": [r.as_dict() for r in reports],
    }


def render(summary: dict) -> str:
    runs, launches, unblocks = summary["runs"], summary["launches"], summary["unblocks"]
    lines = [
        f"runs: {runs['total']} (each counted once, by its saved state)",
        *(f"  {name}: {count}" for name, count in runs["by_eligibility"].items()),
        "  by outcome and code:",
        *(f"    {key}: {count}" for key, count in runs["by_outcome_and_code"].items()),
        f"  with an incomplete history: {runs['incomplete_history']}",
        f"launches: {launches['total']} {launches['by_kind']}",
        f"  failed: {launches['failed']}",
        *(f"    {key}: {count}" for key, count in launches["failed_by_kind_and_code"].items()),
        f"unblocks: {unblocks['applied']} applied, {unblocks['refused']} refused "
        f"({unblocks['in_state_histories']} in the states' histories)",
        f"BLOCKED episodes (each needed a human): {summary['blocked_episodes']}",
        f"torn or foreign journal lines: {summary['torn_lines']}",
        "limitations:",
        *(f"  - {text}" for text in summary["limitations"]),
    ]
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tests.stop_corpus", description=__doc__)
    parser.add_argument("paths", nargs="+", type=Path, help="state directories, or their parents")
    parser.add_argument("--json", action="store_true", help=f"print the {SCHEMA} document")
    args = parser.parse_args(argv)
    reports = [read_run(d) for d in find_state_dirs(args.paths)]
    summary = summarize(reports)
    print(json.dumps(summary, indent=2, sort_keys=True) if args.json else render(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
