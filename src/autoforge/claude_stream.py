"""Claude Code stream-json protocol (#192): lines in, progress out, one outcome.

:class:`ClaudeStream` is the whole of one ``claude -p --output-format
stream-json --verbose`` invocation as a pure reducer, the way
:class:`autoforge.pi_rpc.PiConversation` is Pi's. The driver
(:meth:`autoforge.providers.ClaudeCodeProvider.execute`) feeds it every
stdout line as bytes, reports a line past the per-line bound, an
unterminated last line, an overflow and the end of stdout; nothing here
starts a process, knows a CLI flag or reads a clock. The module knows
Claude's record names and nothing else, so its tests drive it from
synthetic lines with no process at all.

What a stream is (verified on claude 2.1.292, 2026-10-06): one JSON object
per line. ``system`` records (``init``, which names the model;
``thinking_tokens``, which carries ``estimated_tokens`` and is emitted
repeatedly while the model is still thinking; ``hook_started`` and
``hook_response`` from the operator's own hooks, which quote the hook's
output and are never shown), ``assistant`` messages whose content blocks
are ``thinking``, ``text`` or ``tool_use`` (``id``, ``name``, ``input``),
``user`` messages carrying ``tool_result`` blocks (``tool_use_id``,
``is_error``), ``rate_limit_event``, and a ``result`` record that ends the
turn. ``system/api_retry`` (``attempt``, ``max_retries``) is mapped too, from
the CLI's documented message set; it did not occur in the verified sample.

One invocation can run more than one turn (verified on claude 2.1.293,
2026-10-07): a background task or a scheduled wakeup still pending when a
turn ends keeps the CLI alive, and when it fires the CLI runs another turn,
from its own ``system/init`` to its own ``result`` (``result_index`` 0, 1,
...), with notices such as ``system/task_notification`` or
``command_lifecycle`` between turns. ``--output-format text`` prints only
the last turn's result text, at exit. A turn that ends while a background
agent (``Agent`` run in the background) is still running does not get its
``result`` then: the CLI holds it back, the next turn opens with its own
``system/init`` all the same, and once no background agent is left the
held results arrive together, in turn order, after the last turn's records
(verified on claude 2.1.293, 2026-10-07). A turn that ends with only a
background shell task pending gets its ``result`` at once. Either way the
n-th ``result`` belongs to the n-th turn. A background agent's own records
reach the stream too, each with the ``parent_tool_use_id`` of the call
that started the agent, but only as ``assistant`` and ``user`` records; the
agent's start and end are main-conversation notices (``system/task_*``),
and every ``system/init`` and ``result`` is the main conversation's
(verified on claude 2.1.293, 2026-10-07, with three background agents).
The turn count rests on that: a subagent's own ``system/init`` or
``result`` would throw it off.

The outcome:

* The last ``result`` record's ``result`` string is the final assistant
  text, verbatim once decoded: it becomes the invocation's stdout, so the
  CONTROL_RESULT block is parsed exactly as it was from ``--output-format
  text``. Every ``result`` is checked: ``is_error`` first (``subtype:
  success`` can come with ``is_error: true``), then ``subtype`` must be
  ``success``, then ``result`` must be a string. Anything else is a provider
  failure, also in a turn a later one would have followed.
* A line that is not a JSON object with a string ``type`` (including one
  the decoder refuses for an integer or nesting limit), an unterminated
  last line, a queue overflow, a ``result`` outside a turn (one with every
  turn so far closed), and stdout ending before every turn has its
  ``result`` are provider failures too. The first turn opens with the
  stream; a later one opens with its own ``system/init`` or, with every
  turn so far closed, with an ``assistant`` or ``user`` record, whose
  ``system/init`` is then that turn's own. A turn's ``result`` closes it.
* A line past the per-line bound is counted and skipped: a ``tool_result``
  carries whole files, and progress is not worth failing a run for. The
  ``result`` line has no such allowance; if it was the line skipped, the
  stream ends without one and fails, the way Pi's final text must arrive
  whole in one record (ADR 0003).
* Records that arrive with every turn closed and open no turn (notices
  between turns, the operator's ``SessionEnd`` hooks after the last one)
  are counted and otherwise ignored.

Progress: each record becomes at least one :class:`~autoforge.progress.ProgressEvent`
(liveness for #193), and the only strings an event carries are the model
name and one allow-listed input per tool (:data:`TOOL_DETAILS`) -- never a
command line, a tool result, thinking or assistant text. The summary is
flat bounded scalars from the ``result`` records (no nested ``usage`` or
``modelUsage``): the last one's, with ``num_turns`` and ``duration_ms``,
which each ``result`` reports for its own turn, summed over all of them, and
their count; ``total_cost_usd`` is the session's so far, so the last one's
is the invocation's. The cost is integer micro-USD, and a value that is
absent or unreadable is left out rather than reported as zero. So is a sum
once one ``result`` lacks its count: what the others add up to is not the
invocation's total.

Loop detection (#194): given a :class:`~autoforge.loop_detect.LoopObserver`,
each completed tool call is reported as an action fingerprinted from its
name, the digest of its whole ``input`` (kept from the ``tool_use`` until
its ``tool_result`` arrives) and the digest of the whole ``tool_result``
block but its ``tool_use_id``; ``system/api_retry`` is a retry and an
``assistant`` record a completed turn. Only names and digests leave here.
"""

