"""Every crash window of ADR 0008 D14.5, recovered with ``epic resume`` (#183).

The scheduler (or ``epic archive``) runs as ``autoforge`` in a process of its
own (``tests/epic_cli.py``) and is killed with SIGKILL at the exact point of
each window, while the workers it started live on. Each worker keeps its
issue's GitHub in a file (``tests/scheduler_worker.py``'s ``world``), so
the next worker sees the PR the last one opened and a test counts PRs: no
window may start a second worker for a settled run or open a second PR.
The windows inside a worker (killed mid-step, killed between a GitHub write
and the state recording it) run the scheduler in this process instead, as
``tests/test_epic_commands.py`` does. A validator attempt marker (row 10)
is the engine's own recovery, covered with ADR 0006's records in
``tests/test_validation*.py``.
"""

from __future__ import annotations

import json
import os
import pickle
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from autoforge.locking import issue_run_lock_live
from autoforge.scheduler import EpicScheduler
from autoforge.scheduler_state import ClaimClassification, ClaimKind, load_claim_file
from autoforge.state import load_state
from autoforge.transitions import Phase

from .test_epic_commands import Epic, prs, world
from .test_scheduler import BOOTSTRAP, DEADLINE, EPIC, IMPLEMENT, REVIEW, Bench, settled

EPIC_CLI = Path(__file__).with_name("epic_cli.py")


class Process:
    """``autoforge`` in a process of its own over a :class:`Bench`, crashes armed."""

    def __init__(self, b: Bench) -> None:
        self.b = b
        self.runs = 0

    def crash(self, target: str, when: str = "before", **extra: str) -> dict:
        return {"target": target, "when": when, "marker": self.b.marker(f"crash-{target}"), **extra}

    def __call__(self, *words: str, crashes: tuple[dict, ...] = ()) -> subprocess.CompletedProcess:
        b = self.b
        self.runs += 1
        graph = b.tmp / "graph.pickle"
        with open(graph, "wb") as f:
            pickle.dump(b.graph, f)
        spec = b.tmp / f"spec-{self.runs}.json"
        spec.write_text(
            json.dumps(
                {
                    "argv": ["--config", str(b.config_path), *words],
                    "cwd": str(b.repo),
                    "graph": str(graph),
                    "worker_command": [sys.executable, "-P", str(BOOTSTRAP), str(b.fixtures)],
                    "crashes": list(crashes),
                }
            ),
            encoding="utf-8",
        )
        return subprocess.run(
            [sys.executable, "-P", str(EPIC_CLI), str(spec)],
            cwd=b.repo,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=DEADLINE,
        )

    def wait_free(self, n: int) -> None:
        """Until a worker the killed scheduler left exits (its run lock is free)."""
        deadline = time.monotonic() + DEADLINE
        while issue_run_lock_live(self.b.ns(n)):
            assert time.monotonic() < deadline, f"#{n}'s worker never exited"
            time.sleep(0.05)


@pytest.fixture
def proc(tmp_path):
    made: list[Process] = []

    def make() -> Process:
        p = Process(Bench(tmp_path))
        made.append(p)
        return p

    yield make
    for p in made:  # no worker outlives the test: each holds its run lock until it exits
        for directory in (p.b.root / "autoforge" / "scheduler" / "issues").glob("*"):
            if directory.name.isdigit():
                p.wait_free(int(directory.name))


@pytest.fixture
def epic(tmp_path, monkeypatch, capsys):
    made: list[Bench] = []

    def make() -> Epic:
        b = Bench(tmp_path)
        made.append(b)
        return Epic(b, monkeypatch, capsys)

    yield make
    for b in made:
        b.reap()


def _status(p: Process) -> dict:
    done = p("epic", "status", EPIC, "--json")
    return json.loads(done.stdout)


def _row(p: Process, n: int) -> dict:
    (row,) = [r for r in _status(p)["children"] if r["number"] == n]
    return row


# -- the scheduler killed (D14.5 rows 1-7) -------------------------------------------------


