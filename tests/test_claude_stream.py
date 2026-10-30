"""Claude stream-json reducer (#192): synthetic lines in, outcome and progress out.

No process runs here: :class:`ClaudeStream` is fed bytes the way the
provider's duplex driver feeds it. The record shapes follow the stream
verified against claude 2.1.292, and its runs of several turns against
2.1.293 (module docstring).
"""

import json

import pytest

from autoforge import claude_stream
from autoforge.claude_stream import ClaudeStream
from autoforge.interaction import RequestKind
from autoforge.progress import ProgressEvent, ProgressKind
from tests.conftest import DECODER_LIMIT_RECORDS

CWD = "/work/tree"
TEXT = 'done\n<<<CONTROL_RESULT>>>\n{"phase":"FIX","status":"success"}\n<<<END_CONTROL_RESULT>>>\n'


def _line(record: dict) -> bytes:
    return json.dumps(record).encode()


def _result(**over) -> dict:
    record = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "terminal_reason": "completed",
        "stop_reason": "end_turn",
        "num_turns": 7,
        "duration_ms": 61_234,
        "total_cost_usd": 0.02487855,
        "result": TEXT,
        "permission_denials": [{"tool_name": "Bash"}],
        "usage": {"input_tokens": 10, "output_tokens": 20},
        "modelUsage": {"claude-fable-5-1": {"costUSD": 0.02}},
        "session_id": "abc",
    }
    record.update(over)
    return record


def _assistant(*blocks: dict) -> dict:
    return {"type": "assistant", "message": {"content": list(blocks)}}


def _tool_use(tool_id: str, name: str, **tool_input) -> dict:
    return {"type": "tool_use", "id": tool_id, "name": name, "input": tool_input}


def _tool_result(tool_id: str, **extra) -> dict:
    block = {"type": "tool_result", "tool_use_id": tool_id, "content": "file contents"}
    block.update(extra)
    return {"type": "user", "message": {"content": [block]}}


def _run(*records: dict | bytes, ended: bool = True) -> tuple[ClaudeStream, list[ProgressEvent]]:
    events: list[ProgressEvent] = []
    stream = ClaudeStream(cwd=CWD, emit=events.append)
    for record in records:
        stream.feed(record if isinstance(record, bytes) else _line(record))
    if ended:
        stream.stream_ended()
    return stream, events


def test_the_result_text_is_the_final_text_verbatim():
    stream, _ = _run({"type": "system", "subtype": "init", "model": "claude-fable-5-1"}, _result())
    assert stream.failure is None
    assert stream.text == TEXT
    assert not stream.exited_early


def test_the_summary_is_flat_bounded_scalars_with_integer_micro_usd():
    stream, _ = _run(_result())
    summary = stream.summary()
    assert summary["cost_micro_usd"] == 24_879  # 0.02487855 USD, rounded half up
    assert summary["num_turns"] == 7 and summary["duration_ms"] == 61_234
    assert summary["terminal_reason"] == "completed" and summary["result_subtype"] == "success"
    assert summary["permission_denials"] == 1
    assert summary["is_error"] is False and summary["failure"] == ""
    # No float, no nested usage, no session id: scalars only.
    assert all(type(v) in (str, int, bool) for v in summary.values())
    assert not {"usage", "modelUsage", "total_cost_usd", "session_id"} & set(summary)


@pytest.mark.parametrize(
    ("cost", "kept"),
    [
        (0, 0),
        (1.5, 1_500_000),
        (-0.1, None),
        (float("nan"), None),
        (float("inf"), None),
        (True, None),
        ("0.5", None),
        (10**7, None),
    ],
)
def test_an_unreadable_cost_is_left_out_rather_than_reported_as_zero(cost, kept):
    stream, _ = _run(_result(total_cost_usd=cost))
    assert stream.summary().get("cost_micro_usd") == kept


def test_is_error_is_checked_before_the_subtype():
    # #147: a success subtype can come with is_error true.
    stream, _ = _run(_result(is_error=True, api_error_status=529, result="Overloaded"))
    assert stream.text is None
    assert stream.failure == (
        "claude: the run ended in an error (subtype success, terminal_reason completed, "
        "api_error_status 529): Overloaded"
    )


SERVER_SIDE = (
    "This is a server-side issue, usually temporary — try again in a moment. If it persists, "
    "check your inference gateway (127.0.0.1:40147)."
)


