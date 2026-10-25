"""The scheduler's issue worker, in process (ADR 0008 D9; #182).

``run_worker`` is called the way ``autoforge epic _worker`` calls it, on a
real git checkout and a real namespace under its common dir, with the L0 and
L2 descriptors this test holds standing in for the inherited ones (the
proofs of holding are the same on a descriptor of this process). The engine
behind it is the unchanged one, on FakeGitHub, a local bare origin and
scripted agents; the worker as a real subprocess of a scheduler is in
``tests/test_scheduler.py``.
"""

from __future__ import annotations

import json
import os
import shlex
import signal
import sys
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

from autoforge import worker
from autoforge.config import config_digest, load_config_file
from autoforge.engine import ControllerEngine
from autoforge.errors import LockProofError
from autoforge.locking import ControllerLock, IssueRunLock, repository_lock_path
from autoforge.providers import ProviderRegistry, ScriptedProvider
from autoforge.run_namespace import EpicRef, RunBinding, RunKey, issue_namespace, write_binding
from autoforge.scheduler_state import (
    Admission,
    AdmissionState,
    AdmissionStatus,
    Claim,
    ClaimKind,
    Invocation,
    SchedulerConfig,
    SchedulerState,
    StateRef,
    open_claim,
    read_namespace,
    write_scheduler_state,
)
from autoforge.scheduler_stop import (
    STOP_SCHEMA,
    StopMode,
    StopRequest,
    request_stop,
    stop_request_path,
)
from autoforge.state import AutoForgeState, load_state, save_state
from autoforge.transitions import Phase
from autoforge.worker import WorkerArguments, parse_worker_arguments, run_worker
from tests.pi_fake import PI_PROFILES, PiFake, ScriptedPi

from .conftest import (
    EPIC,
    ISSUE,
    PR,
    FakeGitHub,
    Origin,
    analyze_payload,
    block,
    commit_in,
    connect_origin,
    git_out,
    git_repo,
    implement,
    make_engine,
    review_result,
    scripted,
)

TS = "2026-10-19T00:00:00+00:00"
REPO_ID = 700_001
EPIC_REF = EpicRef(number=1, issue_id=8_001)
ISSUE_ID = 9_002
INVOCATION = "0123456789abcdef" * 2
OTHER_INVOCATION = "fedcba9876543210" * 2
ADMISSION = Admission("a" * 64, "b" * 64, TS)
NEXT_ISSUE = "https://github.com/owner/repo/issues/3"


