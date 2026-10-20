"""The parallel EPIC scheduler and its workers, end to end (ADR 0008 D2, D9, D16; #182).

Offline and deterministic. The EPIC's graph is :class:`Graph`, the fake
``gh`` of ``tests/test_epic_graph.py`` behind the real read-only
:class:`~autoforge.github.GitHubClient`. Every worker is a real subprocess
started through ``spawn_supervised``: ``tests/scheduler_worker.py`` runs
the production ``run_worker`` with a FakeGitHub and a scripted agent of its
own, seeded from a per-issue JSON fixture, over a real checkout and a local
bare origin. Synchronization is by files (a step's ``started`` marker and
its ``gate``) observed from the scheduler's ``on_tick``; nothing sleeps for
correctness.
"""

from __future__ import annotations

import ast
import errno
import fcntl
import json
import os
import re
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from autoforge import cli, scheduler
from autoforge.config import load_config_file
from autoforge.epic_graph import GRAPH_READS, LocalFact, NodeReread, RereadStatus
from autoforge.errors import ConfigurationError, ExecutionError, LockError, StateError
from autoforge.locking import (
    ControllerLock,
    IssueRunLock,
    issue_run_lock_live,
    repository_lock_path,
)
from autoforge.run_namespace import (
    EpicRef,
    RunBinding,
    RunKey,
    issue_namespace,
    load_binding,
    write_binding,
)
from autoforge.scheduler import (
    ALLOW_MERGE_NOTICE,
    EpicScheduler,
    SchedulerInputs,
)
from autoforge.scheduler_state import (
    Admission,
    AdmissionReason,
    ClaimClassification,
    ClaimKind,
    ClassificationKind,
    EndReason,
    ExpectedIdentity,
    StateRef,
    classify,
    load_claim_file,
    load_scheduler_state,
    open_claim,
    read_namespace,
)
from autoforge.scheduler_stop import (
    STOP_SCHEMA,
    StopMode,
    StopRequest,
    request_stop,
    stop_request_path,
)
from autoforge.state import load_state
from autoforge.transitions import Phase

from . import claude_fake
from .conftest import FakeGitHub, Origin, block, connect_origin, git_repo
from .test_epic_graph import _CLIENT_WRITES, Hub, _assert_read_only

R = "owner/repo"
REPO_ID = 4242
EPIC = f"https://github.com/{R}/issues/1"
BOOTSTRAP = Path(__file__).with_name("scheduler_worker.py")
SRC = Path(__file__).resolve().parents[1] / "src" / "autoforge"
SCHEDULER_MODULES = (
    "scheduler.py",
    "worker.py",
    "executor_supervised.py",
    "scheduler_stop.py",
    "epic_commands.py",
)
DEADLINE = 120.0


def url(n: int) -> str:
    return f"https://github.com/{R}/issues/{n}"


class Graph(Hub):
    """The fake ``gh`` of #179's tests, serving EPIC #1 of ``owner/repo``."""

    def __init__(self) -> None:
        super().__init__()
        self.issues.clear()
        self.repository_ids = {R: REPO_ID}
        self.add(1, repo=R, parent=None)
        self.on_call: Callable[[str], None] | None = None

    def child(self, n: int, *blockers: int) -> None:
        self.add(n, repo=R, parent=(R, 1))
        for blocker in blockers:
            self.issue(n, R).blockers.append((R, blocker))

    def blocked(self, n: int, blocker: int) -> None:
        self.issue(n, R).blockers.append((R, blocker))

    def close_issue(self, n: int) -> None:
        self.close(n, "completed", repo=R)

    def __call__(self, req):
        if self.on_call is not None:
            self.on_call(_assert_read_only(req.command))
        return super().__call__(req)


IMPLEMENT = {"do": "implement"}
REVIEW = {"do": "review"}


class Bench:
    """A checkout, its bare origin, the graph, the worker fixtures and one configuration."""

    def __init__(self, tmp_path: Path, config: dict | None = None) -> None:
        self.tmp = tmp_path
        self.repo = git_repo(tmp_path / "repo")
        # The checkout's repository, as every 'epic' command reads it (#183); the
        # controller's git transport talks to the bare origin below, never to it.
        subprocess.run(
            ["git", "-C", str(self.repo), "remote", "add", "origin", f"https://github.com/{R}.git"],
            check=True,
        )
        (tmp_path / "origin").mkdir()
        self.origin = Origin.seeded(tmp_path / "origin", self.repo)
        self.fixtures = tmp_path / "fixtures"
        self.markers = tmp_path / "markers"
        for name in ("fixtures", "markers", "home", "opencode-data", "claude-config"):
            (tmp_path / name).mkdir()
        self.graph = Graph()
        self.config_path = tmp_path / "autoforge.json"
        self.config_path.write_text(json.dumps(config or {}), encoding="utf-8")
        self.config = load_config_file(self.config_path)
        self.root = Path(os.path.realpath(repository_lock_path(self.repo).parent.parent))
        self.lines: list[str] = []
        self.schedulers: list[EpicScheduler] = []

    def issue(self, n: int, steps=(IMPLEMENT, REVIEW), *blockers: int, **extra) -> None:
        self.graph.child(n, *blockers)
        fixture = {
            "repository": R,
            "title": f"Issue {n}",
            "origin": {"bare": str(self.origin.bare), "gh": str(self.origin.gh)},
            "home": str(self.tmp / "home"),
            "opencode_data": str(self.tmp / "opencode-data"),
            "claude_config": str(self.tmp / "claude-config"),
            "steps": list(steps),
            **extra,
        }
        (self.fixtures / f"{n}.json").write_text(json.dumps(fixture), encoding="utf-8")

    def marker(self, name: str) -> str:
        return str(self.markers / name)

    def has(self, name: str) -> bool:
        return (self.markers / name).exists()

    def open_gate(self, name: str) -> None:
        (self.markers / name).touch()

    def scheduler(self, **overrides) -> EpicScheduler:
        inputs = SchedulerInputs(
            repository=str(self.repo),
            epic_url=EPIC,
            config=self.config,
            graph=self.graph.client(),
            config_path=str(self.config_path),
            standalone_state_dir=str(self.tmp / "standalone"),
            worker_command=(sys.executable, "-P", str(BOOTSTRAP), str(self.fixtures)),
            output=self.lines.append,
            tick_seconds=0.02,
        )
        for key, value in overrides.items():
            setattr(inputs, key, value)
        self.lines.clear()  # each scheduler's own output
        made = EpicScheduler(inputs)
        self.schedulers.append(made)
        return made

    def ns(self, n: int):
        return issue_namespace(self.root, n)

    def claims(self, n: int):
        return load_claim_file(self.ns(n))

    def state(self, n: int):
        return load_state(self.ns(n).state_file)

    def admitted(self) -> list[int]:
        return [
            int(line.split("admitted issue #")[1].split(" ")[0])
            for line in self.lines
            if "admitted issue #" in line
        ]

    def classification(self, n: int):
        read = read_namespace(self.ns(n))
        return classify(read.lookup, read.claim, expected=ExpectedIdentity(REPO_ID, repository=R))

    def reap(self) -> None:
        """Wait for every worker a scheduler of this test started: none outlives the test."""
        for made in self.schedulers:
            for child in made.children:
                if child.wait(time.monotonic() + DEADLINE) is None:
                    child.signal(signal.SIGINT)
                    child.wait(time.monotonic() + DEADLINE)