def _api_error(status: object, message: str, **over) -> dict:
    """How claude 2.1.314 ends a turn on a model API error once its own
    retries are spent (#395; run against a loopback stand-in of the Messages
    API): its message as the result, ``terminal_reason: api_error``, and the
    HTTP status, ``None`` when the request got no answer."""
    record = _result(
        is_error=True,
        terminal_reason="api_error",
        stop_reason="stop_sequence",
        num_turns=1,
        api_error_status=status,
        result=message,
        permission_denials=[],
    )
    record.update(over)
    return record


@pytest.mark.parametrize(
    ("status", "message"),
    [
        (529, f"API Error: 529 overloaded_error message. {SERVER_SIDE}"),
        (500, f"API Error: 500 api_error message. {SERVER_SIDE}"),
        (502, f"API Error: 502 api_error message. {SERVER_SIDE}"),
        (503, f"API Error: 503 api_error message. {SERVER_SIDE}"),
        (504, f"API Error: 504 api_error message. {SERVER_SIDE}"),
        (408, "API Error: 408 timeout_error message"),
    ],
)
def test_a_turn_ended_by_a_server_error_or_a_request_timeout_is_transient(status, message):
    """#395: the failure is the one it always was; the stream adds a fixed
    reason naming the status, never the message."""
    stream, _ = _run(_api_error(status, message))
    assert stream.text is None
    assert stream.failure == (
        "claude: the run ended in an error (subtype success, terminal_reason api_error, "
        f"api_error_status {status}): {message}"
    )
    assert stream.transient == f"claude: API error {status}"


@pytest.mark.parametrize(
    "record",
    [
        pytest.param(_api_error(400, "API Error: 400 invalid_request_error message"), id="400"),
        pytest.param(
            _api_error(401, "Failed to authenticate. API Error: 401 authentication_error message"),
            id="401",
        ),
        pytest.param(
            _api_error(403, "Failed to authenticate. API Error: 403 permission_error message"),
            id="403",
        ),
        pytest.param(
            _api_error(404, "There's an issue with the selected model (claude-sonnet-5-5)."),
            id="404",
        ),
        pytest.param(
            _api_error(413, "Request too large (max 32MB).", terminal_reason="image_error"),
            id="413",
        ),
        pytest.param(
            _api_error(429, "API Error: Request rejected (429) · rate_limit_error message"),
            id="429",
        ),
        pytest.param(
            _api_error(
                None,
                "API Error: Connection refused — a firewall or proxy may be blocking it "
                "(ECONNREFUSED)",
                api_error="connection_failed",
            ),
            id="connection-refused",
        ),
        pytest.param(
            _api_error(
                None,
                "API Error: Connection dropped (ECONNRESET). Check the gateway set in "
                "ANTHROPIC_BASE_URL, then try again.",
                api_error="connection_failed",
            ),
            id="connection-dropped",
        ),
        pytest.param(_api_error(None, "Request timed out"), id="client-timeout"),
        pytest.param(
            _api_error(
                529,
                f"API Error: 529 overloaded_error message. {SERVER_SIDE}",
                api_error_status=None,
            ),
            id="status-only-in-the-text",
        ),
        pytest.param(
            _api_error(529, "Overloaded", terminal_reason="completed"), id="not-an-api-error-turn"
        ),
        pytest.param(_result(is_error=True, api_error_status=529, result="x"), id="no-reason"),
        pytest.param(_api_error("529", "Overloaded"), id="status-a-string"),
        pytest.param(_api_error(True, "Overloaded"), id="status-a-boolean"),
    ],
)
def test_no_other_error_result_is_transient(record):
    """#395: a 4xx other than 408, 429 included, an error without a status
    and an error result that did not end its turn on an API error fail the
    run and are not transient, whatever their text says."""
    stream, _ = _run(record)
    assert stream.failure is not None and stream.text is None
    assert stream.transient is None


def test_a_transient_api_error_in_a_later_turn_is_transient():
    init = {"type": "system", "subtype": "init", "model": "claude-fable-5-1"}
    stream, _ = _run(_result(), init, _api_error(529, "Overloaded"))
    assert stream.summary()["results"] == 2
    assert stream.failure is not None and stream.transient == "claude: API error 529"


