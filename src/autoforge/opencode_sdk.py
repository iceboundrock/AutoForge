"""The OpenCode SDK bridge's envelope (ADR 0007 §5, #152): records in, records out, one outcome.

:class:`BridgeConversation` is the whole of one ``opencode-sdk`` invocation
as a pure reducer, as :class:`autoforge.pi_rpc.PiConversation` is for Pi. The
caller (:class:`autoforge.providers.OpenCodeSdkProvider`) feeds it every
record the bridge writes on stdout, as bytes, with the current
``time.monotonic()``, writes back the records it returns, and hands each ask
it surfaces to the launch's interaction broker. Nothing here starts a
process, reads a clock, sees a policy or knows an SDK or HTTP name: it knows
the envelope ``autoforge-opencode-bridge``, protocol 1, and nothing else, so
its tests drive it from synthetic records.

The envelope, in order (the bridge, ``helpers/opencode-bridge/src/bridge.mjs``,
is its other end):

1. ``hello`` (bridge): its envelope and protocol, which must be exactly this
   module's, the SDK version it was built with, which must be the pinned one
   (D4.2), and the Node version, which must reach the pinned floor (D4.4).
   Only then is ``start`` sent: the server command by absolute path, the
   caller's session and input ids, the mode (``new``, ``continue`` or
   ``recover``), the prompt, the working directory, the agent, the model and
   its reasoning variant, the runtime version the server must report, the
   policy's start fields and the bridge's limits.
2. ``server`` (bridge): the version the server reports, compared with the
   pin before any session call (D4.2).
3. ``session`` (bridge): the session read back, which must be the one asked
   for (id, mode, directory, agent, model, variant; D9.5).
4. ``admitted`` (bridge): the input read back under its caller id, ``sent``
   false when an identical input was already there and was not sent again.
5. ``event`` (bridge): filtered observations (status, text, reasoning, a
   step, a completed message, a tool's start and end by name and digests, a
   session error, asks a reject settled), made live progress and fed to the
   loop detector; an unknown ``kind`` is counted, never fatal (D10.4).
6. ``ask`` (bridge): a question or a permission of the bound session,
   normalized here into :class:`BridgeAsk` for the broker; a ``decision``
   carries the broker's directive back. An ask that cannot be rendered,
   repeats a provider id, or arrives out of place fails closed: the
   decision is ``fail`` and the invocation fails.
7. ``result`` (bridge): the final assistant text of the admitted input, the
   only text that reaches ``stdout`` (D10.3).
8. ``end`` or ``failure`` (bridge): the outcome and what became of the
   server, with the bridge's counts. ``interrupt`` (controller) asks the
   bridge to abort the turn; end of file on its stdin is the shutdown
   request (D3.3).

Every string that leaves this module for a log (a failure reason, a summary
value) is bounded, made encodable, redacted and put on one line here. Raw
records, prompts and message contents other than the final text never leave
it.
"""

from __future__ import annotations

import enum
import json
import re
import secrets
from collections.abc import Mapping
from dataclasses import dataclass, field

from .errors import InteractionDataError
from .interaction import (
    Allow,
    AllowScope,
    Answer,
    Cancel,
    Deny,
    Directive,
    Fail,
    Pause,
    Question,
    QuestionOption,
    RequestKind,
)
from .loop_detect import LoopMonitor, action_fingerprint
from .pi_rpc import bounded, encodable
from .progress import ProgressEvent, ProgressKind, ProgressSink, relative_path
from .sdk_policy import input_digest, shown

ENVELOPE = "autoforge-opencode-bridge"
PROTOCOL = 1
PREFIX = "opencode-sdk"
MAX_REASON_CHARS = 400


class BridgeMode(enum.StrEnum):
    """What the bridge does with the caller's session (ADR 0007 §5)."""

    # Create the session under the caller's id (it must not exist) and admit the input.
    NEW = "new"
    # Admit the input into the existing session (#126's continuation, D9.6).
    CONTINUE = "continue"
    # Admit nothing: read the admitted input's final answer from the history (D9.9).
    RECOVER = "recover"


