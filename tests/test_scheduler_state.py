"""Scheduler claims, scheduler state and restart reconciliation (ADR 0008 D6, D7, D14; #181).

``claim.json`` and ``scheduler.json`` strict loading and bounds, the claim
operations under the issue's run lock (L2) and their one closing rule, the
pure restart classification, reconciliation and finalization, and every
crash window of #181's table. Worker stand-ins are tiny child processes that
hold L2 (and L0) through ``pass_fds``; no engine or provider is involved,
and nothing sleeps: a child holds its locks until the test closes its stdin.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import pytest

from autoforge import scheduler_state as ss
from autoforge.errors import (
    ClaimConflict,
    LockError,
    LockOrderError,
    NetworkFilesystemError,
    SchedulerStateError,
)
from autoforge.locking import ControllerLock, IssueRunLock, SharedGitLock
from autoforge.run_namespace import (
    EpicRef,
    IssueNamespace,
    RunBinding,
    RunKey,
    archive_issue_state,
    issue_namespace,
    write_binding,
)
from autoforge.safefs import SafeRoot
from autoforge.scheduler_state import (
    MAX_DETAIL_CHARS,
    MAX_HISTORY,
    Admission,
    AdmissionReason,
    AdmissionState,
    AdmissionStatus,
    ArchiveAction,
    Claim,
    ClaimClassification,
    ClaimFile,
    ClaimFileStatus,
    ClaimKind,
    Classification,
    ClassificationKind,
    EndReason,
    ExpectedIdentity,
    Invocation,
    LocalFact,
    ResumableReason,
    SchedulerConfig,
    SchedulerState,
    SnapshotSummary,
    StateRef,
    UncertainReason,
    WorkerDiagnostics,
    archive_claim,
    classify,
    close_claim,
    close_unready,
    finalize_worker,
    load_claim_file,
    load_scheduler_state,
    open_claim,
    open_claims_for_epic,
    quarantine_scheduler_state,
    read_claim_file,
    read_namespace,
    reconcile_epic,
    record_worker,
    scheduler_state_path,
    write_scheduler_state,
)
from autoforge.state import AutoForgeState, save_state
from autoforge.transitions import Phase, Settlement

TS = "2026-10-18T00:00:00+00:00"
REPO_ID = 700_001
REPOSITORY = "owner/repo"  # the binding's recorded owner/name
FOREIGN = "foreign/project"
EPIC = EpicRef(number=1, issue_id=8_001)
OTHER_EPIC = EpicRef(number=5, issue_id=8_005)
ADMISSION = Admission("a" * 64, "b" * 64, TS)
INVOCATION = "0123456789abcdef" * 2
SETTLED_PHASES = [
    Phase.BLOCKED,
    Phase.READY_FOR_MERGE,
    Phase.FAILED,
    Phase.DONE,
    Phase.MERGE,
    Phase.UPDATE_EPIC,
]
IN_PROGRESS_PHASES = [Phase.INITIALIZING, Phase.ANALYZE_EXECUTE, Phase.REVIEW, Phase.FIX]
# Holds the descriptors it was given (``pass_fds``) until its stdin ends.
HOLD = "import sys; sys.stdin.read()"


def _issue_id(n: int) -> int:
    return 9_000 + n


def _url(n: int, repository: str = REPOSITORY) -> str:
    return f"https://github.com/{repository}/issues/{n}"


@pytest.fixture
def common(tmp_path) -> Path:
    """A coordination root (a git common dir stands in as a plain directory)."""
    root = tmp_path / "common"
    root.mkdir()
    return Path(os.path.realpath(root))


def _bind(common: Path, n: int, epic: EpicRef = EPIC, repository_id: int = REPO_ID):
    ns = issue_namespace(common, n)
    write_binding(
        ns,
        RunBinding(
            run_key=RunKey(repository_id, n),
            issue_id=_issue_id(n),
            repository_owner="owner",
            repository_name="repo",
            epic=epic,
            worktree_path=f"/work/autoforge/worktrees/{n}",
            created_at=TS,
        ),
    )
    return ns


def _expected(*issues: int, repository: str | None = None) -> ExpectedIdentity:
    return ExpectedIdentity(REPO_ID, {n: _issue_id(n) for n in issues}, repository)


def _state(
    n: int,
    phase: Phase,
    *,
    epic_number: int = EPIC.number,
    repository: str = REPOSITORY,
    issue_repository: str | None = None,
    epic_repository: str | None = None,
) -> AutoForgeState:
    """A REMOTE state of issue ``n``; its issue and EPIC URLs are in ``repository`` by default."""
    return AutoForgeState(
        run_id=f"af-issue-{n}",
        repository=repository,
        epic_url=_url(epic_number, epic_repository or repository),
        current_issue_url=_url(n, issue_repository or repository),
        phase=phase,
        created_at=TS,
        updated_at=TS,
    )


def _save(ns: IssueNamespace, phase: Phase, **kw) -> None:
    with ns.state_paths().open_root(create=True) as root:
        save_state(_state(ns.issue_number, phase, **kw), ns.state_file, root=root)


def _state_ref(ns: IssueNamespace) -> StateRef:
    return StateRef.from_lookup(read_namespace(ns).lookup)  # type: ignore[arg-type]


def _open(
    ns: IssueNamespace,
    *,
    supersedes: Claim | None = None,
    kind: ClaimKind = ClaimKind.START,
    state_ref: StateRef | None = None,
    expected: ExpectedIdentity | None = None,
) -> Claim:
    with IssueRunLock(ns) as lock:
        return open_claim(
            ns,
            lock,
            kind=kind,
            invocation_id=INVOCATION,
            admission=ADMISSION,
            state_ref=_state_ref(ns) if state_ref is None else state_ref,
            supersedes=supersedes,
            expected=expected,
        )


def _worker(ns: IssueNamespace, claim: Claim) -> WorkerDiagnostics:
    return WorkerDiagnostics(
        pid=4242,
        process_start_time=123_456,
        spawned_at=TS,
        log=str(ns.workers_dir / f"{claim.generation}.log"),
    )


def _spawned(ns: IssueNamespace, claim: Claim) -> Claim:
    return record_worker(ns, claim, _worker(ns, claim))


def _close(ns: IssueNamespace, claim: Claim, classification: ClaimClassification, **kw):
    with IssueRunLock(ns) as lock:
        return close_claim(ns, lock, claim, classification, **kw)


def _classify(ns: IssueNamespace, expected: ExpectedIdentity | None = None) -> Classification:
    read = read_namespace(ns)
    return classify(read.lookup, read.claim, expected=expected)


def _claims(ns: IssueNamespace) -> ClaimFile:
    claim_file = load_claim_file(ns)
    assert claim_file is not None
    return claim_file


def _reconcile(common: Path, epic: EpicRef = EPIC, **kw) -> ss.ReconcileReport:
    kw.setdefault("expected", _expected(*range(1, 20)))
    return reconcile_epic(common, epic, **kw)


def _item(report: ss.ReconcileReport, n: int) -> ss.IssueReconciliation:
    (item,) = [i for i in report.issues if i.issue_number == n]
    return item


def _tree(root: Path) -> dict[str, bytes | None]:
    """Every entry under ``root`` (links unfollowed), with a regular file's bytes."""
    out: dict[str, bytes | None] = {}
    for base, dirs, files in os.walk(root):
        for name in dirs + files:
            path = os.path.join(base, name)
            regular = os.path.isfile(path) and not os.path.islink(path)
            out[os.path.relpath(path, root)] = Path(path).read_bytes() if regular else None
    return out


def _without(tree: dict[str, bytes | None], *names: str) -> dict[str, bytes | None]:
    return {k: v for k, v in tree.items() if not any(k.endswith(name) for name in names)}


@contextmanager
def _child_holding(*fds: int) -> Iterator[subprocess.Popen[bytes]]:
    """A worker stand-in that holds ``fds`` (L0 and L2 copies) until the block ends."""
    child = subprocess.Popen([sys.executable, "-c", HOLD], stdin=subprocess.PIPE, pass_fds=fds)
    try:
        yield child
    finally:
        assert child.stdin is not None
        child.stdin.close()
        child.wait(timeout=60)


@contextmanager
def _worker_holding_l2(ns: IssueNamespace) -> Iterator[subprocess.Popen[bytes]]:
    """The scheduler's spawn: hand L2 to one child, then close the scheduler's own copy."""
    lock = IssueRunLock(ns).acquire()
    fd = lock.handoff()
    with _child_holding(fd) as child:
        lock.release()
        yield child


