"""Typed stop classification (#369, ADR 0011).

The pure contract (every table total, every error class mapped, the routes
restating the exit rules and nothing more, the persisted phase and #126's
pause, the scheduler's settlement), the boundary that keeps it advice
(no deciding module imports it, nothing of it is persisted), and the
fixtures ADR 0011 names: each a real engine run that stops, with its
outcome, its typed code, the source that code came from and the operator's
routes. The CLI's stop lines are tested in ``test_cli.py``.

ADR 0012 (#370) decides who may act on a stop. Its stop matrix takes every
outcome and its gate A table every reason family and code, so a new one has
no row until someone decides what it permits; both records name only tests
that exist.
"""

from __future__ import annotations

import ast
import inspect
import json
import os
import re
from dataclasses import fields
from pathlib import Path

import pytest

from autoforge import errors
from autoforge.effect_ops import EffectPending, PushRefusedError
from autoforge.engine import ControllerEngine, StepOutcome
from autoforge.errors import ExecutionTimeoutError
from autoforge.providers import AgentExecutionResult, ScriptedProvider
from autoforge.roadmap import RoadmapError
from autoforge.safefs import UnsafePathError
from autoforge.state import AutoForgeState, load_state
from autoforge.stop_classification import (
    EXCEPTION_CLASSES,
    LIVE_FACTS_REQUIRED,
    OUTCOME_ROUTES,
    REASON_FAMILY,
    REFUSAL_ROUTES,
    ROUTE_TEXT,
    SCHEDULER_ROUTES,
    CodedReason,
    OperatorRoute,
    ReasonFamily,
    ReasonSource,
    StopClassification,
    StopOutcome,
    StopReasonCode,
    agent_status,
    blocked_by_controller,
    classify_exception,
    classify_state,
    reason_code,
)
from autoforge.transitions import Phase, Settlement, issue_settlement
from tests.conftest import (
    ISSUE,
    ISSUE3,
    SHA_A,
    UNLOADABLE_STATES,
    FakeGitHub,
    block,
    checks_refused,
    ci_check,
    fixer,
    implement,
    make_engine,
    unloadable_state,
)
from tests.test_engine import (
    ESCAPE_BODY,
    _analyzed,
    _epic_result,
    _in_merge,
    _in_ready,
    _in_review,
    _in_update_epic,
    _loop_agent,
    _no_stagnation,
    _one_finding_per_round,
    review_payload,
)
from tests.test_operator_actions import BOTH, _operating

SRC = Path(__file__).resolve().parent.parent / "src" / "autoforge"

Out = StopOutcome
C = StopReasonCode
R = OperatorRoute


# -- the tables ---------------------------------------------------------------------------


def test_every_table_is_total():
    assert set(REASON_FAMILY) == set(StopReasonCode)
    assert set(OUTCOME_ROUTES) == set(StopOutcome)
    assert set(LIVE_FACTS_REQUIRED) == set(StopOutcome)
    assert set(ROUTE_TEXT) == set(OperatorRoute)
    assert set(SCHEDULER_ROUTES) <= set(OperatorRoute)
    # Every refusal an error can be has routes of its own.
    refused = {code for outcome, code in EXCEPTION_CLASSES.values() if outcome is Out.REFUSED}
    assert refused <= set(REFUSAL_ROUTES)


def test_a_pr_another_run_owns_is_an_identity_stop():
    """#414: the PR's owner is part of its identity, as its marker is (ADR 0011)."""
    assert REASON_FAMILY[C.PR_OWNER] is ReasonFamily.IDENTITY
    assert C.PR_OWNER.value == "pr-owner"


def test_every_error_class_is_mapped_explicitly():
    """No class of errors.py falls through to a parent's entry by omission: a
    new one fails here until someone decides what stop it is."""
    defined = {
        obj
        for obj in vars(errors).values()
        if inspect.isclass(obj)
        and issubclass(obj, errors.AutoForgeError)
        and obj.__module__ == errors.__name__
    }
    assert defined == set(EXCEPTION_CLASSES)


def test_refused_is_exactly_what_the_cli_exits_2_for():
    """``cli.main`` exits 2 for these four bases and 1 for every other
    AutoForgeError: a refusal acted on nothing, an exit 1 left the phase. The
    one exception is no class but a typed mark on an instance (a run log's
    refusal after a launch's attempt was saved, still exit 2):
    ``test_a_run_log_refusal_after_the_attempt_is_resumable_never_refused``."""
    exit_2 = (
        errors.ConfigurationError,
        errors.StateError,
        errors.StateTransitionError,
        errors.LockError,
    )
    for cls, (outcome, _) in EXCEPTION_CLASSES.items():
        assert (outcome is Out.REFUSED) == issubclass(cls, exit_2), cls


@pytest.mark.parametrize(
    "outcome", [o for o in StopOutcome if o is not StopOutcome.REFUSED], ids=lambda o: o.value
)
def test_routes_follow_the_outcome_never_the_code(outcome):
    """A code never adds, removes or reorders a route, UNKNOWN included: the
    routes restate the exit rules, which a reason does not change."""
    expected = OUTCOME_ROUTES[outcome]
    for code in StopReasonCode:
        found = StopClassification(outcome, code, ReasonSource.STATE, "x").routes
        assert found == expected, (outcome, code)