# The bridge's limits (its ``start.limits``), in seconds or bytes. The record
# bound is the duplex handle's, so neither side writes what the other drops.
@dataclass(frozen=True)
class BridgeLimits:
    startup_seconds: int = 60
    request_seconds: int = 30
    settle_seconds: int = 30
    abort_seconds: int = 10
    stop_seconds: int = 5
    max_record_bytes: int = 33 * 1024 * 1024
    max_event_bytes: int = 16 * 1024 * 1024
    max_response_bytes: int = 64 * 1024 * 1024
    max_messages: int = 5000

    def as_dict(self) -> dict[str, int]:
        return {
            "startup_seconds": self.startup_seconds,
            "request_seconds": self.request_seconds,
            "settle_seconds": self.settle_seconds,
            "abort_seconds": self.abort_seconds,
            "stop_seconds": self.stop_seconds,
            "max_record_bytes": self.max_record_bytes,
            "max_event_bytes": self.max_event_bytes,
            "max_response_bytes": self.max_response_bytes,
            "max_messages": self.max_messages,
        }


_BASE62 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
_ID_TIME_MASK = 0xFFFFFFFFFFFF


def opencode_id(prefix: str, now_ms: int, *, descending: bool = False, counter: int = 0) -> str:
    """An id in OpenCode's own format: ``<prefix>_``, twelve hex digits of the
    time (``now_ms * 0x1000 + counter``, its low 48 bits, inverted for a
    descending id), then fourteen random base-62 characters.

    A caller-chosen id must sort where the runtime's own would: its history
    orders a session's messages by id, so an input id that sorted before the
    answers already there would not read as the latest input. Session ids
    descend, message ids ascend, as the runtime makes them.
    """
    value = (now_ms * 0x1000 + counter) & _ID_TIME_MASK
    if descending:
        value = ~value & _ID_TIME_MASK
    tail = "".join(secrets.choice(_BASE62) for _ in range(14))
    return f"{prefix}_{value:012x}{tail}"


def split_model(model: str) -> tuple[str, str]:
    """``provider/model`` as ``(provider id, model id)``; the profile's model
    was validated (``sdk_policy.opencode_model_problem``)."""
    provider, _, model_id = model.partition("/")
    return provider, model_id


@dataclass(frozen=True)
class BridgeStart:
    """What one launch asks the bridge to do: its ``start`` record."""

    server_command: str
    session_id: str
    mode: BridgeMode
    input_id: str
    prompt: str
    cwd: str
    agent: str
    provider_id: str
    model_id: str
    variant: str | None
    runtime_version: str
    policy: Mapping[str, object]
    limits: BridgeLimits = field(default_factory=BridgeLimits)

    def record(self) -> dict[str, object]:
        return {
            "type": "start",
            "server_command": self.server_command,
            "session_id": self.session_id,
            "mode": str(self.mode),
            "input_id": self.input_id,
            "prompt": self.prompt,
            "cwd": self.cwd,
            "agent": self.agent,
            "model": {
                "provider_id": self.provider_id,
                "model_id": self.model_id,
                "variant": self.variant,
            },
            "expect": {"runtime_version": self.runtime_version},
            "policy": dict(self.policy),
            "limits": self.limits.as_dict(),
        }


@dataclass(frozen=True)
class BridgeAsk:
    """One ask of the bound session, normalized for the broker.

    ``request_id`` is the bridge's (what a decision names); a question
    carries ``questions``; a permission its ``permission`` and ``patterns``
    (its identity, ADR 0007 D9.6), a ``summary`` to show and the
    ``input_sha256`` a decision is bound to.
    """

    request_id: str
    kind: RequestKind
    provider_request_id: str
    session_id: str
    questions: tuple[Question, ...] = ()
    permission: str = ""
    patterns: tuple[str, ...] = ()
    summary: str = ""
    input_sha256: str = ""