class World:
    """One admitted issue (#2 of EPIC #1) as a scheduler leaves it for its worker.

    The checkout, its bare origin and FakeGitHub; the namespace bound under
    the EPIC; ``scheduler.json`` naming :data:`INVOCATION` and the digest of
    the configuration in ``config_path``; and, once :meth:`admit` ran, L0
    (shared) and L2 (handed off) held by this test with the open claim the
    worker is started for.
    """

    def __init__(self, tmp_path: Path, monkeypatch, config: dict | None = None) -> None:
        self.tmp_path = tmp_path
        self.repo = git_repo(tmp_path / "repo")
        (tmp_path / "origin").mkdir()
        self.origin = Origin.seeded(tmp_path / "origin", self.repo)
        self.gh = FakeGitHub()
        self.gh.add_issue(ISSUE, "Feature")
        self.gh.attach_origin(self.origin)
        self.handler: Callable = lambda req: ""
        self.provider = ScriptedProvider(lambda req: self.handler(req))
        self.registry = ProviderRegistry(
            overrides={"claude": self.provider, "opencode": self.provider}
        )
        self.config_path = ""
        if config is not None:
            self.write_config(config)
        self.root = Path(os.path.realpath(repository_lock_path(self.repo).parent.parent))
        self.ns = issue_namespace(self.root, 2)
        self.l0: ControllerLock | None = None
        self.l2: IssueRunLock | None = None
        self.claim: Claim | None = None
        origin = self.origin
        real = ControllerEngine.__init__

        def patched(engine, *args, **kwargs):
            real(engine, *args, **kwargs)
            connect_origin(engine, origin)

        monkeypatch.setattr(ControllerEngine, "__init__", patched)
        write_binding(
            self.ns,
            RunBinding(
                run_key=RunKey(REPO_ID, 2),
                issue_id=ISSUE_ID,
                repository_owner="owner",
                repository_name="repo",
                epic=EPIC_REF,
                worktree_path=str(tmp_path / "worktrees" / "2"),
                created_at=TS,
            ),
        )
        self.start_scheduler()

    def write_config(self, config: dict) -> None:
        path = self.tmp_path / "autoforge.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        self.config_path = str(path)

    def start_scheduler(self, invocation: str = INVOCATION) -> None:
        config = load_config_file(self.config_path or None)
        write_scheduler_state(
            self.root,
            SchedulerState(
                repository_id=REPO_ID,
                repository_owner="owner",
                repository_name="repo",
                epic=EPIC_REF,
                invocation=Invocation(invocation, TS),
                config=SchedulerConfig(2, 60, 30, config_digest(config)),
                admission=AdmissionState(AdmissionStatus.OPEN),
                created_at=TS,
                updated_at=TS,
            ),
        )

    def admit(self, invocation: str = INVOCATION) -> WorkerArguments:
        """Hold L0 and L2, open the claim (START or RESUME by the state) and hand L2 off."""
        if self.l0 is None:
            self.l0 = ControllerLock(repository_lock_path(self.repo)).acquire()
        l0_fd = self.l0.share()
        self.l2 = IssueRunLock(self.ns).acquire()
        lookup = read_namespace(self.ns).lookup
        has_state = self.ns.state_file.exists()
        previous, self.claim = self.claim, None
        self.claim = open_claim(
            self.ns,
            self.l2,
            kind=ClaimKind.RESUME if has_state else ClaimKind.START,
            invocation_id=invocation,
            admission=ADMISSION,
            state_ref=StateRef.from_lookup(lookup) if has_state else StateRef.absent(),  # type: ignore[arg-type]
            supersedes=previous,  # the scheduler supersedes a resumable claim
        )
        return WorkerArguments(
            repository=str(self.repo),
            config=self.config_path,
            epic=1,
            issue=2,
            claim_id=self.claim.claim_id,
            generation=self.claim.generation,
            invocation_id=invocation,
            l0_fd=l0_fd,
            l2_fd=self.l2.handoff(),
        )

    def run(self, args: WorkerArguments | list[str]) -> int:
        argv = args.argv() if isinstance(args, WorkerArguments) else args
        return run_worker(
            argv, github_factory=lambda cfg: self.gh, provider_factory=lambda cfg: self.registry
        )

    def end_worker(self) -> None:
        """What the reap leaves: this test's copies of the descriptors closed."""
        if self.l2 is not None:
            self.l2.release()
            self.l2 = None

    def close(self) -> None:
        self.end_worker()
        if self.l0 is not None:
            self.l0.release()
            self.l0 = None

    def state(self) -> AutoForgeState:
        return load_state(self.ns.state_file)

    def tree(self) -> dict[str, tuple]:
        """Every entry under the coordination root's ``autoforge`` directory, with its content."""
        out: dict[str, tuple] = {}
        base = self.root / "autoforge"
        for directory, dirs, files in os.walk(base):
            for name in [*dirs, *files]:
                path = Path(directory) / name
                st = os.lstat(path)
                content = path.read_bytes() if path.is_file() else b""
                out[str(path.relative_to(base))] = (st.st_mode, st.st_mtime_ns, content)
        return out


@pytest.fixture
def world(tmp_path, monkeypatch):
    made: list[World] = []

    def make(config: dict | None = None) -> World:
        w = World(tmp_path, monkeypatch, config)
        made.append(w)
        return w

    yield make
    for w in made:
        w.close()


def to_ready(w: World) -> None:
    """Agents: the implementation commits, the review is clean, an UPDATE_EPIC would chain."""

    def agent(req):
        if req.phase == "ANALYZE_EXECUTE":
            return implement(req)
        if req.phase == "UPDATE_EPIC":
            return block(
                {
                    "phase": "UPDATE_EPIC",
                    "status": "success",
                    "progress": "done",
                    "roadmap_section": "- [x] done",
                    "next_issue_url": NEXT_ISSUE,
                }
            )
        return block(review_result(1, w.gh.prs[PR].head_sha))

    w.handler = agent