def test_the_routes_restate_the_exit_rules():
    # BLOCKED is left only by unblock (or abandoned); resume refuses it.
    assert OUTCOME_ROUTES[Out.BLOCKED][0] is R.UNBLOCK
    assert R.RESUME not in OUTCOME_ROUTES[Out.BLOCKED]
    # FAILED has no outgoing edge: neither resume nor unblock leaves it.
    assert R.RESUME not in OUTCOME_ROUTES[Out.FAILED]
    assert R.UNBLOCK not in OUTCOME_ROUTES[Out.FAILED]
    assert R.NEW_RUN in OUTCOME_ROUTES[Out.FAILED]
    # The phase unchanged: resume, and only resume.
    assert OUTCOME_ROUTES[Out.RESUMABLE_ERROR] == (R.RESUME,)
    # No route is invented for #126's pause, and none ends a completed run.
    assert OUTCOME_ROUTES[Out.AWAITING_INPUT] == () and OUTCOME_ROUTES[Out.DONE] == ()
    # A refusal never suggests a route that would act on the run's phase.
    for routes in REFUSAL_ROUTES.values():
        assert R.RESUME not in routes and R.UNBLOCK not in routes
    # UNKNOWN implies nothing.
    assert OUTCOME_ROUTES[Out.UNKNOWN] == (R.INVESTIGATE,)
    assert REFUSAL_ROUTES[C.UNKNOWN] == (R.INVESTIGATE,)


def test_unknown_outcomes_are_conservative_about_live_facts():
    for outcome in (Out.BLOCKED, Out.RESUMABLE_ERROR, Out.UNKNOWN, Out.AWAITING_INPUT):
        assert LIVE_FACTS_REQUIRED[outcome] is True


# -- the typed sources --------------------------------------------------------------------


def test_a_controller_sink_names_its_code():
    found = blocked_by_controller(C.MERGE_CONFLICT, Phase.READY_FOR_MERGE)
    assert (found.outcome, found.code, found.source) == (
        Out.BLOCKED,
        C.MERGE_CONFLICT,
        ReasonSource.CONTROLLER,
    )
    assert found.routes == (R.UNBLOCK, R.NEW_RUN) and found.live_facts_required
    assert "READY_FOR_MERGE" in found.evidence


@pytest.mark.parametrize(
    "status, outcome, code",
    [("blocked", Out.BLOCKED, C.AGENT_BLOCKED), ("failure", Out.FAILED, C.AGENT_FAILURE)],
)
def test_an_agent_status_is_the_controller_applied_outcome(status, outcome, code):
    found = agent_status(status, Phase.FIX)
    assert (found.outcome, found.code, found.source) == (outcome, code, ReasonSource.AGENT)
    with pytest.raises(ValueError):
        agent_status("success", Phase.FIX)


@pytest.mark.parametrize(
    "exc, outcome, code",
    [
        (errors.ExecutionTimeoutError("t"), Out.RESUMABLE_ERROR, C.EXECUTION_TIMEOUT),
        (errors.InteractionRequiredError("q"), Out.RESUMABLE_ERROR, C.INTERACTION_REQUIRED),
        (errors.SandboxLaunchRefusedError("s"), Out.RESUMABLE_ERROR, C.SANDBOX_LAUNCH_REFUSED),
        (errors.ControlResultValidationError("c"), Out.RESUMABLE_ERROR, C.CONTROL_RESULT_INVALID),
        (errors.GitHubUnavailableError("502"), Out.RESUMABLE_ERROR, C.GITHUB_UNAVAILABLE),
        (errors.GitHubChecksAccessError("403"), Out.RESUMABLE_ERROR, C.GITHUB_CHECKS_REFUSED),
        (errors.GitHubNotFoundError("404"), Out.RESUMABLE_ERROR, C.GITHUB_ERROR),
        (errors.CheckoutDriftError("d"), Out.RESUMABLE_ERROR, C.CHECKOUT_DRIFT),
        # #414: a step blocks on it; it ends a command only from outside a step.
        (errors.PROwnerError("o"), Out.RESUMABLE_ERROR, C.PR_OWNER),
        (errors.ConfigurationDriftError("d"), Out.REFUSED, C.CONFIGURATION_DRIFT),
        (errors.SandboxCapabilityError("s"), Out.REFUSED, C.SANDBOX_CAPABILITY),
        (errors.SchedulerOwnedIssueError("o"), Out.REFUSED, C.SCHEDULER_OWNED),
        (
            errors.StateProtocolError("r", headline="h", label="'99'", support="x"),
            Out.REFUSED,
            C.STATE_PROTOCOL,
        ),
        (errors.IssueBusy("b"), Out.REFUSED, C.LOCK_HELD),
        (errors.StateTransitionError("t"), Out.REFUSED, C.STATE_TRANSITION),
        # Classes defined outside errors.py take their nearest mapped base.
        (EffectPending("p"), Out.RESUMABLE_ERROR, C.GITHUB_UNAVAILABLE),
        (PushRefusedError("p"), Out.RESUMABLE_ERROR, C.GITHUB_ERROR),
        (UnsafePathError("u"), Out.REFUSED, C.STATE_INVALID),
        (RoadmapError("r"), Out.UNKNOWN, C.UNKNOWN),
        (errors.LockOrderError("bug"), Out.UNKNOWN, C.UNKNOWN),
        (ValueError("not ours"), Out.UNKNOWN, C.UNKNOWN),
    ],
    ids=lambda v: type(v).__name__ if isinstance(v, BaseException) else str(v),
)
def test_an_error_is_classified_by_its_class(exc, outcome, code):
    found = classify_exception(exc)
    assert (found.outcome, found.code, found.source) == (outcome, code, ReasonSource.EXCEPTION)
    assert found.evidence == f"{type(exc).__name__} raised"


