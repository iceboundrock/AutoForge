"""Pi RPC adapter (#131, ADR 0003): the reducer, and the provider over a fake ``pi``.

Deterministic only: no network, no real Pi, no credentials. The reducer
tests feed :class:`PiConversation` synthetic records with no process; the
provider tests run :class:`PiProvider` against a small Python script that
plays a scripted RPC conversation. Every fixture is synthetic.
"""

from __future__ import annotations

import itertools
import json
import os
import sys
import time
from pathlib import Path

import pytest

from autoforge import executor, providers
from autoforge.config import ProfileConfig
from autoforge.errors import ExecutionError
from autoforge.pi_rpc import PiConversation, js_trim
from autoforge.providers import AgentRequest, PiProvider
from autoforge.result_parser import parse_control_result
from autoforge.transitions import Phase
from tests.conftest import DECODER_LIMIT_RECORDS, SHA_A, analyze_payload, block

MODEL = "openai/gpt-5.6-terra"
STATE = {
    "model": {"provider": "openai", "id": "gpt-5.6-terra", "name": "GPT", "contextWindow": 1},
    "thinkingLevel": "high",
    "isStreaming": False,
}
MODELS = {
    "models": [
        {"provider": "openai", "id": "gpt-5.6-luna"},
        {"provider": "openai", "id": "gpt-5.6-terra"},
    ]
}
ANALYZE_OK = analyze_payload(SHA_A)
# What Pi returns: the text blocks of the last assistant message, trimmed.
FINAL = js_trim(block(ANALYZE_OK))


def assistant(text: str = FINAL, stop: str = "stop", **fields) -> dict:
    """An assistant message whose text blocks join (then trim) to ``text``."""
    half = len(text) // 2
    content = [
        {"type": "thinking", "thinking": "let me think"},
        {"type": "text", "text": "\n" + text[:half]},
        {"type": "toolCall", "id": "t1", "name": "bash", "arguments": {}},
        {"type": "text", "text": text[half:] + "\n\n"},
    ]
    return {"role": "assistant", "content": content, "stopReason": stop, **fields}


# -- the reducer, without a process ---------------------------------------------
class Run:
    """A conversation plus every record it asked to write, decoded."""

    def __init__(self, thinking: str = "high", prompt: str = "do the work", **kw) -> None:
        ids = (f"id-{n}" for n in itertools.count(1))
        self.c = PiConversation(
            model=MODEL,
            thinking=thinking,
            prompt=prompt,
            round_trip_seconds=5,
            new_id=lambda: next(ids),
            **kw,
        )
        self.now = 100.0
        self.raw: list[bytes] = list(self.c.start(self.now))

    @property
    def sent(self) -> list[dict]:
        return [json.loads(line) for line in self.raw]

    def types(self) -> list[str]:
        return [s["type"] for s in self.sent]

    def feed(self, record: dict | bytes) -> list[bytes]:
        data = record if isinstance(record, bytes) else json.dumps(record).encode()
        out = self.c.feed(data, self.now)
        self.raw += out
        return out

    def id_of(self, command: str) -> str:
        return [s["id"] for s in self.sent if s["type"] == command][-1]

    def respond(self, command: str, data=None, success: bool = True, **extra) -> list[bytes]:
        record = {"type": "response", "id": self.id_of(command), "command": command}
        record["success"] = success
        if data is not None:
            record["data"] = data
        record.update(extra)
        return self.feed(record)

    def verified(self) -> Run:
        self.respond("get_state", STATE)
        self.respond("get_available_models", MODELS)
        return self

    def started(self) -> Run:
        self.verified().respond("prompt", {"disposition": "started"})
        return self

    def settle(self, message: dict | None = None) -> Run:
        self.feed({"type": "message_end", "message": message or assistant()})
        self.feed({"type": "agent_end", "messages": [], "willRetry": False})
        self.feed({"type": "agent_settled"})
        return self

    def failure(self) -> str:
        assert self.c.done and self.c.text is None, self.c.summary()
        assert self.c.failure is not None
        return self.c.failure


def test_happy_path_returns_the_final_text_and_the_parser_accepts_it():
    run = Run()
    assert run.types() == ["get_state", "get_available_models"]
    run.verified()
    assert run.types()[-1] == "prompt"
    assert run.sent[-1] == {"id": "id-3", "type": "prompt", "message": "do the work"}
    run.respond("prompt", {"disposition": "started"})
    run.feed({"type": "agent_start"})
    run.feed({"type": "message_start", "message": {"role": "user", "content": "do the work"}})
    partial = {"role": "assistant", "content": [{"type": "text", "text": "some"}]}
    run.feed({"type": "message_update", "message": partial, "assistantMessageEvent": {}})
    run.feed({"type": "tool_execution_start", "toolCallId": "t1", "toolName": "bash"})
    run.feed({"type": "tool_execution_end", "toolCallId": "t1", "toolName": "bash"})
    run.feed({"type": "message_end", "message": {"role": "toolResult", "content": []}})
    run.feed({"type": "message_end", "message": assistant()})
    run.feed({"type": "agent_end", "messages": [], "willRetry": False})
    assert run.types()[-1] == "prompt"  # agent_end is never terminal
    run.feed({"type": "agent_settled"})
    assert run.types()[-1] == "get_last_assistant_text"
    run.respond("get_last_assistant_text", {"text": FINAL})
    assert run.c.done and run.c.failure is None
    assert run.c.text == FINAL
    assert parse_control_result(run.c.text, Phase.ANALYZE_EXECUTE) == ANALYZE_OK
    summary = run.c.summary()
    assert summary == {
        "resolved_provider": "openai",
        "resolved_model_id": "gpt-5.6-terra",
        "resolved_thinking": "high",
        "prompt_disposition": "started",
        "stop_reason": "stop",
        "agent_ends": 1,
        "will_retry": 0,
        "auto_retries": 0,
        "auto_retry_failed": False,
        "tool_executions": 1,
        "ui_dialogs_cancelled": 0,
        "ui_notifications_ignored": 0,
        "unknown_events": 0,
        "abort_sent": False,
        "failure": "",
    }
    # One prompt, never steer/follow_up, never an abort on success.
    assert run.types().count("prompt") == 1 and "abort" not in run.types()


def test_events_map_to_progress_with_one_allow_listed_argument():
    """#192: the accepted prompt is the start, a tool start shows one
    allow-listed argument (never bash's command), a delta is thinking or
    writing (never its content), a retry its attempt numbers."""
    from autoforge.progress import ProgressKind

    events = []
    run = Run(emit=events.append, cwd="/work/tree")
    run.verified()
    assert events == []  # nothing before the prompt is accepted
    run.respond("prompt", {"disposition": "started"})
    for record in [
        {"type": "agent_start"},
        {
            "type": "message_update",
            "assistantMessageEvent": {"type": "thinking_delta", "delta": "SECRET-PLAN"},
        },
        {
            "type": "message_update",
            "assistantMessageEvent": {"type": "text_delta", "delta": "SECRET-TEXT"},
        },
        {"type": "message_update", "assistantMessageEvent": {"type": "toolcall_delta"}},
        {
            "type": "tool_execution_start",
            "toolName": "read",
            "args": {"path": "/work/tree/src/a.py"},
        },
        {"type": "tool_execution_end", "toolName": "read", "isError": False},
        {"type": "tool_execution_start", "toolName": "grep", "args": {"pattern": "def main"}},
        {"type": "tool_execution_end", "toolName": "grep", "isError": True},
        {
            "type": "tool_execution_start",
            "toolName": "bash",
            "args": {"command": "echo SECRET-CMD"},
        },
        {"type": "tool_execution_start", "toolName": "edit", "args": {"path": 7}},
        {"type": "auto_retry_start", "attempt": 2, "maxAttempts": 3, "errorMessage": "overloaded"},
        {"type": "auto_retry_start", "attempt": True},
    ]:
        run.feed(record)
    run.settle()
    run.respond("get_last_assistant_text", {"text": FINAL})
    assert run.c.text == FINAL
    got = [(e.kind, e.tool, e.detail) for e in events]
    assert got[:14] == [
        (ProgressKind.STARTED, "", MODEL),
        (ProgressKind.ACTIVITY, "", ""),
        (ProgressKind.THINKING, "", ""),
        (ProgressKind.ASSISTANT_TEXT, "", ""),
        (ProgressKind.ACTIVITY, "", ""),
        (ProgressKind.TOOL_STARTED, "read", "src/a.py"),
        (ProgressKind.TOOL_FINISHED, "read", ""),
        (ProgressKind.TOOL_STARTED, "grep", "def main"),
        (ProgressKind.TOOL_FAILED, "grep", ""),
        (ProgressKind.TOOL_STARTED, "bash", ""),
        (ProgressKind.TOOL_STARTED, "edit", ""),
        (ProgressKind.PROVIDER_RETRY, "", "attempt 2 of 3"),
        (ProgressKind.PROVIDER_RETRY, "", ""),
        (ProgressKind.ACTIVITY, "", ""),  # message_end
    ]
    assert all(kind is ProgressKind.ACTIVITY for kind, _, _ in got[14:])
    assert "SECRET" not in " ".join(f"{e.tool} {e.detail}" for e in events)


