"""Live agent progress: a provider-neutral event and its terminal renderer (#192).

A provider adapter maps its own protocol (Claude's stream-json, Pi's RPC
events, OpenCode's stderr) into :class:`ProgressEvent` and hands each one to
the :data:`ProgressSink` the engine put in the request. The engine and the
CLI never see a provider record: an event is a closed ``kind``, a tool name,
one allow-listed ``detail`` and a token estimate, and nothing else. The kinds
are the subset of #154's shared lifecycle events that a CLI provider can
report today (started, tool started/finished/failed, provider retry) plus
the liveness kinds a stream adds (thinking, assistant text, activity); #154
extends this set rather than defining a second one.

Every string an event carries is made safe when the event is built, not
where it is printed: ANSI escape sequences, control, format and line
separator characters are removed first, so a secret split by an escape is
whole again; the text is then redacted; and only then clipped, because a
secret cut by the clip would no longer match its pattern. A value too long
to be a path, a pattern or a description (:data:`MAX_RAW_CHARS`) is not
shown at all. A detail is one of the allow-listed tool inputs an adapter
names (a file path, a search pattern, a command's *description*), never a
command line, a tool result, thinking or assistant text.

:class:`ProgressReporter` turns events into lines for the operator. Each
line is prefixed with the time elapsed since the launch and a label (phase
and issue), is redacted again as a whole, and goes to every output: the
step's ``progress.log`` and, when the CLI asks for it, stderr. Thinking and
assistant text are coalesced to one line per :data:`THINKING_INTERVAL_SECONDS`;
a finished tool and bare activity print nothing; and when nothing has been
printed for :data:`HEARTBEAT_AFTER_SECONDS` a heartbeat line says how long
ago the agent last did anything. The reporter never raises into the
adapter or the engine: an output that fails is dropped and never called
again, with one line saying so on the others; an output that fails while
receiving that line is dropped in turn.
"""

from __future__ import annotations

import enum
import re
import threading
import time
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from types import TracebackType

from .redaction import redact


class ProgressKind(enum.StrEnum):
    """What happened. Closed: an adapter maps anything else to ``ACTIVITY``."""

    STARTED = "started"
    THINKING = "thinking"
    ASSISTANT_TEXT = "assistant_text"
    TOOL_STARTED = "tool_started"
    TOOL_FINISHED = "tool_finished"
    TOOL_FAILED = "tool_failed"
    PROVIDER_RETRY = "provider_retry"
    # Any other sign of life: a record that is none of the above.
    ACTIVITY = "activity"


# A raw value longer than this is no path, pattern or description an
# operator needs to read, and redacting it would cost work proportional to
# whatever an agent put in a tool input; it is shown as an ellipsis.
MAX_RAW_CHARS = 4096
MAX_TOOL_CHARS = 64
MAX_DETAIL_CHARS = 160
MAX_LABEL_CHARS = 80
MAX_LINE_CHARS = 400
MAX_TOKENS = 10**12
ELLIPSIS = "…"

# ANSI/VT escape sequences: CSI (7-bit and the C1 byte), OSC up to BEL or
# ST (or the end, when unterminated), DCS/SOS/PM/APC strings, and the
# two-character escapes. What is left of a broken sequence is removed with
# the other controls below.
_ESCAPES = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]"
    r"|\x9b[0-?]*[ -/]*[@-~]"
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?"
    r"|\x1b[PX^_][^\x1b]*(?:\x1b\\)?"
    r"|\x1b[ -/]*[0-~]"
)
_SPACES = re.compile(r"\s+")


def clean(value: object, limit: int) -> str:
    """``value`` as one safe, bounded line: stripped, redacted, then clipped.

    A non-string is empty; a string longer than :data:`MAX_RAW_CHARS` is an
    ellipsis. Escape sequences go first, then every control (C0, C1, DEL),
    format (bidirectional overrides, zero-width characters) and line or
    paragraph separator character; whitespace controls become spaces. The
    result is redacted and only then clipped to ``limit`` characters.
    """
    if not isinstance(value, str) or limit <= 0:
        return ""
    if len(value) > MAX_RAW_CHARS:
        return ELLIPSIS
    text = _ESCAPES.sub("", value)
    kept: list[str] = []
    for ch in text:
        category = unicodedata.category(ch)
        if category == "Cc":
            if ch in "\t\n\r\v\f":
                kept.append(" ")
        elif category in ("Zl", "Zp"):
            kept.append(" ")
        elif category == "Cs":
            kept.append("�")
        elif category != "Cf":
            kept.append(ch)
    text = _SPACES.sub(" ", redact("".join(kept))).strip()
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + ELLIPSIS
    return text