def phases(provider) -> list[str]:
    return [req.phase for req in provider.calls]


# -- the run ------------------------------------------------------------------------------


def test_a_fresh_issue_runs_to_ready_for_merge_and_stops_there_even_with_merge_allowed(
    world, capsys, monkeypatch
):
    w = world({"safety": {"allow_merge": True}})
    config_bytes = Path(w.config_path).read_bytes()
    to_ready(w)

    def no_chaining(self, issue_url):
        raise AssertionError("a worker selected a next issue")

    monkeypatch.setattr(AutoForgeState, "reset_for_new_issue", no_chaining)
    engines: list[ControllerEngine] = []
    build = worker._engine

    def kept(*args, **kwargs):
        engines.append(build(*args, **kwargs))
        return engines[-1]

    monkeypatch.setattr(worker, "_engine", kept)
    assert w.run(w.admit()) == 0
    state = w.state()
    assert state.phase is Phase.READY_FOR_MERGE
    assert (state.epic_url, state.current_issue_url) == (EPIC, ISSUE)
    assert phases(w.provider) == ["ANALYZE_EXECUTE", "REVIEW"]  # never UPDATE_EPIC
    assert w.gh.merges == [] and NEXT_ISSUE not in json.dumps(state.to_dict())
    assert Path(w.config_path).read_bytes() == config_bytes
    # The engine ran on the namespace's state, never a standalone one.
    assert not (w.repo / ".autoforge").exists()
    captured = capsys.readouterr()
    assert "stopped in phase READY_FOR_MERGE" in captured.err
    assert "READY_FOR_MERGE" in captured.out  # the CLI's own step rendering
    # The run names the scheduler's command to continue it, not a standalone one.
    # It names no state directory, and the EPIC by its URL, as 'epic resume'
    # takes it (#183).
    (engine,) = engines
    assert engine.resume_command() == shlex.join(
        ["autoforge", "--config", w.config_path, "epic", "resume", EPIC]
    )


@pytest.mark.parametrize("phase", [Phase.MERGE, Phase.UPDATE_EPIC])
def test_a_state_in_merge_or_update_epic_exits_without_a_step(world, capsys, phase):
    w = world({"safety": {"allow_merge": True}})
    _requesting(w, lambda: request_stop(w.root, 1, StopMode.DRAIN))
    assert w.run(w.admit()) == 0
    w.end_worker()
    os.unlink(stop_request_path(w.root, 1))
    args = w.admit()  # admitted on a resumable state ...
    # ... which an ``unblock`` moved into a phase no worker executes.
    state = w.state()
    state.phase = phase
    with w.ns.state_paths().open_root(create=False) as root:
        save_state(state, w.ns.state_file, root=root)
    before = w.ns.state_file.read_bytes()
    calls, effect_writes = len(w.provider.calls), list(w.gh.effect_writes)
    capsys.readouterr()
    assert w.run(args) == 0
    assert w.ns.state_file.read_bytes() == before
    assert len(w.provider.calls) == calls and w.gh.effect_writes == effect_writes
    assert w.gh.merges == []
    assert f"stopped in phase {phase.value}" in capsys.readouterr().err


def test_a_blocked_run_exits_1_with_its_state_settled(world, capsys):
    w = world()
    w.handler = lambda req: block(
        {"phase": "ANALYZE_EXECUTE", "status": "blocked", "issue_url": ISSUE, "message": "no"}
    )
    assert w.run(w.admit()) == 1
    assert w.state().phase is Phase.BLOCKED
    assert "is BLOCKED" in capsys.readouterr().err


# -- startup refusals (D9.2) ------------------------------------------------------------


def _fresh_root_fd(w: World) -> int:
    return os.open(w.root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)


