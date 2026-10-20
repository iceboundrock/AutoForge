"""The ``autoforge epic`` commands through the CLI (ADR 0008 D9.8, D14.4, D16.4, D17; #183).

Offline and deterministic, on #182's harness (``tests/test_scheduler.py``'s
:class:`Bench`): the EPIC's graph is the fake ``gh`` behind the real
read-only client, every worker is a real subprocess running the production
``run_worker`` (``tests/scheduler_worker.py``), and the scheduler runs in
this process, started by :func:`autoforge.cli.main` exactly as an operator
starts it. The crash windows, where a scheduler or ``epic archive`` dies
with SIGKILL, are ``tests/test_epic_recovery.py``'s.
"""

from __future__ import annotations

import json
import os
import pickle
import shlex
import signal
import sys
from pathlib import Path

import pytest

from autoforge import cli, epic_commands, scheduler_stop
from autoforge.epic_commands import DRAIN_HELP, EXIT_OPEN_CLAIMS, STATUS_SCHEMA
from autoforge.errors import ExecutionError
from autoforge.executor_supervised import spawn_supervised
from autoforge.locking import ControllerLock, IssueRunLock, repository_lock_path
from autoforge.run_namespace import EpicRef, RunBinding, RunKey, write_binding
from autoforge.scheduler import EpicScheduler
from autoforge.scheduler_state import (
    ClaimClassification,
    ClaimKind,
    EndReason,
    load_scheduler_state,
    scheduler_state_path,
)
from autoforge.scheduler_stop import SCHEDULER_CHANGED, stop_request_path
from autoforge.state import AutoForgeState, load_state, save_state, utcnow_iso
from autoforge.transitions import Phase

from .conftest import GIT_IDENT, commit_all, git_out, write_feature
from .test_scheduler import (
    BOOTSTRAP,
    EPIC,
    IMPLEMENT,
    REPO_ID,
    REVIEW,
    Bench,
    R,
    _admitted_but_never_spawned,
    _crashed_scheduler_leaving_an_open_claim,
    settled,
    url,
)

SRC = Path(__file__).resolve().parents[1] / "src" / "autoforge"


class Epic:
    """A :class:`Bench` driven through ``autoforge epic``, the scheduler in this process."""

    def __init__(self, b: Bench, monkeypatch, capsys) -> None:
        self.b = b
        self.capsys = capsys
        self.on_tick = None
        self.spawn = None
        self.clock = None
        monkeypatch.setattr(cli, "_epic_graph", lambda cfg: b.graph.client())
        monkeypatch.setattr(cli, "_epic_scheduler_options", self.options)

        class Tracked(EpicScheduler):
            def __init__(self, inputs) -> None:
                super().__init__(inputs)
                b.schedulers.append(self)  # reaped by the fixture: no worker outlives the test

        monkeypatch.setattr(cli, "EpicScheduler", Tracked)
        monkeypatch.chdir(b.repo)

    def options(self) -> dict:
        b = self.b
        found: dict = {
            "worker_command": (sys.executable, "-P", str(BOOTSTRAP), str(b.fixtures)),
            "output": b.lines.append,
            "tick_seconds": 0.02,
        }
        for key in ("on_tick", "spawn", "clock"):
            if getattr(self, key) is not None:
                found[key] = getattr(self, key)
        return found

    def cli(self, *words: str) -> tuple[int, str, str]:
        code = cli.main(["--config", str(self.b.config_path), *words])
        captured = self.capsys.readouterr()
        return code, captured.out, captured.err

    def run(self, *words: str) -> tuple[int, str, str]:
        self.b.lines.clear()
        return self.cli("epic", "run", EPIC, *words)

    def resume(self, *words: str) -> tuple[int, str, str]:
        self.b.lines.clear()
        return self.cli("epic", "resume", EPIC, *words)

    def status(self) -> tuple[int, dict]:
        code, out, _ = self.cli("epic", "status", EPIC, "--json")
        return code, json.loads(out)

    def rows(self) -> dict[int, dict]:
        _, doc = self.status()
        return {row["number"]: row for row in (*doc["children"], *doc["other_namespaces"])}

    def command(self, *words: str) -> str:
        return shlex.join(["autoforge", "--config", str(self.b.config_path), *words])

    @property
    def said(self) -> str:
        return "\n".join(self.b.lines)


@pytest.fixture
def epic(tmp_path, monkeypatch, capsys):
    made: list[Bench] = []

    def make(config: dict | None = None) -> Epic:
        b = Bench(tmp_path, config)
        made.append(b)
        return Epic(b, monkeypatch, capsys)

    yield make
    for b in made:
        b.reap()


@pytest.fixture
def signals(monkeypatch):
    """Every signal this process sends; ``epic status`` and ``epic stop`` send none."""
    sent: list[tuple[int, int]] = []
    real_kill, real_killpg = os.kill, os.killpg

    def kill(pid, sig):
        if sig:
            sent.append((pid, sig))
        return real_kill(pid, sig)

    def killpg(pgid, sig):
        if sig:
            sent.append((-pgid, sig))
        return real_killpg(pgid, sig)

    monkeypatch.setattr(os, "kill", kill)
    monkeypatch.setattr(os, "killpg", killpg)
    return sent


def world(b: Bench, n: int) -> Path:
    (b.tmp / "worlds").mkdir(exist_ok=True)
    return b.tmp / "worlds" / f"{n}.pickle"


def prs(b: Bench, n: int) -> list:
    with open(world(b, n), "rb") as f:
        return list(pickle.load(f).prs.values())