from __future__ import annotations

import json
import math
from decimal import ROUND_HALF_UP, Decimal

from .loop_detect import LoopObserver, action_fingerprint, digest
from .progress import ProgressEvent, ProgressKind, ProgressSink, clean, relative_path

# The one tool input shown per tool. A tool not listed shows its name only.
# Bash shows its ``description``, never its ``command``: a command line is
# where credentials are typed, and redaction is a baseline, not a proof.
TOOL_DETAILS: dict[str, str] = {
    "Read": "file_path",
    "Edit": "file_path",
    "MultiEdit": "file_path",
    "Write": "file_path",
    "NotebookEdit": "notebook_path",
    "Grep": "pattern",
    "Glob": "pattern",
    "Bash": "description",
    "Task": "description",
    "Agent": "description",
}
_PATH_INPUTS = frozenset({"file_path", "notebook_path"})
# Record types that are activity only. Any other type is counted as unknown
# (forward compatibility) and is activity too.
_ACTIVITY_TYPES = frozenset({"rate_limit_event"})
# Per-turn counts in a ``result``, summed over the invocation's turns.
_PER_TURN_COUNTS = ("num_turns", "duration_ms")
# Tool calls whose result has not arrived yet, kept to name a failed tool
# and to fingerprint the call once it completes. Past the bound a result is
# still counted, only not named, and is no action for the loop detector.
MAX_OPEN_TOOLS = 1024
MAX_REASON_CHARS = 400
MAX_VALUE_CHARS = 120
_MAX_COUNT = 10**15
# A run that cost more than this is not a reading to believe.
_MAX_COST_USD = Decimal(1_000_000)