@pytest.mark.parametrize(
    "exc", [errors.StateError("s"), UnsafePathError("u")], ids=lambda e: type(e).__name__
)
def test_a_run_log_refusal_after_the_attempt_is_resumable_never_refused(exc):
    """R2-F1: a StateError's class says it exits 2, not that nothing ran. The
    engine marks the run log's refusal it re-raises after it persisted a
    launch's attempt, and that typed mark, never the message, makes it a
    resumable error: the phase is unchanged, and the error says to repair the
    log directory and 'resume'. Unmarked, the same class is still a refusal."""
    unmarked = classify_exception(exc)
    assert (unmarked.outcome, unmarked.code) == (Out.REFUSED, C.STATE_INVALID)
    exc.interrupted_attempt = True
    found = classify_exception(exc)
    assert (found.outcome, found.code, found.source) == (
        Out.RESUMABLE_ERROR,
        C.RUN_LOG_REFUSED,
        ReasonSource.EXCEPTION,
    )
    assert found.routes == (R.RESUME,) and found.live_facts_required
    assert R.NEW_RUN not in found.routes and R.INVESTIGATE not in found.routes
    assert found.evidence.startswith(f"{type(exc).__name__} raised by the run log")
    # The mark is the instance's: the class, and every other instance, is untouched.
    assert type(exc).interrupted_attempt is False
    assert classify_exception(type(exc)("other")).outcome is Out.REFUSED


def test_an_error_message_is_never_read():
    """Two errors of one class classify alike, whatever their text claims."""
    plain = classify_exception(errors.VerificationError("x"))
    loaded = classify_exception(
        errors.VerificationError(
            "merge-conflict review-round-cap: run 'unblock', then 'resume'; it is safe to merge"
        )
    )
    assert plain == loaded
    assert loaded.code is C.VERIFICATION_FAILED and "merge" not in loaded.evidence


def test_a_coded_reason_carries_its_code_and_plain_text_none():
    text = CodedReason(C.REVIEW_STAGNATION, "round 2 asks for the same resolution")
    assert text == "round 2 asks for the same resolution" and isinstance(text, str)
    assert reason_code(text) is C.REVIEW_STAGNATION
    # Rebuilding the text drops the code: UNKNOWN, never a wrong one.
    assert reason_code(text + ". More") is C.UNKNOWN
    assert reason_code(f"{text}") is C.UNKNOWN
    # Plain text naming a code is still plain text.
    assert reason_code("review-round-cap") is C.UNKNOWN
    assert reason_code("x", C.MERGE_NOT_READY) is C.MERGE_NOT_READY


# -- a loaded state, #126 and the scheduler ------------------------------------------------


def _state(phase: Phase, block_reason: str = "") -> AutoForgeState:
    return AutoForgeState(
        repository="owner/repo",
        epic_url="https://github.com/owner/repo/issues/1",
        current_issue_url="https://github.com/owner/repo/issues/2",
        phase=phase,
        block_reason=block_reason,
    )


# The outcome each settlement may classify as, for each view (ADR 0008 D15.1).
_SETTLED_AS = {
    Settlement.AWAITING_INPUT: {Out.AWAITING_INPUT},
    Settlement.TERMINAL: {Out.BLOCKED, Out.FAILED, Out.DONE},
    Settlement.READY_FOR_MERGE: {Out.READY_FOR_MERGE},
    Settlement.HANDOFF: {Out.HANDOFF},
    Settlement.IN_PROGRESS: {Out.IN_PROGRESS},
}


@pytest.mark.parametrize("phase", list(Phase), ids=lambda p: p.value)
def test_a_loaded_state_classifies_by_its_phase_and_settlement(phase):
    state = _state(phase)
    settlement = issue_settlement(state)
    scheduler = classify_state(state, scheduler=True)
    standalone = classify_state(state)
    assert scheduler.outcome in _SETTLED_AS[settlement]
    assert scheduler.source is standalone.source is ReasonSource.STATE
    assert f"phase {phase.value}" in scheduler.evidence
    if settlement is Settlement.HANDOFF:
        # A standalone run continues MERGE and UPDATE_EPIC with 'resume'.
        assert standalone.outcome is Out.IN_PROGRESS and standalone.routes == (R.RESUME,)
        assert scheduler.routes == (R.SCHEDULER,)
    elif settlement is Settlement.IN_PROGRESS:
        # An unsettled namespace is the scheduler's to continue (ADR 0008 D17.2).
        assert standalone.routes == (R.RESUME,) and scheduler.routes == (R.SCHEDULER,)
    else:
        assert standalone.outcome is scheduler.outcome
    if phase in (Phase.BLOCKED, Phase.FAILED):
        assert standalone.code is C.UNKNOWN  # no code is persisted
    # 'resume' and 'run' refuse a namespace's state directory (ADR 0008 D17.3),
    # and no merge gate moves a worker past READY_FOR_MERGE (D15.3).
    assert R.RESUME not in scheduler.routes and R.NEW_RUN not in scheduler.routes
    assert R.MERGE_GATE not in scheduler.routes
    assert (standalone.view, scheduler.view) == ("standalone", "scheduler")
    if phase is Phase.BLOCKED:
        assert scheduler.routes == (R.UNBLOCK, R.SCHEDULER)