@pytest.fixture
def bench(tmp_path):
    made: list[Bench] = []

    def make(config: dict | None = None) -> Bench:
        b = Bench(tmp_path, config)
        made.append(b)
        return b

    yield make
    for b in made:
        b.reap()


@pytest.fixture
def no_sigkill(monkeypatch):
    """Every signal this process sends, recorded; SIGKILL fails the test (D16.1)."""
    sent: list[tuple[int, int]] = []
    real_kill, real_killpg = os.kill, os.killpg

    def kill(pid, sig):
        assert sig != signal.SIGKILL, f"SIGKILL sent to {pid}"
        if sig:  # 0 is an existence probe: nothing is sent
            sent.append((pid, sig))
        return real_kill(pid, sig)

    def killpg(pgid, sig):
        assert sig != signal.SIGKILL, f"SIGKILL sent to group {pgid}"
        if sig:
            sent.append((-pgid, sig))
        return real_killpg(pgid, sig)

    monkeypatch.setattr(os, "kill", kill)
    monkeypatch.setattr(os, "killpg", killpg)
    return sent


def settled(b: Bench, n: int) -> ClaimClassification:
    claims = b.claims(n)
    assert claims is not None and claims.current is None, f"#{n}'s claim is still open"
    return claims.history[-1].classification


# -- bounded parallelism ------------------------------------------------------------------


def test_one_worker_at_a_time_in_ready_order_with_merge_allowed_but_never_done(bench):
    b = bench({"safety": {"allow_merge": True}})
    config_bytes = b.config_path.read_bytes()
    for n in (4, 2, 3):
        b.issue(n)
    live_counts: list[int] = []
    sched = b.scheduler(max_parallel=1, on_tick=lambda s: live_counts.append(len(s.live_issues())))
    assert sched.run() == 0
    assert sched.end_reason is EndReason.NOTHING_ADMISSIBLE
    assert b.admitted() == [2, 3, 4] and max(live_counts) == 1
    assert ALLOW_MERGE_NOTICE in "\n".join(b.lines)
    assert b.config_path.read_bytes() == config_bytes
    for n in (2, 3, 4):
        assert b.state(n).phase is Phase.READY_FOR_MERGE  # never MERGE, never UPDATE_EPIC
        assert settled(b, n) is ClaimClassification.SETTLED
        assert len(b.claims(n).history) == 1  # never relaunched
    state = load_scheduler_state(b.root, 1)
    assert state.invocation.end_reason is EndReason.NOTHING_ADMISSIBLE
    assert state.invocation.invocation_id == sched.invocation_id


def test_at_most_max_parallel_workers_are_ever_live_and_never_share_a_file(bench):
    b = bench()
    issues = (2, 3, 4, 5, 6)
    for n in issues:
        b.issue(n, ({**IMPLEMENT, "started": b.marker(f"s{n}"), "gate": b.marker(f"g{n}")}, REVIEW))
    seen: list[int] = []
    opened: set[int] = set()

    def tick(s: EpicScheduler) -> None:
        live = [n for n in issues if issue_run_lock_live(b.ns(n))]
        seen.append(len(live))
        assert len(live) <= 3
        waiting = [n for n in issues if b.has(f"s{n}") and n not in opened]
        everyone_admitted = all(b.ns(n).claim_file.exists() for n in issues)
        if len(waiting) == 3 or (waiting and everyone_admitted):
            for n in waiting:
                b.open_gate(f"g{n}")
                opened.add(n)

    sched = b.scheduler(max_parallel=3, on_tick=tick)
    assert sched.run() == 0
    assert max(seen) == 3 and sorted(b.admitted()) == list(issues)
    assert b.admitted()[:3] == [2, 3, 4]
    states = {n: b.state(n) for n in issues}
    assert all(s.phase is Phase.READY_FOR_MERGE for s in states.values())
    # Isolation: no two workers share a state file, a log or a worktree.
    assert len({b.ns(n).state_file for n in issues}) == 5
    logs = {str(c.claim.worker.log) for n in issues for c in b.claims(n).history}
    assert len(logs) == 5 and all(Path(log).is_file() for log in logs)
    worktrees = {n: load_binding(b.ns(n)).worktree_path for n in issues}
    assert len(set(worktrees.values())) == 5
    assert all(Path(path).name == str(n) for n, path in worktrees.items())
    assert len({s.run_id for s in states.values()}) == 5


