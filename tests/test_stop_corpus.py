"""The read-only stop corpus tool (#369, ADR 0011 D7): ``tests/stop_corpus.py``.

Synthetic runs, each a state directory as the controller leaves one: a
current state, a state moved aside, the run log of a run a new run replaced,
a state the loader refuses beside a run log that reads. The tool counts each
run id once, classified by one state file, counts each launch and each
operator action once in its own denominator, reads no reason text, opens
nothing that is not a regular file, flags what it cannot read whole, and
writes nothing.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import asdict
from pathlib import Path

import pytest

from autoforge.runlog import ExecutionRecord
from autoforge.state import AutoForgeState, save_state
from autoforge.stop_classification import ReasonSource, StopOutcome
from autoforge.transitions import Phase
from tests import stop_corpus
from tests.conftest import EPIC, ISSUE, state_tree, unloadable_state, watched_fifo


def _id(name: str) -> str:
    return f"af-20261102T000000Z-{name}"


def _state(run: str, phase: Phase, **kw) -> AutoForgeState:
    return AutoForgeState(
        run_id=_id(run),
        repository="owner/repo",
        epic_url=EPIC,
        current_issue_url=ISSUE,
        phase=phase,
        created_at="2026-11-02T00:00:00Z",
        updated_at="2026-11-02T00:00:00Z",
        **kw,
    )


def _run(
    root: Path, dirname: str, phase: Phase, *, run: str = "", filename: str = "state.json", **kw
) -> tuple[Path, AutoForgeState]:
    state_dir = root / dirname / ".autoforge"
    state_dir.mkdir(parents=True, exist_ok=True)
    state = _state(run or dirname, phase, **kw)
    save_state(state, state_dir / filename)
    return state_dir, state


def _line(record) -> bytes:
    if isinstance(record, bytes):
        return record
    if isinstance(record, ExecutionRecord):
        record = asdict(record)
    return json.dumps(record, sort_keys=True).encode()


def _journal(state_dir: Path, run_id: str, records: list, extra: bytes = b"") -> Path:
    logs = state_dir / "logs" / run_id
    logs.mkdir(parents=True)
    path = logs / "events.jsonl"
    path.write_bytes(b"\n".join(_line(r) for r in records) + b"\n" + extra)
    return path


def _agent(run_id: str, seq: int = 1, **kw) -> ExecutionRecord:
    return ExecutionRecord(run_id=run_id, seq=seq, phase="ANALYZE_EXECUTE", profile="p", **kw)


def _unblock(run_id: str, applied: bool) -> ExecutionRecord:
    return ExecutionRecord(
        run_id=run_id,
        seq=0,
        phase="BLOCKED-unblock",
        profile="(operator unblock; no agent)",
        metadata={"applied": applied},
        error="" if applied else "unblock refused: still conflicting",
    )


def _config_change(run_id: str) -> ExecutionRecord:
    return ExecutionRecord(
        run_id=run_id,
        seq=0,
        phase="REVIEW-config-change",
        profile="(operator configuration change; no agent)",
        metadata={"config_file": "autoforge.yaml"},
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
    run_id = state.run_id
    _journal(
        blocked,
        run_id,
        [
            _agent(run_id, 1, timed_out=True, exit_code=-1, error="killed", ended_by="idle"),
            _agent(run_id, 2, exit_code=1, error="exited 1"),
            _agent(run_id, 3, error="no CONTROL_RESULT"),  # exit 0: no typed cause
            _agent(run_id, 4),
            ExecutionRecord(
                run_id=run_id,
                seq=0,
                phase="REVIEW",
                timeout_seconds=60,
                exit_code=2,
                error="exit 2",
                profile="validation",
            ),
            _config_change(run_id),
            _unblock(run_id, applied=True),
            _unblock(run_id, applied=False),
            _unblock(run_id, applied=True),
        ],
    )
    dirs["blocked"] = blocked
    ready, state = _run(root, "ready", Phase.READY_FOR_MERGE)
    _journal(ready, state.run_id, [_agent(state.run_id)])
    dirs["ready"] = ready
    # Mid-workflow, its journal torn by a crash, holding another run's line and
    # one whose timeout is no number.
    running, state = _run(root, "running", Phase.REVIEW)
    bad = asdict(_agent(state.run_id, 2))
    bad["timeout_seconds"] = "60"
    _journal(
        running,
        state.run_id,
        [_agent(state.run_id), _agent("af-someone-else", 9), bad],
        extra=b'{"run_id": "torn',
    )
    dirs["running"] = running
    failed, state = _run(root, "failed", Phase.FAILED, block_reason="tests red")
    dirs["failed"] = failed  # no run log at all
    # A LOCAL run: its state does not load, its run log does.
    local, state = _run(root, "local", Phase.BLOCKED)
    path = local / "state.json"
    path.write_bytes(unloadable_state("local-mode", path.read_bytes())[0])
    run_id = state.run_id
    _journal(
        local,
        run_id,
        [
            _agent(run_id, 1, exit_code=1, error="exited 1"),
            _agent(run_id, 2),
            _unblock(run_id, True),
        ],
    )
    dirs["local"] = local
    # A torn state names no run; its run log is a run of its own.
    torn, state = _run(root, "torn", Phase.DONE)
    path = torn / "state.json"
    path.write_bytes(unloadable_state("invalid-json", path.read_bytes())[0])
    _journal(torn, state.run_id, [_agent(state.run_id)])
    dirs["torn"] = torn
    # A new standalone run replaced an earlier one; only its run log is left.
    replaced, state = _run(root, "replaced", Phase.DONE, run="replaced-new")
    _journal(replaced, state.run_id, [_agent(state.run_id)])
    old = _id("replaced-old")
    _journal(replaced, old, [_agent(old, 1), _agent(old, 2)])
    dirs["replaced"] = replaced
    # A scheduler namespace 'epic archive' retired: only the archived state.
    archived, state = _run(
        root,
        "namespace",
        Phase.BLOCKED,
        run="archived",
        filename="state.json.archived-20261101T000000Z",
    )
    _journal(archived, state.run_id, [_agent(state.run_id)])
    dirs["archived"] = archived
    return dirs


def _summary(*paths: Path) -> dict:
    return stop_corpus.summarize(stop_corpus.read_corpus(paths))


def _report(summary: dict, name: str) -> dict:
    (found,) = [r for r in summary["run_reports"] if r["run_id"] == _id(name)]
    return found


def test_the_corpus_counts_each_unit_once_and_reads_nothing_it_should_not(tmp_path):
    root = tmp_path / "corpus"
    _corpus(root)
    before = state_tree(root)
    summary = _summary(root)
    assert state_tree(root) == before  # nothing written, created or replaced

    runs = summary["runs"]
    assert runs["total"] == 9 == sum(runs["by_eligibility"].values())
    assert runs["by_state"] == {"loaded": 6, "refused": 1, "absent": 2}
    assert runs["by_eligibility"] == {
        "intentional": 2,  # READY_FOR_MERGE, DONE
        "deterministic": 1,  # REVIEW: 'resume' re-enters it
        "needs_human": 3,  # BLOCKED, FAILED, the archived BLOCKED
        "undecidable": 3,  # a LOCAL state, two runs whose state is gone
    }
    assert runs["by_outcome_and_code"] == {
        "blocked:unknown": 2,  # its reason text named 'merge-conflict': never read
        "done:done": 1,
        "failed:unknown": 1,
        "in_progress:in-progress": 1,
        "ready_for_merge:ready-for-merge": 1,
        "refused:state-invalid": 1,
        "unknown:unknown": 2,
    }
    blocked = _report(summary, "blocked")
    assert blocked["failed_launches"] == {
        "agent:execution-timeout": 1,
        "agent:execution-failed": 1,
        "agent:unknown": 1,
        "command:unknown": 1,
    }
    # An operator's line is no launch, and each is counted once.
    assert blocked["launches"] == {"agent": 4, "command": 1}
    assert blocked["operator_actions"] == {
        "configuration-change": 1,
        "unblock-applied": 2,
        "unblock-refused": 1,
    }
    assert blocked["unblock_history"] == 2 and not blocked["incomplete"]

    # The LOCAL run's state is refused, and its launches and unblock still count.
    local = _report(summary, "local")
    assert (local["state"], local["journal"], local["eligibility"]) == (
        "refused",
        "read",
        "undecidable",
    )
    assert local["launches"] == {"agent": 2} and local["operator_actions"] == {"unblock-applied": 1}
    # A run a new run replaced, and one whose state is torn: their logs count.
    for name in ("replaced-old", "torn"):
        gone = _report(summary, name)
        assert (gone["state"], gone["journal"], gone["incomplete"]) == ("absent", "read", True)
        assert gone["final"]["outcome"] == "unknown" and gone["final"]["source"] == "state"
    assert _report(summary, "replaced-old")["launches"] == {"agent": 2}
    archived = _report(summary, "archived")
    assert archived["state_file"].endswith("state.json.archived-20261101T000000Z")
    assert archived["final"]["outcome"] == "blocked" and not archived["incomplete"]

    # The torn state names no run: counted apart, never as one.
    unidentified = summary["unidentified_states"]
    assert unidentified["total"] == 1
    assert unidentified["by_outcome_and_code"] == {"refused:state-invalid": 1}

    launches = summary["launches"]
    assert launches["by_kind"] == {"agent": 13, "command": 1}
    assert launches["total"] == 14 and launches["failed"] == 5
    assert summary["operator_actions"] == {
        "total": 5,
        "by_kind": {"configuration-change": 1, "unblock-applied": 3, "unblock-refused": 1},
        "unblocks_in_state_histories": 2,
    }
    assert summary["blocked_episodes"] == {"ended_by_unblock": 3, "still_blocked": 2}
    assert summary["journal_lines"] == {"torn": 1, "foreign": 1, "malformed": 1}
    assert summary["unreadable_directories"] == []

    incomplete = {r["run_id"] for r in summary["run_reports"] if r["incomplete"]}
    assert incomplete == {_id(n) for n in ("running", "failed", "local", "torn", "replaced-old")}
    assert summary["limitations"] == list(stop_corpus.LIMITATIONS)


def test_a_run_found_twice_is_one_run_counted_once(tmp_path):
    """A directory given twice, and one copied elsewhere: each run id once, its
    launches and unblocks once, the copies counted and never added."""
    root = tmp_path / "corpus"
    dirs = _corpus(root)
    once = _summary(root)
    copy = tmp_path / "backup" / ".autoforge"
    shutil.copytree(dirs["blocked"], copy, symlinks=True)
    twice = _summary(root, dirs["blocked"], copy.parent)
    for key in ("runs", "launches", "operator_actions", "blocked_episodes", "journal_lines"):
        if key == "runs":
            assert {k: v for k, v in twice[key].items() if k != "with_copies"} == {
                k: v for k, v in once[key].items() if k != "with_copies"
            }
        else:
            assert twice[key] == once[key], key
    blocked = _report(twice, "blocked")
    assert (blocked["state_copies"], blocked["journal_copies"]) == (1, 1)
    assert len(blocked["state_dirs"]) == 2
    assert twice["runs"]["with_copies"] == 1 and once["runs"]["with_copies"] == 0


def test_a_directory_with_only_a_run_log_or_a_moved_state_is_found(tmp_path):
    logs_only = tmp_path / "a" / ".autoforge"
    _journal(logs_only, _id("orphan"), [_agent(_id("orphan"))])
    moved, _ = _run(tmp_path, "b", Phase.REVIEW, filename="state.json.corrupt-20261101T000000Z")
    # Not a state directory: a 'logs' tree that holds no run log.
    (tmp_path / "c" / "logs" / "app.log").mkdir(parents=True)
    found = stop_corpus.find_state_dirs([tmp_path])
    assert found == sorted([logs_only.resolve(), moved.resolve()])
    summary = _summary(tmp_path)
    assert {r["run_id"]: r["state"] for r in summary["run_reports"]} == {
        _id("orphan"): "absent",
        _id("b"): "loaded",
    }


def test_a_scheduler_namespace_is_reached_from_the_common_dir(tmp_path):
    common = tmp_path / ".git" / "autoforge"
    namespace = common / "scheduler" / "issues" / "7" / "state"
    namespace.mkdir(parents=True)
    save_state(_state("seven", Phase.READY_FOR_MERGE), namespace / "state.json")
    assert stop_corpus.find_state_dirs([common]) == [namespace.resolve()]


def test_a_fifo_is_refused_without_being_opened(tmp_path):
    """A FIFO at a run log, or at a state file, is never opened (a reader would
    block, and a waiting writer would be released): the run is flagged, and
    every other run is still counted."""
    root = tmp_path / "corpus"
    _corpus(root)
    fifo_dir, state = _run(root, "fifo", Phase.REVIEW)
    logs = fifo_dir / "logs" / state.run_id
    logs.mkdir(parents=True)
    with (
        watched_fifo(logs / "events.jsonl") as journal_opened,
        watched_fifo(fifo_dir / "state.json.archived-20261101T000000Z") as state_opened,
    ):
        summary = _summary(root)
    assert journal_opened == [] and state_opened == []
    fifo = _report(summary, "fifo")
    assert (fifo["journal"], fifo["incomplete"]) == ("not-regular", True)
    assert summary["runs"]["total"] == 10
    # The FIFO state names no run that can be read: counted apart.
    assert summary["unidentified_states"]["total"] == 2
    assert summary["launches"]["total"] == 14


def test_a_link_to_a_run_log_is_never_followed(tmp_path):
    elsewhere, state = _run(tmp_path / "elsewhere", "real", Phase.REVIEW)
    real = _journal(elsewhere, state.run_id, [_agent(state.run_id)])
    linked, state = _run(tmp_path / "corpus", "linked", Phase.REVIEW)
    (linked / "logs" / state.run_id).mkdir(parents=True)
    (linked / "logs" / state.run_id / "events.jsonl").symlink_to(real)
    dir_link, state = _run(tmp_path / "corpus", "dirlink", Phase.REVIEW)
    (dir_link / "logs").mkdir()
    (dir_link / "logs" / state.run_id).symlink_to(real.parent, target_is_directory=True)
    summary = _summary(tmp_path / "corpus")
    for name in ("linked", "dirlink"):
        report = _report(summary, name)
        assert (report["journal"], report["launches"], report["incomplete"]) == (
            "not-regular",
            {},
            True,
        )


def test_a_run_log_past_its_bound_is_never_held(tmp_path, monkeypatch):
    state_dir, state = _run(tmp_path, "big", Phase.REVIEW)
    _journal(state_dir, state.run_id, [_agent(state.run_id), _agent(state.run_id, 2)])
    monkeypatch.setattr(stop_corpus, "MAX_EVENT_JOURNAL_BYTES", 64)
    assert _report(_summary(tmp_path), "big")["journal"] == "too-large"

    # The bound holds on the opened file, whatever its size was when looked at.
    real_lstat = stop_corpus.SafeRoot.lstat

    def shrunk(self, relpath):
        st = real_lstat(self, relpath)
        if st is None or not relpath.endswith("events.jsonl"):
            return st
        return os.stat_result((st.st_mode, *([0] * 5), 0, 0, 0, 0))

    monkeypatch.setattr(stop_corpus.SafeRoot, "lstat", shrunk)
    assert _report(_summary(tmp_path), "big")["journal"] == "too-large"


@pytest.mark.parametrize(
    "field, value",
    [
        ("timeout_seconds", "60"),
        ("timeout_seconds", -1),
        ("timeout_seconds", True),
        ("exit_code", "1"),
        ("exit_code", 1.5),
        ("timed_out", "yes"),
        ("error", 1),
        ("ended_by", None),
        ("profile", ["(operator"]),
        ("phase", 0),
        ("metadata", ["applied"]),
        ("run_id", 7),
    ],
)
def test_a_field_of_another_type_is_malformed_never_converted(tmp_path, field, value):
    state_dir, state = _run(tmp_path, "typed", Phase.REVIEW)
    bad = asdict(_agent(state.run_id, 2))
    bad[field] = value
    _journal(state_dir, state.run_id, [_agent(state.run_id), bad])
    report = _report(_summary(tmp_path), "typed")
    assert (report["launches"], report["malformed_lines"], report["incomplete"]) == (
        {"agent": 1},
        1,
        True,
    )


def test_an_unblock_line_that_does_not_say_whether_it_applied_is_malformed(tmp_path):
    state_dir, state = _run(tmp_path, "unblock", Phase.REVIEW)
    lines = []
    for applied in ("true", None, 1):
        record = asdict(_unblock(state.run_id, True))
        record["metadata"] = {} if applied is None else {"applied": applied}
        lines.append(record)
    _journal(state_dir, state.run_id, lines)
    report = _report(_summary(tmp_path), "unblock")
    assert report["operator_actions"] == {} and report["malformed_lines"] == 3


def test_one_unreadable_run_or_directory_never_stops_the_corpus(tmp_path, monkeypatch):
    root = tmp_path / "corpus"
    dirs = _corpus(root)
    real_open, real_load = stop_corpus._open_dir, stop_corpus.load_state

    def open_dir(directory):
        if directory == dirs["ready"].resolve():
            raise PermissionError(13, "denied")
        return real_open(directory)

    def load(path, **kw):
        if path.parent == dirs["blocked"].resolve():
            raise RuntimeError("a bug")
        return real_load(path, **kw)

    monkeypatch.setattr(stop_corpus, "_open_dir", open_dir)
    monkeypatch.setattr(stop_corpus, "load_state", load)
    summary = _summary(root)
    assert summary["unreadable_directories"] == [
        {"state_dir": str(dirs["ready"].resolve()), "error": "PermissionError"}
    ]
    blocked = _report(summary, "blocked")
    # Not a StateError: classified unknown, and still the blocked run, by the
    # run id its file names, with its run log counted.
    assert blocked["final"]["outcome"] == "unknown" and blocked["state"] == "refused"
    assert blocked["launches"] == {"agent": 4, "command": 1}
    assert summary["runs"]["total"] == 8  # every run but the unreadable directory's


def test_every_outcome_has_one_eligibility():
    assert set(stop_corpus.ELIGIBILITY) == set(StopOutcome)
    assert set(stop_corpus.ELIGIBILITY.values()) == set(stop_corpus.ELIGIBILITIES)
    # A BLOCKED or FAILED run is never eligible for deterministic recovery,
    # and an UNKNOWN one is never counted as decided.
    assert stop_corpus.ELIGIBILITY[StopOutcome.BLOCKED] == "needs_human"
    assert stop_corpus.ELIGIBILITY[StopOutcome.FAILED] == "needs_human"
    assert stop_corpus.ELIGIBILITY[StopOutcome.UNKNOWN] == "undecidable"
    # A run with no state is never classified from anything but that absence.
    assert stop_corpus.NO_STATE.source is ReasonSource.STATE
    assert stop_corpus.ELIGIBILITY[stop_corpus.NO_STATE.outcome] == "undecidable"


def test_the_command_line_prints_the_summary_and_the_document(tmp_path, capsys):
    root = tmp_path / "corpus"
    _corpus(root)
    assert stop_corpus.main([str(root)]) == 0
    text = capsys.readouterr().out
    assert text.startswith("runs: 9 (each run id counted once, by one state file)")
    assert "operator actions (no launch): 5" in text
    assert "limitations:" in text and "merge-conflict: run 'resume'" not in text
    assert stop_corpus.main(["--json", str(root / "blocked"), str(root)]) == 0
    document = json.loads(capsys.readouterr().out)
    assert document["schema"] == stop_corpus.SCHEMA
    # A directory given twice (itself and through its parent) is one run.
    assert document["runs"]["total"] == 9
    assert "tests red" not in json.dumps(document)


def test_a_link_is_never_followed_to_a_state(tmp_path):
    root = tmp_path / "corpus"
    real, _ = _run(tmp_path / "elsewhere", "real", Phase.DONE)
    root.mkdir()
    (root / "link").symlink_to(real.parent, target_is_directory=True)
    assert stop_corpus.find_state_dirs([root]) == []