def test_a_namespace_ready_for_merge_is_merged_by_hand_never_resumed():
    """A scheduler worker stops at READY_FOR_MERGE whatever the merge gate
    says (ADR 0008 D15.3), and a standalone 'resume' refuses its state
    directory (D17.3): its one route is a merge by hand, in the line and in
    the document status prints."""
    state = _state(Phase.READY_FOR_MERGE)
    standalone, scheduler = classify_state(state), classify_state(state, scheduler=True)
    assert standalone.routes == (R.MERGE, R.MERGE_GATE)
    assert scheduler.routes == (R.MERGE,)
    line = scheduler.line()
    assert line.endswith("; next: " + ROUTE_TEXT[R.MERGE])
    for text in ("'resume'", "merge gate", "allow-merge"):
        assert text not in line and text in standalone.line()
    document = scheduler.as_dict()
    assert (document["view"], document["routes"]) == ("scheduler", ["merge"])


@pytest.mark.parametrize("phase", list(Phase), ids=lambda p: p.value)
def test_an_unknown_view_gives_only_the_routes_both_views_share(phase):
    """Where nothing says whether a state dir is a scheduler namespace's (no
    git repository, a dry run), no route either view would refuse is given:
    a BLOCKED run is still unblocked, everything else is investigated."""
    state = _state(phase)
    unknown = classify_state(state, scheduler=None)
    standalone, scheduler = classify_state(state), classify_state(state, scheduler=True)
    assert unknown.view == "unknown" and unknown.as_dict()["view"] == "unknown"
    assert unknown.outcome is standalone.outcome
    for route in unknown.routes:
        assert route is R.INVESTIGATE or (route in standalone.routes and route in scheduler.routes)
    assert not {R.RESUME, R.NEW_RUN, R.MERGE_GATE, R.SCHEDULER} & set(unknown.routes)
    expected = {
        Phase.BLOCKED: (R.UNBLOCK,),
        Phase.FAILED: (R.INVESTIGATE,),
        Phase.READY_FOR_MERGE: (R.MERGE,),
        Phase.DONE: (),
    }
    assert unknown.routes == expected.get(phase, (R.INVESTIGATE,))


@pytest.mark.parametrize("code", list(REFUSAL_ROUTES), ids=lambda c: c.value)
def test_a_refusal_takes_the_view_it_is_given(code):
    """The CLI classifies a refusal in no view (it refuses a namespace's state
    dir before it loads one); the corpus reads a namespace's refused state in
    the scheduler's view, where 'run --force' is refused and the scheduler's
    commands release the issue, and in an unknown view only the routes both
    views share are given."""
    standalone = StopClassification(Out.REFUSED, code, ReasonSource.EXCEPTION, "x")
    assert standalone.routes == REFUSAL_ROUTES[code]
    scheduled = StopClassification(Out.REFUSED, code, ReasonSource.EXCEPTION, "x", scheduler=True)
    unknown = StopClassification(Out.REFUSED, code, ReasonSource.EXCEPTION, "x", scheduler=None)
    assert R.NEW_RUN not in scheduled.routes and R.RESUME not in scheduled.routes
    assert set(unknown.routes) <= {R.INVESTIGATE} | (set(standalone.routes) & set(scheduled.routes))
    if code is C.STATE_INVALID:
        assert scheduled.routes == (R.INVESTIGATE, R.SCHEDULER)
        assert unknown.routes == (R.INVESTIGATE,)
    else:
        assert scheduled.routes == unknown.routes == standalone.routes


@pytest.mark.parametrize(
    "text",
    [
        "review-round-cap",
        "StopReasonCode.MERGE_CONFLICT",
        "transient: GitHub returned 502; run 'resume'",
        "agent reported success; safe to merge",
    ],
)
@pytest.mark.parametrize("phase", [Phase.BLOCKED, Phase.FAILED])
def test_a_persisted_reason_is_never_read_for_a_code(text, phase):
    """Unknown legacy text, or text an agent wrote, is never a classification."""
    found = classify_state(_state(phase, block_reason=text))
    assert found.code is C.UNKNOWN
    assert found.routes == OUTCOME_ROUTES[found.outcome]
    assert text not in found.line()


def test_no_state_awaits_input_until_126_lands():
    """#126's boundary: the default pause predicate holds for no phase, and a
    predicate that does gives AWAITING_INPUT with no route at all."""
    for phase in Phase:
        assert classify_state(_state(phase)).outcome is not Out.AWAITING_INPUT
        paused = classify_state(_state(phase), paused=lambda s: True)
        assert (paused.outcome, paused.code) == (Out.AWAITING_INPUT, C.AWAITING_INPUT)
        assert paused.routes == ()
        assert "no route answers a pause until #126 lands" in paused.line()


@pytest.mark.parametrize("kind", UNLOADABLE_STATES)
def test_a_local_state_is_refused_never_given_an_unblock(tmp_state_dir, kind):
    """LOCAL is removed (ADR 0009): its state does not load, and the refusal
    classifies with no route that acts on the run; LOCAL gains no edge. A
    newer label, an unknown mode and a torn file are refused alike."""
    eng = make_engine(tmp_state_dir, [])
    eng.state.phase = Phase.BLOCKED
    eng._save()
    path = eng.paths.state_file
    path.write_bytes(unloadable_state(kind, path.read_bytes())[0])
    with pytest.raises(errors.StateError) as caught:
        load_state(path)
    found = classify_exception(caught.value)
    assert found.outcome is Out.REFUSED
    assert found.code in (C.STATE_INVALID, C.STATE_PROTOCOL)
    assert R.UNBLOCK not in found.routes and R.RESUME not in found.routes


