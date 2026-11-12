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

Model API errors (#395, run on claude 2.1.314 against a loopback stand-in of
the Messages API, 2026-10-30): once the CLI has spent its own retries of a
request (``system/api_retry``, ``attempt`` of ``max_retries``), the turn ends
with an ``assistant`` record holding the CLI's error message and a ``result``
with ``subtype: success``, ``is_error: true``, ``terminal_reason: api_error``
and the HTTP status as ``api_error_status``, and the CLI exits 1. A failing
``result`` with that terminal reason and a status of 408 (a request timeout)
or 5xx (529 is overloaded; the CLI words every 5xx as a server-side issue,
usually temporary) makes the failure transient: :attr:`ClaudeStream.transient`
is a fixed reason naming the status. Nothing else does. Not another 4xx: 400,
401, 403 and 404 are the request's, the credential's or the model's; a 429
may be a plan's usage limit, which resets hours later, and is typed as a
limit instead (:attr:`ClaudeStream.limit`, #434), which nothing relaunches; a 413 ends with
``terminal_reason: image_error``. Not an error without a status: a refused or
dropped connection (``api_error: connection_failed``, which a wrong
``ANTHROPIC_BASE_URL`` gives too) or the CLI's own request timeout, which
carries neither a status nor an ``api_error`` and is told apart only by its
message text. No message text is read for this: the record's typed fields are
the whole check.

The ``assistant`` record that comes before that ``result`` is the CLI's own,
and typed too: ``is_api_error_message: true`` and the same status as
``api_error_status`` (``null`` without one). The first such record is kept
as :attr:`ClaudeStream.api_error` (``claude: API error <status>``, or
``claude: API error`` without a status), and one with status 429 as
:attr:`ClaudeStream.api_limit`, whatever follows them: the terminal
``result`` that would say the same may never arrive, when the CLI stays
alive until a limit kills it or exits without writing it (R3-F1 of PR #480).
Neither record fails the run by itself; the outcome stays the ``result``'s,
as the paragraph above says. A stream that ended without its ``result``
after such a record is no result-less exit (:attr:`ClaudeStream.result_less`)
and fails with the API error as its :attr:`ClaudeStream.failure_kind`. No
message text is read for it either.

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
invocation's total. The turns opened and the tool calls still waiting for
their ``tool_result`` say where a killed run's stream stood: in the real
runs #343 examined, the stream sent nothing while a tool ran (a foreground
``Bash`` call, ten minutes at most) nor between turns while a background
task or a wakeup kept the CLI alive, so an idle timeout fires in one or the
other. They are what the stream showed, not a verdict: turns that ended
while a background agent ran wait for their held-back results, so fewer
results than turns can be turns already over; that agent's own calls are
open calls like the main conversation's; a ``tool_result`` skipped past the
per-line bound leaves its call open; and a call past :data:`MAX_OPEN_TOOLS`,
or without an id, is never counted open. Only ``results`` equal to
``turns`` (every turn opened has ended) reads one way.

A reply in hand (#439): given ``is_reply``, the driver's test of a ``result``
text, :attr:`ClaudeStream.replied` says the run is decided on that text while
the CLI still runs: every turn opened has its ``result``, the last is a
success whose text the test accepts, and no tool call is open. That is a CLI
between turns kept alive by something the agent left pending, the state the
paragraph above reads ``results`` equal to ``turns`` as, with a reply that
needs no further turn. A record that opens a turn ends it, until that turn's
own ``result`` decides again; a notice between turns does not. A background
agent's held-back results keep it false until they have arrived. A
``tool_result`` skipped past the per-line bound leaves its call open and so
keeps it false too: the run then waits for the CLI, as it did before #439.

Loop detection (#194): given a :class:`~autoforge.loop_detect.LoopObserver`,
each completed tool call is reported as an action fingerprinted from its
name, the digest of its whole ``input`` (kept from the ``tool_use`` until
its ``tool_result`` arrives) and the digest of the whole ``tool_result``
block but its ``tool_use_id``; ``system/api_retry`` is a retry and an
``assistant`` record a completed turn. Only names and digests leave here.

Refused requests (#258, run on claude 2.1.312 against a loopback stand-in
of the Messages API, 2026-10-27): a ``-p`` run asks no one. A tool call
that needs a permission its mode does not grant (``manual``, ``dontAsk``,
``acceptEdits`` for a command) or that a deny rule of the user settings
names is refused at once: a ``system/permission_denied`` record (``tool_name``,
``tool_use_id``), an error ``tool_result`` telling the model to go on
without it, and the call listed in the turn's ``result`` under
``permission_denials`` (``tool_name``, ``tool_use_id``, ``tool_input``); the
run goes on and exits 0. ``bypassPermissions``, the adapter's default,
asked for nothing in that run (a write under ``.git/`` and ``.claude/``
included). ``AskUserQuestion`` is not offered to the model under ``-p``; a
call to it anyway gets an error ``tool_result``. Each of these is kept in
:attr:`ClaudeStream.refused`, once per ``tool_use_id``: its kind and, for a
permission, the tool's name and its one allow-listed input
(:data:`TOOL_DETAILS`), never a command line.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from decimal import ROUND_HALF_UP, Decimal

from .interaction import RequestKind
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
# The tool through which the model asks the user a question.
QUESTION_TOOL = "AskUserQuestion"
# Record types that are activity only. Any other type is counted as unknown
# (forward compatibility) and is activity too.
_ACTIVITY_TYPES = frozenset({"rate_limit_event"})
# Per-turn counts in a ``result``, summed over the invocation's turns.
_PER_TURN_COUNTS = ("num_turns", "duration_ms")
# Tool calls whose result has not arrived yet, kept to name a failed tool
# and to fingerprint the call once it completes. Past the bound a result is
# still counted, only not named, and is no action for the loop detector.
MAX_OPEN_TOOLS = 1024
# Refused requests kept to name; past it they are still counted.
MAX_REFUSED = 64
MAX_REASON_CHARS = 400
MAX_VALUE_CHARS = 120
MAX_SUBJECT_CHARS = 160
_MAX_COUNT = 10**15
# The ``api_error_status`` values of a turn that ended on a model API error
# that make the failure transient (module docstring, #395): a request
# timeout and every server error.
TRANSIENT_API_STATUSES = frozenset({408, *range(500, 600)})
# The ``api_error_status`` of a turn that ended at a usage or rate limit
# (#434): never transient, and never relaunched automatically (a plan's usage
# limit resets hours later, and no record says when).
LIMIT_API_STATUS = 429
# A run that cost more than this is not a reading to believe.
_MAX_COST_USD = Decimal(1_000_000)
# Two of the fixed reasons :attr:`ClaudeStream.failure_kind` takes (#434).
_PROTOCOL_VIOLATION = "claude: stream-json protocol violation"
_ENDED_EARLY = "claude: stdout ended before a result event"


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
    success and :attr:`failure` the bounded, redacted reason otherwise,
    :attr:`failure_kind` a fixed reason naming what failed it (#434),
    :attr:`transient` a fixed reason when that failure is a transient model
    API error, :attr:`api_error` and :attr:`api_limit` the model API errors
    the CLI reported in its own typed messages (R3-F1 of PR #480), and
    :attr:`refused` the requests the CLI refused, whatever the outcome.
    """

    def __init__(
        self,
        *,
        cwd: str = "",
        emit: ProgressSink | None = None,
        loop: LoopObserver | None = None,
        is_reply: Callable[[str], bool] | None = None,
    ) -> None:
        self._cwd = cwd
        self._emit = emit
        self._loop = loop
        self._is_reply = is_reply
        # The newest ``result`` text passed ``is_reply``, asked once per result.
        self._text_is_reply = False
        self.text: str | None = None
        self.failure: str | None = None
        # Set with ``failure`` when the result that failed the run ended its
        # turn on a transient model API error (module docstring): a fixed
        # reason naming the status, never text from the stream.
        self.transient: str | None = None
        # Set with ``failure`` when that result ended its turn on a model API
        # error with status 429 (#434): a usage or rate limit, which a launch
        # now would meet again. A fixed reason, never text from the stream.
        self.limit: str | None = None
        # Set with ``failure``: what failed the run, as a fixed reason that
        # carries no text from the stream (#434): ``claude: API error <status>``
        # for a turn ended on a model API error with a status (401 and 403 an
        # authentication or permission refusal), ``claude: API error`` for one
        # without, ``claude: the run ended in an error``, ``claude: the run did
        # not succeed``, ``claude: stream-json protocol violation`` or
        # ``claude: stdout ended before a result event`` (:attr:`api_error`
        # instead when the CLI had reported one, R3-F1). The adapter keeps it
        # when a limit then killed the CLI, which drops ``failure`` itself, so
        # the failure is not mistaken for the limit's (R2-F1 of PR #480).
        self.failure_kind: str | None = None
        # The first model API error the CLI reported in an ``assistant``
        # record of its own (``is_api_error_message: true``), as a fixed
        # reason naming its ``api_error_status``: ``claude: API error
        # <status>``, or ``claude: API error`` without a readable status
        # (module docstring, R3-F1 of PR #480). Kept whatever follows the
        # record, the terminal ``result`` included or never arriving; it
        # fails nothing by itself.
        self.api_error: str | None = None
        # Set when such a record carried status 429 (#434): a usage or rate
        # limit the CLI reported, with or without the ``result`` that would
        # set :attr:`limit`. A fixed reason, never text from the stream.
        self.api_limit: str | None = None
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
        # Open tool calls: their name, with a loop observer their input's
        # digest, and their allow-listed input (:data:`TOOL_DETAILS`).
        self._open_tools: dict[str, tuple[str, bytes, str]] = {}
        # Requests the CLI refused (module docstring): the first
        # :data:`MAX_REFUSED`, each its kind and its cleaned subject ("" for
        # none), and how many there were; a ``tool_use_id`` counts once.
        self.refused: list[tuple[RequestKind, str]] = []
        self.refused_count = 0
        self._refused_ids: set[str] = set()
        self._records = 0
        self._oversize = 0
        self._unknown = 0
        self._after_result = 0
        self._tool_calls = 0
        # ``system/init`` records seen: the CLI started a session (#434).
        self._inits = 0
        self._tool_errors = 0
        self._retries = 0
        # Typed model API-error records of the CLI's (``is_api_error_message``).
        self._api_errors = 0
        self._result: dict[str, str | int | bool] = {}

    @property
    def replied(self) -> bool:
        """The run is decided on :attr:`text`, whatever the CLI does (module docstring, #439)."""
        return (
            self._text_is_reply
            and self.failure is None
            and self._results == self._turns
            and not self._open_tools
        )

    # -- driver interface ----------------------------------------------------
    def feed(self, data: bytes) -> None:
        """Reduce one stdout line (without its LF)."""
        self._records += 1
        if self.failure is not None:
            # The outcome is decided: a later turn cannot redeem it. The
            # driver stops a CLI that runs on after it (#394).
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
        # A model API error the CLI reported before its stdout ended is what
        # failed the run, as far as anything typed tells (R3-F1 of PR #480).
        kind = self.api_error or _ENDED_EARLY
        after = (
            f", after reporting {self.api_error.removeprefix('claude: ')}" if self.api_error else ""
        )
        if self._results:
            self._fail(f"claude: exited inside a turn that has no result event{after}", kind)
            return
        reason = f"claude: exited without a result event{after}"
        if self._oversize:
            reason += (
                f" ({self._oversize} line(s) past the per-line bound were skipped; "
                "the result must arrive whole in one line)"
            )
        self._fail(reason, kind)

    @property
    def result_less(self) -> bool:
        """Whether the CLI died inside a running session, before any ``result`` (#434).

        A typed fact for the automatic-recovery rule, which relaunches only
        what positive evidence shows safe to re-enter (ADR 0012 D11.2): the
        absence of a result says nothing of why the CLI ended. So it holds
        only when the stream shows the session was running before it ended:
        the CLI's own ``system/init`` (its configuration loaded and its
        session started) and at least one tool call of the model's
        (``tool_use``: the model API answered the credential, and the model
        was working; the CLI's own message for a failed API request carries
        no tool call). Then stdout reached EOF with no turn ever closed, no
        line skipped past the per-line bound (it may have been the result)
        and no other failure first. An empty stream, or one that ended before
        the model's first tool call, is no result-less exit: an
        authentication, permission, configuration or host failure the CLI
        reports only on stderr ends it exactly so, and stderr is free text,
        never read for this (R1-F1 of PR #480). Neither is a stream that had
        a result, or failed on a record, nor one in which the CLI reported a
        model API error of its own (:attr:`api_error`): its session had
        failed on the model API, an authentication refusal or a usage limit
        among the causes, which a launch now may meet again, and the
        ``result`` that would have said so was never written (R3-F1 of PR
        #480).
        """
        return (
            self.exited_early
            and self._results == 0
            and self._oversize == 0
            and self._inits > 0
            and self._tool_calls > 0
            and self.api_error is None
        )

    def summary(self) -> dict[str, str | int | bool]:
        """The flat, bounded, redacted summary for ``execution.json``."""
        return {
            **self._result,
            "results": self._results,
            # Where the stream stood when it ended (#343): ``turns`` equal to
            # ``results`` is a CLI between turns, kept alive by something the
            # agent left pending. Fewer results with an open tool call is a
            # CLI inside that call, or a background agent's call while the
            # turns it outlived wait for their held results, or a call
            # whose oversize result was skipped (module docstring). The
            # count stops at MAX_OPEN_TOOLS.
            "turns": self._turns,
            "open_tool_calls": len(self._open_tools),
            "records": self._records,
            "oversize_records": self._oversize,
            "unknown_events": self._unknown,
            "records_after_result": self._after_result,
            "tool_calls": self._tool_calls,
            "tool_errors": self._tool_errors,
            "api_retries": self._retries,
            "api_error_records": self._api_errors,
            "failure": self.failure or "",
        }

    # -- internals -----------------------------------------------------------
    def _send(self, event: ProgressEvent) -> None:
        if self._emit is not None:
            self._emit(event)

    def _fail(self, reason: str, kind: str) -> None:
        """Fail the run, unless it failed already: ``reason`` for the record,
        ``kind`` the fixed reason :attr:`failure_kind` keeps."""
        if self.failure is None:
            self.failure = clean(reason, MAX_REASON_CHARS)
            self.failure_kind = kind
            self.text = None

    def _protocol(self, what: str) -> None:
        self._fail(f"claude: stream-json protocol violation: {what}", _PROTOCOL_VIOLATION)

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
            for denial in denials:
                if isinstance(denial, dict):
                    name = denial.get("tool_name")
                    name = name if isinstance(name, str) else ""
                    detail = self._detail(name, denial.get("tool_input"))
                    self._refuse(RequestKind.PERMISSION, denial.get("tool_use_id"), name, detail)
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
            api_error = record.get("terminal_reason") == "api_error"
            self._fail(
                f"claude: the run ended in an error ({', '.join(facts)})"
                + (f": {excerpt}" if excerpt else ""),
                (f"claude: API error {status}" if status is not None else "claude: API error")
                if api_error
                else "claude: the run ended in an error",
            )
            # The first failure decides the outcome (:meth:`feed`), so this
            # result's failure is the run's.
            if api_error and status in TRANSIENT_API_STATUSES:
                self.transient = f"claude: API error {status}"
            if api_error and status == LIMIT_API_STATUS:
                self.limit = f"claude: API error {status}"
        elif is_error is not False:
            self._protocol("the result event has no boolean 'is_error'")
        elif subtype != "success":
            self._fail(
                f"claude: the run did not succeed (subtype {clean(subtype, 60) or '-'})",
                "claude: the run did not succeed",
            )
        elif not isinstance(result, str):
            self._protocol("the result event has no string 'result'")
        else:
            self.text = result
            self._text_is_reply = self._is_reply is not None and self._is_reply(result)

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
            self._inits += 1
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
        elif subtype == "permission_denied":
            tool_id = record.get("tool_use_id")
            call = self._open_tools.get(tool_id) if isinstance(tool_id, str) else None
            name = record.get("tool_name")
            if not isinstance(name, str):
                name = call[0] if call is not None else ""
            self._refuse(RequestKind.PERMISSION, tool_id, name, call[2] if call else "")
            self._send(ProgressEvent(ProgressKind.ACTIVITY))
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
        if record.get("is_api_error_message") is True:
            self._take_api_error(record)
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

    def _take_api_error(self, record: dict) -> None:
        """Keep the typed facts of the CLI's own model API-error message (R3-F1 of PR #480).

        Its two typed fields only, never its text: the flag that brought it
        here and its ``api_error_status``. A status that is no count (absent,
        ``null``, a string) still names an API error, one without a status.
        """
        self._api_errors += 1
        status = _count(record.get("api_error_status"))
        reason = f"claude: API error {status}" if status is not None else "claude: API error"
        if self.api_error is None:
            self.api_error = reason
        if status == LIMIT_API_STATUS:
            self.api_limit = reason

    def _tool_use(self, block: dict) -> None:
        self._tool_calls += 1
        name = block.get("name")
        name = name if isinstance(name, str) else ""
        tool_id = block.get("id")
        tool_input = block.get("input")
        detail = self._detail(name, tool_input)
        if isinstance(tool_id, str) and len(self._open_tools) < MAX_OPEN_TOOLS:
            self._open_tools[tool_id] = (
                name,
                digest(tool_input) if self._loop is not None else b"",
                detail,
            )
        self._send(ProgressEvent(ProgressKind.TOOL_STARTED, tool=name, detail=detail))

    def _detail(self, name: str, tool_input: object) -> str:
        """The one input of ``name``'s call shown for it (:data:`TOOL_DETAILS`), or ""."""
        field = TOOL_DETAILS.get(name)
        if field is None or not isinstance(tool_input, dict):
            return ""
        value = tool_input.get(field)
        if field in _PATH_INPUTS:
            return relative_path(value, self._cwd)
        return value if isinstance(value, str) else ""

    def _refuse(self, kind: RequestKind, tool_id: object, name: str, detail: str) -> None:
        """Keep a request the CLI refused, once per ``tool_use_id``."""
        if isinstance(tool_id, str):
            if tool_id in self._refused_ids:
                return
            if len(self._refused_ids) < MAX_OPEN_TOOLS:
                self._refused_ids.add(tool_id)
        self.refused_count += 1
        if len(self.refused) < MAX_REFUSED:
            subject = f"{name}: {detail}" if name and detail else name
            self.refused.append((kind, clean(subject, MAX_SUBJECT_CHARS)))

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
                if name == QUESTION_TOOL:
                    # Not offered under -p (module docstring): the question
                    # reached no one. Its text is the model's; only the kind is kept.
                    self._refuse(RequestKind.QUESTION, tool_id, "", "")
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