def test_a_free_slot_admits_the_next_issue_while_a_slow_one_runs(bench):
    b = bench()
    b.issue(2, ({**IMPLEMENT, "started": b.marker("s2"), "gate": b.marker("g2")}, REVIEW))
    b.issue(3)
    b.issue(4)

    def tick(s: EpicScheduler) -> None:
        claims = b.claims(4)
        if claims is not None and claims.current is None:  # #4 settled while #2 waits
            assert s.live_issues() == (2,)
            b.open_gate("g2")

    sched = b.scheduler(max_parallel=2, on_tick=tick)
    assert sched.run() == 0
    assert b.admitted() == [2, 3, 4]
    for n in (2, 3, 4):
        assert b.state(n).phase is Phase.READY_FOR_MERGE
        assert settled(b, n) is ClaimClassification.SETTLED


def test_no_unbounded_mode(bench):
    b = bench()
    for value in (0, 5, -1, True):
        with pytest.raises(ConfigurationError, match="no unbounded mode"):
            b.scheduler(max_parallel=value)


# -- outcomes ------------------------------------------------------------------------------


def test_a_blocked_issue_is_settled_and_never_relaunched_while_others_continue(bench):
    b = bench()
    b.issue(2, ({"do": "blocked"},))
    b.issue(3)
    sched = b.scheduler(max_parallel=1)
    assert sched.run() == 0
    assert b.state(2).phase is Phase.BLOCKED and b.state(3).phase is Phase.READY_FOR_MERGE
    assert settled(b, 2) is ClaimClassification.SETTLED and len(b.claims(2).history) == 1
    assert sched.local_facts()[2] is LocalFact.HELD_SETTLED_NAMESPACE
    assert b.admitted() == [2, 3]


def test_a_failing_provider_is_the_engines_failure_and_the_scheduler_only_finalizes(bench):
    b = bench()
    b.issue(2, ({"do": "garbage"}, {"do": "garbage"}))
    b.issue(3)
    sched = b.scheduler(max_parallel=2)
    assert sched.run() == 0
    # The engine's own handling: no valid result, the state as it was.
    assert b.state(2).phase is Phase.ANALYZE_EXECUTE
    assert settled(b, 2) is ClaimClassification.INTERRUPTED
    assert b.claims(2).history[-1].exit_status == 1
    assert sched.local_facts()[2] is LocalFact.HELD_RESUMABLE  # not relaunched this time
    assert b.admitted() == [2, 3] and b.state(3).phase is Phase.READY_FOR_MERGE
    assert (
        "did not return a valid CONTROL_RESULT"
        in Path(b.claims(2).history[-1].claim.worker.log).read_text()
    )


def test_ready_for_merge_settles_and_its_dependant_waits_for_the_blocker_to_close(bench):
    b = bench()
    b.issue(2)
    b.issue(3, (IMPLEMENT, REVIEW), 2)
    first = b.scheduler(max_parallel=2)
    assert first.run() == 0
    assert b.admitted() == [2] and b.state(2).phase is Phase.READY_FOR_MERGE
    assert b.claims(3) is None and not b.ns(3).directory.exists()
    # Merged by a human and closed as completed: the dependant is ready.
    b.graph.close_issue(2)
    second = b.scheduler(max_parallel=2)
    assert second.run() == 0
    assert b.admitted() == [3] and b.state(3).phase is Phase.READY_FOR_MERGE
    assert second.local_facts()[2] is LocalFact.HELD_SETTLED_NAMESPACE  # never relaunched


# -- stop and Ctrl-C (D16) ------------------------------------------------------------------


def test_a_drain_returns_each_worker_at_a_step_boundary(bench, no_sigkill):
    b = bench()
    b.issue(2, ({**IMPLEMENT, "started": b.marker("s2"), "gate": b.marker("g2")}, REVIEW))
    b.issue(3)

    def tick(s: EpicScheduler) -> None:
        if b.has("s2") and not b.has("g2"):
            assert request_stop(b.root, 1, StopMode.DRAIN).written
            b.open_gate("g2")

    sched = b.scheduler(max_parallel=1, on_tick=tick)
    assert sched.run() == 0
    assert sched.end_reason is EndReason.DRAINED
    assert sched.admission.reason is AdmissionReason.STOP_REQUESTED
    assert b.state(2).phase is Phase.REVIEW  # its step done, the next never started
    assert settled(b, 2) is ClaimClassification.INTERRUPTED
    assert b.claims(3) is None and b.admitted() == [2]
    assert no_sigkill == [] and "draining" in "\n".join(b.lines)


def test_ctrl_c_tears_down_a_running_agent_and_leaves_the_state_in_progress(
    bench, tmp_path, no_sigkill
):
    home, profile = claude_fake.fake_claude(
        tmp_path, [claude_fake.init(), {"sleep": 120}], profile_name="analyze_execute"
    )
    b = bench(
        {
            "profiles": {
                "analyze_execute": {
                    "command": profile.command,
                    "options": {"permission_mode": "bypassPermissions"},
                }
            }
        }
    )
    b.issue(2, (IMPLEMENT,), real_claude=True)

    def tick(s: EpicScheduler) -> None:
        if (home / "log.json").exists() and not s._interrupting:
            s.request_interrupt()  # what the SIGINT handler does

    sched = b.scheduler(on_tick=tick)
    assert sched.run() == 130
    assert sched.end_reason is EndReason.INTERRUPTED
    assert sched.admission.reason is AdmissionReason.INTERRUPTED
    assert b.state(2).phase is Phase.ANALYZE_EXECUTE
    assert settled(b, 2) is ClaimClassification.INTERRUPTED
    assert b.claims(2).history[-1].exit_status == 130
    assert b.classification(2).kind is ClassificationKind.RESUMABLE
    # The agent was torn down by the worker's own teardown: no process names its home.
    assert not _processes_naming(str(home))
    signals = [sig for pid, sig in no_sigkill]
    assert signals == [signal.SIGINT]  # one Ctrl-C, forwarded once


def _processes_naming(text: str) -> list[int]:
    found = []
    for entry in os.listdir("/proc") if os.path.isdir("/proc") else []:
        if not entry.isdigit():
            continue
        try:
            cmdline = Path(f"/proc/{entry}/cmdline").read_bytes()
        except OSError:
            continue
        if text.encode() in cmdline:
            found.append(int(entry))
    return found