def standalone_state(path: Path, issue: int, phase: Phase) -> None:
    """A standalone REMOTE run of ``issue`` in ``phase``, as the configured state."""
    state = AutoForgeState(
        repository=R,
        epic_url=EPIC,
        current_issue_url=url(issue),
        phase=phase,
        run_id="standalone",
        created_at=utcnow_iso(),
        updated_at=utcnow_iso(),
    )
    if phase is Phase.REVIEW:
        state.current_pr_url = f"https://github.com/{R}/pull/77"
    path.mkdir(parents=True, exist_ok=True)
    save_state(state, path / "state.json")


def _bind(b: Bench, n: int, epic: EpicRef | None = None, *, issue_id: int = 0) -> None:
    """A binding and nothing else: a namespace of EPIC #1, or of ``epic``."""
    write_binding(
        b.ns(n),
        RunBinding(
            run_key=RunKey(REPO_ID, n),
            issue_id=issue_id or 100_000 + n,
            repository_owner="owner",
            repository_name="repo",
            epic=epic or EpicRef(1, b.graph.issue(1, R).id),
            worktree_path=str(b.tmp / f"wt{n}"),
        ),
    )


def _bind_other_epic(b: Bench, n: int) -> None:
    _bind(b, n, EpicRef(99, 99099))


# -- the command surface -----------------------------------------------------------------


def test_the_epic_commands_and_their_flags(capsys):
    parser = cli.build_parser()
    for words in (["epic", "--help"], ["epic", "stop", "--help"]):
        with pytest.raises(SystemExit):
            parser.parse_args(words)
    out = capsys.readouterr().out
    assert "_worker" not in out
    assert "a drain cannot be escalated" in " ".join(out.split())
    for value in ("0", "5", "x"):
        with pytest.raises(SystemExit):
            parser.parse_args(["epic", "run", EPIC, "--max-parallel", value])
    assert "between 1 and 4" in capsys.readouterr().err
    args = parser.parse_args(["epic", "resume", EPIC, "--max-parallel", "4"])
    assert args.max_parallel == 4 and not args.accept_config_change
    args = parser.parse_args(["epic", "resume", EPIC, "--accept-config-change"])
    assert args.accept_config_change
    with pytest.raises(SystemExit):  # epic resume's only (D9.4): epic run resumes no run
        parser.parse_args(["epic", "run", EPIC, "--accept-config-change"])
    with pytest.raises(SystemExit):  # there is no --allow-merge
        parser.parse_args(["epic", "run", EPIC, "--allow-merge"])
    with pytest.raises(SystemExit):
        parser.parse_args(["epic", "archive", EPIC, "0"])


@pytest.mark.parametrize(
    "words",
    [
        ["run", EPIC],
        ["run", EPIC, "--dry-run"],
        ["resume", EPIC],
        ["status", EPIC],
        ["stop", EPIC],
        ["archive", EPIC, "2"],
    ],
)
def test_every_epic_command_refuses_state_dir(tmp_path, capsys, words):
    assert cli.main(["--state-dir", str(tmp_path / "sd"), "epic", *words]) == 2
    assert "refuse --state-dir" in capsys.readouterr().err
    assert not (tmp_path / "sd").exists()


OTHER = "other/repo"
OTHER_EPIC = f"https://github.com/{OTHER}/issues/1"  # this checkout's EPIC's number


def _tree(root: Path) -> dict[str, tuple[bytes, int] | None]:
    """Every entry of the scheduler's tree with its bytes and mtime: a refusal changes none."""
    base = root / "autoforge"
    found: dict[str, tuple[bytes, int] | None] = {}
    for path in sorted(base.rglob("*")):
        name = str(path.relative_to(base))
        found[name] = (path.read_bytes(), path.stat().st_mtime_ns) if path.is_file() else None
    return found


def _other_repository_with_an_epic(b: Bench) -> None:
    """EPIC #1 of ``other/repo`` with a ready child #2: one the CLI would otherwise run."""
    b.graph.repository_ids[OTHER] = 5151
    b.graph.add(1, repo=OTHER, parent=None)
    b.graph.add(2, repo=OTHER, parent=(OTHER, 1))


@pytest.mark.parametrize(
    "words",
    [
        ["run", OTHER_EPIC],
        ["run", OTHER_EPIC, "--dry-run"],
        ["resume", OTHER_EPIC],
        ["status", OTHER_EPIC, "--json"],
        ["stop", OTHER_EPIC],
        ["stop", OTHER_EPIC, "--now"],
        ["archive", OTHER_EPIC, "2"],
    ],
)
def test_every_epic_command_refuses_an_epic_of_another_repository(epic, words):
    e = epic()
    b = e.b
    _other_repository_with_an_epic(b)
    assert e.run()[0] == 0  # this checkout's scheduler.json for its own EPIC #1
    b.graph.child(2)
    _admitted_but_never_spawned(b, 2, b.graph.issue(2, R).id)
    before = _tree(b.root)
    calls: list[str] = []
    b.graph.on_call = calls.append
    code, out, err = e.cli("epic", *words)
    assert code == 2 and out == ""
    assert f"{OTHER_EPIC} is an EPIC of {OTHER}, and this checkout's 'origin' is {R}" in err
    # Nothing written here (no lock, binding, claim, scheduler.json or stop
    # request; this checkout's own EPIC #1 untouched), and nothing read from GitHub.
    assert _tree(b.root) == before and calls == []
    assert b.claims(2).current is not None and b.admitted() == []