def test_a_transient_api_error_after_the_run_failed_otherwise_is_not_transient():
    """The first failure is the run's: a later API error is not read."""
    for first in (b"not json", _line(_result(subtype="error_max_turns"))):
        stream, _ = _run(first, _api_error(529, "Overloaded"))
        assert stream.failure is not None and "529" not in stream.failure
        assert stream.transient is None


def test_a_run_that_succeeds_or_ends_without_a_result_is_not_transient():
    assert _run(_result())[0].transient is None
    stream, _ = _run({"type": "system", "subtype": "api_retry", "attempt": 10, "max_retries": 10})
    assert stream.failure == "claude: exited without a result event"
    assert stream.transient is None


def test_a_subtype_other_than_success_is_a_failure():
    stream, _ = _run(_result(subtype="error_max_turns", result=TEXT))
    assert stream.text is None
    assert stream.failure == "claude: the run did not succeed (subtype error_max_turns)"


@pytest.mark.parametrize(
    ("over", "why"),
    [
        ({"is_error": None}, "no boolean 'is_error'"),
        ({"is_error": "false"}, "no boolean 'is_error'"),
        ({"result": None}, "no string 'result'"),
        ({"result": ["x"]}, "no string 'result'"),
    ],
)
def test_a_malformed_result_event_is_a_protocol_failure(over, why):
    stream, _ = _run(_result(**over))
    assert stream.text is None
    assert stream.failure == f"claude: stream-json protocol violation: the result event has {why}"


@pytest.mark.parametrize(
    ("line", "why"),
    [
        (b"not json", "a stdout line is not JSON"),
        (b"\xff\xfe", "a stdout line is not JSON"),
        (b"[1, 2]", "a stdout line is not a JSON object with a 'type'"),
        (b'{"type": 3}', "a stdout line is not a JSON object with a 'type'"),
    ],
)
def test_a_malformed_line_fails_the_run_even_before_a_good_result(line, why):
    stream, _ = _run(line, _result())
    assert stream.text is None
    assert stream.failure == f"claude: stream-json protocol violation: {why}"


@pytest.mark.parametrize("line", DECODER_LIMIT_RECORDS.values(), ids=DECODER_LIMIT_RECORDS.keys())
def test_a_line_past_the_decoders_limits_is_a_protocol_failure_not_an_exception(line):
    stream, _ = _run(line, _result())
    assert stream.text is None
    assert stream.failure == (
        "claude: stream-json protocol violation: "
        "a stdout line exceeds the JSON decoder's integer or nesting limit"
    )
    assert stream.summary()["failure"] == stream.failure and stream.summary()["records"] == 2


def test_an_interrupt_inside_the_decoder_is_not_taken_for_a_bad_line(monkeypatch):
    def interrupted(text: str) -> object:
        raise KeyboardInterrupt

    monkeypatch.setattr(claude_stream.json, "loads", interrupted)
    stream = ClaudeStream()
    with pytest.raises(KeyboardInterrupt):
        stream.feed(b'{"type": "system"}')
    assert stream.failure is None


def test_no_result_before_stdout_ends_is_a_failure():
    stream, _ = _run({"type": "system", "subtype": "init", "model": "m"})
    assert stream.exited_early
    assert stream.text is None
    assert stream.failure == "claude: exited without a result event"


def _later_turn(text: str, **over) -> tuple[dict, ...]:
    """A turn the CLI runs after a result: a finished background task's
    notice, then the turn itself (verified on claude 2.1.293)."""
    return (
        {"type": "system", "subtype": "task_notification"},
        {"type": "system", "subtype": "init", "model": "claude-fable-5-1"},
        _assistant({"type": "text", "text": text}),
        _result(result=text, **over),
    )