def _write_raw(ns: IssueNamespace, data: object) -> None:
    ns.directory.mkdir(parents=True, exist_ok=True)
    ns.claim_file.write_text(json.dumps(data))


def _raw(ns: IssueNamespace) -> dict:
    return json.loads(ns.claim_file.read_text())


# -- issue_settlement is the scheduler's only view of a state: test_transitions.py ------


# -- claim.json: schema, strict loading, bounds ----------------------------------------


def test_a_claim_file_round_trips_and_names_its_bindings_run(common):
    ns = _bind(common, 7)
    first = _open(ns)
    first = _spawned(ns, first)
    _close(ns, first, ClaimClassification.INTERRUPTED, exit_status=-2, detail="ctrl-c")
    _save(ns, Phase.REVIEW)
    second = _open(ns, kind=ClaimKind.RESUME)
    claim_file = _claims(ns)
    assert claim_file.run_key == RunKey(REPO_ID, 7)
    assert (claim_file.issue_id, claim_file.epic) == (_issue_id(7), EPIC)
    assert claim_file.next_generation == 3 and claim_file.current == second
    assert second.state_ref == StateRef(True, "af-issue-7", Settlement.IN_PROGRESS)
    (closed,) = claim_file.history
    assert closed.claim == first and closed.claim.worker == _worker(ns, first)
    assert (closed.classification, closed.exit_status, closed.detail) == (
        ClaimClassification.INTERRUPTED,
        -2,
        "ctrl-c",
    )
    raw = _raw(ns)
    assert raw["schema"] == "autoforge.scheduler-claim/1"
    assert ClaimFile.from_dict(raw) == claim_file
    assert ClaimFile.from_dict(json.loads(json.dumps(claim_file.to_dict()))) == claim_file
    assert set(raw["history"][0]["closed"]) == {
        "at",
        "classification",
        "settlement",
        "exit_status",
        "detail",
    }


def _a_full_claim_document(common: Path) -> tuple[IssueNamespace, dict]:
    ns = _bind(common, 7)
    first = _spawned(ns, _open(ns))
    _close(ns, first, ClaimClassification.INTERRUPTED)
    _spawned(ns, _open(ns))
    return ns, _raw(ns)


@pytest.mark.parametrize(
    "where",
    [
        (),
        ("run_key",),
        ("epic",),
        ("current",),
        ("current", "admission"),
        ("current", "state_ref"),
        ("current", "worker"),
        ("history", 0),
        ("history", 0, "closed"),
    ],
)
def test_an_unknown_field_at_any_level_is_refused(common, where):
    ns, raw = _a_full_claim_document(common)
    target = raw
    for key in where:
        target = target[key]
    target["surprise"] = 1
    _write_raw(ns, raw)
    with pytest.raises(SchedulerStateError, match=r"unknown field\(s\) \['surprise'\]"):
        load_claim_file(ns)


@pytest.mark.parametrize("schema", ["autoforge.scheduler-claim/2", None, "autoforge.run-binding/1"])
def test_an_unknown_claim_schema_is_refused_as_such(common, schema):
    ns, raw = _a_full_claim_document(common)
    raw["schema"] = schema
    raw["from_the_future"] = True  # refused as a version, not as unknown fields
    _write_raw(ns, raw)
    with pytest.raises(SchedulerStateError, match="reads only autoforge.scheduler-claim/1"):
        load_claim_file(ns)


def _set(raw: dict, path: tuple, value: object) -> None:
    for key in path[:-1]:
        raw = raw[key]
    raw[path[-1]] = value


@pytest.mark.parametrize(
    ("path", "value", "message"),
    [
        (("history", 0, "closed", "detail"), "x" * (MAX_DETAIL_CHARS + 1), "over its bound"),
        (("current", "claim_id"), "Z" * 32, "lowercase hexadecimal"),
        (("current", "invocation_id"), "ab" * 17, "over its bound|lowercase hexadecimal"),
        (("current", "admission", "node_digest"), "c" * 63, "64 lowercase"),
        (("current", "generation"), True, "must be an integer"),
        (("current", "kind"), "restart", "must be one of 'start', 'resume'"),
        (("current", "created_at"), "2026-10-18T00:00:00+02:00", "UTC time"),
        (("current", "worker", "log"), "workers/2.log", "absolute path"),
        (("current", "worker", "pid"), 0, "from 1 to"),
        (("current", "worker", "process_start_time"), -1, "from 0 to"),
        (("current", "state_ref", "run_id"), "../escape", "not a run id"),
        (("current", "state_ref", "settlement"), "finished", "must be one of"),
        (("history", 0, "closed", "exit_status"), 4096, "from -255 to 255"),
        (("history", 0, "closed", "classification"), "done", "must be one of"),
        (("issue_id",), 0, "from 1 to"),
        (("next_generation",), 2**63, "from 1 to"),
    ],
)
def test_an_out_of_bound_or_mistyped_value_is_refused(common, path, value, message):
    ns, raw = _a_full_claim_document(common)
    _set(raw, path, value)
    _write_raw(ns, raw)
    with pytest.raises(SchedulerStateError, match=message):
        load_claim_file(ns)


def test_history_is_capped_at_sixteen_and_drops_the_oldest(common):
    ns = _bind(common, 7)
    for _ in range(MAX_HISTORY + 4):
        _close(ns, _open(ns), ClaimClassification.ABANDONED)
    claim_file = _claims(ns)
    assert len(claim_file.history) == MAX_HISTORY
    assert [c.claim.generation for c in claim_file.history] == list(range(5, 21))
    assert claim_file.next_generation == 21
    raw = _raw(ns)
    raw["history"].append(raw["history"][-1])
    _write_raw(ns, raw)
    with pytest.raises(SchedulerStateError, match="holds 17 claims, over 16"):
        load_claim_file(ns)


@pytest.mark.parametrize("damage", ["current not last", "history order", "same claim id", "next"])
def test_a_broken_generation_order_is_refused(common, damage):
    ns, raw = _a_full_claim_document(common)  # history [1], current 2, next 3
    if damage == "current not last":
        raw["next_generation"] = 4
        message = "not the last one allocated"
    elif damage == "history order":
        raw["history"][0]["generation"] = 2
        raw["current"]["generation"] = 2
        message = "out of ascending order"
    elif damage == "same claim id":
        raw["history"][0]["claim_id"] = raw["current"]["claim_id"]
        message = "one claim_id twice"
    else:
        raw["current"] = None
        raw["next_generation"] = 1
        message = "not above generation 1"
    _write_raw(ns, raw)
    with pytest.raises(SchedulerStateError, match=message):
        load_claim_file(ns)


@pytest.mark.parametrize(
    ("classification", "worker", "settlement", "message"),
    [
        ("abandoned", True, None, "has a recorded worker"),
        ("interrupted", False, None, "has no recorded worker"),
        ("settled", False, None, "without a settled settlement"),
        ("settled", False, "in_progress", "without a settled settlement"),
    ],
)
def test_the_loader_refuses_a_history_the_closing_rule_forbids(
    common, classification, worker, settlement, message
):
    ns, raw = _a_full_claim_document(common)
    closed = raw["history"][0]
    if not worker:
        closed["worker"] = None
    closed["closed"]["classification"] = classification
    closed["closed"]["settlement"] = settlement
    _write_raw(ns, raw)
    with pytest.raises(SchedulerStateError, match=message):
        load_claim_file(ns)


@pytest.mark.parametrize(
    "damage",
    ["garbage", "duplicate key", "nan", "not utf-8", "array", "null", "symlink", "too large"],
)
def test_a_claim_file_that_does_not_parse_is_refused_and_left_alone(common, tmp_path, damage):
    ns = _bind(common, 7)
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_text("{}")
    if damage == "garbage":
        ns.claim_file.write_text("{ not json")
    elif damage == "duplicate key":
        ns.claim_file.write_text('{"schema": "autoforge.scheduler-claim/1", "schema": "x"}')
    elif damage == "nan":
        ns.claim_file.write_text('{"schema": NaN}')
    elif damage == "not utf-8":
        ns.claim_file.write_bytes(b'{"schema": "\xff"}')
    elif damage == "array":
        ns.claim_file.write_text("[]")
    elif damage == "null":
        ns.claim_file.write_text("null")
    elif damage == "symlink":
        ns.claim_file.symlink_to(elsewhere)
    else:
        ns.claim_file.write_text(" " * (ss.MAX_CLAIM_BYTES + 1))
    before = _tree(common)
    with pytest.raises(SchedulerStateError):
        load_claim_file(ns)
    read = read_claim_file(ns)
    assert read.status is ClaimFileStatus.CORRUPT and read.reason
    assert _tree(common) == before and elsewhere.read_text() == "{}"