def _refusal_cases():
    def claim_id(w, args):
        return replace(args, claim_id="f" * 32)

    def generation(w, args):
        return replace(args, generation=args.generation + 1)

    def invocation(w, args):
        return replace(args, invocation_id=OTHER_INVOCATION)

    def scheduler_invocation(w, args):
        w.start_scheduler(OTHER_INVOCATION)  # a new scheduler since the claim
        return args

    def config_digest_changed(w, args):
        w.write_config({"github": {"timeout_seconds": 77}})  # edited since the start
        return replace(args, config=w.config_path)

    def config_in_the_checkout(w, args):
        path = w.repo / "autoforge.json"  # a tree an enforced launch writes (#280)
        path.write_text(json.dumps({"sandbox": {"mode": "required"}}), encoding="utf-8")
        w.config_path = str(path)
        w.start_scheduler()  # the digest matches; where the file lies does not
        return replace(args, config=w.config_path)

    def config_through_a_dotdot(w, args):
        path = w.repo / "autoforge.json"  # opened through a link and a .. (#280, R2-F1)
        path.write_text(json.dumps({"sandbox": {"mode": "required"}}), encoding="utf-8")
        (w.repo / "conf").mkdir()
        (w.tmp_path / "operator").mkdir()
        (w.tmp_path / "operator" / "alias").symlink_to(w.repo / "conf")
        w.config_path = f"{w.tmp_path}/operator/alias/../autoforge.json"
        w.start_scheduler()
        return replace(args, config=w.config_path)

    def l0_identity(w, args):
        return replace(args, l0_fd=os.open(w.tmp_path, os.O_RDONLY | os.O_DIRECTORY))

    def l2_identity(w, args):
        return replace(args, l2_fd=os.open(w.root, os.O_RDONLY | os.O_DIRECTORY))

    def l0_not_held(w, args):
        w.l0.release()  # nothing holds L0 now; a descriptor of the right object
        w.l0 = None
        return replace(args, l0_fd=_fresh_root_fd(w))

    def l2_not_held(w, args):
        w.l2.release()  # nothing holds L2 now; a descriptor of the right file
        w.l2 = None
        return replace(args, l2_fd=os.open(w.ns.run_lock_file, os.O_RDONLY))

    def other_epic(w, args):
        return replace(args, epic=3)

    return {
        "claim-id": (claim_id, "claim"),
        "generation": (generation, "generation"),
        "invocation": (invocation, "invocation"),
        "scheduler-invocation": (scheduler_invocation, "scheduler.json names invocation"),
        "config-digest": (config_digest_changed, "digest differs"),
        "config-in-the-checkout": (config_in_the_checkout, "autoforge.json lies in"),
        "config-through-a-dotdot": (config_through_a_dotdot, "/../autoforge.json is reached"),
        "l0-identity": (l0_identity, "names another object"),
        "l2-identity": (l2_identity, "names another object"),
        "l0-not-held": (l0_not_held, "nothing holds it"),
        "l2-not-held": (l2_not_held, "nothing holds it"),
        "other-epic": (other_epic, "bound under EPIC #1"),
    }


REFUSALS = _refusal_cases()


@pytest.mark.parametrize("case", list(REFUSALS))
def test_a_mismatched_start_is_refused_before_any_state_access(world, capsys, case):
    w = world({})
    to_ready(w)
    args = w.admit()
    mutate, expected = REFUSALS[case]
    args = mutate(w, args)
    before = w.tree()
    assert w.run(args) == 2
    err = capsys.readouterr().err
    assert "refused to start, nothing was read or written" in err and expected in err
    assert w.tree() == before
    assert not w.ns.state_dir.exists()
    assert w.provider.calls == [] and w.gh.calls == []
    # A refusal leaves the descriptors it was given open and untouched.
    os.fstat(args.l0_fd)
    os.fstat(args.l2_fd)


def test_a_config_in_a_providers_writable_directory_is_refused_at_the_start(
    world, capsys, monkeypatch
):
    """#280 (R3-F1): as every command refuses it. A dedicated
    ``CLAUDE_CONFIG_DIR`` is writable to every enforced Claude Code launch,
    outside the repository, so a configuration file there may say what an
    agent wrote; the worker's environment declares it as its launches would
    get it."""
    w = world({})
    to_ready(w)
    args = w.admit()
    directory = w.tmp_path / "claude-autoforge"
    directory.mkdir()
    path = directory / "autoforge.json"
    path.write_text(json.dumps({"sandbox": {"mode": "required"}}), encoding="utf-8")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(directory))
    w.config_path = str(path)
    w.start_scheduler()  # the digest matches; where the file lies does not
    args = replace(args, config=w.config_path)
    before = w.tree()
    assert w.run(args) == 2
    err = capsys.readouterr().err
    assert "refused to start, nothing was read or written" in err
    assert f"autoforge.json lies in {directory}, the Claude Code configuration directory" in err
    assert w.tree() == before
    assert not w.ns.state_dir.exists()
    assert w.provider.calls == [] and w.gh.calls == []