def test_an_epic_command_needs_a_github_origin_and_compares_it_case_insensitively(epic):
    e = epic()
    b = e.b
    git_out("-C", str(b.repo), "remote", "remove", "origin")
    code, _, err = e.cli("epic", "run", EPIC, "--dry-run")
    assert code == 2 and "its 'origin' remote cannot be read" in err
    git_out("-C", str(b.repo), "remote", "add", "origin", "https://gitlab.com/owner/repo.git")
    code, _, err = e.cli("epic", "status", EPIC)
    assert code == 2 and "its 'origin' remote does not name one" in err
    assert not (b.root / "autoforge").exists()
    # GitHub's names are case-insensitive: another spelling is the same repository.
    git_out("-C", str(b.repo), "remote", "set-url", "origin", "git@github.com:Owner/Repo.git")
    assert e.run()[0] == 0
    code, out, _ = e.cli("epic", "stop", "https://github.com/OWNER/repo/issues/1")
    assert code == 0 and "which has ended" in out


def test_stop_and_archive_refuse_files_of_another_repository(epic):
    """The origin re-pointed after a scheduler ran: its files still name the repository."""
    e = epic()
    b = e.b
    _other_repository_with_an_epic(b)
    assert e.run()[0] == 0
    b.graph.child(2)
    _admitted_but_never_spawned(b, 2, b.graph.issue(2, R).id)
    git_out("-C", str(b.repo), "remote", "set-url", "origin", f"https://github.com/{OTHER}.git")
    scheduler_json = scheduler_state_path(b.root, 1).read_bytes()
    claims = b.ns(2).claim_file.read_bytes()
    code, _, err = e.cli("epic", "stop", OTHER_EPIC, "--now")
    assert code == 2 and f"scheduler.json of EPIC #1 here is {R}'s, not {OTHER}'s" in err
    assert not stop_request_path(b.root, 1).exists()
    code, _, err = e.cli("epic", "archive", OTHER_EPIC, "2")
    assert code == 2 and f"bound under {R}, not {OTHER}: nothing was archived" in err
    assert b.ns(2).claim_file.read_bytes() == claims and b.claims(2).current is not None
    assert scheduler_state_path(b.root, 1).read_bytes() == scheduler_json


# -- epic status -------------------------------------------------------------------------


STATUS_KEYS = {
    "schema",
    "repository",
    "epic",
    "scheduler",
    "stop_request",
    "snapshot",
    "problems",
    "children",
    "other_namespaces",
    "rejected",
}
ROW_KEYS = {
    "number",
    "category",
    "hold_reason",
    "detail",
    "verdict",
    "blockers",
    "phase",
    "settlement",
    "classification",
    "pr_url",
    "state_dir",
    "live",
    "orphaned",
    "claim",
    "next",
}


def test_status_gives_every_child_exactly_one_category(epic, tmp_path):
    e = epic({"state_dir": str(tmp_path / "standalone")})
    b = e.b
    b.issue(2)
    b.issue(3, ({"do": "blocked"},))
    b.issue(4, ({"do": "garbage"}, {"do": "garbage"}))
    b.issue(5, (IMPLEMENT, REVIEW), 2)
    b.issue(13)
    b.graph.child(6)
    b.graph.close_issue(6)
    b.graph.add(7, repo="other/repo", parent=(R, 1))
    code, _, _ = e.run("--max-parallel", "4")
    assert code == 0 and sorted(b.admitted()) == [2, 3, 4, 13]
    done = b.state(13)
    done.phase = Phase.DONE
    save_state(done, b.ns(13).state_file)
    for n in (8, 9, 10, 11, 12, 14):
        b.graph.child(n)
    _admitted_but_never_spawned(b, 8, b.graph.issue(8, R).id)
    _admitted_but_never_spawned(b, 9, b.graph.issue(9, R).id)
    b.ns(9).state_dir.mkdir()
    b.ns(9).state_file.write_text("{ not json")
    _bind_other_epic(b, 10)
    standalone_state(tmp_path / "standalone", 14, Phase.REVIEW)
    scheduler_json = scheduler_state_path(b.root, 1).read_bytes()
    lock = IssueRunLock(b.ns(12)).acquire()
    try:
        code, doc = e.status()
        text = e.cli("epic", "status", EPIC)[1]
    finally:
        lock.release()
    assert code == 0
    rows = {row["number"]: row for row in doc["children"]}
    assert {n: row["category"] for n, row in rows.items()} == {
        2: "ready-for-merge",
        3: "blocked",
        4: "resumable",
        5: "blocked-by-dependency",
        6: "closed",
        7: "unsupported",
        8: "claimed",
        9: "uncertain",
        10: "held",
        11: "ready",
        12: "running",
        13: "settled",
        14: "held",
    }
    assert doc["other_namespaces"] == [] and doc["problems"] == []
    assert rows[10]["hold_reason"] == "other-epic" and rows[14]["hold_reason"] == "standalone-state"
    assert all(rows[n]["hold_reason"] is None for n in rows if n not in (10, 14))
    resume = e.command("epic", "resume", EPIC)
    # BLOCKED: its reason, the exact unblock on the namespace, then epic resume.
    blocked = rows[3]
    assert "BLOCKED: " in blocked["detail"] and blocked["phase"] == "BLOCKED"
    unblock = e.command("--state-dir", str(b.ns(3).state_dir), "unblock", "--reason", "<why>")
    assert blocked["next"] == [unblock, resume]
    assert rows[2]["pr_url"] and rows[2]["next"] == []
    assert rows[5]["blockers"] == ["#2 (open)"]
    assert rows[9]["classification"] == "uncertain(state-corrupt)"
    assert rows[9]["next"] == [e.command("epic", "archive", EPIC, "9"), resume]
    assert rows[8]["claim"]["generation"] == 1 and rows[8]["next"] == [resume]
    assert rows[13]["next"] == [e.command("epic", "archive", EPIC, "13"), resume]
    assert rows[12]["live"] and rows[12]["claim"] is None
    assert rows[4]["next"] == [resume] and rows[4]["phase"] == "ANALYZE_EXECUTE"
    # The schema is strict: every key, always present.
    assert set(doc) == STATUS_KEYS and doc["schema"] == STATUS_SCHEMA
    assert all(set(row) == ROW_KEYS for row in rows.values())
    assert set(rows[8]["claim"]) == {
        "claim_id",
        "generation",
        "kind",
        "invocation_id",
        "created_at",
        "worker",
    }
    assert doc["scheduler"]["state"] == "readable" and doc["scheduler"]["max_parallel"] == 4
    assert (
        doc["scheduler"]["invocation"]["ended"] and doc["scheduler"]["invocation"]["hint"] is None
    )
    assert doc["stop_request"]["present"] is False and doc["snapshot"]["taken"]
    assert doc["epic"] == {"number": 1, "url": EPIC, "issue_id": b.graph.issue(1, R).id}
    assert "#3 blocked: BLOCKED: " in text and "#12 running: a live process" in text
    assert "#10 held:other-epic" in text and f"next: {unblock}" in text
    # Lock-free and read-only: nothing changed.
    assert scheduler_state_path(b.root, 1).read_bytes() == scheduler_json
    assert not b.ns(11).directory.exists() and not b.ns(14).directory.exists()