# -- classification is advice, never authorization ----------------------------------------


def _imports(path: Path) -> set[str]:
    """The modules ``path`` imports, as written (``from . import x`` gives ``x``)."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.ImportFrom):
            if node.module:
                names.add(node.module)
            else:
                names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
    return names


def test_only_the_sinks_and_the_cli_import_the_classification():
    """No module that decides a transition, a retry, a write, a permission or a
    budget can read a classification: only the engine (which names a sink's
    code), the CLI (which prints it) and the recovery package (#374, which
    renders it for ``status``, and which only the CLI imports) import the
    module."""
    importers = {
        path.stem
        for path in SRC.rglob("*.py")
        if path.stem != "stop_classification"
        and {"stop_classification", "autoforge.stop_classification"} & _imports(path)
    }
    assert importers == {"engine", "cli", "recovery_package"}
    package_importers = {
        path.stem
        for path in SRC.rglob("*.py")
        if {"recovery_package", "autoforge.recovery_package"} & _imports(path)
    }
    assert package_importers == {"cli"}
    own = _imports(SRC / "stop_classification.py")
    stdlib = {"__future__", "collections.abc", "dataclasses", "enum", "typing"}
    assert own - stdlib == {"errors", "transitions", "state"}  # state: typing only


def test_the_engine_only_names_codes_and_never_reads_a_route():
    """The engine builds classifications at its sinks and puts them on the
    outcome; it never classifies a state or an error, and never reads what a
    classification says about routes, so no decision can depend on one."""
    tree = ast.parse((SRC / "engine.py").read_text())
    used = {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and node.attr in ("routes", "live_facts_required", "family")
    }
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert used == set()
    assert not called & {"classify_state", "classify_exception"}
    # Every BLOCKED a verify or apply step returns goes through `_blocked`.
    source = (SRC / "engine.py").read_text()
    assert source.count("return Phase.BLOCKED,") == 1  # `_blocked` itself


def _stopping(status: str, message: str = "need decision") -> dict:
    return {"phase": "ANALYZE_EXECUTE", "status": status, "message": message}


def test_nothing_of_a_classification_is_persisted(tmp_state_dir, fake_github):
    """ADR 0011 D5: no state field, no protocol change; the outcome carries it."""
    assert "stop" in {f.name for f in fields(StepOutcome)}
    assert not [f.name for f in fields(AutoForgeState) if "stop" in f.name]
    eng = make_engine(tmp_state_dir, [block(_stopping("blocked"))], github=fake_github, origin=True)
    eng.state.phase = Phase.ANALYZE_EXECUTE
    eng._save()
    before = json.loads(eng.paths.state_file.read_text())
    out = eng.step()
    assert out.stop is not None and out.stop.code is C.AGENT_BLOCKED
    after = json.loads(eng.paths.state_file.read_text())
    assert set(after) == set(before)
    assert after["protocol_version"] == before["protocol_version"]
    assert C.AGENT_BLOCKED.value not in eng.paths.state_file.read_text()


def test_unblock_takes_no_classification():
    for name in ("unblock", "_unblock_decision", "unblock_preview"):
        params = inspect.signature(getattr(ControllerEngine, name)).parameters
        assert not [p for p in params if "stop" in p or "code" in p], name


# -- the fixtures: real stops, each with outcome, code, source and routes ----------------


def _expect(out: StepOutcome, outcome: StopOutcome, code: StopReasonCode, source: ReasonSource):
    assert out.stop is not None, out.message
    assert (out.stop.outcome, out.stop.code, out.stop.source) == (outcome, code, source)
    assert out.stop.routes == OUTCOME_ROUTES[outcome]
    return out.stop


def test_fixture_review_round_cap_at_the_entry(tmp_state_dir):
    """The cap stops a round before it starts: a direct `_block` with its code."""
    gh = FakeGitHub()
    eng = _in_review(tmp_state_dir, gh, [block(review_payload(3, SHA_A, []))], round_done=2)
    eng.config.workflow.max_review_rounds = 2
    eng.state.last_review_result = "stale"
    eng._save()
    stop = _expect(eng.step(), Out.BLOCKED, C.REVIEW_ROUND_CAP, ReasonSource.CONTROLLER)
    assert stop.evidence == "the controller entered BLOCKED from REVIEW"


def test_fixture_review_round_cap_after_a_round_with_findings(tmp_state_dir):
    """The cap reached by a round that ended with findings: the apply step's code."""
    gh = FakeGitHub()
    agent = _loop_agent(gh, _one_finding_per_round)
    eng = make_engine(tmp_state_dir, agent, github=gh, origin=True)
    eng.config.workflow.max_review_rounds = 2
    _no_stagnation(eng.config)
    eng._save()
    outcomes = eng.run(max_steps=50)
    assert outcomes[-1].next_phase == "BLOCKED"
    _expect(outcomes[-1], Out.BLOCKED, C.REVIEW_ROUND_CAP, ReasonSource.CONTROLLER)
    assert all(o.stop is None for o in outcomes[:-1])


