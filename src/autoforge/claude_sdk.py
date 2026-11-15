"""The Claude Agent SDK helper's envelope, protocol 1 (#151; ADR 0007 §5).

Records in, one outcome out.

:class:`ClaudeSdkSession` is the controller's side of one helper invocation
as a pure reducer, the way :class:`autoforge.pi_rpc.PiConversation` is Pi's
and :class:`autoforge.claude_stream.ClaudeStream` is ``claude -p``'s. The
driver (:meth:`autoforge.providers.ClaudeSdkProvider.execute`) feeds it every
record the helper writes, reports a record past the per-record bound, an
unterminated last line, an overflow and the end of stdout, and sends what
:attr:`ClaudeSdkSession.outbox` holds. Nothing here starts a process, reads
a clock or imports the SDK; the module knows the envelope's record names and
the few Claude Code fields the outcome turns on, and nothing else. The
helper program is ``sdk_helpers/claude_helper.py``, a separate executable.

The envelope: one JSON object per LF-terminated line, UTF-8 (the helper
writes ASCII with escapes), at most the ``max_record_bytes`` the ``start``
record names. Every record has a ``type`` and exactly its fields:

========================  ==========  ==================================================
record                    direction   fields
========================  ==========  ==================================================
``hello``                 helper      ``envelope``, ``protocol``, ``sdk_version``,
                                      ``cli_version``, ``python_version``: first
``start``                 controller  ``session_id`` (the caller's UUID, D9.1), ``mode``
                                      (``new``), ``prompt``, ``cwd``, ``model``,
                                      ``effort``, ``permission_mode``, ``policy``
                                      (``ClaudeSdkPolicy.start_fields``),
                                      ``hook_timeout_seconds``, ``interactions``
                                      (``live``, ``defer``), ``max_record_bytes``
``message``               helper      ``data``: one SDK message as the CLI record it was
                                      parsed from, with no assistant or thinking text,
                                      no tool result content and no raw usage object
``ask``                   helper      ``request_id``, ``kind``, ``tool_name``,
                                      ``tool_use_id``, ``input`` (``null`` when it does
                                      not fit a record), ``input_sha256``, ``agent``
``decision``              controller  ``request_id``, ``directive`` (``allow``,
                                      ``deny``, ``fail``), ``updated_input``,
                                      ``guidance``
``interrupt``             controller  none
``end``                   helper      ``outcome`` (``result``, ``interrupted``),
                                      ``skipped_unknown``, ``asks``, ``hook_denials``,
                                      ``host_denials``: last, before exit 0
``failure``               helper      ``reason``: before a non-zero exit
========================  ==========  ==================================================

End of file on the helper's stdin is the shutdown request; ``pause`` and a
continuation (``mode: continue``, the ``defer`` directive) are #126's and
not part of this release's protocol 1 (ADR 0007 D7.5, D9.2-D9.4).

The outcome (ADR 0007 D10.3, D10.4):

* ``hello`` must name this envelope and protocol and the manifest's pinned
  pair exactly, on a Python at or above the interpreter floor; anything else
  is a provider failure ("SDK pair mismatch", D4.1) and ``start`` is never
  sent.
* Every ``system/init`` must report the caller's session id and match the
  policy (:meth:`autoforge.sdk_policy.ClaudeSdkPolicy.init_problems`); the
  ``result`` must report the same session id.
* Only the ``result`` of the main turn is a completion, and only when it is
  ``success`` with ``is_error: false`` (an API error can come with ``subtype:
  success``), ``terminal_reason: completed``, a ``stop_reason`` of a finished
  turn, no ``deferred_tool_use`` and no injected origin, and the helper's
  ``end`` then says ``result``. Its ``result`` text becomes stdout, verbatim;
  no other text crosses the envelope, so a ``CONTROL_RESULT`` planted in an
  earlier message, a diagnostic or stderr is never parsed. An interrupted
  turn's result never is.
* An unknown record type, a missing or extra field, a ``request_id`` nobody
  issued, a record after ``end`` and a stream that ends without ``end`` or
  ``failure`` fail closed; so does an ``ask`` about a call the policy does
  not ask about. An SDK message of a type the helper does not know is counted
  (``skipped_unknown``) and otherwise ignored, and so is an observation of a
  type this reducer does not know.
* A subagent's message (``parent_tool_use_id``) fails the run: the policy
  disallows ``Agent`` and ``Task`` (attribution is gate G-C8), so one means
  the runtime ran something the policy does not let it.

What reaches the model of a decision, verified on the pinned pair (SDK
0.2.163 with its bundled Claude Code 2.1.286, against a loopback stand-in of
the Messages API, so no model was called; ADR 0007 §3's amendment): an
``AskUserQuestion`` call answered by the hook's ``allow`` with the original
``questions`` and an ``answers`` object mapping each question's text to the
labels picked, joined by ``", "`` (several for ``multiSelect``), is told
``Your questions have been answered: "<question>"="<labels>"``; an answer
outside the options is passed on as ``The user answered: ...``; a bare
``allow`` without ``answers`` falls through to the permission host, which
denies it. ``deny`` with a reason reaches the model as ``PreToolUse:<tool>
hook error: <reason>``; ``allow`` with an ``updatedInput`` runs that input;
two calls in one turn are asked about one at a time.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass

from .claude_stream import ClaudeStream
from .errors import InteractionDataError
from .interaction import Answer, Question, QuestionOption, RequestKind
from .loop_detect import LoopObserver
from .progress import ProgressEvent, ProgressKind, ProgressSink, clean
from .result_parser import holds_control_result
from .sdk_assets import HelperPin, dotted
from .sdk_policy import (
    CLAUDE_SDK_QUESTION_TOOL,
    MAX_QUESTIONS,
    ClaudeSdkPolicy,
    canonical_json,
    input_digest,
)

ENVELOPE = "autoforge-claude-helper"
PROTOCOL = 1
# How much longer than the per-wait limit the CLI gives a PreToolUse hook
# (``HookMatcher.timeout``) before it times the hook out (ADR 0007 D7.3):
# the broker always answers within ``max_wait_seconds``, so the CLI never
# times a waiting hook out first. Verified on the pinned pair: a hook past
# its timeout is reported to the model as not answered and its call is not
# run; one answered within it is (gate G-C10).
HOOK_TIMEOUT_MARGIN_SECONDS = 30

# The result subtypes of the pinned pair (SDK ``ResultMessage``); another
# one fails closed.
RESULT_SUBTYPES = frozenset(
    {
        "success",
        "error_max_turns",
        "error_during_execution",
        "error_max_budget_usd",
        "error_max_structured_output_retries",
    }
)
# What ends a completed main turn: the query loop completed (verified on the
# pinned pair), on the model's own end of turn or a stop sequence.
COMPLETED_TERMINAL_REASON = "completed"
COMPLETED_STOP_REASONS = frozenset({"end_turn", "stop_sequence"})
# An ``AssistantMessage.error`` the SDK types (``AssistantMessageError``),
# as the status :class:`ClaudeStream` types a model API error by; only the
# usage or rate limit has one (``None``: an API error without a status).
ASSISTANT_ERRORS: dict[str, int | None] = {
    "rate_limit": 429,
    "authentication_failed": None,
    "billing_error": None,
    "invalid_request": None,
    "server_error": None,
    "unknown": None,
}

MAX_REASON_CHARS = 400
MAX_VALUE_CHARS = 120

_RECORD_KEYS: dict[str, frozenset[str]] = {
    "hello": frozenset(
        {"type", "envelope", "protocol", "sdk_version", "cli_version", "python_version"}
    ),
    "message": frozenset({"type", "data"}),
    "ask": frozenset(
        {
            "type",
            "request_id",
            "kind",
            "tool_name",
            "tool_use_id",
            "input",
            "input_sha256",
            "agent",
        }
    ),
    "end": frozenset(
        {"type", "outcome", "skipped_unknown", "asks", "hook_denials", "host_denials"}
    ),
    "failure": frozenset({"type", "reason"}),
}
_END_COUNTS = ("skipped_unknown", "asks", "hook_denials", "host_denials")
_PYTHON_RE = re.compile(r"([0-9]+)\.([0-9]+)\.([0-9]+)[a-z0-9+]*")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_MAX_COUNT = 10**9

# The input a modified call must still carry, per tool: the strings the
# tool's own schema requires. A modified input without them is refused,
# never sent (the provider's schema, as far as AutoForge knows it).
TOOL_REQUIRED_STRINGS: dict[str, tuple[str, ...]] = {
    "Bash": ("command",),
    "Read": ("file_path",),
    "Glob": ("pattern",),
    "Grep": ("pattern",),
    "Edit": ("file_path", "old_string", "new_string"),
    "Write": ("file_path", "content"),
    "NotebookEdit": ("notebook_path", "new_source"),
    "WebFetch": ("url", "prompt"),
    "WebSearch": ("query",),
}

INTERRUPT = b'{"type":"interrupt"}'


def _line(record: Mapping[str, object]) -> bytes:
    return json.dumps(record, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def start_record(
    *,
    session_id: str,
    prompt: str,
    cwd: str,
    model: str,
    effort: str,
    policy: ClaudeSdkPolicy,
    max_record_bytes: int,
) -> bytes:
    """The ``start`` record of a new session (ADR 0007 §5), as one line."""
    return _line(
        {
            "type": "start",
            "session_id": session_id,
            "mode": "new",
            "prompt": prompt,
            "cwd": cwd,
            "model": model,
            "effort": effort,
            "permission_mode": policy.permission_mode,
            "policy": policy.start_fields(),
            "hook_timeout_seconds": policy.interaction.max_wait_seconds
            + HOOK_TIMEOUT_MARGIN_SECONDS,
            "interactions": {"live": policy.interaction.live, "defer": False},
            "max_record_bytes": max_record_bytes,
        }
    )


def decision_record(
    request_id: str,
    directive: str,
    *,
    updated_input: Mapping[str, object] | None = None,
    guidance: str | None = None,
) -> bytes:
    """The ``decision`` record answering ``request_id``."""
    if directive not in ("allow", "deny", "fail"):
        raise ValueError(f"unknown directive {directive!r}")
    return _line(
        {
            "type": "decision",
            "request_id": request_id,
            "directive": directive,
            "updated_input": None if updated_input is None else dict(updated_input),
            "guidance": guidance,
        }
    )


@dataclass(frozen=True)
class Ask:
    """One ``ask`` the helper sent: an interaction-eligible call's PreToolUse
    hook, waiting for a directive. ``problem`` is why its data cannot be put
    to anyone (past a bound, not canonical JSON, a digest that differs), in
    which case the driver answers it as the broker's ``Fail``."""

    request_id: str
    kind: RequestKind
    tool_name: str
    tool_use_id: str
    tool_input: Mapping[str, object] | None
    input_sha256: str
    agent: str | None
    problem: str = ""