def _blocked_standalone_run(b: Bench, monkeypatch, capsys) -> str:
    """A standalone run of issue #9, BLOCKED, through the CLI on this checkout."""
    gh = FakeGitHub(R)
    gh.add_issue(url(9), "Standalone")
    gh.attach_origin(b.origin)
    from autoforge.engine import ControllerEngine
    from autoforge.providers import ProviderRegistry, ScriptedProvider

    provider = ScriptedProvider(
        lambda req: block({"phase": req.phase, "status": "blocked", "message": "stuck"})
    )
    real = ControllerEngine.__init__

    def patched(self, *a, **k):
        k.setdefault("github", gh)
        k.setdefault("providers", ProviderRegistry(overrides={"claude": provider}))
        real(self, *a, **k)
        connect_origin(self, b.origin)

    monkeypatch.setattr(ControllerEngine, "__init__", patched)
    monkeypatch.chdir(b.repo)
    sd = str(b.tmp / "standalone")
    assert cli.main(["--state-dir", sd, "run", "--epic", EPIC, "--issue", url(9)]) == 1
    capsys.readouterr()
    return sd


@pytest.mark.parametrize("how", ["second-interrupt", "exception"])
def test_a_scheduler_exit_with_a_live_worker_keeps_standalone_commands_out(
    bench, monkeypatch, capsys, no_sigkill, how
):
    b = bench()
    sd = _blocked_standalone_run(b, monkeypatch, capsys)
    stubborn = 2 if how == "second-interrupt" else 0
    b.issue(
        2,
        (
            {
                "do": "hold",
                "started": b.marker("s2"),
                "gate": b.marker("g2"),
                "stubborn": stubborn,
                "noted": b.marker("n2"),
            },
        ),
    )
    interrupts = []

    def tick(s: EpicScheduler) -> None:
        if not b.has("s2"):
            return
        if how == "exception":
            raise RuntimeError("injected: the scheduler crashed")
        if not interrupts:
            interrupts.append(1)
            s.request_interrupt()
        elif b.has("n2") and len(interrupts) == 1:
            interrupts.append(2)
            s.request_interrupt()  # the second Ctrl-C: exit at once

    sched = b.scheduler(on_tick=tick)
    started = time.monotonic()
    if how == "exception":
        with pytest.raises(RuntimeError, match="injected"):
            sched.run()
    else:
        assert sched.run() == 130
        assert "still run" in "\n".join(b.lines)
        assert time.monotonic() - started < b.config.scheduler.stop_grace_seconds
    (child,) = sched.children
    assert child.poll() is None, "the worker outlived its scheduler"
    # The worker holds L0 by inheritance: no standalone controller runs.
    with pytest.raises(LockError):
        ControllerLock(repository_lock_path(b.repo)).acquire()
    assert cli.main(["--state-dir", sd, "unblock", "--reason", "x"]) == 2
    assert "lock" in capsys.readouterr().err.lower()
    assert cli.main(["--state-dir", str(b.tmp / "other"), "run", "--epic", EPIC]
                    + ["--issue", url(9)]) == 2  # fmt: skip
    assert "lock" in capsys.readouterr().err.lower()
    assert load_state(Path(sd) / "state.json").phase is Phase.BLOCKED
    # Until the worker exits; then they run, and the claim is classifiable.
    b.open_gate("g2")
    assert child.wait(time.monotonic() + DEADLINE) is not None
    assert cli.main(["--state-dir", sd, "unblock", "--reason", "x"]) == 0
    assert b.classification(2).kind is ClassificationKind.RESUMABLE
    assert signal.SIGKILL not in [sig for _, sig in no_sigkill]
    if how == "second-interrupt":
        assert [sig for _, sig in no_sigkill] == [signal.SIGINT, signal.SIGINT]


def test_sighup_with_a_dead_terminal_still_finalizes_every_claim(bench, no_sigkill):
    b = bench()
    b.issue(2, ({"do": "hold", "started": b.marker("s2"), "gate": b.marker("never")},))
    b.issue(3, ({"do": "hold", "started": b.marker("s3"), "gate": b.marker("never")},))
    lines: list[str] = []

    def dead_terminal(line: str) -> None:
        if b.has("s2") and b.has("s3"):
            raise OSError(errno.EIO, "Input/output error")
        lines.append(line)

    def tick(s: EpicScheduler) -> None:
        if b.has("s2") and b.has("s3") and not s._interrupting:
            os.kill(os.getpid(), signal.SIGHUP)  # the terminal went away

    sched = b.scheduler(max_parallel=2, on_tick=tick, output=dead_terminal)
    assert sched.run() == 130
    assert sched.end_reason is EndReason.INTERRUPTED
    for n in (2, 3):
        assert settled(b, n) is ClaimClassification.INTERRUPTED
        assert b.classification(n).kind is ClassificationKind.RESUMABLE
    assert signal.SIGKILL not in [sig for _, sig in no_sigkill]


def test_a_stop_request_of_another_invocation_is_removed_on_the_next_tick(bench):
    b = bench()
    b.issue(2, ({**IMPLEMENT, "started": b.marker("s2"), "gate": b.marker("g2")}, REVIEW))
    path = stop_request_path(b.root, 1)
    stale = StopRequest("f" * 32, StopMode.NOW, "2026-10-19T00:00:00+00:00").to_dict()
    ticks: list[bool] = []

    def tick(s: EpicScheduler) -> None:
        if not ticks:
            path.write_text(json.dumps(stale))
        ticks.append(path.exists())
        if len(ticks) >= 2 and not path.exists():
            b.open_gate("g2")

    sched = b.scheduler(on_tick=tick)
    assert sched.run() == 0
    assert ticks[0] is True and ticks[1] is False  # removed by the very next tick
    assert b.state(2).phase is Phase.READY_FOR_MERGE  # and not obeyed