@pytest.mark.parametrize(
    ("window", "target", "when"),
    [
        ("selected", "autoforge.scheduler:write_binding", "before"),
        ("binding written", "autoforge.scheduler:open_claim", "before"),
        ("claim persisted", "spawn", "before"),
        ("finalization written", "autoforge.scheduler:finalize_worker", "after"),
        ("worker settled", "autoforge.scheduler:finalize_worker", "before"),
    ],
)
def test_a_scheduler_killed_in_a_window_is_recovered_by_epic_resume(proc, window, target, when):
    p = proc()
    b = p.b
    b.issue(2, (IMPLEMENT, REVIEW), world=str(world(b, 2)))
    done = p("epic", "run", EPIC, crashes=(p.crash(target, when),))
    assert done.returncode == -signal.SIGKILL, done.stderr
    p.wait_free(2)
    claims = load_claim_file(b.ns(2))
    if window == "selected":  # nothing but the bare directory and its run.lock
        assert claims is None and not b.ns(2).binding_file.exists()
        assert sorted(x.name for x in b.ns(2).directory.iterdir()) == ["run.lock"]
        assert _row(p, 2)["category"] == "ready"
    elif window == "binding written":
        assert claims is None and b.ns(2).binding_file.exists()
        assert _row(p, 2)["category"] == "ready"
    elif window == "claim persisted":  # an open claim, never a worker
        assert claims.current is not None and claims.current.worker is None
        assert not b.ns(2).state_file.exists()
        assert _row(p, 2)["category"] == "claimed"
        refused = p("epic", "run", EPIC)
        assert refused.returncode == 2 and "epic resume" in refused.stderr
    elif window == "worker settled":  # the worker finished; its claim is still open
        assert claims.current is not None and b.state(2).phase is Phase.READY_FOR_MERGE
        assert _row(p, 2)["category"] == "claimed"
    else:  # finalized: nothing is left to recover
        assert claims.current is None and b.state(2).phase is Phase.READY_FOR_MERGE
    resumed = p("epic", "resume", EPIC)
    assert resumed.returncode == 0, resumed.stderr
    assert b.state(2).phase is Phase.READY_FOR_MERGE and len(prs(b, 2)) == 1
    history = [c.classification for c in load_claim_file(b.ns(2)).history]
    expected = {
        "selected": [ClaimClassification.SETTLED],
        "binding written": [ClaimClassification.SETTLED],
        "claim persisted": [ClaimClassification.SUPERSEDED, ClaimClassification.SETTLED],
        "worker settled": [ClaimClassification.SETTLED],  # finalized, never relaunched
        "finalization written": [ClaimClassification.SETTLED],
    }[window]
    assert history == expected
    if window in ("worker settled", "finalization written"):
        assert "admitted issue #2" not in resumed.stderr
    assert _row(p, 2)["category"] == "ready-for-merge"


@pytest.mark.parametrize(
    ("window", "target"),
    [
        ("spawned", "autoforge.scheduler:record_worker"),
        ("worker running", "autoforge.scheduler:EpicScheduler._reap"),
    ],
)
def test_a_scheduler_killed_while_its_worker_lives_refuses_resume_until_the_worker_exits(
    proc, window, target
):
    p = proc()
    b = p.b
    gated = {**IMPLEMENT, "started": b.marker("s2"), "gate": b.marker("g2")}
    b.issue(2, (gated, REVIEW), world=str(world(b, 2)))
    crash = (
        p.crash(target, if_exists=b.marker("s2"))
        if window == "worker running"
        else (p.crash(target))
    )
    done = p("epic", "run", EPIC, crashes=(crash,))
    assert done.returncode == -signal.SIGKILL, done.stderr
    deadline = time.monotonic() + DEADLINE
    while not b.has("s2"):
        assert time.monotonic() < deadline
        time.sleep(0.02)
    # The worker lives on, holding the inherited L0 and L2: resume fails fast.
    refused = p("epic", "resume", EPIC)
    assert refused.returncode == 2 and "lock" in refused.stderr.lower()
    row = _row(p, 2)
    assert row["category"] == "running" and row["live"]
    assert (row["claim"]["worker"] is None) == (window == "spawned")
    assert _status(p)["scheduler"]["invocation"]["hint"] is None
    b.open_gate("g2")
    p.wait_free(2)
    hint = _status(p)["scheduler"]["invocation"]["hint"]
    assert hint.startswith("not ended: if no 'autoforge epic' process is running")
    resumed = p("epic", "resume", EPIC)
    assert resumed.returncode == 0, resumed.stderr
    assert "admitted issue #2" not in resumed.stderr  # settled: finalized, never relaunched
    history = load_claim_file(b.ns(2)).history
    assert [c.classification for c in history] == [ClaimClassification.SETTLED]
    assert b.state(2).phase is Phase.READY_FOR_MERGE and len(prs(b, 2)) == 1
    assert _status(p)["scheduler"]["invocation"]["ended"]


def test_a_stop_after_the_scheduler_was_killed_names_its_surviving_worker(proc):
    """SIGKILL leaves the invocation not ended: ``epic stop`` cannot call the scheduler dead."""
    p = proc()
    b = p.b
    gated = {**IMPLEMENT, "started": b.marker("s2"), "gate": b.marker("g2")}
    b.issue(2, (gated, REVIEW), world=str(world(b, 2)))
    crash = p.crash("autoforge.scheduler:EpicScheduler._reap", if_exists=b.marker("s2"))
    done = p("epic", "run", EPIC, crashes=(crash,))
    assert done.returncode == -signal.SIGKILL, done.stderr
    deadline = time.monotonic() + DEADLINE
    while not b.has("s2"):
        assert time.monotonic() < deadline
        time.sleep(0.02)
    assert not _status(p)["scheduler"]["invocation"]["ended"]
    stopped = p("epic", "stop", EPIC, "--now")
    assert stopped.returncode == 0, stopped.stderr
    out = " ".join(stopped.stdout.split())
    assert "now stop requested for scheduler invocation" in out and "which has ended" not in out
    assert (
        "workers holding their run locks: #2. If no 'autoforge epic' process of this EPIC is "
        "running, its invocation ended without finalizing (a crash or a kill) and these workers "
        "survived it: a stop request reaches each only at its next step boundary, '--now' "
        "cannot signal them, and no process is ever signalled by a recorded PID"
    ) in out
    # Nothing signalled the survivor: it drains at its next step boundary.
    b.open_gate("g2")
    p.wait_free(2)
    assert b.state(2).phase is Phase.REVIEW and len(prs(b, 2)) == 1
    assert load_claim_file(b.ns(2)).current is not None
    b.issue(2, (REVIEW,), world=str(world(b, 2)))
    resumed = p("epic", "resume", EPIC)
    assert resumed.returncode == 0, resumed.stderr
    assert b.state(2).phase is Phase.READY_FOR_MERGE and len(prs(b, 2)) == 1