def test_commands_are_ascii_json_records_with_no_line_break():
    run = Run(prompt="line one\nline two     \ud800 /template")
    run.verified()
    prompt_line = run.raw[-1]
    assert b"\n" not in prompt_line and prompt_line.isascii()
    assert json.loads(prompt_line)["message"].startswith("line one\nline two  ")
    # A prompt that begins with `/` is sent as is; the disposition is the backstop.
    assert Run(prompt="/review").verified().sent[-1]["message"] == "/review"


# -- id correlation --------------------------------------------------------------
def test_responses_out_of_order_are_correlated_by_id():
    run = Run()
    run.respond("get_available_models", MODELS)
    assert run.types() == ["get_state", "get_available_models"]  # not yet
    run.feed({"type": "some_future_event"})
    run.respond("get_state", STATE)
    assert run.types()[-1] == "prompt"


def test_events_before_the_prompt_response_are_reduced():
    run = Run().verified()
    run.feed({"type": "agent_start"})
    run.settle()
    assert run.types()[-1] == "prompt"  # the prompt is not yet accepted
    run.respond("prompt", {"disposition": "started"})
    assert run.types()[-1] == "get_last_assistant_text"
    run.respond("get_last_assistant_text", {"text": FINAL})
    assert run.c.text == FINAL


def test_a_response_to_an_unknown_id_is_a_protocol_failure():
    run = Run()
    run.feed({"type": "response", "id": "never-sent", "command": "get_state", "success": True})
    assert "protocol violation" in run.failure() and "never sent" in run.failure()


def test_a_second_response_to_the_same_id_is_a_protocol_failure():
    run = Run()
    run.respond("get_state", STATE)
    run.respond("get_state", STATE)
    assert "already answered" in run.failure()


def test_a_response_naming_another_command_is_a_protocol_failure():
    run = Run()
    run.feed(
        {"type": "response", "id": run.id_of("get_state"), "command": "prompt", "success": True}
    )
    assert "names command 'prompt'" in run.failure()


def test_a_response_without_success_is_a_protocol_failure():
    run = Run()
    run.feed({"type": "response", "id": run.id_of("get_state"), "command": "get_state"})
    assert "no boolean 'success'" in run.failure()


@pytest.mark.parametrize(
    ("record", "why"),
    [
        (b"Loading extensions...", "a stdout record is not JSON"),
        *(
            pytest.param(
                line,
                "a stdout record exceeds the JSON decoder's integer or nesting limit",
                id=name,
            )
            for name, line in DECODER_LIMIT_RECORDS.items()
        ),
    ],
)
def test_a_record_the_decoder_refuses_is_a_protocol_failure_not_an_exception(record, why):
    run = Run().started()
    run.feed(record)
    assert run.failure() == f"pi: protocol violation: {why}"


# -- settlement --------------------------------------------------------------------
def test_a_retry_after_agent_end_does_not_end_the_run_early():
    run = Run().started()
    run.feed({"type": "message_end", "message": assistant("", stop="error", errorMessage="503")})
    run.feed({"type": "agent_end", "messages": [], "willRetry": True})
    run.feed({"type": "auto_retry_start", "attempt": 1, "maxAttempts": 3, "delayMs": 10})
    run.feed({"type": "auto_retry_end", "success": True, "attempt": 1})
    assert "get_last_assistant_text" not in run.types()
    run.settle()
    assert run.types()[-1] == "get_last_assistant_text"
    run.respond("get_last_assistant_text", {"text": FINAL})
    assert run.c.text == FINAL
    summary = run.c.summary()
    assert summary["agent_ends"] == 2 and summary["will_retry"] == 1
    assert summary["auto_retries"] == 1 and summary["auto_retry_failed"] is False


def test_exhausted_retries_are_a_failure_with_the_final_error():
    run = Run().started()
    run.feed({"type": "message_end", "message": assistant("", stop="error", errorMessage="x")})
    run.feed({"type": "agent_end", "messages": [], "willRetry": True})
    run.feed({"type": "auto_retry_start", "attempt": 3})
    run.feed({"type": "auto_retry_end", "success": False, "finalError": "model is at capacity"})
    run.feed({"type": "agent_settled"})
    assert run.failure() == "pi: automatic retries failed: model is at capacity"
    assert "get_last_assistant_text" not in run.types()
    assert run.c.summary()["auto_retry_failed"] is True


def test_agent_settled_before_the_prompt_is_sent_is_not_the_end():
    run = Run()
    run.feed({"type": "agent_settled"})
    run.verified().respond("prompt", {"disposition": "started"})
    assert run.types()[-1] == "prompt" and not run.c.done


# -- prompt outcomes ------------------------------------------------------------------
def test_a_rejected_prompt_is_a_failure_with_its_bounded_error():
    run = Run().verified()
    run.respond("prompt", success=False, error="No API key found for openai.\nLog in first.")
    assert run.failure() == (
        "pi: prompt rejected before acceptance: No API key found for openai. Log in first."
    )


@pytest.mark.parametrize("disposition", ["queued", "handled", "deferred-ish", None])
def test_a_prompt_disposition_other_than_started_is_a_protocol_failure(disposition):
    run = Run().verified()
    run.respond("prompt", {} if disposition is None else {"disposition": disposition})
    failure = run.failure()
    assert "protocol violation" in failure
    if disposition is not None:
        assert f"disposition '{disposition}'" in failure
    assert "get_last_assistant_text" not in run.types()


# -- after acceptance ------------------------------------------------------------------
@pytest.mark.parametrize(
    ("message", "expected"),
    [
        (
            "Token refresh failed: invalid_grant (OAuth refresh)",
            "pi: model error after acceptance: Token refresh failed: invalid_grant (OAuth refresh)",
        ),
        (
            '429 {"error":{"code":"subscription_sharing_usage_limit_exceeded"}}',
            "pi: subscription usage limit reached",
        ),
        (None, "pi: model error after acceptance: no error message"),
    ],
)
def test_stop_reason_error_is_a_provider_failure(message, expected):
    run = Run().started()
    fields = {} if message is None else {"errorMessage": message}
    run.settle(assistant("partial", stop="error", **fields))
    assert run.failure() == expected
    assert run.c.summary()["stop_reason"] == "error"


def test_stop_reason_aborted_is_a_provider_failure():
    run = Run().started().settle(assistant("partial", stop="aborted"))
    assert run.failure() == "pi: the run was aborted, not by AutoForge"


def test_stop_reason_length_is_a_provider_failure_and_is_recorded():
    # Pi 1.0.1 drops a cut-off answer from its context, so the text cannot be
    # read back; get_last_assistant_text is never asked.
    run = Run().started().settle(assistant(stop="length"))
    assert run.failure() == "pi: the model hit its output limit (stopReason length)"
    assert run.c.summary()["stop_reason"] == "length"
    assert "get_last_assistant_text" not in run.types()


@pytest.mark.parametrize("stop", ["toolUse", "deferred", "pending", "later", None])
def test_any_other_final_stop_reason_is_a_protocol_failure(stop):
    message = assistant()
    if stop is None:
        del message["stopReason"]
    else:
        message["stopReason"] = stop
    run = Run().started().settle(message)
    assert "protocol violation: final stopReason" in run.failure()


def test_a_run_with_no_assistant_message_is_no_assistant_text():
    run = Run().started()
    run.feed({"type": "agent_end", "messages": []})
    run.feed({"type": "agent_settled"})
    assert run.failure().startswith("pi: no assistant text")


def test_secrets_in_pi_errors_are_redacted_and_bounded():
    run = Run().started()
    secret = "sk-proj-" + "a" * 40
    run.settle(assistant("x", stop="error", errorMessage=f"bad key {secret} " + "y" * 5000))
    failure = run.failure()
    assert secret not in failure and "***REDACTED***" in failure
    assert len(failure) <= 400
    assert secret not in json.dumps(run.c.summary())