def test_a_now_request_interrupts_like_ctrl_c(bench, no_sigkill):
    b = bench()
    b.issue(2, ({"do": "hold", "started": b.marker("s2"), "gate": b.marker("never")},))

    def tick(s: EpicScheduler) -> None:
        if b.has("s2") and not stop_request_path(b.root, 1).exists():
            assert request_stop(b.root, 1, StopMode.NOW).written

    sched = b.scheduler(on_tick=tick)
    assert sched.run() == 130
    assert sched.end_reason is EndReason.STOPPED
    assert settled(b, 2) is ClaimClassification.INTERRUPTED
    assert [sig for _, sig in no_sigkill] == [signal.SIGINT]


def _request_with_mode(invocation: str, mode: object) -> str:
    return json.dumps(
        {
            "schema": STOP_SCHEMA,
            "invocation_id": invocation,
            "mode": mode,
            "requested_at": "2026-10-19T00:00:00+00:00",
        }
    )


@pytest.mark.parametrize("mode", [[], {}], ids=["array", "object"])
def test_an_unhashable_mode_drains_the_scheduler_and_its_worker(bench, no_sigkill, mode):
    # PR #248 R1-F1: the scheduler's tick and the worker's step boundary read it,
    # and each takes it as a drain instead of raising.
    b = bench()
    b.issue(2, ({**IMPLEMENT, "started": b.marker("s2"), "gate": b.marker("g2")}, REVIEW))
    b.issue(3)
    path = stop_request_path(b.root, 1)

    def tick(s: EpicScheduler) -> None:
        if b.has("s2") and not b.has("g2"):
            path.write_text(_request_with_mode(s.invocation_id, mode))
            b.open_gate("g2")

    sched = b.scheduler(max_parallel=1, on_tick=tick)
    assert sched.run() == 0
    assert sched.end_reason is EndReason.DRAINED
    assert sched.admission.reason is AdmissionReason.STOP_REQUESTED
    assert "does not parse is a drain" in "\n".join(b.lines)
    assert b.state(2).phase is Phase.REVIEW  # the worker returned at its step boundary
    assert settled(b, 2) is ClaimClassification.INTERRUPTED
    logs = "".join(log.read_text() for log in b.ns(2).workers_dir.iterdir())
    assert "does not parse is a drain" in logs and "the scheduler is draining" in logs
    assert b.claims(3) is None and b.admitted() == [2]
    assert path.exists() and no_sigkill == []  # kept on a tick: only a start removes it


@pytest.mark.parametrize("mode", [[], {}], ids=["array", "object"])
def test_an_unhashable_mode_left_from_before_is_removed_at_start(bench, mode):
    b = bench()
    b.issue(2)
    path = stop_request_path(b.root, 1)
    path.parent.mkdir(parents=True)
    path.write_text(_request_with_mode("f" * 32, mode))
    sched = b.scheduler()
    assert sched.run() == 0
    assert "removed the stale stop-request" in "\n".join(b.lines)
    assert not path.exists()
    assert b.state(2).phase is Phase.READY_FOR_MERGE  # and not obeyed


def test_the_grace_expiring_forwards_a_second_sigint_and_exits(bench, no_sigkill):
    b = bench()
    b.issue(
        2,
        (
            {
                "do": "hold",
                "started": b.marker("s2"),
                "gate": b.marker("g2"),
                "stubborn": 2,
                "noted": b.marker("n2"),
            },
        ),
    )
    now = [0.0]

    def tick(s: EpicScheduler) -> None:
        if b.has("s2") and not s._interrupting:
            s.request_interrupt()
        elif b.has("n2"):
            now[0] += 3600  # the grace has passed

    sched = b.scheduler(on_tick=tick, clock=lambda: now[0])
    assert sched.run() == 130
    assert "expired" in "\n".join(b.lines)
    assert [sig for _, sig in no_sigkill] == [signal.SIGINT, signal.SIGINT]
    b.open_gate("g2")


# -- admission races and failures (D2.2, D2.3) ------------------------------------------------


def test_a_dependency_added_between_snapshot_and_claim_writes_no_claim(bench):
    b = bench()
    b.issue(2)
    b.graph.child(7)  # the blocker-to-be, held by a fixture that never runs
    (b.fixtures / "7.json").write_text("{}")
    reads: list[str] = []

    def on_call(endpoint: str) -> None:
        # The snapshot reads #2 through the EPIC's sub-issue listing; the
        # admission's re-read GETs it, and a blocker has been added since.
        if endpoint == f"repos/{R}/issues/2":
            reads.append(endpoint)
            if len(reads) == 1:
                b.graph.blocked(2, 7)
        if endpoint.startswith(f"repos/{R}/issues/1/sub_issues"):
            snapshots.append(endpoint)

    b.graph.on_call = on_call
    snapshots: list[str] = []
    sched = b.scheduler()
    # #7 has no dependency: it is ready too; its worker is never wanted, so it
    # is held as another invocation's settled namespace would be.
    sched._held[7] = LocalFact.HELD_SETTLED_NAMESPACE
    assert sched.run() == 0
    assert "changed since the snapshot" in "\n".join(b.lines)
    assert b.claims(2) is None  # no claim was written
    assert len(snapshots) == 2  # and a new snapshot was taken at once, which blocks it
    assert sched.local_facts().get(2) is None
    assert b.admitted() == []


def test_a_graph_that_stays_stale_is_re_read_once_per_poll_interval(bench, monkeypatch):
    # PR #248 R1-F2: three immediate re-snapshots, then one snapshot and one
    # re-read per poll interval; the stale snapshot is never re-read per tick.
    b = bench()
    b.issue(2)
    now = [0.0]
    snapshots: list[float] = []
    issue_reads: list[float] = []
    rereads: list[float] = []
    real = scheduler.reread_node

    def always_stale(client, snapshot, issue):
        found = real(client, snapshot, issue)
        rereads.append(now[0])
        return NodeReread(found.number, RereadStatus.STALE, found.node_digest, ("injected",))

    def on_call(endpoint: str) -> None:
        if endpoint == f"repos/{R}/issues/2":
            issue_reads.append(now[0])
        if endpoint.startswith(f"repos/{R}/issues/1/sub_issues"):
            snapshots.append(now[0])

    def tick(s: EpicScheduler) -> None:
        now[0] += 1.0
        if now[0] == 150.0:
            assert request_stop(b.root, 1, StopMode.DRAIN).written

    monkeypatch.setattr(scheduler, "reread_node", always_stale)
    b.graph.on_call = on_call
    sched = b.scheduler(on_tick=tick, clock=lambda: now[0], sleep=lambda seconds: None)
    interval = sched.poll_interval
    assert interval == 60 and scheduler.MAX_STALE_RESNAPSHOTS == 3
    assert sched.run() == 0
    assert sched.end_reason is EndReason.DRAINED
    expected = [0.0, 1.0, 2.0, 3.0, 3.0 + interval, 3.0 + 2 * interval]
    assert rereads == expected
    assert issue_reads == expected  # GitHub itself: one GET of the issue per re-read
    assert snapshots == expected
    assert "\n".join(b.lines).count("changed since the snapshot") == len(expected)
    assert b.claims(2) is None and b.admitted() == []