def test_reading_creates_nothing(common):
    before = _tree(common)
    assert load_claim_file(issue_namespace(common, 7)) is None
    assert read_claim_file(issue_namespace(common, 7)).status is ClaimFileStatus.ABSENT
    assert load_scheduler_state(common, 1) is None
    assert _tree(common) == before


# -- claim operations --------------------------------------------------------------------


def test_a_claim_is_fsynced_before_open_claim_returns_and_one_is_open_per_issue(
    common, monkeypatch
):
    ns = _bind(common, 7)
    synced: list[int] = []
    real_fsync = os.fsync

    def fsync(fd: int) -> None:
        synced.append(fd)
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fsync)
    claim = _open(ns)
    assert synced, "the temporary and its directory are fsynced inside the call"
    monkeypatch.undo()
    assert _claims(ns).current == claim and claim.generation == 1
    before = ns.claim_file.read_bytes()
    with pytest.raises(ClaimConflict, match="already holds the open claim of generation 1"):
        _open(ns)
    assert ns.claim_file.read_bytes() == before


def test_generations_are_monotonic_within_the_file(common):
    ns = _bind(common, 7)
    generations = []
    for _ in range(3):
        claim = _open(ns)
        generations.append(claim.generation)
        _close(ns, claim, ClaimClassification.ABANDONED)
    assert generations == [1, 2, 3]
    assert _claims(ns).next_generation == 4
    assert len({c.claim.claim_id for c in _claims(ns).history}) == 3


def test_supersede_is_one_atomic_write(common, monkeypatch):
    ns = _bind(common, 7)
    old = _open(ns)
    writes: list[str] = []
    real = SafeRoot.write_bytes

    def write_bytes(self, relpath, data, **kw):
        writes.append(relpath)
        return real(self, relpath, data, **kw)

    monkeypatch.setattr(SafeRoot, "write_bytes", write_bytes)
    new = _open(ns, supersedes=old, kind=ClaimKind.RESUME)
    assert writes == ["autoforge/scheduler/issues/7/claim.json"]
    claim_file = _claims(ns)
    assert claim_file.current == new and new.generation == 2
    (superseded,) = claim_file.history
    assert superseded.claim == old
    assert superseded.classification is ClaimClassification.SUPERSEDED
    assert "superseded by generation 2" in superseded.detail


def test_supersede_compares_with_the_open_claim(common):
    ns = _bind(common, 7)
    first = _open(ns)
    second = _open(ns, supersedes=first)
    before = ns.claim_file.read_bytes()
    with pytest.raises(ClaimConflict, match="holds generation 2, not the caller's generation 1"):
        _open(ns, supersedes=first)
    _close(ns, second, ClaimClassification.ABANDONED)
    with pytest.raises(ClaimConflict, match="holds no open claim"):
        _open(ns, supersedes=second)
    assert _claims(ns).current is None and len(_claims(ns).history) == 2
    assert before != ns.claim_file.read_bytes()  # only the close was written


@pytest.mark.parametrize(
    ("case", "found"),
    [
        ("blocked", "settled(terminal)"),
        ("ready for merge", "settled(ready_for_merge)"),
        ("handoff", "settled(handoff)"),
        ("corrupt state", "uncertain(state-corrupt)"),
        ("missing state", "uncertain(state-missing)"),
        ("state of another epic", "uncertain(state-mismatch)"),
        ("state of another repository", "uncertain(state-mismatch)"),
    ],
)
def test_supersede_refuses_a_settled_or_uncertain_claim_and_leaves_the_file(common, case, found):
    """D7.2: replacing an open claim requires the classification resumable, under L2."""
    ns = _bind(common, 7)
    if case == "missing state":
        _save(ns, Phase.REVIEW)  # admitted with a state: present=true
    claim = _spawned(ns, _open(ns))
    if case == "blocked":
        _save(ns, Phase.BLOCKED)
    elif case == "ready for merge":
        _save(ns, Phase.READY_FOR_MERGE)
    elif case == "handoff":
        _save(ns, Phase.MERGE)
    elif case == "corrupt state":
        ns.state_dir.mkdir()
        ns.state_file.write_text("{ not json")
    elif case == "missing state":
        ns.state_file.unlink()
    elif case == "state of another repository":
        _save(ns, Phase.REVIEW, repository=FOREIGN)  # issue 7 and EPIC 1, of foreign/project
    else:
        _save(ns, Phase.REVIEW, epic_number=OTHER_EPIC.number)
    assert str(_classify(ns, _expected(7))) == found
    before = _tree(common)
    with pytest.raises(SchedulerStateError, match=re.escape(f"is {found}")) as refused:
        _open(ns, supersedes=claim, kind=ClaimKind.RESUME, state_ref=StateRef.absent())
    assert not isinstance(refused.value, ClaimConflict)
    assert "refusing to supersede generation 1" in str(refused.value)
    assert _without(_tree(common), "run.lock") == _without(before, "run.lock")
    assert _claims(ns).current == claim and _claims(ns).history == ()


def test_supersede_replaces_a_resumable_in_progress_claim(common):
    ns = _bind(common, 7)
    claim = _spawned(ns, _open(ns))
    _save(ns, Phase.FIX)
    assert str(_classify(ns, _expected(7))) == "resumable(in-progress)"
    resumed = _open(ns, supersedes=claim, kind=ClaimKind.RESUME)
    claim_file = _claims(ns)
    assert claim_file.current == resumed and resumed.generation == 2
    assert resumed.state_ref.present and resumed.state_ref.settlement is Settlement.IN_PROGRESS
    (superseded,) = claim_file.history
    assert (superseded.claim, superseded.classification) == (
        claim,
        ClaimClassification.SUPERSEDED,
    )


def test_only_open_claim_closes_a_claim_as_superseded(common):
    ns = _bind(common, 7)
    claim = _open(ns)
    before = ns.claim_file.read_bytes()
    with pytest.raises(SchedulerStateError, match="except by the write that installs"):
        _close(ns, claim, ClaimClassification.SUPERSEDED)
    assert ns.claim_file.read_bytes() == before


def test_the_closing_rule_is_decided_on_the_claim_as_it_is_on_disk(common):
    ns = _bind(common, 7)
    claim = _open(ns)
    before = ns.claim_file.read_bytes()
    with pytest.raises(SchedulerStateError, match="no recorded worker, so it is closed abandoned"):
        _close(ns, claim, ClaimClassification.INTERRUPTED)
    assert ns.claim_file.read_bytes() == before
    spawned = _spawned(ns, claim)
    before = ns.claim_file.read_bytes()
    # The caller's stale copy without the worker does not get it abandoned.
    with pytest.raises(SchedulerStateError, match="recorded worker, so it is closed interrupted"):
        _close(ns, claim, ClaimClassification.ABANDONED)
    assert ns.claim_file.read_bytes() == before
    closed = _close(ns, spawned, ClaimClassification.INTERRUPTED)
    assert closed.claim.worker == _worker(ns, claim)


def test_a_settled_close_needs_a_settled_settlement(common):
    ns = _bind(common, 7)
    claim = _open(ns)
    for settlement in (None, Settlement.IN_PROGRESS):
        with pytest.raises(SchedulerStateError, match="without a settled settlement"):
            _close(ns, claim, ClaimClassification.SETTLED, settlement=settlement)
    assert _claims(ns).current == claim
    closed = _close(ns, claim, ClaimClassification.SETTLED, settlement=Settlement.TERMINAL)
    assert closed.settlement is Settlement.TERMINAL


def test_detail_is_redacted_and_bounded(common):
    ns = _bind(common, 7)
    secret = "ghp_" + "A" * 36
    closed = _close(
        ns, _open(ns), ClaimClassification.ABANDONED, detail=f"GITHUB_TOKEN={secret} " + "x" * 900
    )
    assert secret not in closed.detail and "REDACTED" in closed.detail
    assert len(closed.detail) == MAX_DETAIL_CHARS and closed.detail.endswith("[... truncated]")
    assert secret not in ns.claim_file.read_text()


