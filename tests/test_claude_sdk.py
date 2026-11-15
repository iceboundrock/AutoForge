"""The ``claude-sdk`` transport (#151): the envelope reducer, the helper and the adapter.

Three layers, none of which needs the real SDK, Claude Code, a network or a
credential:

- :class:`autoforge.claude_sdk.ClaudeSdkSession` driven by synthetic helper
  records, as ``test_claude_stream.py`` drives the stream reducer;
- the real helper program (``sdk_helpers/claude_helper.py``) as a child, on
  the fake ``claude_agent_sdk`` of :mod:`tests.claude_sdk_fake`, spoken to
  directly and through the real :class:`~autoforge.providers.ClaudeSdkProvider`
  and duplex handle;
- the structural rules of ADR 0007 D2.1-D2.3: no SDK import in the
  controller, the helper's imports and interpreter floor, the manifest's
  digest.

The opt-in gate probes on an installed pinned pair are ``test_claude_sdk_probe.py``.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import subprocess
import threading
import time
import uuid
from pathlib import Path

import pytest

from autoforge import claude_sdk
from autoforge.claude_sdk import (
    Ask,
    ClaudeSdkSession,
    answer_input,
    ask_questions,
    modified_input_problem,
    start_record,
)
from autoforge.config import ProfileConfig
from autoforge.errors import ConfigurationError, ExecutionError, InteractionDataError
from autoforge.interaction import (
    Allow,
    Answer,
    Cancel,
    DecisionKind,
    Deny,
    InteractionBroker,
    InteractionRequest,
    Lifecycle,
    QuestionResponse,
    RequestKind,
    Unattended,
)
from autoforge.providers import (
    CLAUDE_SDK_CAPABILITIES,
    AgentRequest,
    ClaudeSdkProvider,
    claude_config_files,
    provider_for,
)
from autoforge.sdk_assets import helper_pin, helper_problem, helpers_dir, load_manifest
from autoforge.sdk_policy import (
    CLAUDE_SDK,
    ClaudeSdkRoots,
    claude_sdk_policy,
    interaction_settings,
)

from . import claude_sdk_fake as fake

KEY = "sk-ant-api03-" + "k" * 48
FINAL = "done\n<<<CONTROL_RESULT>>>\n{}\n<<<END_CONTROL_RESULT>>>"
SRC = Path(__file__).resolve().parent.parent / "src" / "autoforge"
HELPER = SRC / "sdk_helpers" / "claude_helper.py"


# -- the reducer ---------------------------------------------------------------------
SESSION = "6a1f3c9e-0d2b-4c55-9d3e-7b1a2c3d4e5f"


def _session(options: dict | None = None, session_id: str = SESSION) -> ClaudeSdkSession:
    policy = claude_sdk_policy("fix", options or {})
    start = start_record(
        session_id=session_id,
        prompt="p",
        cwd="/w",
        model="m",
        effort="",
        policy=policy,
        max_record_bytes=1 << 20,
    )
    return ClaudeSdkSession(
        pin=helper_pin(CLAUDE_SDK), policy=policy, session_id=session_id, start=start
    )


def _rec(record_type: str, **fields: object) -> bytes:
    return json.dumps({"type": record_type, **fields}).encode()


HELLO = _rec(
    "hello",
    envelope="autoforge-claude-helper",
    protocol=1,
    sdk_version="0.2.163",
    cli_version="2.1.286",
    python_version="3.12.1",
)


def _init(policy_options: dict | None = None, **over: object) -> bytes:
    tools = list(claude_sdk_policy("fix", policy_options or {}).tools)
    data = {
        "type": "system",
        "subtype": "init",
        "session_id": SESSION,
        "model": "claude-fable-5-2",
        "permissionMode": "default",
        "apiKeySource": "ANTHROPIC_API_KEY",
        "tools": tools,
        "mcp_servers": [],
        "plugins": [],
        **over,
    }
    return _rec("message", data=data)


def _result(**over: object) -> bytes:
    data = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "num_turns": 1,
        "duration_ms": 5,
        "session_id": SESSION,
        "stop_reason": "end_turn",
        "terminal_reason": "completed",
        "result": FINAL,
        "deferred_tool_use": None,
        "origin": None,
        **over,
    }
    return _rec("message", data=data)


END = _rec("end", outcome="result", skipped_unknown=0, asks=0, hook_denials=0, host_denials=0)


def _feed(session: ClaudeSdkSession, *records: bytes) -> list[Ask]:
    asks = []
    for record in records:
        ask = session.feed(record)
        if ask is not None:
            asks.append(ask)
    return asks


def test_hello_sends_start_and_a_completed_turn_is_the_text():
    session = _session()
    _feed(session, HELLO)
    assert session.started and len(session.outbox) == 1
    start = json.loads(session.outbox[0])
    assert start["type"] == "start" and start["mode"] == "new"
    assert start["session_id"] == SESSION and start["interactions"] == {
        "live": False,
        "defer": False,
    }
    assert start["policy"]["verbatim_prompts"] is True
    assert start["hook_timeout_seconds"] == 120 + claude_sdk.HOOK_TIMEOUT_MARGIN_SECONDS
    text = {"type": "assistant", "message": {"content": [{"type": "text"}]}}
    _feed(session, _init(), _rec("message", data=text))
    _feed(session, _result(), END)
    assert session.done and session.failed is None and session.text == FINAL
    assert session.summary()["session_id"] == SESSION
    assert session.reply_proven


@pytest.mark.parametrize(
    ("field", "value", "named"),
    [
        ("envelope", "autoforge-other", "a damaged or foreign helper"),
        ("protocol", 2, "a damaged or foreign helper"),
        ("protocol", True, "a damaged or foreign helper"),
        ("sdk_version", "0.2.164", "SDK pair mismatch"),
        ("cli_version", "2.1.304", "SDK pair mismatch"),
        ("python_version", "3.9.18", "SDK pair mismatch"),
        ("python_version", None, "SDK pair mismatch"),
    ],
)
def test_a_hello_off_the_pinned_pair_never_starts(field, value, named):
    hello = json.loads(HELLO)
    hello[field] = value
    session = _session()
    _feed(session, json.dumps(hello).encode())
    assert not session.started and not session.outbox
    assert named in (session.failed or "")


@pytest.mark.parametrize(
    ("records", "named"),
    [
        ([_rec("message", data={})], "before hello"),
        ([HELLO, b"not json"], "not JSON"),
        ([HELLO, _rec("noise")], "unknown record type"),
        ([HELLO, _rec("end", outcome="result")], "exactly protocol 1's fields"),
        ([HELLO, HELLO], "a second hello"),
        ([HELLO, _init(), END], "says result before any result"),
        ([HELLO, _init(), _result(), END, END], "after the helper's end"),
        (
            [
                HELLO,
                _rec(
                    "end",
                    outcome="interrupted",
                    skipped_unknown=0,
                    asks=0,
                    hook_denials=0,
                    host_denials=0,
                ),
            ],
            "nobody asked it to",
        ),
    ],
)
def test_the_envelope_fails_closed(records, named):
    session = _session()
    _feed(session, *records)
    assert session.text is None
    assert named in (session.failed or ""), session.failed


@pytest.mark.parametrize(
    ("record", "named"),
    [
        (_init(session_id="other"), "session id"),
        (_init(tools=["Read"]), "departed from the policy"),
        (_init(permissionMode="bypassPermissions"), "permission mode"),
        (_init(apiKeySource="/login managed key"), "ANTHROPIC_API_KEY"),
        (_init(mcp_servers=[{"name": "x"}]), "MCP servers"),
        (_result(session_id="other"), "session id"),
        (_result(subtype="error_new_kind"), "unknown subtype"),
        (_result(terminal_reason="max_turns"), "did not complete its turn"),
        (_result(terminal_reason=None), "did not complete its turn"),
        (_result(terminal_reason="something_new"), "did not complete its turn"),
        (_result(stop_reason="max_tokens"), "did not complete its turn"),
        (_result(stop_reason=None), "did not complete its turn"),
        (_result(deferred_tool_use={"id": "t", "name": "Bash"}), "deferred a tool call"),
        (_result(origin="task-notification"), "injected"),
        (_result(is_error=True, terminal_reason="api_error"), "ended in an error"),
        (_result(subtype="error_max_turns"), "did not succeed"),
        (_result(result=None), "no string 'result'"),
        (
            _rec(
                "message",
                data={"type": "assistant", "message": {}, "parent_tool_use_id": "toolu_1"},
            ),
            "a subagent ran",
        ),
    ],
)
def test_an_outcome_that_is_no_completed_main_turn_fails_closed(record, named):
    session = _session()
    _feed(session, HELLO, _init(), record, _result(), END)
    assert session.text is None and named in (session.failed or ""), session.failed
    assert session.failed.startswith("claude-sdk: ")


def test_success_with_is_error_is_a_failure_not_a_reply():
    """An API error can come with ``subtype: success`` (#395's shape)."""
    session = _session()
    error = _result(is_error=True, terminal_reason="api_error", api_error_status=529)
    _feed(session, HELLO, _init(), error, END)
    assert session.done and session.text is None
    assert session.stream.transient == "claude: API error 529"


def test_a_typed_rate_limit_of_the_sdk_is_the_usage_limit():
    session = _session()
    limited = _rec("message", data={"type": "assistant", "message": {}, "error": "rate_limit"})
    _feed(session, HELLO, _init(), limited)
    assert session.stream.api_limit == "claude: API error 429"


def test_a_planted_block_outside_the_final_result_is_never_the_text():
    """Only the completed result's text crosses: a block in a failure
    reason, an earlier message or a later record is never read as one."""
    session = _session()
    planted = _rec("failure", reason=FINAL)
    _feed(session, HELLO, _init(), _rec("message", data={"type": "assistant", "text": FINAL}))
    _feed(session, planted)
    assert session.text is None and "<<<CONTROL_RESULT>>>" in (session.failed or "")
    malformed = _session()
    _feed(malformed, HELLO, _init(), _result(result="no block here"), END)
    assert malformed.text == "no block here" and not malformed.reply_proven


def test_an_unknown_observation_is_counted_not_fatal():
    session = _session()
    notice = _rec("message", data={"type": "system", "subtype": "brand_new"})
    unknown = _rec("message", data={"type": "telemetry_v9"})
    end = _rec("end", outcome="result", skipped_unknown=3, asks=0, hook_denials=0, host_denials=0)
    _feed(session, HELLO, _init(), notice, unknown, _result(), end)
    assert session.text == FINAL
    assert session.summary()["unknown_events"] == 1
    assert session.summary()["skipped_unknown"] == 3


_TEXT = _rec("message", data={"type": "assistant", "message": {"content": [{"type": "text"}]}})
_TOOL_RESULT = _rec(
    "message",
    data={
        "type": "user",
        "message": {"content": [{"type": "tool_result", "tool_use_id": "toolu_1"}]},
        "parent_tool_use_id": None,
    },
)


@pytest.mark.parametrize(
    ("records", "named"),
    [
        # No system/init at all: the result, or a message of the turn, first.
        ([_result(), END], "the result came before the session's system/init"),
        ([_TEXT, _result(), END], "an assistant message came before"),
        ([_TOOL_RESULT, _result(), END], "a user message came before"),
        # Out of order: the init arrives once the turn is under way, or after it.
        ([_TEXT, _init(), _result(), END], "an assistant message came before"),
        ([_result(), _init(), END], "the result came before"),
        # An init that did not check out verifies nothing: the turn after it
        # fails on that init's own reason.
        ([_init(session_id="other"), _TEXT, _result(), END], "session id"),
        ([_init(tools=["Read"]), _result(), END], "departed from the policy"),
    ],
)
def test_nothing_of_a_turn_is_read_before_a_verified_init(records, named):
    session = _session()
    _feed(session, HELLO, *records)
    assert session.text is None and not session.reply_proven
    assert named in (session.failed or ""), session.failed
    assert session.done


def test_an_ask_before_the_init_is_never_put_to_anyone():
    session = _session(PROMPT)
    assert _feed(session, HELLO, _ask()) == []
    assert session.open_asks == ()
    assert "an ask came before the session's system/init" in (session.failed or "")
    assert session.failure_kind == "claude-sdk: no verified system/init"
    # A late init changes nothing: the session has failed.
    _feed(session, _init(PROMPT), _result(), END)
    assert session.text is None


def test_observations_before_the_init_are_read_and_the_turn_after_it_is_the_reply():
    """A system notice or a rate-limit observation may come before the init;
    only the turn itself needs one first."""
    session = _session()
    notice = _rec("message", data={"type": "system", "subtype": "status"})
    limit = _rec("message", data={"type": "rate_limit_event", "status": "allowed"})
    _feed(session, HELLO, notice, limit, _init(), _TEXT, _result(), END)
    assert session.failed is None and session.text == FINAL and session.reply_proven


def test_a_turn_interrupted_before_its_init_ends_interrupted_not_failed():
    """The controller interrupted before the session started (a limit during
    connect): the helper's interrupted end is the outcome, with no init."""
    session = _session()
    _feed(session, HELLO)
    session.interrupt()
    interrupted = _rec(
        "end", outcome="interrupted", skipped_unknown=0, asks=0, hook_denials=0, host_denials=0
    )
    _feed(session, interrupted)
    assert session.outcome == "interrupted" and session.failure is None and session.text is None


def _ask(**over: object) -> bytes:
    tool_input = over.pop("input", {"command": "ls", "description": "list"})
    fields = {
        "request_id": "ask-1",
        "kind": "permission",
        "tool_name": "Bash",
        "tool_use_id": "toolu_1",
        "input": tool_input,
        "input_sha256": hashlib.sha256(
            json.dumps(tool_input, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "agent": None,
        **over,
    }
    return _rec("ask", **fields)


PROMPT = {"interaction_policy": "prompt", "ask_tools": "Bash"}


def test_an_ask_is_handed_to_the_driver_and_answered_once():
    session = _session(PROMPT)
    (ask,) = _feed(session, HELLO, _init(PROMPT), _ask())
    assert ask.kind is RequestKind.PERMISSION and not ask.problem
    assert session.open_asks == (ask,)
    line = json.loads(session.decide(ask, "deny", guidance="no"))
    assert line == {
        "type": "decision",
        "request_id": "ask-1",
        "directive": "deny",
        "updated_input": None,
        "guidance": "no",
    }
    with pytest.raises(ValueError):
        session.decide(ask, "allow")


@pytest.mark.parametrize(
    ("records", "named"),
    [
        ([_ask(tool_name="Read")], "does not ask about"),
        ([_ask(kind="question")], "does not match its tool"),
        ([_ask(), _ask(tool_use_id="toolu_2")], "fresh request id"),
        ([_ask(), _ask(request_id="ask-2")], "fresh tool call id"),
        ([_ask(input_sha256="x")], "input digest"),
        ([_ask(input=["a"])], "not an object"),
        (
            [
                _ask(),
                _rec(
                    "end",
                    outcome="result",
                    skipped_unknown=0,
                    asks=1,
                    hook_denials=0,
                    host_denials=0,
                ),
            ],
            "ask still open",
        ),
    ],
)
def test_an_ask_out_of_protocol_fails_closed(records, named):
    session = _session(PROMPT)
    _feed(session, HELLO, _init(PROMPT), *records)
    assert named in (session.failed or ""), session.failed


def test_ask_data_that_cannot_be_shown_is_a_problem_not_a_request():
    session = _session(PROMPT)
    big = {"command": "x" * (70 * 1024)}
    (oversize, digest, missing) = _feed(
        session,
        HELLO,
        _init(PROMPT),
        _ask(input=big),
        _ask(request_id="ask-2", tool_use_id="toolu_2", input_sha256="0" * 64),
        _ask(request_id="ask-3", tool_use_id="toolu_3", input=None, input_sha256="0" * 64),
    )
    assert "more than 65536" in oversize.problem
    assert "does not match the digest" in digest.problem
    assert "does not fit one helper record" in missing.problem


QUESTION = {
    "questions": [
        {
            "question": "Which colors?",
            "header": "Colors",
            "multiSelect": True,
            "options": [
                {"label": "Red", "description": "r"},
                {"label": "Blue", "description": "b"},
            ],
        },
        {
            "question": "Which size?",
            "header": "Size",
            "multiSelect": False,
            "options": [{"label": "S", "description": "s"}, {"label": "M", "description": "m"}],
        },
    ]
}


def _question_ask(tool_input: object = None) -> Ask:
    return Ask(
        request_id="ask-1",
        kind=RequestKind.QUESTION,
        tool_name="AskUserQuestion",
        tool_use_id="toolu_q",
        tool_input=QUESTION if tool_input is None else tool_input,
        input_sha256="0" * 64,
        agent=None,
    )


def test_questions_keep_their_raw_labels_and_answers_echo_the_questions():
    ask = _question_ask()
    questions = ask_questions(ask)
    assert [q.multiple for q in questions] == [True, False] and all(q.custom for q in questions)
    assert [o.label for o in questions[0].options] == ["Red", "Blue"]
    answer = Answer(
        (QuestionResponse(choices=("Red", "Blue")), QuestionResponse(custom_text="Extra large"))
    )
    updated = answer_input(ask, answer)
    assert updated["questions"] == QUESTION["questions"]
    assert updated["answers"] == {"Which colors?": "Red, Blue", "Which size?": "Extra large"}


_ESCAPED_LABEL = [{"label": "\x1b[31mRed", "description": ""}]


@pytest.mark.parametrize(
    "tool_input",
    [
        {"questions": QUESTION["questions"], "answers": {}},
        {"questions": []},
        {"questions": [{**QUESTION["questions"][0], "preview": "x"}]},
        {"questions": [{**QUESTION["questions"][0], "options": [{"label": "A"}]}]},
        {"questions": [{**QUESTION["questions"][0], "multiSelect": "yes"}]},
        {"questions": [{**QUESTION["questions"][0], "options": _ESCAPED_LABEL}]},
    ],
)
def test_a_question_the_broker_cannot_render_is_refused(tool_input):
    with pytest.raises(InteractionDataError):
        ask_questions(_question_ask(tool_input))


def test_a_questions_text_is_cleaned_for_display_and_its_key_kept_raw():
    raw = "\x1b[2JWhich colors?"
    ask = _question_ask({"questions": [{**QUESTION["questions"][0], "question": raw}]})
    (question,) = ask_questions(ask)
    assert "\x1b" not in question.text and question.text.endswith("Which colors?")
    answer = Answer((QuestionResponse(choices=("Red",)),))
    assert answer_input(ask, answer)["answers"] == {raw: "Red"}


_CALL = {"command": "rm -r x", "description": "remove x"}


@pytest.mark.parametrize(
    ("tool", "updated", "original"),
    [
        ("Bash", {"command": "ls"}, _CALL),
        (
            "Bash",
            {"command": "ls", "description": "", "timeout": 5000, "run_in_background": False},
            _CALL,
        ),
        ("Bash", {"command": "ls", "timeout": 600_000.0}, _CALL),
        # A field AutoForge's schema does not name, kept as the call carried it.
        ("Bash", {"command": "ls", "newer_field": [1]}, {**_CALL, "newer_field": [1]}),
        # Empty text where the tool takes it: an edit that deletes, an empty file.
        ("Edit", {"file_path": "/w/a", "old_string": "x", "new_string": ""}, None),
        ("Write", {"file_path": "/w/a", "content": ""}, None),
        ("Grep", {"pattern": "x", "output_mode": "count", "-n": True, "-C": 0}, None),
        ("NotebookEdit", {"notebook_path": "/w/n", "new_source": "", "edit_mode": "delete"}, None),
        ("WebSearch", {"query": "q", "allowed_domains": ["example.org"]}, None),
        ("Read", {"file_path": "/w/a", "offset": 0, "limit": 10}, None),
    ],
)
def test_a_modified_input_the_tool_takes_passes(tool, updated, original):
    assert modified_input_problem(tool, updated, original) == ""


@pytest.mark.parametrize(
    ("tool", "updated", "named"),
    [
        # An empty modified input is a modified input, and is checked.
        ("Bash", {}, "lacks command"),
        ("Write", {}, "lacks file_path, content"),
        ("Bash", {"cmd": "ls"}, "lacks command"),
        ("Bash", {"command": ""}, "command must be a non-empty string"),
        ("Bash", {"command": None}, "command must be a non-empty string"),
        ("Bash", {"command": "ls", "timeout": "5000"}, "timeout must be a number"),
        ("Bash", {"command": "ls", "timeout": True}, "timeout must be a number"),
        ("Bash", {"command": "ls", "timeout": 0}, "timeout must be a number"),
        ("Bash", {"command": "ls", "timeout": 600_001}, "timeout must be a number"),
        ("Bash", {"command": "ls", "run_in_background": "yes"}, "run_in_background must be"),
        ("Bash", {"command": "ls", "description": 7}, "description must be a string"),
        ("Read", {"file_path": "/w/a", "offset": -1}, "offset must be a whole number"),
        ("Read", {"file_path": "/w/a", "limit": 2.5}, "limit must be a whole number"),
        ("Grep", {"pattern": "x", "output_mode": "lines"}, "output_mode must be one of"),
        ("Grep", {"pattern": "x", "-i": 1}, "-i must be true or false"),
        ("Edit", {"file_path": "/w/a", "old_string": "x", "new_string": None}, "new_string"),
        ("WebSearch", {"query": "q", "blocked_domains": "a.org"}, "blocked_domains must be"),
        # A field the tool does not take, which the call did not carry.
        ("Bash", {"command": "ls", "sudo": True}, "sets sudo, which the Bash tool does not"),
        # One the call carried, with another value.
        ("Bash", {"command": "ls", "newer_field": [2]}, "sets newer_field"),
        ("AskUserQuestion", {"questions": []}, "knows no input schema of AskUserQuestion"),
    ],
)
def test_a_modified_input_the_tool_would_not_take_is_refused(tool, updated, named):
    original = {**_CALL, "newer_field": [1]}
    assert named in modified_input_problem(tool, updated, original)


def test_every_tool_a_profile_may_ask_about_has_an_input_schema():
    from autoforge.sdk_policy import CLAUDE_SDK_TOOLS

    assert set(claude_sdk.TOOL_INPUT_SCHEMAS) == set(CLAUDE_SDK_TOOLS)


# -- the helper and the adapter, on the fake SDK -------------------------------------
@pytest.fixture(scope="module")
def sdk_python(tmp_path_factory: pytest.TempPathFactory) -> str:
    return str(fake.make_environment(tmp_path_factory.mktemp("fake-sdk") / "env"))


@pytest.fixture
def launch_env(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-proj-" + "o" * 40)
    monkeypatch.setenv("FAKE_SECRET_TOKEN", "t" * 30)


class Scripted:
    """A resolver: the next scripted decision (or a callable of the request)."""

    def __init__(self, *decisions) -> None:
        self.decisions = list(decisions)
        self.asked: list[InteractionRequest] = []

    def resolve(self, request: InteractionRequest, deadline: float):
        self.asked.append(request)
        decision = self.decisions.pop(0)
        return decision(request, deadline) if callable(decision) else decision


def _profile(command: str, options: dict | None = None) -> ProfileConfig:
    return ProfileConfig(
        name="fix",
        provider=CLAUDE_SDK,
        model="claude-fable-5-2",
        effort="high",
        command=command,
        options=dict(options or {}),
    )


def _request(tmp_path: Path, command: str, prompt: str, options=None, resolver=None, **kw):
    profile = _profile(command, options)
    adapter = kw.pop("adapter", None) or ClaudeSdkProvider()
    adapter.validate_profile(profile)
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    state = tmp_path / "sandbox"
    ClaudeSdkRoots.under(state).config.mkdir(parents=True, exist_ok=True)
    broker = kw.pop("broker", "default")
    if broker == "default":
        broker = InteractionBroker(
            interaction_settings(profile.options, "test"),
            adapter.interaction_capabilities(profile),
            resolver or Unattended(),
        )
    req = AgentRequest(
        phase="fix",
        prompt=prompt,
        cwd=str(work),
        profile=profile,
        idle_timeout_seconds=kw.pop("idle", 60),
        max_runtime_seconds=kw.pop("ceiling", 120),
        env_allowlist=("PATH", "HOME", "OPENAI_API_KEY", "FAKE_SECRET_*"),
        env=kw.pop("env", None),
        interaction=broker,
        state_dir=str(state),
    )
    return adapter, req


def _run(tmp_path, command, prompt, options=None, resolver=None, **kw):
    adapter, req = _request(tmp_path, command, prompt, options, resolver, **kw)
    started = time.monotonic()
    result = adapter.execute(req)
    return result, req, time.monotonic() - started


def test_a_completed_session_through_the_real_helper(tmp_path, sdk_python, launch_env):
    prompt = fake.script(
        steps=[
            {"message": {"kind": "assistant", "content": [{"type": "text", "text": FINAL}]}},
            {"tool": {"id": "toolu_1", "name": "Bash", "input": {"command": "ls"}}},
            {"print": f"stray output {FINAL}"},
        ],
        result={"result": FINAL},
    )
    result, req, _ = _run(tmp_path, sdk_python, prompt)
    assert result.exit_code == 0 and result.provider_failure is None and not result.timed_out
    assert result.stdout == FINAL and result.reply_delivered
    assert result.interaction is not None and result.interaction.lifecycle is Lifecycle.COMPLETED
    summary = result.provider_summary
    assert summary["sdk_version"] == "0.2.163" and summary["outcome"] == "result"
    uuid.UUID(str(summary["session_id"]))
    assert summary["cost_micro_usd"] == 1200 and summary["tool_calls"] == 1
    entries = fake.log(Path(req.cwd))
    options, env = entries[0]["options"], entries[0]["env"]
    policy = claude_sdk_policy("fix", {})
    assert options["session_id"] == summary["session_id"]
    assert options["setting_sources"] == [] and options["plugins"] == []
    assert options["mcp_servers"] == {} and options["strict_mcp_config"] is True
    assert options["extra_args"] == {} and options["env"] == {}
    assert options["tools"] == list(policy.tools)
    assert options["allowed_tools"] == list(policy.allowed_tools)
    assert options["disallowed_tools"] == list(policy.disallowed_tools)
    assert options["permission_mode"] == "default" and options["verbatim_prompts"] is True
    assert options["model"] == "claude-fable-5-2" and options["effort"] == "high"
    assert options["cwd"] == req.cwd and options["cli_path"].endswith("/_bundled/claude")
    assert options["hooks"] == {"PreToolUse": [{"matcher": None, "timeout": 150.0, "n": 1}]}
    assert options["can_use_tool"] is True
    assert env["CLAUDE_CONFIG_DIR"] == str(ClaudeSdkRoots.under(Path(req.state_dir)).config)
    assert env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"
    assert env["CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK"] == "1"
    assert env["ANTHROPIC_API_KEY"] == KEY
    # Other providers' and credential-shaped variables never reach it.
    assert env["OPENAI_API_KEY"] is None and env["FAKE_SECRET_TOKEN"] is None
    assert entries[1] == {"hook": "Bash", "tool_use_id": "toolu_1", "output": {}}
    # The staged copy is gone, and stray output went to stderr, not the wire.
    assert "stray output" in result.stderr


def test_the_bash_tool_gets_the_clis_shell_isolation(tmp_path, sdk_python, launch_env):
    """The SDK's bundled Claude Code is the CLI #165 isolates: its launch gets
    the same CLAUDE_ENV_FILE cleaner, shell and empty prefix."""
    from autoforge.providers import CLAUDE_SHELL_CLEANER

    result, req, _ = _run(tmp_path, sdk_python, fake.script(result={"result": FINAL}))
    assert result.stdout == FINAL
    entry = fake.log(Path(req.cwd))[0]
    assert entry["env"]["CLAUDE_ENV_FILE"] and entry["env"]["CLAUDE_CODE_SHELL_PREFIX"] == ""
    assert entry["env_file"].startswith(CLAUDE_SHELL_CLEANER)
    assert not os.path.exists(entry["env"]["CLAUDE_ENV_FILE"])  # removed with the launch


@pytest.mark.parametrize("name", ["settings.json", ".claude.json"])
def test_an_env_left_in_the_persistent_root_refuses_the_next_launch(
    tmp_path, sdk_python, launch_env, name, monkeypatch
):
    # The real resolution, not conftest's empty directory.
    monkeypatch.setattr("autoforge.providers.claude_config_files", claude_config_files)
    adapter, req = _request(tmp_path, sdk_python, fake.script())
    root = ClaudeSdkRoots.under(Path(req.state_dir)).config
    (root / name).write_text(json.dumps({"env": {"GH_TOKEN": "x" * 20}}), encoding="utf-8")
    with pytest.raises(ExecutionError, match="GH_TOKEN in the env of"):
        adapter.execute(req)
    assert fake.log(Path(req.cwd)) == []


def test_a_failing_result_with_exit_zero_is_a_provider_failure(tmp_path, sdk_python, launch_env):
    prompt = fake.script(result={"is_error": True, "terminal_reason": "api_error", "result": FINAL})
    result, _, _ = _run(tmp_path, sdk_python, prompt)
    assert result.exit_code == 0 and result.stdout == ""
    assert "ended in an error" in (result.provider_failure or "")
    assert not result.reply_delivered


@pytest.mark.parametrize(
    ("script", "named"),
    [
        ({"init": {"tools": ["Read", "WebFetch"]}}, "departed from the policy"),
        ({"result": {"session_id": "11111111-1111-4111-8111-111111111111"}}, "session id"),
        ({"result": {"terminal_reason": "max_turns", "result": FINAL}}, "did not complete"),
        ({"steps": [{"stop": True}]}, "ended without a result"),
        ({"steps": [{"raise": "runtime"}]}, "the helper failed: RuntimeError"),
        ({"connect": "raise"}, "the helper failed: RuntimeError"),
        (
            {"steps": [{"message": {"kind": "assistant", "parent_tool_use_id": "toolu_a"}}]},
            "a subagent ran",
        ),
    ],
)
def test_an_sdk_failure_is_a_bounded_redacted_provider_failure(
    tmp_path, sdk_python, launch_env, script, named
):
    result, _, took = _run(tmp_path, sdk_python, fake.script(**script))
    assert named in (result.provider_failure or ""), result.provider_failure
    assert result.stdout == "" and not result.timed_out and took < 30
    # An SDK exception's text (here, a key) is never quoted.
    assert "sk-ant-api03-" not in (result.provider_failure or "") + result.stderr


_SAID = {"message": {"kind": "assistant", "content": [{"type": "text", "text": FINAL}]}}


@pytest.mark.parametrize(
    ("script", "named"),
    [
        # The session emits no system/init at all.
        ({"init": False}, "the result came before the session's system/init"),
        ({"init": False, "steps": [_SAID]}, "an assistant message came before"),
        (
            {"init": False, "steps": [{"tool": {"id": "t", "name": "Read", "input": {}}}]},
            "an assistant message came before",
        ),
        # Its system/init comes once the turn is under way.
        ({"init": False, "steps": [_SAID, {"init": {}}]}, "an assistant message came before"),
    ],
)
def test_a_session_without_a_verified_init_is_never_a_reply(
    tmp_path, sdk_python, launch_env, script, named
):
    result, _, took = _run(tmp_path, sdk_python, fake.script(result={"result": FINAL}, **script))
    assert named in (result.provider_failure or ""), result.provider_failure
    assert result.stdout == "" and not result.reply_delivered and took < 30


def test_an_ask_before_the_init_is_never_put_to_the_operator(tmp_path, sdk_python, launch_env):
    resolver = Scripted(Allow())
    # The hook's ask reaches the controller before any message of the turn.
    call = {"tool": {**_bash()["tool"], "silent": True}}
    prompt = fake.script(init=False, steps=[call], result={"result": FINAL})
    result, req, took = _run(tmp_path, sdk_python, prompt, PROMPT, resolver)
    assert resolver.asked == []
    assert "an ask came before the session's system/init" in (result.provider_failure or "")
    assert result.stdout == "" and not result.reply_delivered and took < 30
    # The helper's waiting hook was settled by the wind-down, as a denial.
    (hook,) = [e for e in fake.log(Path(req.cwd)) if e.get("hook")]
    assert hook["output"]["hookSpecificOutput"]["permissionDecision"] == "deny"


@pytest.mark.parametrize(
    "late",
    [
        ['{"type":"failure","reason":"the helper failed after all"}\n'],
        ['{"type":"message","data":{"type":"system","subtype":"status"}}\n'],
        [
            '{"type":"end","outcome":"result","skipped_unknown":0,"asks":0,'
            '"hook_denials":0,"host_denials":0}\n'
        ],
        ["not json\n"],
    ],
)
def test_a_record_the_helper_writes_after_its_end_fails_the_run(
    tmp_path, sdk_python, launch_env, late
):
    """Whatever stdout still held after the helper's ``end`` is read whole
    before the outcome is: a completed reply followed by anything is no reply."""
    prompt = fake.script(result={"result": FINAL}, after_end=late)
    result, _, _ = _run(tmp_path, sdk_python, prompt)
    assert "a record after the helper's end" in (result.provider_failure or "")
    assert result.stdout == "" and not result.reply_delivered


def test_an_unterminated_tail_after_the_helpers_end_fails_the_run(tmp_path, sdk_python, launch_env):
    prompt = fake.script(result={"result": FINAL}, after_end=['{"type":"failure","rea'])
    result, _, _ = _run(tmp_path, sdk_python, prompt)
    assert "unterminated record" in (result.provider_failure or "")
    assert result.stdout == "" and not result.reply_delivered


class _Rest:
    """A finished duplex handle's stdout: the items still queued, then EOF."""

    def __init__(self, *items) -> None:
        from autoforge.executor_duplex import Eof

        self.items = [*items, Eof()]

    def read_line(self, timeout=None):
        return self.items[0] if len(self.items) == 1 else self.items.pop(0)

    def unread_at_deadline(self):
        return []


def test_the_rest_of_stdout_is_read_past_an_end_it_holds():
    """An ``end`` in the remainder ends the session, and what follows it is
    still read: a failure after it fails the run (the adapter's drain, as
    after a wind-down that did not reach the end in time)."""
    from autoforge.executor_duplex import Fragment, Oversize, Record

    for late, named in (
        (Record(_rec("failure", reason="late")), "a record after the helper's end"),
        (Oversize(1 << 20, b"{"), "longer than"),
        (Fragment(b'{"type"'), "unterminated record"),
    ):
        session = _session()
        _feed(session, HELLO, _init(), _TEXT)
        ClaudeSdkProvider._read_rest(_Rest(Record(_result()), Record(END), late), session)
        assert session.text is None and named in (session.failed or ""), session.failed
    clean = _session()
    _feed(clean, HELLO, _init(), _TEXT)
    ClaudeSdkProvider._read_rest(_Rest(Record(_result()), Record(END)), clean)
    assert clean.failed is None and clean.text == FINAL and clean.reply_proven


def test_a_pair_mismatch_never_starts_a_session(tmp_path_factory, tmp_path, launch_env):
    python = fake.make_environment(tmp_path_factory.mktemp("old-sdk") / "env", version="0.2.150")
    result, req, _ = _run(tmp_path, str(python), fake.script())
    assert "SDK pair mismatch" in (result.provider_failure or "")
    assert "claude-agent-sdk 0.2.150" in result.provider_failure
    assert fake.log(Path(req.cwd)) == []  # never connected


def test_a_limit_interrupts_the_turn_and_wins(tmp_path, sdk_python, launch_env):
    prompt = fake.script(steps=[{"sleep": 60}], result={"result": FINAL})
    result, _, took = _run(tmp_path, sdk_python, prompt, ceiling=2)
    assert result.timed_out and result.timeout_limit == "max_runtime"
    assert result.stdout == "" and result.provider_failure is None
    assert result.provider_summary["outcome"] == "interrupted"
    assert took < 2 + ClaudeSdkProvider().interrupt_seconds


def test_an_idle_limit_ends_a_silent_turn(tmp_path, sdk_python, launch_env):
    result, _, took = _run(tmp_path, sdk_python, fake.script(steps=[{"sleep": 60}]), idle=2)
    assert result.timed_out and result.timeout_limit == "idle" and took < 20


def test_a_detached_descendant_is_killed_and_reported(tmp_path, sdk_python, launch_env):
    marker = f"sleep 301.{os.getpid()}"
    prompt = fake.script(steps=[{"spawn": ["sh", "-c", marker]}], result={"result": FINAL})
    result, _, _ = _run(tmp_path, sdk_python, prompt)
    assert result.stdout == FINAL
    assert result.orphans_killed or result.orphans_unchecked or result.descendants_killed
    if not result.orphans_unchecked:
        found = subprocess.run(["pgrep", "-f", marker], capture_output=True, text=True).stdout
        assert not found.strip()


def test_a_flood_of_records_is_read_and_counted(tmp_path, sdk_python, launch_env):
    prompt = fake.script(steps=[{"flood": 300}], result={"result": FINAL})
    result, _, _ = _run(tmp_path, sdk_python, prompt)
    assert result.stdout == FINAL and result.provider_summary["records"] >= 300


def test_an_output_flood_is_bounded_and_fails_closed(tmp_path, sdk_python, launch_env):
    """Past the duplex queue's bound, records the reducer has not read end the
    invocation (an overflow): the run fails, and no reply is taken from it."""
    prompt = fake.script(steps=[{"flood": 20000}], result={"result": FINAL})
    result, _, took = _run(tmp_path, sdk_python, prompt)
    assert took < 60
    if result.provider_failure is not None:
        assert "faster than they were read" in result.provider_failure
        assert result.stdout == "" and not result.reply_delivered
    else:
        assert result.stdout == FINAL


def _question_script(**result: object) -> str:
    return fake.script(
        steps=[{"tool": {"id": "toolu_q", "name": "AskUserQuestion", "input": QUESTION}}],
        result={"result": FINAL, **result},
    )


def test_a_question_is_answered_live_with_the_questions_echoed(tmp_path, sdk_python, launch_env):
    resolver = Scripted(
        Answer((QuestionResponse(choices=("Blue",)), QuestionResponse(custom_text="Huge")))
    )
    options = {"interaction_policy": "prompt"}
    result, req, _ = _run(tmp_path, sdk_python, _question_script(), options, resolver)
    assert result.stdout == FINAL and result.provider_failure is None
    (request,) = resolver.asked
    assert request.kind is RequestKind.QUESTION and len(request.questions) == 2
    assert DecisionKind.CUSTOM_ANSWER in request.offers
    assert DecisionKind.ALLOW_SESSION not in CLAUDE_SDK_CAPABILITIES.offers(RequestKind.PERMISSION)
    hook = next(e for e in fake.log(Path(req.cwd)) if e.get("hook"))
    output = hook["output"]["hookSpecificOutput"]
    assert output["permissionDecision"] == "allow"
    assert output["updatedInput"] == {
        "questions": QUESTION["questions"],
        "answers": {"Which colors?": "Blue", "Which size?": "Huge"},
    }


@pytest.mark.parametrize(
    ("decision", "expected"),
    [
        (Allow(), {"hookEventName": "PreToolUse", "permissionDecision": "allow"}),
        (
            Allow(updated_input={"command": "echo safer"}),
            {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
                "updatedInput": {"command": "echo safer"},
            },
        ),
        (
            # Optional fields of the kinds the tool takes, and one the call
            # itself carried, unchanged.
            Allow(
                updated_input={
                    "command": "echo safer",
                    "timeout": 5000,
                    "description": "",
                    "hint": "kept",
                }
            ),
            {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
                "updatedInput": {
                    "command": "echo safer",
                    "timeout": 5000,
                    "description": "",
                    "hint": "kept",
                },
            },
        ),
        (
            Deny(guidance="Use Read instead."),
            {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": "Use Read instead.",
            },
        ),
        (
            Cancel(),
            {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": "AutoForge's operator declined this call",
            },
        ),
    ],
)
def test_a_permission_is_decided_live(tmp_path, sdk_python, launch_env, decision, expected):
    prompt = fake.script(
        steps=[
            {
                "tool": {
                    "id": "toolu_b",
                    "name": "Bash",
                    "input": {"command": "rm -r x", "hint": "kept"},
                }
            }
        ],
        result={"result": FINAL},
    )
    resolver = Scripted(decision)
    result, req, _ = _run(tmp_path, sdk_python, prompt, PROMPT, resolver)
    assert result.stdout == FINAL and result.provider_failure is None
    (request,) = resolver.asked
    assert request.tool_name == "Bash" and "rm -r x" in request.summary
    hook = next(e for e in fake.log(Path(req.cwd)) if e.get("hook"))
    assert hook["output"]["hookSpecificOutput"] == expected
    # A denial alone is an observation: the run that adapts and ends is usable.
    assert result.interaction is not None and result.interaction.lifecycle is Lifecycle.COMPLETED


def _bash(command: str = "rm -r x", call: str = "toolu_b") -> dict:
    return {"tool": {"id": call, "name": "Bash", "input": {"command": command}}}


@pytest.mark.parametrize(
    ("options", "steps", "decisions", "named"),
    [
        ({}, [_bash()], [], "not one this profile approves"),
        (
            {"interaction_policy": "fail", "tools": "Read,Bash"},
            [{"tool": {"id": "t", "name": "WebFetch", "input": {"url": "x"}}}],
            [],
            "not one this profile approves",
        ),
    ],
)
def test_a_call_the_policy_does_not_ask_about_is_denied_by_the_hook(
    tmp_path, sdk_python, launch_env, options, steps, decisions, named
):
    if options.get("interaction_policy") != "fail":
        options = {**options, "tools": "Read"}
    prompt = fake.script(steps=steps, result={"result": FINAL})
    result, req, _ = _run(tmp_path, sdk_python, prompt, options, Scripted(*decisions))
    assert result.stdout == FINAL
    hook = next(e for e in fake.log(Path(req.cwd)) if e.get("hook"))
    assert named in hook["output"]["hookSpecificOutput"]["permissionDecisionReason"]
    assert result.provider_summary["hook_denials"] == 1


def test_a_bare_allow_of_a_question_falls_to_the_host_which_denies(
    tmp_path, sdk_python, launch_env, monkeypatch
):
    """What the pinned pair does with ``allow`` and no ``answers`` (the fake
    models it): the permission host is asked and denies."""
    prompt = fake.script(
        steps=[{"tool": {"id": "toolu_q", "name": "AskUserQuestion", "input": QUESTION}}],
        result={"result": FINAL},
    )
    adapter, req = _request(tmp_path, sdk_python, prompt, {"interaction_policy": "prompt"})

    # Answer the helper's ask with a bare allow, as a broken resolver would.
    def bare(child, session, ask, broker, asked):
        adapter._send(child, session, session.decide(ask, "allow"))

    monkeypatch.setattr(adapter, "_answer", bare)
    result = adapter.execute(req)
    host = next(e for e in fake.log(Path(req.cwd)) if e.get("host"))
    assert host["result"]["behavior"] == "deny"
    assert result.provider_summary["host_denials"] == 1


@pytest.mark.parametrize(
    ("options", "steps", "decisions", "named"),
    [
        ({"interaction_policy": "fail"}, [], [], None),
        (PROMPT, [_bash()], [Allow(updated_input={"cmd": "ls"})], "lacks command"),
        # An empty modified input is checked like any other, never sent.
        (PROMPT, [_bash()], [Allow(updated_input={})], "lacks command"),
        (
            PROMPT,
            [_bash()],
            [Allow(updated_input={"command": "ls", "timeout": "soon"})],
            "timeout must be a number",
        ),
        (
            PROMPT,
            [_bash()],
            [Allow(updated_input={"command": "ls", "run_in_background": "yes"})],
            "run_in_background must be true or false",
        ),
        (
            PROMPT,
            [_bash()],
            [Allow(updated_input={"command": "ls", "sudo": True})],
            "which the Bash tool does not take",
        ),
        (
            {**PROMPT, "max_interactions": "1"},
            [_bash(), _bash("ls", "toolu_c")],
            [Allow(), Allow()],
            "interaction limit reached",
        ),
        (
            # A modified input that redaction would change is refused where it
            # is built: the resolver's decision never reaches the helper.
            PROMPT,
            [_bash()],
            [lambda r, d: Allow(updated_input={"command": f"curl https://x:{'a' * 40}@h/p"})],
            "asking for it failed (InteractionDataError)",
        ),
        (
            {"interaction_policy": "prompt"},
            [{"tool": {"id": "toolu_q", "name": "AskUserQuestion", "input": QUESTION}}],
            [
                lambda r, d: Answer(
                    (
                        QuestionResponse(choices=("Red",)),
                        QuestionResponse(custom_text=f"ANTHROPIC_API_KEY={KEY}"),
                    )
                )
            ],
            "asking for it failed (InteractionDataError)",
        ),
        (PROMPT, [_bash(command="x" * (70 * 1024))], [Allow()], "more than 65536"),
        (
            {"interaction_policy": "prompt"},
            [
                {
                    "tool": {
                        "id": "toolu_q",
                        "name": "AskUserQuestion",
                        "input": {"questions": [{"question": "q"}]},
                    }
                }
            ],
            [],
            "exactly protocol 1's fields",
        ),
    ],
)
def test_an_ask_that_cannot_be_decided_fails_the_invocation(
    tmp_path, sdk_python, launch_env, options, steps, decisions, named
):
    if options.get("interaction_policy") == "fail":
        # Under fail nothing can ask: AskUserQuestion is not offered at all.
        assert "AskUserQuestion" in claude_sdk_policy("fix", options).disallowed_tools
        return
    prompt = fake.script(steps=steps, result={"result": FINAL})
    result, req, took = _run(tmp_path, sdk_python, prompt, options, Scripted(*decisions))
    assert result.interaction is not None and result.interaction.lifecycle is Lifecycle.FAILED
    assert named in (result.provider_failure or ""), result.provider_failure
    assert result.stdout == "" and took < 30
    hooks = [e for e in fake.log(Path(req.cwd)) if e.get("hook")]
    assert hooks[-1]["output"]["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_a_stalled_decision_is_bounded_by_the_invocations_limit(tmp_path, sdk_python, launch_env):
    def stall(request, deadline):
        time.sleep(max(0.0, deadline - time.monotonic()) + 0.2)
        return Allow()

    prompt = fake.script(steps=[_bash()], result={"result": FINAL})
    options = {**PROMPT, "max_wait_seconds": "30"}
    result, _, took = _run(tmp_path, sdk_python, prompt, options, Scripted(stall), ceiling=3)
    assert result.stdout == ""
    assert took < 3 + ClaudeSdkProvider().interrupt_seconds + 5
    assert result.timed_out or (
        result.interaction is not None and result.interaction.lifecycle is Lifecycle.FAILED
    )


def test_ctrl_c_during_a_wait_tears_the_helper_down(tmp_path, sdk_python, launch_env):
    def interrupt(request, deadline):
        raise KeyboardInterrupt

    marker = f"sleep 302.{os.getpid()}"
    prompt = fake.script(steps=[{"spawn": ["sh", "-c", marker]}, _bash()])
    adapter, req = _request(tmp_path, sdk_python, prompt, PROMPT, Scripted(interrupt))
    before = set(Path("/tmp").glob("autoforge-claude-sdk-*"))
    with pytest.raises(KeyboardInterrupt):
        adapter.execute(req)
    assert set(Path("/tmp").glob("autoforge-claude-sdk-*")) <= before
    found = subprocess.run(["pgrep", "-f", marker], capture_output=True, text=True).stdout
    assert not found.strip()


def test_the_launch_is_refused_before_anything_starts(tmp_path, sdk_python, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    adapter, req = _request(tmp_path, sdk_python, fake.script())
    with pytest.raises(ConfigurationError, match="ANTHROPIC_API_KEY is not set"):
        adapter.integration_preflight(req.env_allowlist)
    with pytest.raises(ExecutionError, match="ANTHROPIC_API_KEY is not set"):
        adapter.execute(req)
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    with pytest.raises(ExecutionError, match="CLAUDE_CODE_OAUTH_TOKEN"):
        adapter.execute(
            AgentRequest(**{**req.__dict__, "env": {"CLAUDE_CODE_OAUTH_TOKEN": "x" * 20}})
        )
    with pytest.raises(ExecutionError, match="no state directory"):
        adapter.execute(AgentRequest(**{**req.__dict__, "state_dir": ""}))
    assert fake.log(Path(req.cwd)) == []


def test_a_helper_that_is_not_the_manifests_refuses_the_launch(
    tmp_path, sdk_python, launch_env, monkeypatch
):
    copy = tmp_path / "helpers"
    copy.mkdir()
    (copy / "manifest.json").write_text(
        (helpers_dir() / "manifest.json").read_text(), encoding="utf-8"
    )
    (copy / "claude_helper.py").write_text(HELPER.read_text() + "\n# changed\n", encoding="utf-8")
    monkeypatch.setattr("autoforge.providers.helpers_dir", lambda: copy)
    monkeypatch.setattr("autoforge.sdk_assets.helpers_dir", lambda: copy)
    adapter, req = _request(tmp_path, sdk_python, fake.script())
    with pytest.raises(ConfigurationError, match="does not match the manifest's sha256"):
        adapter.integration_preflight(req.env_allowlist)
    with pytest.raises(ConfigurationError, match="does not match the manifest's sha256"):
        adapter.execute(req)
    assert fake.log(Path(req.cwd)) == []


def test_a_correction_is_a_fresh_session(tmp_path, sdk_python, launch_env):
    prompt = fake.script(result={"result": "no block"})
    first, req, _ = _run(tmp_path, sdk_python, prompt)
    second, _, _ = _run(tmp_path, sdk_python, prompt)
    assert first.stdout == "no block" and not first.reply_delivered
    assert first.provider_summary["session_id"] != second.provider_summary["session_id"]


# -- the helper alone --------------------------------------------------------------------
def _helper(tmp_path: Path, python: str) -> subprocess.Popen:
    env = {"PATH": os.environ.get("PATH", ""), "ANTHROPIC_API_KEY": KEY, "HOME": str(tmp_path)}
    return subprocess.Popen(
        [python, "-I", str(HELPER)],
        cwd=tmp_path,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )


def _start(tmp_path: Path, prompt: str, **over: object) -> bytes:
    policy = claude_sdk_policy("fix", {})
    record = json.loads(
        start_record(
            session_id=str(uuid.uuid4()),
            prompt=prompt,
            cwd=str(tmp_path),
            model="m",
            effort="",
            policy=policy,
            max_record_bytes=1 << 20,
        )
    )
    record.update(over)
    return json.dumps(record).encode() + b"\n"


def _records(out: bytes) -> list[dict]:
    return [json.loads(line) for line in out.splitlines()]


def test_the_helper_says_hello_first_and_writes_only_records(tmp_path, sdk_python):
    proc = _helper(tmp_path, sdk_python)
    out, err = proc.communicate(_start(tmp_path, fake.script(result={"result": FINAL})), 30)
    records = _records(out)
    assert records[0]["type"] == "hello" and records[0]["protocol"] == 1
    assert [r["type"] for r in records[1:]] == ["message"] * 2 + ["end"]
    assert proc.returncode == 0


@pytest.mark.parametrize(
    ("over", "named"),
    [
        ({"mode": "continue"}, "only a new session"),
        ({"interactions": {"live": False, "defer": True}}, "deferral is not supported"),
        ({"extra": 1}, "not protocol 1's start"),
        ({"max_record_bytes": 0}, "max_record_bytes"),
    ],
)
def test_the_helper_refuses_a_start_off_protocol(tmp_path, sdk_python, over, named):
    proc = _helper(tmp_path, sdk_python)
    out, _ = proc.communicate(_start(tmp_path, fake.script(), **over), 30)
    failure = _records(out)[-1]
    assert failure["type"] == "failure" and named in failure["reason"]
    assert proc.returncode == 1
    assert fake.log(tmp_path) == []


def test_the_helper_refuses_a_policy_it_would_have_to_widen(tmp_path, sdk_python):
    policy = claude_sdk_policy("fix", {}).start_fields()
    for key, value in (("setting_sources", ["project"]), ("extra_args", {"x": None})):
        proc = _helper(tmp_path, sdk_python)
        out, _ = proc.communicate(
            _start(tmp_path, fake.script(), policy={**policy, key: value}), 30
        )
        assert f"policy {key} is not the fixed value" in _records(out)[-1]["reason"]


def test_stdin_ending_mid_turn_interrupts_and_ends_promptly(tmp_path, sdk_python):
    proc = _helper(tmp_path, sdk_python)
    assert proc.stdin is not None and proc.stdout is not None
    proc.stdin.write(_start(tmp_path, fake.script(steps=[{"sleep": 60}])))
    proc.stdin.flush()
    assert json.loads(proc.stdout.readline())["type"] == "hello"
    assert json.loads(proc.stdout.readline())["data"]["subtype"] == "init"
    started = time.monotonic()
    proc.stdin.close()
    rest = _records(proc.stdout.read())
    proc.wait(30)
    assert time.monotonic() - started < 15
    assert rest[-1]["type"] == "end" and rest[-1]["outcome"] == "interrupted"
    assert rest[0]["data"]["terminal_reason"] == "aborted_tools"
    assert proc.returncode == 0


def test_a_decision_for_no_open_ask_is_a_helper_failure(tmp_path, sdk_python):
    proc = _helper(tmp_path, sdk_python)
    assert proc.stdin is not None and proc.stdout is not None
    proc.stdin.write(_start(tmp_path, fake.script(steps=[{"sleep": 30}])))
    proc.stdin.flush()
    proc.stdout.readline()
    proc.stdout.readline()
    stale = claude_sdk.decision_record("ask-9", "allow")
    proc.stdin.write(stale + b"\n")
    proc.stdin.flush()
    out = _records(proc.stdout.read())
    proc.wait(30)
    assert out[-1]["type"] == "failure" and "not waiting" in out[-1]["reason"]
    assert proc.returncode == 1


def test_a_helper_without_the_sdk_says_so(tmp_path):
    bare = tmp_path / "bare"
    subprocess.run(
        [os.sys.executable, "-m", "venv", "--without-pip", str(bare)],
        check=True,
        capture_output=True,
    )
    proc = _helper(tmp_path, str(bare / "bin" / "python"))
    out, _ = proc.communicate(b"", 30)
    (failure,) = _records(out)
    assert failure["type"] == "failure" and "cannot be imported" in failure["reason"]


# -- structure (ADR 0007 D2.1-D2.3) ------------------------------------------------------
def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(("." * node.level) + node.module)
    return names


def test_the_controller_never_imports_the_sdk_or_the_helper():
    for path in SRC.rglob("*.py"):
        if "sdk_helpers" in path.parts:
            continue
        for name in _imports(path):
            assert not name.startswith("claude_agent_sdk"), path
            assert "sdk_helpers" not in name, path
    assert not (SRC / "sdk_helpers" / "__init__.py").exists()


def test_the_helper_imports_the_standard_library_and_the_sdk_only():
    import sys

    allowed = set(sys.stdlib_module_names) | {"claude_agent_sdk"}
    for name in _imports(HELPER):
        assert not name.startswith("."), name
        assert name.split(".")[0] in allowed, name
        assert not name.startswith("autoforge"), name


def test_the_helper_runs_on_the_interpreter_floor():
    """Python 3.10 (ADR 0007 D2.3): its grammar, and no 3.11-only name."""
    source = HELPER.read_text(encoding="utf-8")
    ast.parse(source, feature_version=(3, 10))
    for name in ("asyncio.timeout(", "TaskGroup", "ExceptionGroup", "except*", "tomllib"):
        assert name not in source


def test_the_manifest_ships_the_helper_with_its_digest():
    pin = load_manifest()[CLAUDE_SDK]
    assert pin.shipped and helper_problem(pin) == ""
    assert pin.sha256 == hashlib.sha256(HELPER.read_bytes()).hexdigest(), (
        "sdk_helpers/claude_helper.py changed: set its sha256 in sdk_helpers/manifest.json"
    )


def test_the_helper_lock_pins_the_pair_with_hashes():
    lock = SRC.parent.parent / "helpers" / "claude-sdk" / "requirements.txt"
    text = lock.read_text(encoding="utf-8")
    assert "claude-agent-sdk==0.2.163" in text
    pins = [line for line in text.splitlines() if "==" in line and not line.startswith("#")]
    assert pins and all("--hash=sha256:" in text.split(line, 1)[1].split("==")[0] for line in pins)


def test_the_install_guide_names_the_interpreter_by_an_absolute_path():
    """``command`` runs as written, with no shell: a ``~`` would never expand."""
    import yaml

    guide = (SRC.parent.parent / "helpers" / "claude-sdk" / "README.md").read_text("utf-8")
    blocks = [b.split("```", 1)[0] for b in guide.split("```yaml\n")[1:]]
    assert blocks
    for block in blocks:
        for profile in yaml.safe_load(block)["profiles"].values():
            assert os.path.isabs(profile["command"]), profile["command"]


def test_claude_sdk_now_launches_and_opencode_sdk_is_still_refused(tmp_path):
    claude = provider_for(_profile("/usr/bin/python3"))
    assert claude.launch_problem() == ""
    opencode = provider_for(
        ProfileConfig(
            name="fix",
            provider="opencode-sdk",
            model="openai/gpt-5.6",
            command="node",
            options={"server_command": "opencode", "credential_env": "OPENAI_API_KEY"},
        )
    )
    with pytest.raises(ConfigurationError, match="cannot launch in this release"):
        opencode.integration_preflight(("PATH",))


def test_the_staged_helper_and_the_environment_are_declared_to_the_sandbox(tmp_path):
    env_root = tmp_path / "env"
    (env_root / "bin").mkdir(parents=True)
    (env_root / "lib").mkdir()
    (env_root / "pyvenv.cfg").write_text("home = /usr/bin\n", encoding="utf-8")
    python = env_root / "bin" / "python"
    python.symlink_to(os.path.realpath(os.sys.executable))
    staged = tmp_path / "stage" / "claude_helper.py"
    needs = ClaudeSdkProvider().sandbox_requirements(
        _profile(str(python)),
        [str(python), "-I", str(staged)],
        {"PATH": "/usr/bin", "ANTHROPIC_API_KEY": KEY},
        str(tmp_path / "home"),
    )
    assert str(staged.parent) in needs.config
    assert str(env_root) in needs.install_roots
    assert str(python) in needs.spelled_paths
    assert needs.executable == os.path.realpath(os.sys.executable)


def test_the_engines_state_directory_is_not_declared_twice():
    from autoforge.providers import _without_engine_state
    from autoforge.sandbox_policy import AuthItem, AuthMode, ProviderRequirements

    own = AuthItem(AuthMode.DIR_RW, "root", "/c/autoforge/sandbox/7/sdk/claude-sdk/config")
    other = AuthItem(AuthMode.DIR_RW, "other", "/elsewhere")
    needs = ProviderRequirements(auth=(own, other), state=("/c/x",))
    kept = _without_engine_state(needs, (own.source, "/c/x"))
    assert kept.auth == (other,) and kept.state == ()
    assert _without_engine_state(needs, ()) is needs


def test_concurrent_records_never_reach_the_parser_unbounded(tmp_path, sdk_python, launch_env):
    """A result past the stdout bound is tail-bounded, as every adapter's."""
    adapter = ClaudeSdkProvider(max_text_bytes=1024)
    prompt = fake.script(result={"result": "x" * 5000 + FINAL})
    result, _, _ = _run(tmp_path, sdk_python, prompt, adapter=adapter)
    assert result.stdout_truncated and result.stdout.endswith(FINAL)
    assert len(result.stdout.encode()) <= 1024


def test_threads_left_behind_are_none(tmp_path, sdk_python, launch_env):
    before = threading.active_count()
    _run(tmp_path, sdk_python, fake.script(result={"result": FINAL}))
    deadline = time.monotonic() + 5
    while threading.active_count() > before and time.monotonic() < deadline:
        time.sleep(0.05)
    assert threading.active_count() <= before


# -- the engine's provider state directories (#151) -------------------------------------
def test_the_engine_makes_the_providers_state_directories_in_every_mode(tmp_state_dir):
    from autoforge.providers import ClaudeCodeProvider

    from .conftest import make_engine

    eng = make_engine(tmp_state_dir, [])
    profile = _profile("/usr/bin/python3")
    base, config = eng._provider_state(ClaudeSdkProvider(), profile)
    common = eng.workspace().git_dirs()[-1]
    assert Path(base).parent == common / "autoforge" / "sandbox"
    assert Path(config) == ClaudeSdkRoots.under(Path(base)).config and Path(config).is_dir()
    # Under the git common dir, never inside an issue worktree.
    assert common / "autoforge" / "worktrees" not in Path(config).parents
    # Idempotent, and nothing for a provider that names none.
    assert eng._provider_state(ClaudeSdkProvider(), profile) == (base, config)
    before = sorted(p.name for p in Path(base).iterdir())
    claude = ProfileConfig(name="fix", provider="claude", model="opus", command="claude")
    assert eng._provider_state(ClaudeCodeProvider(), claude) == ()
    assert sorted(p.name for p in Path(base).iterdir()) == before


def test_a_provider_state_directory_is_an_engine_carve_out_inside_the_sandbox(tmp_path):
    from autoforge.sandbox import Access, Origin
    from autoforge.sandbox_policy import LaunchClass, LaunchFacts, sandbox_request

    common = tmp_path / "repo" / ".git"
    state = common / "autoforge" / "sandbox" / "7" / "sdk" / "claude-sdk" / "config"
    facts = LaunchFacts(
        launch_class=LaunchClass.REMOTE_AGENT,
        workspace=str(common / "autoforge" / "worktrees" / "7"),
        home=str(tmp_path / "home"),
        controller_env={},
        git_entry=str(common / "autoforge" / "worktrees" / "7" / ".git"),
        git_dir=str(common / "worktrees" / "7"),
        common_dir=str(common),
        private_objects=str(tmp_path / "o"),
        overlay_work=str(tmp_path / "w"),
        provider_state=(str(state),),
    )
    request = sandbox_request(facts)
    (projection,) = [p for p in request.projections if p.source == str(state)]
    assert projection.access is Access.WRITABLE and projection.origin is Origin.ENGINE
    assert projection.carve_out


def test_a_launch_is_handed_its_providers_state_directory(tmp_state_dir):
    """Through a real step: the engine makes the directories the provider
    names before every launch, a correction's included, and hands the issue's
    sandbox directory over; a CLI provider gets none."""
    from autoforge.errors import ControlResultValidationError
    from autoforge.providers import ProviderRegistry, ScriptedProvider

    from .conftest import make_engine

    class Stateful(ScriptedProvider):
        def state_directories(self, profile: ProfileConfig) -> tuple[str, ...]:
            return ("sdk/probe/state",)

    eng = make_engine(tmp_state_dir, [], origin=True)
    provider = Stateful(["no result"] * 4)
    eng.providers = ProviderRegistry(overrides={"claude": provider, "opencode": provider})
    eng.step()  # INITIALIZING -> ANALYZE_EXECUTE
    with pytest.raises(ControlResultValidationError):
        eng.step()
    assert len(provider.calls) == 2
    common = eng.workspace().git_dirs()[-1]
    for call in provider.calls:
        assert Path(call.state_dir).parent == common / "autoforge" / "sandbox"
        assert (Path(call.state_dir) / "sdk" / "probe" / "state").is_dir()
    assert eng._provider_state(ScriptedProvider(), provider.calls[0].profile) == ()