def test_an_entry_of_issues_that_is_not_a_namespace_is_a_status_problem(epic):
    e = epic()
    b = e.b
    b.issue(2)
    issues = b.ns(2).directory.parent
    issues.mkdir(parents=True)
    (issues / "notes.txt").write_text("stray", encoding="utf-8")
    (issues / "7").write_text("not a directory", encoding="utf-8")
    code, doc = e.status()
    assert code == 1
    assert [entry["name"] for entry in doc["rejected"]] == ["7", "notes.txt"]
    rejected = [p for p in doc["problems"] if "is not a namespace" in p]
    assert len(rejected) == 2 and len(doc["problems"]) == 2
    assert rejected[1].startswith(
        "issues/notes.txt is not a namespace (not a decimal issue number) and is never read"
    )
    assert doc["children"][0]["category"] == "ready"  # every child is still shown
    code, out, _ = e.cli("epic", "status", EPIC)
    assert code == 1 and "problem: issues/7 is not a namespace (" in out
    assert out.count("issues/notes.txt") == 1


def test_status_while_a_scheduler_runs_shows_the_hung_worker_and_a_spawn_failure(epic, signals):
    e = epic()
    b = e.b
    b.issue(2, ({"do": "hold", "started": b.marker("s2"), "gate": b.marker("g2")},))
    b.issue(3)

    def spawn(argv, **kwargs):
        if argv[argv.index("--issue") + 1] == "3":
            raise ExecutionError("injected: no worker for #3")
        return spawn_supervised(argv, **kwargs)

    seen: dict = {}

    def tick(s: EpicScheduler) -> None:
        if b.has("s2") and not seen:
            seen["status"] = e.status()
            seen["text"] = e.cli("epic", "status", EPIC)[1]
            seen["signals"] = list(signals)
            b.open_gate("g2")

    e.spawn, e.on_tick = spawn, tick
    code, _, _ = e.run()
    assert code == 0
    status_code, doc = seen["status"]
    assert status_code == 0 and seen["signals"] == []
    rows = {row["number"]: row for row in doc["children"]}
    (child,) = b.schedulers[0].children
    running = rows[2]
    assert running["category"] == "running" and running["live"]
    worker = running["claim"]["worker"]
    assert worker["pid"] == child.pid and worker["process_start_time"] is not None
    assert worker["log"].endswith("/workers/1.log")
    assert "worker diagnostics (never signalled; a PID may be reused): pid" in seen["text"]
    assert rows[3]["category"] == "held" and rows[3]["hold_reason"] == "spawn-failed"
    assert rows[3]["detail"].startswith("the worker could not be started")
    invocation = doc["scheduler"]["invocation"]
    assert not invocation["ended"] and invocation["hint"] is None  # a worker runs
    assert doc["scheduler"]["admission"] == {"status": "open", "reason": None}
    # Once the invocation has ended, the hold is gone: the next run tries again.
    rows = e.rows()
    assert rows[3]["category"] == "ready" and rows[2]["category"] == "resumable"
    assert [sig for _, sig in signals] == []


def test_a_crashed_scheduler_is_not_ended_and_epic_run_points_to_resume(epic):
    e = epic()
    b = e.b
    _crashed_scheduler_leaving_an_open_claim(b)
    code, doc = e.status()
    invocation = doc["scheduler"]["invocation"]
    resume = e.command("epic", "resume", EPIC)
    assert not invocation["ended"]
    assert invocation["hint"] == (
        "not ended: if no 'autoforge epic' process is running, it ended without finalizing "
        f"(crash or kill); run '{resume}'"
    )
    assert doc["children"][0]["category"] == "claimed"
    assert (
        "not ended: if no 'autoforge epic' process is running" in e.cli("epic", "status", EPIC)[1]
    )
    before = scheduler_state_path(b.root, 1).read_bytes()
    code, _, err = e.run()
    assert code == 2 and "'epic run' refuses: claims of this EPIC are still open: #2" in err
    assert resume in err
    assert scheduler_state_path(b.root, 1).read_bytes() == before  # nothing written
    code, _, err = e.run("--force")  # --force never bypasses the refusal
    assert code == 2 and "still open" in err
    b.issue(2, (IMPLEMENT, REVIEW))
    code, _, _ = e.resume()
    assert code == 0 and b.state(2).phase is Phase.READY_FOR_MERGE
    assert [c.classification for c in b.claims(2).history] == [
        ClaimClassification.SUPERSEDED,
        ClaimClassification.SETTLED,
    ]