def test_fixture_review_stagnation(tmp_state_dir):
    gh = FakeGitHub()
    texts = {1: "Add a regression test", 2: "  add   A REGRESSION test  "}
    agent = _loop_agent(gh, lambda rnd: _one_finding_per_round(rnd, texts[rnd]))
    eng = make_engine(tmp_state_dir, agent, github=gh, origin=True)
    outcomes = eng.run(max_steps=50)
    assert "identical resolutions" in eng.state.block_reason
    _expect(outcomes[-1], Out.BLOCKED, C.REVIEW_STAGNATION, ReasonSource.CONTROLLER)


def test_fixture_step_budget(tmp_state_dir):
    gh = FakeGitHub()
    eng = make_engine(
        tmp_state_dir, _loop_agent(gh, _one_finding_per_round), github=gh, origin=True
    )
    eng.config.workflow.max_total_steps = 2
    eng._save()
    outcomes = eng.run(max_steps=50)
    _expect(outcomes[-1], Out.BLOCKED, C.STEP_BUDGET, ReasonSource.CONTROLLER)


@pytest.mark.parametrize("policy", ["approve", "auto"])
def test_fixture_operator_actions_k10(tmp_state_dir, policy):
    """K10: a FIX plan with operator actions waits for the operator's comments."""
    eng = _operating(tmp_state_dir, FakeGitHub(), fixer(BOTH), policy=policy)
    out = eng.step()
    assert out.next_phase == "BLOCKED"
    stop = _expect(out, Out.BLOCKED, C.OPERATOR_ACTIONS, ReasonSource.CONTROLLER)
    assert stop.family.value == "operator_wait"


def test_fixture_true_pr_conflict_at_the_merge_gate(tmp_state_dir, fake_github):
    pr = fake_github.add_pr(head_sha=SHA_A)
    pr.mergeable = "CONFLICTING"
    eng = _in_ready(tmp_state_dir, fake_github)
    out = eng.step(allow_merge=True)
    assert "mergeable=CONFLICTING" in eng.state.block_reason
    _expect(out, Out.BLOCKED, C.MERGE_CONFLICT, ReasonSource.CONTROLLER)


def test_fixture_failing_checks_are_reported_before_a_conflict(tmp_state_dir, fake_github):
    """The gate reports checks first; the code is the one the sink reported."""
    pr = fake_github.add_pr(head_sha=SHA_A)
    pr.mergeable = "CONFLICTING"
    pr.checks = [ci_check(conclusion="FAILURE")]
    eng = _in_ready(tmp_state_dir, fake_github)
    _expect(eng.step(allow_merge=True), Out.BLOCKED, C.MERGE_CHECKS, ReasonSource.CONTROLLER)


@pytest.mark.parametrize("phase", [Phase.READY_FOR_MERGE, Phase.MERGE])
def test_fixture_checks_refused_291_is_typed_never_a_pass(tmp_state_dir, fake_github, phase):
    """#291 (fixed) as a regression negative: GitHub refusing the checks is its
    own typed reason, never a merge-gate pass and never a not-found."""
    fake_github.add_pr(head_sha=SHA_A)
    fake_github.checks_error = checks_refused()
    eng = _in_merge(tmp_state_dir, fake_github, phase=phase)
    out = eng.step(allow_merge=True)
    _expect(out, Out.BLOCKED, C.GITHUB_CHECKS_REFUSED, ReasonSource.CONTROLLER)
    assert fake_github.merges == []


def test_fixture_327_a_pr_body_gh_sanitizes_is_no_conflict(tmp_state_dir, fake_github):
    """#327 / PR #330 (fixed) as a regression negative: a body `gh` prints
    otherwise is judged on the exact read, so ANALYZE_EXECUTE advances and
    no stop of any kind, an effect conflict least of all, is classified."""
    fake_github.gh_sanitizes = True
    eng = make_engine(
        tmp_state_dir,
        lambda req: implement(req, pr_body=ESCAPE_BODY),
        github=fake_github,
        origin=True,
    )
    out = _analyzed(eng)
    assert out.next_phase == "REVIEW" and out.stop is None


def test_fixture_pending_effect_conflict(tmp_state_dir, fake_github):
    """A journaled effect GitHub shows otherwise: the PR #314 conflict a
    controller before #327 met when it compared the body `gh` printed. The
    record keeps its attempt and the run blocks with the record's code; the
    exact read (the fixture above) is what keeps today's run out of it."""
    fake_github.gh_sanitizes = True
    eng = make_engine(
        tmp_state_dir,
        lambda req: implement(req, pr_body=ESCAPE_BODY),
        github=fake_github,
        origin=True,
    )
    fake_github.get_pr_exact = fake_github.get_pr
    out = _analyzed(eng)
    assert "carries its identity but is not the object" in eng.state.block_reason
    _expect(out, Out.BLOCKED, C.EFFECT_CONFLICT, ReasonSource.CONTROLLER)
    assert [w[0] for w in fake_github.effect_writes] == ["create_pull_request"]


@pytest.mark.parametrize(
    "status, outcome, code",
    [("blocked", Out.BLOCKED, C.AGENT_BLOCKED), ("failure", Out.FAILED, C.AGENT_FAILURE)],
)
def test_fixture_an_agent_reports_it_stopped(tmp_state_dir, fake_github, status, outcome, code):
    eng = make_engine(
        tmp_state_dir, [block(_stopping(status, "cannot"))], github=fake_github, origin=True
    )
    eng.state.phase = Phase.ANALYZE_EXECUTE
    stop = _expect(eng.step(), outcome, code, ReasonSource.AGENT)
    assert load_state(eng.paths.state_file).phase.value == outcome.value.upper()
    assert "cannot" not in stop.line()  # the agent's text is quoted elsewhere, never evidence