# -- model and thinking verification -------------------------------------------------
@pytest.mark.parametrize(
    ("model", "expected"),
    [
        (
            {"provider": "openai-codex", "id": "gpt-5.6-terra"},
            "Pi resolved openai-codex/gpt-5.6-terra",
        ),
        (
            {"provider": "openai", "id": "gpt-5.6-terra-mini"},
            "Pi resolved openai/gpt-5.6-terra-mini",
        ),
        ({"provider": "openai", "id": "GPT-5.6-Terra"}, "Pi resolved openai/GPT-5.6-Terra"),
    ],
)
def test_a_model_mismatch_fails_before_the_prompt(model, expected):
    run = Run()
    run.respond("get_state", {**STATE, "model": model})
    run.respond("get_available_models", MODELS)
    assert run.failure() == f"pi: model mismatch: configured {MODEL}, {expected}"
    assert "prompt" not in run.types()
    assert run.c.summary()["resolved_model_id"] == model["id"]


def test_a_thinking_clamp_fails_before_the_prompt():
    run = Run(thinking="minimal")
    run.respond("get_state", {**STATE, "thinkingLevel": "low"})
    run.respond("get_available_models", MODELS)
    assert run.failure().startswith("pi: thinking mismatch: configured minimal, Pi resolved low")
    assert "prompt" not in run.types()


def test_a_missing_model_is_model_unavailable():
    run = Run()
    run.respond("get_state", {"thinkingLevel": "high"})
    assert run.failure() == f"pi: model unavailable: Pi resolved no model for {MODEL}"


def test_a_placeholder_model_outside_the_available_list_is_model_unavailable():
    """Pi starts with a placeholder whose id equals the configured string, so
    get_state matches; only get_available_models shows it does not exist."""
    run = Run()
    run.respond("get_available_models", {"models": [{"provider": "openai", "id": "gpt-5.6-luna"}]})
    run.respond("get_state", STATE)
    assert run.failure().startswith(f"pi: model unavailable: {MODEL} is not among")
    assert "prompt" not in run.types()


@pytest.mark.parametrize(
    "data",
    [
        None,
        {"model": "openai/gpt-5.6-terra", "thinkingLevel": "high"},
        {"model": {"provider": "openai"}, "thinkingLevel": "high"},
        {"model": STATE["model"]},
    ],
)
def test_a_get_state_missing_a_required_field_fails_closed(data):
    run = Run()
    run.respond("get_state", data)
    assert "protocol violation" in run.failure()


def test_get_available_models_without_a_list_fails_closed():
    run = Run()
    run.respond("get_available_models", {"models": "openai/gpt-5.6-terra"})
    assert "no 'models' list" in run.failure()


# -- final text ---------------------------------------------------------------------------
@pytest.mark.parametrize("data", [{}, {"text": None}, {"text": 7}, {"text": " \n﻿"}])
def test_no_assistant_text_is_a_provider_failure(data):
    """Pi sends ``data: {}`` (the key absent) although its docs say ``null``."""
    run = Run().started().settle()
    run.respond("get_last_assistant_text", data)
    assert run.failure() == "pi: no assistant text: the run ended without a final answer"


def test_a_final_text_that_differs_from_the_last_message_end_is_a_protocol_failure():
    run = Run().started().settle()
    run.respond("get_last_assistant_text", {"text": FINAL + " and one more line"})
    assert "differs from the text of the last assistant message_end" in run.failure()


def test_the_cross_check_trims_as_javascript_does():
    """U+FEFF is trimmed by JS and not by Python; U+001C the other way round."""
    text = "\x1cfinal answer   kept"
    run = Run().started()
    run.settle(assistant("﻿" + text + "　"))
    run.respond("get_last_assistant_text", {"text": text})
    assert run.c.text == text
    assert js_trim("﻿\x1c x \x85") == "\x1c x \x85"


def test_an_aborted_empty_message_is_skipped_like_pi_skips_it():
    run = Run().started()
    run.feed({"type": "message_end", "message": assistant()})
    run.feed(
        {"type": "message_end", "message": {**assistant(), "content": [], "stopReason": "aborted"}}
    )
    run.feed({"type": "message_end", "message": assistant()})
    run.feed({"type": "agent_settled"})
    run.respond("get_last_assistant_text", {"text": FINAL})
    assert run.c.text == FINAL


# -- framing hazards and malformed records ----------------------------------------------
def test_u2028_and_u2029_inside_a_json_string_are_content():
    run = Run().started()
    text = "before middle after"
    record = json.dumps({"type": "message_end", "message": assistant(text)}, ensure_ascii=False)
    run.feed(record.encode("utf-8"))
    run.feed({"type": "agent_settled"})
    run.respond("get_last_assistant_text", {"text": text})
    assert run.c.text == text


def test_a_parse_error_response_is_a_protocol_failure():
    run = Run()
    run.feed(
        {"type": "response", "command": "parse", "success": False, "error": "Unexpected token"}
    )
    assert run.failure() == (
        "pi: protocol violation: Pi could not parse a command: Unexpected token"
    )


@pytest.mark.parametrize(
    "data", [b"not json", b"\xff\xfe{}", b"[1, 2]", b'{"no": "type"}', b'{"type": 3}']
)
def test_a_record_that_is_not_a_typed_json_object_is_a_protocol_failure(data):
    run = Run()
    run.feed(data)
    assert "protocol violation" in run.failure()


def test_system_and_user_message_ends_are_not_the_answer():
    # Pi 1.0.1 emits the system prompt and the user prompt as message_end
    # records of their own before the assistant's.
    run = Run().started()
    run.feed({"type": "message_end", "message": {"role": "system", "content": ""}})
    user = {"role": "user", "content": [{"type": "text", "text": "do the work"}]}
    run.feed({"type": "message_end", "message": user})
    run.settle()
    run.respond("get_last_assistant_text", {"text": FINAL})
    assert run.c.text == FINAL


def test_unknown_events_are_ignored_and_counted():
    run = Run().started()
    run.feed({"type": "brand_new_event", "payload": {"x": 1}})
    run.feed({"type": "another_one"})
    run.feed({"type": "turn_end"})  # known, deliberately ignored
    run.feed({"type": "entry_appended", "entry": {"type": "context_edit"}})  # known too
    run.settle()
    run.respond("get_last_assistant_text", {"text": FINAL})
    assert run.c.text == FINAL and run.c.summary()["unknown_events"] == 2


# -- extension UI ---------------------------------------------------------------------------
def test_a_dialog_is_cancelled_at_once_and_fails_the_run():
    run = Run().started()
    notify = {"type": "extension_ui_request", "id": "ui-1", "method": "notify", "message": "hi"}
    assert run.feed(notify) == [] and not run.c.done
    out = run.feed(
        {"type": "extension_ui_request", "id": "ui-2", "method": "confirm", "title": "Run rm?"}
    )
    assert out == [b'{"type":"extension_ui_response","id":"ui-2","cancelled":true}']
    assert run.failure() == "pi: an extension asked for input (confirm); the dialog was cancelled"
    # Later dialogs are still answered, so an `editor` can never wait forever.
    later = run.feed({"type": "extension_ui_request", "id": "ui-3", "method": "editor"})
    assert later == [b'{"type":"extension_ui_response","id":"ui-3","cancelled":true}']
    summary = run.c.summary()
    assert summary["ui_dialogs_cancelled"] == 2 and summary["ui_notifications_ignored"] == 1


def test_an_unknown_extension_ui_method_fails_closed():
    run = Run().started()
    assert run.feed({"type": "extension_ui_request", "id": "u", "method": "teleport"}) == []
    assert "cannot answer ('teleport')" in run.failure()


@pytest.mark.parametrize("method", [["confirm"], {"m": "confirm"}, None, 7])
def test_a_non_string_extension_ui_method_fails_closed(method):
    run = Run().started()
    record = {"type": "extension_ui_request", "id": "u", "method": method}
    assert run.feed(record) == []
    assert run.failure() == (
        "pi: protocol violation: an extension UI request without a string 'method'"
    )


# -- bounds and lifecycle --------------------------------------------------------------------
def test_an_unanswered_round_trip_fails_at_its_bound():
    run = Run()
    assert run.c.next_wakeup == 105.0
    run.c.tick(104.9)
    assert not run.c.done
    run.c.tick(105.0)
    assert run.failure() == "pi: no response to get_state within 5s"


def test_the_prompt_round_trip_is_not_bounded_but_the_text_round_trip_is():
    run = Run().verified()
    assert run.c.next_wakeup is None
    run.respond("prompt", {"disposition": "started"})
    run.now = 500.0
    run.settle()
    assert run.c.next_wakeup == 505.0