def encode(record: Mapping[str, object]) -> bytes:
    """One controller record: ASCII JSON, so neither a raw LF nor a U+2028
    reaches the wire, and a lone surrogate is escaped."""
    return json.dumps(record, ensure_ascii=True, separators=(",", ":")).encode("ascii")


def decision_record(ask: BridgeAsk, directive: Directive) -> dict[str, object]:
    """The ``decision`` record that carries ``directive`` for ``ask``.

    An answer to a question carries, per question, the labels picked and
    the operator's own words after them. An approval is ``once``: a session
    approval would be an ``always`` the policy never sends, and a modified
    input no reply carries (ADR 0007 D9.6); the broker offers neither, and
    this refuses either should one arrive. A denial is ``reject``, with the
    guidance as the feedback the model reads; a cancellation a bare
    ``reject``; a pause leaves the ask unanswered; a failure rejects it.
    """
    record: dict[str, object] = {"type": "decision", "request_id": ask.request_id}
    if isinstance(directive, Pause):
        record["directive"] = "pause"
        return record
    if isinstance(directive, Fail):
        record["directive"] = "fail"
        return record
    decision = directive.decision
    if ask.kind is RequestKind.QUESTION:
        if isinstance(decision, Answer):
            answers: list[list[str]] = []
            for response in decision.responses:
                picked = list(response.choices)
                if response.custom_text is not None:
                    picked.append(response.custom_text)
                answers.append(picked)
            record["directive"] = "answer"
            record["answers"] = answers
            return record
        if isinstance(decision, Cancel):
            record["directive"] = "reject"
            return record
    else:
        if isinstance(decision, Allow):
            if decision.scope is not AllowScope.ONCE or decision.updated_input is not None:
                raise InteractionDataError(
                    "an OpenCode approval is once and carries no modified input"
                )
            record["directive"] = "once"
            return record
        if isinstance(decision, Deny):
            record["directive"] = "reject"
            if decision.guidance is not None:
                record["message"] = decision.guidance
            return record
        if isinstance(decision, Cancel):
            record["directive"] = "reject"
            return record
    raise InteractionDataError(f"a {ask.kind} takes no {type(decision).__name__}")


_REQUEST_ID = re.compile(r"[1-9][0-9]{0,8}")
_VERSION = re.compile(r"([0-9]+)\.([0-9]+)\.([0-9]+)")
# The bridge's own failure codes; any other is shown as ``unknown``.
_FAILURE_CODES = frozenset(
    {
        "protocol",
        "startup",
        "pair",
        "stream",
        "session",
        "admission",
        "request",
        "interaction",
        "completion",
        "model",
        "bounds",
        "stopped",
        "internal",
    }
)
_SERVER_STOPS = frozenset({"sigterm", "sigkill", "exited", "survived", "never_started"})
_STATUS = frozenset({"busy", "idle", "retry"})
_EVENT_KINDS = frozenset(
    {"status", "text", "reasoning", "step", "message", "tool", "error", "asks_settled"}
)
# A failed model call the runtime marks retryable with one of these statuses
# is a transient failure of the provider (#164), as for the CLI providers.
_TRANSIENT_STATUSES = frozenset({408, 500, 502, 503, 504, 529})


def _version(text: object) -> tuple[int, ...] | None:
    match = _VERSION.fullmatch(text) if isinstance(text, str) else None
    return tuple(int(part) for part in match.groups()) if match else None