# -- exit codes --------------------------------------------------------------------------


def test_an_uncertain_open_claim_is_never_admitted_and_ends_with_exit_3(epic):
    e = epic()
    b = e.b
    b.graph.child(2)
    _admitted_but_never_spawned(b, 2, b.graph.issue(2, R).id)
    b.ns(2).state_dir.mkdir()
    b.ns(2).state_file.write_text("[]")
    code, _, _ = e.resume()
    assert code == EXIT_OPEN_CLAIMS
    assert "issue #2: claim generation 1 (start)" in e.said and "left open" in e.said
    assert b.claims(2).current is not None and b.admitted() == []
    rows = e.rows()
    assert rows[2]["category"] == "uncertain" and rows[2]["state_dir"] == str(b.ns(2).state_dir)
    # epic archive is the recovery: then the issue is admitted fresh.
    code, out, _ = e.cli("epic", "archive", EPIC, "2")
    assert code == 0 and "closed as archived" in out and "state was moved aside" in out
    code, out, _ = e.cli("epic", "archive", EPIC, "2")
    assert code == 0 and "nothing left to archive" in out
    b.issue(2)
    code, _, _ = e.resume()
    assert code == 0 and b.state(2).phase is Phase.READY_FOR_MERGE
    history = b.claims(2).history
    assert [c.classification for c in history] == [
        ClaimClassification.ARCHIVED,
        ClaimClassification.SETTLED,
    ]
    assert history[1].claim.kind is ClaimKind.START


def test_global_uncertainty_and_a_held_lock_exit_non_zero(epic):
    e = epic()
    b = e.b
    b.graph.child(2)
    with ControllerLock(repository_lock_path(b.repo)):
        assert e.run()[0] == 2
        assert e.cli("epic", "archive", EPIC, "2")[0] == 2
    _admitted_but_never_spawned(b, 5, 9005, repository_id=999)  # another repository's binding
    code, _, _ = e.resume()
    assert code == 1 and "global uncertainty" in e.said and b.admitted() == []
    code, doc = e.status()
    assert code == 1 and any("issue #5" in p for p in doc["problems"])


# -- epic run --dry-run and --force -------------------------------------------------------


def test_a_dry_run_prints_the_plan_and_creates_nothing(epic):
    e = epic()
    b = e.b
    b.issue(2)
    b.issue(3)
    b.issue(4, (IMPLEMENT,), 2)
    b.issue(5)
    worktrees = git_out("-C", str(b.repo), "worktree", "list")
    code, out, _ = e.run("--dry-run")
    assert code == 0
    assert "dry run: nothing is locked, written or started" in out
    assert "#2: ready (1 open children depend on it)" in out and "#4: blocked by #2 (open)" in out
    assert "admitted now: #2 (start), #3 (start)" in out and "queued for a free slot: #5" in out
    assert not (b.root / "autoforge").exists()  # no lock, namespace or scheduler.json
    assert git_out("-C", str(b.repo), "worktree", "list") == worktrees
    assert e.run("--dry-run", "--max-parallel", "1")[1].count("admitted now: #2 (start)") == 1


def test_force_moves_only_a_corrupt_scheduler_json_aside(epic):
    e = epic()
    b = e.b
    b.issue(2)
    path = scheduler_state_path(b.root, 1)
    path.parent.mkdir(parents=True)
    path.write_text("{")
    code, out, _ = e.run("--dry-run")
    assert code == 0 and "'epic run' would admit nothing" in out and "--force" in out
    assert "--force would move it aside" in e.run("--dry-run", "--force")[1]
    code, _, _ = e.run()
    assert code == 1 and "'epic run --force' moves it aside" in e.said
    assert path.read_text() == "{"
    code, _, _ = e.run("--force")
    assert code == 0 and "the unreadable scheduler.json was moved aside to" in e.said
    (moved,) = path.parent.glob("scheduler.json.corrupt-*")
    assert moved.read_text() == "{" and b.state(2).phase is Phase.READY_FOR_MERGE
    code, _, _ = e.run("--force")
    assert code == 0 and "scheduler.json is readable; nothing was moved aside" in e.said


# -- manual changes while the scheduler runs -----------------------------------------------


def _jumping_clock():
    """Every reading 100 s past the last: each free slot takes a new snapshot at once."""
    now = [0.0]

    def clock() -> float:
        now[0] += 100.0
        return now[0]

    return clock