def test_fixture_resumable_timeout_keeps_the_phase(tmp_state_dir, fake_github):
    """A killed agent: the phase is unchanged and the error's class says resume."""

    class TimingOut(ScriptedProvider):
        def execute(self, req):
            self.calls.append(req)
            return AgentExecutionResult(
                command=["x"],
                exit_code=-1,
                stdout="",
                stderr="",
                started_at="t",
                finished_at="t",
                timed_out=True,
            )

    eng = make_engine(tmp_state_dir, [], github=fake_github, origin=True)
    eng.providers._overrides = {"claude": TimingOut(), "opencode": TimingOut()}
    eng.state.phase = Phase.ANALYZE_EXECUTE
    with pytest.raises(ExecutionTimeoutError) as caught:
        eng.step()
    assert load_state(eng.paths.state_file).phase is Phase.ANALYZE_EXECUTE
    found = classify_exception(caught.value)
    assert (found.outcome, found.code) == (Out.RESUMABLE_ERROR, C.EXECUTION_TIMEOUT)
    assert found.routes == (R.RESUME,) and found.live_facts_required


def test_fixture_a_post_agent_run_log_refusal_keeps_the_phase_for_resume(tmp_state_dir):
    """R2-F1, the post-agent journal refusal of
    ``test_review_post_agent_journal_refusal_keeps_the_phase_for_resume``: the
    reviewer returned, the journal append was refused, the phase and the
    persisted attempt are kept for repair-then-resume. The error is a
    StateError (exit 2), and classifies as the resumable error it is, never as
    a refusal routed to a new run."""
    from autoforge.runlog import MAX_EVENT_JOURNAL_BYTES

    gh = FakeGitHub()
    eng = _in_review(tmp_state_dir, gh, None)
    journal = Path(eng.paths.logs_dir) / eng.state.run_id / "events.jsonl"

    def reviews_then_enlarges_the_journal(req):
        journal.parent.mkdir(parents=True, exist_ok=True)
        journal.touch()
        os.truncate(journal, 2 * MAX_EVENT_JOURNAL_BYTES)
        return block(review_payload(1, SHA_A, []))

    eng.provider._handler = reviews_then_enlarges_the_journal
    with pytest.raises(errors.StateError, match="after the agent had returned") as caught:
        eng.step()
    saved = load_state(eng.paths.state_file)
    assert (saved.phase, saved.attempt) == (Phase.REVIEW, 1)
    found = classify_exception(caught.value)
    assert (found.outcome, found.code) == (Out.RESUMABLE_ERROR, C.RUN_LOG_REFUSED)
    assert found.routes == (R.RESUME,) and found.live_facts_required
    eng.close()


def test_fixture_a_refused_step_directory_after_the_attempt_is_resumable(
    tmp_state_dir, monkeypatch
):
    """R2-F1: the run log's refusal of the step directory, after the attempt
    was saved and before the launch, is marked the same way."""
    from autoforge.runlog import RunLogger

    def refuses(self, phase, attempt):
        raise errors.StateError("corrupted run log directory")

    monkeypatch.setattr(RunLogger, "begin_step", refuses)
    eng = _in_review(tmp_state_dir, FakeGitHub(), [block(review_payload(1, SHA_A, []))])
    with pytest.raises(errors.StateError, match="before the agent was launched") as caught:
        eng.step()
    assert eng.provider.calls == []
    saved = load_state(eng.paths.state_file)
    assert (saved.phase, saved.attempt) == (Phase.REVIEW, 1)
    found = classify_exception(caught.value)
    assert (found.outcome, found.code) == (Out.RESUMABLE_ERROR, C.RUN_LOG_REFUSED)


def test_fixture_a_plain_reason_at_a_funnel_is_unknown_and_still_blocked(tmp_state_dir):
    """The safe fallback: a BLOCKED reason no sink coded is UNKNOWN, and the
    outcome and its routes are what BLOCKED always allows."""
    eng = make_engine(tmp_state_dir, [])
    eng.state.phase = Phase.REVIEW
    out = eng._block(Phase.REVIEW, None, "plain text, say 'review-round-cap'")
    _expect(out, Out.BLOCKED, C.UNKNOWN, ReasonSource.CONTROLLER)
    out = eng._block(Phase.REVIEW, None, CodedReason(C.MERGE_CONFLICT, "coded"))
    assert out.stop is not None and out.stop.code is C.MERGE_CONFLICT
    assert eng.state.block_reason == "coded"


def test_fixture_a_conclusive_next_issue_read_failure_routes_to_unblock(tmp_state_dir, fake_github):
    """UPDATE_EPIC's conclusive read failure of the next issue blocks, and the
    reason it saves says 'unblock', as its stop line does: 'resume' refuses a
    BLOCKED run."""
    fake_github.get_issue_errors[ISSUE3] = errors.GitHubError("HTTP 403: not accessible")
    eng = _in_update_epic(tmp_state_dir, fake_github, [_epic_result(ISSUE3)])
    stop = _expect(eng.step(), Out.BLOCKED, C.GITHUB_READ_FAILED, ReasonSource.CONTROLLER)
    assert stop.routes[0] is R.UNBLOCK and R.RESUME not in stop.routes
    saved = load_state(eng.paths.state_file)
    assert saved.phase is Phase.BLOCKED and saved.current_issue_url == ISSUE
    assert saved.block_reason.startswith(f"next issue {ISSUE3} could not be verified")
    assert saved.block_reason.endswith("Fix the cause, then 'unblock'.")
    assert "resume" not in saved.block_reason