@pytest.mark.parametrize(
    "words, expected",
    [
        (["--repository", "repo"], "absolute path"),
        (["--l0-fd", "2"], "inherited descriptor"),
        (["--l2-fd", "{l0}"], "one descriptor"),
        (["--claim-id", "XYZ"], "32-hex"),
        (["--generation", "0"], "positive"),
        (["--surprise"], "unrecognized"),
    ],
)
def test_malformed_arguments_are_refused(world, capsys, words, expected):
    w = world()
    args = w.admit()
    argv = args.argv()
    flag, *value = words
    if value:
        argv[argv.index(flag) + 1] = value[0].format(l0=args.l0_fd)
    else:
        argv.append(flag)
    before = w.tree()
    assert w.run(argv) == 2
    assert expected in capsys.readouterr().err
    assert w.tree() == before


def test_the_arguments_round_trip():
    args = WorkerArguments(
        repository="/r",
        config="/c.yaml",
        epic=1,
        issue=2,
        claim_id="a" * 32,
        generation=3,
        invocation_id="b" * 32,
        l0_fd=5,
        l2_fd=6,
        accept_config_change=True,
    )
    assert parse_worker_arguments(args.argv()) == args
    plain = replace(args, config="", accept_config_change=False)
    assert parse_worker_arguments(plain.argv()) == plain
    assert "--config" not in plain.argv() and "--accept-config-change" not in plain.argv()


def test_a_start_claim_over_an_existing_state_is_refused(world, capsys):
    w = world()
    to_ready(w)
    args = w.admit()
    # A state appeared after the START claim was opened (another writer).
    with w.ns.state_paths().open_root(create=True) as root:
        save_state(
            AutoForgeState(
                run_id="af-other",
                repository="owner/repo",
                epic_url=EPIC,
                current_issue_url=ISSUE,
                phase=Phase.ANALYZE_EXECUTE,
                created_at=TS,
                updated_at=TS,
            ),
            w.ns.state_file,
            root=root,
        )
    before = w.ns.state_file.read_bytes()
    assert w.run(args) == 2
    assert "admitted with no state" in capsys.readouterr().err
    assert w.ns.state_file.read_bytes() == before and w.provider.calls == []


# -- the step boundary (D13.3, D16.4) -------------------------------------------------------


def _requesting(w: World, write: Callable[[], None]) -> None:
    """The implementation agent's launch is when the request arrives."""

    def agent(req):
        if req.phase == "ANALYZE_EXECUTE":
            write()
            return implement(req)
        return block(review_result(1, w.gh.prs[PR].head_sha))

    w.handler = agent


def test_a_drain_returns_at_the_next_step_boundary(world, capsys):
    w = world()
    _requesting(w, lambda: request_stop(w.root, 1, StopMode.DRAIN))
    assert w.run(w.admit()) == 0
    assert phases(w.provider) == ["ANALYZE_EXECUTE"]
    assert w.state().phase is Phase.REVIEW  # the step completed; the next never started
    assert "the scheduler is draining" in capsys.readouterr().err


def _with_mode(mode: object) -> str:
    return json.dumps(
        {"schema": STOP_SCHEMA, "invocation_id": INVOCATION, "mode": mode, "requested_at": TS}
    )


@pytest.mark.parametrize(
    "content",
    ["{broken", _with_mode([]), _with_mode({})],
    ids=["bad-json", "array-mode", "object-mode"],  # unhashable modes: PR #248 R1-F1
)
def test_a_request_that_does_not_parse_drains_and_never_crashes(world, capsys, content):
    w = world()
    _requesting(w, lambda: stop_request_path(w.root, 1).write_text(content))
    assert w.run(w.admit()) == 0
    assert phases(w.provider) == ["ANALYZE_EXECUTE"] and w.state().phase is Phase.REVIEW
    err = capsys.readouterr().err
    assert "does not parse is a drain" in err and "draining" in err