def test_record_worker_is_compare_and_write_and_records_once(common):
    ns = _bind(common, 7)
    first = _open(ns)
    second = _open(ns, supersedes=first)
    before = ns.claim_file.read_bytes()
    with pytest.raises(ClaimConflict, match="not the caller's generation 1"):
        _spawned(ns, first)
    assert ns.claim_file.read_bytes() == before
    recorded = _spawned(ns, second)
    assert _claims(ns).current == recorded and recorded.worker is not None
    with pytest.raises(ClaimConflict, match="already records worker pid 4242"):
        _spawned(ns, recorded)


def test_a_stale_writer_holding_generation_3_cannot_close_generation_4(common):
    ns = _bind(common, 7)
    for _ in range(2):
        _close(ns, _open(ns), ClaimClassification.ABANDONED)
    third = _spawned(ns, _open(ns))
    assert third.generation == 3
    fourth = _open(ns, supersedes=third, kind=ClaimKind.RESUME)
    assert fourth.generation == 4
    _save(ns, Phase.BLOCKED)
    before = _tree(common)
    for classification in (ClaimClassification.INTERRUPTED, ClaimClassification.SETTLED):
        with pytest.raises(
            ClaimConflict, match="holds generation 4, not the caller's generation 3"
        ):
            _close(ns, third, classification, settlement=Settlement.TERMINAL)
    with IssueRunLock(ns) as lock:
        outcome = finalize_worker(ns, lock, third, 0)
    assert outcome.closed is None
    assert outcome.classification.uncertain is UncertainReason.CLAIM_CONFLICT
    with pytest.raises(ClaimConflict):
        _spawned(ns, third)
    assert _without(_tree(common), "run.lock") == _without(before, "run.lock")
    assert _claims(ns).current == fourth


def test_every_claim_write_but_the_workers_diagnostics_needs_the_issues_run_lock(common):
    ns = _bind(common, 7)
    other = _bind(common, 8)
    claim = _open(ns)
    before = _tree(common)
    not_held = IssueRunLock(ns)
    with IssueRunLock(other) as wrong:
        for lock in (not_held, wrong):
            calls = [
                lambda lock=lock: open_claim(
                    ns,
                    lock,
                    kind=ClaimKind.START,
                    invocation_id=INVOCATION,
                    admission=ADMISSION,
                    state_ref=StateRef.absent(),
                    supersedes=claim,
                ),
                lambda lock=lock: close_claim(ns, lock, claim, ClaimClassification.ABANDONED),
                lambda lock=lock: close_unready(ns, lock, claim),
                lambda lock=lock: archive_claim(ns, lock),
                lambda lock=lock: finalize_worker(ns, lock, claim, 0),
            ]
            for call in calls:
                with pytest.raises(SchedulerStateError, match=r"does not hold its run lock \(L2\)"):
                    call()
    assert _without(_tree(common), "run.lock") == _without(before, "run.lock")


def test_claim_and_scheduler_writes_are_refused_while_holding_l1(common):
    ns = _bind(common, 7)
    with IssueRunLock(ns) as lock, SharedGitLock(common):
        with pytest.raises(LockOrderError, match="while holding the shared-Git lock"):
            open_claim(
                ns,
                lock,
                kind=ClaimKind.START,
                invocation_id=INVOCATION,
                admission=ADMISSION,
                state_ref=StateRef.absent(),
            )
        with pytest.raises(LockOrderError):
            write_scheduler_state(common, _scheduler())
    assert load_claim_file(ns) is None and load_scheduler_state(common, 1) is None


@pytest.mark.parametrize(
    "problem", ["no binding", "corrupt claim", "null claim", "other run", "other epic"]
)
def test_a_claim_is_written_only_over_this_namespaces_own_file(common, problem):
    ns = issue_namespace(common, 7)
    if problem == "no binding":
        ns.directory.mkdir(parents=True)
        message = "has no binding"
    else:
        _bind(common, 7)
        if problem == "corrupt claim":
            ns.claim_file.write_text("{ not json")
            message = "not valid JSON"
        elif problem == "null claim":
            ns.claim_file.write_text("null")
            message = "holds JSON null"
        elif problem == "other run":
            other = _bind(common, 8)
            _open(other)
            ns.claim_file.write_text(other.claim_file.read_text())
            message = "names run 700001/8"
        else:
            _close(ns, _open(ns), ClaimClassification.ABANDONED)
            raw = _raw(ns)
            raw["epic"] = {"number": 5, "issue_id": 8_005}
            ns.claim_file.write_text(json.dumps(raw))
            message = "names EPIC #5"
    before = _tree(ns.directory)
    with pytest.raises(SchedulerStateError, match=message):
        _open(ns, state_ref=StateRef.absent())
    assert _without(_tree(ns.directory), "run.lock") == _without(before, "run.lock")


# -- restart classification and the crash windows of #181 ------------------------------


def test_crash_before_the_claim_leaves_nothing_to_reconcile(common):
    """Window: a ready issue selected, no claim; or L2 taken before the binding."""
    assert _reconcile(common).issues == ()
    with IssueRunLock(issue_namespace(common, 7)):
        pass  # the directory now holds only run.lock
    report = _reconcile(common)
    assert report.issues == () and report.local_facts() == {} and report.admissible
    with pytest.raises(SchedulerStateError, match="needs no classification"):
        _classify(issue_namespace(common, 7))


def test_binding_written_without_a_claim_is_fresh(common):
    ns = _bind(common, 7)
    assert _classify(ns, _expected(7)).kind is ClassificationKind.FRESH
    item = _item(_reconcile(common), 7)
    assert item.fact is None and item.classification is not None
    assert str(item.classification) == "fresh"


def test_claim_before_spawn_is_resumable_superseded_on_readmission_or_closed_unready(common):
    """Window: claim persisted, crash before the spawn (no worker, no state)."""
    ns = _bind(common, 7)
    claim = _open(ns)
    before = ns.claim_file.read_bytes()
    item = _item(_reconcile(common), 7)
    assert str(item.classification) == "resumable(not-started)" and item.fact is None
    assert item.classification is not None and item.classification.open_claim == claim
    assert ns.claim_file.read_bytes() == before, "kept open"

    # Re-admission supersedes it in one write.
    readmitted = _open(ns, supersedes=claim, kind=ClaimKind.RESUME)
    claim_file = _claims(ns)
    assert claim_file.current == readmitted
    assert [c.classification for c in claim_file.history] == [ClaimClassification.SUPERSEDED]

    # A snapshot no longer shows it ready: close_unready closes it abandoned.
    with IssueRunLock(ns) as lock:
        closed = close_unready(ns, lock, readmitted, detail="blocked by #3")
    assert closed.classification is ClaimClassification.ABANDONED
    assert closed.detail == "blocked by #3" and closed.settlement is None
    assert str(_classify(ns, _expected(7))) == "fresh"


def test_spawn_before_the_worker_is_recorded_is_live_then_follows_its_state(common):
    """Window: spawned, crash before the diagnostics are recorded."""
    ns = _bind(common, 7)
    claim = _open(ns)
    with _worker_holding_l2(ns):
        before = _tree(common)
        item = _item(_reconcile(common), 7)
        assert item.fact is LocalFact.OWNED and item.observed_live
        assert item.classification is None
        _save(ns, Phase.BLOCKED)  # the worker's engine settles before it exits
        assert _item(_reconcile(common), 7).observed_live
        assert _claims(ns).current == claim
        assert _without(_tree(common), "state.json", "state") == _without(before, "state")
    item = _item(_reconcile(common), 7)
    assert item.fact is LocalFact.HELD_SETTLED_NAMESPACE
    assert item.closed is not None and item.closed.classification is ClaimClassification.SETTLED
    assert item.closed.settlement is Settlement.TERMINAL and item.closed.claim.worker is None


def test_an_active_worker_is_never_reclassified_while_it_holds_l2(common):
    """Window: worker running."""
    ns = _bind(common, 7)
    claim = _spawned(ns, _open(ns))
    _save(ns, Phase.READY_FOR_MERGE)
    with _worker_holding_l2(ns):
        before = _tree(common)
        for _ in range(3):
            item = _item(_reconcile(common), 7)
            assert item.fact is LocalFact.OWNED and item.observed_live
            assert item.closed is None
        assert _tree(common) == before
        assert _claims(ns).current == claim