# -- drift guards --------------------------------------------------------------------------


def _literal_text(node: ast.AST) -> str:
    return "".join(
        n.value for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str)
    )


def test_no_blocked_reason_advises_resume_without_unblock():
    """'resume' refuses a BLOCKED run, so a reason the engine blocks with that
    names 'resume' names 'unblock' too; so do the reasons
    ``_implementation_pr_candidate`` returns for its callers to block with."""
    tree = ast.parse((SRC / "engine.py").read_text())
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = getattr(node.func, "attr", getattr(node.func, "id", ""))
            text = _literal_text(node)
            if name in ("_blocked", "_block") and "resume" in text and "unblock" not in text:
                offenders.append(node.lineno)
        if isinstance(node, ast.FunctionDef) and node.name == "_implementation_pr_candidate":
            body = node.body[1:]  # its docstring is no reason
            text = "".join(_literal_text(stmt) for stmt in body)
            assert text and "resume" not in text
    assert offenders == []


def test_every_code_is_produced_by_some_sink():
    """No code is declared for a sink that does not exist: each is named by the
    engine, by an error class, by the agent-status sink or by a loaded state."""
    engine = (SRC / "engine.py").read_text()
    named = {code for code in StopReasonCode if f"StopReasonCode.{code.name}" in engine}
    named |= {code for _, code in EXCEPTION_CLASSES.values()}
    # The run log's refusals the engine marks after it saved a launch's attempt.
    assert engine.count("exc.interrupted_attempt = True") == 2
    marked = errors.StateError("s")
    marked.interrupted_attempt = True
    named.add(classify_exception(marked).code)
    named |= {agent_status(s, Phase.FIX).code for s in ("blocked", "failure")}
    for phase in Phase:
        for scheduler in (False, True):
            named.add(classify_state(_state(phase), scheduler=scheduler).code)
    named.add(classify_state(_state(Phase.REVIEW), paused=lambda s: True).code)
    assert named == set(StopReasonCode)


ADR = SRC.parent.parent / "docs" / "adr"
ADR_0011 = ADR / "0011-typed-stop-classification-and-recovery-baseline.md"
ADR_0012 = ADR / "0012-recovery-and-escalation-stop-authority-and-advisor-gates.md"


def _undefined_tests_named_in(adr: Path) -> list[str]:
    """The tests ``adr`` names in backticks that no ``tests/test_*.py`` defines."""
    named = set(re.findall(r"`(test_[a-z0-9_]+)`", adr.read_text()))
    assert named
    defined = {
        node.name
        for path in (SRC.parent.parent / "tests").glob("test_*.py")
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.FunctionDef)
    }
    return sorted(named - defined)


def test_every_test_adr_0011_names_exists():
    assert _undefined_tests_named_in(ADR_0011) == []


def test_every_test_adr_0012_names_exists():
    assert _undefined_tests_named_in(ADR_0012) == []


def _adr_0012_table(decision: str) -> dict[str, list[str]]:
    """The rows of ADR 0012's ``### <decision>.`` section whose first cell is
    one code span, keyed by it, each row as its stripped cells."""
    text = ADR_0012.read_text()
    start = text.index(f"\n### {decision}. ")
    section = text[start : text.index("\n### ", start + 1)]
    rows = {}
    for line in section.splitlines():
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if line.startswith("| `") and re.fullmatch(r"`[a-z_]+`", cells[0]):
            rows[cells[0].strip("`")] = cells
    return rows


def test_adr_0012_stop_matrix_takes_every_outcome_that_needs_recovery():
    """ADR 0012 D2 says who leaves each stop and how. An outcome added to
    ``StopOutcome`` has no row there until someone decides it; the stops by
    design keep ADR 0011's routes and are named instead."""
    by_design = {Out.READY_FOR_MERGE, Out.HANDOFF, Out.DONE, Out.IN_PROGRESS}
    assert set(_adr_0012_table("D2")) == {o.value for o in set(Out) - by_design}
    text = ADR_0012.read_text()
    paragraph = text[text.index("The stops by design") :].split("\n\n")[0]
    assert {f"`{o.value}`" for o in by_design} <= set(re.findall(r"`[a-z_]+`", paragraph))


def test_adr_0012_gate_a_takes_every_reason_family_and_code():
    """ADR 0012 D5's no-go for autonomous recovery rests on every family of
    stop codes failing one of its three conditions. A family or a code added
    later has no row there until someone decides whether it reopens gate A,
    the ``intentional`` family's included: its row says it is no recovery
    case, and a code added to it is a decision like any other (PR #446
    R1-F4)."""
    rows = _adr_0012_table("D5")
    assert set(rows) == {family.value for family in ReasonFamily}
    for family in ReasonFamily:
        codes = {code.value for code, of in REASON_FAMILY.items() if of is family}
        assert codes, family
        assert set(re.findall(r"`([a-z0-9-]+)`", rows[family.value][1])) == codes, family
