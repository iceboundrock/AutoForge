"""Pi RPC wire protocol (ADR 0003): records in, commands out, one outcome.

:class:`PiConversation` is the whole of one ``pi --mode rpc`` invocation as
a pure reducer. The caller (:class:`autoforge.providers.PiProvider`) feeds
it every stdout record as bytes and the current ``time.monotonic()``, and
writes back the command records it returns; nothing here starts a process,
knows a CLI flag or reads a clock. The module knows Pi's command, response
and event names and nothing else, so its tests drive it from synthetic
records with no process at all.

The conversation, in order (ADR 0003 §2.1 to §2.8):

1. ``get_state`` and ``get_available_models``, correlated by id (Pi handles
   commands concurrently, so never by order). The resolved model must be
   present, its ``provider`` and ``id`` must equal the configured ones
   exactly, the pair must be in the available models (Pi starts with a
   placeholder for an unknown id, so ``get_state`` alone cannot catch a
   typo), and ``thinkingLevel`` must equal the configured effort (a clamp
   is a hard failure). Each is a bounded round trip.
2. One ``prompt``, never in argv. Its response must be ``success: true``
   with ``disposition: "started"``; ``success: false`` is a rejection
   before acceptance, and any other disposition is a protocol failure.
3. Events until ``agent_settled``. ``agent_end`` is never terminal. The
   last assistant ``message_end`` decides: ``stop`` goes on, ``length``,
   ``error`` and ``aborted`` are provider failures, anything else is a
   protocol failure. An extension dialog is answered at once with
   ``cancelled: true`` and fails the run. Unknown event types are counted
   and otherwise ignored.
4. ``get_last_assistant_text``, a bounded round trip. A missing, ``null``,
   non-string or blank ``text`` is "no assistant text"; a text that differs
   from the one derived from the last assistant ``message_end`` (its
   ``text`` blocks joined with no separator, then trimmed as JavaScript's
   ``String.prototype.trim`` does) is a protocol failure.

Every string that leaves this module for a log (a failure reason, a summary
value) is bounded, made encodable, redacted and put on one line here. Raw
records, the ``get_state`` object, the model list and message contents
other than the final text never leave it.

Live progress (#192): with an ``emit`` sink, every event record becomes a
:class:`~autoforge.progress.ProgressEvent`. ``tool_execution_start`` is a
tool start whose detail is one allow-listed argument (:data:`TOOL_DETAILS`;
never ``bash``'s command), ``tool_execution_end`` a finished or failed tool
by its ``isError``, a ``message_update`` whose ``assistantMessageEvent`` is
a thinking or text delta is thinking or assistant text (never its content),
``auto_retry_start`` a provider retry with its attempt numbers, the
accepted prompt the start, and anything else bare activity. The argument
names are read from Pi's tool schemas at v1.0.x, not from a run.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from .progress import ProgressEvent, ProgressKind, ProgressSink, relative_path
from .redaction import redact

# Extension UI methods that wait for an answer (``editor`` has no Pi-side
# timeout and would wait forever), and the fire-and-forget ones.
DIALOG_METHODS = frozenset({"select", "confirm", "input", "editor"})
NOTIFY_METHODS = frozenset({"notify", "setStatus", "setWidget", "setTitle", "set_editor_text"})
SUCCESS_STOP_REASONS = ("stop",)
# Events the reducer reads, and known events it deliberately ignores. Any
# other type is counted as unknown (forward compatibility); the count is a
# diagnostic only.
_REDUCED_EVENTS = frozenset(
    {
        "agent_end",
        "agent_settled",
        "message_end",
        "tool_execution_start",
        "auto_retry_start",
        "auto_retry_end",
        "extension_ui_request",
    }
)
_IGNORED_EVENTS = frozenset(
    {
        "agent_start",
        "turn_start",
        "turn_end",
        "message_start",
        "message_update",
        "tool_execution_update",
        "tool_execution_end",
        "auto_compaction_start",
        "auto_compaction_end",
        "queue_update",
        "extension_error",
        # Pi 1.0.1 emits it after a `length` stop or a retried error, when it
        # drops the abandoned message from the context.
        "entry_appended",
    }
)
# What Pi reports when a ChatGPT subscription has run out of usage.
_USAGE_LIMIT_CODE = "subscription_sharing_usage_limit_exceeded"
# Commands answered within the round-trip bound. ``prompt`` is not one: its
# response follows Pi's preflight, which may refresh a credential.
_ROUND_TRIPS = frozenset({"get_state", "get_available_models", "get_last_assistant_text"})

MAX_REASON_CHARS = 400
MAX_VALUE_CHARS = 200
# The one argument shown per Pi tool in progress; ``bash`` has only its
# command, which is never shown.
TOOL_DETAILS: dict[str, str] = {
    "read": "path",
    "edit": "path",
    "write": "path",
    "ls": "path",
    "grep": "pattern",
    "find": "pattern",
}
_PATH_ARGS = frozenset({"path"})
# JavaScript's ``String.prototype.trim`` set (WhiteSpace and LineTerminator,
# ECMA-262): not Python's ``str.isspace``, which adds U+001C..U+001F and
# U+0085 and lacks U+FEFF.
_JS_SPACE = "\t\n\v\f\r                  　﻿"
_WHITESPACE_RUN = re.compile(r"\s+")


def js_trim(text: str) -> str:
    """``text`` trimmed exactly as JavaScript's ``String.prototype.trim`` trims."""
    return text.strip(_JS_SPACE)