def test_a_scheduler_crash_leaves_l0_with_its_workers_then_each_is_classified_by_state(common):
    """Window: the scheduler crashed, its workers alive with L0 and L2."""
    settled, running = _bind(common, 7), _bind(common, 8)
    claims = {ns.issue_number: _spawned(ns, _open(ns)) for ns in (settled, running)}
    lock_path = common / "autoforge" / "controller.lock"
    l0 = ControllerLock(lock_path).acquire()
    l0_fd = l0.share()
    l2 = [IssueRunLock(ns).acquire() for ns in (settled, running)]
    with _child_holding(l0_fd, *(lock.handoff() for lock in l2)):
        for lock in (*l2, l0):
            lock.release()  # the scheduler is gone; its worker is not
        with pytest.raises(LockError):
            ControllerLock(lock_path).acquire()  # no new reconciler can take L0
        _save(settled, Phase.BLOCKED)
        _save(running, Phase.REVIEW)
    with ControllerLock(lock_path):
        report = _reconcile(common)
    seven, eight = _item(report, 7), _item(report, 8)
    assert seven.fact is LocalFact.HELD_SETTLED_NAMESPACE
    assert seven.closed is not None and seven.closed.claim == claims[7]
    assert eight.fact is None and str(eight.classification) == "resumable(in-progress)"
    assert _claims(running).current == claims[8], "kept open for supersession"
    assert report.local_facts() == {7: LocalFact.HELD_SETTLED_NAMESPACE}


@pytest.mark.parametrize("phase", SETTLED_PHASES)
def test_a_settled_worker_whose_claim_was_not_finalized_is_closed_settled_never_relaunched(
    common, phase
):
    """Window: worker settled, crash before finalization."""
    ns = _bind(common, 7)
    claim = _spawned(ns, _open(ns))
    _save(ns, phase)
    state_before = ns.state_file.read_bytes()
    item = _item(_reconcile(common), 7)
    expected = {
        Phase.READY_FOR_MERGE: Settlement.READY_FOR_MERGE,
        Phase.MERGE: Settlement.HANDOFF,
        Phase.UPDATE_EPIC: Settlement.HANDOFF,
    }.get(phase, Settlement.TERMINAL)
    assert item.fact is LocalFact.HELD_SETTLED_NAMESPACE
    assert str(item.classification) == f"settled({expected.value})"
    assert item.closed is not None and item.closed.claim == claim
    assert (item.closed.classification, item.closed.settlement) == (
        ClaimClassification.SETTLED,
        expected,
    )
    assert _claims(ns).current is None
    # Window: finalization written. Nothing more to do, and still never relaunched.
    again = _item(_reconcile(common), 7)
    assert again.fact is LocalFact.HELD_SETTLED_NAMESPACE and again.closed is None
    assert len(_claims(ns).history) == 1
    assert ns.state_file.read_bytes() == state_before


def test_resume_after_a_crash_keeps_the_claim_open_until_supersession_or_close_unready(common):
    """Window: worker killed mid-step (a valid in-progress state)."""
    ns = _bind(common, 7)
    claim = _spawned(ns, _open(ns))
    _save(ns, Phase.FIX)
    item = _item(_reconcile(common), 7)
    assert str(item.classification) == "resumable(in-progress)" and item.fact is None
    assert _item(_reconcile(common), 7).classification == item.classification
    assert _claims(ns).current == claim
    with IssueRunLock(ns) as lock:
        closed = close_unready(ns, lock, claim)
    assert closed.classification is ClaimClassification.INTERRUPTED
    assert closed.settlement is Settlement.IN_PROGRESS
    # With its claim closed, an in-progress state is still resumable, never fresh.
    assert str(_classify(ns, _expected(7))) == "resumable(in-progress)"


def test_close_unready_refuses_a_claim_that_is_not_resumable(common):
    ns = _bind(common, 7)
    claim = _open(ns)
    _save(ns, Phase.BLOCKED)
    with IssueRunLock(ns) as lock, pytest.raises(SchedulerStateError, match="is settled"):
        close_unready(ns, lock, claim)
    other = _bind(common, 8)
    stale = _open(other)
    _open(other, supersedes=stale)
    with IssueRunLock(other) as lock, pytest.raises(ClaimConflict):
        close_unready(other, lock, stale)
    assert _claims(ns).current == claim


def _uncertain_case(common: Path, case: str, tmp_path: Path) -> tuple[IssueNamespace, str]:
    """Build one uncertain namespace for issue 7; returns it and the expected reason."""
    ns = issue_namespace(common, 7)
    if case == "claim without binding":
        other = _bind(common, 8)
        _open(other)
        ns.directory.mkdir(parents=True)
        ns.claim_file.write_text(other.claim_file.read_text())
        return ns, "unbound"
    if case == "state without binding":
        ns.directory.mkdir(parents=True)
        _save(ns, Phase.REVIEW)
        return ns, "unbound"
    if case == "corrupt binding":
        ns.directory.mkdir(parents=True)
        ns.binding_file.write_text("{ not json")
        return ns, "binding-unreadable"
    if case == "binding of another repository":
        _bind(common, 7, repository_id=REPO_ID + 1)
        _open(ns)
        return ns, "repository-mismatch"
    _bind(common, 7)
    if case == "binding of another issue id":
        raw = json.loads(ns.binding_file.read_text())
        raw["issue_id"] = 1
        ns.binding_file.write_text(json.dumps(raw))
        return ns, "binding-mismatch"
    if case == "missing state":
        _save(ns, Phase.REVIEW)
        _open(ns)  # admitted with a state
        ns.state_file.unlink()
        return ns, "state-missing"
    _open(ns)
    if case == "corrupt state":
        ns.state_dir.mkdir()
        ns.state_file.write_text("{ not json")
        return ns, "state-corrupt"
    if case == "state of unknown protocol":
        _save(ns, Phase.REVIEW)
        raw = json.loads(ns.state_file.read_text())
        raw["protocol_version"] = "99"
        ns.state_file.write_text(json.dumps(raw))
        return ns, "state-corrupt"
    if case == "state of another issue":
        with ns.state_paths().open_root(create=True) as root:
            save_state(_state(9, Phase.REVIEW), ns.state_file, root=root)
        return ns, "state-mismatch"
    if case == "state of another epic":
        _save(ns, Phase.REVIEW, epic_number=5)
        return ns, "state-mismatch"
    if case == "state of another repository":
        _save(ns, Phase.READY_FOR_MERGE, repository=FOREIGN)  # issue 7 and EPIC 1 there
        return ns, "state-mismatch"
    raw = _raw(ns)
    if case == "corrupt claim":
        ns.claim_file.write_text("{ not json")
        return ns, "claim-corrupt"
    if case == "null claim":
        ns.claim_file.write_text("null")
        return ns, "claim-corrupt"
    if case == "unknown claim schema":
        raw["schema"] = "autoforge.scheduler-claim/2"
        reason = "claim-corrupt"
    elif case == "claim of another issue id":
        raw["issue_id"] = _issue_id(8)
        reason = "claim-mismatch"
    else:  # "claim of another epic"
        raw["epic"] = {"number": 5, "issue_id": 8_005}
        reason = "claim-epic-mismatch"
    ns.claim_file.write_text(json.dumps(raw))
    return ns, reason


UNCERTAIN_CASES = [
    "claim without binding",
    "state without binding",
    "corrupt binding",
    "binding of another repository",
    "binding of another issue id",
    "missing state",
    "corrupt state",
    "state of unknown protocol",
    "state of another issue",
    "state of another epic",
    "state of another repository",
    "corrupt claim",
    "null claim",
    "unknown claim schema",
    "claim of another issue id",
    "claim of another epic",
]


@pytest.mark.parametrize("case", UNCERTAIN_CASES)
def test_each_uncertain_trigger_keeps_its_claim_open_and_holds_the_issue(common, tmp_path, case):
    ns, reason = _uncertain_case(common, case, tmp_path)
    before = _tree(ns.directory)
    report = _reconcile(common)
    item = _item(report, 7)
    assert item.fact is LocalFact.HELD_UNCERTAIN and item.closed is None
    assert item.classification is not None and str(item.classification) == f"uncertain({reason})"
    assert item.detail
    assert _without(_tree(ns.directory), "run.lock") == _without(before, "run.lock")
    assert report.admissible is (reason != "repository-mismatch")
    assert _item(_reconcile(common), 7).classification == item.classification


