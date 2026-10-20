"""The provider-neutral interaction contract, its broker and the terminal resolver (#134).

ADR 0007 D5.1, D5.2, D5.4, D7.1-D7.4, D7.6, D7.7 and D10.5. The contract is
held at its own boundary (construction refuses what it cannot render or
bind), the broker's decisions are driven by a scripted resolver and a fake
clock, the terminal resolver reads a pipe or a real pseudo-terminal, and the
engine is driven by a scripted adapter that asks through the launch's broker
as an SDK adapter would. No provider, SDK or helper runs: none can launch in
this release.
"""

from __future__ import annotations

import ast
import dataclasses
import io
import json
import os
import pty
import re
import select
import subprocess
import sys
import termios
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

import autoforge.interaction as interaction_module
from autoforge.cli import TerminalResolver, _attach_terminal
from autoforge.errors import (
    ExecutionError,
    InteractionDataError,
    InteractionRequiredError,
    LockError,
)
from autoforge.interaction import (
    NO_CAPABILITIES,
    Allow,
    AllowScope,
    Answer,
    Cancel,
    Capabilities,
    DecisionKind,
    Deliver,
    Deny,
    Fail,
    InteractionBroker,
    InteractionDecision,
    InteractionOutcome,
    InteractionRequest,
    Lifecycle,
    LocalBinding,
    Pause,
    Question,
    QuestionOption,
    QuestionResponse,
    RemoteBinding,
    RequestKind,
    SessionIdentity,
    Unattended,
    Unavailable,
    UnavailableReason,
    decision_problem,
    required_offers,
)
from autoforge.locking import ControllerLock
from autoforge.providers import AgentExecutionResult, ScriptedProvider
from autoforge.sdk_policy import (
    MAX_ANSWER_CHARS,
    MAX_OPTIONS,
    MAX_QUESTION_CHARS,
    MAX_QUESTIONS,
    InteractionSettings,
    input_digest,
)
from autoforge.state import load_state
from autoforge.transitions import Phase

from .conftest import commit_all, git_repo, implement, make_engine, make_local_engine, write_feature

TOKEN = "ghp_" + "a1B2" * 9
DIGEST = input_digest({"command": "make test"}, "a test input")
LIVE = Capabilities(live_questions=True, live_permissions=True)
PROMPT = InteractionSettings(policy="prompt")
FAIL = InteractionSettings(policy="fail")
PAUSE = InteractionSettings(policy="pause")
NOW = 1000.0
FAR = NOW + 3600.0


class Clock:
    """A monotonic clock a test moves by hand."""

    def __init__(self, now: float = NOW) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class Scripted:
    """A resolver answering each request with the next scripted outcome (a
    decision, an :class:`Unavailable`, an exception to raise, or a callable
    of the request and the deadline); ``seen`` records what it was asked."""

    def __init__(self, *outcomes, clock: Clock | None = None, takes: float = 0.0) -> None:
        self.outcomes = list(outcomes)
        self.seen: list[tuple[InteractionRequest, float]] = []
        self.clock = clock
        self.takes = takes

    def resolve(self, request, deadline):
        self.seen.append((request, deadline))
        if self.clock is not None:
            self.clock.now += self.takes
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome(request, deadline) if callable(outcome) else outcome


def question(*labels: str, text: str = "Which database?", header: str = "Database", **kw):
    return Question(
        text=text,
        header=header,
        options=tuple(QuestionOption(label, f"use {label}") for label in labels or ("A", "B")),
        **kw,
    )


def broker(settings=PROMPT, capabilities=LIVE, *outcomes, clock=None, pause=False, takes=0.0):
    clock = clock or Clock()
    resolver = Scripted(*outcomes, clock=clock, takes=takes)
    return (
        InteractionBroker(settings, capabilities, resolver, pause_available=pause, clock=clock),
        resolver,
    )


def permission(b: InteractionBroker, tool: str = "Bash", **kw) -> InteractionRequest:
    return b.permission("toolu_01", tool, "command: make test", DIGEST, **kw)


# -- the contract (D5.1) ----------------------------------------------------------
def test_a_question_is_cleaned_and_redacted_where_it_is_shown_and_kept_exact_where_it_binds():
    q = Question(
        text="Pick \x1b[31mone\x1b[0m\nof these",
        header=f"DB {TOKEN}",
        options=(
            QuestionOption("Postgres", "the \x07default " + "x" * 600),
            QuestionOption("SQLite"),
        ),
        multiple=True,
    )
    assert q.text == "Pick one of these"
    assert TOKEN not in q.header and "REDACTED" in q.header
    assert "\x07" not in q.options[0].description and len(q.options[0].description) <= 500
    assert [o.label for o in q.options] == ["Postgres", "SQLite"]
    # Idempotent: what was built rebuilds equal, as a reload would.
    assert dataclasses.replace(q) == q


@pytest.mark.parametrize(
    "build, rule",
    [
        (lambda: question(text="q" * (MAX_QUESTION_CHARS + 1)), "longer than"),
        (lambda: question(text="   "), "no text"),
        (lambda: question(*[f"o{n}" for n in range(MAX_OPTIONS + 1)]), "more than"),
        (lambda: Question("q", ()), "no list of options"),
        (lambda: question("A", "a"), "shown alike"),
        (lambda: QuestionOption("bad\x1b[2Jlabel"), "control or escape"),
        (lambda: QuestionOption(f"key {TOKEN}"), "credential-shaped"),
        (lambda: QuestionOption("  "), "blank"),
        (lambda: QuestionOption("x" * 201), "longer than"),
        (lambda: Question("q", ("A",)), "not a QuestionOption"),  # type: ignore[arg-type]
        (lambda: question(multiple="yes"), "not a boolean"),
    ],
)
def test_a_question_the_operator_could_not_decide_on_is_refused_without_its_value(build, rule):
    with pytest.raises(InteractionDataError, match=rule) as excinfo:
        build()
    assert TOKEN not in str(excinfo.value) and "\x1b" not in str(excinfo.value)


def _request(**kw) -> InteractionRequest:
    fields = dict(
        request_id="1",
        provider_request_id="toolu_01",
        kind=RequestKind.PERMISSION,
        offers=frozenset({DecisionKind.ALLOW_ONCE}),
        tool_name="Bash",
        summary="command: ls",
        input_sha256=DIGEST,
    )
    fields.update(kw)
    return InteractionRequest(**fields)


@pytest.mark.parametrize(
    "fields, rule",
    [
        ({"request_id": ""}, "blank"),
        ({"provider_request_id": "a\nb"}, "more than one line"),
        ({"provider_request_id": "x" * 257}, "longer than"),
        ({"provider_request_id": TOKEN}, "credential-shaped"),
        ({"kind": "elicitation"}, "unknown kind"),
        ({"offers": frozenset({"approve_forever"})}, "unknown decision"),
        ({"offers": frozenset({DecisionKind.ANSWER})}, "cannot take"),
        ({"tool_name": ""}, "blank"),
        ({"tool_name": "x" * 129}, "longer than"),
        ({"input_sha256": "abc"}, "not a SHA-256"),
        ({"agent": "sub\tagent"}, "more than one line"),
        ({"questions": (question(),)}, "carries questions"),
    ],
)
def test_a_permission_request_that_cannot_be_bound_is_refused(fields, rule):
    with pytest.raises(InteractionDataError, match=rule):
        _request(**fields)


@pytest.mark.parametrize(
    "fields, rule",
    [
        ({"questions": ()}, "carries 0 questions"),
        (
            {"questions": tuple(question(text=f"q{n}") for n in range(MAX_QUESTIONS + 1))},
            "carries 5",
        ),
        ({"questions": (question(), question())}, "same question twice"),
        ({"questions": (question(),), "tool_name": "Bash"}, "carries a tool call"),
        (
            {"questions": (question(),), "offers": frozenset({DecisionKind.CUSTOM_ANSWER})},
            "no question allows",
        ),
        (
            {"questions": (question(),), "offers": frozenset({DecisionKind.ALLOW_ONCE})},
            "cannot take",
        ),
    ],
)
def test_a_question_request_that_cannot_be_rendered_is_refused(fields, rule):
    base = dict(
        kind=RequestKind.QUESTION, offers=frozenset(), tool_name="", summary="", input_sha256=""
    )
    with pytest.raises(InteractionDataError, match=rule):
        _request(**{**base, **fields})


def test_what_names_the_request_on_one_bounded_line():
    assert _request().what == "permission: Bash"
    assert _request(agent="reviewer").what == "permission: Bash, from subagent reviewer"
    q = _request(
        kind="question",
        offers=frozenset(),
        tool_name="",
        summary="",
        input_sha256="",
        questions=(question(text="one"), question(text="two", header="")),
    )
    assert q.kind is RequestKind.QUESTION and q.what == "question: Database (+1 more)"