@pytest.mark.parametrize(
    ("steps", "stage"),
    [
        (0, "before answering get_state and get_available_models"),
        (1, "before answering the prompt"),
        (2, "before agent_settled"),
        (3, "before answering get_last_assistant_text"),
    ],
)
def test_an_early_end_of_stdout_names_the_stage(steps, stage):
    run = Run()
    if steps >= 1:
        run.verified()
    if steps >= 2:
        run.respond("prompt", {"disposition": "started"})
    if steps >= 3:
        run.settle()
    run.c.stream_ended()
    assert run.failure() == f"pi: exited {stage}" and run.c.exited_early


def test_abort_is_offered_only_for_a_running_prompt():
    run = Run()
    assert run.c.begin_abort(run.now) == [] and run.c.abort_settled
    run.started()
    run.c.deadline_reached()
    [line] = run.c.begin_abort(run.now)
    assert json.loads(line)["type"] == "abort" and not run.c.abort_settled
    assert run.c.begin_abort(run.now) == []  # once
    run.feed(
        {"type": "response", "id": json.loads(line)["id"], "command": "abort", "success": True}
    )
    run.feed({"type": "agent_settled"})
    assert run.c.abort_settled and run.c.summary()["abort_sent"] is True
    assert run.c.failure.startswith("pi: the deadline was reached")


# -- the provider over a fake `pi` ------------------------------------------------------------
_FAKE_PI = r"""
import json, subprocess, sys, time
scenario = json.load(open(sys.argv[1]))
log = open(scenario["log"], "a")
log.write(json.dumps({"argv": sys.argv[2:]}) + "\n")
log.flush()
if sys.argv[2:4] == ["auth", "check"]:
    # The per-launch preflight; `env` lets a test see what reached it.
    log.write(json.dumps({"auth_env": sorted(k for k in __import__("os").environ)}) + "\n")
    log.flush()
    auth = scenario.get("auth", {"status": "ready", "provider": "openai", "authType": "oauth"})
    print(json.dumps(auth))
    sys.exit(scenario.get("auth_exit", 0))
out = sys.stdout.buffer
eol = b"\r\n" if scenario.get("crlf") else b"\n"
ids = {}

def emit(obj):
    out.write(json.dumps(obj, ensure_ascii=False).encode("utf-8") + eol)
    out.flush()

def run(actions):
    for a in actions:
        if "respond" in a:
            command = a["respond"]
            r = {"type": "response", "id": a.get("id", ids.get(command)), "command": command,
                 "success": a.get("success", True)}
            for key in ("data", "error"):
                if key in a:
                    r[key] = a[key]
            emit(r)
        elif "emit" in a:
            emit(a["emit"])
        elif "raw" in a:
            out.write(a["raw"].encode("utf-8"))
            out.flush()
        elif "stderr" in a:
            sys.stderr.write(a["stderr"])
            sys.stderr.flush()
        elif "sleep" in a:
            time.sleep(a["sleep"])
        elif "spawn_holder" in a:
            holder = "import time; time.sleep(%d)" % a["spawn_holder"]
            subprocess.Popen([sys.executable, "-c", holder])
        elif "spawn_detached" in a:
            # As Pi's bash tool starts a command: in a session of its own,
            # holding none of Pi's pipes.
            detached = "import time; time.sleep(%d)" % a["spawn_detached"]
            child = subprocess.Popen(
                [sys.executable, "-c", detached], start_new_session=True,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            log.write(json.dumps({"detached": child.pid}) + "\n")
            log.flush()
        elif "exit" in a:
            sys.exit(a["exit"])

run(scenario.get("start", []))
for line in sys.stdin.buffer:
    log.write(json.dumps({"stdin": line.decode("utf-8"), "at": time.time()}) + "\n")
    log.flush()
    command = json.loads(line)
    ids[command["type"]] = command.get("id")
    run(scenario["on"].get(command["type"], []))
run(scenario.get("eof", [{"exit": 0}]))
"""


def _happy(text: str = FINAL, events: list[dict] | None = None) -> dict:
    stream = events or [
        {"type": "agent_start"},
        {"type": "message_update", "message": {"role": "assistant", "content": []}},
        {"type": "message_end", "message": assistant(text)},
        {"type": "agent_end", "messages": [], "willRetry": False},
        {"type": "agent_settled"},
    ]
    return {
        "on": {
            "get_state": [{"respond": "get_state", "data": STATE}],
            "get_available_models": [{"respond": "get_available_models", "data": MODELS}],
            "prompt": [{"respond": "prompt", "data": {"disposition": "started"}}]
            + [{"emit": e} for e in stream],
            "get_last_assistant_text": [
                {"respond": "get_last_assistant_text", "data": {"text": text}}
            ],
            "abort": [{"respond": "abort"}, {"emit": {"type": "agent_settled"}}],
        }
    }


class FakePi:
    def __init__(self, tmp_path: Path, scenario: dict) -> None:
        self.log = tmp_path / "fake-pi.log"
        scenario = {**scenario, "log": str(self.log)}
        script = tmp_path / "fake_pi.py"
        script.write_text(_FAKE_PI, encoding="utf-8")
        spec = tmp_path / "scenario.json"
        spec.write_text(json.dumps(scenario), encoding="utf-8")
        self.command = tmp_path / "pi"
        self.command.write_text(
            f'#!/bin/sh\nexec "{sys.executable}" "{script}" "{spec}" "$@"\n', encoding="utf-8"
        )
        self.command.chmod(0o755)

    def entries(self) -> list[dict]:
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]

    def stdin(self) -> list[str]:
        return [e["stdin"] for e in self.entries() if "stdin" in e]

    def commands(self) -> list[str]:
        return [json.loads(line)["type"] for line in self.stdin()]

    def sent_at(self, command: str) -> float:
        """When the first ``command`` record reached the fake (``time.time()``)."""
        return next(
            e["at"]
            for e in self.entries()
            if "stdin" in e and json.loads(e["stdin"])["type"] == command
        )

    def argv(self) -> list[str]:
        """The RPC launch's argv (the auth preflight's is :meth:`auth_argvs`)."""
        return next(e["argv"] for e in self.entries() if e.get("argv", [""])[0] == "--mode")

    def launched(self) -> bool:
        return any(e.get("argv", [""])[0] == "--mode" for e in self.entries())

    def auth_argvs(self) -> list[list[str]]:
        return [e["argv"] for e in self.entries() if e.get("argv", [""])[:2] == ["auth", "check"]]

    def auth_env(self) -> list[str]:
        return next(e["auth_env"] for e in self.entries() if "auth_env" in e)


def _profile(
    command: Path, effort: str = "high", name: str = "analyze_execute", **options: str
) -> ProfileConfig:
    return ProfileConfig(
        name=name,
        provider="pi",
        model=MODEL,
        effort=effort,
        command=str(command),
        options=options,
    )


def _execute(
    tmp_path,
    scenario,
    timeout=20,
    idle=None,
    prompt="implement #131",
    options=None,
    env_allowlist=("PATH",),
    abort_seconds=1,
    **kw,
):
    fake = FakePi(tmp_path, scenario)
    provider = PiProvider(round_trip_seconds=5, abort_seconds=abort_seconds, **kw)
    req = AgentRequest(
        phase="ANALYZE_EXECUTE",
        prompt=prompt,
        cwd=str(tmp_path),
        profile=_profile(fake.command, **(options or {})),
        idle_timeout_seconds=idle,
        max_runtime_seconds=timeout,
        env_allowlist=env_allowlist,
    )
    return provider.execute(req), fake


def test_provider_happy_path_returns_the_final_text_byte_for_byte(tmp_path):
    res, fake = _execute(tmp_path, _happy())
    assert res.provider_failure is None and not res.timed_out and res.exit_code == 0
    assert res.stdout == FINAL and res.stdout_tail_offset == 0 and not res.stdout_truncated
    assert parse_control_result(res.stdout_tail, Phase.ANALYZE_EXECUTE) == ANALYZE_OK
    assert fake.commands() == [
        "get_state",
        "get_available_models",
        "prompt",
        "get_last_assistant_text",
    ]
    assert json.loads(fake.stdin()[2])["message"] == "implement #131"
    # The prompt is never in argv: it travelled as the stdin record above.
    assert fake.argv() == [
        "--mode",
        "rpc",
        "--no-session",
        "--no-approve",
        "--no-extensions",
        "--no-skills",
        "--no-prompt-templates",
        "--no-themes",
        "--offline",
        "--tools",
        "read,bash,edit,write",
        "--model",
        MODEL,
        "--thinking",
        "high",
    ]
    # The launch was preceded by one read-only auth check for the same model.
    assert fake.auth_argvs() == [["auth", "check", "--model", MODEL, "--json", "--no-refresh"]]
    assert "implement #131" not in " ".join(res.command)
    assert (res.provider, res.model, res.effort) == ("pi", MODEL, "high")
    assert res.provider_summary["stop_reason"] == "stop"
    assert res.provider_summary["resolved_model_id"] == "gpt-5.6-terra"
    assert not (res.descendants_killed or res.group_survived_kill or res.capture_abandoned)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux-only subreaper")