def test_a_blocker_closed_and_another_reopened_mid_run_and_a_child_removed(epic):
    e = epic()
    b = e.b
    b.issue(2, ({**IMPLEMENT, "started": b.marker("s2"), "gate": b.marker("g2")}, REVIEW))
    b.issue(3, (IMPLEMENT, REVIEW), 2)  # ready once #2 is closed as completed
    b.graph.add(4, repo=R, parent=None)  # a blocker outside the EPIC
    b.graph.close_issue(4)
    b.issue(5, (IMPLEMENT, REVIEW), 4)  # ready until #4 is reopened

    def tick(s: EpicScheduler) -> None:
        if b.has("s2") and not b.has("g2"):
            b.graph.close_issue(2)  # merged by a human while its worker runs
            b.graph.reopen(4, repo=R)
            b.open_gate("g2")

    e.on_tick, e.clock = tick, _jumping_clock()
    code, _, _ = e.run("--max-parallel", "1")
    assert code == 0 and b.admitted() == [2, 3]
    assert b.claims(5) is None
    rows = e.rows()
    assert rows[5]["category"] == "blocked-by-dependency"
    assert rows[5]["blockers"] == ["#4 (open, reopened)"]
    assert rows[2]["category"] == "closed" and rows[3]["category"] == "ready-for-merge"
    # A child removed from the EPIC: its namespace is shown, orphaned, and left alone.
    b.graph.issue(3, R).parent = None
    _, doc = e.status()
    (orphan,) = doc["other_namespaces"]
    assert orphan["number"] == 3 and orphan["orphaned"] and orphan["verdict"] is None
    assert orphan["category"] == "ready-for-merge"
    code, _, _ = e.resume()
    assert code == 0 and b.admitted() == [] and len(b.claims(3).history) == 1


def test_the_epic_closed_mid_run_admits_nothing_more(epic):
    e = epic()
    b = e.b
    b.issue(2, ({**IMPLEMENT, "started": b.marker("s2"), "gate": b.marker("g2")}, REVIEW))
    b.issue(3)

    def tick(s: EpicScheduler) -> None:
        if b.has("s2") and not b.has("g2"):
            b.graph.close_issue(1)
            b.open_gate("g2")

    e.on_tick, e.clock = tick, _jumping_clock()
    code, _, _ = e.run("--max-parallel", "1")
    assert b.admitted() == [2] and b.claims(3) is None, e.said
    assert settled(b, 2) is ClaimClassification.SETTLED
    _, doc = e.status()
    assert doc["snapshot"]["taken"] is False and "closed" in doc["snapshot"]["problem"]
    assert code == 1 and "refused (epic-not-open)" in e.said and "done (degraded)" in e.said


def test_a_child_re_parented_mid_run_is_never_admitted(epic):
    e = epic()
    b = e.b
    b.issue(2, ({**IMPLEMENT, "started": b.marker("s2"), "gate": b.marker("g2")}, REVIEW))
    b.issue(3)
    b.graph.add(9, repo=R, parent=None)  # another EPIC

    def tick(s: EpicScheduler) -> None:
        if b.has("s2") and not b.has("g2"):
            b.graph.issue(3, R).parent = (R, 9)
            b.open_gate("g2")

    e.on_tick, e.clock = tick, _jumping_clock()
    code, _, _ = e.run("--max-parallel", "1")
    assert code == 0 and b.admitted() == [2] and b.claims(3) is None, e.said
    assert 3 not in e.rows()


def test_an_issue_completed_and_a_dependency_added_between_snapshot_and_claim(epic):
    e = epic()
    b = e.b
    b.issue(6)
    b.issue(8)
    b.graph.child(7)
    b.graph.close(7, "not_planned", repo=R)

    def on_call(endpoint: str) -> None:
        # The snapshot lists the children; each admission's re-read GETs its issue.
        if endpoint == f"repos/{R}/issues/6":
            b.graph.close_issue(6)
        if endpoint == f"repos/{R}/issues/8" and (R, 7) not in b.graph.issue(8, R).blockers:
            b.graph.blocked(8, 7)

    b.graph.on_call = on_call
    code, _, _ = e.run()
    b.graph.on_call = None
    assert code == 0 and b.admitted() == []
    assert b.claims(6) is None and b.claims(8) is None  # no claim was written
    assert "changed since the snapshot" in e.said
    rows = e.rows()
    assert rows[6]["category"] == "closed"
    assert rows[8]["category"] == "blocked-by-dependency"
    assert rows[8]["blockers"] == ["#7 (closed-unsatisfied: not_planned)"]


def test_a_human_push_to_an_active_pr_is_reviewed_at_the_new_head(epic):
    e = epic()
    b = e.b
    pushed: list[str] = []
    b.issue(
        2,
        (
            IMPLEMENT,
            {**REVIEW, "started": b.marker("s2"), "gate": b.marker("g2")},
            {**REVIEW, "round": 2},
        ),
        world=str(world(b, 2)),
    )

    def tick(s: EpicScheduler) -> None:
        if b.has("s2") and not pushed:
            branch = load_state(b.ns(2).state_file).current_branch
            human = b.tmp / "human"
            git_out("clone", "-q", "-b", branch, str(b.origin.bare), str(human))
            git_out("-C", str(human), *GIT_IDENT, "commit", "-q", "--allow-empty", "-m", "human")
            git_out("-C", str(human), "push", "-q", "origin", branch)
            pushed.append(git_out("-C", str(human), "rev-parse", "HEAD"))
            b.open_gate("g2")

    e.on_tick = tick
    code, _, _ = e.run()
    assert code == 0 and pushed
    state = b.state(2)
    assert state.phase is Phase.READY_FOR_MERGE and state.reviewed_head_sha == pushed[0]
    assert len(prs(b, 2)) == 1 and settled(b, 2) is ClaimClassification.SETTLED
    assert len(b.claims(2).history) == 1


def test_with_the_merge_gate_open_no_worker_merges_or_writes_the_epic(epic):
    e = epic({"version": 1, "safety": {"allow_merge": True}})
    b = e.b
    b.issue(2, world=str(world(b, 2)))
    code, _, _ = e.run()
    assert code == 0 and b.state(2).phase is Phase.READY_FOR_MERGE
    with open(world(b, 2), "rb") as f:
        gh = pickle.load(f)
    assert gh.merges == [] and gh.edited_issues == []
    assert not any(call[0] in ("merge_pr", "edit_issue_body") for call in gh.calls)
    assert e.rows()[2]["category"] == "ready-for-merge"