def test_a_state_absent_while_no_claim_is_open_is_never_uncertain(common):
    ns = _bind(common, 7)
    _save(ns, Phase.REVIEW)
    claim = _open(ns)  # admitted with a state: present=true
    assert claim.state_ref.present
    _close(ns, claim, ClaimClassification.ABANDONED)
    ns.state_file.unlink()
    assert str(_classify(ns, _expected(7))) == "fresh"


# Which part of a state names foreign/project; the numbers are always issue 7 and EPIC 1.
FOREIGN_STATES = {
    "all of it": {"repository": FOREIGN},
    "its repository": {
        "repository": FOREIGN,
        "issue_repository": REPOSITORY,
        "epic_repository": REPOSITORY,
    },
    "its issue": {"issue_repository": FOREIGN},
    "its epic": {"epic_repository": FOREIGN},
}


@pytest.mark.parametrize("foreign", list(FOREIGN_STATES))
@pytest.mark.parametrize("phase", [Phase.READY_FOR_MERGE, Phase.BLOCKED, Phase.FIX])
def test_a_state_of_another_repository_with_matching_numbers_is_uncertain_and_keeps_its_claim(
    common, foreign, phase
):
    """An issue number names an issue of any repository: the state's repository is compared too.

    Settled or in progress, a state naming foreign/project's issue 7 and
    EPIC 1 is not owner/repo's: reconciliation and finalization keep its
    claim open, and it is neither superseded nor closed unready.
    """
    ns = _bind(common, 7)
    claim = _spawned(ns, _open(ns))
    _save(ns, phase, **FOREIGN_STATES[foreign])
    before = _tree(ns.directory)
    for expected in (None, _expected(7), _expected(7, repository=REPOSITORY)):
        found = _classify(ns, expected)
        assert str(found) == "uncertain(state-mismatch)" and found.open_claim == claim
        assert FOREIGN in found.detail
    for expected in (_expected(*range(1, 20)), _expected(*range(1, 20), repository=REPOSITORY)):
        report = _reconcile(common, expected=expected)
        item = _item(report, 7)
        assert item.fact is LocalFact.HELD_UNCERTAIN and item.closed is None
        assert str(item.classification) == "uncertain(state-mismatch)"
        assert report.admissible, "a state is the issue's uncertainty, not the scheduler's"
    with IssueRunLock(ns) as lock:
        outcome = finalize_worker(ns, lock, claim, 0, expected=_expected(7, repository=REPOSITORY))
        assert outcome.closed is None
        assert str(outcome.classification) == "uncertain(state-mismatch)"
        assert finalize_worker(ns, lock, claim, 0).closed is None
        with pytest.raises(SchedulerStateError, match=re.escape("is uncertain(state-mismatch)")):
            close_unready(ns, lock, claim)
    with pytest.raises(SchedulerStateError, match=re.escape("is uncertain(state-mismatch)")):
        _open(ns, supersedes=claim, kind=ClaimKind.RESUME, state_ref=StateRef.absent())
    assert _without(_tree(ns.directory), "run.lock") == _without(before, "run.lock")
    assert _claims(ns).current == claim and _claims(ns).history == ()


@pytest.mark.parametrize(
    ("phase", "found"),
    [(Phase.READY_FOR_MERGE, "settled(ready_for_merge)"), (Phase.FIX, "resumable(in-progress)")],
)
def test_a_renamed_repository_keeps_its_states(common, phase, found):
    """D5.1: a rename is tolerated: the binding's recorded name or the caller's live one."""
    ns = _bind(common, 7)  # recorded as owner/repo
    renamed = _expected(7, repository="new-owner/renamed")
    # Written before the rename, a state names the recorded name (GitHub folds case).
    _save(ns, phase, repository="Owner/Repo")
    for expected in (None, _expected(7), renamed):
        assert str(_classify(ns, expected)) == found
    # Written after it, a state names the live name, which only the caller knows.
    _save(ns, phase, repository="new-owner/renamed")
    assert str(_classify(ns, renamed)) == found
    assert str(_classify(ns, _expected(7, repository="NEW-OWNER/Renamed"))) == found
    assert str(_classify(ns)) == "uncertain(state-mismatch)"
    assert str(_classify(ns, _expected(7))) == "uncertain(state-mismatch)"
    # A state naming two names of the repository at once is not one the engine writes.
    _save(ns, phase, repository="new-owner/renamed", epic_repository=REPOSITORY)
    assert str(_classify(ns, renamed)) == "uncertain(state-mismatch)"


def test_after_a_rename_the_claim_operations_recognise_the_live_name(common):
    renamed = "new-owner/renamed"
    settled = _bind(common, 7)
    settled_claim = _spawned(settled, _open(settled))
    _save(settled, Phase.READY_FOR_MERGE, repository=renamed)
    resumable = _bind(common, 8)
    resumable_claim = _spawned(resumable, _open(resumable))
    _save(resumable, Phase.FIX, repository=renamed)

    report = _reconcile(common, expected=_expected(*range(1, 20), repository=renamed))
    seven = _item(report, 7)
    assert seven.fact is LocalFact.HELD_SETTLED_NAMESPACE
    assert seven.closed is not None and seven.closed.claim == settled_claim
    assert str(_item(report, 8).classification) == "resumable(in-progress)"
    assert _claims(resumable).current == resumable_claim

    # Without the live name the state is not recognised, and nothing is replaced.
    with pytest.raises(SchedulerStateError, match=re.escape("is uncertain(state-mismatch)")):
        _open(resumable, supersedes=resumable_claim, kind=ClaimKind.RESUME)
    resumed = _open(
        resumable,
        supersedes=resumable_claim,
        kind=ClaimKind.RESUME,
        expected=_expected(8, repository=renamed),
    )
    assert _claims(resumable).current == resumed
    with IssueRunLock(resumable) as lock:
        with pytest.raises(SchedulerStateError, match=re.escape("is uncertain(state-mismatch)")):
            close_unready(resumable, lock, resumed)
        closed = close_unready(resumable, lock, resumed, expected=_expected(8, repository=renamed))
    assert closed.classification is ClaimClassification.ABANDONED

    reaped = _bind(common, 9)
    reaped_claim = _spawned(reaped, _open(reaped))
    _save(reaped, Phase.BLOCKED, repository=renamed)
    with IssueRunLock(reaped) as lock:
        assert finalize_worker(reaped, lock, reaped_claim, 0).closed is None
        outcome = finalize_worker(
            reaped, lock, reaped_claim, 0, expected=_expected(9, repository=renamed)
        )
    assert outcome.closed is not None
    assert outcome.closed.classification is ClaimClassification.SETTLED


@pytest.mark.parametrize(
    "repository",
    ["", "owner", "owner/", "/repo", "owner/repo/extra", "own er/repo", "o" * 40 + "/repo", 7],
)
def test_the_expected_repository_is_an_owner_name_pair(repository):
    with pytest.raises(SchedulerStateError, match="expected repository"):
        ExpectedIdentity(REPO_ID, {}, repository)  # type: ignore[arg-type]


def test_classification_is_pure_and_total_over_its_inputs(common):
    ns = _bind(common, 7)
    _open(ns)
    _save(ns, Phase.REVIEW)
    read = read_namespace(ns)
    before = _tree(common)
    first = classify(read.lookup, read.claim, expected=_expected(7))
    assert classify(read.lookup, read.claim, expected=_expected(7)) == first
    assert _tree(common) == before
    assert first.kind is ClassificationKind.RESUMABLE
    assert first.resumable is ResumableReason.IN_PROGRESS
    with pytest.raises(SchedulerStateError):
        Classification(ClassificationKind.SETTLED, settlement=Settlement.IN_PROGRESS)
    with pytest.raises(SchedulerStateError):
        Classification(ClassificationKind.FRESH, uncertain=UncertainReason.UNBOUND)


def test_a_namespace_bound_to_another_epic_is_held_and_left_alone(common):
    ns = _bind(common, 7, epic=OTHER_EPIC)
    claim = _spawned(ns, _open(ns))
    _save(ns, Phase.BLOCKED, epic_number=OTHER_EPIC.number)
    before = _tree(common)
    six = EpicRef(number=6, issue_id=8_006)
    item = _item(_reconcile(common, six), 7)
    assert item.fact is LocalFact.HELD_OTHER_EPIC and item.closed is None
    assert "EPIC #5" in item.detail
    assert _without(_tree(common), "run.lock") == _without(before, "run.lock")
    assert _claims(ns).current == claim
    # Its own EPIC's reconciliation settles it.
    assert _item(_reconcile(common, OTHER_EPIC), 7).fact is LocalFact.HELD_SETTLED_NAMESPACE