@dataclass(frozen=True)
class ProgressEvent:
    """One provider-neutral progress event, made safe on construction.

    ``tool`` is the tool's name; ``detail`` is the one allow-listed input
    the adapter chose for it; ``tokens`` an adapter's token estimate (0 when
    it has none); ``at`` the monotonic time the adapter saw it.
    """

    kind: ProgressKind
    tool: str = ""
    detail: str = ""
    tokens: int = 0
    at: float = field(default_factory=time.monotonic)

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", ProgressKind(self.kind))
        object.__setattr__(self, "tool", clean(self.tool, MAX_TOOL_CHARS))
        object.__setattr__(self, "detail", clean(self.detail, MAX_DETAIL_CHARS))
        tokens = self.tokens
        if isinstance(tokens, bool) or not isinstance(tokens, int) or tokens < 0:
            tokens = 0
        object.__setattr__(self, "tokens", min(tokens, MAX_TOKENS))


ProgressSink = Callable[[ProgressEvent], None]


def guarded(sink: ProgressSink | None) -> ProgressSink | None:
    """``sink`` wrapped so that it can never fail the invocation reporting to it.

    The first exception the sink raises drops it for the rest of the
    invocation; progress is observability, never part of the outcome.
    """
    if sink is None:
        return None
    live = [sink]

    def emit(event: ProgressEvent) -> None:
        if not live:
            return
        try:
            live[0](event)
        except Exception:
            live.clear()

    return emit


def relative_path(path: object, cwd: str) -> str:
    """``path`` relative to ``cwd`` when it lies under it; ``""`` when it is no string."""
    if not isinstance(path, str):
        return ""
    if cwd:
        base = cwd.rstrip("/") + "/"
        if path.startswith(base) and len(path) > len(base):
            return path[len(base) :]
    return path


THINKING_INTERVAL_SECONDS = 30.0
HEARTBEAT_AFTER_SECONDS = 120.0
HEARTBEAT_POLL_SECONDS = 5.0
# How long closing the reporter waits for its heartbeat thread.
_JOIN_SECONDS = 10.0

Output = Callable[[str], None]