@pytest.mark.parametrize(
    "build, rule",
    [
        (lambda: QuestionResponse(), "picks nothing"),
        (lambda: QuestionResponse(("A", "A")), "same option twice"),
        (lambda: QuestionResponse(custom_text="  "), "blank"),
        (lambda: QuestionResponse(custom_text=f"use {TOKEN}"), "credential-shaped"),
        (lambda: QuestionResponse(custom_text="redraw\rline"), "control or escape"),
        (lambda: QuestionResponse(custom_text="x" * 2001), "longer than"),
        (lambda: Deny(f"try {TOKEN}"), "credential-shaped"),
        (lambda: Deny(""), "blank"),
        (lambda: Allow("forever"), "unknown scope"),  # type: ignore[arg-type]
        (lambda: Allow(updated_input=["ls"]), "not an object"),  # type: ignore[arg-type]
        (lambda: Allow(updated_input={"command": f"curl -H {TOKEN}"}), "credential-shaped"),
        (lambda: Allow(updated_input={"command": "x" * 70_000}), "bytes of JSON"),
        (lambda: Unavailable("asleep"), "unknown reason"),  # type: ignore[arg-type]
    ],
)
def test_a_decision_that_would_send_something_unseen_or_altered_is_refused(build, rule):
    with pytest.raises(InteractionDataError, match=rule) as excinfo:
        build()
    assert TOKEN not in str(excinfo.value)


def test_required_offers_name_exactly_what_a_decision_needs():
    assert required_offers(Answer((QuestionResponse(("A",)),))) == {DecisionKind.ANSWER}
    assert required_offers(Answer((QuestionResponse(custom_text="mine"),))) == {
        DecisionKind.CUSTOM_ANSWER
    }
    assert required_offers(Answer((QuestionResponse(("A",), "mine"),))) == {
        DecisionKind.ANSWER,
        DecisionKind.CUSTOM_ANSWER,
    }
    assert required_offers(Allow()) == {DecisionKind.ALLOW_ONCE}
    assert required_offers(Allow(AllowScope.SESSION, {"command": "ls"})) == {
        DecisionKind.ALLOW_SESSION,
        DecisionKind.MODIFIED_INPUT,
    }
    assert required_offers(Deny()) == {DecisionKind.DENY}
    assert required_offers(Deny("run the unit tests only")) == {DecisionKind.DENY_GUIDANCE}


def test_a_modified_input_is_a_read_only_snapshot_of_what_was_validated():
    """The whole input, nested values too, is copied when the decision is
    built: changing the caller's object afterwards, or what was sent, never
    changes what the decision carries, so nothing unvalidated is sent."""
    given = {"command": "make lint", "env": {"MODE": "check"}, "args": ["--fix", {"n": 1}]}
    decision = Allow(updated_input=given)
    given["command"] = "rm -rf ~"
    given["env"]["MODE"] = f"curl -H {TOKEN}"
    given["args"].append("--force")
    given["args"][1]["n"] = 2
    expected = {"command": "make lint", "env": {"MODE": "check"}, "args": ["--fix", {"n": 1}]}
    assert decision.input_to_send() == expected
    assert decision.updated_input is not None
    with pytest.raises(TypeError):
        decision.updated_input["command"] = "rm -rf ~"  # type: ignore[index]
    with pytest.raises(TypeError):
        decision.updated_input["env"]["MODE"] = "write"  # type: ignore[index]
    assert isinstance(decision.updated_input["args"], tuple)
    sent = decision.input_to_send()
    assert sent is not None
    sent["env"]["MODE"] = "write"
    sent["args"].append("--force")
    assert decision.input_to_send() == expected
    # A decision rebuilt from its own snapshot is equal, as a reload would be.
    assert dataclasses.replace(decision) == decision
    assert Allow(updated_input={"command": "ls"}).input_to_send() == {"command": "ls"}
    assert Allow().input_to_send() is None
    assert required_offers(Cancel()) == {DecisionKind.CANCEL}


def test_decision_problem_holds_a_decision_to_the_request_it_answers():
    b, _ = broker()
    perm = permission(b)
    single = b.question("q1", [question("A", "B")])
    multi = b.question("q2", [question("A", "B", custom=True, multiple=True)])
    assert decision_problem(perm, Allow()) is None
    assert decision_problem(single, Answer((QuestionResponse(("B",)),))) is None
    assert decision_problem(multi, Answer((QuestionResponse(("A", "B"), "and C"),))) is None
    assert "answer or a cancellation" in decision_problem(single, Allow())
    assert "approval, a denial" in decision_problem(perm, Answer((QuestionResponse(("A",)),)))
    assert "not one of its options" in decision_problem(single, Answer((QuestionResponse(("C",)),)))
    mixed = b.question("q3", [question(text="one"), question(text="two", custom=True)])
    assert "question 1: it allows no answer of the operator's own" in decision_problem(
        mixed, Answer((QuestionResponse(custom_text="C"), QuestionResponse(("A",))))
    )
    assert "takes one answer, got 2" in decision_problem(
        single, Answer((QuestionResponse(("A", "B")),))
    )
    assert "2 responses for 1" in decision_problem(
        single, Answer((QuestionResponse(("A",)), QuestionResponse(("B",))))
    )
    assert "custom_answer" in decision_problem(single, Answer((QuestionResponse(custom_text="C"),)))
    assert decision_problem(perm, "allow") == "it is not a decision"


def test_offers_follow_the_declared_capabilities():
    assert NO_CAPABILITIES.offers(RequestKind.QUESTION, custom=True) == frozenset()
    assert NO_CAPABILITIES.offers(RequestKind.PERMISSION) == frozenset()
    assert LIVE.offers(RequestKind.QUESTION) == {DecisionKind.ANSWER, DecisionKind.CANCEL}
    assert DecisionKind.CUSTOM_ANSWER in LIVE.offers(RequestKind.QUESTION, custom=True)
    # A provider that carries no session approval and no modified input
    # (ADR 0007 §3: Claude's and OpenCode's gaps) is never offered either.
    narrowed = Capabilities(
        live_permissions=True,
        unsupported=frozenset({DecisionKind.ALLOW_SESSION, DecisionKind.MODIFIED_INPUT}),
    )
    assert narrowed.offers(RequestKind.PERMISSION) == {
        DecisionKind.ALLOW_ONCE,
        DecisionKind.DENY,
        DecisionKind.DENY_GUIDANCE,
        DecisionKind.CANCEL,
    }
    assert narrowed.offers(RequestKind.QUESTION) == frozenset()
    with pytest.raises(InteractionDataError, match="unknown decision"):
        Capabilities(unsupported=frozenset({"approve_forever"}))
    with pytest.raises(InteractionDataError, match="not a boolean"):
        Capabilities(live_questions=1)  # type: ignore[arg-type]


def test_a_pause_needs_the_kinds_live_capability_and_a_way_back_to_the_session():
    assert not LIVE.can_pause(RequestKind.QUESTION)
    replay = Capabilities(live_questions=True, durable_replay=True)
    assert replay.can_pause(RequestKind.QUESTION) and not replay.can_pause(RequestKind.PERMISSION)
    message = Capabilities(live_questions=True, live_permissions=True, continuation_recovery=True)
    assert message.can_pause(RequestKind.PERMISSION)


def test_an_outcome_carries_a_pending_request_on_a_pause_and_only_then():
    b, _ = broker()
    perm = permission(b)
    assert InteractionOutcome(Lifecycle.PAUSED, perm).pending == perm
    assert InteractionOutcome("completed").lifecycle is Lifecycle.COMPLETED
    with pytest.raises(InteractionDataError):
        InteractionOutcome(Lifecycle.PAUSED)
    with pytest.raises(InteractionDataError):
        InteractionOutcome(Lifecycle.FAILED, perm)


def _identity(**kw) -> SessionIdentity:
    fields = dict(
        provider="sdk-a",
        sdk_version="0.2.163",
        runtime_version="2.1.286",
        policy_digest="a" * 64,
        profile="analyze_execute",
        profile_digest="b" * 64,
        cwd="/work/tree",
        session_root="/state/sdk/a",
        session_id="1b4e28ba-2fa1-11d2-883f-0016d3cca427",
        workflow=RemoteBinding("owner/repo", 134, "ANALYZE_EXECUTE", 1, 0),
    )
    fields.update(kw)
    return SessionIdentity(**fields)


def test_a_session_identity_is_equal_only_to_one_that_binds_the_same_session():
    """D8.4: a continuation launches only when the current launch would
    produce an identical identity; any one field apart refuses it."""
    assert _identity() == _identity()
    for change in (
        {"runtime_version": "2.1.300"},
        {"policy_digest": "c" * 64},
        {"session_root": "/state/sdk/b"},
        {"workflow": RemoteBinding("owner/repo", 134, "ANALYZE_EXECUTE", 2, 0)},
        {"workflow": LocalBinding("/repo/.git/autoforge/state", "ANALYZE_EXECUTE", 1)},
    ):
        assert _identity(**change) != _identity()


@pytest.mark.parametrize(
    "build, rule",
    [
        (lambda: _identity(policy_digest="A" * 64), "not a SHA-256"),
        (lambda: _identity(cwd="relative/tree"), "absolute path"),
        (lambda: _identity(session_id=TOKEN), "credential-shaped"),
        (lambda: _identity(workflow={"issue": 1}), "no known mode"),
        (lambda: RemoteBinding("owner/repo", 0, "FIX", 1, 0), "at least 1"),
        (lambda: RemoteBinding("o/r", 1, "FIX", 1, 0, head_sha="abc"), "full commit id"),
        (lambda: LocalBinding("/state", "FIX", True), "at least 1"),
        (lambda: LocalBinding("state", "FIX", 1), "absolute path"),
    ],
)
def test_a_session_identity_that_could_not_be_compared_exactly_is_refused(build, rule):
    with pytest.raises(InteractionDataError, match=rule) as excinfo:
        build()
    assert TOKEN not in str(excinfo.value)