def _failing_spawn(*args, **kwargs):
    raise ExecutionError("failed to start: injected")


def test_a_failed_spawn_closes_the_claim_abandoned_and_holds_the_issue(bench):
    b = bench()
    b.issue(2)
    sched = b.scheduler(spawn=_failing_spawn)
    assert sched.run() == 0
    assert settled(b, 2) is ClaimClassification.ABANDONED
    assert sched.local_facts()[2] is LocalFact.HELD_SPAWN_FAILED
    assert not issue_run_lock_live(b.ns(2)) and not b.ns(2).state_file.exists()


def test_a_spawn_failure_while_a_child_holds_l2_leaves_the_claim_open(bench):
    b = bench()
    b.issue(2)
    holders: list[subprocess.Popen] = []

    def stand_in(argv, *, pass_fds, stdout_fd, cwd, env):
        holders.append(
            subprocess.Popen(
                [sys.executable, "-c", "import sys; sys.stdin.read()"],
                stdin=subprocess.PIPE,
                pass_fds=tuple(pass_fds),
            )
        )
        raise ExecutionError("injected after the child started")

    sched = b.scheduler(spawn=stand_in)
    try:
        assert sched.run() == 0
        claims = b.claims(2)
        assert claims is not None and claims.current is not None  # open
        assert sched.local_facts()[2] is LocalFact.OWNED
        assert issue_run_lock_live(b.ns(2))
    finally:
        for holder in holders:
            holder.communicate(b"", timeout=60)
    assert b.classification(2).kind is ClassificationKind.RESUMABLE


def test_a_failed_record_worker_leaves_the_claim_open_until_the_reap(bench, monkeypatch):
    b = bench()
    b.issue(2)

    def refused(*args, **kwargs):
        raise StateError("injected: the claim file cannot be written")

    monkeypatch.setattr(scheduler, "record_worker", refused)
    seen_open: list[bool] = []

    def tick(s: EpicScheduler) -> None:
        claims = b.claims(2)
        if s.live_issues():
            seen_open.append(claims.current is not None and claims.current.worker is None)

    sched = b.scheduler(on_tick=tick)
    assert sched.run() == 0
    assert seen_open and all(seen_open)
    assert settled(b, 2) is ClaimClassification.SETTLED
    assert b.state(2).phase is Phase.READY_FOR_MERGE


# -- resume (D14.2) ------------------------------------------------------------------------


def _crashed_scheduler_leaving_an_open_claim(b: Bench) -> None:
    """A scheduler that died, its worker interrupted after: an open claim, a state in progress."""
    b.issue(2, ({"do": "hold", "started": b.marker("s2"), "gate": b.marker("never")},))

    def tick(s: EpicScheduler) -> None:
        if b.has("s2"):
            raise RuntimeError("injected crash")

    sched = b.scheduler(on_tick=tick)
    with pytest.raises(RuntimeError):
        sched.run()
    (child,) = sched.children
    child.signal(signal.SIGINT)
    assert child.wait(time.monotonic() + DEADLINE) == 130
    claims = b.claims(2)
    assert claims.current is not None and claims.current.worker is not None
    assert b.classification(2).kind is ClassificationKind.RESUMABLE
    os.unlink(b.marker("s2"))


def _admitted_but_never_spawned(
    b: Bench, n: int, issue_id: int, repository_id: int = REPO_ID
) -> None:
    """An admission that died before its spawn: a binding and an open claim, no state."""
    ns = b.ns(n)
    write_binding(
        ns,
        RunBinding(
            run_key=RunKey(repository_id, n),
            issue_id=issue_id,
            repository_owner="owner",
            repository_name="repo",
            epic=EpicRef(1, b.graph.issue(1, R).id),
            worktree_path=str(b.tmp / f"wt{n}"),
        ),
    )
    with IssueRunLock(ns) as lock:
        open_claim(
            ns,
            lock,
            kind=ClaimKind.START,
            invocation_id="e" * 32,
            admission=Admission("a" * 64, "b" * 64, "2026-10-19T00:00:00+00:00"),
            state_ref=StateRef.absent(),
        )


def _status_probe(b: Bench, n: int) -> int:
    """``run.lock`` held shared, as ``epic status``'s probe holds it for an instant (D8.3)."""
    fd = os.open(b.ns(n).run_lock_file, os.O_RDONLY | os.O_CLOEXEC)
    fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    return fd


def test_a_ready_resumable_claim_is_superseded_and_the_run_resumes(bench):
    b = bench()
    _crashed_scheduler_leaving_an_open_claim(b)
    old = b.claims(2).current
    b.issue(2, (IMPLEMENT, REVIEW))  # the resumed run's agents
    sched = b.scheduler()
    assert sched.run() == 0
    history = b.claims(2).history
    assert [c.classification for c in history] == [
        ClaimClassification.SUPERSEDED,
        ClaimClassification.SETTLED,
    ]
    assert history[0].claim.claim_id == old.claim_id
    assert history[1].claim.kind is ClaimKind.RESUME
    assert b.state(2).phase is Phase.READY_FOR_MERGE