def test_each_turn_ends_in_a_result_and_the_last_one_is_the_outcome():
    """A background task or a scheduled wakeup still pending when a turn
    ends keeps the CLI alive for another turn with its own result; text
    output prints only the last result's text, and so does the reducer."""
    stream, events = _run(
        _result(result="waiting", num_turns=5, duration_ms=1_000, total_cost_usd=0.5),
        *_later_turn("checked", num_turns=2, duration_ms=200, total_cost_usd=0.75),
        {"type": "command_lifecycle", "state": "started"},
        *_later_turn(TEXT, num_turns=1, duration_ms=30, total_cost_usd=0.8),
        {"type": "command_lifecycle", "state": "completed"},
    )
    assert stream.failure is None and not stream.exited_early
    assert stream.text == TEXT
    summary = stream.summary()
    assert summary["results"] == 3
    # Turns and duration are per result; the cost is the session's so far.
    assert summary["num_turns"] == 8 and summary["duration_ms"] == 1_230
    assert summary["cost_micro_usd"] == 800_000
    # Notices between turns and after the last one belong to no turn.
    assert summary["records_after_result"] == 4
    # Each later turn is progress like the first.
    assert [e.kind for e in events].count(ProgressKind.STARTED) == 2
    assert [e.kind for e in events].count(ProgressKind.ASSISTANT_TEXT) == 2


_ABSENT = object()


@pytest.mark.parametrize("lacking", [0, 1, 2], ids=["first", "middle", "last"])
@pytest.mark.parametrize(
    "value",
    [_ABSENT, None, -1, True, "7", 10**16],
    ids=["absent", "null", "negative", "bool", "string", "past-bound"],
)
def test_a_sum_is_left_out_once_one_result_lacks_its_count(lacking, value):
    """``num_turns`` is per result: once one result lacks it, absent or
    unreadable, what the others add up to is not the invocation's total,
    and a later result that has it does not bring the sum back."""
    turns = [(_result(result="waiting"),), _later_turn("checked"), _later_turn(TEXT)]
    result = turns[lacking][-1]
    if value is _ABSENT:
        del result["num_turns"]
    else:
        result["num_turns"] = value
    stream, _ = _run(*(record for turn in turns for record in turn))
    assert stream.failure is None and stream.text == TEXT
    summary = stream.summary()
    assert "num_turns" not in summary
    # The other count is in every result, and is still summed.
    assert summary["duration_ms"] == 3 * 61_234 and summary["results"] == 3


def test_an_error_result_in_an_earlier_turn_fails_the_run():
    stream, _ = _run(_result(is_error=True, result="Overloaded"), *_later_turn(TEXT))
    assert stream.text is None
    assert stream.failure == (
        "claude: the run ended in an error (subtype success, terminal_reason completed): Overloaded"
    )
    assert stream.summary()["results"] == 1


def test_an_error_result_in_a_later_turn_fails_the_run():
    stream, _ = _run(_result(), *_later_turn("", subtype="error_during_execution"))
    assert stream.text is None
    assert stream.failure == "claude: the run did not succeed (subtype error_during_execution)"


@pytest.mark.parametrize(
    "between",
    [
        (),
        ({"type": "system", "subtype": "task_notification"},),
        ({"type": "command_lifecycle", "state": "started"},),
    ],
)
def test_a_result_outside_a_turn_is_a_failure(between):
    """A result ends a turn; only a ``system/init``, ``assistant`` or
    ``user`` record opens the next one (claude 2.1.293 opens each with
    ``system/init``). A result with no turn since the last one, notices
    between turns aside, ends no turn, and its text is never the outcome."""
    stream, _ = _run(_result(), *between, _result(result="other"))
    assert stream.text is None
    assert stream.failure == "claude: stream-json protocol violation: a result event outside a turn"
    assert stream.summary()["results"] == 1


@pytest.mark.parametrize(
    "opens",
    [
        {"type": "system", "subtype": "init", "model": "m"},
        _assistant({"type": "text", "text": "more"}),
        _tool_result("t1"),
    ],
)
def test_stdout_ending_inside_a_later_turn_is_a_failure(opens):
    stream, _ = _run(_result(), {"type": "system", "subtype": "task_notification"}, opens)
    assert stream.exited_early
    assert stream.text is None
    assert stream.failure == "claude: exited inside a turn that has no result event"


_INIT = {"type": "system", "subtype": "init", "model": "claude-fable-5-1"}


def _held_turn(text: str) -> tuple[dict, ...]:
    """A turn the CLI runs while a background agent is still running, with
    its result held back (verified on claude 2.1.293): a finished task's
    notice, then the turn up to its last text, and no result."""
    return (
        {"type": "system", "subtype": "task_notification"},
        _INIT,
        _assistant({"type": "text", "text": text}),
    )