def _clock_text(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def _ago(seconds: float) -> str:
    total = max(0, int(seconds))
    if total < 60:
        return f"{total}s"
    if total < 3600:
        return f"{total // 60}m{total % 60:02d}s"
    return f"{total // 3600}h{total % 3600 // 60:02d}m"


def _tokens_text(tokens: int) -> str:
    if tokens >= 1000:
        return f"~{tokens // 1000}k tokens"
    return f"~{tokens} tokens"


class ProgressReporter:
    """Renders one invocation's progress events as prefixed lines.

    Use as a context manager around the launch: entering starts the
    heartbeat thread, leaving stops and joins it, so nothing it starts
    outlives the invocation. :meth:`sink` is the :data:`ProgressSink` handed
    to the adapter and may be called from any thread; lines are written
    under one lock, in order, to every output.
    """

    def __init__(
        self,
        label: str,
        outputs: Sequence[Output],
        *,
        clock: Callable[[], float] = time.monotonic,
        thinking_interval: float = THINKING_INTERVAL_SECONDS,
        heartbeat_after: float = HEARTBEAT_AFTER_SECONDS,
        poll_seconds: float = HEARTBEAT_POLL_SECONDS,
    ) -> None:
        self._label = clean(label, MAX_LABEL_CHARS)
        self._outputs = list(outputs)
        self._clock = clock
        self._thinking_interval = thinking_interval
        self._heartbeat_after = heartbeat_after
        self._poll_seconds = poll_seconds
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._start = clock()
        self._last_printed = self._start
        self._last_event: float | None = None
        self._last_quiet_line: float | None = None
        self._events = 0
        self._tokens = 0

    @property
    def events(self) -> int:
        """How many events the sink has received."""
        return self._events

    def __enter__(self) -> ProgressReporter:
        thread = threading.Thread(target=self._heartbeat, name="autoforge-progress", daemon=True)
        try:
            thread.start()
        except RuntimeError:
            # The system refused a thread: the launch goes on without a
            # heartbeat, since progress never decides whether an agent runs.
            self.line("no heartbeat: the progress thread could not be started")
            return self
        self._thread = thread
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        """Stop the heartbeat thread and wait for it. Idempotent."""
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(_JOIN_SECONDS)

    def line(self, text: str) -> None:
        """Print one controller line (the pre-launch facts, the end) with the prefix.

        The text is cleaned like an event's detail, since it can quote a
        provider's failure reason or a path. Never raises: progress does not
        decide whether an agent runs or what its result is.
        """
        try:
            with self._lock:
                self._emit(clean(text, MAX_LINE_CHARS), self._clock())
        except Exception:  # pragma: no cover - _emit already contains output failures
            pass

    def sink(self, event: ProgressEvent) -> None:
        """Take one event from the adapter. Never raises."""
        try:
            with self._lock:
                self._take(event, self._clock())
        except Exception:  # pragma: no cover - _emit already contains output failures
            pass

    def poll(self) -> None:
        """Print a heartbeat when nothing has been printed for the heartbeat interval."""
        with self._lock:
            now = self._clock()
            if now - self._last_printed < self._heartbeat_after:
                return
            if self._last_event is None:
                text = "still running, no activity yet"
            else:
                text = (
                    f"still running, last activity {_ago(now - self._last_event)} ago, "
                    f"{self._events} events"
                )
            self._emit(text, now)

    # -- internals -----------------------------------------------------------
    def _heartbeat(self) -> None:
        while not self._stop.wait(self._poll_seconds):
            try:
                self.poll()
            except Exception:  # pragma: no cover - _emit already contains output failures
                pass

    def _take(self, event: ProgressEvent, now: float) -> None:
        self._events += 1
        self._last_event = now
        kind = event.kind
        if kind in (ProgressKind.THINKING, ProgressKind.ASSISTANT_TEXT):
            if event.tokens:
                self._tokens = event.tokens
            last = self._last_quiet_line
            if last is not None and now - last < self._thinking_interval:
                return
            self._last_quiet_line = now
            if kind is ProgressKind.ASSISTANT_TEXT:
                self._emit("writing…", now)
            elif self._tokens:
                self._emit(f"thinking… {_tokens_text(self._tokens)}", now)
            else:
                self._emit("thinking…", now)
        elif kind is ProgressKind.TOOL_STARTED:
            self._emit(f"{event.tool or 'tool'} {event.detail}".rstrip(), now)
        elif kind is ProgressKind.TOOL_FAILED:
            self._emit(f"{event.tool or 'tool'} failed", now)
        elif kind is ProgressKind.STARTED:
            self._emit(f"agent started ({event.detail})" if event.detail else "agent started", now)
        elif kind is ProgressKind.PROVIDER_RETRY:
            self._emit(
                f"provider retry ({event.detail})" if event.detail else "provider retry", now
            )
        # TOOL_FINISHED and ACTIVITY are liveness only: counted, not printed.

    def _emit(self, text: str, now: float) -> None:
        prefix = f"[{_clock_text(now - self._start)} {self._label}]"
        line = redact(f"{prefix} {text}")
        if len(line) > MAX_LINE_CHARS:
            line = line[: MAX_LINE_CHARS - 1] + ELLIPSIS
        self._last_printed = now
        failed = self._deliver(line)
        # Each dropped output is noted on the ones still live; one that fails
        # receiving a note is dropped and noted in turn, until none fails.
        while failed:
            error = failed.pop(0)
            note = redact(
                f"{prefix} progress output dropped: "
                f"{type(error).__name__}: {clean(str(error), MAX_DETAIL_CHARS)}"
            )
            failed.extend(self._deliver(note))

    def _deliver(self, line: str) -> list[Exception]:
        """Write ``line`` to every live output; drop each that fails and return its errors."""
        kept: list[Output] = []
        failed: list[Exception] = []
        for output in self._outputs:
            try:
                output(line)
            except Exception as exc:
                failed.append(exc)
            else:
                kept.append(output)
        self._outputs = kept
        return failed