@pytest.mark.parametrize(
    "bound",
    [OTHER_EPIC, EpicRef(number=EPIC.number, issue_id=8_999)],
    ids=["another epic", "this epic number with another id"],
)
def test_a_binding_of_another_repository_is_global_uncertainty_whatever_its_epic(common, bound):
    """D14.3: the repository is checked before the binding's EPIC is."""
    ns = _bind(common, 7, epic=bound, repository_id=REPO_ID + 1)
    claim = _open(ns)
    _bind(common, 8)
    before = _tree(common)
    report = _reconcile(common)
    item = _item(report, 7)
    assert item.fact is LocalFact.HELD_UNCERTAIN and item.closed is None
    assert item.classification is not None
    assert str(item.classification) == "uncertain(repository-mismatch)"
    assert item.classification.open_claim == claim
    assert f"repository_id {REPO_ID + 1}" in item.detail
    assert not report.admissible and report.global_problems == (item.detail,)
    assert str(_item(report, 8).classification) == "fresh"
    assert _without(_tree(common), "run.lock") == _without(before, "run.lock")
    assert _claims(ns).current == claim


def test_the_callers_own_children_are_owned_without_taking_their_lock(common):
    ns = _bind(common, 7)
    _open(ns)
    with IssueRunLock(ns):  # the scheduler's own L2 copy is still open
        item = _item(_reconcile(common, own_children={7}), 7)
    assert item.fact is LocalFact.OWNED and item.own_child and not item.observed_live


def test_reconciliation_never_spawns_never_writes_state_and_never_reaches_github(
    common, monkeypatch
):
    namespaces = [_bind(common, n) for n in (7, 8, 9)]
    for ns in namespaces:
        _spawned(ns, _open(ns))
    _save(namespaces[0], Phase.BLOCKED)
    _save(namespaces[1], Phase.REVIEW)
    states_before = {ns.issue_number: _tree(ns.state_dir) for ns in namespaces[:2]}

    def refused(*args, **kwargs):
        raise AssertionError("reconciliation started a process")

    monkeypatch.setattr(subprocess.Popen, "__init__", refused)
    monkeypatch.setattr("autoforge.executor.execute", refused)
    monkeypatch.setattr(os, "fork", refused)
    report = _reconcile(common)
    assert [i.issue_number for i in report.issues] == [7, 8, 9]
    for ns in namespaces[:2]:
        assert _tree(ns.state_dir) == states_before[ns.issue_number]
    assert not namespaces[2].state_dir.exists(), "no state is created"


def test_an_unusable_run_lock_holds_the_issue_as_uncertain(common, tmp_path):
    ns = _bind(common, 7)
    (tmp_path / "elsewhere").write_text("")
    ns.run_lock_file.symlink_to(tmp_path / "elsewhere")
    item = _item(_reconcile(common), 7)
    assert item.fact is LocalFact.HELD_UNCERTAIN
    assert item.classification is not None
    assert item.classification.uncertain is UncertainReason.RUN_LOCK_UNUSABLE


def test_a_run_lock_on_a_network_filesystem_is_a_global_problem(common, monkeypatch):
    ns = _bind(common, 7)
    claim = _open(ns)

    def acquire(self):
        raise NetworkFilesystemError(f"{self.lock_path} lies on nfs; GITHUB_TOKEN=ghp_{'A' * 36}")

    monkeypatch.setattr(IssueRunLock, "acquire", acquire)
    report = _reconcile(common)
    item = _item(report, 7)
    assert item.fact is LocalFact.HELD_UNCERTAIN and not report.admissible
    assert "lies on nfs" in report.global_problems[0]
    assert "ghp_" not in item.detail and "ghp_" not in report.global_problems[0]
    assert _claims(ns).current == claim


def test_entries_that_are_no_namespace_are_reported_and_a_listing_failure_is_global(
    common, tmp_path
):
    _bind(common, 7)
    issues = common / "autoforge" / "scheduler" / "issues"
    (issues / "notes").mkdir()
    report = _reconcile(common)
    assert [r.name for r in report.rejected] == ["notes"]
    assert report.admissible
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    (common / "autoforge" / "scheduler").rename(tmp_path / "moved")
    (common / "autoforge" / "scheduler").symlink_to(blocked, target_is_directory=True)
    report = _reconcile(common)
    assert not report.admissible and report.issues == ()
    assert "cannot be listed" in report.global_problems[0]


# -- finalization of a reaped worker ---------------------------------------------------


@pytest.mark.parametrize("phase", SETTLED_PHASES)
def test_a_reaped_worker_with_a_settled_state_closes_its_claim_settled(common, phase):
    ns = _bind(common, 7)
    claim = _spawned(ns, _open(ns))
    _save(ns, phase)
    with IssueRunLock(ns) as lock:
        outcome = finalize_worker(ns, lock, claim, 1)
    assert outcome.classification.kind is ClassificationKind.SETTLED
    assert outcome.closed is not None
    assert outcome.closed.classification is ClaimClassification.SETTLED
    assert outcome.closed.exit_status == 1, "recorded, never deciding"


@pytest.mark.parametrize("recorded", [True, False])
@pytest.mark.parametrize("state", ["in progress", "absent, never present"])
def test_a_reaped_worker_that_did_not_settle_is_interrupted_or_abandoned(common, recorded, state):
    ns = _bind(common, 7)
    claim = _open(ns)
    if recorded:
        claim = _spawned(ns, claim)
    if state == "in progress":
        _save(ns, Phase.ANALYZE_EXECUTE)
    with IssueRunLock(ns) as lock:
        outcome = finalize_worker(ns, lock, claim, 0)  # a 0 exit is not success
    assert outcome.closed is not None
    assert outcome.closed.classification is (
        ClaimClassification.INTERRUPTED if recorded else ClaimClassification.ABANDONED
    )
    assert outcome.closed.exit_status == 0
    assert _claims(ns).current is None


@pytest.mark.parametrize(
    ("case", "reason"),
    [
        ("missing state", "state-missing"),
        ("corrupt state", "state-corrupt"),
        ("state of another issue", "state-mismatch"),
        ("state of another repository", "state-mismatch"),
        ("corrupt claim", "claim-corrupt"),
    ],
)
def test_finalization_leaves_an_uncertain_claim_open(common, tmp_path, case, reason):
    ns, expected_reason = _uncertain_case(common, case, tmp_path)
    assert expected_reason == reason
    claim_file = load_claim_file(ns) if case != "corrupt claim" else None
    claim = claim_file.current if claim_file is not None else _a_claim()
    assert claim is not None
    before = _tree(ns.directory)
    with IssueRunLock(ns) as lock:
        outcome = finalize_worker(ns, lock, claim, 0)
    assert outcome.closed is None and str(outcome.classification) == f"uncertain({reason})"
    assert _without(_tree(ns.directory), "run.lock") == _without(before, "run.lock")


def _a_claim() -> Claim:
    return Claim("f" * 32, 1, INVOCATION, ClaimKind.START, ADMISSION, StateRef.absent(), TS)


# -- epic archive's claim step -----------------------------------------------------------


def test_archive_closes_an_open_claim_whatever_the_state_and_is_idempotent(common):
    ns = _bind(common, 7)
    claim = _spawned(ns, _open(ns))
    _save(ns, Phase.REVIEW)
    with IssueRunLock(ns) as lock:
        done = archive_claim(ns, lock)
        assert done.action is ArchiveAction.CLOSED
        assert done.closed is not None and done.closed.claim == claim
        assert done.closed.classification is ClaimClassification.ARCHIVED
        before = _tree(common)
        assert archive_claim(ns, lock).action is ArchiveAction.NOTHING
        assert _tree(common) == before
    # Window: epic archive crashed after archive_claim, before the state was moved.
    assert str(_classify(ns, _expected(7))) == "resumable(in-progress)"
    # Window: epic archive finished.
    with IssueRunLock(ns):
        assert archive_issue_state(ns) is not None
    assert str(_classify(ns, _expected(7))) == "fresh"
    assert _item(_reconcile(common), 7).fact is None