# -- epic stop ---------------------------------------------------------------------------


def test_every_stop_outcome(epic, monkeypatch, signals):
    e = epic()
    b = e.b
    # No scheduler.json: refused, nothing created.
    code, _, err = e.cli("epic", "stop", EPIC)
    assert code == 2 and "nothing to stop" in err
    assert not (b.root / "autoforge").exists()
    assert e.run()[0] == 0  # an EPIC with no children: the invocation ends at once
    ended = load_scheduler_state(b.root, 1).invocation.invocation_id
    # Delivered to an ended invocation: survivors drain; --now cannot signal them.
    locks = [IssueRunLock(b.ns(n)).acquire() for n in (4, 5)]
    _bind_other_epic(b, 4)
    _bind(b, 5)
    try:
        code, out, _ = e.cli("epic", "stop", EPIC, "--now")
    finally:
        for lock in locks:
            lock.release()
    assert code == 0 and "which has ended" in out and "'now' cannot signal them" in out
    assert "workers still running (their run locks are held): #5;" in out  # #4: another EPIC
    # A request already there: its mode reported, nothing written.
    request = stop_request_path(b.root, 1).read_bytes()
    code, out, _ = e.cli("epic", "stop", EPIC)
    assert code == 1 and "a now stop request is already there" in out
    assert stop_request_path(b.root, 1).read_bytes() == request
    # A stale request is removed by the next scheduler, which then runs normally.
    b.issue(2)
    code, _, _ = e.run()
    assert code == 0 and "removed the stale stop-request" in e.said
    assert load_scheduler_state(b.root, 1).invocation.end_reason is EndReason.NOTHING_ADMISSIBLE
    assert b.admitted() == [2] and ended != load_scheduler_state(b.root, 1).invocation.invocation_id
    # A request that does not parse: read as a drain, reported, nothing written.
    stop_request_path(b.root, 1).write_text("garbage")
    code, out, _ = e.cli("epic", "stop", EPIC, "--now")
    assert code == 1 and "does not parse" in out and DRAIN_HELP in out
    stop_request_path(b.root, 1).unlink()
    # The scheduler changed under the write: not delivered.
    real = scheduler_stop.load_scheduler_state
    reads: list[int] = []

    def changed(root, number):
        state = real(root, number)
        reads.append(1)
        if len(reads) == 2:
            from dataclasses import replace

            return replace(state, invocation=replace(state.invocation, invocation_id="f" * 32))
        return state

    monkeypatch.setattr(scheduler_stop, "load_scheduler_state", changed)
    code, out, _ = e.cli("epic", "stop", EPIC)
    assert code == 1 and SCHEDULER_CHANGED in out
    assert signals == []


def test_a_drain_and_a_now_stop_of_a_running_scheduler(epic, signals):
    e = epic()
    b = e.b
    gated = {**IMPLEMENT, "started": b.marker("s2"), "gate": b.marker("g2")}
    b.issue(2, (gated, REVIEW), world=str(world(b, 2)))  # resumed below: its PR lives on
    b.issue(3)
    stops: list[tuple[int, str]] = []

    def drain(s: EpicScheduler) -> None:
        if b.has("s2") and not stops:
            code, out, _ = e.cli("epic", "stop", EPIC)
            stops.append((code, out))
            b.open_gate("g2")

    e.on_tick = drain
    code, _, _ = e.run("--max-parallel", "1")
    assert code == 0 and b.schedulers[-1].end_reason is EndReason.DRAINED
    assert stops[0][0] == 0 and DRAIN_HELP in stops[0][1]
    assert b.state(2).phase is Phase.REVIEW and b.admitted() == [2]  # #3 never admitted
    assert signals == []
    # --now: the scheduler behaves as on Ctrl-C (#2, resumed, is admitted first).
    hold = {"do": "hold", "started": b.marker("h2"), "gate": b.marker("never")}
    b.issue(2, (hold,), world=str(world(b, 2)))

    def now(s: EpicScheduler) -> None:
        if b.has("h2") and len(stops) == 1:
            stops.append(e.cli("epic", "stop", EPIC, "--now")[:2])

    e.on_tick = now
    code, _, _ = e.run("--max-parallel", "1")
    assert code == 130 and b.schedulers[-1].end_reason is EndReason.STOPPED, e.said
    assert stops[1][0] == 0 and "removed the stale stop-request" in e.said
    assert settled(b, 2) is ClaimClassification.INTERRUPTED and b.claims(3) is None
    assert [sig for _, sig in signals] == [signal.SIGINT]  # the scheduler's, never SIGKILL


# -- epic archive ------------------------------------------------------------------------


