"""The OpenCode bridge's envelope as a pure reducer (#152; ADR 0007 §5).

Synthetic records only: no process, no Node, no runtime. The real bridge
is exercised in tests/test_opencode_sdk_provider.py.
"""

from __future__ import annotations

import json

import pytest

from autoforge.config import LoopDetectionConfig
from autoforge.errors import InteractionDataError
from autoforge.interaction import (
    Allow,
    AllowScope,
    Answer,
    Cancel,
    Deliver,
    Deny,
    Fail,
    Pause,
    QuestionResponse,
    RequestKind,
)
from autoforge.loop_detect import LoopMonitor
from autoforge.opencode_sdk import (
    ENVELOPE,
    PROTOCOL,
    BridgeConversation,
    BridgeLimits,
    BridgeMode,
    BridgeStart,
    decision_record,
    encode,
    opencode_id,
    split_model,
)
from autoforge.progress import ProgressKind

SESSION = "ses_e33486bf1ffegjRQB5CrCJ0pHh"
INPUT = "msg_1ccb8a7b30013zn29CLmfDa5N2"
PROMPT = "Review the change. SECRET-PROMPT-TEXT"


def _start(mode: BridgeMode = BridgeMode.NEW, variant: str | None = None) -> BridgeStart:
    return BridgeStart(
        server_command="/opt/opencode/bin/opencode",
        session_id=SESSION,
        mode=mode,
        input_id=INPUT,
        prompt=PROMPT,
        cwd="/work/tree",
        agent="autoforge",
        provider_id="openai",
        model_id="gpt-5.6-luna",
        variant=variant,
        runtime_version="1.18.34",
        policy={"schema": 1, "provider": "opencode-sdk", "agent": "autoforge"},
        limits=BridgeLimits(),
    )


def _rec(**fields: object) -> bytes:
    return json.dumps(fields).encode()


def _hello(**override: object) -> bytes:
    fields = {
        "type": "hello",
        "envelope": ENVELOPE,
        "protocol": PROTOCOL,
        "sdk_version": "1.18.34",
        "node_version": "24.11.1",
    }
    fields.update(override)
    return json.dumps(fields).encode()


def _session(start: BridgeStart, **override: object) -> bytes:
    fields = {
        "type": "session",
        "session_id": start.session_id,
        "mode": str(start.mode),
        "directory": start.cwd,
        "agent": start.agent,
        "provider_id": start.provider_id,
        "model_id": start.model_id,
        "variant": start.variant,
    }
    fields.update(override)
    return json.dumps(fields).encode()


def _conversation(start: BridgeStart | None = None, **kwargs) -> BridgeConversation:
    return BridgeConversation(
        start or _start(), sdk_version="1.18.34", node_minimum=(24,), **kwargs
    )


def _running(start: BridgeStart | None = None, **kwargs) -> BridgeConversation:
    start = start or _start()
    conversation = _conversation(start, **kwargs)
    for record in (
        _hello(),
        _rec(type="server", runtime_version="1.18.34"),
        _session(start),
        _rec(type="admitted", input_id=start.input_id, sent=True),
    ):
        replies, ask = conversation.feed(record, 0.0)
        assert ask is None
    assert conversation.failure is None and conversation.admitted
    return conversation


def _result(**override: object) -> bytes:
    fields = {
        "type": "result",
        "session_id": SESSION,
        "input_id": INPUT,
        "message_id": "msg_answer",
        "finish": "stop",
        "text": "the answer",
    }
    fields.update(override)
    return json.dumps(fields).encode()


END = _rec(
    type="end",
    outcome="result",
    server={"stopped": "sigterm", "exit_code": None, "signal": "SIGTERM"},
    events=4,
    skipped_unknown=1,
    foreign_events=0,
    asks=0,
    sibling_settled=0,
)


def _question(request_id: str = "1", **override: object) -> bytes:
    fields = {
        "type": "ask",
        "request_id": request_id,
        "kind": "question",
        "provider_request_id": f"que_{request_id}",
        "session_id": SESSION,
        "questions": [
            {
                "question": "Which colour?",
                "header": "Colour",
                "options": [
                    {"label": "red", "description": "warm"},
                    {"label": "blue", "description": "cool"},
                ],
                "multiple": None,
                "custom": None,
            }
        ],
        "tool": {"message_id": "msg_t", "call_id": "call_1"},
    }
    fields.update(override)
    return json.dumps(fields).encode()