def test_the_contract_names_no_provider_and_imports_no_transport():
    """D5.1, D5.4: plain types, no I/O, no provider name and no SDK type. The
    module imports the standard library and two controller modules, the
    error taxonomy and the interaction-data bounds, and nothing else."""
    path = Path(interaction_module.__file__)
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(("." * node.level) + (node.module or ""))
    assert imported == {
        "__future__",
        "enum",
        "json",
        "re",
        "threading",
        "time",
        "collections.abc",
        "dataclasses",
        "types",
        "typing",
        ".errors",
        ".sdk_policy",
    }
    text = path.read_text(encoding="utf-8").lower()
    for name in ("claude", "opencode", "anthropic", "subprocess", "socket", "http"):
        assert name not in text
    assert not re.search(r"\bpi\b", text)


# -- the broker (D5.2, D7.1, D7.3, D7.4, D7.6) ----------------------------------
def test_fail_answers_every_request_with_the_policy_named_and_never_asks():
    b, resolver = broker(FAIL, LIVE, Allow())
    directive = b.handle(permission(b), FAR)
    assert directive == Fail(
        "the agent asked for a decision (permission: Bash); interaction_policy is fail"
    )
    assert resolver.seen == []
    assert b.outcome() == InteractionOutcome(Lifecycle.FAILED)
    assert b.failure == directive.reason and b.ended == directive


def test_prompt_asks_the_resolver_until_the_wait_limit_and_delivers_its_decision():
    clock = Clock()
    b, resolver = broker(PROMPT, LIVE, Allow(), clock=clock, takes=3.0)
    request = permission(b)
    directive = b.handle(request, FAR)
    assert directive == Deliver(Allow())
    assert resolver.seen == [(request, NOW + PROMPT.max_wait_seconds)]
    assert b.outcome() == InteractionOutcome(Lifecycle.COMPLETED) and b.failure is None
    entry = b.record()["requests"][0]
    assert entry["directive"] == "deliver" and entry["decision"] == ["allow_once"]
    assert entry["waited_seconds"] == 3.0


def test_the_wait_ends_at_the_invocations_limit_when_that_comes_first_and_never_pauses():
    """D7.3: the resolver's deadline is the earlier of the two; only the
    wait's own expiry is D7.4's, since a ceiling leaves no time to tear down
    for a pause."""
    pausable = Capabilities(live_permissions=True, durable_replay=True)
    b, resolver = broker(PROMPT, pausable, Unavailable(UnavailableReason.EXPIRED), pause=True)
    directive = b.handle(permission(b), NOW + 30)
    assert resolver.seen[0][1] == NOW + 30
    assert directive == Fail(
        "the agent asked for a decision (permission: Bash); the invocation's limit fell due "
        "during the wait"
    )


def test_no_time_left_before_the_invocations_limit_fails_without_asking():
    b, resolver = broker(PROMPT, LIVE, Allow())
    assert "no time is left" in b.handle(permission(b), NOW).reason
    assert resolver.seen == []


@pytest.mark.parametrize(
    "reason, text",
    [
        (UnavailableReason.NO_TERMINAL, "the controller's stdin is not a terminal"),
        (UnavailableReason.END_OF_INPUT, "the operator's input ended"),
        (UnavailableReason.EXPIRED, "no decision came within max_wait_seconds (120s)"),
    ],
)
def test_a_live_wait_that_cannot_be_answered_fails_while_no_pause_is_available(reason, text):
    """D7.4 in this release: #126 has not landed, so the three degradations fail."""
    pausable = Capabilities(live_permissions=True, durable_replay=True)
    b, _ = broker(PROMPT, pausable, Unavailable(reason))
    directive = b.handle(permission(b), FAR)
    assert directive == Fail(
        f"the agent asked for a decision (permission: Bash); {text}, and no pause is "
        "available for it"
    )
    assert b.record()["requests"][0]["unavailable"] == str(reason)


@pytest.mark.parametrize("reason", list(UnavailableReason))
def test_a_live_wait_pauses_where_the_pause_is_available_and_the_kind_can_pause(reason):
    """D7.4 once #126 lands: the broker's half, ready for it."""
    pausable = Capabilities(live_permissions=True, live_questions=True, durable_replay=True)
    b, _ = broker(PROMPT, pausable, Unavailable(reason), pause=True)
    request = permission(b)
    assert b.handle(request, FAR) == Pause(request)
    assert b.outcome() == InteractionOutcome(Lifecycle.PAUSED, request)
    # The kind whose decision the provider cannot carry live never pauses.
    replay_questions = Capabilities(live_questions=True, durable_replay=True)
    b, _ = broker(PROMPT, replay_questions, Unavailable(reason), pause=True)
    assert isinstance(b.handle(permission(b), FAR), Fail)


def test_prompt_without_a_live_capability_fails_and_nothing_else_degrades():
    """Every adapter declares NO_CAPABILITIES in this release: under prompt
    an ask has nothing to offer and fails, before anyone is asked."""
    b, resolver = broker(PROMPT, NO_CAPABILITIES, Allow(), pause=True)
    request = b.question("q1", [question()])
    assert request.offers == frozenset()
    directive = b.handle(request, FAR)
    assert "cannot carry a live question decision" in directive.reason
    assert resolver.seen == []


def test_pause_pauses_where_it_can_and_fails_otherwise():
    b, resolver = broker(
        PAUSE, Capabilities(live_questions=True, continuation_recovery=True), pause=True
    )
    request = b.question("q1", [question()])
    assert b.handle(request, FAR) == Pause(request) and resolver.seen == []
    b, _ = broker(PAUSE, LIVE, pause=True)
    assert "interaction_policy is pause, but no pause" in b.handle(permission(b), FAR).reason


def test_the_aggregate_limit_counts_every_request_and_fails_the_next():
    settings = InteractionSettings(policy="prompt", max_interactions=2)
    b, resolver = broker(settings, LIVE, Allow(), Deny("not that one"), Allow())
    assert isinstance(b.handle(permission(b), FAR), Deliver)
    assert isinstance(b.handle(permission(b), FAR), Deliver)
    third = b.handle(permission(b), FAR)
    assert (
        third.reason.startswith("interaction limit reached:")
        and "(max_interactions)" in third.reason
    )
    assert len(resolver.seen) == 2 and b.handled == 3
    for _ in range(10):
        assert isinstance(b.handle(permission(b), FAR), Fail)
    record = b.record()
    assert record["handled"] == 13 and len(record["requests"]) == 3
    assert record["requests"][2]["reason"] == third.reason


@pytest.mark.parametrize("settings", [FAIL, PROMPT])
def test_a_provider_that_keeps_asking_cannot_grow_the_record_past_its_bound(settings):
    """Requests after the interactions ended or past the aggregate limit all
    fail, and the record keeps max_interactions of them and the first one
    past it, however many more come."""
    settings = dataclasses.replace(settings, max_interactions=2)
    b, resolver = broker(settings, LIVE, Unavailable(UnavailableReason.NO_TERMINAL))
    directives = [b.handle(permission(b), FAR) for _ in range(50)]
    assert all(isinstance(directive, Fail) for directive in directives)
    assert len(resolver.seen) == (1 if settings.policy == "prompt" else 0)
    record = b.record()
    assert b.handled == record["handled"] == 50
    assert len(record["requests"]) == 3
    assert [entry["request_id"] for entry in record["requests"]] == ["1", "2", "3"]
    assert "after an earlier request ended" in directives[-1].reason
    assert b.failure == directives[0].reason


@pytest.mark.parametrize(
    "decision, problem",
    [
        (Allow(AllowScope.SESSION), "allow_session"),
        (Allow(updated_input={"command": "make lint"}), "modified_input"),
        (Deny("run lint instead"), "deny_guidance"),
        (Answer((QuestionResponse(("A",)),)), "a permission takes"),
        ("yes", "it is not a decision"),
    ],
)
def test_a_decision_the_provider_cannot_carry_is_never_delivered(decision, problem):
    caps = Capabilities(
        live_permissions=True,
        unsupported=frozenset(
            {DecisionKind.ALLOW_SESSION, DecisionKind.MODIFIED_INPUT, DecisionKind.DENY_GUIDANCE}
        ),
    )
    b, _ = broker(PROMPT, caps, decision)
    directive = b.handle(permission(b), FAR)
    assert isinstance(directive, Fail) and "the decision was not delivered" in directive.reason
    assert problem in directive.reason


def test_a_request_is_handled_once_and_only_as_it_was_minted():
    b, resolver = broker(PROMPT, LIVE, Allow(), Allow())
    request = permission(b)
    assert isinstance(b.handle(request, FAR), Deliver)
    again = b.handle(request, FAR)
    assert "did not issue, changed or already handled" in again.reason
    # An identity change under a minted id (another tool, another input) and
    # a request from elsewhere never reach the resolver.
    b, resolver = broker(PROMPT, LIVE, Allow())
    minted = permission(b)
    forged = dataclasses.replace(minted, tool_name="Write", input_sha256="0" * 64)
    assert "did not issue, changed" in b.handle(forged, FAR).reason
    assert resolver.seen == []
    other, _ = broker()
    b2, _ = broker()
    assert "did not issue" in b2.handle(permission(other), FAR).reason
    assert "not a request" in b2.handle("toolu_01", FAR).reason  # type: ignore[arg-type]