def _held_run(results: tuple[dict, ...]) -> tuple[ClaudeStream, list[ProgressEvent]]:
    """Three turns whose results the CLI holds back while a background agent
    runs, then ``results``: once no background agent is left, the held
    results arrive together, in turn order, after the last turn's records
    (verified on claude 2.1.293, ``result_index`` 0, 1, 2)."""
    return _run(
        _INIT,
        _assistant(_tool_use("a1", "Agent", description="Migrate the tests")),
        {"type": "system", "subtype": "task_started", "task_id": "a"},
        _tool_result("a1"),
        # The background agent's own records name the call that started it.
        {
            **_assistant(_tool_use("s1", "Bash", description="Run the tests")),
            "parent_tool_use_id": "a1",
        },
        _assistant({"type": "text", "text": "waiting"}),
        {**_tool_result("s1"), "parent_tool_use_id": "a1"},
        *_held_turn("checked"),
        *_held_turn(TEXT),
        *results,
    )


def test_results_held_back_until_the_last_turn_end_their_turns_in_order():
    """Background agents still running when a turn ends make the CLI hold
    that turn's result back; each later turn opens with its own
    ``system/init`` and the results follow the last turn, in order. Text
    output prints only the last one's text, and so does the reducer."""
    stream, events = _held_run(
        (
            _result(result="waiting", result_index=0, num_turns=5, duration_ms=1_000),
            _result(result="checked", result_index=1, num_turns=2, duration_ms=200),
            _result(result=TEXT, result_index=2, num_turns=1, duration_ms=30),
        )
    )
    assert stream.failure is None and not stream.exited_early
    assert stream.text == TEXT
    summary = stream.summary()
    assert summary["results"] == 3 and summary["records_after_result"] == 0
    assert summary["num_turns"] == 8 and summary["duration_ms"] == 1_230
    assert [e.kind for e in events].count(ProgressKind.STARTED) == 3


def test_a_result_sent_at_once_and_results_held_back_mix_in_one_run():
    """A turn that ends with no background agent running gets its result at
    once; later turns that end while one runs get theirs at the end."""
    stream, _ = _run(
        _INIT,
        _result(result="waiting"),
        *_held_turn("checked"),
        *_held_turn(TEXT),
        _result(result="checked"),
        _result(result=TEXT),
    )
    assert stream.failure is None and stream.text == TEXT
    assert stream.summary()["results"] == 3


@pytest.mark.parametrize(
    ("records", "turns", "results", "open_calls"),
    [
        # Inside a foreground tool call: its result has not arrived.
        ((_INIT, _assistant(_tool_use("t1", "Bash", description="Run the suite"))), 1, 0, 1),
        # Between turns: the turn ended on a background task's start, and
        # the CLI waits for it (#343's five Claude idle kills).
        (
            (
                _INIT,
                _assistant(_tool_use("t1", "Bash", run_in_background=True)),
                _tool_result("t1", content="Command running in background with ID: b1"),
                _result(result="The suite is running; I'll report when it finishes."),
            ),
            1,
            1,
            0,
        ),
        # A later turn, opened by the task's notice, ended too.
        (
            (
                _INIT,
                _result(result="waiting"),
                {"type": "system", "subtype": "task_notification"},
                _INIT,
                _assistant(_tool_use("t2", "Read", file_path=f"{CWD}/a.py")),
                _tool_result("t2"),
                _result(),
            ),
            2,
            2,
            0,
        ),
    ],
    ids=["inside-a-tool-call", "between-turns", "two-turns-closed"],
)
def test_the_summary_says_where_a_stream_stood_when_it_ended(records, turns, results, open_calls):
    """A run killed while its stream was silent left no record of why; the
    turns opened, the results and the tool calls still open tell a CLI
    inside a tool call from one kept alive between turns (#343)."""
    stream, _ = _run(*records, ended=False)
    summary = stream.summary()
    assert (summary["turns"], summary["results"], summary["open_tool_calls"]) == (
        turns,
        results,
        open_calls,
    )


def test_stdout_ending_before_every_held_result_arrived_is_a_failure():
    """The text of an earlier turn is never the outcome: a run whose last
    turn has no result failed, however many results arrived."""
    stream, _ = _held_run((_result(result="waiting"), _result(result="checked")))
    assert stream.exited_early
    assert stream.text is None
    assert stream.failure == "claude: exited inside a turn that has no result event"
    assert stream.summary()["results"] == 2