def _permission(request_id: str = "1", **override: object) -> bytes:
    fields = {
        "type": "ask",
        "request_id": request_id,
        "kind": "permission",
        "provider_request_id": f"per_{request_id}",
        "session_id": SESSION,
        "permission": "bash",
        "patterns": ["git status"],
        "metadata_sha256": "a" * 64,
        "tool": None,
    }
    fields.update(override)
    return json.dumps(fields).encode()


# -- ids and the start record --------------------------------------------------------
def test_ids_are_in_the_runtimes_format_and_sort_as_its_own():
    early = opencode_id("msg", 1_794_435_000_000)
    late = opencode_id("msg", 1_794_435_000_001)
    assert early.startswith("msg_") and len(early) == 4 + 12 + 14
    assert early < late  # message ids ascend
    newer = opencode_id("ses", 1_794_435_000_001, descending=True)
    older = opencode_id("ses", 1_794_435_000_000, descending=True)
    assert newer < older  # session ids descend
    assert opencode_id("msg", 1) != opencode_id("msg", 1)  # the random tail


def test_split_model_keeps_everything_after_the_first_slash():
    assert split_model("openrouter/anthropic/claude") == ("openrouter", "anthropic/claude")


def test_the_start_record_carries_exactly_what_the_bridge_checks():
    record = _start(variant="high").record()
    assert set(record) == {
        "type",
        "server_command",
        "session_id",
        "mode",
        "input_id",
        "prompt",
        "cwd",
        "agent",
        "model",
        "expect",
        "policy",
        "limits",
    }
    assert record["model"] == {
        "provider_id": "openai",
        "model_id": "gpt-5.6-luna",
        "variant": "high",
    }
    assert record["expect"] == {"runtime_version": "1.18.34"}
    assert record["limits"] == BridgeLimits().as_dict()


def test_controller_records_are_ascii_on_one_line():
    data = encode({"type": "start", "prompt": "line\nbreak   and \udcff"})
    assert data.isascii() and b"\n" not in data


# -- hello, server, session, admitted ---------------------------------------------------
def test_start_is_sent_only_after_a_hello_of_the_pinned_pair():
    events: list = []
    conversation = _conversation(emit=events.append)
    replies, ask = conversation.feed(_hello(), 0.0)
    assert ask is None and conversation.failure is None
    (start,) = replies
    assert json.loads(start)["type"] == "start"
    assert [event.kind for event in events] == [ProgressKind.STARTED]


@pytest.mark.parametrize(
    ("override", "reason"),
    [
        ({"envelope": "autoforge-claude-helper"}, "not autoforge-opencode-bridge 1"),
        ({"protocol": 2}, "not autoforge-opencode-bridge 1"),
        ({"sdk_version": "1.19.0"}, "SDK pair mismatch"),
        ({"node_version": "22.12.0"}, "below the supported 24"),
        ({"node_version": "v24"}, "below the supported 24"),
    ],
)
def test_a_hello_of_another_pair_sends_nothing(override, reason):
    conversation = _conversation()
    replies, _ = conversation.feed(_hello(**override), 0.0)
    assert replies == [] and reason in conversation.failure


def test_a_runtime_other_than_the_pinned_one_fails():
    conversation = _conversation()
    conversation.feed(_hello(), 0.0)
    conversation.feed(_rec(type="server", runtime_version="1.19.0"), 0.0)
    assert "runtime pair mismatch" in conversation.failure and "1.19.0" in conversation.failure


@pytest.mark.parametrize(
    "field", ["session_id", "mode", "directory", "agent", "provider_id", "model_id", "variant"]
)
def test_a_session_read_back_unlike_the_start_fails(field):
    start = _start()
    conversation = _conversation(start)
    conversation.feed(_hello(), 0.0)
    conversation.feed(_rec(type="server", runtime_version="1.18.34"), 0.0)
    conversation.feed(_session(start, **{field: "something else"}), 0.0)
    assert f"not the one asked for ({field})" in conversation.failure


def test_an_admission_of_another_input_fails():
    start = _start()
    conversation = _conversation(start)
    for record in (_hello(), _rec(type="server", runtime_version="1.18.34"), _session(start)):
        conversation.feed(record, 0.0)
    conversation.feed(_rec(type="admitted", input_id="msg_other", sent=True), 0.0)
    assert "the admitted input is not the one sent" in conversation.failure