def test_a_request_of_another_invocation_is_not_ours(world):
    w = world()
    stale = StopRequest(OTHER_INVOCATION, StopMode.NOW, TS).to_dict()
    _requesting(w, lambda: stop_request_path(w.root, 1).write_text(json.dumps(stale)))
    assert w.run(w.admit()) == 0
    assert w.state().phase is Phase.READY_FOR_MERGE


def test_a_request_already_there_stops_before_the_first_step(world):
    w = world()
    to_ready(w)
    assert request_stop(w.root, 1, StopMode.DRAIN).written
    assert w.run(w.admit()) == 0
    assert w.provider.calls == [] and w.state().phase is Phase.INITIALIZING


def test_a_failed_proof_of_l2_stops_before_the_next_step(world, capsys, monkeypatch):
    w = world()
    lost = []
    real = IssueRunLock.prove_held

    def prove_held(lock):
        if lost:
            raise LockProofError("injected: the run lock is not held")
        return real(lock)

    monkeypatch.setattr(IssueRunLock, "prove_held", prove_held)
    _requesting(w, lambda: lost.append(True))
    assert w.run(w.admit()) == 0
    assert phases(w.provider) == ["ANALYZE_EXECUTE"] and w.state().phase is Phase.REVIEW
    assert "the run lock (L2) is not proven held" in capsys.readouterr().err


# -- signals ------------------------------------------------------------------------------


@pytest.mark.parametrize("how", ["SIGTERM", "SIGINT", "SIGINT-ignored-before"])
def test_an_interrupt_ends_the_worker_with_130_and_the_state_in_progress(world, capsys, how):
    """A real signal, delivered while the agent runs; restored handlers after.

    ``SIGINT-ignored-before``: a scheduler started as a background job passes
    an ignored SIGINT on, and Python then installs no handler of its own;
    the worker installs it, or the scheduler's interrupt would never land.
    """
    w = world()
    original = signal.getsignal(signal.SIGINT)
    if how == "SIGINT-ignored-before":
        signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        before = (signal.getsignal(signal.SIGINT), signal.getsignal(signal.SIGTERM))

        def interrupted(req):
            os.kill(os.getpid(), signal.SIGTERM if how == "SIGTERM" else signal.SIGINT)
            raise AssertionError("the signal was not delivered")

        w.handler = interrupted
        assert w.run(w.admit()) == 130
        assert (signal.getsignal(signal.SIGINT), signal.getsignal(signal.SIGTERM)) == before
    finally:
        signal.signal(signal.SIGINT, original)
    assert w.state().phase is Phase.ANALYZE_EXECUTE
    assert "interrupted; the state is as the last step saved it" in capsys.readouterr().err


# -- the recorded configuration (#219, D9.4) ----------------------------------------------


def test_a_resumed_state_passes_the_recorded_configuration_gate(world, capsys):
    w = world({})
    _requesting(w, lambda: request_stop(w.root, 1, StopMode.DRAIN))
    assert w.run(w.admit()) == 0
    assert w.state().phase is Phase.REVIEW
    w.end_worker()
    # The operator changed a routing; a new scheduler started under it.
    os.unlink(stop_request_path(w.root, 1))
    w.write_config({"profiles": {"review_round_1": {"model": "openai/another-model"}}})
    w.start_scheduler(OTHER_INVOCATION)
    to_ready(w)
    before = w.ns.state_file.read_bytes()
    assert w.run(w.admit(OTHER_INVOCATION)) == 2
    err = capsys.readouterr().err
    assert "profiles.review_round_1" in err and "autoforge" in err
    assert w.ns.state_file.read_bytes() == before
    w.end_worker()
    args = replace(w.admit(OTHER_INVOCATION), accept_config_change=True)
    assert w.run(args) == 0
    assert w.state().phase is Phase.READY_FOR_MERGE
    assert w.provider.calls[-1].profile.model == "openai/another-model"