def test_a_held_result_past_the_last_turn_is_a_failure():
    stream, _ = _held_run(
        (
            _result(result="waiting"),
            _result(result="checked"),
            _result(result=TEXT),
            _result(result="other"),
        )
    )
    assert stream.text is None
    assert stream.failure == "claude: stream-json protocol violation: a result event outside a turn"
    assert stream.summary()["results"] == 3


def test_an_error_in_a_held_result_fails_the_run():
    stream, _ = _held_run(
        (
            _result(result="waiting"),
            _result(is_error=True, result="Overloaded"),
            _result(result=TEXT),
        )
    )
    assert stream.text is None
    assert stream.failure == (
        "claude: the run ended in an error (subtype success, terminal_reason completed): Overloaded"
    )


def test_a_turn_opened_before_its_init_is_one_turn():
    """After a result, an ``assistant`` record opens the next turn; the
    ``system/init`` that follows is that turn's own, not one more."""
    stream, _ = _run(
        _INIT,
        _result(result="waiting"),
        _assistant({"type": "text", "text": "more"}),
        *_held_turn(TEXT),
        _result(result=TEXT),
    )
    assert stream.failure is None and stream.text == TEXT
    assert stream.summary()["results"] == 2


def test_records_after_the_result_are_counted_and_ignored():
    stream, events = _run(_result(), {"type": "system", "subtype": "hook_response"})
    assert stream.failure is None and stream.text == TEXT
    assert stream.summary()["records_after_result"] == 1
    assert stream.summary()["results"] == 1
    assert events == []


def test_an_oversize_ordinary_line_is_counted_not_fatal():
    events: list[ProgressEvent] = []
    stream = ClaudeStream(cwd=CWD, emit=events.append)
    stream.oversize(1024)
    stream.feed(_line(_result()))
    stream.stream_ended()
    assert stream.failure is None and stream.text == TEXT
    assert stream.summary()["oversize_records"] == 1
    assert [e.kind for e in events] == [ProgressKind.ACTIVITY]


def test_an_oversize_result_line_fails_like_a_missing_result():
    # The result line has no allowance: skipped, the stream ends without one.
    stream = ClaudeStream(cwd=CWD)
    stream.oversize(1024)
    stream.stream_ended()
    assert stream.exited_early and stream.text is None
    assert stream.failure == (
        "claude: exited without a result event (1 line(s) past the per-line bound were "
        "skipped; the result must arrive whole in one line)"
    )


def test_a_framing_error_is_a_protocol_failure():
    stream = ClaudeStream()
    stream.framing_error("stdout ended inside an unterminated line")
    stream.stream_ended()
    assert not stream.exited_early
    assert stream.failure == (
        "claude: stream-json protocol violation: stdout ended inside an unterminated line"
    )


def test_tools_map_to_progress_with_one_allow_listed_input():
    stream, events = _run(
        {"type": "system", "subtype": "init", "model": "claude-fable-5-1"},
        {"type": "system", "subtype": "thinking_tokens", "estimated_tokens": 12_345},
        _assistant({"type": "thinking", "thinking": "secret plan"}),
        _assistant(_tool_use("t1", "Read", file_path=f"{CWD}/src/a.py")),
        _tool_result("t1"),
        _assistant(_tool_use("t2", "Grep", pattern="def main", path=CWD)),
        _tool_result("t2", is_error=True),
        _assistant(
            _tool_use("t3", "Bash", command="curl -H 'x' https://h", description="Fetch it")
        ),
        _assistant(_tool_use("t4", "WebFetch", url="https://example.com/?q=1")),
        _assistant({"type": "text", "text": "Here is what I did"}),
        {"type": "system", "subtype": "api_retry", "attempt": 2, "max_retries": 10},
        {"type": "rate_limit_event", "rate_limit_info": {}},
        {"type": "system", "subtype": "hook_started", "output": "hook says hi"},
        {"type": "brand_new_event"},
        _result(),
    )
    got = [(e.kind, e.tool, e.detail, e.tokens) for e in events]
    assert got == [
        (ProgressKind.STARTED, "", "claude-fable-5-1", 0),
        (ProgressKind.THINKING, "", "", 12_345),
        (ProgressKind.THINKING, "", "", 0),
        (ProgressKind.TOOL_STARTED, "Read", "src/a.py", 0),
        (ProgressKind.TOOL_FINISHED, "Read", "", 0),
        (ProgressKind.TOOL_STARTED, "Grep", "def main", 0),
        (ProgressKind.TOOL_FAILED, "Grep", "", 0),
        (ProgressKind.TOOL_STARTED, "Bash", "Fetch it", 0),
        (ProgressKind.TOOL_STARTED, "WebFetch", "", 0),
        (ProgressKind.ASSISTANT_TEXT, "", "", 0),
        (ProgressKind.PROVIDER_RETRY, "", "attempt 2 of 10", 0),
        (ProgressKind.ACTIVITY, "", "", 0),
        (ProgressKind.ACTIVITY, "", "", 0),
        (ProgressKind.ACTIVITY, "", "", 0),
    ]
    summary = stream.summary()
    assert summary["tool_calls"] == 4 and summary["tool_errors"] == 1
    assert summary["api_retries"] == 1 and summary["unknown_events"] == 1