def ask_questions(ask: Ask) -> tuple[Question, ...]:
    """The questions of an ``AskUserQuestion`` call, as the contract's
    :class:`~autoforge.interaction.Question`; :class:`InteractionDataError`
    for one the broker cannot render (D10.4).

    Its input is ``{"questions": [...]}``, one to four questions of exactly
    ``question``, ``header``, ``options`` (each exactly ``label`` and
    ``description``) and ``multiSelect``. Each allows an answer of the
    operator's own words: the pinned pair passes an answer outside the
    options on to the model (module docstring).
    """
    raw = ask.tool_input
    if raw is None or set(raw) != {"questions"}:
        raise InteractionDataError("a question call's input is not exactly its questions")
    items = raw["questions"]
    if not isinstance(items, (list, tuple)) or not 1 <= len(items) <= MAX_QUESTIONS:
        raise InteractionDataError(f"a question call does not ask 1 to {MAX_QUESTIONS} questions")
    questions = []
    for item in items:
        if not isinstance(item, Mapping) or set(item) != {
            "question",
            "header",
            "options",
            "multiSelect",
        }:
            raise InteractionDataError("a question is not exactly protocol 1's fields")
        options = item["options"]
        if not isinstance(options, (list, tuple)) or not all(
            isinstance(option, Mapping) and set(option) == {"label", "description"}
            for option in options
        ):
            raise InteractionDataError("a question's options are not labels with descriptions")
        questions.append(
            Question(
                text=item["question"],
                header=item["header"],
                options=tuple(
                    QuestionOption(label=option["label"], description=option["description"])
                    for option in options
                ),
                multiple=item["multiSelect"],
                custom=True,
            )
        )
    return tuple(questions)