# -- parity -------------------------------------------------------------------------------


def _routed(provider: str, pi: PiFake | None) -> dict:
    profiles = {}
    for name, (model, effort) in PI_PROFILES.items():
        if provider == "pi":
            assert pi is not None
            entry = {"provider": "pi", "model": model, "effort": effort}
            entry |= {"command": str(pi.command), "extra_args": [], "options": {}}
        elif provider == "claude":
            entry = {"provider": "claude", "model": "fable", "effort": "high", "command": "claude"}
        else:
            entry = {"provider": "opencode", "model": "openai/gpt-5.6-terra", "effort": "high"}
            entry |= {"command": "opencode"}
        profiles[name] = entry
    return {"profiles": profiles}


@pytest.mark.parametrize("provider", ["claude", "opencode", "pi"])
def test_every_provider_runs_through_the_same_worker_path(world, tmp_path, provider):
    pi = PiFake(tmp_path / "pi") if provider == "pi" else None
    w = world(_routed(provider, pi))
    to_ready(w)
    scripted_pi = ScriptedPi(pi, lambda req: w.handler(req)) if pi is not None else None
    if scripted_pi is not None:
        w.registry = ProviderRegistry(overrides={"pi": scripted_pi})
    assert w.run(w.admit()) == 0
    assert w.state().phase is Phase.READY_FOR_MERGE
    used = scripted_pi if scripted_pi is not None else w.provider
    assert phases(used) == ["ANALYZE_EXECUTE", "REVIEW"]
    assert {req.profile.provider for req in used.calls} == {provider}
    if pi is not None:
        assert w.provider.calls == [] and len(pi.launches()) == 2
    # The same writes, by the engine alone: its PR and its review comment.
    assert [name for name, *_ in w.gh.effect_writes] == ["create_pull_request", "create_pr_comment"]


# A validator that passes only on a tree holding ``fixed.txt``.
NEEDS_FIX = [
    sys.executable,
    "-c",
    "import os, sys; sys.exit(0 if os.path.exists('fixed.txt') else 1)",
]


def _validated_agents(gh: FakeGitHub) -> Callable:
    def first(req) -> str:  # committed, but fails the controller's validation
        return block(analyze_payload(commit_in(req.cwd)))

    def second(req) -> str:  # the relaunch repairs it
        (Path(req.cwd) / "fixed.txt").write_text("ok\n", encoding="utf-8")
        git_out("-C", req.cwd, "add", "fixed.txt")
        return block(analyze_payload(commit_in(req.cwd)))

    def review(req) -> str:
        return block(review_result(1, gh.prs[PR].head_sha))

    return scripted(first, second, review)


def _summary(state: AutoForgeState, provider) -> tuple:
    return (
        state.phase,
        [record["outcome"] for record in state.validation_records],
        phases(provider),
    )


def test_a_failing_candidate_takes_the_same_next_phase_as_in_a_standalone_run(
    world, tmp_path_factory
):
    """#158's gates apply unchanged: the same failure, relaunch and records."""
    w = world({"validation": {"commands": [NEEDS_FIX]}})
    w.handler = _validated_agents(w.gh)
    assert w.run(w.admit()) == 0
    in_worker = _summary(w.state(), w.provider)

    gh = FakeGitHub()
    gh.add_issue(ISSUE, "Feature")
    cfg = load_config_file(w.config_path)
    state_dir = tmp_path_factory.mktemp("standalone") / ".autoforge"
    eng = make_engine(state_dir, None, github=gh, cfg=cfg, origin=True)
    eng.provider._handler = _validated_agents(gh)  # type: ignore[attr-defined]
    eng.run(allow_merge=False)
    standalone = _summary(eng.state, eng.provider)  # type: ignore[attr-defined]
    eng.close()

    assert in_worker == standalone
    # The failed candidate went back to ANALYZE_EXECUTE, unpublished, in both.
    assert in_worker == (
        Phase.READY_FOR_MERGE,
        ["FAIL", "PASS"],
        ["ANALYZE_EXECUTE", "ANALYZE_EXECUTE", "REVIEW"],
    )