# -- a worker killed (D14.5 rows 8 and 9) ---------------------------------------------------


def test_a_worker_killed_mid_step_is_resumed_by_epic_resume(epic):
    e = epic()
    b = e.b
    hold = {"do": "hold", "started": b.marker("s2"), "gate": b.marker("never")}
    b.issue(2, (hold,), world=str(world(b, 2)))
    killed: list[int] = []

    def tick(s: EpicScheduler) -> None:
        if b.has("s2") and not killed:
            (child,) = s.children
            os.kill(child.pid, signal.SIGKILL)  # the test's kill; the scheduler never sends one
            killed.append(child.pid)

    e.on_tick = tick
    assert e.run()[0] == 0 and killed
    assert settled(b, 2) is ClaimClassification.INTERRUPTED
    assert b.claims(2).history[-1].exit_status == -signal.SIGKILL
    assert e.rows()[2]["category"] == "resumable"
    b.issue(2, (IMPLEMENT, REVIEW), world=str(world(b, 2)))
    e.on_tick = None
    assert e.resume()[0] == 0
    history = b.claims(2).history
    assert [c.claim.kind for c in history] == [ClaimKind.START, ClaimKind.RESUME]
    assert b.state(2).phase is Phase.READY_FOR_MERGE and len(prs(b, 2)) == 1


def test_a_worker_killed_after_opening_its_pr_adopts_it_on_resume(epic):
    e = epic()
    b = e.b
    crash = {"method": "create_pull_request", "marker": b.marker("pr-opened")}
    b.issue(2, (IMPLEMENT, REVIEW), world=str(world(b, 2)), gh_crash=crash)
    assert e.run()[0] == 0 and b.has("pr-opened")
    (pr,) = prs(b, 2)  # on GitHub, and not yet recorded by the state
    assert load_state(b.ns(2).state_file).current_pr_url == ""
    assert b.claims(2).history[-1].exit_status == -signal.SIGKILL
    b.issue(2, (REVIEW,), world=str(world(b, 2)))
    assert e.resume()[0] == 0
    state = b.state(2)
    assert state.phase is Phase.READY_FOR_MERGE and state.current_pr_url == pr.url
    assert len(prs(b, 2)) == 1  # adopted, never opened twice


# -- epic archive killed (D14.5 rows 11 and 12) ---------------------------------------------


@pytest.mark.parametrize(
    ("window", "target", "extra"),
    [
        ("claim closed", "autoforge.epic_commands:archive_issue_state", {}),
        ("linked, not unlinked", "autoforge.safefs:SafeRoot.unlink", {"match": "state.json"}),
    ],
)
def test_an_archive_killed_in_a_window_is_completed_by_a_re_run(proc, window, target, extra):
    p = proc()
    b = p.b
    b.issue(2, (IMPLEMENT,))  # no reviewer: the worker stops with its state in REVIEW
    stuck = p("epic", "run", EPIC, crashes=(p.crash("autoforge.scheduler:finalize_worker"),))
    assert stuck.returncode == -signal.SIGKILL, stuck.stderr  # an open claim, a state in progress
    p.wait_free(2)
    assert load_claim_file(b.ns(2)).current is not None
    done = p("epic", "archive", EPIC, "2", crashes=(p.crash(target, **extra),))
    assert done.returncode == -signal.SIGKILL, done.stderr
    claims = load_claim_file(b.ns(2))
    assert claims.current is None
    assert claims.history[-1].classification is ClaimClassification.ARCHIVED
    names = sorted(x.name for x in b.ns(2).state_dir.iterdir() if x.name.startswith("state.json"))
    if window == "claim closed":  # until the re-run, the namespace classifies by its state
        assert names == ["state.json"]
        assert _row(p, 2)["category"] == "resumable"
    else:
        assert len(names) == 2 and names[0] == "state.json"
    again = p("epic", "archive", EPIC, "2")
    assert again.returncode == 0, again.stderr
    assert "closed as archived" not in again.stdout and "state was moved aside" in again.stdout
    assert not b.ns(2).state_file.exists()
    assert _row(p, 2)["category"] == "ready"
    last = p("epic", "archive", EPIC, "2")
    assert last.returncode == 0 and "nothing left to archive" in last.stdout