def answer_input(ask: Ask, answer: Answer) -> dict[str, object]:
    """The ``updatedInput`` that answers ``ask`` with ``answer``: the original
    ``questions`` and an ``answers`` object mapping each question's own text
    to its labels, and the operator's own words, joined by ``", "``."""
    raw = ask.tool_input
    assert raw is not None
    items = raw["questions"]
    assert isinstance(items, (list, tuple))
    answers: dict[str, str] = {}
    for item, response in zip(items, answer.responses, strict=True):
        assert isinstance(item, Mapping)
        picked = [*response.choices]
        if response.custom_text is not None:
            picked.append(response.custom_text)
        answers[str(item["question"])] = ", ".join(picked)
    questions = json.loads(canonical_json(list(items)))
    return {"questions": questions, "answers": answers}


def modified_input_problem(tool_name: str, updated: Mapping[str, object]) -> str:
    """Why ``updated`` is no input ``tool_name`` takes, or "" (names only)."""
    missing = [
        key
        for key in TOOL_REQUIRED_STRINGS.get(tool_name, ())
        if not isinstance(updated.get(key), str) or not updated.get(key)
    ]
    if missing:
        return f"a modified {tool_name} input lacks {', '.join(missing)}"
    return ""


def _count(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < _MAX_COUNT:
        return None
    return value


class ClaudeSdkSession:
    """One helper invocation as a reducer (module docstring). Not thread-safe;
    one driver owns it.

    ``start`` is the ``start`` record, sent once ``hello`` checks out.
    ``emit`` and ``loop`` are the progress sink and the loop observer the
    messages are reported to, through a :class:`ClaudeStream`.
    """

    def __init__(
        self,
        *,
        pin: HelperPin,
        policy: ClaudeSdkPolicy,
        session_id: str,
        start: bytes,
        cwd: str = "",
        emit: ProgressSink | None = None,
        loop: LoopObserver | None = None,
    ) -> None:
        self._pin = pin
        self._policy = policy
        self._session_id = session_id
        self._start = start
        self._emit = emit
        self.stream = ClaudeStream(cwd=cwd, emit=emit, loop=loop, is_reply=holds_control_result)
        # What the driver sends next, in order.
        self.outbox: list[bytes] = []
        # A failure of the envelope, the helper, the pair or the policy
        # (the stream's own is ``stream.failure``): bounded and redacted.
        self.failure: str | None = None
        # The fixed kind of that failure, never text the helper sent (#434).
        self.failure_kind: str | None = None
        # The helper's ``end`` outcome: ``result`` or ``interrupted``.
        self.outcome: str | None = None
        # The helper reported a failure of its own (a ``failure`` record).
        self.helper_failed = False
        # The controller asked for the turn to end (an interrupt, stdin closed).
        self.interrupt_sent = False
        self.versions: dict[str, str] = {}
        self._state = "hello"
        self._asks: dict[str, Ask] = {}
        self._seen_requests: set[str] = set()
        self._seen_calls: set[str] = set()
        self._asked = 0
        self._inits = 0
        self._subagent_records = 0
        self._result_checked = False
        self._model = ""
        self._end_counts: dict[str, int] = {}
        self._records = 0
        self._oversize = 0

    # -- the facts the driver reads ----------------------------------------------
    @property
    def started(self) -> bool:
        """``hello`` checked out and ``start`` was queued."""
        return self._state != "hello"

    @property
    def done(self) -> bool:
        """Nothing the helper writes from here changes the outcome: its ``end``
        arrived, or the envelope failed. A failing result is followed by the
        helper's ``end`` at once, which is read too."""
        return self._state == "ended" or self.failure is not None

    @property
    def failed(self) -> str | None:
        """Why the invocation failed, the envelope's or the stream's, or ``None``."""
        if self.failure is not None:
            return self.failure
        if self.stream.failure is not None:
            return _ours(self.stream.failure)
        return None

    @property
    def text(self) -> str | None:
        """The completed main turn's ``result`` text, or ``None`` (module docstring)."""
        if (
            self.failed is not None
            or self.outcome != "result"
            or not self._result_checked
            or self.stream.text is None
        ):
            return None
        return self.stream.text

    @property
    def reply_proven(self) -> bool:
        """The helper's account proves :attr:`text` the run's last word (#479):
        its ``end`` followed the result, which decided the run on a block, with
        every record read whole and every tool call tracked to its end."""
        return self.text is not None and self._oversize == 0 and self.stream.reply_proven

    @property
    def open_asks(self) -> tuple[Ask, ...]:
        return tuple(self._asks.values())

    # -- driver interface ------------------------------------------------------
    def feed(self, data: bytes) -> Ask | None:
        """Reduce one record of the helper's (without its LF); an :class:`Ask`
        for the driver to put to the broker and answer with :meth:`decide`."""
        self._records += 1
        if self._state == "ended" and self.failure is None:
            self._protocol("a record after the helper's end")
            return None
        if self.failure is not None:
            return None
        try:
            record = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError):
            self._protocol("a record is not JSON")
            return None
        if not isinstance(record, dict) or not isinstance(record.get("type"), str):
            self._protocol("a record is not a JSON object with a type")
            return None
        kind = record["type"]
        keys = _RECORD_KEYS.get(kind)
        if keys is None:
            self._protocol(f"an unknown record type {clean(kind, 40)!r}")
            return None
        if set(record) != keys:
            self._protocol(f"a {kind} record does not hold exactly protocol 1's fields")
            return None
        if kind == "failure":
            self._helper_failure(record)
            return None
        if self._state == "hello":
            if kind != "hello":
                self._protocol(f"a {kind} record before hello")
            else:
                self._hello(record)
            return None
        if kind == "hello":
            self._protocol("a second hello")
        elif kind == "message":
            self._message(record["data"])
        elif kind == "ask":
            return self._ask(record)
        else:
            self._end(record)
        return None

    def oversize(self, limit: int) -> None:
        """A record past ``limit`` bytes was dropped by the framer: the helper
        bounds every record it writes, so this is a protocol violation."""
        self._oversize += 1
        self._protocol(f"a record is longer than {limit} bytes")

    def framing_error(self, what: str) -> None:
        self._protocol(what)

    def stream_ended(self) -> None:
        """The helper's stdout reached its end."""
        if self._state == "ended" or self.failure is not None:
            return
        self._fail(
            "claude-sdk: the helper's stdout ended before its end record",
            "claude-sdk: the helper ended without its outcome",
        )

    def interrupt(self) -> bytes:
        """The ``interrupt`` record: the controller ends the running turn."""
        self.interrupt_sent = True
        return INTERRUPT

    def stdin_closed(self) -> None:
        """The controller closed the helper's stdin: a shutdown request."""
        self.interrupt_sent = True

    def decide(
        self,
        ask: Ask,
        directive: str,
        *,
        updated_input: Mapping[str, object] | None = None,
        guidance: str | None = None,
    ) -> bytes:
        """The ``decision`` record for ``ask``, which is then no longer open."""
        if self._asks.pop(ask.request_id, None) is None:
            raise ValueError("a decision for an ask that is not open")
        return decision_record(
            ask.request_id, directive, updated_input=updated_input, guidance=guidance
        )

    def summary(self) -> dict[str, str | int | bool]:
        """The flat, bounded, redacted summary for ``execution.json``: the
        pair and the session, the stream's (:meth:`ClaudeStream.summary`), and
        the helper's counts. Never a raw record, a text or a header."""
        summary: dict[str, str | int | bool] = dict(self.versions)
        summary.update(self.stream.summary())
        summary.update(
            {
                "session_id": self._session_id,
                "model": self._model,
                "outcome": self.outcome or "",
                "helper_records": self._records,
                "asked": self._asked,
                "subagent_records": self._subagent_records,
                "failure": self.failed or "",
            }
        )
        summary.update(self._end_counts)
        return summary

    # -- internals ---------------------------------------------------------------
    def _fail(self, reason: str, kind: str) -> None:
        if self.failure is None:
            self.failure = clean(reason, MAX_REASON_CHARS)
            self.failure_kind = kind

    def _protocol(self, what: str) -> None:
        self._fail(
            f"claude-sdk: helper protocol violation: {what}",
            "claude-sdk: helper protocol violation",
        )

    def _helper_failure(self, record: dict) -> None:
        reason = clean(record["reason"], MAX_REASON_CHARS - 40)
        self.helper_failed = True
        self._fail(
            f"claude-sdk: the helper failed: {reason or 'no reason given'}",
            "claude-sdk: the helper failed",
        )

    def _hello(self, record: dict) -> None:
        pin = self._pin
        sdk, cli, python = record["sdk_version"], record["cli_version"], record["python_version"]
        self.versions = {
            "sdk_version": clean(sdk, 40),
            "cli_version": clean(cli, 40),
            "python_version": clean(python, 40),
        }
        protocol = record["protocol"]
        if (
            record["envelope"] != pin.envelope
            or type(protocol) is not int
            or protocol != pin.protocol
        ):
            self._fail(
                f"claude-sdk: the helper speaks {clean(record['envelope'], 60)!r} protocol "
                f"{clean(str(record['protocol']), 20)}, not {pin.envelope} protocol "
                f"{pin.protocol}: a damaged or foreign helper",
                "claude-sdk: helper protocol mismatch",
            )
            return
        mismatch = []
        if sdk != pin.sdk_version:
            mismatch.append(f"{pin.sdk_package} {self.versions['sdk_version'] or '(none)'}")
        if cli != pin.runtime_version:
            mismatch.append(f"bundled {pin.runtime} {self.versions['cli_version'] or '(none)'}")
        matched = _PYTHON_RE.fullmatch(python) if isinstance(python, str) else None
        if matched is None or dotted(".".join(matched.groups())) < pin.interpreter_minimum:
            floor = ".".join(str(part) for part in pin.interpreter_minimum)
            mismatch.append(
                f"Python {self.versions['python_version'] or '(none)'} (needs {floor} or newer)"
            )
        if mismatch:
            self._fail(
                f"claude-sdk: SDK pair mismatch: the helper environment has "
                f"{', '.join(mismatch)}; this AutoForge supports {pin.pair()} exactly "
                "(ADR 0007 D4.1). Install the pinned pair from helpers/claude-sdk/",
                "claude-sdk: SDK pair mismatch",
            )
            return
        self._state = "running"
        self.outbox.append(self._start)

    def _message(self, data: object) -> None:
        if not isinstance(data, dict) or not isinstance(data.get("type"), str):
            self._protocol("a message carries no record with a type")
            return
        if data.get("oversize") is True:
            # The helper could not fit the message in one record even with
            # its tool inputs digested: an observation the reducer cannot read.
            self.stream.oversize(0)
            return
        kind = data["type"]
        if kind in ("assistant", "user") and data.get("parent_tool_use_id") is not None:
            self._subagent_records += 1
            self._fail(
                "claude-sdk: a subagent ran, although the policy disallows Agent and Task "
                "(its attribution is gate G-C8)",
                "claude-sdk: an unattributed subagent ran",
            )
            return
        if kind == "system" and data.get("subtype") == "init":
            self._init(data)
            if self.failure is not None:
                return
        elif kind == "assistant":
            data = self._typed_api_error(data)
        elif kind == "result":
            self._result(data)
            if self.failure is not None:
                return
        self.stream.feed_record(data)

    def _init(self, init: dict) -> None:
        self._inits += 1
        model = init.get("model")
        if isinstance(model, str) and not self._model:
            self._model = clean(model, MAX_VALUE_CHARS)
        if init.get("session_id") != self._session_id:
            self._fail(
                "claude-sdk: the session's system/init does not report the session id "
                "AutoForge gave it (ADR 0007 D9.1)",
                "claude-sdk: session id mismatch",
            )
            return
        problems = self._policy.init_problems(init)
        if problems:
            self._fail(
                "claude-sdk: the runtime departed from the policy at system/init: "
                + "; ".join(problems),
                "claude-sdk: the runtime departed from the policy",
            )

    @staticmethod
    def _typed_api_error(data: dict) -> dict:
        """An ``assistant`` record whose SDK-typed ``error`` names a model API
        error, as the CLI's own typed record (``is_api_error_message``,
        ``api_error_status``) :class:`ClaudeStream` reads; unchanged otherwise.
        A usage or rate limit has status 429; the others carry none."""
        error = data.get("error")
        if error is None:
            return data
        status = ASSISTANT_ERRORS.get(error) if isinstance(error, str) else None
        return {**data, "is_api_error_message": True, "api_error_status": status}

    def _result(self, result: dict) -> None:
        """The SDK checks of the main turn's result, before the stream's own
        (``is_error``, ``subtype``, a string ``result``)."""
        if result.get("session_id") != self._session_id:
            self._fail(
                "claude-sdk: the result does not report the session id AutoForge gave the "
                "session (ADR 0007 D9.1)",
                "claude-sdk: session id mismatch",
            )
            return
        subtype = result.get("subtype")
        if subtype not in RESULT_SUBTYPES:
            self._fail(
                f"claude-sdk: the result has an unknown subtype {clean(subtype, 60)!r}",
                "claude-sdk: an unknown outcome",
            )
            return
        if result.get("is_error") is not False or subtype != "success":
            return  # the stream fails it with its facts
        if result.get("deferred_tool_use") is not None:
            self._fail(
                "claude-sdk: the result deferred a tool call, which this release never asks "
                "for (a pause is #126's)",
                "claude-sdk: an unrequested deferral",
            )
            return
        origin = result.get("origin")
        if origin not in (None, "human"):
            self._fail(
                "claude-sdk: the result answers a turn the session injected, not AutoForge's "
                "prompt",
                "claude-sdk: an injected turn's result",
            )
            return
        terminal, stop = result.get("terminal_reason"), result.get("stop_reason")
        if terminal != COMPLETED_TERMINAL_REASON or stop not in COMPLETED_STOP_REASONS:
            self._fail(
                "claude-sdk: the result is a success that did not complete its turn "
                f"(terminal_reason {clean(terminal, 60) or '-'}, stop_reason "
                f"{clean(stop, 60) or '-'})",
                "claude-sdk: the turn did not complete",
            )
            return
        self._result_checked = True

    def _ask(self, record: dict) -> Ask | None:
        request_id, kind = record["request_id"], record["kind"]
        tool, call, agent = record["tool_name"], record["tool_use_id"], record["agent"]
        if not isinstance(request_id, str) or not request_id or request_id in self._seen_requests:
            self._protocol("an ask without a fresh request id")
            return None
        if not isinstance(call, str) or not call or call in self._seen_calls:
            self._protocol("an ask without a fresh tool call id")
            return None
        if kind not in ("question", "permission") or not isinstance(tool, str):
            self._protocol("an ask of no known kind")
            return None
        if (kind == "question") != (tool == CLAUDE_SDK_QUESTION_TOOL):
            self._protocol("an ask whose kind does not match its tool")
            return None
        if tool not in self._policy.ask_tools:
            self._protocol("an ask about a call the policy does not ask about")
            return None
        if agent is not None and not isinstance(agent, str):
            self._protocol("an ask from an agent with no name")
            return None
        tool_input, digest = record["input"], record["input_sha256"]
        if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
            self._protocol("an ask without an input digest")
            return None
        if tool_input is not None and not isinstance(tool_input, dict):
            self._protocol("an ask whose input is not an object")
            return None
        self._seen_requests.add(request_id)
        self._seen_calls.add(call)
        self._asked += 1
        problem = ""
        if tool_input is None:
            problem = "the call's input does not fit one helper record"
        else:
            try:
                if input_digest(tool_input, "the call's input") != digest:
                    problem = "the call's input does not match the digest the helper sent"
            except InteractionDataError as exc:
                problem = str(exc)
        ask = Ask(
            request_id=request_id,
            kind=RequestKind(kind),
            tool_name=tool,
            tool_use_id=call,
            tool_input=tool_input,
            input_sha256=digest,
            agent=agent,
            problem=problem,
        )
        self._asks[request_id] = ask
        if self._emit is not None:
            self._emit(ProgressEvent(ProgressKind.ACTIVITY))
        return ask

    def _end(self, record: dict) -> None:
        outcome = record["outcome"]
        counts = {key: _count(record[key]) for key in _END_COUNTS}
        if outcome not in ("result", "interrupted") or any(v is None for v in counts.values()):
            self._protocol("an end record of no known outcome")
            return
        self._end_counts = {key: value for key, value in counts.items() if value is not None}
        if self._asks:
            self._protocol("an end record with an ask still open")
            return
        if outcome == "interrupted" and not self.interrupt_sent:
            self._protocol("the helper interrupted a turn nobody asked it to")
            return
        if outcome == "result" and not self._result_checked and self.failed is None:
            self._protocol("an end record says result before any result")
            return
        self.outcome = outcome
        self._state = "ended"


def _ours(reason: str) -> str:
    """A :class:`ClaudeStream` reason, named for this provider."""
    return "claude-sdk: " + reason.removeprefix("claude: ")