def test_no_command_thinking_text_or_tool_result_reaches_an_event():
    stream, events = _run(
        _assistant({"type": "thinking", "thinking": "THINKING-SENTINEL"}),
        _assistant({"type": "text", "text": "TEXT-SENTINEL"}),
        _assistant(_tool_use("t1", "Bash", command="echo COMMAND-SENTINEL")),
        _tool_result("t1", content="RESULT-SENTINEL"),
        {"type": "system", "subtype": "hook_response", "output": "HOOK-SENTINEL"},
        _result(),
    )
    shown = " ".join(f"{e.tool} {e.detail}" for e in events)
    assert "SENTINEL" not in shown
    assert "Bash" in shown
    assert "SENTINEL" not in json.dumps(stream.summary())


def test_a_path_outside_the_worktree_is_shown_as_given():
    _, events = _run(_assistant(_tool_use("t1", "Edit", file_path="/etc/hosts")), _result())
    assert events[0].detail == "/etc/hosts"


def test_a_failing_sink_is_the_drivers_concern_not_the_reducers():
    # The provider wraps the sink in ``guarded``; the reducer itself calls
    # whatever it is given, so an unguarded raising sink propagates.
    def boom(event):
        raise RuntimeError("sink")

    stream = ClaudeStream(emit=boom)
    with pytest.raises(RuntimeError):
        stream.feed(_line({"type": "rate_limit_event"}))


# -- loop detection hooks (#194) --------------------------------------------------
class _Observer:
    """Records what the reducer reports to a loop detector."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def action(self, name: str, fingerprint: bytes) -> None:
        self.calls.append(("action", name, fingerprint))

    def retry(self) -> None:
        self.calls.append(("retry",))

    def turn(self) -> None:
        self.calls.append(("turn",))


def _observed(*records: dict) -> list[tuple]:
    observer = _Observer()
    stream = ClaudeStream(cwd=CWD, emit=lambda event: None, loop=observer)
    for record in records:
        stream.feed(_line(record))
    return observer.calls


def test_a_completed_call_is_one_action_fingerprinted_by_input_and_result_not_id():
    calls = _observed(
        _assistant(_tool_use("t1", "Bash", command="make test")),
        _tool_result("t1"),
        _assistant(_tool_use("t2", "Bash", command="make test")),
        _tool_result("t2"),
        _assistant(_tool_use("t3", "Bash", command="make lint")),
        _tool_result("t3"),
        _assistant(_tool_use("t4", "Bash", command="make test")),
        _tool_result("t4", content="other output"),
    )
    actions = [call for call in calls if call[0] == "action"]
    assert [name for _, name, _ in actions] == ["Bash"] * 4
    first, same, other_input, other_result = (fingerprint for *_, fingerprint in actions)
    assert first == same
    assert len({first, other_input, other_result}) == 3
    assert calls.count(("turn",)) == 4


def test_a_result_for_an_unknown_call_is_no_action():
    assert _observed(_tool_result("never-started")) == []


def test_an_api_retry_is_reported_and_an_assistant_message_ends_a_turn():
    calls = _observed(
        {"type": "system", "subtype": "api_retry", "attempt": 1, "max_retries": 10},
        _assistant({"type": "text", "text": "hi"}),
    )
    assert calls == [("retry",), ("turn",)]


def test_without_a_loop_observer_inputs_are_not_digested():
    stream = ClaudeStream(cwd=CWD, emit=lambda event: None)
    stream.feed(_line(_assistant(_tool_use("t1", "Bash", command="make test"))))
    assert stream._open_tools == {"t1": ("Bash", b"", "")}


# -- requests the CLI refused (#258) ---------------------------------------------------
def _denied(tool_id: str, name: str, **extra) -> dict:
    """claude 2.1.312's record of a tool call it refused."""
    record = {"type": "system", "subtype": "permission_denied", "tool_name": name}
    return {**record, "tool_use_id": tool_id, "message": "needs approval.", **extra}