def _count(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= _MAX_COUNT:
        return None
    return value


def _micro_usd(value: object) -> int | None:
    """``value`` USD as integer micro-USD, or None when it is no finite, non-negative amount."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    amount = Decimal(repr(value)) if isinstance(value, float) else Decimal(value)
    if not Decimal(0) <= amount <= _MAX_COST_USD:
        return None
    return int((amount * 1_000_000).to_integral_value(rounding=ROUND_HALF_UP))


class ClaudeStream:
    """One stream-json invocation as a reducer. Not thread-safe; one driver owns it.

    When the stream is over, :attr:`text` holds the final assistant text on
    success and :attr:`failure` the bounded, redacted reason otherwise.
    """

    def __init__(
        self,
        *,
        cwd: str = "",
        emit: ProgressSink | None = None,
        loop: LoopObserver | None = None,
    ) -> None:
        self._cwd = cwd
        self._emit = emit
        self._loop = loop
        self.text: str | None = None
        self.failure: str | None = None
        # stdout ended inside a turn; the driver names the exit code.
        self.exited_early = False
        # Turns opened so far: the first with the stream, each later one by
        # its ``system/init`` or, with every turn so far closed, by a record
        # that opens one. Result n closes turn n, so a turn waits for its
        # result while ``_results < _turns``.
        self._turns = 1
        # The newest turn has had its ``system/init``; the next one opens a turn.
        self._turn_has_init = False
        self._results = 0
        # The per-turn counts summed over the results so far. A count one
        # result lacks makes its sum unknown, and it is dropped for good.
        self._per_turn: dict[str, int] = dict.fromkeys(_PER_TURN_COUNTS, 0)
        # Open tool calls: their name and, with a loop observer, their input's digest.
        self._open_tools: dict[str, tuple[str, bytes]] = {}
        self._records = 0
        self._oversize = 0
        self._unknown = 0
        self._after_result = 0
        self._tool_calls = 0
        self._tool_errors = 0
        self._retries = 0
        self._result: dict[str, str | int | bool] = {}

    # -- driver interface ----------------------------------------------------
    def feed(self, data: bytes) -> None:
        """Reduce one stdout line (without its LF)."""
        self._records += 1
        if self.failure is not None:
            return
        try:
            record = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._protocol("a stdout line is not JSON")
            return
        except (ValueError, RecursionError):
            # The decoder's own limits, which are not JSONDecodeError: an
            # integer past ``sys.get_int_max_str_digits()`` and nesting past
            # the recursion limit or the C stack, both well inside the
            # per-line bound. An interrupt is neither and still propagates.
            self._protocol("a stdout line exceeds the JSON decoder's integer or nesting limit")
            return
        if not isinstance(record, dict) or not isinstance(record.get("type"), str):
            self._protocol("a stdout line is not a JSON object with a 'type'")
            return
        kind = record["type"]
        if kind == "result":
            if self._results < self._turns:
                self._take_result(record)
            else:
                self._protocol("a result event outside a turn")
            return
        is_init = kind == "system" and record.get("subtype") == "init"
        if self._results == self._turns:
            if kind not in ("assistant", "user") and not is_init:
                self._after_result += 1
                return
            self._turns += 1
            self._turn_has_init = is_init
        elif is_init:
            if self._turn_has_init:
                self._turns += 1
            self._turn_has_init = True
        self._observe(kind, record)

    def oversize(self, limit: int) -> None:
        """A line past ``limit`` bytes was skipped by the framer."""
        self._oversize += 1
        self._send(ProgressEvent(ProgressKind.ACTIVITY))

    def framing_error(self, what: str) -> None:
        self._protocol(what)

    def stream_ended(self) -> None:
        """stdout reached EOF."""
        if self._results == self._turns or self.failure is not None:
            return
        self.exited_early = True
        if self._results:
            self._fail("claude: exited inside a turn that has no result event")
            return
        reason = "claude: exited without a result event"
        if self._oversize:
            reason += (
                f" ({self._oversize} line(s) past the per-line bound were skipped; "
                "the result must arrive whole in one line)"
            )
        self._fail(reason)

    def summary(self) -> dict[str, str | int | bool]:
        """The flat, bounded, redacted summary for ``execution.json``."""
        return {
            **self._result,
            "results": self._results,
            "records": self._records,
            "oversize_records": self._oversize,
            "unknown_events": self._unknown,
            "records_after_result": self._after_result,
            "tool_calls": self._tool_calls,
            "tool_errors": self._tool_errors,
            "api_retries": self._retries,
            "failure": self.failure or "",
        }

    # -- internals -----------------------------------------------------------
    def _send(self, event: ProgressEvent) -> None:
        if self._emit is not None:
            self._emit(event)

    def _fail(self, reason: str) -> None:
        if self.failure is None:
            self.failure = clean(reason, MAX_REASON_CHARS)
            self.text = None

    def _protocol(self, what: str) -> None:
        self._fail(f"claude: stream-json protocol violation: {what}")

    def _take_result(self, record: dict) -> None:
        self._results += 1
        subtype = record.get("subtype")
        is_error = record.get("is_error")
        summary: dict[str, str | int | bool] = {
            "result_subtype": clean(subtype, MAX_VALUE_CHARS),
            "is_error": is_error is True,
            "terminal_reason": clean(record.get("terminal_reason"), MAX_VALUE_CHARS),
            "stop_reason": clean(record.get("stop_reason"), MAX_VALUE_CHARS),
        }
        for key, total in list(self._per_turn.items()):
            value = _count(record.get(key))
            if value is None:
                del self._per_turn[key]
            else:
                self._per_turn[key] = min(total + value, _MAX_COUNT)
        summary.update(self._per_turn)
        cost = _micro_usd(record.get("total_cost_usd"))
        if cost is not None:
            summary["cost_micro_usd"] = cost
        denials = record.get("permission_denials")
        if isinstance(denials, list):
            summary["permission_denials"] = len(denials)
        self._result = summary
        result = record.get("result")
        if is_error is True:
            facts = [f"subtype {summary['result_subtype'] or '-'}"]
            if summary["terminal_reason"]:
                facts.append(f"terminal_reason {summary['terminal_reason']}")
            status = _count(record.get("api_error_status"))
            if status is not None:
                facts.append(f"api_error_status {status}")
            excerpt = clean(result, MAX_VALUE_CHARS * 2)
            self._fail(
                f"claude: the run ended in an error ({', '.join(facts)})"
                + (f": {excerpt}" if excerpt else "")
            )
        elif is_error is not False:
            self._protocol("the result event has no boolean 'is_error'")
        elif subtype != "success":
            self._fail(f"claude: the run did not succeed (subtype {clean(subtype, 60) or '-'})")
        elif not isinstance(result, str):
            self._protocol("the result event has no string 'result'")
        else:
            self.text = result

    def _observe(self, kind: str, record: dict) -> None:
        if kind == "system":
            self._system(record)
        elif kind == "assistant":
            self._assistant(record)
        elif kind == "user":
            self._user(record)
        else:
            if kind not in _ACTIVITY_TYPES:
                self._unknown += 1
            self._send(ProgressEvent(ProgressKind.ACTIVITY))

    def _system(self, record: dict) -> None:
        subtype = record.get("subtype")
        if subtype == "init":
            model = record.get("model")
            self._send(
                ProgressEvent(ProgressKind.STARTED, detail=model if isinstance(model, str) else "")
            )
        elif subtype == "thinking_tokens":
            tokens = _count(record.get("estimated_tokens")) or 0
            self._send(ProgressEvent(ProgressKind.THINKING, tokens=tokens))
        elif subtype == "api_retry":
            self._retries += 1
            if self._loop is not None:
                self._loop.retry()
            attempt, limit = _count(record.get("attempt")), _count(record.get("max_retries"))
            detail = ""
            if attempt is not None:
                detail = f"attempt {attempt}" + (f" of {limit}" if limit is not None else "")
            self._send(ProgressEvent(ProgressKind.PROVIDER_RETRY, detail=detail))
        else:
            # Hooks, status and compaction notices: liveness only. A hook's
            # output is the operator's, and is never shown.
            self._send(ProgressEvent(ProgressKind.ACTIVITY))

    @staticmethod
    def _blocks(record: dict) -> list[dict]:
        message = record.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            return []
        return [block for block in content if isinstance(block, dict)]

    def _assistant(self, record: dict) -> None:
        sent = False
        for block in self._blocks(record):
            kind = block.get("type")
            if kind == "thinking":
                self._send(ProgressEvent(ProgressKind.THINKING))
            elif kind == "text":
                self._send(ProgressEvent(ProgressKind.ASSISTANT_TEXT))
            elif kind == "tool_use":
                self._tool_use(block)
            else:
                continue
            sent = True
        if not sent:
            self._send(ProgressEvent(ProgressKind.ACTIVITY))
        if self._loop is not None:
            self._loop.turn()

    def _tool_use(self, block: dict) -> None:
        self._tool_calls += 1
        name = block.get("name")
        name = name if isinstance(name, str) else ""
        tool_id = block.get("id")
        tool_input = block.get("input")
        if isinstance(tool_id, str) and len(self._open_tools) < MAX_OPEN_TOOLS:
            self._open_tools[tool_id] = (
                name,
                digest(tool_input) if self._loop is not None else b"",
            )
        detail = ""
        field = TOOL_DETAILS.get(name)
        if field is not None and isinstance(tool_input, dict):
            value = tool_input.get(field)
            if field in _PATH_INPUTS:
                detail = relative_path(value, self._cwd)
            elif isinstance(value, str):
                detail = value
        self._send(ProgressEvent(ProgressKind.TOOL_STARTED, tool=name, detail=detail))

    def _user(self, record: dict) -> None:
        sent = False
        for block in self._blocks(record):
            if block.get("type") != "tool_result":
                continue
            tool_id = block.get("tool_use_id")
            call = self._open_tools.pop(tool_id, None) if isinstance(tool_id, str) else None
            name = call[0] if call is not None else ""
            if block.get("is_error") is True:
                self._tool_errors += 1
                self._send(ProgressEvent(ProgressKind.TOOL_FAILED, tool=name))
            else:
                self._send(ProgressEvent(ProgressKind.TOOL_FINISHED, tool=name))
            if self._loop is not None and call is not None:
                # The id differs on every call; everything else is the result.
                result = {key: value for key, value in block.items() if key != "tool_use_id"}
                self._loop.action(name, action_fingerprint(name, call[1], digest(result)))
            sent = True
        if not sent:
            self._send(ProgressEvent(ProgressKind.ACTIVITY))