def test_a_resumable_claim_whose_issue_is_now_blocked_is_closed_unready(bench):
    b = bench()
    _crashed_scheduler_leaving_an_open_claim(b)
    # With a recorded worker: interrupted.
    b.graph.child(8)
    (b.fixtures / "8.json").write_text("{}")
    b.graph.blocked(2, 8)
    # Without one: an admission that died before its spawn, abandoned.
    b.graph.child(5, 8)
    _admitted_but_never_spawned(b, 5, b.graph.issue(5, R).id)
    sched = b.scheduler()
    sched._held[8] = LocalFact.HELD_SETTLED_NAMESPACE  # the blocker never runs here
    assert sched.run() == 0
    assert settled(b, 2) is ClaimClassification.INTERRUPTED
    assert settled(b, 5) is ClaimClassification.ABANDONED
    assert b.admitted() == []


def test_a_claim_a_status_probe_hid_at_startup_is_closed_unready_by_the_same_invocation(
    bench, monkeypatch
):
    """R2-F1: a run lock probed during startup reconciliation is reconciled at the snapshot."""
    b = bench()
    _crashed_scheduler_leaving_an_open_claim(b)
    b.graph.close_issue(2)  # its claim recorded a worker: interrupted
    _admitted_but_never_spawned(b, 5, 9005)  # no longer a child at all: orphaned, abandoned
    probes = [_status_probe(b, n) for n in (2, 5)]
    real = scheduler.reconcile_epic
    passes: list[tuple[object, list[tuple[int, bool]]]] = []

    def reconcile(*args, **kwargs):
        report = real(*args, **kwargs)
        passes.append(
            (kwargs.get("only"), [(i.issue_number, i.observed_live) for i in report.issues])
        )
        while probes:  # the probe ends after startup reconciliation, before the first snapshot
            os.close(probes.pop())
        return report

    monkeypatch.setattr(scheduler, "reconcile_epic", reconcile)
    sched = b.scheduler()
    assert sched.run() == 0
    assert settled(b, 2) is ClaimClassification.INTERRUPTED
    assert settled(b, 5) is ClaimClassification.ABANDONED
    assert sched.local_facts()[5] is LocalFact.HELD_ORPHANED
    assert passes == [(None, [(2, True), (5, True)]), ({2, 5}, [(2, False), (5, False)])]
    assert b.admitted() == []
    assert not any("stays held" in line for line in b.lines)


def test_a_run_lock_held_at_every_re_check_stays_owned_and_its_claim_open(bench):
    b = bench()
    b.graph.child(5)
    b.graph.close_issue(5)
    _admitted_but_never_spawned(b, 5, b.graph.issue(5, R).id)
    probe = _status_probe(b, 5)
    try:
        sched = b.scheduler()
        assert sched.run() == 0
    finally:
        os.close(probe)
    claims = b.claims(5)
    assert claims is not None and claims.current is not None, "never closed while held"
    assert sched.local_facts() == {5: LocalFact.OWNED}
    assert any(
        "issue #5: its run lock stays held by a process this scheduler did not start" in line
        for line in b.lines
    )


def test_a_claim_a_status_probe_hid_at_close_unready_is_closed_by_the_next_snapshot(
    bench, monkeypatch
):
    """R3-F1: a run lock probed only during the first close_unready is tried again."""
    b = bench()
    b.graph.child(5)
    b.graph.close_issue(5)
    _admitted_but_never_spawned(b, 5, b.graph.issue(5, R).id)
    real = EpicScheduler._close_unready
    probed: list[bool] = []
    open_after_probe: list[bool] = []

    def close_unready(self, taken):
        probe = None if probed else _status_probe(b, 5)
        probed.append(probe is not None)
        try:
            real(self, taken)
        finally:
            if probe is not None:
                os.close(probe)  # the probe ends right after the first attempt
                open_after_probe.append(b.claims(5).current is not None)

    monkeypatch.setattr(EpicScheduler, "_close_unready", close_unready)
    sched = b.scheduler()
    assert sched.run() == 0
    assert probed == [True, False]
    assert open_after_probe == [True], "refused while the probe held it"
    assert settled(b, 5) is ClaimClassification.ABANDONED
    assert sched.local_facts() == {}
    assert b.admitted() == []
    assert not any("stays held" in line for line in b.lines)


def test_a_run_lock_held_from_close_unready_on_is_re_checked_once_then_reported(bench, monkeypatch):
    """R3-F1: a holder that stays costs one more snapshot, never a wait; the next start closes."""
    b = bench()
    b.graph.child(5)
    b.graph.close_issue(5)
    _admitted_but_never_spawned(b, 5, b.graph.issue(5, R).id)
    real = EpicScheduler._close_unready
    probes: list[int] = []
    calls: list[int] = []

    def close_unready(self, taken):
        calls.append(len(calls))
        if len(calls) == 1:
            probes.append(_status_probe(b, 5))  # held from the first attempt on
        real(self, taken)

    monkeypatch.setattr(EpicScheduler, "_close_unready", close_unready)
    sched = b.scheduler()
    try:
        assert sched.run() == 0
        assert calls == [0, 1]
        claims = b.claims(5)
        assert claims is not None and claims.current is not None, "never closed while held"
        assert sched.local_facts() == {5: LocalFact.OWNED}
        assert any(
            "issue #5: its run lock stays held by a process this scheduler did not start; "
            "it was not admitted; the earlier invocation's claim stays open for the next start"
            in line
            for line in b.lines
        )
    finally:
        while probes:
            os.close(probes.pop())
    assert b.scheduler().run() == 0
    assert settled(b, 5) is ClaimClassification.ABANDONED