def test_provider_a_detached_tool_process_is_killed_and_reported(tmp_path):
    """Pi's bash tool detaches every command; what one leaves running after
    Pi exits is caught and killed, and the result is kept (ADR 0002 §4b)."""
    scenario = _happy()
    scenario["on"]["prompt"].insert(1, {"spawn_detached": 60})
    res, fake = _execute(tmp_path, scenario)
    assert res.provider_failure is None and res.stdout == FINAL and res.exit_code == 0
    assert res.orphans_killed and not res.orphan_survived_kill and not res.orphans_unchecked
    assert not res.descendants_killed
    pid = next(e["detached"] for e in fake.entries() if "detached" in e)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_provider_crlf_records_and_raw_u2028_in_strings(tmp_path):
    text = js_trim(block(ANALYZE_OK)) + "\n  tail   end"
    scenario = {**_happy(text), "crlf": True}
    res, _ = _execute(tmp_path, scenario)
    assert res.provider_failure is None and res.stdout == text


def test_provider_stderr_is_never_parsed(tmp_path):
    fake_block = block({**ANALYZE_OK, "pr_title": "from-stderr"})
    noise = '{"type":"response","id":"x","success":true}\n' + fake_block
    scenario = _happy()
    scenario["start"] = [{"stderr": noise}]
    scenario["on"]["prompt"].insert(1, {"stderr": '{"type":"agent_settled"}\n'})
    res, _ = _execute(tmp_path, scenario)
    assert res.provider_failure is None and res.stdout == FINAL
    parsed = parse_control_result(res.stdout, Phase.ANALYZE_EXECUTE)
    assert parsed["pr_title"] == ANALYZE_OK["pr_title"]
    assert "from-stderr" in res.stderr and "from-stderr" not in res.stdout


@pytest.mark.parametrize(
    ("raw", "why"),
    [
        ("Loading extensions...", "a stdout record is not JSON"),
        *(
            pytest.param(
                line.decode(),
                "a stdout record exceeds the JSON decoder's integer or nesting limit",
                id=name,
            )
            for name, line in DECODER_LIMIT_RECORDS.items()
        ),
    ],
)
def test_provider_an_undecodable_stdout_line_is_a_protocol_failure(tmp_path, raw, why):
    scenario = _happy()
    scenario["on"]["prompt"].insert(1, {"raw": raw + "\n"})
    res, fake = _execute(tmp_path, scenario)
    assert res.provider_failure == f"pi: protocol violation: {why}"
    assert res.exit_code == 0 and not res.timed_out and res.stdout == ""
    # The running prompt was aborted, then stdin closed (Pi exited on EOF).
    assert fake.commands()[-1] == "abort"
    assert res.provider_summary["failure"] == res.provider_failure


def test_provider_a_parse_error_response_is_a_protocol_failure(tmp_path):
    scenario = _happy()
    scenario["on"]["get_state"] = [
        {"emit": {"type": "response", "command": "parse", "success": False, "error": "bad"}}
    ]
    res, fake = _execute(tmp_path, scenario)
    assert res.provider_failure == "pi: protocol violation: Pi could not parse a command: bad"
    assert "prompt" not in fake.commands()


def test_provider_an_oversize_record_is_a_protocol_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(providers, "PI_MAX_RECORD_BYTES", 1024)
    monkeypatch.setattr(providers, "PI_MAX_PENDING_BYTES", 4096)
    scenario = _happy()
    scenario["on"]["prompt"].insert(
        1, {"raw": '{"type":"message_update","x":"' + "a" * 5000 + '"}\n'}
    )
    res, _ = _execute(tmp_path, scenario)
    assert res.provider_failure == "pi: protocol violation: a stdout record exceeded 1024 bytes"


def test_provider_a_response_to_an_unknown_id_is_a_protocol_failure(tmp_path):
    scenario = _happy()
    scenario["on"]["get_state"] = [{"respond": "get_state", "data": STATE, "id": "bogus"}]
    res, _ = _execute(tmp_path, scenario)
    assert "a response to an id AutoForge never sent" in res.provider_failure


def test_provider_out_of_order_responses_interleaved_with_events(tmp_path):
    scenario = _happy()
    scenario["on"]["get_state"] = [{"emit": {"type": "queue_update"}}]
    scenario["on"]["get_available_models"] = [
        {"respond": "get_available_models", "data": MODELS},
        {"emit": {"type": "unrelated_event"}},
        {"respond": "get_state", "data": STATE},
    ]
    res, _ = _execute(tmp_path, scenario)
    assert res.provider_failure is None and res.stdout == FINAL
    assert res.provider_summary["unknown_events"] == 1


@pytest.mark.parametrize("code", [0, 1])
def test_provider_an_exit_before_settling_names_the_real_exit_code(tmp_path, code):
    scenario = _happy()
    scenario["on"]["prompt"] = [
        {"respond": "prompt", "data": {"disposition": "started"}},
        {"stderr": "fatal: out of memory\n"},
        {"exit": code},
    ]
    res, _ = _execute(tmp_path, scenario)
    assert res.provider_failure == f"pi: exited before agent_settled (exit {code})"
    assert res.exit_code == code and not res.timed_out
    assert "out of memory" in res.stderr


def test_provider_a_model_mismatch_never_sends_the_prompt(tmp_path):
    scenario = _happy()
    scenario["on"]["get_state"] = [
        {"respond": "get_state", "data": {**STATE, "thinkingLevel": "medium"}}
    ]
    res, fake = _execute(tmp_path, scenario)
    assert res.provider_failure.startswith("pi: thinking mismatch: configured high")
    assert "prompt" not in fake.commands() and "abort" not in fake.commands()
    assert res.exit_code == 0


def test_provider_extension_dialog_is_cancelled_with_the_exact_record(tmp_path):
    scenario = _happy()
    scenario["on"]["prompt"] = [
        {"respond": "prompt", "data": {"disposition": "started"}},
        {
            "emit": {
                "type": "extension_ui_request",
                "id": "ui-7",
                "method": "notify",
                "message": "x",
            }
        },
        {"emit": {"type": "extension_ui_request", "id": "ui-8", "method": "confirm", "title": "?"}},
    ]
    res, fake = _execute(tmp_path, scenario)
    assert '{"type":"extension_ui_response","id":"ui-8","cancelled":true}\n' in fake.stdin()
    assert not any('"ui-7"' in line for line in fake.stdin())  # notify is not answered
    assert res.provider_failure == (
        "pi: an extension asked for input (confirm); the dialog was cancelled"
    )
    assert fake.commands()[-1] == "abort"
    assert res.provider_summary["ui_dialogs_cancelled"] == 1


def test_provider_a_malformed_extension_ui_method_is_a_provider_failure(tmp_path):
    scenario = _happy()
    scenario["on"]["prompt"] = [
        {"respond": "prompt", "data": {"disposition": "started"}},
        {"emit": {"type": "extension_ui_request", "id": "ui-9", "method": ["confirm"]}},
    ]
    res, fake = _execute(tmp_path, scenario)
    assert res.provider_failure == (
        "pi: protocol violation: an extension UI request without a string 'method'"
    )
    assert not any('"ui-9"' in line for line in fake.stdin())
    assert fake.commands()[-1] == "abort"


def test_provider_deadline_mid_stream_aborts_then_closes_stdin(tmp_path):
    scenario = _happy()
    scenario["on"]["prompt"] = [
        {"respond": "prompt", "data": {"disposition": "started"}},
        {"emit": {"type": "agent_start"}},
    ]
    started, launched = time.monotonic(), time.time()
    res, fake = _execute(tmp_path, scenario, timeout=2)
    assert time.monotonic() - started < 10
    assert res.timed_out and res.provider_failure is None and res.stdout == ""
    assert res.exit_code == 0  # Pi settled the abort and exited on stdin EOF
    assert fake.commands()[-1] == "abort"
    # #193: the abort follows the maximum runtime, it does not eat into it.
    assert fake.sent_at("abort") - launched >= 2
    assert res.provider_summary["abort_sent"] is True
    assert res.provider_summary["failure"].startswith("pi: the deadline was reached")
    assert res.timeout_limit == "max_runtime"