class BridgeConversation:
    """One bridge invocation as a reducer. Not thread-safe; one driver owns it.

    ``feed`` takes one stdout record and returns the records to write back and
    the ask, if the record was one, that the driver must take to the broker;
    :meth:`decided` turns the broker's directive into the reply. The
    conversation is over when :attr:`done`: :attr:`text` holds the final
    assistant text on success, :attr:`failure` the bounded reason otherwise,
    :attr:`interrupted` whether the turn was interrupted instead.
    """

    def __init__(
        self,
        start: BridgeStart,
        *,
        sdk_version: str,
        node_minimum: tuple[int, ...],
        emit: ProgressSink | None = None,
        loop: LoopMonitor | None = None,
    ) -> None:
        self._start = start
        self._sdk_version = sdk_version
        self._node_minimum = node_minimum
        self._emit = emit
        self._loop = loop
        self.failure: str | None = None
        # The bridge's own report of a failed model call, for the transient check.
        self.model_error: dict[str, object] | None = None
        self.text: str | None = None
        self.interrupted = False
        self.done = False
        # Whether the bridge exited without its last record (a crash or a kill).
        self.exited_early = False
        self._stage = "hello"
        self._started = False
        self._admitted = False
        self._sent = False
        self._result: dict[str, object] | None = None
        self._asks: dict[str, BridgeAsk] = {}
        self._provider_asks: set[str] = set()
        self._answered: set[str] = set()
        self._interrupt_sent = False
        self._counts = {
            "events": 0,
            "unknown_events": 0,
            "tools": 0,
            "asks": 0,
            "asks_settled_by_reject": 0,
        }
        self._bridge_counts: dict[str, int] = {}
        self.server: dict[str, object] = {}
        self.runtime_version = ""
        self.node_version = ""
        self.session_id = ""
        self.message_id = ""
        self.finish = ""

    # -- the driver's calls ----------------------------------------------------
    @property
    def admitted(self) -> bool:
        """Whether the input was admitted (read back under its id)."""
        return self._admitted

    def feed(self, data: bytes, now: float) -> tuple[list[bytes], BridgeAsk | None]:
        if self.done:
            return [], None
        try:
            record = json.loads(data)
        except (ValueError, UnicodeDecodeError):
            self._protocol("a record is not JSON")
            return [], None
        if not isinstance(record, dict) or not isinstance(record.get("type"), str):
            self._protocol("a record has no type")
            return [], None
        kind = record["type"]
        handler = {
            "hello": self._hello,
            "server": self._server,
            "session": self._session,
            "admitted": self._admitted_record,
            "event": self._event,
            "ask": self._ask,
            "result": self._result_record,
            "end": self._end,
            "failure": self._failure_record,
        }.get(kind)
        if handler is None:
            self._protocol(f"unknown record type {bounded(kind, 40)!r}")
            return [], None
        return handler(record, now)

    def decided(self, ask: BridgeAsk, directive: Directive) -> list[bytes]:
        """The reply carrying the broker's ``directive`` for ``ask``; a
        :class:`Fail` also fails the invocation with its reason (D7.6)."""
        if ask.request_id in self._answered or self._asks.get(ask.request_id) is not ask:
            self._protocol("a decision for an ask that is not open")
            return []
        self._answered.add(ask.request_id)
        try:
            record = decision_record(ask, directive)
        except InteractionDataError as exc:
            record = {"type": "decision", "request_id": ask.request_id, "directive": "fail"}
            self._fail(f"{PREFIX}: the decision cannot be carried: {exc}")
        if isinstance(directive, Fail):
            self._fail(directive.reason)
        return [encode(record)]

    def refused(self, ask: BridgeAsk, reason: str) -> list[bytes]:
        """Reject ``ask`` and fail the invocation: an ask the broker could not
        take (its data refused), never offered to anyone (D10.4)."""
        return self.decided(ask, Fail(reason))

    def interrupt(self) -> list[bytes]:
        """Ask the bridge to abort the turn (once)."""
        if self._interrupt_sent or not self._started:
            return []
        self._interrupt_sent = True
        return [encode({"type": "interrupt"})]

    def oversize(self, head: bytes, limit: int) -> None:
        self._protocol(f"a record is larger than {limit} bytes")

    def framing_error(self, what: str) -> None:
        self._protocol(what)

    def stream_ended(self) -> None:
        """stdout ended before the bridge's last record."""
        if not self.done:
            self.exited_early = True
            self._fail(f"{PREFIX}: the bridge exited without its last record ({self._stage})")

    def stdin_closed(self) -> None:
        if not self.done:
            self._fail(f"{PREFIX}: the bridge closed its stdin while the invocation ran")

    def deadline_reached(self) -> None:
        """A limit cut the invocation short: whatever the protocol said is not read."""
        self.done = True

    def summary(self) -> dict[str, str | int | bool]:
        """A flat, bounded account for ``provider_summary``: the pair, the
        session and input ids, the counts, and what became of the server."""
        summary: dict[str, str | int | bool] = {
            "envelope": f"{ENVELOPE}/{PROTOCOL}",
            "stage": self._stage,
            "mode": str(self._start.mode),
            "session_id": self._start.session_id,
            "input_id": self._start.input_id,
            "admitted": self._admitted,
            "input_sent": self._sent,
            **self._counts,
        }
        if self.runtime_version:
            summary["runtime_version"] = self.runtime_version
        if self.node_version:
            summary["node_version"] = self.node_version
        if self.message_id:
            summary["message_id"] = self.message_id
            summary["finish"] = self.finish
        if self.interrupted:
            summary["interrupted"] = True
        for key in ("stopped", "exit_code", "signal"):
            value = self.server.get(key)
            if isinstance(value, (str, int)) and not isinstance(value, bool):
                summary[f"server_{key}"] = bounded(value, 40) if isinstance(value, str) else value
        for key, value in self._bridge_counts.items():
            summary[f"bridge_{key}"] = value
        return summary

    # -- records ------------------------------------------------------------------
    def _hello(self, record: dict, now: float) -> tuple[list[bytes], BridgeAsk | None]:
        if self._stage != "hello":
            return self._out_of_place("hello")
        if record.get("envelope") != ENVELOPE or record.get("protocol") != PROTOCOL:
            self._fail(
                f"{PREFIX}: the helper speaks {bounded(record.get('envelope'), 60) or '?'} "
                f"protocol {bounded(str(record.get('protocol')), 10)}, not {ENVELOPE} {PROTOCOL}"
            )
            return [], None
        sdk = record.get("sdk_version")
        if sdk != self._sdk_version:
            self._fail(
                f"{PREFIX}: SDK pair mismatch: the bridge was built with @opencode-ai/sdk "
                f"{bounded(sdk, 40) or '?'}, not the pinned {self._sdk_version}"
            )
            return [], None
        node = _version(record.get("node_version"))
        self.node_version = bounded(record.get("node_version"), 40)
        if node is None or node < self._node_minimum:
            floor = ".".join(str(part) for part in self._node_minimum)
            self._fail(
                f"{PREFIX}: the bridge runs on Node {self.node_version or '?'}, below the "
                f"supported {floor} (ADR 0007 D4.4)"
            )
            return [], None
        self._stage = "start"
        self._started = True
        self._send(ProgressEvent(ProgressKind.STARTED))
        return [encode(self._start.record())], None

    def _server(self, record: dict, now: float) -> tuple[list[bytes], BridgeAsk | None]:
        if self._stage != "start":
            return self._out_of_place("server")
        version = record.get("runtime_version")
        self.runtime_version = bounded(version, 40)
        self._stage = "server"
        if version != self._start.runtime_version:
            self._fail(
                f"{PREFIX}: runtime pair mismatch: the server reports opencode-ai "
                f"{self.runtime_version or '?'}, not the pinned {self._start.runtime_version}"
            )
        return [], None

    def _session(self, record: dict, now: float) -> tuple[list[bytes], BridgeAsk | None]:
        if self._stage != "server":
            return self._out_of_place("session")
        start = self._start
        expected = {
            "session_id": start.session_id,
            "mode": str(start.mode),
            "directory": start.cwd,
            "agent": start.agent,
            "provider_id": start.provider_id,
            "model_id": start.model_id,
            "variant": start.variant,
        }
        wrong = sorted(key for key, value in expected.items() if record.get(key) != value)
        if wrong:
            self._fail(f"{PREFIX}: the session is not the one asked for ({', '.join(wrong)})")
            return [], None
        self.session_id = start.session_id
        self._stage = "session"
        return [], None

    def _admitted_record(self, record: dict, now: float) -> tuple[list[bytes], BridgeAsk | None]:
        if self._stage != "session" or self._start.mode is BridgeMode.RECOVER:
            return self._out_of_place("admitted")
        sent = record.get("sent")
        if record.get("input_id") != self._start.input_id or not isinstance(sent, bool):
            self._protocol("the admitted input is not the one sent")
            return [], None
        self._admitted = True
        self._sent = sent
        self._stage = "running"
        return [], None

    def _event(self, record: dict, now: float) -> tuple[list[bytes], BridgeAsk | None]:
        if self._stage not in ("session", "running"):
            return self._out_of_place("event")
        self._counts["events"] += 1
        kind = record.get("kind")
        if kind not in _EVENT_KINDS:
            # An observation of a kind this module does not know: counted (D10.4).
            self._counts["unknown_events"] += 1
            self._send(ProgressEvent(ProgressKind.ACTIVITY))
            return [], None
        if kind == "status":
            status = record.get("status")
            if status not in _STATUS:
                self._protocol("an unknown session status")
                return [], None
            if status == "retry":
                attempt = record.get("attempt")
                detail = ""
                if isinstance(attempt, int) and not isinstance(attempt, bool):
                    detail = f"attempt {attempt}"
                self._send(ProgressEvent(ProgressKind.PROVIDER_RETRY, detail=detail))
                if self._loop is not None:
                    self._loop.retry(now)
            else:
                self._send(ProgressEvent(ProgressKind.ACTIVITY))
        elif kind == "text":
            self._send(ProgressEvent(ProgressKind.ASSISTANT_TEXT))
        elif kind == "reasoning":
            self._send(ProgressEvent(ProgressKind.THINKING))
        elif kind == "message":
            if record.get("finish") in ("stop", "length", "tool-calls") and self._loop is not None:
                self._loop.turn(now)
            self._send(ProgressEvent(ProgressKind.ACTIVITY))
        elif kind == "tool":
            self._tool(record, now)
        elif kind == "asks_settled":
            count = record.get("count")
            if isinstance(count, int) and not isinstance(count, bool) and count > 0:
                self._counts["asks_settled_by_reject"] += count
            self._send(ProgressEvent(ProgressKind.ACTIVITY))
        else:
            self._send(ProgressEvent(ProgressKind.ACTIVITY))
        return [], None

    def _tool(self, record: dict, now: float) -> None:
        name = record.get("tool")
        name = name if isinstance(name, str) else ""
        status = record.get("status")
        if status == "running":
            self._counts["tools"] += 1
            detail = record.get("detail")
            detail = relative_path(detail, self._start.cwd) if isinstance(detail, str) else ""
            self._send(ProgressEvent(ProgressKind.TOOL_STARTED, tool=name, detail=detail))
        elif status in ("completed", "error"):
            self._send(
                ProgressEvent(
                    ProgressKind.TOOL_FINISHED
                    if status == "completed"
                    else ProgressKind.TOOL_FAILED,
                    tool=name,
                )
            )
            inputs, result = record.get("input_sha256"), record.get("result_sha256")
            if (
                self._loop is not None
                and isinstance(inputs, str)
                and isinstance(result, str)
                and re.fullmatch(r"[0-9a-f]{64}", inputs)
                and re.fullmatch(r"[0-9a-f]{64}", result)
            ):
                self._loop.action(
                    name,
                    action_fingerprint(name, bytes.fromhex(inputs), bytes.fromhex(result)),
                    now,
                )
        else:
            self._counts["unknown_events"] += 1

    def _ask(self, record: dict, now: float) -> tuple[list[bytes], BridgeAsk | None]:
        request_id = record.get("request_id")
        if not isinstance(request_id, str) or not _REQUEST_ID.fullmatch(request_id):
            self._protocol("an ask without a request id")
            return [], None
        if request_id in self._asks:
            self._protocol("an ask repeats a request id")
            return [], None
        if self._stage != "running":
            self._fail(f"{PREFIX}: an ask arrived before the input was admitted")
            return [
                encode({"type": "decision", "request_id": request_id, "directive": "fail"})
            ], None
        self._counts["asks"] += 1
        provider_id = record.get("provider_request_id")
        session_id = record.get("session_id")
        kind = record.get("kind")
        refuse = ""
        if not isinstance(provider_id, str) or not provider_id:
            refuse = "an ask without the provider's id"
        elif provider_id in self._provider_asks:
            refuse = "the same ask arrived twice"
        elif session_id != self._start.session_id:
            # Child sessions are not attributed until gate G-O9 passes.
            refuse = "an ask from a session other than the bound one"
        elif kind not in ("question", "permission"):
            refuse = "an ask of an unknown kind"
        ask: BridgeAsk | None = None
        if not refuse:
            try:
                ask = self._normalize(request_id, record)
            except InteractionDataError as exc:
                refuse = f"an ask the broker cannot render ({exc})"
        if ask is None:
            self._fail(f"{PREFIX}: {refuse}; it was rejected")
            return [
                encode({"type": "decision", "request_id": request_id, "directive": "fail"})
            ], None
        self._provider_asks.add(ask.provider_request_id)
        self._asks[request_id] = ask
        return [], ask

    def _normalize(self, request_id: str, record: dict) -> BridgeAsk:
        provider_id = str(record["provider_request_id"])
        if record["kind"] == "question":
            raw = record.get("questions")
            if not isinstance(raw, list):
                raise InteractionDataError("questions is not a list")
            questions = []
            for item in raw:
                if not isinstance(item, dict):
                    raise InteractionDataError("a question is not an object")
                options = item.get("options")
                if not isinstance(options, list):
                    raise InteractionDataError("a question's options are not a list")
                multiple, custom = item.get("multiple"), item.get("custom")
                questions.append(
                    Question(
                        text=item.get("question"),  # type: ignore[arg-type]
                        header=item.get("header", ""),
                        options=tuple(
                            QuestionOption(
                                label=option.get("label") if isinstance(option, dict) else None,  # type: ignore[arg-type]
                                description=(
                                    option.get("description", "")
                                    if isinstance(option, dict)
                                    else ""
                                ),
                            )
                            for option in options
                        ),
                        # The runtime's own defaults (1.18.34's ``QuestionInfo``):
                        # one pick unless ``multiple``, and an answer of the
                        # operator's own words unless ``custom`` is false; the
                        # model cannot set ``custom`` (the tool's input has none).
                        multiple=multiple is True,
                        custom=custom is not False,
                    )
                )
            return BridgeAsk(
                request_id=request_id,
                kind=RequestKind.QUESTION,
                provider_request_id=provider_id,
                session_id=self._start.session_id,
                questions=tuple(questions),
            )
        permission = record.get("permission")
        patterns = record.get("patterns")
        metadata = record.get("metadata_sha256")
        if not isinstance(permission, str) or not permission.strip():
            raise InteractionDataError("a permission without its name")
        if not isinstance(patterns, list) or not all(isinstance(p, str) for p in patterns):
            raise InteractionDataError("a permission's patterns are not strings")
        if not isinstance(metadata, str) or not re.fullmatch(r"[0-9a-f]{64}", metadata):
            raise InteractionDataError("a permission without its metadata digest")
        identity = {"permission": permission, "patterns": patterns, "metadata_sha256": metadata}
        return BridgeAsk(
            request_id=request_id,
            kind=RequestKind.PERMISSION,
            provider_request_id=provider_id,
            session_id=self._start.session_id,
            permission=permission,
            patterns=tuple(patterns),
            summary=shown("patterns: " + ", ".join(patterns)) if patterns else "",
            input_sha256=input_digest(identity, "a permission's identity"),
        )

    def _result_record(self, record: dict, now: float) -> tuple[list[bytes], BridgeAsk | None]:
        start = self._start
        if self._result is not None:
            self._protocol("a second result")
            return [], None
        recovering = start.mode is BridgeMode.RECOVER and self._stage == "session"
        if self._stage != "running" and not recovering:
            return self._out_of_place("result")
        text = record.get("text")
        finish = record.get("finish")
        message = record.get("message_id")
        if (
            record.get("session_id") != start.session_id
            or record.get("input_id") != start.input_id
            or not isinstance(text, str)
            or not isinstance(message, str)
            or finish not in ("stop", "length")
        ):
            self._protocol("the result is not the admitted input's final answer")
            return [], None
        self._result = record
        self.message_id = bounded(message, 128)
        self.finish = str(finish)
        self._stage = "result"
        return [], None

    def _end(self, record: dict, now: float) -> tuple[list[bytes], BridgeAsk | None]:
        self._take_counts(record)
        outcome = record.get("outcome")
        if outcome == "result":
            if self._result is None:
                self._protocol("the bridge ended with a result it never sent")
                return [], None
            if self._interrupt_sent:
                # The turn finished before the abort reached it; the controller
                # asked for it to stop, so the answer is not taken.
                self.interrupted = True
            else:
                text = self._result.get("text")
                self.text = encodable(text) if isinstance(text, str) else None
        elif outcome == "interrupted":
            self.interrupted = True
        else:
            self._protocol("the bridge ended with an unknown outcome")
            return [], None
        self.done = True
        return [], None

    def _failure_record(self, record: dict, now: float) -> tuple[list[bytes], BridgeAsk | None]:
        self._take_counts(record)
        code = record.get("code")
        code = code if code in _FAILURE_CODES else "unknown"
        reason = bounded(record.get("reason"), MAX_REASON_CHARS - 60) or "no reason given"
        details = record.get("details")
        if isinstance(details, dict) and code == "model":
            self.model_error = {
                key: details.get(key)
                for key in ("error", "status", "retryable")
                if isinstance(details.get(key), (str, int, bool))
            }
        self._fail(f"{PREFIX}: {reason} ({code})")
        return [], None

    def _take_counts(self, record: dict) -> None:
        server = record.get("server")
        if isinstance(server, dict):
            stopped = server.get("stopped")
            self.server = {
                "stopped": stopped if stopped in _SERVER_STOPS else "unknown",
                "exit_code": server.get("exit_code")
                if isinstance(server.get("exit_code"), int)
                else None,
                "signal": bounded(server.get("signal"), 20) or None,
            }
        for key in ("events", "skipped_unknown", "foreign_events", "asks", "sibling_settled"):
            value = record.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                self._bridge_counts[key] = value

    def transient(self) -> str | None:
        """A fixed reason when the failure is a model call the runtime marked
        retryable with a transient status (#164); never text from the output."""
        error = self.model_error or {}
        status = error.get("status")
        if (
            error.get("retryable") is True
            and isinstance(status, int)
            and not isinstance(status, bool)
            and status in _TRANSIENT_STATUSES
        ):
            return f"{PREFIX}: model API error {status}"
        return None

    # -- helpers -----------------------------------------------------------------------
    def _send(self, event: ProgressEvent) -> None:
        if self._emit is not None:
            self._emit(event)

    def _out_of_place(self, what: str) -> tuple[list[bytes], BridgeAsk | None]:
        self._protocol(f"a {what} record arrived out of place ({self._stage})")
        return [], None

    def _protocol(self, what: str) -> None:
        self._fail(f"{PREFIX}: protocol violation: {what}")

    def _fail(self, reason: str) -> None:
        if self.failure is None:
            self.failure = bounded(reason, MAX_REASON_CHARS)