def encodable(text: str) -> str:
    """``text`` with any lone surrogate (legal in a JSON string, not in UTF-8)
    replaced by U+FFFD, so it can be logged; everything else unchanged."""
    return text.encode("utf-16", "surrogatepass").decode("utf-16", "replace")


def bounded(value: object, limit: int = MAX_VALUE_CHARS) -> str:
    """A loggable one-line rendering of Pi-supplied ``value``: redacted, then clipped."""
    if not isinstance(value, str):
        return ""
    text = _WHITESPACE_RUN.sub(" ", redact(encodable(value))).strip()
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _encode(record: dict[str, object]) -> bytes:
    # ASCII-only, so neither a raw LF nor a U+2028 ever reaches the wire,
    # and a lone surrogate in the prompt is escaped rather than unencodable.
    return json.dumps(record, ensure_ascii=True, separators=(",", ":")).encode("ascii")


@dataclass
class _Pending:
    command: str
    sent_at: float


class PiConversation:
    """One Pi RPC invocation as a reducer. Not thread-safe; one driver owns it.

    ``start`` returns the first commands; ``feed`` takes one stdout record
    and returns the commands to write in reply. The conversation is over
    when :attr:`done`: :attr:`text` holds the final assistant text on
    success, :attr:`failure` the bounded reason otherwise.
    """

    def __init__(
        self,
        *,
        model: str,
        thinking: str,
        prompt: str,
        round_trip_seconds: float,
        new_id: Callable[[], str] | None = None,
        emit: ProgressSink | None = None,
        cwd: str = "",
    ) -> None:
        self._emit = emit
        self._cwd = cwd
        self._model = model
        self._provider, _, self._model_id = model.partition("/")
        self._thinking = thinking
        self._prompt = prompt
        self._round_trip = round_trip_seconds
        self._new_id = new_id or (lambda: str(uuid.uuid4()))
        self._pending: dict[str, _Pending] = {}
        self.text: str | None = None
        self.failure: str | None = None
        # Pi's stdout ended before the conversation was over; the driver
        # names the exit code once the process has been reaped.
        self.exited_early = False
        self._state_checked = False
        self._models: list[tuple[object, object]] | None = None
        self._prompt_sent = False
        self._prompt_started = False
        self._settled = False
        self._abort_sent = False
        self._deadline = False
        self._last_assistant: dict | None = None
        self._last_text_message: dict | None = None
        self._retry_failure: str | None = None
        # The summary (ADR 0003 §2.6): scalars only.
        self._resolved = {"provider": "", "id": "", "thinking": ""}
        self._disposition = ""
        self._stop_reason = ""
        self._agent_ends = 0
        self._will_retry = 0
        self._auto_retries = 0
        self._tool_executions = 0
        self._dialogs_cancelled = 0
        self._notifications_ignored = 0
        self._unknown_events = 0

    # -- driver interface ----------------------------------------------------
    @property
    def done(self) -> bool:
        return self.text is not None or self.failure is not None

    @property
    def abort_settled(self) -> bool:
        """After :meth:`begin_abort`: whether Pi has settled the aborted run."""
        return self._settled or not self._abort_sent

    @property
    def next_wakeup(self) -> float | None:
        """When the oldest bounded round trip runs out, if one is pending."""
        times = [
            p.sent_at + self._round_trip
            for p in self._pending.values()
            if p.command in _ROUND_TRIPS
        ]
        return min(times) if times and not self.done else None

    def start(self, now: float) -> list[bytes]:
        return [self._command("get_state", now), self._command("get_available_models", now)]

    def feed(self, data: bytes, now: float) -> list[bytes]:
        """Reduce one stdout record; returns the records to write in reply."""
        try:
            record = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._protocol("a stdout record is not JSON")
            return []
        except (ValueError, RecursionError):
            # The decoder's integer-digit and nesting limits, which are not
            # JSONDecodeError (as in ``claude_stream``); an interrupt still
            # propagates.
            self._protocol("a stdout record exceeds the JSON decoder's integer or nesting limit")
            return []
        if not isinstance(record, dict) or not isinstance(record.get("type"), str):
            self._protocol("a stdout record is not a JSON object with a 'type'")
            return []
        kind = record["type"]
        if kind == "response":
            return self._response(record, now)
        if kind == "extension_ui_request":
            return self._ui_request(record)
        if self.done:
            # The outcome is fixed; only the end of an aborted run matters.
            if kind == "agent_settled":
                self._settled = True
            return []
        self._progress(kind, record)
        if kind in _REDUCED_EVENTS:
            return self._event(kind, record, now)
        if kind not in _IGNORED_EVENTS:
            self._unknown_events += 1
        return []

    def tick(self, now: float) -> None:
        """Fail when a bounded round trip has gone unanswered past its bound."""
        wake = self.next_wakeup
        if wake is not None and now >= wake:
            late = min(
                (p for p in self._pending.values() if p.command in _ROUND_TRIPS),
                key=lambda p: p.sent_at,
            )
            self._fail(f"pi: no response to {late.command} within {self._round_trip:g}s")

    def framing_error(self, what: str) -> None:
        self._protocol(what)

    def stream_ended(self) -> None:
        """Pi's stdout reached EOF."""
        if self.done:
            return
        self.exited_early = True
        self._fail(f"pi: exited {self._stage()}")

    def stdin_closed(self) -> None:
        self._fail(f"pi: closed its stdin {self._stage()}")

    def deadline_reached(self) -> None:
        """The invocation's deadline cut the conversation short."""
        self._deadline = True
        self._fail(f"pi: the deadline was reached {self._stage()}")

    def begin_abort(self, now: float) -> list[bytes]:
        """The ``abort`` record when a prompt is running, else nothing."""
        if not self._prompt_sent or self._settled or self._abort_sent:
            return []
        self._abort_sent = True
        return [self._command("abort", now)]

    def summary(self) -> dict[str, str | int | bool]:
        """The flat, bounded, redacted summary for ``execution.json``."""
        return {
            "resolved_provider": bounded(self._resolved["provider"]),
            "resolved_model_id": bounded(self._resolved["id"]),
            "resolved_thinking": bounded(self._resolved["thinking"]),
            "prompt_disposition": bounded(self._disposition),
            "stop_reason": bounded(self._stop_reason),
            "agent_ends": self._agent_ends,
            "will_retry": self._will_retry,
            "auto_retries": self._auto_retries,
            "auto_retry_failed": self._retry_failure is not None,
            "tool_executions": self._tool_executions,
            "ui_dialogs_cancelled": self._dialogs_cancelled,
            "ui_notifications_ignored": self._notifications_ignored,
            "unknown_events": self._unknown_events,
            "abort_sent": self._abort_sent,
            "failure": self.failure or "",
        }

    # -- internals -----------------------------------------------------------
    def _command(self, command: str, now: float, **fields: object) -> bytes:
        request_id = self._new_id()
        self._pending[request_id] = _Pending(command, now)
        return _encode({"id": request_id, "type": command, **fields})

    def _fail(self, reason: str) -> None:
        if not self.done:
            self.failure = bounded(reason, MAX_REASON_CHARS)

    def _protocol(self, what: str) -> None:
        self._fail(f"pi: protocol violation: {what}")

    def _stage(self) -> str:
        if not self._prompt_sent:
            return "before answering get_state and get_available_models"
        if not self._prompt_started:
            return "before answering the prompt"
        if not self._settled:
            return "before agent_settled"
        return "before answering get_last_assistant_text"

    def _response(self, record: dict, now: float) -> list[bytes]:
        request_id = record.get("id")
        if request_id is None and record.get("command") == "parse":
            self._protocol(f"Pi could not parse a command: {bounded(record.get('error'))}")
            return []
        pending = self._pending.pop(request_id, None) if isinstance(request_id, str) else None
        if pending is None:
            self._protocol("a response to an id AutoForge never sent (or already answered)")
            return []
        command = record.get("command")
        if command is not None and command != pending.command:
            self._protocol(f"the response to {pending.command} names command {bounded(command)!r}")
            return []
        if self.done:
            return []
        success = record.get("success")
        if not isinstance(success, bool):
            self._protocol(f"the response to {pending.command} has no boolean 'success'")
            return []
        if pending.command == "abort":
            return []
        error = bounded(record.get("error"))
        if not success:
            if pending.command == "prompt":
                self._fail(f"pi: prompt rejected before acceptance: {error or 'no reason given'}")
            else:
                self._protocol(f"{pending.command} failed: {error or 'no reason given'}")
            return []
        data = record.get("data")
        if pending.command == "get_state":
            self._check_state(data)
        elif pending.command == "get_available_models":
            self._check_models(data)
        elif pending.command == "prompt":
            self._check_disposition(data)
        elif pending.command == "get_last_assistant_text":
            self._check_text(data)
        return self._advance(now)

    def _check_state(self, data: object) -> None:
        if not isinstance(data, dict):
            self._protocol("the get_state response has no 'data' object")
            return
        model = data.get("model")
        if model is None:
            self._fail(f"pi: model unavailable: Pi resolved no model for {self._model}")
            return
        if not isinstance(model, dict):
            self._protocol("get_state 'model' is not an object")
            return
        provider, model_id = model.get("provider"), model.get("id")
        if not isinstance(provider, str) or not isinstance(model_id, str):
            self._protocol("get_state 'model' lacks a string 'provider' or 'id'")
            return
        self._resolved["provider"], self._resolved["id"] = provider, model_id
        if (provider, model_id) != (self._provider, self._model_id):
            self._fail(
                f"pi: model mismatch: configured {self._model}, Pi resolved "
                f"{bounded(provider)}/{bounded(model_id)}"
            )
            return
        level = data.get("thinkingLevel")
        if not isinstance(level, str):
            self._protocol("get_state lacks a string 'thinkingLevel'")
            return
        self._resolved["thinking"] = level
        if level != self._thinking:
            self._fail(
                f"pi: thinking mismatch: configured {self._thinking}, Pi resolved "
                f"{bounded(level)} (set an effort this model supports)"
            )
            return
        self._state_checked = True

    def _check_models(self, data: object) -> None:
        models = data.get("models") if isinstance(data, dict) else None
        if not isinstance(models, list):
            self._protocol("the get_available_models response has no 'models' list")
            return
        self._models = [(m.get("provider"), m.get("id")) for m in models if isinstance(m, dict)]

    def _check_disposition(self, data: object) -> None:
        disposition = data.get("disposition") if isinstance(data, dict) else None
        if not isinstance(disposition, str):
            self._protocol("the prompt response has no 'disposition'")
            return
        self._disposition = disposition
        if disposition != "started":
            # AutoForge sent one prompt into an idle session: `queued` means it
            # was not idle, `handled` that an extension or template consumed it.
            self._protocol(f"prompt disposition {bounded(disposition)!r}, expected 'started'")
            return
        self._prompt_started = True
        self._send(ProgressEvent(ProgressKind.STARTED, detail=self._model))

    def _check_text(self, data: object) -> None:
        if not isinstance(data, dict):
            self._protocol("the get_last_assistant_text response has no 'data' object")
            return
        text = data.get("text")
        if not isinstance(text, str) or not js_trim(text):
            self._fail("pi: no assistant text: the run ended without a final answer")
            return
        if text != self._derived_text():
            self._protocol(
                "get_last_assistant_text differs from the text of the last assistant message_end"
            )
            return
        self.text = text

    def _derived_text(self) -> str | None:
        message = self._last_text_message
        content = message.get("content") if message is not None else None
        if not isinstance(content, list):
            return None
        joined = "".join(
            block["text"]
            for block in content
            if isinstance(block, dict)
            and block.get("type") == "text"
            and isinstance(block.get("text"), str)
        )
        return js_trim(joined) or None

    def _advance(self, now: float) -> list[bytes]:
        """Send what the conversation is ready for next."""
        if self.done:
            return []
        if not self._prompt_sent:
            if not self._state_checked or self._models is None:
                return []
            if (self._provider, self._model_id) not in self._models:
                self._fail(
                    f"pi: model unavailable: {self._model} is not among Pi's available models "
                    f"(an id outside Pi's catalog, or no credential for {self._provider!r})"
                )
                return []
            self._prompt_sent = True
            return [self._command("prompt", now, message=self._prompt)]
        if not (self._prompt_started and self._settled):
            return []
        if any(p.command == "get_last_assistant_text" for p in self._pending.values()):
            return []
        if not self._classify_end():
            return []
        return [self._command("get_last_assistant_text", now)]

    def _classify_end(self) -> bool:
        """After settling: whether the run ended in an answer worth reading."""
        if self._retry_failure is not None:
            self._fail(self._retry_failure)
            return False
        message = self._last_assistant
        if message is None:
            self._fail("pi: no assistant text: the run settled without an assistant message")
            return False
        stop = message.get("stopReason")
        self._stop_reason = stop if isinstance(stop, str) else ""
        if stop in SUCCESS_STOP_REASONS:
            return True
        if stop == "length":
            # Pi drops a cut-off answer from its context (verified on Pi 1.0.1),
            # so get_last_assistant_text cannot return it.
            self._fail("pi: the model hit its output limit (stopReason length)")
        elif stop == "error":
            self._fail(_model_error("model error after acceptance", message.get("errorMessage")))
        elif stop == "aborted":
            self._fail("pi: the run was aborted, not by AutoForge")
        else:
            self._protocol(f"final stopReason {bounded(stop)!r} is not a completed answer")
        return False

    def _ui_request(self, record: dict) -> list[bytes]:
        method = record.get("method")
        if not isinstance(method, str):
            # An array or object is unhashable: test the type before membership.
            self._protocol("an extension UI request without a string 'method'")
            return []
        if method in NOTIFY_METHODS:
            self._notifications_ignored += 1
            return []
        request_id = record.get("id")
        if method not in DIALOG_METHODS or not isinstance(request_id, str):
            self._protocol(f"an extension UI request AutoForge cannot answer ({bounded(method)!r})")
            return []
        # Answered at once, whatever state the run is in: an unanswered
        # `editor` would wait forever.
        self._dialogs_cancelled += 1
        self._fail(f"pi: an extension asked for input ({method}); the dialog was cancelled")
        return [_encode({"type": "extension_ui_response", "id": request_id, "cancelled": True})]

    def _send(self, event: ProgressEvent) -> None:
        if self._emit is not None:
            self._emit(event)

    def _progress(self, kind: str, record: dict) -> None:
        """Report one event record as progress (module docstring)."""
        if self._emit is None:
            return
        if kind == "tool_execution_start":
            name = record.get("toolName")
            name = name if isinstance(name, str) else ""
            args = record.get("args")
            detail = ""
            arg = TOOL_DETAILS.get(name)
            if arg is not None and isinstance(args, dict):
                value = args.get(arg)
                if arg in _PATH_ARGS:
                    detail = relative_path(value, self._cwd)
                elif isinstance(value, str):
                    detail = value
            self._send(ProgressEvent(ProgressKind.TOOL_STARTED, tool=name, detail=detail))
        elif kind == "tool_execution_end":
            name = record.get("toolName")
            failed = record.get("isError") is True
            self._send(
                ProgressEvent(
                    ProgressKind.TOOL_FAILED if failed else ProgressKind.TOOL_FINISHED,
                    tool=name if isinstance(name, str) else "",
                )
            )
        elif kind == "message_update":
            update = record.get("assistantMessageEvent")
            step = update.get("type") if isinstance(update, dict) else None
            if isinstance(step, str) and step.startswith("thinking"):
                self._send(ProgressEvent(ProgressKind.THINKING))
            elif isinstance(step, str) and step.startswith("text"):
                self._send(ProgressEvent(ProgressKind.ASSISTANT_TEXT))
            else:
                self._send(ProgressEvent(ProgressKind.ACTIVITY))
        elif kind == "auto_retry_start":
            attempt, limit = record.get("attempt"), record.get("maxAttempts")
            detail = ""
            if isinstance(attempt, int) and not isinstance(attempt, bool):
                detail = f"attempt {attempt}"
                if isinstance(limit, int) and not isinstance(limit, bool):
                    detail += f" of {limit}"
            self._send(ProgressEvent(ProgressKind.PROVIDER_RETRY, detail=detail))
        else:
            self._send(ProgressEvent(ProgressKind.ACTIVITY))

    def _event(self, kind: str, record: dict, now: float) -> list[bytes]:
        if kind == "agent_settled":
            if self._prompt_sent:
                self._settled = True
                return self._advance(now)
        elif kind == "agent_end":
            self._agent_ends += 1
            if record.get("willRetry") is True:
                self._will_retry += 1
        elif kind == "message_end":
            message = record.get("message")
            if isinstance(message, dict) and message.get("role") == "assistant":
                self._last_assistant = message
                stop = message.get("stopReason")
                content = message.get("content")
                # Pi's own reading skips an aborted message with no content.
                if not (stop == "aborted" and not content):
                    self._last_text_message = message
                if stop in SUCCESS_STOP_REASONS:
                    self._retry_failure = None
        elif kind == "tool_execution_start":
            self._tool_executions += 1
        elif kind == "auto_retry_start":
            self._auto_retries += 1
        elif kind == "auto_retry_end":
            if record.get("success") is False:
                self._retry_failure = _model_error(
                    "automatic retries failed", record.get("finalError")
                )
            else:
                self._retry_failure = None
        return []


def _model_error(what: str, message: object) -> str:
    if isinstance(message, str) and _USAGE_LIMIT_CODE in message:
        return "pi: subscription usage limit reached"
    detail = bounded(message, MAX_REASON_CHARS - 60)
    return f"pi: {what}: {detail or 'no error message'}"