def test_provider_a_silent_pi_is_aborted_at_its_idle_timeout(tmp_path):
    """#193: Pi answers, starts the prompt and goes silent. The abort goes out
    once the idle limit, counted from Pi's last record, has fallen due, never
    before it, and the timeout is named as the idle one."""
    scenario = _happy()
    scenario["on"]["prompt"] = [
        {"respond": "prompt", "data": {"disposition": "started"}},
        {"emit": {"type": "agent_start"}},
    ]
    started, launched = time.monotonic(), time.time()
    res, fake = _execute(tmp_path, scenario, timeout=60, idle=2)
    assert time.monotonic() - started < 15
    assert res.timed_out and res.timeout_limit == "idle" and res.stdout == ""
    assert res.last_activity_at is not None
    assert fake.commands()[-1] == "abort"
    assert fake.sent_at("abort") - launched >= 2


def test_provider_a_pi_silent_for_most_of_its_idle_limit_is_not_cut_short(tmp_path):
    """#193 review: the abort window comes after the idle limit, never out of
    it. A Pi that is silent for 3.2s under a 4s idle limit (longer than the
    limit less the abort window) answers in time and its result is kept."""
    scenario = _happy()
    scenario["on"]["prompt"] = [
        {"respond": "prompt", "data": {"disposition": "started"}},
        {"emit": {"type": "agent_start"}},
        {"sleep": 3.2},
        *scenario["on"]["prompt"][2:],
    ]
    res, fake = _execute(tmp_path, scenario, timeout=60, idle=4)
    assert not res.timed_out and res.provider_failure is None and res.stdout == FINAL
    assert res.exit_code == 0 and "abort" not in fake.commands()


def test_provider_a_pi_that_keeps_reporting_outlives_its_idle_timeout(tmp_path):
    scenario = _happy()
    update = {"type": "message_update", "message": {"role": "assistant", "content": []}}
    ticks = [{"sleep": 0.3}, {"emit": update}] * 6
    scenario["on"]["prompt"] = [
        {"respond": "prompt", "data": {"disposition": "started"}},
        *ticks,
        *scenario["on"]["prompt"][1:],
    ]
    started = time.monotonic()
    res, _ = _execute(tmp_path, scenario, timeout=60, idle=1)
    assert time.monotonic() - started >= 1.8
    assert not res.timed_out and res.provider_failure is None and res.stdout == FINAL


def test_provider_a_protocol_failure_shutdown_is_not_stretched_by_output(tmp_path):
    """#193 review: the abort after a protocol failure runs under a pinned
    deadline. A Pi that keeps writing through it and ignores stdin EOF is
    killed one idle limit after its failure plus the abort window, not kept
    alive by its own output (with no ceiling, until the one-week backstop)."""
    scenario = _happy()
    update = {"type": "message_update", "message": {"role": "assistant", "content": []}}
    scenario["on"]["prompt"] = [
        {"respond": "prompt", "data": {"disposition": "started"}},
        {"emit": {"type": "extension_ui_request", "id": "ui-9", "method": ["confirm"]}},
    ]
    scenario["on"]["abort"] = [{"respond": "abort"}] + [{"sleep": 0.1}, {"emit": update}] * 300
    started = time.monotonic()
    res, fake = _execute(tmp_path, scenario, timeout=None, idle=1)
    assert time.monotonic() - started < 1 + 1 + executor._KILL_GRACE_SECONDS + 5
    assert fake.commands()[-1] == "abort"
    assert res.provider_summary["failure"] == (
        "pi: protocol violation: an extension UI request without a string 'method'"
    )
    assert res.timed_out and res.exit_code == -1 and res.timeout_limit == "idle"


def test_provider_a_pi_that_ignores_abort_and_stdin_close_is_killed(tmp_path):
    scenario = _happy()
    scenario["on"]["prompt"] = [{"respond": "prompt", "data": {"disposition": "started"}}]
    scenario["on"]["abort"] = []
    scenario["eof"] = [{"sleep": 60}]
    started = time.monotonic()
    res, fake = _execute(tmp_path, scenario, timeout=2)
    assert time.monotonic() - started < 2 + executor._KILL_GRACE_SECONDS + 5
    assert res.timed_out and res.exit_code == -1
    assert "abort" in fake.commands()


def test_provider_a_pipe_holding_child_is_killed_and_reported(tmp_path, monkeypatch):
    monkeypatch.setattr(executor, "_EXIT_GRACE_SECONDS", 0.5)
    scenario = {**_happy(), "start": [{"spawn_holder": 30}]}
    res, _ = _execute(tmp_path, scenario)
    assert res.provider_failure is None and res.stdout == FINAL and res.exit_code == 0
    assert res.descendants_killed and not res.timed_out


def test_provider_keeps_the_tail_of_a_text_past_the_bound(tmp_path):
    text = "x" * 300 + "é" + FINAL
    res, _ = _execute(tmp_path, _happy(text), max_text_bytes=len(FINAL.encode()) + 1)
    assert res.stdout_truncated and res.stdout_tail_offset == 0
    assert res.stdout == "�" + FINAL  # the cut split the two-byte character
    assert parse_control_result(res.stdout_tail, Phase.ANALYZE_EXECUTE) == ANALYZE_OK


@pytest.mark.parametrize("require_oauth", ["true", "false"])
def test_provider_a_spawn_failure_raises_execution_error(tmp_path, require_oauth):
    provider = PiProvider()
    profile = _profile(tmp_path / "no-such-pi", require_oauth=require_oauth)
    req = AgentRequest("REVIEW", "p", str(tmp_path), profile, None, 5, env_allowlist=("PATH",))
    with pytest.raises(ExecutionError):
        provider.execute(req)


# -- the unattended loading policy (docs/pi-policy.md) ------------------------------------------
_ALWAYS = [
    "--no-approve",
    "--no-extensions",
    "--no-skills",
    "--no-prompt-templates",
    "--no-themes",
    "--offline",
]


@pytest.mark.parametrize(
    ("name", "options", "tools"),
    [
        ("analyze_execute", {}, "read,bash,edit,write"),
        ("fix", {}, "read,bash,edit,write"),
        ("replan_reexecute", {}, "read,bash,edit,write"),
        ("review_round_1", {}, "read,bash"),
        ("review_round_6_plus", {}, "read,bash"),
        ("update_epic", {}, "read,bash"),
        ("default", {}, "read,bash"),
        ("review_round_1", {"tools": "read,bash,grep,find,ls"}, "read,bash,grep,find,ls"),
        ("analyze_execute", {"tools": "read"}, "read"),
    ],
)
def test_argv_always_disables_project_resources_and_names_the_tools(name, options, tools):
    profile = _profile(Path("/opt/pi"), name=name, **options)
    PiProvider().validate_profile(profile)
    argv = PiProvider().build_command_for(profile, "prompt")
    assert argv[:4] == ["/opt/pi", "--mode", "rpc", "--no-session"]
    assert argv[4:10] == _ALWAYS
    assert argv[10:12] == ["--tools", tools]
    assert argv[12:] == ["--model", MODEL, "--thinking", "high"]
    # Nothing that would turn trust or approvals on, or put a key in argv.
    assert not {"--approve", "-a", "--api-key", "--no-context-files"} & set(argv)


def test_context_files_false_adds_no_context_files():
    profile = _profile(Path("/opt/pi"), context_files="false")
    PiProvider().validate_profile(profile)
    argv = PiProvider().build_command_for(profile, "prompt")
    assert argv[4:10] == _ALWAYS and argv[12] == "--no-context-files"
    assert argv[13:] == ["--model", MODEL, "--thinking", "high"]


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"tools": ""}, "no empty entry"),
        ({"tools": "read, bash"}, "no spaces"),
        ({"tools": "read,,bash"}, "no empty entry"),
        ({"tools": "read,bash,"}, "no empty entry"),
        ({"tools": "Read"}, "no spaces"),
        ({"tools": "read,bogus"}, "unknown tool(s) bogus"),
        ({"tools": "read,read"}, "listed twice"),
        ({"context_files": "no"}, "context_files must be true or false"),
    ],
)
def test_invalid_loading_options_are_refused(options, message):
    from autoforge.errors import ConfigurationError

    with pytest.raises(ConfigurationError, match=message.replace("(", r"\(").replace(")", r"\)")):
        PiProvider().validate_profile(_profile(Path("/opt/pi"), **options))