@pytest.mark.parametrize(
    "record",
    [
        _rec(type="server", runtime_version="1.18.34"),
        _rec(type="admitted", input_id=INPUT, sent=True),
        _rec(type="event", kind="text"),
        _result(),
    ],
)
def test_a_record_out_of_place_is_a_protocol_violation(record):
    conversation = _conversation()
    conversation.feed(record, 0.0)
    assert "arrived out of place (hello)" in conversation.failure


@pytest.mark.parametrize(
    ("data", "reason"),
    [
        (b"not json", "a record is not JSON"),
        (b"[1]", "a record has no type"),
        (_rec(type="telemetry"), "unknown record type"),
    ],
)
def test_what_is_not_a_record_fails_closed(data, reason):
    conversation = _conversation()
    conversation.feed(data, 0.0)
    assert reason in conversation.failure


# -- events -----------------------------------------------------------------------------
def test_events_become_progress_and_loop_input():
    events: list = []
    monitor = LoopMonitor(LoopDetectionConfig().for_invocation("opencode-sdk", "review"), 0.0)
    conversation = _running(emit=events.append, loop=monitor)
    events.clear()
    for record in (
        _rec(type="event", kind="status", status="busy"),
        _rec(type="event", kind="reasoning"),
        _rec(type="event", kind="text"),
        _rec(
            type="event",
            kind="tool",
            status="running",
            tool="read",
            call_id="c1",
            detail="/work/tree/a.py",
        ),
        _rec(
            type="event",
            kind="tool",
            status="completed",
            tool="read",
            call_id="c1",
            input_sha256="1" * 64,
            result_sha256="2" * 64,
        ),
        _rec(
            type="event",
            kind="tool",
            status="error",
            tool="bash",
            call_id="c2",
            input_sha256="3" * 64,
            result_sha256="4" * 64,
        ),
        _rec(type="event", kind="status", status="retry", attempt=2),
        _rec(type="event", kind="message", finish="stop"),
        _rec(type="event", kind="asks_settled", count=2),
        _rec(type="event", kind="hologram"),
    ):
        conversation.feed(record, 1.0)
    kinds = [event.kind for event in events]
    assert kinds == [
        ProgressKind.ACTIVITY,
        ProgressKind.THINKING,
        ProgressKind.ASSISTANT_TEXT,
        ProgressKind.TOOL_STARTED,
        ProgressKind.TOOL_FINISHED,
        ProgressKind.TOOL_FAILED,
        ProgressKind.PROVIDER_RETRY,
        ProgressKind.ACTIVITY,
        ProgressKind.ACTIVITY,
        ProgressKind.ACTIVITY,
    ]
    assert events[3].detail == "a.py"  # relative to the worktree
    assert events[6].detail == "attempt 2"
    summary = conversation.summary()
    assert summary["unknown_events"] == 1 and summary["asks_settled_by_reject"] == 2
    assert summary["tools"] == 1
    assert monitor.calibration()["loop_actions"] == 2
    assert conversation.failure is None


def test_an_unknown_session_status_fails_closed():
    conversation = _running()
    conversation.feed(_rec(type="event", kind="status", status="hibernating"), 0.0)
    assert "an unknown session status" in conversation.failure


# -- asks and decisions ------------------------------------------------------------------
def test_a_question_is_normalized_for_the_broker():
    conversation = _running()
    replies, ask = conversation.feed(_question(), 0.0)
    assert replies == [] and ask is not None
    assert ask.kind is RequestKind.QUESTION and ask.provider_request_id == "que_1"
    (question,) = ask.questions
    assert question.text == "Which colour?" and question.header == "Colour"
    assert [option.label for option in question.options] == ["red", "blue"]
    # The runtime's defaults: one pick, and the operator's own words allowed.
    assert not question.multiple and question.custom
    conversation = _running()
    _, strict = conversation.feed(
        _question(
            questions=[
                {
                    "question": "Pick?",
                    "header": "",
                    "options": [{"label": "a", "description": ""}],
                    "multiple": True,
                    "custom": False,
                }
            ]
        ),
        0.0,
    )
    assert strict.questions[0].multiple and not strict.questions[0].custom


def test_a_permission_is_bound_to_its_identity_digest():
    conversation = _running()
    _, ask = conversation.feed(_permission(), 0.0)
    assert ask.kind is RequestKind.PERMISSION
    assert (ask.permission, ask.patterns) == ("bash", ("git status",))
    assert ask.summary == "patterns: git status"
    other = _running()
    _, moved = other.feed(_permission(patterns=["git push"]), 0.0)
    assert len(ask.input_sha256) == 64 and moved.input_sha256 != ask.input_sha256