def test_archive_crashed_before_the_state_was_moved_classifies_by_the_state(common):
    ns = _bind(common, 7)
    _spawned(ns, _open(ns))
    _save(ns, Phase.BLOCKED)
    with IssueRunLock(ns) as lock:
        assert archive_claim(ns, lock).action is ArchiveAction.CLOSED
    item = _item(_reconcile(common), 7)
    assert item.fact is LocalFact.HELD_SETTLED_NAMESPACE and item.closed is None


@pytest.mark.parametrize("damage", ["garbage", "null", "unknown schema", "other run", "symlink"])
def test_archive_moves_a_claim_file_the_writers_refuse_aside(common, tmp_path, damage):
    ns = _bind(common, 7)
    target = tmp_path / "target"
    target.write_text("not mine")
    if damage == "garbage":
        ns.claim_file.write_text("{ not json")
    elif damage == "null":
        ns.claim_file.write_text("null")
    elif damage == "unknown schema":
        _open(ns)
        raw = _raw(ns)
        raw["schema"] = "autoforge.scheduler-claim/9"
        ns.claim_file.write_text(json.dumps(raw))
    elif damage == "other run":
        _open(ns)
        raw = _raw(ns)
        raw["issue_id"] = 1
        ns.claim_file.write_text(json.dumps(raw))
    else:
        ns.claim_file.symlink_to(target)
    original = os.readlink(ns.claim_file) if damage == "symlink" else ns.claim_file.read_bytes()
    with IssueRunLock(ns) as lock:
        moved = archive_claim(ns, lock)
        assert moved.action is ArchiveAction.MOVED_ASIDE and moved.reason
        assert moved.moved_to is not None
        assert moved.moved_to.name.startswith("claim.json.archived-")
        assert not os.path.lexists(ns.claim_file)
        kept = os.readlink(moved.moved_to) if damage == "symlink" else moved.moved_to.read_bytes()
        assert kept == original
        assert archive_claim(ns, lock).action is ArchiveAction.NOTHING
    assert target.read_text() == "not mine"
    assert str(_classify(ns, _expected(7))) == "fresh"


def test_archive_of_a_namespace_without_a_claim_changes_nothing(common):
    with IssueRunLock(issue_namespace(common, 7)) as lock:
        before = _tree(common)
        assert archive_claim(issue_namespace(common, 7), lock).action is ArchiveAction.NOTHING
        assert _tree(common) == before


# -- open-claim queries ------------------------------------------------------------------


def test_open_claims_for_epic_lists_this_epics_open_claims_and_unreadable_ones(common):
    seven, eight, nine = (_bind(common, n) for n in (7, 8, 9))
    ten = _bind(common, 10, OTHER_EPIC)
    eleven = _bind(common, 11)
    open_seven = _open(seven)
    _close(eight, _open(eight), ClaimClassification.ABANDONED)
    nine.claim_file.write_text("{ not json")
    _open(ten)
    eleven.claim_file.write_text("null")
    before = _tree(common)
    found = open_claims_for_epic(common, EPIC)
    assert [(c.issue_number, c.claim) for c in found] == [(7, open_seven), (9, None), (11, None)]
    assert found[1].problem and "holds JSON null" in found[2].problem
    assert [c.issue_number for c in open_claims_for_epic(common, OTHER_EPIC)] == [9, 10, 11]
    assert _tree(common) == before


# -- scheduler.json ----------------------------------------------------------------------


def _scheduler(**kw) -> SchedulerState:
    fields: dict = dict(
        repository_id=REPO_ID,
        repository_owner="owner",
        repository_name="repo",
        epic=EPIC,
        invocation=Invocation(INVOCATION, TS),
        config=SchedulerConfig(2, 60, 30, "c" * 64),
        admission=AdmissionState(AdmissionStatus.OPEN),
        created_at=TS,
        updated_at=TS,
    )
    fields.update(kw)
    return SchedulerState(**fields)


def test_scheduler_state_round_trips_atomically_and_holds_no_claim(common, monkeypatch):
    state = _scheduler(
        invocation=Invocation(INVOCATION, TS, TS, EndReason.DRAINED),
        admission=AdmissionState(AdmissionStatus.CLOSED, AdmissionReason.STOP_REQUESTED),
        last_snapshot=SnapshotSummary("d" * 64, TS, children=3, ready=1, blocked=2),
    )
    writes: list[str] = []
    real = SafeRoot.write_bytes

    def write_bytes(self, relpath, data, **kw):
        writes.append(relpath)
        return real(self, relpath, data, **kw)

    monkeypatch.setattr(SafeRoot, "write_bytes", write_bytes)
    written = write_scheduler_state(common, state)
    assert writes == ["autoforge/scheduler/epics/1/scheduler.json"]
    assert written == replace(state, updated_at=written.updated_at)
    assert load_scheduler_state(common, 1) == written
    path = scheduler_state_path(common, 1)
    raw = json.loads(path.read_text())
    assert raw["schema"] == "autoforge.scheduler/1"
    assert set(raw) == {
        "schema",
        "repository",
        "epic",
        "invocation",
        "config",
        "admission",
        "last_snapshot",
        "created_at",
        "updated_at",
    }
    raw["claims"] = []
    path.write_text(json.dumps(raw))
    with pytest.raises(SchedulerStateError, match=r"unknown field\(s\) \['claims'\]"):
        load_scheduler_state(common, 1)


@pytest.mark.parametrize(
    ("path", "value", "message"),
    [
        (("schema",), "autoforge.scheduler/2", "reads only autoforge.scheduler/1"),
        (("config", "max_parallel"), 5, "from 1 to 4"),
        (("config", "max_parallel"), 0, "from 1 to 4"),
        (("config", "poll_interval_seconds"), 14, "from 15 to 3600"),
        (("config", "stop_grace_seconds"), 601, "from 10 to 600"),
        (("config", "config_digest"), "nope", "64 lowercase"),
        (("admission",), {"status": "open", "reason": "interrupted"}, "takes the reason"),
        (("admission",), {"status": "closed", "reason": None}, "takes the reason"),
        (("admission",), {"status": "degraded", "reason": "interrupted"}, "takes the reason"),
        (("invocation", "end_reason"), "drained", "only one of ended_at and end_reason"),
        (("invocation", "end_reason"), None, "only one of"),
        (("invocation", "invocation_id"), "x", "32 lowercase"),
        (("repository", "owner"), "a/b", "not a GitHub owner"),
        (("last_snapshot",), {"digest": "d" * 64, "observed_at": TS, "counts": {}}, "missing"),
        (("epic", "extra"), 1, "unknown field"),
    ],
)
def test_scheduler_state_is_strict(common, path, value, message):
    write_scheduler_state(
        common, _scheduler(invocation=Invocation(INVOCATION, TS, TS, EndReason.STOPPED))
    )
    file = scheduler_state_path(common, 1)
    raw = json.loads(file.read_text())
    if path == ("invocation", "end_reason") and value is None:
        raw["invocation"]["ended_at"] = TS
    if path == ("invocation", "end_reason") and value == "drained":
        raw["invocation"]["ended_at"] = None
    _set(raw, path, value)
    file.write_text(json.dumps(raw))
    with pytest.raises(SchedulerStateError, match=message):
        load_scheduler_state(common, 1)


@pytest.mark.parametrize(
    ("content", "message"), [("{ corrupt", "not valid JSON"), ("null", "holds JSON null")]
)
def test_a_corrupt_scheduler_state_is_quarantined_only_on_request(common, content, message):
    assert quarantine_scheduler_state(common, 1) is None
    write_scheduler_state(common, _scheduler())
    with pytest.raises(SchedulerStateError, match="only a corrupt one is quarantined"):
        quarantine_scheduler_state(common, 1)
    file = scheduler_state_path(common, 1)
    file.write_text(content)
    with pytest.raises(SchedulerStateError, match=message):
        load_scheduler_state(common, 1)  # global uncertainty, until --force
    moved = quarantine_scheduler_state(common, 1)
    assert moved is not None and moved.name.startswith("scheduler.json.corrupt-")
    assert moved.read_text() == content and not file.exists()
    write_scheduler_state(common, _scheduler())
    assert load_scheduler_state(common, 1) is not None


def test_a_scheduler_state_names_its_own_epic(common):
    write_scheduler_state(common, _scheduler(epic=OTHER_EPIC))
    other = scheduler_state_path(common, 5)
    mine = scheduler_state_path(common, 1)
    mine.parent.mkdir(parents=True)
    mine.write_bytes(other.read_bytes())
    with pytest.raises(SchedulerStateError, match="names EPIC #5, not #1"):
        load_scheduler_state(common, 1)