def test_pi_environment_names_exclude_provider_keys_and_unknown_pi_names(tmp_path, monkeypatch):
    for name, value in [
        ("OPENAI_API_KEY", "fake-openai"),
        ("ANTHROPIC_API_KEY", "fake-anthropic"),
        ("PI_FOO", "x"),
        ("PI_PACKAGE_DIR", "/elsewhere"),
        ("PI_CODING_AGENT_DIR", str(tmp_path / "agent")),
    ]:
        monkeypatch.setenv(name, value)
    res, fake = _execute(tmp_path, _happy())
    assert res.provider_failure is None
    seen = set(fake.auth_env())
    assert "PI_CODING_AGENT_DIR" in seen
    assert not {"OPENAI_API_KEY", "ANTHROPIC_API_KEY", "PI_FOO", "PI_PACKAGE_DIR"} & seen


def test_an_allow_listed_api_key_refuses_an_oauth_profile_before_any_spawn(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "fake-openai")
    with pytest.raises(ExecutionError, match="OPENAI_API_KEY is set") as caught:
        _execute(tmp_path, _happy(), env_allowlist=("PATH", "OPENAI_API_KEY"))
    assert "fake-openai" not in str(caught.value)
    assert not (tmp_path / "fake-pi.log").exists()


def test_an_allow_listed_api_key_is_allowed_without_require_oauth(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "fake-openai")
    res, fake = _execute(
        tmp_path,
        _happy(),
        options={"require_oauth": "false"},
        env_allowlist=("PATH", "OPENAI_API_KEY"),
    )
    assert res.provider_failure is None and fake.auth_argvs() == []


@pytest.mark.parametrize(
    ("auth", "auth_exit", "expected"),
    [
        (
            {"status": "ready", "provider": "openai", "authType": "api_key"},
            0,
            "requires Pi's ChatGPT sign-in",
        ),
        (
            {"status": "not_ready", "provider": "openai", "reason": "credentials_not_configured"},
            1,
            "Pi has no credential",
        ),
        (
            {"status": "ready", "provider": "anthropic", "authType": "oauth"},
            0,
            "not the configured 'openai'",
        ),
        ("not json", 1, "without a result AutoForge can read"),
    ],
)
def test_a_refused_auth_preflight_never_launches_pi(tmp_path, auth, auth_exit, expected):
    res, fake = _execute(tmp_path, {**_happy(), "auth": auth, "auth_exit": auth_exit})
    assert res.provider_failure.startswith("pi: the auth preflight refused the launch: ")
    assert expected in res.provider_failure
    assert res.command[1:3] == ["auth", "check"] and res.exit_code == auth_exit
    assert res.stdout == "" and res.provider_summary == {"auth_preflight": "refused"}
    assert not fake.launched()


# -- an installed Pi honours the policy (opt-in) ------------------------------------------------
_PI_E2E_BIN = "AUTOFORGE_PI_E2E_BIN"
_MARKER_EXTENSION = """import { writeFileSync } from "node:fs";
writeFileSync(%s, "loaded");
export default function (pi: any) {
  pi.registerCommand(%s, { description: "probe", handler: async () => {} });
}
"""


def _hostile_project(root: Path) -> dict[str, Path]:
    """A repository that loads everything Pi can load, already trusted, and a
    global agent dir that does the same: every load writes a marker file."""
    paths = {name: root / name for name in ("home", "agent", "repo", "marks", "pkg")}
    for path in paths.values():
        path.mkdir(parents=True)
    agent, repo, marks, pkg = paths["agent"], paths["repo"], paths["marks"], paths["pkg"]
    for directory in (
        ".pi/extensions",
        ".pi/skills/probe-skill",
        ".pi/prompts",
        ".agents/skills/x",
    ):
        (repo / directory).mkdir(parents=True)
    (pkg / "extensions").mkdir()
    (agent / "extensions").mkdir()

    def extension(marker: str, command: str) -> str:
        return _MARKER_EXTENSION % (json.dumps(str(marks / marker)), json.dumps(command))

    def mcp(marker: str) -> str:
        write = f"open({str(marks / marker)!r}, 'w').write('started')"
        return json.dumps(
            {"mcpServers": {marker: {"command": sys.executable, "args": ["-c", write]}}}
        )

    (repo / ".pi/extensions/marker.ts").write_text(extension("project-extension", "probe-cmd"))
    (agent / "extensions/global.ts").write_text(extension("global-extension", "global-cmd"))
    (pkg / "extensions/pkg.ts").write_text(extension("package-extension", "pkg-cmd"))
    (pkg / "package.json").write_text(
        json.dumps(
            {"name": "probe-pkg", "version": "0.0.0", "pi": {"extensions": ["./extensions"]}}
        )
    )
    skill = "---\nname: {0}\ndescription: probe\n---\nbody\n"
    (repo / ".pi/skills/probe-skill/SKILL.md").write_text(skill.format("probe-skill"))
    (repo / ".agents/skills/x/SKILL.md").write_text(skill.format("x"))
    (repo / ".pi/prompts/probe-prompt.md").write_text("---\ndescription: probe\n---\nhello\n")
    (repo / ".pi/settings.json").write_text(json.dumps({"packages": [str(pkg)]}))
    (repo / ".pi/mcp.json").write_text(mcp("project-mcp"))
    (agent / "mcp.json").write_text(mcp("global-mcp"))
    (repo / ".pi/SYSTEM.md").write_text("project system prompt\n")
    # The operator once answered "trust" for this path.
    (agent / "trust.json").write_text(json.dumps({str(repo): True}))
    return paths


_ALL_MARKERS = [
    "global-extension",
    "global-mcp",
    "package-extension",
    "project-extension",
    "project-mcp",
]