@pytest.mark.parametrize(
    ("record", "reason"),
    [
        (_question(session_id="ses_child"), "a session other than the bound one"),
        (_question(kind="elicitation"), "an ask of an unknown kind"),
        (_question(provider_request_id=""), "without the provider's id"),
        (_question(questions="Which?"), "the broker cannot render"),
        (_question(questions=[{"question": "x", "header": "", "options": []}]), "cannot render"),
        (_permission(metadata_sha256="nope"), "without its metadata digest"),
        (_permission(patterns=[1]), "patterns are not strings"),
    ],
)
def test_an_ask_that_cannot_be_rendered_is_rejected_and_fails(record, reason):
    conversation = _running()
    replies, ask = conversation.feed(record, 0.0)
    assert ask is None and reason in conversation.failure
    assert [json.loads(reply) for reply in replies] == [
        {"type": "decision", "request_id": "1", "directive": "fail"}
    ]


def test_the_same_ask_twice_fails_closed():
    conversation = _running()
    _, first = conversation.feed(_question("1"), 0.0)
    assert first is not None
    replies, again = conversation.feed(_question("2", provider_request_id="que_1"), 0.0)
    assert again is None and "the same ask arrived twice" in conversation.failure
    assert json.loads(replies[0])["directive"] == "fail"


def test_a_request_id_used_twice_is_a_protocol_violation():
    conversation = _running()
    conversation.feed(_question("1"), 0.0)
    conversation.feed(_question("1", provider_request_id="que_9"), 0.0)
    assert "repeats a request id" in conversation.failure


def test_an_ask_before_the_admission_is_rejected():
    start = _start()
    conversation = _conversation(start)
    for record in (_hello(), _rec(type="server", runtime_version="1.18.34"), _session(start)):
        conversation.feed(record, 0.0)
    replies, ask = conversation.feed(_question(), 0.0)
    assert ask is None and "before the input was admitted" in conversation.failure
    assert json.loads(replies[0])["directive"] == "fail"


def test_decisions_carry_answers_custom_words_and_feedback():
    conversation = _running()
    _, question = conversation.feed(_question("1"), 0.0)
    answer = Answer((QuestionResponse(choices=("blue",), custom_text="and teal"),))
    assert json.loads(conversation.decided(question, Deliver(answer))[0]) == {
        "type": "decision",
        "request_id": "1",
        "directive": "answer",
        "answers": [["blue", "and teal"]],
    }
    _, permission = conversation.feed(_permission("2"), 0.0)
    assert json.loads(conversation.decided(permission, Deliver(Deny("use git diff")))[0]) == {
        "type": "decision",
        "request_id": "2",
        "directive": "reject",
        "message": "use git diff",
    }
    assert conversation.failure is None


@pytest.mark.parametrize(
    ("kind", "directive", "expected"),
    [
        ("question", Deliver(Cancel()), {"directive": "reject"}),
        ("permission", Deliver(Allow()), {"directive": "once"}),
        ("permission", Deliver(Deny()), {"directive": "reject"}),
        ("permission", Deliver(Cancel()), {"directive": "reject"}),
        ("question", Pause(request=None), {"directive": "pause"}),  # type: ignore[arg-type]
    ],
)
def test_each_directive_maps_to_one_reply(kind, directive, expected):
    conversation = _running()
    _, ask = conversation.feed(_question() if kind == "question" else _permission(), 0.0)
    record = json.loads(conversation.decided(ask, directive)[0])
    assert record == {"type": "decision", "request_id": "1", **expected}
    assert conversation.failure is None


def test_a_fail_directive_rejects_and_fails_with_the_brokers_reason():
    conversation = _running()
    _, ask = conversation.feed(_question(), 0.0)
    record = json.loads(conversation.decided(ask, Fail("interaction_policy is fail"))[0])
    assert record["directive"] == "fail"
    assert conversation.failure == "interaction_policy is fail"


@pytest.mark.parametrize(
    "decision",
    [Allow(scope=AllowScope.SESSION), Allow(updated_input={"command": "ls"})],
)
def test_what_no_opencode_reply_carries_is_never_sent(decision):
    conversation = _running()
    _, ask = conversation.feed(_permission(), 0.0)
    record = json.loads(conversation.decided(ask, Deliver(decision))[0])
    assert record["directive"] == "fail"
    assert "cannot be carried" in conversation.failure
    with pytest.raises(InteractionDataError):
        decision_record(ask, Deliver(decision))