def test_the_first_failure_ends_the_invocations_interactions():
    b, resolver = broker(PROMPT, LIVE, Unavailable(UnavailableReason.NO_TERMINAL), Allow())
    first = b.handle(permission(b), FAR)
    later = b.handle(permission(b, tool="Write"), FAR)
    assert "after an earlier request ended" in later.reason and len(resolver.seen) == 1
    assert b.failure == first.reason


def test_a_request_after_a_pause_fails_the_invocation():
    """A provider that asks again instead of stopping (Claude ignoring a
    deferral, D9.2) turns the pause into a failure."""
    caps = Capabilities(live_permissions=True, durable_replay=True)
    b, _ = broker(PAUSE, caps, pause=True)
    assert isinstance(b.handle(permission(b), FAR), Pause)
    assert isinstance(b.handle(permission(b), FAR), Fail)
    assert b.outcome() == InteractionOutcome(Lifecycle.FAILED)


def test_a_resolver_that_fails_fails_the_request_and_ctrl_c_is_no_decision():
    b, _ = broker(PROMPT, LIVE, OSError("terminal gone"))
    assert "asking for it failed (OSError)" in b.handle(permission(b), FAR).reason
    b, _ = broker(PROMPT, LIVE, KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        b.handle(permission(b), FAR)
    assert b.ended is None


def test_a_decision_that_comes_after_the_deadline_is_not_taken():
    clock = Clock()
    b, _ = broker(PROMPT, LIVE, Allow(), clock=clock, takes=PROMPT.max_wait_seconds + 1)
    directive = b.handle(permission(b), FAR)
    assert "no decision came within max_wait_seconds" in directive.reason


def test_the_record_is_bounded_and_redacted_and_keeps_no_operator_text():
    b, _ = broker(
        PROMPT,
        LIVE,
        Answer((QuestionResponse(("A",), "a secret plan"),)),
        Deny("the private guidance"),
        Allow(updated_input={"command": "the modified command"}),
    )
    asked = b.question("q1", [question(header=f"key {TOKEN}", custom=True, multiple=True)])
    b.handle(asked, FAR)
    b.handle(permission(b), FAR)
    b.handle(permission(b, tool="Write"), FAR)
    record = b.record()
    text = json.dumps(record)
    for kept_out in (TOKEN, "a secret plan", "the private guidance", "the modified command"):
        assert kept_out not in text
    assert record["policy"] == "prompt" and record["handled"] == 3
    assert [entry["decision"] for entry in record["requests"]] == [
        ["answer", "custom_answer"],
        ["deny_guidance"],
        ["allow_once", "modified_input"],
    ]
    assert record["requests"][1]["input_sha256"] == DIGEST
    assert record["requests"][0]["what"].startswith("question: key ***REDACTED")


# -- two providers' asks through one contract (acceptance 1, 5) ----------------------
# How an SDK adapter would map its provider's ask to the contract and the
# decision back: Claude's AskUserQuestion input and its ``answers`` map, its
# PreToolUse decision; OpenCode's QuestionInfo list and its ``answers:
# string[][]``, its permission reply (ADR 0007 §3). Synthetic shapes from
# the ADR's source reading, never a provider record. Each fake declares its
# provider's gaps from §3's rows: Claude's hook carries no session approval,
# and no custom answer until G-C3 shows one; no OpenCode reply carries a
# modified input.
CLAUDE_LIKE = Capabilities(
    live_questions=True,
    live_permissions=True,
    unsupported=frozenset({DecisionKind.ALLOW_SESSION, DecisionKind.CUSTOM_ANSWER}),
)
OPENCODE_LIKE = Capabilities(
    live_questions=True,
    live_permissions=True,
    unsupported=frozenset({DecisionKind.MODIFIED_INPUT}),
)
CLAUDE_INPUT = {
    "questions": [
        {
            "question": "Which database?",
            "header": "Database",
            "options": [
                {"label": "Postgres", "description": "the server"},
                {"label": "SQLite", "description": "a file"},
            ],
            "multiSelect": False,
        },
        {
            "question": "Which extras?",
            "header": "Extras",
            "options": [{"label": "Cache"}, {"label": "Search"}],
            "multiSelect": True,
        },
    ]
}
OPENCODE_QUESTIONS = [
    {
        "question": "Which database?",
        "header": "Database",
        "options": [
            {"label": "Postgres", "description": "the server"},
            {"label": "SQLite", "description": "a file"},
        ],
    },
    {
        "question": "Which extras?",
        "header": "Extras",
        "options": [{"label": "Cache"}, {"label": "Search"}],
        "multiple": True,
    },
]


def _claude_request(b: InteractionBroker, tool_use_id: str, tool_input: dict) -> InteractionRequest:
    return b.question(
        tool_use_id,
        [
            Question(
                text=q["question"],
                header=q["header"],
                options=tuple(
                    QuestionOption(o["label"], o.get("description", "")) for o in q["options"]
                ),
                multiple=q["multiSelect"],
            )
            for q in tool_input["questions"]
        ],
    )


def _opencode_request(b: InteractionBroker, ask_id: str, infos: list[dict]) -> InteractionRequest:
    return b.question(
        ask_id,
        [
            Question(
                text=q["question"],
                header=q["header"],
                options=tuple(
                    QuestionOption(o["label"], o.get("description", "")) for o in q["options"]
                ),
                multiple=q.get("multiple", False),
                custom=q.get("custom", False),
            )
            for q in infos
        ],
    )


def _claude_answers(tool_input: dict, answer: Answer) -> dict:
    return {
        q["question"]: ", ".join(response.choices)
        for q, response in zip(tool_input["questions"], answer.responses, strict=True)
    }


def _opencode_answers(answer: Answer) -> list[list[str]]:
    return [
        [*response.choices, *([response.custom_text] if response.custom_text else [])]
        for response in answer.responses
    ]


def _claude_reply(decision: InteractionDecision) -> dict:
    if isinstance(decision, Allow):
        reply: dict = {"permissionDecision": "allow"}
        if decision.updated_input is not None:
            reply["updatedInput"] = decision.input_to_send()
        return reply
    reply = {"permissionDecision": "deny"}
    if isinstance(decision, Deny) and decision.guidance is not None:
        reply["permissionDecisionReason"] = decision.guidance
    return reply


def _opencode_reply(decision: InteractionDecision) -> dict:
    if isinstance(decision, Allow):
        return {"reply": "once" if decision.scope is AllowScope.ONCE else "always"}
    reply = {"reply": "reject"}
    if isinstance(decision, Deny) and decision.guidance is not None:
        reply["message"] = decision.guidance
    return reply


def _live(capabilities: Capabilities, typed: bytes, ask) -> tuple[object, str]:
    """``ask(broker)``'s request, handled by a broker asking the terminal
    resolver, which reads ``typed``: the directive and what was shown."""
    with _pipe(typed) as (fd, out):
        resolver = TerminalResolver(input_fd=fd, output=out, is_terminal=_tty)
        b = InteractionBroker(PROMPT, capabilities, resolver)
        return b.handle(ask(b), FAR_REAL()), out.getvalue()


def test_equivalent_questions_of_two_providers_are_one_request_with_one_rendering():
    claude, _ = broker(PROMPT, CLAUDE_LIKE)
    opencode, _ = broker(PROMPT, OPENCODE_LIKE)
    c = _claude_request(claude, "toolu_01", CLAUDE_INPUT)
    o = _opencode_request(opencode, "que_01", OPENCODE_QUESTIONS)
    assert dataclasses.replace(c, provider_request_id="x") == dataclasses.replace(
        o, provider_request_id="x"
    )
    shown = []
    decisions = []
    for request in (c, o):
        with _pipe(b"1\n1,2\n") as (fd, out):
            decisions.append(
                TerminalResolver(input_fd=fd, output=out, is_terminal=_tty).resolve(
                    request, FAR_REAL()
                )
            )
            shown.append(out.getvalue())
    assert shown[0] == shown[1] and decisions[0] == decisions[1]
    answer = decisions[0]
    assert _claude_answers(CLAUDE_INPUT, answer) == {
        "Which database?": "Postgres",
        "Which extras?": "Cache, Search",
    }
    assert _opencode_answers(answer) == [["Postgres"], ["Cache", "Search"]]


def test_a_custom_answer_reaches_the_provider_that_can_carry_one_and_is_never_guessed():
    infos = [{**OPENCODE_QUESTIONS[0], "custom": True}]
    directive, shown = _live(
        OPENCODE_LIKE,
        b"0\n  MySQL, as the ops team runs it \n",
        lambda b: _opencode_request(b, "que_01", infos),
    )
    assert "0. something else, in your own words" in shown
    assert _opencode_answers(directive.decision) == [["MySQL, as the ops team runs it"]]
    # Claude's question may allow it, but the provider cannot carry it: no
    # "something else" is offered, and none is delivered.
    question_ = Question(
        "Which database?", (QuestionOption("Postgres"), QuestionOption("SQLite")), custom=True
    )
    directive, shown = _live(CLAUDE_LIKE, b"0\n2\n", lambda b: b.question("toolu_01", [question_]))
    assert "something else" not in shown and "choose from 1 to 2" in shown
    assert _claude_answers(
        {"questions": [{"question": "Which database?"}]}, directive.decision
    ) == {"Which database?": "SQLite"}
    claude, _ = broker(PROMPT, CLAUDE_LIKE, Answer((QuestionResponse(custom_text="MySQL"),)))
    refused = claude.handle(claude.question("toolu_02", [question_]), FAR)
    assert "cannot carry custom_answer" in refused.reason


@pytest.mark.parametrize(
    "typed, claude, opencode",
    [
        (b"y\n", {"permissionDecision": "allow"}, {"reply": "once"}),
        (b"s\ny\n", {"permissionDecision": "allow"}, {"reply": "always"}),
        (
            b"n run the unit tests only\n",
            {"permissionDecision": "deny", "permissionDecisionReason": "run the unit tests only"},
            {"reply": "reject", "message": "run the unit tests only"},
        ),
        (b"c\n", {"permissionDecision": "deny"}, {"reply": "reject"}),
    ],
)
def test_a_permission_decision_maps_back_to_each_providers_reply(typed, claude, opencode):
    """Once, session, deny with feedback and cancel round-trip through one
    rendering; a session approval Claude cannot carry is never offered to it."""
    ask = lambda b: b.permission("toolu_01", "Bash", "command: make test", DIGEST)  # noqa: E731
    directive, shown = _live(CLAUDE_LIKE, typed, ask)
    assert _claude_reply(directive.decision) == claude
    assert "  s  " not in shown
    directive, _ = _live(OPENCODE_LIKE, typed, ask)
    assert _opencode_reply(directive.decision) == opencode


def test_a_modified_input_reaches_only_the_provider_that_carries_one():
    given = {"command": "make lint"}
    changed = Allow(updated_input=given)
    claude, _ = broker(PROMPT, CLAUDE_LIKE, changed)
    delivered = claude.handle(permission(claude), FAR)
    # The resolver's own object changing after delivery changes nothing sent.
    given["command"] = "make deploy"
    assert _claude_reply(delivered.decision) == {
        "permissionDecision": "allow",
        "updatedInput": {"command": "make lint"},
    }
    opencode, _ = broker(PROMPT, OPENCODE_LIKE, changed)
    refused = opencode.handle(permission(opencode), FAR)
    assert "cannot carry modified_input" in refused.reason


def test_a_payload_one_provider_could_send_that_the_contract_cannot_render_is_refused():
    b, _ = broker()
    duplicate = {"questions": [CLAUDE_INPUT["questions"][0]] * 2}
    with pytest.raises(InteractionDataError, match="same question twice"):
        _claude_request(b, "toolu_02", duplicate)
    lookalike = [{**OPENCODE_QUESTIONS[0], "options": [{"label": "Cache"}, {"label": "cache"}]}]
    with pytest.raises(InteractionDataError, match="shown alike"):
        _opencode_request(b, "que_02", lookalike)


# -- the terminal resolver (cli.py) --------------------------------------------------
def FAR_REAL() -> float:  # noqa: N802 - a deadline far ahead on the real clock
    return time.monotonic() + 60


def _tty(fd: int) -> bool:
    return True


@contextmanager
def _pipe(data: bytes, *, close: bool = True):
    """A pipe holding ``data`` (then its end, unless ``close`` is false) and a
    text output; yields the read end and the output."""
    r, w = os.pipe()
    try:
        os.write(w, data)
        if close:
            os.close(w)
            w = -1
        yield r, io.StringIO()
    finally:
        os.close(r)
        if w != -1:
            os.close(w)


def _resolve(request, data: bytes, *, close: bool = True, deadline: float | None = None):
    with _pipe(data, close=close) as (fd, out):
        resolver = TerminalResolver(input_fd=fd, output=out, is_terminal=_tty)
        return resolver.resolve(request, deadline or FAR_REAL()), out.getvalue()


def test_the_terminal_resolver_asks_nothing_without_a_terminal():
    b, _ = broker()
    with _pipe(b"y\n") as (fd, out):
        resolver = TerminalResolver(input_fd=fd, output=out)
        assert resolver.resolve(permission(b), FAR_REAL()) == Unavailable(
            UnavailableReason.NO_TERMINAL
        )
        assert out.getvalue() == "" and os.read(fd, 10) == b"y\n"


@pytest.mark.parametrize(
    "typed, decision",
    [
        (b"y\n", Allow(AllowScope.ONCE)),
        (b"S\n", Allow(AllowScope.SESSION)),
        (b"n\n", Deny()),
        (b"n  run only the unit tests \n", Deny("run only the unit tests")),
        (b"c\n", Cancel()),
        (b"maybe\n\ny\n", Allow()),
    ],
)
def test_the_terminal_resolver_takes_a_permission_decision(typed, decision):
    b, _ = broker()
    result, shown = _resolve(permission(b), typed)
    assert result == decision
    assert "asks permission to use Bash (request 1)" in shown
    assert "command: make test" in shown and f"input sha256 {DIGEST}" in shown


def test_the_terminal_resolver_offers_only_what_the_provider_can_carry():
    caps = Capabilities(
        live_permissions=True,
        unsupported=frozenset({DecisionKind.ALLOW_SESSION, DecisionKind.DENY_GUIDANCE}),
    )
    b, _ = broker(PROMPT, caps)
    result, shown = _resolve(permission(b), b"s\nn because\nn\n")
    assert result == Deny()
    assert "  s  " not in shown and "choose y/n/c" in shown
    assert "expected one of y, n, c" in shown and "takes no guidance" in shown


def test_the_terminal_resolver_refuses_typed_text_it_could_not_send_back_exactly():
    b, _ = broker()
    typed = f"n use {TOKEN}\n".encode() + b"\xff\xfe\n" + b"x" * 9000 + b"\nc\n"
    result, shown = _resolve(permission(b), typed)
    assert result == Cancel()
    assert "credential-shaped" in shown and TOKEN not in shown
    assert "not UTF-8" in shown and "too long" in shown


def test_the_terminal_resolver_answers_questions_one_by_one():
    b, _ = broker()
    request = b.question(
        "q1",
        [
            question("Postgres", "SQLite", custom=True),
            question("Cache", "Search", text="Which extras?", header="Extras", multiple=True),
        ],
    )
    result, shown = _resolve(request, b"3\n1,1\nx\n0\n  my own db  \n2,1\n")
    assert result == Answer(
        (QuestionResponse(custom_text="my own db"), QuestionResponse(("Search", "Cache")))
    )
    assert "(1/2) Database" in shown and "1. Postgres - use Postgres" in shown
    assert "0. something else, in your own words" in shown
    assert shown.count("0. something else") == 1  # the second question allows none
    assert (
        "choose from 0 to 2" in shown
        and "chosen twice" in shown
        and "expected option numbers" in shown
    )


def test_the_terminal_resolver_cancels_a_question_and_refuses_two_picks_for_one():
    b, _ = broker()
    request = b.question("q1", [question()])
    result, shown = _resolve(request, b"1,2\nc\n")
    assert result == Cancel() and "takes one answer" in shown
    assert "or c to cancel" in shown


def test_the_terminal_resolver_reports_the_end_of_input_and_an_expired_wait():
    b, _ = broker()
    assert _resolve(permission(b), b"")[0] == Unavailable(UnavailableReason.END_OF_INPUT)
    b, _ = broker()
    started = time.monotonic()
    result, _ = _resolve(permission(b), b"", close=False, deadline=started + 0.2)
    assert result == Unavailable(UnavailableReason.EXPIRED)
    assert time.monotonic() - started < 5


def test_the_terminal_resolver_drops_what_was_typed_before_it_asked():
    """A key pressed before the request was shown never answers it: on a
    real terminal the type-ahead is flushed, and only what is typed after
    the prompt counts."""
    master, slave = pty.openpty()
    try:
        os.write(master, b"y\n")
        time.sleep(0.05)

        class Output(io.StringIO):
            def write(self, text):
                if text.startswith("  choose"):
                    os.write(master, b"c\n")
                return super().write(text)

        b, _ = broker()
        result = TerminalResolver(input_fd=slave, output=Output()).resolve(
            permission(b), FAR_REAL()
        )
        assert result == Cancel()
    finally:
        os.close(master)
        os.close(slave)


@pytest.mark.parametrize(
    "typed, rule",
    [
        ("²", "expected option numbers"),
        ("١", "expected option numbers"),
        ("1²", "expected option numbers"),
        ("9" * 5000, "expected option numbers"),
        ("+1", "expected option numbers"),
        ("-1", "expected option numbers"),
        ("1 2", "expected option numbers"),
        ("100", "choose from 1 to 2"),
    ],
    ids=["superscript", "arabic-indic", "mixed", "past-int-limit", "plus", "minus", "two", "big"],
)
def test_a_number_that_picks_no_option_is_asked_again_and_the_next_pick_taken(typed, rule):
    """R2-F2: ``str.isdigit()`` takes a superscript two, which ``int()``
    refuses, an Arabic-Indic digit, which ``int()`` takes, and a digit string
    ``int()`` refuses past its limit. An option number is a few ASCII digits;
    anything else is unusable input, asked again within the deadline, never
    an error that ends the invocation."""
    b, _ = broker()
    result, shown = _resolve(b.question("q1", [question()]), typed.encode() + b"\n2\n")
    assert result == Answer((QuestionResponse(("B",)),))
    assert rule in shown


# -- the terminal resolver at a real pseudo-terminal (its own line editing) ---------
class _Typist(io.StringIO):
    """The resolver's output at a real pseudo-terminal. Its n-th prompt has
    the n-th of ``typed`` typed at the master end, from a thread of its own,
    so the input reaches the terminal once the resolver has taken over its
    line editing, and while it reads; ``at_prompt(n)`` runs first.
    ``modes`` keeps the terminal's local modes at each prompt."""

    def __init__(self, master: int, slave: int, typed: tuple[bytes, ...], at_prompt) -> None:
        super().__init__()
        self._master, self._slave, self._typed = master, slave, list(typed)
        self._at_prompt = at_prompt
        self.modes: list[int] = []

    def write(self, text: str) -> int:
        if text.endswith(": "):
            self.modes.append(termios.tcgetattr(self._slave)[3])
            if self._at_prompt is not None:
                self._at_prompt(len(self.modes))
            if self._typed:
                data = self._typed.pop(0)
                threading.Thread(target=self._type, args=(data,), daemon=True).start()
        return super().write(text)

    def _type(self, data: bytes) -> None:
        try:
            while data:
                data = data[os.write(self._master, data) :]
        except OSError:  # the terminal closed before all of it was typed
            pass


@dataclasses.dataclass
class _Session:
    result: object
    printed: str  # what the resolver wrote to its output
    shown: bytes  # what the terminal showed
    modes: list[int]  # the terminal's local modes at each prompt
    restored: bool  # the terminal's settings afterwards are those before


def _at_terminal(resolve, *typed: bytes, read_only: bool = False, at_prompt=None) -> _Session:
    """``resolve(resolver)`` with a terminal resolver reading a real
    pseudo-terminal (through a read-only descriptor of its device, with
    ``read_only``) at which each of ``typed`` is typed at a prompt;
    ``at_prompt(slave, n)`` runs at the n-th prompt."""
    master, slave = pty.openpty()
    shown = bytearray()

    def drain() -> None:
        while True:
            try:
                data = os.read(master, 65536)
            except OSError:  # every descriptor of the slave end closed
                return
            if not data:
                return
            shown.extend(data)

    reader = threading.Thread(target=drain, daemon=True)
    reader.start()
    fd = os.open(os.ttyname(slave), os.O_RDONLY | os.O_NOCTTY) if read_only else slave
    before = termios.tcgetattr(slave)
    output = _Typist(master, slave, typed, at_prompt and (lambda n: at_prompt(slave, n)))
    try:
        result = resolve(TerminalResolver(input_fd=fd, output=output))
        restored = termios.tcgetattr(slave) == before
    finally:
        if fd != slave:
            os.close(fd)
        os.close(slave)
        reader.join(10)
        os.close(master)
    return _Session(result, output.getvalue(), bytes(shown), output.modes, restored)


def _deliver(ask):
    """A ``resolve`` for :func:`_at_terminal`: ``ask(broker)``'s request,
    handled by a ``prompt`` broker asking the terminal resolver."""

    def resolve(resolver):
        b = InteractionBroker(PROMPT, LIVE, resolver)
        return b.handle(ask(b), FAR_REAL())

    return resolve


def test_long_multibyte_guidance_from_a_real_terminal_reaches_the_provider_whole():
    """R2-F1: in canonical mode Linux keeps 4095 bytes of a line and drops the
    rest without a word, so 1500 three-byte characters, within
    MAX_ANSWER_CHARS, reached the broker as 1365. The resolver turns
    canonical mode and echo off while it reads, keeps Ctrl-C's signal, and
    delivers the guidance whole; it echoes on the terminal, never on its
    output, and the terminal's settings come back."""
    long = "数" * 1500
    session = _at_terminal(_deliver(permission), b"n " + long.encode() + b"\n")
    assert session.result == Deliver(Deny(long))
    assert long.encode() in session.shown and long not in session.printed
    assert session.modes and all(
        not mode & (termios.ICANON | termios.ECHO) and mode & termios.ISIG for mode in session.modes
    )
    assert session.restored


@pytest.mark.parametrize(
    "text",
    ["数据" * (MAX_ANSWER_CHARS // 2), "\U0001f600" * MAX_ANSWER_CHARS],
    ids=["three-byte", "four-byte"],
)
def test_an_answer_of_the_full_length_in_any_script_is_kept_whole(text):
    """Every answer MAX_ANSWER_CHARS allows fits the resolver's line, up to
    four UTF-8 bytes a character: nearly twice what a canonical line holds."""
    custom = lambda b: b.question("q1", [question("Postgres", "SQLite", custom=True)])  # noqa: E731
    session = _at_terminal(_deliver(custom), b"0\n" + text.encode() + b"\n")
    assert session.result == Deliver(Answer((QuestionResponse(custom_text=text),)))
    assert session.restored


def test_typed_text_past_a_bound_is_refused_whole_and_asked_again():
    """Past MAX_ANSWER_CHARS the guidance is refused by rule; past the line's
    bytes nothing more is kept or echoed, the bell rings once and the line
    is refused when it ends; the kill key starts a line again. Nothing is
    ever delivered in part."""
    typed = (
        b"n " + ("数" * (MAX_ANSWER_CHARS + 1)).encode() + b"\n"
        b"n " + ("数" * 3000).encode() + b"\n"
        b"n " + b"x" * 9000 + b"\x15n run the unit tests only\n"
    )
    session = _at_terminal(_deliver(permission), typed)
    assert session.result == Deliver(Deny("run the unit tests only"))
    assert f"guidance is longer than {MAX_ANSWER_CHARS} characters" in session.printed
    assert session.printed.count("the line is too long") == 1
    assert session.shown.count(b"\a") == 2
    assert session.restored


@pytest.mark.parametrize(
    "typed, decision, echoed",
    [
        ("n 数据库\x7f\n", Deny("数据"), "库\b\b  \b\b".encode()),
        ("n ab\x08c\n", Deny("ac"), b"b\b \bc"),
        ("n run the linter\x17tests\n", Deny("run the tests"), b"linter" + b"\b \b" * 6),
        ("y\x15n stop\n", Deny("stop"), b"y\b \bn stop"),
        ("n x\x04y\n", Deny("xy"), b"n xy"),
        ("\x04", Unavailable(UnavailableReason.END_OF_INPUT), b""),
        ("n a\x1bb\nc\n", Cancel(), b"n a^[b"),
        ("n a\x11b\x13c\n", Deny("abc"), b"n abc"),
    ],
    ids=["erase-wide", "backspace", "word-erase", "kill", "eof-within", "eof", "control", "flow"],
)
def test_the_terminal_resolver_edits_the_line_with_the_terminals_keys(typed, decision, echoed):
    """The terminal's erase (and backspace), word-erase, kill and end-of-file
    keys work as they do in canonical mode; a wide character is erased in
    its two columns, and a control character is shown as ^X and refused.
    The stop and start keys are never part of a line, as under IXON, and
    the stop key suspends nothing: all of the line is echoed."""
    b, _ = broker()
    request = permission(b)
    session = _at_terminal(lambda r: r.resolve(request, FAR_REAL()), typed.encode())
    assert session.result == decision
    assert echoed in session.shown
    assert session.restored
    if isinstance(decision, Cancel):
        assert "control or escape character" in session.printed


def test_a_line_read_while_the_terminals_own_editing_came_back_on_is_refused():
    """A stop and continue under a shell that resets the terminal, or another
    program's ``stty``, can turn canonical mode back on while the resolver
    waits: a line read through it may have been cut, so it is refused, the
    resolver's own editing taken back, and the next line is taken whole."""
    canonical: list[list] = []

    def at_prompt(slave: int, n: int) -> None:
        if n == 1:
            mode = termios.tcgetattr(slave)
            mode[3] |= termios.ICANON | termios.ECHO
            termios.tcsetattr(slave, termios.TCSANOW, mode)
            canonical.append(mode)

    long = "数" * 1500
    session = _at_terminal(
        _deliver(permission),
        b"n " + long.encode() + b"\n",
        b"n " + long.encode() + b"\n",
        at_prompt=at_prompt,
    )
    assert canonical and session.result == Deliver(Deny(long))
    assert session.printed.count("line editing came back on") == 1
    assert len(session.modes) == 2 and not session.modes[1] & termios.ICANON
    assert session.restored


def test_output_flow_control_that_came_back_on_is_taken_back_and_suspends_nothing():
    """Another program's ``stty ixon`` while the resolver waits lets the stop
    key suspend the terminal's output again (R3-F1): the resolver turns flow
    control off at the next key it reads, which restarts the output, so what
    it echoes is shown and the line, not cut, is taken."""

    def at_prompt(slave: int, n: int) -> None:
        if n == 1:
            mode = termios.tcgetattr(slave)
            mode[0] |= termios.IXON
            termios.tcsetattr(slave, termios.TCSANOW, mode)

    session = _at_terminal(_deliver(permission), b"\x13n tests only\n", at_prompt=at_prompt)
    assert session.result == Deliver(Deny("tests only"))
    assert b"n tests only" in session.shown and "came back on" not in session.printed
    assert session.restored


def test_the_resolver_reads_no_more_keys_while_the_terminal_does_not_take_its_output():
    """What a terminal whose output is suspended has not taken stays bounded
    (R3-F1): past _MAX_UNSHOWN_BYTES of echo the resolver reads no more keys,
    so the answer typed after them is not taken and the wait ends at its
    deadline. Each line below echoes 8000 bytes and its kill erases them in
    24000 more."""

    def at_prompt(slave: int, n: int) -> None:
        termios.tcflow(slave, termios.TCOOFF)

    b, _ = broker()
    request = permission(b)
    started = time.monotonic()
    session = _at_terminal(
        lambda r: r.resolve(request, started + 1.0),
        (b"x" * 8000 + b"\x15") * 4 + b"y\n",
        at_prompt=at_prompt,
    )
    assert session.result == Unavailable(UnavailableReason.EXPIRED)
    assert time.monotonic() - started < 10 and session.restored


def test_text_shown_on_another_suspended_terminal_never_holds_the_resolver():
    """An output that is a terminal other than the one the resolver reads is
    written to as it takes it as well (R3-F1): with its output suspended for
    the whole wait, the resolver still asks again on an unusable line and
    ends at its deadline."""
    master, slave = pty.openpty()
    out_master, out_slave = pty.openpty()
    termios.tcflow(out_slave, termios.TCOOFF)
    output = open(out_slave, "w", encoding="utf-8", closefd=False)
    outcome: list = []

    def type_once_taken_over() -> None:
        give_up = time.monotonic() + 30
        while termios.tcgetattr(slave)[3] & termios.ICANON and time.monotonic() < give_up:
            time.sleep(0.01)
        os.write(master, b"maybe\ny")

    b, _ = broker()
    request = permission(b)
    typist = threading.Thread(target=type_once_taken_over, daemon=True)
    resolver = TerminalResolver(input_fd=slave, output=output)
    asker = threading.Thread(
        target=lambda: outcome.append(resolver.resolve(request, time.monotonic() + 1.0)),
        daemon=True,
    )
    try:
        started = time.monotonic()
        typist.start()
        asker.start()
        asker.join(15)
        assert not asker.is_alive() and time.monotonic() - started < 10
        assert outcome == [Unavailable(UnavailableReason.EXPIRED)]
        typist.join(10)
    finally:
        termios.tcflow(out_slave, termios.TCOON)
        output.close()
        for fd in (master, slave, out_master, out_slave):
            os.close(fd)


def test_the_resolver_echoes_on_the_terminal_through_a_read_only_descriptor():
    """A terminal read through a read-only descriptor is still echoed on,
    through its device opened to write."""
    session = _at_terminal(_deliver(permission), b"n tests only\n", read_only=True)
    assert session.result == Deliver(Deny("tests only"))
    assert b"n tests only" in session.shown and "tests only" not in session.printed
    assert session.restored


def test_the_terminal_settings_come_back_when_the_resolver_is_interrupted_or_expires():
    master, slave = pty.openpty()
    try:
        before = termios.tcgetattr(slave)

        class Interrupting(io.StringIO):
            def write(self, text):
                if text.endswith(": "):
                    raise KeyboardInterrupt
                return super().write(text)

        b, _ = broker()
        with pytest.raises(KeyboardInterrupt):
            TerminalResolver(input_fd=slave, output=Interrupting()).resolve(
                permission(b), FAR_REAL()
            )
        assert termios.tcgetattr(slave) == before
        result = TerminalResolver(input_fd=slave, output=io.StringIO()).resolve(
            permission(b), time.monotonic() + 0.2
        )
        assert result == Unavailable(UnavailableReason.EXPIRED)
        assert termios.tcgetattr(slave) == before
    finally:
        os.close(master)
        os.close(slave)


_CTRL_C_CHILD = """
import fcntl, signal, sys, termios, time
sys.path.insert(0, sys.argv[1])
from autoforge.cli import TerminalResolver
from autoforge.interaction import Capabilities, InteractionBroker
from autoforge.sdk_policy import InteractionSettings, input_digest

signal.signal(signal.SIGINT, signal.default_int_handler)
fcntl.ioctl(0, termios.TIOCSCTTY, 0)
before = termios.tcgetattr(0)
broker = InteractionBroker(
    InteractionSettings(policy="prompt"), Capabilities(live_permissions=True), TerminalResolver()
)
request = broker.permission("toolu_01", "Bash", "command: make test", input_digest({}, "input"))
try:
    broker.handle(request, time.monotonic() + 60)
except KeyboardInterrupt:
    print("interrupted", termios.tcgetattr(0) == before, flush=True)
"""


def test_ctrl_c_at_a_real_controlling_terminal_still_interrupts_and_settings_come_back():
    """D7.7 with the resolver's own line editing: ISIG stays on, so Ctrl-C at
    the controlling terminal still raises KeyboardInterrupt (the broker
    decides nothing), and the terminal's settings are restored."""
    master, slave = pty.openpty()
    src = Path(interaction_module.__file__).resolve().parents[1]
    child = subprocess.Popen(
        [sys.executable, "-c", _CTRL_C_CHILD, str(src)],
        stdin=slave,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        assert child.stderr is not None
        printed = b""
        while b"choose" not in printed:
            ready, _, _ = select.select([child.stderr], [], [], 30)
            data = os.read(child.stderr.fileno(), 4096) if ready else b""
            assert data, printed
            printed += data
        os.write(master, b"n half an answ\x03")
        out, err = child.communicate(timeout=30)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
        os.close(master)
        os.close(slave)
    assert out == b"interrupted True\n", err


_SUSPENDED_CHILD = """
import json, sys, termios, time
sys.path.insert(0, sys.argv[1])
from autoforge.cli import TerminalResolver
from autoforge.interaction import Capabilities, InteractionBroker, Unattended
from autoforge.sdk_policy import InteractionSettings, input_digest

broker = InteractionBroker(
    InteractionSettings(policy="prompt"), Capabilities(live_permissions=True), Unattended()
)
request = broker.permission("toolu_01", "Bash", "command: make test", input_digest({}, "input"))
before = termios.tcgetattr(0)
started = time.monotonic()
result = TerminalResolver().resolve(request, started + float(sys.argv[2]))
print(json.dumps({"result": repr(result), "seconds": time.monotonic() - started,
                  "restored": termios.tcgetattr(0) == before}), flush=True)
"""


@pytest.mark.parametrize(
    "suspend, typed, wait, expected",
    [
        ("stop key", b"\x13y\r", 20.0, Allow(AllowScope.ONCE)),
        ("stop key", b"\x13y", 1.0, Unavailable(UnavailableReason.EXPIRED)),
        ("stop key before", b"y\r", 20.0, Allow(AllowScope.ONCE)),
        ("tcflow", b"y\r", 20.0, Allow(AllowScope.ONCE)),
        ("tcflow", b"maybe\ry", 1.0, Unavailable(UnavailableReason.EXPIRED)),
    ],
    ids=["stop-key-answer", "stop-key-expiry", "stopped-before", "tcflow-answer", "tcflow-expiry"],
)
def test_suspended_output_never_holds_the_resolver_past_its_deadline(
    suspend, typed, wait, expected
):
    """R3-F1: a terminal whose output is suspended does not take a write, and
    a blocking write waits until it resumes: the stop key (Ctrl-S under
    IXON, a pseudo-terminal's default) typed after the prompt held the
    resolver's echo, and so the resolver, past its deadline until Ctrl-Q.

    At a real pseudo-terminal that is the resolver's stdin and its stderr,
    as in the CLI: while the resolver reads, the stop and start keys are
    taken as under IXON but suspend nothing, and output a stop key
    suspended before it asked comes back. Output another program suspends
    (``tcflow``) holds neither the echo nor the resolver's messages: an
    answer is delivered at once, and without one the resolver ends at its
    deadline. The terminal's settings come back either way."""
    master, slave = pty.openpty()
    src = Path(interaction_module.__file__).resolve().parents[1]
    if suspend == "stop key before":
        os.write(master, b"\x13")
    child = subprocess.Popen(
        [sys.executable, "-c", _SUSPENDED_CHILD, str(src), str(wait)],
        stdin=slave,
        stdout=subprocess.PIPE,
        stderr=slave,
        start_new_session=True,
    )
    shown = b""
    try:
        while not shown.endswith(b"choose y/s/n/c: "):
            ready, _, _ = select.select([master], [], [], 30)
            data = os.read(master, 4096) if ready else b""
            assert data, shown
            shown += data
        if suspend == "tcflow":
            termios.tcflow(slave, termios.TCOOFF)
        os.write(master, typed)
        out, _ = child.communicate(timeout=wait + 15)
        termios.tcflow(slave, termios.TCOON)
        while select.select([master], [], [], 0.2)[0]:
            shown += os.read(master, 4096)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
        os.close(master)
        os.close(slave)
    report = json.loads(out)
    assert report["result"] == repr(expected)
    assert report["seconds"] < (wait if isinstance(expected, Allow) else wait + 2)
    assert report["restored"]
    if suspend != "tcflow":
        assert b"choose y/s/n/c: y" in shown and b"^S" not in shown


def test_a_superscript_digit_then_a_valid_pick_at_a_real_terminal_is_delivered():
    """R2-F2's reproduction: ``²`` and then a valid pick, typed at a real
    terminal, deliver the pick instead of failing the invocation."""
    one = lambda b: b.question("q1", [question()])  # noqa: E731
    session = _at_terminal(_deliver(one), "²\n1\n".encode())
    assert session.result == Deliver(Answer((QuestionResponse(("A",)),)))
    assert "expected option numbers" in session.printed
    assert session.restored


def test_attach_terminal_gives_a_real_run_the_terminal_resolver(tmp_state_dir):
    eng = make_engine(tmp_state_dir)
    assert eng.interaction_resolver is None
    _attach_terminal(eng)
    assert isinstance(eng.interaction_resolver, TerminalResolver)
    assert eng.progress_output is not None


# -- the engine (D5.2, D7.2, D7.6, D10.5) ----------------------------------------------
class _Asking(ScriptedProvider):
    """A scripted agent that asks before it works, as an SDK adapter would
    (ADR 0007 D5.2): it mints each ask through the launch's broker, follows
    the directive, and reports ``provider_failure`` and the outcome on a
    failure. ``claimed`` replaces the outcome it reports."""

    def __init__(self, script, settings, capabilities=LIVE, *, claimed=None, transient=None):
        super().__init__(script)
        self.settings = settings
        self.capabilities = capabilities
        self.claimed = claimed
        self.transient = transient
        self.brokers: list[InteractionBroker | None] = []
        self.directives: list[object] = []

    def interaction_settings(self, profile):
        return self.settings

    def interaction_capabilities(self, profile):
        return self.capabilities

    def execute(self, req):
        broker = req.interaction
        self.brokers.append(broker)
        if broker is not None:
            request = broker.permission("toolu_01", "Bash", "command: make test", DIGEST)
            directive = broker.handle(request, time.monotonic() + 600)
            self.directives.append(directive)
            if not isinstance(directive, Deliver):
                self.calls.append(req)
                return AgentExecutionResult(
                    command=["x"],
                    exit_code=0,
                    stdout="",
                    stderr="denied",
                    started_at="t",
                    finished_at="t",
                    provider_failure=f"scripted: {broker.failure}" if broker.failure else None,
                    transient_failure=self.transient,
                    interaction=self.claimed or broker.outcome(),
                )
        result = super().execute(req)
        result.interaction = self.claimed or (broker.outcome() if broker else None)
        return result


def _install(eng, provider):
    eng.providers._overrides = dict.fromkeys(eng.providers._overrides, provider)
    eng.provider = provider


def _last_execution(eng) -> dict:
    run_dir = eng.paths.logs_dir / eng.state.run_id
    step = sorted(p for p in run_dir.iterdir() if p.is_dir())[-1]
    return json.loads((step / "execution.json").read_text(encoding="utf-8"))


def test_an_agent_that_cannot_ask_launches_exactly_as_before(tmp_state_dir, fake_github):
    eng = make_engine(tmp_state_dir, [], github=fake_github, origin=True)
    eng.provider._handler = implement
    assert eng.step().next_phase == "ANALYZE_EXECUTE"
    assert eng.step().next_phase == "REVIEW"
    assert eng.provider.calls[0].interaction is None
    assert "interaction" not in _last_execution(eng)


def test_fail_raises_interaction_required_with_the_phase_unchanged_and_no_relaunch(
    tmp_state_dir, fake_github
):
    """D7.6: never corrected, never relaunched as transient (a relaunch would
    ask again), whatever else the adapter reported."""
    eng = make_engine(tmp_state_dir, [], github=fake_github, origin=True)
    eng.config.execution.max_correction_attempts = 3
    provider = _Asking(implement, FAIL, transient="scripted: a transient failure")
    _install(eng, provider)
    assert eng.step().next_phase == "ANALYZE_EXECUTE"
    with pytest.raises(InteractionRequiredError) as excinfo:
        eng.step()
    text = str(excinfo.value)
    assert "the agent asked for a decision (permission: Bash); interaction_policy is fail" in text
    assert "Not relaunched: it would ask again" in text and "State unchanged." in text
    assert len(provider.calls) == 1
    state = load_state(eng.paths.state_file)
    assert state.phase == Phase.ANALYZE_EXECUTE and state.attempt == 1
    execution = _last_execution(eng)
    assert execution["interaction"]["lifecycle"] == "failed"
    assert execution["interaction"]["requests"][0]["directive"] == "fail"
    assert "interaction_policy is fail" in execution["error"]


def test_prompt_asks_the_engines_resolver_under_the_repository_lock_and_goes_on(
    tmp_state_dir, fake_github
):
    """D7.2: the wait for a person runs inside the launch, so under the
    repository lock the command holds; the decision is delivered and the
    agent's result is verified as ever."""
    eng = make_engine(tmp_state_dir, [], github=fake_github, origin=True)
    provider = _Asking(implement, PROMPT)
    _install(eng, provider)
    held = []

    def while_locked(request, deadline):
        try:
            ControllerLock(eng.lock_path()).acquire().release()
        except LockError:
            held.append(request.request_id)
        return Allow()

    eng.interaction_resolver = Scripted(while_locked)
    assert eng.step().next_phase == "ANALYZE_EXECUTE"
    assert eng.step().next_phase == "REVIEW"
    assert held == ["1"] and provider.directives == [Deliver(Allow())]
    interaction = _last_execution(eng)["interaction"]
    assert interaction["lifecycle"] == "completed" and interaction["handled"] == 1


def test_prompt_without_a_resolver_has_no_one_to_ask_and_fails(tmp_state_dir, fake_github):
    eng = make_engine(tmp_state_dir, [], github=fake_github, origin=True)
    _install(eng, _Asking(implement, PROMPT))
    eng.step()
    with pytest.raises(InteractionRequiredError, match="stdin is not a terminal, and no pause"):
        eng.step()
    assert load_state(eng.paths.state_file).phase == Phase.ANALYZE_EXECUTE


def test_each_launch_gets_a_broker_of_its_own(tmp_state_dir, fake_github):
    """A correction is a new logical invocation (D8.3): its count starts afresh."""
    eng = make_engine(tmp_state_dir, [], github=fake_github, origin=True)
    eng.config.execution.max_correction_attempts = 1
    provider = _Asking(["no block", ""], PROMPT)
    _install(eng, provider)
    eng.interaction_resolver = Scripted(Allow(), Allow())
    eng.step()
    with pytest.raises(Exception, match="CONTROL_RESULT"):
        eng.step()
    assert len(provider.brokers) == 2 and provider.brokers[0] is not provider.brokers[1]
    assert [b.handled for b in provider.brokers] == [1, 1]


def test_a_failure_the_adapter_did_not_report_is_still_the_brokers(tmp_state_dir, fake_github):
    """The broker's account wins over an adapter's claim of a completed run."""
    eng = make_engine(tmp_state_dir, [], github=fake_github, origin=True)
    provider = _Asking(implement, FAIL, claimed=InteractionOutcome(Lifecycle.COMPLETED))
    _install(eng, provider)
    eng.step()
    with pytest.raises(InteractionRequiredError, match="interaction_policy is fail"):
        eng.step()


def test_a_pause_with_no_pending_record_to_pause_into_fails_and_leaves_nothing_pending(
    tmp_state_dir, fake_github
):
    eng = make_engine(tmp_state_dir, [], github=fake_github, origin=True)
    pending = InteractionBroker(PROMPT, LIVE, Unattended()).permission("t", "Bash", "", DIGEST)
    provider = _Asking(implement, PROMPT, claimed=InteractionOutcome(Lifecycle.PAUSED, pending))
    _install(eng, provider)
    eng.interaction_resolver = Scripted(Allow())
    eng.step()
    before = eng.paths.state_file.read_text(encoding="utf-8")
    with pytest.raises(InteractionRequiredError, match="nothing is pending"):
        eng.step()
    assert len(provider.calls) == 1
    assert load_state(eng.paths.state_file).phase == Phase.ANALYZE_EXECUTE
    # The step's entry was persisted before the launch, as for any step; no
    # record of the ask, pending or answered, was (#126 owns one).
    after = json.loads(eng.paths.state_file.read_text(encoding="utf-8"))
    assert set(after) == set(json.loads(before))
    assert "toolu" not in json.dumps(after) and after["attempt"] == 1


def test_an_interrupted_turn_is_a_failed_launch_and_not_relaunched(tmp_state_dir, fake_github):
    eng = make_engine(tmp_state_dir, [], github=fake_github, origin=True)
    eng.config.execution.max_correction_attempts = 3
    provider = _Asking(implement, PROMPT, claimed=InteractionOutcome(Lifecycle.INTERRUPTED))
    _install(eng, provider)
    eng.interaction_resolver = Scripted(Allow())
    eng.step()
    with pytest.raises(ExecutionError, match="was interrupted before its turn ended") as excinfo:
        eng.step()
    assert not isinstance(excinfo.value, InteractionRequiredError)
    assert len(provider.calls) == 1


def test_a_local_interaction_failure_keeps_the_launch_charge(tmp_path):
    root = git_repo(tmp_path)
    (root / "src").mkdir(exist_ok=True)
    (root / "src" / "app.py").write_text("def main():\n    pass\n", encoding="utf-8")
    write_feature(root)
    commit_all(root, "initial")
    eng = make_local_engine(root, "features/add-filter.md")
    eng.step()  # INITIALIZING -> ANALYZE_EXECUTE
    provider = _Asking("", FAIL)
    _install(eng, provider)
    with pytest.raises(InteractionRequiredError, match="interaction_policy is fail"):
        eng.step()
    assert len(provider.calls) == 1
    persisted = load_state(eng.paths.state_file)
    assert persisted.phase == Phase.ANALYZE_EXECUTE and persisted.local_pending_attempts == 1


def test_a_dry_run_creates_no_broker_and_asks_no_one(tmp_state_dir, fake_github):
    eng = make_engine(tmp_state_dir, [], github=fake_github, origin=True)
    provider = _Asking(implement, PROMPT)
    _install(eng, provider)
    resolver = Scripted()
    eng.interaction_resolver = resolver
    eng.step()
    outcome = eng.step(dry_run=True)
    assert outcome.plan is not None
    assert provider.calls == [] and provider.brokers == [] and resolver.seen == []