def test_a_refused_call_is_kept_once_with_its_tool_and_its_allow_listed_input():
    """As 2.1.312 streams a refusal: its record, an error tool result, and
    the call again in the result's ``permission_denials``. One request each;
    a Bash call shows its description, never its command."""
    denials = [
        {"tool_name": "Bash", "tool_use_id": "t1", "tool_input": {"command": "touch x"}},
        {"tool_name": "Write", "tool_use_id": "t2", "tool_input": {"file_path": f"{CWD}/a.py"}},
    ]
    stream, events = _run(
        _assistant(_tool_use("t1", "Bash", command="touch x", description="Create the file")),
        _denied("t1", "Bash"),
        _tool_result("t1", is_error=True),
        _assistant(_tool_use("t2", "Write", file_path=f"{CWD}/a.py", content="x")),
        _denied("t2", "Write", decision_reason_type="mode"),
        _tool_result("t2", is_error=True),
        _result(permission_denials=denials),
    )
    assert stream.failure is None and stream.text == TEXT
    assert stream.refused == [
        (RequestKind.PERMISSION, "Bash: Create the file"),
        (RequestKind.PERMISSION, "Write: a.py"),
    ]
    assert stream.refused_count == 2
    assert "touch" not in repr(stream.refused)
    # The refusal record is liveness only.
    assert [e.kind for e in events].count(ProgressKind.ACTIVITY) == 2


def test_a_refusal_is_read_from_the_result_alone_and_from_its_record_alone():
    """A stream with no ``permission_denied`` record (the field is older than
    it) names the call from its listed input; a run that ends before its
    result names it from the record."""
    listed = [{"tool_name": "Read", "tool_use_id": "t9", "tool_input": {"file_path": "/etc/x"}}]
    stream, _ = _run(_result(permission_denials=listed))
    assert stream.refused == [(RequestKind.PERMISSION, "Read: /etc/x")]
    stream, _ = _run(
        _assistant(_tool_use("t1", "Edit", file_path=f"{CWD}/b.py")), _denied("t1", "Edit")
    )
    assert stream.failure is not None and stream.refused == [(RequestKind.PERMISSION, "Edit: b.py")]


def test_a_question_is_refused_only_when_its_call_failed():
    stream, _ = _run(
        _assistant(_tool_use("q1", "AskUserQuestion", questions=[{"question": "Which?"}])),
        _tool_result("q1", is_error=True, content="No such tool available"),
        _assistant(_tool_use("q2", "AskUserQuestion", questions=[])),
        _tool_result("q2"),
        _result(permission_denials=[]),
    )
    assert stream.refused == [(RequestKind.QUESTION, "")] and stream.refused_count == 1


def test_refused_subjects_are_cleaned_and_bounded_and_their_number_is_not():
    token = "ghp_" + "a" * 36
    many = [
        {"tool_name": "Bash", "tool_use_id": f"t{i}", "tool_input": {"description": "x" * 500}}
        for i in range(claude_stream.MAX_REFUSED + 6)
    ]
    stream, _ = _run(
        _assistant(_tool_use("s", "Bash", description=f"\x1b[31mDeploy {token}\x1b[0m")),
        _denied("s", "Bash"),
        _result(permission_denials=many),
    )
    assert stream.refused[0] == (RequestKind.PERMISSION, "Bash: Deploy ***REDACTED***")
    assert len(stream.refused) == claude_stream.MAX_REFUSED
    assert stream.refused_count == claude_stream.MAX_REFUSED + 7
    assert all(len(s) <= claude_stream.MAX_SUBJECT_CHARS for _, s in stream.refused)