def test_a_second_decision_for_one_ask_is_refused():
    conversation = _running()
    _, ask = conversation.feed(_question(), 0.0)
    conversation.decided(ask, Deliver(Cancel()))
    assert conversation.decided(ask, Deliver(Cancel())) == []
    assert "a decision for an ask that is not open" in conversation.failure


# -- the outcome --------------------------------------------------------------------------
def test_the_final_answer_is_taken_only_with_the_end():
    conversation = _running()
    conversation.feed(_result(), 0.0)
    assert conversation.text is None and not conversation.done
    conversation.feed(END, 0.0)
    assert conversation.done and conversation.text == "the answer"
    summary = conversation.summary()
    assert summary["message_id"] == "msg_answer" and summary["finish"] == "stop"
    assert summary["server_stopped"] == "sigterm" and summary["bridge_skipped_unknown"] == 1
    # Names, ids and counts only: never the prompt or the answer.
    rendered = json.dumps(summary)
    assert "SECRET-PROMPT-TEXT" not in rendered and "the answer" not in rendered


@pytest.mark.parametrize(
    "override",
    [
        {"input_id": "msg_earlier"},
        {"session_id": "ses_other"},
        {"finish": "tool-calls"},
        {"finish": "content-filter"},
        {"text": None},
    ],
)
def test_a_result_that_is_not_the_admitted_inputs_answer_fails(override):
    conversation = _running()
    conversation.feed(_result(**override), 0.0)
    assert "not the admitted input's final answer" in conversation.failure


def test_a_second_result_fails():
    conversation = _running()
    conversation.feed(_result(), 0.0)
    conversation.feed(_result(), 0.0)
    assert "a second result" in conversation.failure


def test_an_end_claiming_a_result_it_never_sent_fails():
    conversation = _running()
    conversation.feed(END, 0.0)
    assert "a result it never sent" in conversation.failure


def test_an_interrupted_end_takes_no_answer():
    conversation = _running()
    assert json.loads(conversation.interrupt()[0]) == {"type": "interrupt"}
    assert conversation.interrupt() == []  # once
    conversation.feed(_result(), 0.0)
    conversation.feed(END, 0.0)
    assert conversation.done and conversation.interrupted and conversation.text is None


def test_recover_takes_the_answer_without_an_admission():
    start = _start(BridgeMode.RECOVER)
    conversation = _conversation(start)
    for record in (_hello(), _rec(type="server", runtime_version="1.18.34"), _session(start)):
        conversation.feed(record, 0.0)
    conversation.feed(_result(), 0.0)
    conversation.feed(END, 0.0)
    assert conversation.text == "the answer" and not conversation.admitted
    other = _conversation(start)
    for record in (_hello(), _rec(type="server", runtime_version="1.18.34"), _session(start)):
        other.feed(record, 0.0)
    other.feed(_rec(type="admitted", input_id=INPUT, sent=True), 0.0)
    assert "out of place" in other.failure  # recover admits nothing


def test_the_bridges_failure_is_named_and_a_retryable_503_is_transient():
    conversation = _running()
    conversation.feed(
        _rec(
            type="failure",
            code="model",
            reason="the answer ended with an error (APIError)",
            details={"error": "APIError", "status": 503, "retryable": True},
            server={"stopped": "sigterm"},
        ),
        0.0,
    )
    assert conversation.failure == (
        "opencode-sdk: the answer ended with an error (APIError) (model)"
    )
    assert conversation.transient() == "opencode-sdk: model API error 503"
    plain = _running()
    plain.feed(
        _rec(type="failure", code="model", reason="x", details={"status": 400, "retryable": False}),
        0.0,
    )
    assert plain.transient() is None


def test_a_failure_reason_is_bounded_and_redacted():
    conversation = _running()
    conversation.feed(
        _rec(type="failure", code="weird", reason="token ghp_" + "a" * 36 + " " + "x" * 2000),
        0.0,
    )
    assert "ghp_" + "a" * 36 not in conversation.failure
    assert len(conversation.failure) <= 400 and "(unknown)" in conversation.failure


def test_stdout_ending_early_is_named_with_the_stage():
    conversation = _running()
    conversation.stream_ended()
    assert conversation.exited_early
    assert "exited without its last record (running)" in conversation.failure
