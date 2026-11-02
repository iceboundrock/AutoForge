"""The read-only stop corpus tool (#369, ADR 0011 D7): ``tests/stop_corpus.py``.

Synthetic runs, each a state directory as the controller saves one, with a
run log written as the controller writes its journal lines: the tool
classifies each run once by its saved state, counts each launch and each
unblock once in its own denominator, reads no reason text, flags what it
cannot read whole, and writes nothing.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from autoforge.runlog import ExecutionRecord
from autoforge.state import AutoForgeState, save_state
from autoforge.stop_classification import StopOutcome
from autoforge.transitions import Phase
from tests import stop_corpus
from tests.conftest import EPIC, ISSUE, state_tree, unloadable_state


def _run(root: Path, name: str, phase: Phase, **fields) -> tuple[Path, AutoForgeState]:
    state_dir = root / name / ".autoforge"
    state_dir.mkdir(parents=True)
    state = AutoForgeState(
        run_id=f"af-20261102T000000Z-{name}",
        repository="owner/repo",
        epic_url=EPIC,
        current_issue_url=ISSUE,
        phase=phase,
        created_at="2026-11-02T00:00:00Z",
        updated_at="2026-11-02T00:00:00Z",
        **fields,
    )
    save_state(state, state_dir / "state.json")
    return state_dir, state


def _journal(state_dir: Path, state: AutoForgeState, records: list, extra: bytes = b"") -> None:
    logs = state_dir / "logs" / state.run_id
    logs.mkdir(parents=True)
    lines = [json.dumps(asdict(r), sort_keys=True).encode() for r in records]
    (logs / "events.jsonl").write_bytes(b"\n".join(lines) + b"\n" + extra)


def _agent(state: AutoForgeState, seq: int, **kw) -> ExecutionRecord:
    return ExecutionRecord(run_id=state.run_id, seq=seq, phase="ANALYZE_EXECUTE", profile="p", **kw)


def _unblock(state: AutoForgeState, applied: bool) -> ExecutionRecord:
    return ExecutionRecord(
        run_id=state.run_id,
        seq=0,
        phase="BLOCKED-unblock",
        profile="(operator unblock; no agent)",
        metadata={"applied": applied},
        error="" if applied else "unblock refused: still conflicting",
    )


def _corpus(root: Path) -> dict[str, Path]:
    dirs: dict[str, Path] = {}
    # BLOCKED twice before, unblocked twice, blocked again; its reason names a
    # code and a route, which the tool must not read.
    blocked, state = _run(
        root,
        "blocked",
        Phase.BLOCKED,
        block_reason="merge-conflict: run 'resume'",
        unblock_history=[
            {"at": "t", "reason": "r", "block_reason": "b", "phase": "REVIEW", "detail": "d"},
            {"at": "t", "reason": "r", "block_reason": "b", "phase": "REVIEW", "detail": "d"},
        ],
    )
    _journal(
        blocked,
        state,
        [
            _agent(state, 1, timed_out=True, exit_code=-1, error="killed", ended_by="idle"),
            _agent(state, 2, exit_code=1, error="exited 1"),
            _agent(state, 3, error="no CONTROL_RESULT"),  # exit 0: no typed cause
            _agent(state, 4),
            ExecutionRecord(
                run_id=state.run_id,
                seq=0,
                phase="REVIEW",
                timeout_seconds=60,
                exit_code=2,
                error="exit 2",
                profile="validation",
            ),
            _unblock(state, applied=True),
            _unblock(state, applied=False),
            _unblock(state, applied=True),
        ],
    )
    dirs["blocked"] = blocked
    ready, state = _run(root, "ready", Phase.READY_FOR_MERGE)
    _journal(ready, state, [_agent(state, 1)])
    dirs["ready"] = ready
    # Mid-workflow, its journal torn by a crash and holding another run's line.
    running, state = _run(root, "running", Phase.REVIEW)
    foreign = _agent(state, 9)
    foreign.run_id = "af-someone-else"
    _journal(
        running,
        state,
        [_agent(state, 1), foreign],
        extra=b'{"run_id": "torn',
    )
    dirs["running"] = running
    failed, state = _run(root, "failed", Phase.FAILED, block_reason="tests red")
    dirs["failed"] = failed  # no run log at all
    local, state = _run(root, "local", Phase.BLOCKED)
    path = local / "state.json"
    path.write_bytes(unloadable_state("local-mode", path.read_bytes())[0])
    dirs["local"] = local
    torn, state = _run(root, "torn", Phase.DONE)
    path = torn / "state.json"
    path.write_bytes(unloadable_state("invalid-json", path.read_bytes())[0])
    dirs["torn"] = torn
    return dirs


def test_the_corpus_counts_each_unit_once_and_reads_nothing_it_should_not(tmp_path):
    root = tmp_path / "corpus"
    dirs = _corpus(root)
    before = state_tree(root)
    reports = [stop_corpus.read_run(d) for d in stop_corpus.find_state_dirs([root])]
    summary = stop_corpus.summarize(reports)
    assert state_tree(root) == before  # nothing written, created or replaced

    runs = summary["runs"]
    assert runs["total"] == len(dirs) == sum(runs["by_eligibility"].values())
    assert runs["by_eligibility"] == {
        "intentional": 1,  # READY_FOR_MERGE
        "deterministic": 1,  # REVIEW: 'resume' re-enters it
        "needs_human": 2,  # BLOCKED, FAILED
        "undecidable": 2,  # a LOCAL state, a torn state file
    }
    assert runs["by_outcome_and_code"] == {
        "blocked:unknown": 1,  # its reason text named 'merge-conflict': never read
        "failed:unknown": 1,
        "in_progress:in-progress": 1,
        "ready_for_merge:ready-for-merge": 1,
        "refused:state-invalid": 2,
    }
    by_dir = {r.state_dir: r for r in reports}
    blocked = by_dir[str(dirs["blocked"].resolve())]
    assert blocked.failed_launches == {
        "agent:execution-timeout": 1,
        "agent:execution-failed": 1,
        "agent:unknown": 1,
        "command:unknown": 1,
    }
    assert blocked.launches == {"agent": 4, "command": 1, "operator": 3}
    assert (blocked.unblocks_applied, blocked.unblocks_refused, blocked.unblock_history) == (
        2,
        1,
        2,
    )
    assert not blocked.incomplete

    launches = summary["launches"]
    assert launches["total"] == 4 + 1 + 3 + 1 + 1  # blocked, ready, running
    assert launches["failed"] == 4
    assert summary["unblocks"] == {"applied": 2, "refused": 1, "in_state_histories": 2}
    assert summary["blocked_episodes"] == {"ended_by_unblock": 2, "still_blocked": 1}
    assert summary["torn_lines"] == 2  # the torn tail and the other run's line

    incomplete = {Path(r.state_dir).parent.name for r in reports if r.incomplete}
    assert incomplete == {"running", "failed", "local", "torn"}
    assert summary["limitations"] == list(stop_corpus.LIMITATIONS)


def test_every_outcome_has_one_eligibility():
    assert set(stop_corpus.ELIGIBILITY) == set(StopOutcome)
    assert set(stop_corpus.ELIGIBILITY.values()) == set(stop_corpus.ELIGIBILITIES)
    # A BLOCKED or FAILED run is never eligible for deterministic recovery,
    # and an UNKNOWN one is never counted as decided.
    assert stop_corpus.ELIGIBILITY[StopOutcome.BLOCKED] == "needs_human"
    assert stop_corpus.ELIGIBILITY[StopOutcome.FAILED] == "needs_human"
    assert stop_corpus.ELIGIBILITY[StopOutcome.UNKNOWN] == "undecidable"


def test_the_command_line_prints_the_summary_and_the_document(tmp_path, capsys):
    root = tmp_path / "corpus"
    _corpus(root)
    assert stop_corpus.main([str(root)]) == 0
    text = capsys.readouterr().out
    assert text.startswith("runs: 6 (each counted once, by its saved state)")
    assert "limitations:" in text and "merge-conflict: run 'resume'" not in text
    assert stop_corpus.main(["--json", str(root / "blocked"), str(root)]) == 0
    document = json.loads(capsys.readouterr().out)
    assert document["schema"] == stop_corpus.SCHEMA
    # A directory given twice (itself and through its parent) is one run.
    assert document["runs"]["total"] == 6
    assert "tests red" not in json.dumps(document)


def test_a_link_is_never_followed_to_a_state(tmp_path):
    root = tmp_path / "corpus"
    real, _ = _run(tmp_path / "elsewhere", "real", Phase.DONE)
    root.mkdir()
    (root / "link").symlink_to(real.parent, target_is_directory=True)
    assert stop_corpus.find_state_dirs([root]) == []