def _commands_and_markers(
    argv: list[str], paths: dict[str, Path], settle: float
) -> tuple[list[str], list[str]]:
    """Start Pi, ask ``get_commands`` and ``get_state``, then wait up to
    ``settle`` seconds for every marker (MCP servers connect in the
    background) before closing stdin."""
    import subprocess

    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(paths["home"]),
        "PI_CODING_AGENT_DIR": str(paths["agent"]),
        "LANG": "C.UTF-8",
    }
    proc = subprocess.Popen(
        argv,
        cwd=paths["repo"],
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    try:
        assert proc.stdin is not None and proc.stdout is not None
        for request in ({"id": "c", "type": "get_commands"}, {"id": "s", "type": "get_state"}):
            proc.stdin.write((json.dumps(request) + "\n").encode())
        proc.stdin.flush()
        responses: dict[str, dict] = {}
        while len(responses) < 2:
            line = proc.stdout.readline()
            assert line, "pi exited before answering"
            record = json.loads(line)
            if record.get("type") == "response":
                assert record["success"], record
                responses[record["id"]] = record
        assert responses["s"]["data"]["model"]["id"] == MODEL.split("/", 1)[1]
        deadline = time.monotonic() + settle
        while time.monotonic() < deadline and len(list(paths["marks"].iterdir())) < 5:
            time.sleep(0.1)
        proc.stdin.close()
        assert proc.wait(timeout=60) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    names = sorted(c["name"] for c in responses["c"]["data"]["commands"])
    return names, sorted(p.name for p in paths["marks"].iterdir())


@pytest.mark.skipif(not os.environ.get(_PI_E2E_BIN), reason=f"set {_PI_E2E_BIN} to a pi binary")
def test_an_installed_pi_loads_nothing_from_a_trusted_hostile_project(tmp_path):
    """Real Pi, temp HOME and agent dir, no credential and no model call.

    The same setup first runs under Pi's bare RPC argv, which must load its
    extensions, package, MCP servers, skill and prompt template (otherwise
    this fixture proves nothing), then under the adapter's argv, which must
    load none of them.
    """
    pi = os.environ[_PI_E2E_BIN]
    control = _hostile_project(tmp_path / "control")
    bare = [pi, "--mode", "rpc", "--no-session", "--model", MODEL, "--thinking", "high"]
    names, marks = _commands_and_markers(bare, control, settle=20)
    assert {"probe-cmd", "global-cmd", "pkg-cmd", "probe-prompt", "skill:probe-skill"} <= set(names)
    assert marks == _ALL_MARKERS

    policy = _hostile_project(tmp_path / "policy")
    argv = PiProvider().build_command_for(_profile(Path(pi)), "prompt")
    assert _commands_and_markers(argv, policy, settle=3) == ([], [])


# -- loop detection hooks (#194) --------------------------------------------------
class _LoopRecorder:
    """Records what the reducer reports to a loop monitor, with its clock."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def action(self, name: str, fingerprint: bytes, now: float) -> None:
        self.calls.append(("action", name, fingerprint, now))

    def retry(self, now: float) -> None:
        self.calls.append(("retry", now))

    def turn(self, now: float) -> None:
        self.calls.append(("turn", now))


def _tool(run: Run, call_id: str, args: dict, result: object) -> None:
    run.feed(
        {"type": "tool_execution_start", "toolCallId": call_id, "toolName": "bash", "args": args}
    )
    run.feed(
        {
            "type": "tool_execution_end",
            "toolCallId": call_id,
            "toolName": "bash",
            "result": result,
            "isError": False,
        }
    )


def test_a_tool_execution_is_one_action_fingerprinted_by_args_and_result_not_id():
    loop = _LoopRecorder()
    run = Run(loop=loop).started()
    _tool(run, "c1", {"command": "make test"}, "ok")
    _tool(run, "c2", {"command": "make test"}, "ok")
    _tool(run, "c3", {"command": "make lint"}, "ok")
    _tool(run, "c4", {"command": "make test"}, "failed")
    actions = [call for call in loop.calls if call[0] == "action"]
    assert [(name, now) for _, name, _, now in actions] == [("bash", 100.0)] * 4
    first, same, other_args, other_result = (call[2] for call in actions)
    assert first == same
    assert len({first, other_args, other_result}) == 3


def test_an_end_without_a_start_is_no_action_and_retries_and_turns_are_reported():
    loop = _LoopRecorder()
    run = Run(loop=loop).started()
    run.feed({"type": "tool_execution_end", "toolCallId": "never", "toolName": "bash"})
    run.feed({"type": "auto_retry_start", "attempt": 1, "maxAttempts": 3})
    run.feed({"type": "message_end", "message": {"role": "user", "content": []}})
    run.feed({"type": "message_end", "message": assistant()})
    assert loop.calls == [("retry", 100.0), ("turn", 100.0)]


@pytest.mark.parametrize(
    ("stop", "turn"),
    [("stop", True), ("toolUse", True), ("length", True), ("error", False), ("aborted", False)],
)
def test_only_a_completed_assistant_message_is_a_turn(stop, turn):
    """A failed or aborted attempt is no completed turn: Pi emits one before
    each of its retries (R2-F1)."""
    loop = _LoopRecorder()
    run = Run(loop=loop).started()
    run.feed({"type": "message_end", "message": assistant("", stop=stop)})
    assert loop.calls == ([("turn", 100.0)] if turn else [])


def _retry_monitor(mode: str = "kill"):
    from autoforge.config import LoopDetectionConfig
    from autoforge.loop_detect import LoopMonitor

    config = LoopDetectionConfig(mode=mode, max_cycle_repeats=3, novelty_window_seconds=60)
    return LoopMonitor(config, 100.0)


def _failed_attempt(run: Run, attempt: int) -> None:
    """One failed attempt in Pi's order (verified on Pi 1.0.1): the error
    message, the agent end that will retry, then the retry."""
    run.feed({"type": "message_end", "message": assistant("", stop="error", errorMessage="503")})
    run.feed({"type": "agent_end", "messages": [], "willRetry": True})
    run.feed(
        {
            "type": "auto_retry_start",
            "attempt": attempt,
            "maxAttempts": 10,
            "delayMs": 30000,
            "errorMessage": "503",
        }
    )


def test_failed_attempts_between_retries_do_not_break_a_retry_storm():
    from autoforge.loop_detect import SIGNAL_RETRY_STORM

    monitor = _retry_monitor()
    run = Run(loop=monitor).started()
    for attempt in (1, 2):
        _failed_attempt(run, attempt)
        assert monitor.verdict is None
        run.now += 30
    _failed_attempt(run, 3)
    verdict = monitor.verdict
    assert verdict is not None and verdict.signal == SIGNAL_RETRY_STORM
    assert (verdict.repeats, verdict.started, verdict.at) == (3, 100.0, 160.0)


def test_calibration_counts_every_retry_of_a_failing_streak():
    monitor = _retry_monitor(mode="warn")
    run = Run(loop=monitor).started()
    for attempt in range(1, 9):
        _failed_attempt(run, attempt)
        run.now += 30
    assert monitor.verdict is None
    assert monitor.calibration()["loop_max_retry_streak"] == 8


def test_a_completed_response_between_retries_ends_the_streak():
    monitor = _retry_monitor()
    run = Run(loop=monitor).started()
    for attempt in (1, 2):
        _failed_attempt(run, attempt)
        run.now += 30
    # The retry succeeds: a completed response that calls a tool.
    run.feed({"type": "message_end", "message": assistant(stop="toolUse")})
    run.feed({"type": "auto_retry_end", "success": True, "attempt": 2})
    for attempt in (1, 2):
        run.now += 30
        _failed_attempt(run, attempt)
    assert monitor.verdict is None
    assert monitor.calibration()["loop_max_retry_streak"] == 2


def test_without_a_loop_monitor_no_tool_execution_is_remembered():
    run = Run().started()
    _tool(run, "c1", {"command": "make test"}, "ok")
    run.feed({"type": "tool_execution_start", "toolCallId": "c2", "toolName": "bash"})
    assert run.c._open_tools == {}


def test_provider_a_looping_agent_is_killed_in_kill_mode_as_a_timeout(tmp_path):
    """#194 over the fake ``pi``: the same tool execution (same args, same
    result) eight times ends the invocation at once, before the agent's
    long sleep; the outcome is a timeout with the ``loop`` limit."""
    from autoforge.config import LoopDetectionConfig
    from autoforge.loop_detect import LIMIT_LOOP

    stream: list[dict] = [{"type": "agent_start"}]
    for n in range(12):
        args = {"command": "gh pr checks 5"}
        stream += [
            {
                "type": "tool_execution_start",
                "toolCallId": f"c{n}",
                "toolName": "bash",
                "args": args,
            },
            {
                "type": "tool_execution_end",
                "toolCallId": f"c{n}",
                "toolName": "bash",
                "result": {"content": [{"type": "text", "text": "pending"}]},
                "isError": False,
            },
        ]
    scenario = _happy(events=stream)
    scenario["on"]["prompt"].append({"sleep": 30})
    fake = FakePi(tmp_path, scenario)
    req = AgentRequest(
        phase="FIX",
        prompt="fix it",
        cwd=str(tmp_path),
        profile=_profile(fake.command, name="fix"),
        idle_timeout_seconds=None,
        max_runtime_seconds=60,
        env_allowlist=("PATH",),
        loop_detection=LoopDetectionConfig(mode="kill"),
    )
    started = time.monotonic()
    res = PiProvider(round_trip_seconds=5, abort_seconds=1).execute(req)
    assert time.monotonic() - started < 20
    assert res.timed_out and res.timeout_limit == LIMIT_LOOP and res.provider_failure is None
    assert res.loop is not None and res.loop.action == "killed"
    record = res.loop.record()
    assert (record["signal"], record["period"], record["repeats"]) == ("action_cycle", 1, 8)
    assert record["tools"] == ["bash"]
    assert res.provider_summary["loop_max_cycle_repeats"] == 8
    assert "abort" not in fake.commands()


def test_provider_by_default_a_looping_pi_is_warned_about_not_killed(tmp_path):
    """#199 (R1-F2): no real run has measured Pi yet, so with
    ``execution.loop_detection.mode`` unset the same loop that ``kill`` ends
    is only reported, and the agent runs to its own end."""
    from autoforge.config import LoopDetectionConfig

    stream: list[dict] = [{"type": "agent_start"}]
    for n in range(12):
        args = {"command": "gh pr checks 5"}
        stream += [
            {
                "type": "tool_execution_start",
                "toolCallId": f"c{n}",
                "toolName": "bash",
                "args": args,
            },
            {
                "type": "tool_execution_end",
                "toolCallId": f"c{n}",
                "toolName": "bash",
                "result": {"content": [{"type": "text", "text": "pending"}]},
                "isError": False,
            },
        ]
    stream += [
        {"type": "message_end", "message": assistant(FINAL)},
        {"type": "agent_end", "messages": [], "willRetry": False},
        {"type": "agent_settled"},
    ]
    fake = FakePi(tmp_path, _happy(events=stream))
    req = AgentRequest(
        phase="FIX",
        prompt="fix it",
        cwd=str(tmp_path),
        profile=_profile(fake.command, name="fix"),
        idle_timeout_seconds=None,
        max_runtime_seconds=60,
        env_allowlist=("PATH",),
        loop_detection=LoopDetectionConfig(),
    )
    assert req.loop_detection.mode == "warn"
    res = PiProvider(round_trip_seconds=5, abort_seconds=1).execute(req)
    assert not res.timed_out and not res.timeout_limit and res.provider_failure is None
    assert res.exit_code == 0 and FINAL in res.stdout
    assert res.loop is not None and res.loop.action == "warned"
    assert res.provider_summary["loop_max_cycle_repeats"] == 12
