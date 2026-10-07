"""Detect an agent stuck in a loop: output still flows, nothing new happens (#194).

#193 kills an agent that stops producing output. An agent that keeps
producing output while it repeats itself resets that timer forever; this
module decides when that is the case. It is pure and provider-neutral:
adapters and the executor's readers feed it monotonic times with tool names
and opaque digests, output bytes and provider retries. It reads no clock,
keeps no tool input, result or output line, and no digest leaves it.

A loop is defined over what the agent does and what comes back -- the same
action, with the same input and the same result, repeated with no new action
in between -- never over repeated text alone. Four signals:

* ``action_cycle`` (A). Every completed tool call is one action, which its
  adapter fingerprints as (tool name, digest of the full canonical input,
  digest of the full result) with :func:`digest` and
  :func:`action_fingerprint`. A cycle of period ``p <= max_cycle_period``
  repeated at least ``max_cycle_repeats`` times consecutively at the tail of
  the action sequence is a loop. Re-running a test after each edit is not:
  the edits in between differ.
* ``no_novelty`` (B). For ``novelty_window_seconds`` no action appeared that
  the invocation had not already seen, while at least ``max_cycle_period *
  max_cycle_repeats`` replayed actions fell inside the window. Below that
  floor the window is inconclusive, so one long tool call (#193's concern,
  the idle limit) is never read as a loop.
* ``repeated_lines`` (C). Unstructured output (OpenCode's stderr, a text
  profile's stdout) is cut into lines at LF and at CR; each is normalized
  (escape sequences dropped, hex and digit runs masked, whitespace collapsed)
  and hashed. A line, or a cycle of up to ``max_cycle_period`` lines,
  repeated ``max_line_repeats`` times consecutively is a loop. What a CR
  alone ends is redrawn in place by what follows (a progress bar, a
  spinner): a display, not a line, so it is dropped, while CRLF ends a line
  like LF. A line with no letter left once pytest's progress marks are
  dropped (blank, a rule, a pytest dot line) says nothing that could repeat
  as an action and is skipped.
* ``retry_storm`` (D). At least ``max_cycle_repeats`` provider retries in a
  row, spanning ``novelty_window_seconds``, with no completed turn or action
  in between; only an adapter that can tell a retry reports one.

Each signal has a severity: the fraction of its threshold reached, 1 or more
being conclusive. :class:`LoopMonitor` applies ``execution.loop_detection``:
from half a threshold it warns, again at most every
:data:`WARN_INTERVAL_SECONDS` and once more when the finding becomes
conclusive, never on every event; in ``kill`` mode it returns the first
conclusive finding as the verdict its caller ends the invocation on. In every
mode, ``off`` included, it keeps the calibration figures
(:meth:`LoopMonitor.calibration`) the default thresholds are to be set from.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from .config import LoopDetectionConfig
from .progress import MAX_TOOL_CHARS, clean

SIGNAL_ACTION_CYCLE = "action_cycle"
SIGNAL_NO_NOVELTY = "no_novelty"
SIGNAL_REPEATED_LINES = "repeated_lines"
SIGNAL_RETRY_STORM = "retry_storm"

MODE_KILL = "kill"
MODE_WARN = "warn"
MODE_OFF = "off"

# ``timeout_limit`` / ``ended_by`` of an invocation the detector ended, next
# to the executor's ``idle`` and ``max_runtime``.
LIMIT_LOOP = "loop"

# What :class:`LoopReport` says the detector did about its finding.
ACTION_KILLED = "killed"
ACTION_WARNED = "warned"

DIGEST_BYTES = 16
# Distinct actions remembered for ``no_novelty``. Past it an unseen action
# counts as new without being remembered: the bound errs toward no loop.
MAX_SEEN_ACTIONS = 65_536
# A line is hashed from its first bytes; the rest is dropped as it arrives.
MAX_LINE_BYTES = 1024
# Tool names a ``no_novelty`` finding lists.
MAX_FINDING_TOOLS = 8
# A finding is warned about from this fraction of its threshold on.
WARN_AT = 0.5
# The shortest gap between two warnings, the length of the progress
# heartbeat: a loop that goes on is mentioned again, an event stream is not
# echoed.
WARN_INTERVAL_SECONDS = 120.0

_LINE_END = re.compile(rb"\r\n|\r|\n")
# The escape sequences ``progress.clean`` removes: a colour code is not text.
_ESCAPES = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]"
    r"|\x9b[0-?]*[ -/]*[@-~]"
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?"
    r"|\x1b[PX^_][^\x1b]*(?:\x1b\\)?"
    r"|\x1b[ -/]*[0-~]"
)
# A hex run is a word of hex digits holding at least one decimal digit (a
# commit, an address, a request id): "deadbeef" alone could be a word.
_HEX_RUN = re.compile(r"\b(?:0[xX])?(?=[0-9a-fA-F]*[0-9])[0-9a-fA-F]+\b")
_DIGIT_RUN = re.compile(r"[0-9]+")
_SPACE_RUN = re.compile(r"\s+")
_LETTER = re.compile(r"[^\W\d_]")
# A word of pytest's per-test status characters with a dot among them
# (``..F..s..``): a progress mark, not text.
_PYTEST_MARKS = re.compile(r"(?<!\S)[.FEsxX]*\.[.FEsxX]*(?!\S)")


def digest(value: object) -> bytes:
    """A digest of ``value``'s canonical JSON: sorted keys, no whitespace.

    What an adapter keeps of a tool input or result: the same content always
    gives the same bytes, and nothing of the content can be read back. A
    value that cannot be serialized gets a random digest, so it never makes
    two actions look the same.
    """
    try:
        text = json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=repr
        )
        data = text.encode("utf-8", "surrogatepass")
    except (TypeError, ValueError, RecursionError):
        return os.urandom(DIGEST_BYTES)
    return hashlib.blake2b(data, digest_size=DIGEST_BYTES).digest()


def action_fingerprint(name: str, input_digest: bytes, result_digest: bytes) -> bytes:
    """One completed tool call: its name, its input's digest and its result's."""
    h = hashlib.blake2b(digest_size=DIGEST_BYTES)
    for part in (name.encode("utf-8", "surrogatepass"), input_digest, result_digest):
        h.update(len(part).to_bytes(4, "big"))
        h.update(part)
    return h.digest()


def line_key(raw: bytes) -> bytes | None:
    """The digest of one normalized output line; ``None`` for a line with no letter.

    Escape sequences are dropped, hex runs and then digit runs become ``#``
    and whitespace collapses, so ``retrying in 5s (attempt 37)`` and
    ``retrying in 6s (attempt 38)`` are the same line. Whether it has a letter
    is judged without pytest's progress marks.
    """
    text = _ESCAPES.sub("", raw.decode("utf-8", "replace"))
    text = _DIGIT_RUN.sub("#", _HEX_RUN.sub("#", text))
    text = _SPACE_RUN.sub(" ", text).strip()
    if not _LETTER.search(_PYTEST_MARKS.sub("", text)):
        return None
    return hashlib.blake2b(text.encode("utf-8"), digest_size=DIGEST_BYTES).digest()


def span_text(seconds: float) -> str:
    """``9m14s``: how long a pattern lasted."""
    total = max(0, int(seconds))
    if total < 60:
        return f"{total}s"
    if total < 3600:
        return f"{total // 60}m{total % 60:02d}s"
    return f"{total // 3600}h{total % 3600 // 60:02d}m"


@dataclass(frozen=True)
class LoopFinding:
    """One signal's view of the tail of the invocation.

    ``repeats``: complete consecutive copies of the cycle (A, C), replayed
    actions inside the window (B) or retries in a row (D). ``period``: the
    cycle's length (A, C; 0 otherwise). ``tools``: the cycle's tool names in
    order (A) or the tools replayed (B), cleaned. ``stream``: the output
    stream (C). ``started`` and ``at``: monotonic times the pattern began and
    was last seen. ``severity``: the fraction of the threshold reached.
    """

    signal: str
    repeats: int
    period: int = 0
    tools: tuple[str, ...] = ()
    stream: str = ""
    started: float = 0.0
    at: float = 0.0
    severity: float = 0.0

    @property
    def conclusive(self) -> bool:
        return self.severity >= 1.0

    def describe(self) -> str:
        """The pattern, for the error text and ``execution.json``."""
        span = span_text(self.at - self.started)
        tools = ", ".join(self.tools)
        if self.signal == SIGNAL_ACTION_CYCLE:
            what = (
                f"the same action ({tools})"
                if self.period == 1
                else f"the same {self.period}-step action cycle ({tools})"
            )
            return f"{what} repeated {self.repeats} times over {span} with no new action"
        if self.signal == SIGNAL_NO_NOVELTY:
            return (
                f"no new action for {span}: the last {self.repeats} actions ({tools}) "
                "all repeated earlier ones"
            )
        if self.signal == SIGNAL_REPEATED_LINES:
            what = (
                f"the same {self.stream} line"
                if self.period == 1
                else f"the same {self.period}-line {self.stream} cycle"
            )
            return f"{what} repeated {self.repeats} times over {span} with no new line"
        return f"{self.repeats} provider retries in a row over {span} with no completed turn"

    def warning(self) -> str:
        """The short form, for a progress line."""
        tools = ", ".join(self.tools)
        if self.signal == SIGNAL_ACTION_CYCLE:
            return f"{self.period}-step cycle ({tools}) repeated {self.repeats}×"
        if self.signal == SIGNAL_NO_NOVELTY:
            span = span_text(self.at - self.started)
            return f"no new action for {span}, {self.repeats} replayed ({tools})"
        if self.signal == SIGNAL_REPEATED_LINES:
            if self.period == 1:
                return f"{self.stream} line repeated {self.repeats}×"
            return f"{self.period}-line {self.stream} cycle repeated {self.repeats}×"
        span = span_text(self.at - self.started)
        return f"{self.repeats} provider retries in a row over {span}"


@dataclass(frozen=True)
class LoopReport:
    """What the detector did in one invocation: the finding it killed for
    (``killed``), or the strongest it warned about (``warned``)."""

    action: str
    finding: LoopFinding
    warnings: int
    # The monotonic time of the launch: the origin of the record's offsets.
    origin: float

    def describe(self) -> str:
        return self.finding.describe()

    def record(self) -> dict[str, object]:
        """The ``loop`` entry of ``execution.json``: names, counts and times only.

        No tool input, result, output line or digest: the description is
        built from the cleaned tool names and the counts.
        """
        finding = self.finding
        return {
            "signal": finding.signal,
            "action": self.action,
            "period": finding.period,
            "repeats": finding.repeats,
            "tools": list(finding.tools),
            "stream": finding.stream,
            "started_after_seconds": round(max(0.0, finding.started - self.origin), 1),
            "detected_after_seconds": round(max(0.0, finding.at - self.origin), 1),
            "description": finding.describe(),
        }


@dataclass(frozen=True)
class _Run:
    period: int
    repeats: int
    started: float
    names: tuple[str, ...]


class _CycleTracker:
    """The repetition at the tail of a sequence, for every period up to a bound.

    For each period ``p`` it counts how many items in a row equalled the item
    ``p`` before them; a run of ``r`` such items is ``1 + r // p`` complete
    copies of a ``p``-item cycle. When a run begins, the time and the names
    of its first copy are kept, so a finding can say when it started and
    what the cycle is. O(max_period) per item.
    """

    def __init__(self, max_period: int) -> None:
        self._max_period = max_period
        self._tail: deque[tuple[bytes, str, float]] = deque(maxlen=max_period)
        self._run = [0] * (max_period + 1)
        self._first: list[tuple[float, tuple[str, ...]]] = [(0.0, ())] * (max_period + 1)

    def feed(self, key: bytes, name: str, now: float) -> _Run | None:
        """Add one item; the strongest repetition now at the tail, if any.

        Among equal repeat counts the shortest period wins, so ``A A A A``
        is a 1-step cycle repeated 4 times, not a 2-step one repeated twice.
        """
        tail = self._tail
        best: _Run | None = None
        for p in range(1, self._max_period + 1):
            if len(tail) < p or tail[-p][0] != key:
                self._run[p] = 0
                continue
            if self._run[p] == 0:
                copy = list(tail)[-p:]
                self._first[p] = (copy[0][2], tuple(item[1] for item in copy))
            self._run[p] += 1
            repeats = 1 + self._run[p] // p
            if repeats >= 2 and (best is None or repeats > best.repeats):
                started, names = self._first[p]
                best = _Run(p, repeats, started, names)
        tail.append((key, name, now))
        return best


class _LineFramer:
    """Cut one stream's bytes into lines, keeping each line's head.

    LF and CRLF end a line; what a CR alone ends is redrawn in place and is
    dropped. A CR that ends a chunk is decided by the next one, so a CRLF
    split across two reads still ends a line.
    """

    def __init__(self) -> None:
        self._head = bytearray()
        self._cr = False

    def feed(self, data: bytes) -> list[bytes]:
        lines: list[bytes] = []
        if self._cr:
            self._cr = False
            if data.startswith(b"\n"):
                lines.append(bytes(self._head))
                data = data[1:]
            self._head.clear()
        pos = 0
        for match in _LINE_END.finditer(data):
            self._take(data[pos : match.start()])
            pos = match.end()
            if match.group() == b"\r":
                if pos == len(data):
                    self._cr = True
                    return lines
            else:
                lines.append(bytes(self._head))
            self._head.clear()
        self._take(data[pos:])
        return lines

    def _take(self, part: bytes) -> None:
        room = MAX_LINE_BYTES - len(self._head)
        if room > 0:
            self._head += part[:room]


class LoopObserver(Protocol):
    """What a clock-free protocol reducer reports to the loop detector.

    The driver that owns the reducer supplies the time (see
    ``providers._ClockedLoop``), so the reducer still reads no clock.
    """

    def action(self, name: str, fingerprint: bytes) -> None:
        """A tool call completed (see :func:`action_fingerprint`)."""

    def retry(self) -> None:
        """The provider retried a request."""

    def turn(self) -> None:
        """The provider completed a turn."""


Warn = Callable[[str], None]


class LoopMonitor:
    """One invocation's loop detector, under ``execution.loop_detection``.

    Feed it with :meth:`action`, :meth:`retry`, :meth:`turn` and
    :meth:`output`, each with the monotonic time the caller saw the event.
    Each returns the verdict once, when in ``kill`` mode a signal first
    becomes conclusive, and ``None`` otherwise; the caller then ends the
    invocation. Warnings go to ``warn`` (a progress line); one that raises is
    not called again. Thread-safe: an executor feeds it from its two reader
    threads.
    """

    def __init__(
        self,
        config: LoopDetectionConfig,
        origin: float,
        warn: Warn | None = None,
        *,
        warn_interval: float = WARN_INTERVAL_SECONDS,
    ) -> None:
        self.mode = config.mode
        self._period = config.max_cycle_period
        self._cycle_repeats = config.max_cycle_repeats
        self._window = float(config.novelty_window_seconds)
        self._line_repeats = config.max_line_repeats
        self._floor = config.max_cycle_period * config.max_cycle_repeats
        self._origin = origin
        self._warn = warn
        self._warn_interval = warn_interval
        self._lock = threading.Lock()
        # Signal A.
        self._cycles = _CycleTracker(self._period)
        # Signal B: what was seen, when something last was new, and the
        # replays since (their times, bounded by the floor, and their names).
        self._seen: set[bytes] = set()
        self._novel_at: float | None = None
        self._replayed = 0
        self._replay_times: deque[float] = deque(maxlen=self._floor)
        self._replay_names: dict[str, None] = {}
        # Signal C, per stream.
        self._streams: dict[str, tuple[_LineFramer, _CycleTracker]] = {}
        # Signal D.
        self._retries = 0
        self._retry_start = 0.0
        # Calibration, whatever the mode.
        self._actions = 0
        self._max_cycle = (0, 0)
        self._quiet_seconds = 0.0
        self._quiet_actions = 0
        self._max_lines = 0
        self._max_retries = 0
        # Outcome.
        self._verdict: LoopFinding | None = None
        self._strongest: LoopFinding | None = None
        self._warnings = 0
        self._warned_at: float | None = None
        self._warned_conclusive = False

    # -- feeding -------------------------------------------------------------
    def action(self, name: str, fingerprint: bytes, now: float) -> LoopFinding | None:
        """A completed tool call (see :func:`action_fingerprint`)."""
        with self._lock:
            tool = clean(name, MAX_TOOL_CHARS) or "tool"
            self._actions += 1
            self._retries = 0
            run = self._cycles.feed(fingerprint, tool, now)
            cycle = None
            if run is not None:
                if run.repeats > self._max_cycle[0]:
                    self._max_cycle = (run.repeats, run.period)
                cycle = LoopFinding(
                    SIGNAL_ACTION_CYCLE,
                    run.repeats,
                    run.period,
                    run.names,
                    started=run.started,
                    at=now,
                    severity=run.repeats / self._cycle_repeats,
                )
            return self._consider(now, cycle, self._novelty(tool, fingerprint, now))

    def retry(self, now: float) -> LoopFinding | None:
        """The provider retried a request (an API error, a rate limit)."""
        with self._lock:
            if self._retries == 0:
                self._retry_start = now
            self._retries += 1
            self._max_retries = max(self._max_retries, self._retries)
            span = now - self._retry_start
            storm = LoopFinding(
                SIGNAL_RETRY_STORM,
                self._retries,
                started=self._retry_start,
                at=now,
                severity=min(self._retries / self._cycle_repeats, span / self._window),
            )
            return self._consider(now, storm)

    def turn(self, now: float) -> None:
        """The provider completed a turn: a retry streak, if any, is over."""
        with self._lock:
            self._retries = 0

    def output(self, stream: str, data: bytes, now: float) -> LoopFinding | None:
        """A chunk of unstructured output on ``stream``; hashed line by line and dropped."""
        with self._lock:
            framer, lines = self._streams.setdefault(
                stream, (_LineFramer(), _CycleTracker(self._period))
            )
            verdict = None
            for raw in framer.feed(data):
                key = line_key(raw)
                if key is None:
                    continue
                run = lines.feed(key, stream, now)
                if run is None:
                    continue
                self._max_lines = max(self._max_lines, run.repeats)
                severity = run.repeats / self._line_repeats
                if severity < WARN_AT:
                    continue
                finding = LoopFinding(
                    SIGNAL_REPEATED_LINES,
                    run.repeats,
                    run.period,
                    stream=stream,
                    started=run.started,
                    at=now,
                    severity=severity,
                )
                verdict = self._consider(now, finding) or verdict
            return verdict

    # -- results -------------------------------------------------------------
    @property
    def verdict(self) -> LoopFinding | None:
        """The finding the invocation is to be killed for, in ``kill`` mode."""
        with self._lock:
            return self._verdict

    @property
    def warnings(self) -> int:
        with self._lock:
            return self._warnings

    def report(self, *, killed: bool) -> LoopReport | None:
        """The verdict, else the strongest finding warned about; ``None`` when neither.

        ``killed``: whether the caller ended the invocation on the verdict.
        A child that exited on its own just as the verdict came was not
        killed, so its verdict is reported as a finding only.
        """
        with self._lock:
            if self._verdict is not None:
                action = ACTION_KILLED if killed else ACTION_WARNED
                return LoopReport(action, self._verdict, self._warnings, self._origin)
            if self._strongest is not None:
                return LoopReport(ACTION_WARNED, self._strongest, self._warnings, self._origin)
            return None

    def calibration(self) -> dict[str, str | int | bool]:
        """How close the invocation came to each threshold, for ``provider_summary``.

        Recorded in every mode, so real runs show how far legitimate work
        stays from the thresholds: the most consecutive copies of an action
        cycle (and its period), the longest stretch with no new action (in
        seconds and in replayed actions), the most consecutive copies of a
        line cycle and the longest provider retry streak.
        """
        with self._lock:
            return {
                "loop_actions": self._actions,
                "loop_max_cycle_repeats": self._max_cycle[0],
                "loop_max_cycle_period": self._max_cycle[1],
                "loop_longest_novelty_free_seconds": int(self._quiet_seconds),
                "loop_longest_novelty_free_actions": self._quiet_actions,
                "loop_max_line_repeats": self._max_lines,
                "loop_max_retry_streak": self._max_retries,
            }

    # -- internals -----------------------------------------------------------
    def _novelty(self, tool: str, fingerprint: bytes, now: float) -> LoopFinding | None:
        if fingerprint not in self._seen:
            if len(self._seen) < MAX_SEEN_ACTIONS:
                self._seen.add(fingerprint)
            self._novel_at = now
            self._replayed = 0
            self._replay_times.clear()
            self._replay_names.clear()
            return None
        novel_at = self._novel_at if self._novel_at is not None else now
        self._replayed += 1
        self._replay_times.append(now)
        if tool not in self._replay_names and len(self._replay_names) < MAX_FINDING_TOOLS:
            self._replay_names[tool] = None
        stretch = now - novel_at
        self._quiet_seconds = max(self._quiet_seconds, stretch)
        self._quiet_actions = max(self._quiet_actions, self._replayed)
        since = now - self._window
        in_window = 0
        for at in reversed(self._replay_times):
            if at < since:
                break
            in_window += 1
        return LoopFinding(
            SIGNAL_NO_NOVELTY,
            in_window,
            tools=tuple(self._replay_names),
            started=novel_at,
            at=now,
            severity=min(stretch / self._window, in_window / self._floor),
        )

    def _consider(self, now: float, *candidates: LoopFinding | None) -> LoopFinding | None:
        """Warn about, or return as the verdict, the strongest of ``candidates``."""
        found = [f for f in candidates if f is not None and f.severity >= WARN_AT]
        if not found or self.mode == MODE_OFF or self._verdict is not None:
            return None
        conclusive = [f for f in found if f.conclusive]
        # The candidates come in signal order, the most specific first: a
        # cycle names its steps, which a stretch with no novelty cannot.
        finding = conclusive[0] if conclusive else max(found, key=lambda f: f.severity)
        if self.mode == MODE_KILL and finding.conclusive:
            self._verdict = finding
            return finding
        if self._strongest is None or finding.severity >= self._strongest.severity:
            self._strongest = finding
        due = (
            self._warned_at is None
            or now - self._warned_at >= self._warn_interval
            or (finding.conclusive and not self._warned_conclusive)
        )
        if due:
            self._warned_at = now
            self._warnings += 1
            if finding.conclusive:
                self._warned_conclusive = True
                text = f"loop detected, not killed (mode {self.mode}): {finding.warning()}"
            else:
                text = f"possible loop: {finding.warning()}"
            self._say(text)
        return None

    def _say(self, text: str) -> None:
        warn = self._warn
        if warn is None:
            return
        try:
            warn(text)
        except Exception:
            # A warning is observability; it never changes the outcome.
            self._warn = None