def test_a_blocked_issue_archived_is_admitted_fresh_and_adopts_its_pr(epic):
    e = epic()
    b = e.b
    b.issue(2, (IMPLEMENT, {"do": "blocked"}), world=str(world(b, 2)))
    assert e.run()[0] == 0
    first = b.state(2)
    assert first.phase is Phase.BLOCKED and first.current_pr_url
    assert e.rows()[2]["category"] == "blocked"
    code, out, _ = e.cli("epic", "archive", EPIC, "2")
    assert code == 0
    (archived,) = b.ns(2).state_dir.glob("state.json.archived-*")
    assert f"the state was moved aside to {archived}" in out
    assert "kept: the run logs" in out and "the claim history" in out
    assert "loop-bound evidence" in out and "now live only in" in out
    assert load_state(archived).phase is Phase.BLOCKED
    assert e.rows()[2]["category"] == "ready"
    b.issue(2, (REVIEW,), world=str(world(b, 2)))  # the PR is adopted: no implementer runs
    assert e.run()[0] == 0
    again = b.state(2)
    assert again.phase is Phase.READY_FOR_MERGE and again.current_pr_url == first.current_pr_url
    assert again.run_id != first.run_id and len(prs(b, 2)) == 1
    assert [c.claim.generation for c in b.claims(2).history] == [1, 2]
    assert e.cli("epic", "archive", EPIC, "2")[0] == 0
    assert "nothing left to archive" in e.cli("epic", "archive", EPIC, "2")[1]


def test_archive_moves_an_unusable_claim_file_aside(epic):
    e = epic()
    b = e.b
    b.issue(2)
    _bind(b, 2, issue_id=b.graph.issue(2, R).id)
    b.ns(2).claim_file.write_text("{not json", encoding="utf-8")
    assert e.rows()[2]["category"] == "uncertain"
    code, out, _ = e.cli("epic", "archive", EPIC, "2")
    assert code == 0 and "the claim file could not be used" in out and "moved aside to" in out
    assert not b.ns(2).claim_file.exists()
    assert list(b.ns(2).claim_file.parent.glob(f"{b.ns(2).claim_file.name}.*"))
    assert e.rows()[2]["category"] == "ready"
    assert "nothing left to archive" in e.cli("epic", "archive", EPIC, "2")[1]


def test_archive_is_refused_while_the_issue_has_a_live_owner_or_another_epic(epic):
    e = epic()
    b = e.b
    b.graph.child(2)
    _admitted_but_never_spawned(b, 2, b.graph.issue(2, R).id)
    lock = IssueRunLock(b.ns(2)).acquire()
    try:
        code, _, err = e.cli("epic", "archive", EPIC, "2")
    finally:
        lock.release()
    assert code == 2 and "has a live owner" in err and b.claims(2).current is not None
    _bind_other_epic(b, 3)
    code, _, err = e.cli("epic", "archive", EPIC, "3")
    assert code == 2 and "bound under EPIC #99" in err
    code, out, _ = e.cli("epic", "archive", EPIC, "6")
    assert code == 0 and "nothing left to archive" in out and not b.ns(6).directory.exists()


# -- standalone runs and LOCAL ----------------------------------------------------------


@pytest.mark.parametrize("phase", [Phase.REVIEW, Phase.BLOCKED, Phase.FAILED])
def test_epic_run_holds_the_configured_standalone_state_in_any_phase_but_done(
    epic, tmp_path, phase
):
    e = epic({"state_dir": str(tmp_path / "standalone")})
    b = e.b
    b.issue(2)
    standalone_state(tmp_path / "standalone", 2, phase)
    code, _, _ = e.run()
    assert code == 0 and b.admitted() == [] and b.claims(2) is None
    assert "the standalone state" in e.said
    row = e.rows()[2]
    assert row["category"] == "held" and row["hold_reason"] == "standalone-state"
    assert f"phase {phase.value}" in row["detail"]


def test_a_done_standalone_state_holds_nothing_and_a_scheduler_issue_refuses_run(epic, tmp_path):
    e = epic({"state_dir": str(tmp_path / "standalone")})
    b = e.b
    b.issue(2)
    standalone_state(tmp_path / "standalone", 2, Phase.DONE)
    assert e.run()[0] == 0 and b.admitted() == [2]
    # The forward guard: a standalone run of a scheduler-owned issue is refused.
    other = tmp_path / "other"
    code, _, err = e.cli("--state-dir", str(other), "run", "--epic", EPIC, "--issue", url(2))
    assert code == 2 and "epic" in err and "scheduler" in err
    assert not (other / "state.json").exists()


def test_local_mode_is_unaffected_by_scheduler_namespaces(epic):
    e = epic()
    b = e.b
    b.issue(2, ({**IMPLEMENT, "started": b.marker("s2"), "gate": b.marker("g2")}, REVIEW))
    write_feature(b.repo)
    commit_all(b.repo, "feature")
    during: list[tuple[int, str]] = []

    def tick(s: EpicScheduler) -> None:
        if b.has("s2") and not during:
            # A LOCAL run is excluded by L0 in time only, and a dry run takes no lock.
            during.append(e.cli("local", "run", "features/add-filter.md")[::2])
            during.append(e.cli("local", "run", "features/add-filter.md", "--dry-run")[:2])
            b.open_gate("g2")

    e.on_tick = tick
    assert e.run()[0] == 0
    (locked, locked_err), (dry, dry_out) = during
    assert locked == 2 and "lock" in locked_err.lower()
    assert dry == 0 and "mode: LOCAL" in dry_out
    code, out = e.cli("local", "run", "features/add-filter.md", "--dry-run")[:2]
    assert code == 0 and "mode: LOCAL" in out


# -- structure ---------------------------------------------------------------------------


def test_the_epic_commands_signal_nothing_and_write_no_github():
    import ast

    tree = ast.parse((SRC / "epic_commands.py").read_text(encoding="utf-8"))
    names = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    names |= {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    assert not names & {"kill", "killpg", "signal", "send_signal", "SIGKILL", "SIGINT"}
    imported = {(node.module or "") for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert not imported & {"github", "effects", "effect_ops", "result_parser", "providers"}
    assert epic_commands.STATUS_SCHEMA == "autoforge.epic-status/1"