def test_a_close_unready_refused_after_another_issue_s_re_check_is_re_checked_too(
    bench, monkeypatch
):
    """R3-F1: each issue seen held gets its own re-check, even after another's was spent."""
    b = bench()
    b.graph.child(7)
    _admitted_but_never_spawned(b, 7, b.graph.issue(7, R).id)
    b.graph.child(5)
    _admitted_but_never_spawned(b, 5, b.graph.issue(5, R).id)
    held = _status_probe(b, 7)  # held throughout: #7's re-check is spent at the first end
    real = EpicScheduler._close_unready
    calls: list[int] = []

    def close_unready(self, taken):
        calls.append(len(calls))
        probe = _status_probe(b, 5) if len(calls) == 2 else None
        try:
            real(self, taken)
        finally:
            if probe is not None:
                os.close(probe)

    def tick(s: EpicScheduler) -> None:
        if len(calls) == 1 and b.graph.issue(5, R).state == "open":
            b.graph.close_issue(5)  # not ready from the re-check snapshot on

    monkeypatch.setattr(EpicScheduler, "_close_unready", close_unready)
    sched = b.scheduler(on_tick=tick)
    # Ready on GitHub but held here (as the reverse guard holds it): not admitted.
    sched._held[5] = LocalFact.HELD_STANDALONE_STATE
    try:
        assert sched.run() == 0
    finally:
        os.close(held)
    assert len(calls) == 3
    assert settled(b, 5) is ClaimClassification.ABANDONED
    assert b.claims(7).current is not None
    assert sched.local_facts() == {5: LocalFact.HELD_STANDALONE_STATE, 7: LocalFact.OWNED}
    assert any("issue #7: its run lock stays held" in line for line in b.lines)
    assert not any("issue #5: its run lock stays held" in line for line in b.lines)
    assert b.admitted() == []


def test_a_binding_of_another_repository_found_at_the_re_check_admits_nothing(bench, monkeypatch):
    """Global uncertainty (D14.3) a status probe hid at startup closes admission when found."""
    b = bench()
    b.issue(3)
    b.graph.child(7)
    _admitted_but_never_spawned(b, 7, b.graph.issue(7, R).id, repository_id=REPO_ID + 1)
    probes = [_status_probe(b, 7)]
    real = scheduler.reconcile_epic

    def reconcile(*args, **kwargs):
        report = real(*args, **kwargs)
        while probes:
            os.close(probes.pop())
        return report

    monkeypatch.setattr(scheduler, "reconcile_epic", reconcile)
    sched = b.scheduler()
    assert sched.run() == 1
    assert b.admitted() == []
    assert b.claims(3) is None
    assert b.claims(7).current is not None
    state = load_scheduler_state(b.root, 1)
    assert state.admission.reason is AdmissionReason.EPIC_NOT_ADMISSIBLE
    assert state.invocation.end_reason is EndReason.DEGRADED
    assert any("global uncertainty, nothing is admitted" in line for line in b.lines)


# -- structure: no second effect system, no provider branch ----------------------------------

_PROVIDER_NAMES = {"claude", "opencode", "pi", "codex", "anthropic", "openai", "scripted"}


def _tree(name: str) -> ast.Module:
    return ast.parse((SRC / name).read_text(encoding="utf-8"))


def _runtime(name: str) -> list[ast.AST]:
    """Every node of ``src/autoforge/<name>`` but those under ``if TYPE_CHECKING:``."""
    tree = _tree(name)
    typing_only = {
        id(inner)
        for node in ast.walk(tree)
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.Name)
        and node.test.id == "TYPE_CHECKING"
        for stmt in node.body
        for inner in ast.walk(stmt)
    }
    return [node for node in ast.walk(tree) if id(node) not in typing_only]


def _defined(name: str) -> set[str]:
    """The public top-level classes and functions of ``src/autoforge/<name>``."""
    return {
        node.name
        for node in _tree(name).body
        if isinstance(node, (ast.ClassDef, ast.FunctionDef)) and not node.name.startswith("_")
    }


@pytest.mark.parametrize("name", SCHEDULER_MODULES)
def test_no_github_write_effect_record_or_result_parser_in_scheduler_modules(name):
    effect_types = _defined("effects.py") | _defined("effect_ops.py")
    parsers = _defined("result_parser.py")
    for node in _runtime(name):
        if isinstance(node, ast.Attribute):
            assert node.attr not in _CLIENT_WRITES, f"{name} calls {node.attr}"
            assert node.attr not in effect_types | parsers, f"{name} uses {node.attr}"
        if isinstance(node, ast.Name):
            assert node.id not in effect_types | parsers, f"{name} names {node.id}"
        if isinstance(node, ast.ImportFrom):
            module = (node.module or "").split(".")[-1]
            assert module not in ("effects", "effect_ops", "result_parser", "github"), (
                f"{name} imports {node.module}"
            )


def test_the_scheduler_reads_only_the_four_graph_reads():
    on_graph = {
        node.attr
        for node in ast.walk(_tree("scheduler.py"))
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "graph"
    }
    assert on_graph and on_graph <= GRAPH_READS


def _words(text: str) -> set[str]:
    return {word for word in re.split(r"[^a-z0-9]+", text.lower()) if word}


@pytest.mark.parametrize("name", SCHEDULER_MODULES)
def test_no_provider_name_in_scheduler_modules(name):
    for node in _runtime(name):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            words = _words(node.value)
        elif isinstance(node, ast.Name):
            words = _words(node.id)
        elif isinstance(node, ast.Attribute):
            words = _words(node.attr)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [alias.name for alias in node.names] + [getattr(node, "module", "") or ""]
            words = set().union(*(_words(n) for n in names))
            assert "providers" not in words, f"{name} imports a provider module"
        else:
            continue
        assert not words & _PROVIDER_NAMES, f"{name}: {sorted(words & _PROVIDER_NAMES)}"


def test_the_worker_command_is_absolute_and_keeps_the_cwd_off_the_path(tmp_path):
    command = scheduler.DEFAULT_WORKER_COMMAND
    assert os.path.isabs(command[0]) and command[1:] == ("-P", "-m", "autoforge.cli")
    # The production entry point: a checkout's own ``autoforge/`` is never imported.
    (tmp_path / "autoforge").mkdir()
    (tmp_path / "autoforge" / "__init__.py").write_text("raise SystemExit(99)\n")
    done = subprocess.run(
        [*command, "epic", "_worker", "--repository", "relative"],
        cwd=tmp_path,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert done.returncode == 2, done.stderr
    assert "refused to start, nothing was read or written" in done.stderr
    assert "not an absolute path" in done.stderr
