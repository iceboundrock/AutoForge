"""The provider-neutral interaction contract and its broker (ADR 0007 D5.1, D5.2; #134).

An *interaction* is a provider's request for a human decision: a question
the agent asks, or a permission one of its tool calls needs. This module is
everything the engine, an adapter and a resolver share about one:

- the request, :class:`InteractionRequest`: a controller-assigned id, the
  provider's opaque id, the kind, a bounded and redacted rendering (the
  questions, or the tool, a summary of its input and the input's digest),
  the agent it came from, and ``offers``, the decisions its provider can
  carry for it;
- the decision: :class:`Answer`, :class:`Allow`, :class:`Deny` or
  :class:`Cancel`. Deferral is not one of them: it is the broker's
  directive, never an operator's decision;
- the :class:`SessionIdentity` a pause binds (#126 records it; nothing here
  persists anything);
- the :class:`Resolver` the engine supplies: the terminal resolver of
  :mod:`autoforge.cli`, :class:`Unattended` when there is no one to ask, or
  a scripted one in tests;
- the :class:`InteractionPolicy` and the :class:`Capabilities` an adapter
  declares per provider and pair (D5.3: a capability is true only once its
  gate passed, ADR 0007 §3);
- the lifecycle discriminant of an invocation's outcome
  (:class:`Lifecycle`, carried by :class:`InteractionOutcome` on
  ``AgentExecutionResult.interaction``, D10.5).

:class:`InteractionBroker` decides what happens to each request. The engine
creates one per launch and hands it to the adapter as
``AgentRequest.interaction``, as it hands over ``progress``; the adapter
mints each provider ask through it (:meth:`InteractionBroker.question`,
:meth:`InteractionBroker.permission`) and calls
:meth:`InteractionBroker.handle`, which returns a directive:
:class:`Deliver`, :class:`Pause` or :class:`Fail`. The broker owns the
policy (D7.1), the per-wait and aggregate limits (D7.3), the degradation of
a live wait (D7.4), the capability check (a decision the provider cannot
carry is never offered, and never delivered) and a bounded, redacted record
of each request for the run log. It never sees a provider record, and the
adapter never sees the policy.

What this module is not (D5.4): a transport. It carries decisions, never
bytes, starts no process, reads no file and names no provider, SDK or wire
record; each helper keeps its own envelope. Every value is bounded where it
is built, with the rules of :mod:`autoforge.sdk_policy` (docs/sdk-policy.md
§10): a value that decides what a tool may do or what the agent is told (an
option label, an answer, guidance, a modified input, an identity) is
refused past its bound or when redaction would change it, never clipped; a
question's text is refused past its bound too, so an operator never decides
on part of a question, and is then cleaned and redacted; a value that is
only shown (a header, a description, a permission summary) is cleaned,
redacted and clipped. A refusal is an
:class:`~autoforge.errors.InteractionDataError` naming the field and the
rule, never the value; the adapter then answers the ask as it answers
:class:`Fail` (D10.4).

``pause`` (D7.5), its pending record, the operator commands and recovery are
#126's. Until it lands the engine passes ``pause_available=False``, so the
broker never answers :class:`Pause`, and config validation refuses
``interaction_policy: pause`` (:func:`autoforge.sdk_policy.interaction_settings`).
"""

from __future__ import annotations

import enum
import re
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from .errors import InteractionDataError
from .sdk_policy import (
    INTERACTION_FAIL,
    INTERACTION_PAUSE,
    INTERACTION_PROMPT,
    MAX_ANSWER_CHARS,
    MAX_OPTION_CHARS,
    MAX_QUESTION_CHARS,
    MAX_QUESTIONS,
    MAX_SHOWN_CHARS,
    MAX_TOOL_NAME_CHARS,
    InteractionSettings,
    exact_input,
    exact_text,
    question_options,
    shown,
)

# An identity the controller compares or sends back verbatim: the
# controller's request id, the provider's, a subagent's name, a session id.
MAX_IDENTITY_CHARS = 256
# A path a session identity binds (the working directory, the session root).
MAX_PATH_CHARS = 4096
# What an operator is shown of a question's header and an option's
# description, and of a permission's input (sdk_policy.permission_summary).
MAX_HEADER_CHARS = 100
MAX_DESCRIPTION_CHARS = 500
MAX_SUMMARY_CHARS = 3 * MAX_SHOWN_CHARS
# What the run log keeps of one fail reason.
MAX_REASON_CHARS = 400

_SHA256 = re.compile(r"[0-9a-f]{64}")
_GIT_SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


class RequestKind(enum.StrEnum):
    """What the provider asks for."""

    QUESTION = "question"
    PERMISSION = "permission"


class DecisionKind(enum.StrEnum):
    """A decision a provider may or may not be able to carry (ADR 0007 §3's rows)."""

    ANSWER = "answer"
    CUSTOM_ANSWER = "custom_answer"
    ALLOW_ONCE = "allow_once"
    ALLOW_SESSION = "allow_session"
    MODIFIED_INPUT = "modified_input"
    DENY = "deny"
    DENY_GUIDANCE = "deny_guidance"
    CANCEL = "cancel"


# Which decisions each kind of request can take at all.
QUESTION_DECISIONS = frozenset(
    {DecisionKind.ANSWER, DecisionKind.CUSTOM_ANSWER, DecisionKind.CANCEL}
)
PERMISSION_DECISIONS = frozenset(
    {
        DecisionKind.ALLOW_ONCE,
        DecisionKind.ALLOW_SESSION,
        DecisionKind.MODIFIED_INPUT,
        DecisionKind.DENY,
        DecisionKind.DENY_GUIDANCE,
        DecisionKind.CANCEL,
    }
)


class InteractionPolicy(enum.StrEnum):
    """``options.interaction_policy`` (D7.1); :mod:`autoforge.sdk_policy` validates it."""

    PROMPT = INTERACTION_PROMPT
    PAUSE = INTERACTION_PAUSE
    FAIL = INTERACTION_FAIL


class Lifecycle(enum.StrEnum):
    """How an invocation that could ask ended (D5.1)."""

    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    PAUSED = "paused"


def _identity(value: object, what: str, limit: int = MAX_IDENTITY_CHARS) -> str:
    """A non-blank identity :func:`autoforge.sdk_policy.exact_text` accepts, on one line."""
    text = exact_text(value, what, limit)
    if not text.strip() or "\n" in text or "\t" in text:
        raise InteractionDataError(f"{what} is blank or spans more than one line")
    return text


def _flag(value: object, what: str) -> bool:
    if not isinstance(value, bool):
        raise InteractionDataError(f"{what} is not a boolean")
    return value


def _display(value: object, what: str, limit: int) -> str:
    """A value that is only shown: a string, cleaned, redacted and clipped to ``limit``."""
    if not isinstance(value, str):
        raise InteractionDataError(f"{what} is not a string")
    return shown(value, limit)


# -- the request ---------------------------------------------------------------
@dataclass(frozen=True)
class QuestionOption:
    """One option of a question: its raw ``label`` (what an answer carries,
    refused rather than adjusted) and a ``description`` that is only shown."""

    label: str
    description: str = ""

    def __post_init__(self) -> None:
        if not exact_text(self.label, "an option label", MAX_OPTION_CHARS).strip():
            raise InteractionDataError("an option label is blank")
        description = _display(self.description, "an option description", MAX_DESCRIPTION_CHARS)
        object.__setattr__(self, "description", description)

    @property
    def shown(self) -> str:
        """The label as the operator sees it; never sent back."""
        return shown(self.label, MAX_OPTION_CHARS)


@dataclass(frozen=True)
class Question:
    """One question: ``text`` and ``header`` are shown; ``options`` carry the
    labels an answer picks from; ``multiple`` allows several, ``custom`` an
    answer of the operator's own words."""

    text: str
    options: tuple[QuestionOption, ...]
    header: str = ""
    multiple: bool = False
    custom: bool = False

    def __post_init__(self) -> None:
        if isinstance(self.text, str) and len(self.text) > MAX_QUESTION_CHARS:
            raise InteractionDataError(f"a question is longer than {MAX_QUESTION_CHARS} characters")
        text = _display(self.text, "a question", MAX_QUESTION_CHARS)
        if not text:
            raise InteractionDataError("a question has no text")
        object.__setattr__(self, "text", text)
        object.__setattr__(
            self, "header", _display(self.header, "a question header", MAX_HEADER_CHARS)
        )
        options = tuple(self.options)
        if not all(isinstance(option, QuestionOption) for option in options):
            raise InteractionDataError("a question option is not a QuestionOption")
        # Count, exactness and that no two labels are shown alike (a person
        # could not tell which one they pick).
        question_options([option.label for option in options], "a question")
        object.__setattr__(self, "options", options)
        _flag(self.multiple, "a question's multiple")
        _flag(self.custom, "a question's custom")

    def option(self, label: str) -> QuestionOption | None:
        return next((option for option in self.options if option.label == label), None)


@dataclass(frozen=True)
class InteractionRequest:
    """One request for a decision, bounded and redacted on construction (D5.1).

    ``request_id`` is the controller's, stable within the logical
    invocation; ``provider_request_id`` the provider's, opaque. ``agent`` is
    ``None`` for the main agent, else the subagent's name. A question
    carries ``questions``; a permission ``tool_name``, a ``summary`` of its
    input (:func:`autoforge.sdk_policy.permission_summary`) and
    ``input_sha256``, the digest the decision is bound to
    (:func:`autoforge.sdk_policy.input_digest`). ``offers`` is what the
    provider can carry for it (:meth:`Capabilities.offers`).
    """

    request_id: str
    provider_request_id: str
    kind: RequestKind
    offers: frozenset[DecisionKind]
    agent: str | None = None
    questions: tuple[Question, ...] = ()
    tool_name: str = ""
    summary: str = ""
    input_sha256: str = ""

    def __post_init__(self) -> None:
        _identity(self.request_id, "a request id")
        _identity(self.provider_request_id, "a provider request id")
        try:
            kind = RequestKind(self.kind)
        except ValueError:
            raise InteractionDataError("a request has an unknown kind") from None
        object.__setattr__(self, "kind", kind)
        if self.agent is not None:
            _identity(self.agent, "a subagent name")
        try:
            offers = frozenset(DecisionKind(offer) for offer in self.offers)
        except (TypeError, ValueError):
            raise InteractionDataError("a request offers an unknown decision") from None
        questions = tuple(self.questions)
        if kind is RequestKind.QUESTION:
            if not 1 <= len(questions) <= MAX_QUESTIONS:
                raise InteractionDataError(
                    f"a question request carries {len(questions)} questions, not 1 to "
                    f"{MAX_QUESTIONS}"
                )
            if not all(isinstance(question, Question) for question in questions):
                raise InteractionDataError("a question is not a Question")
            if len({question.text.casefold() for question in questions}) != len(questions):
                raise InteractionDataError("a question request asks the same question twice")
            if self.tool_name or self.summary or self.input_sha256:
                raise InteractionDataError("a question request carries a tool call")
            if DecisionKind.CUSTOM_ANSWER in offers and not any(q.custom for q in questions):
                raise InteractionDataError(
                    "a question request offers a custom answer no question allows"
                )
            possible = QUESTION_DECISIONS
        else:
            if questions:
                raise InteractionDataError("a permission request carries questions")
            _identity(self.tool_name, "a tool name", MAX_TOOL_NAME_CHARS)
            object.__setattr__(
                self, "summary", _display(self.summary, "a permission summary", MAX_SUMMARY_CHARS)
            )
            if not isinstance(self.input_sha256, str) or not _SHA256.fullmatch(self.input_sha256):
                raise InteractionDataError("a permission's input digest is not a SHA-256")
            possible = PERMISSION_DECISIONS
        if not offers <= possible:
            raise InteractionDataError(f"a {kind} request offers a decision it cannot take")
        object.__setattr__(self, "questions", questions)
        object.__setattr__(self, "offers", offers)

    @property
    def what(self) -> str:
        """What was asked, on one short line for a message: ``permission: Bash``."""
        if self.kind is RequestKind.PERMISSION:
            text = f"permission: {self.tool_name}"
        else:
            header = self.questions[0].header
            text = f"question: {header}" if header else "question"
            if len(self.questions) > 1:
                text += f" (+{len(self.questions) - 1} more)"
        if self.agent is not None:
            text += f", from subagent {self.agent}"
        return shown(text, 2 * MAX_SHOWN_CHARS)


# -- the decision --------------------------------------------------------------
@dataclass(frozen=True)
class QuestionResponse:
    """The answer to one question: the labels picked, and the operator's own
    words where the question allows them."""

    choices: tuple[str, ...] = ()
    custom_text: str | None = None

    def __post_init__(self) -> None:
        choices = tuple(self.choices)
        for choice in choices:
            exact_text(choice, "a chosen option", MAX_OPTION_CHARS)
        if len(set(choices)) != len(choices):
            raise InteractionDataError("an answer picks the same option twice")
        object.__setattr__(self, "choices", choices)
        if (
            self.custom_text is not None
            and not exact_text(self.custom_text, "a custom answer", MAX_ANSWER_CHARS).strip()
        ):
            raise InteractionDataError("a custom answer is blank")
        if not choices and self.custom_text is None:
            raise InteractionDataError("an answer picks nothing")


@dataclass(frozen=True)
class Answer:
    """The answer to a question request: one response per question, in order."""

    responses: tuple[QuestionResponse, ...]

    def __post_init__(self) -> None:
        responses = tuple(self.responses)
        if not all(isinstance(response, QuestionResponse) for response in responses):
            raise InteractionDataError("an answer's response is not a QuestionResponse")
        object.__setattr__(self, "responses", responses)


class AllowScope(enum.StrEnum):
    ONCE = "once"
    SESSION = "session"


@dataclass(frozen=True)
class Allow:
    """Approve the tool call, ``once`` or for the ``session``; with
    ``updated_input``, the call runs on that input instead, sent back
    exactly as validated (:func:`autoforge.sdk_policy.exact_input`)."""

    scope: AllowScope = AllowScope.ONCE
    updated_input: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        try:
            object.__setattr__(self, "scope", AllowScope(self.scope))
        except ValueError:
            raise InteractionDataError("an approval has an unknown scope") from None
        if self.updated_input is not None:
            if not isinstance(self.updated_input, Mapping):
                raise InteractionDataError("a modified input is not an object")
            exact_input(dict(self.updated_input), "a modified input")


@dataclass(frozen=True)
class Deny:
    """Refuse the tool call; ``guidance`` tells the agent what to do instead."""

    guidance: str | None = None

    def __post_init__(self) -> None:
        if (
            self.guidance is not None
            and not exact_text(self.guidance, "guidance", MAX_ANSWER_CHARS).strip()
        ):
            raise InteractionDataError("guidance is blank")


@dataclass(frozen=True)
class Cancel:
    """Decline to decide: the provider is told no, with nothing more."""


InteractionDecision = Answer | Allow | Deny | Cancel


def required_offers(decision: InteractionDecision) -> frozenset[DecisionKind]:
    """What a request must offer for ``decision`` to be delivered."""
    if isinstance(decision, Answer):
        needs = {DecisionKind.ANSWER}
        if any(response.custom_text is not None for response in decision.responses):
            needs.add(DecisionKind.CUSTOM_ANSWER)
        if all(not response.choices for response in decision.responses):
            needs.discard(DecisionKind.ANSWER)
        return frozenset(needs)
    if isinstance(decision, Allow):
        needs = {
            DecisionKind.ALLOW_ONCE
            if decision.scope is AllowScope.ONCE
            else DecisionKind.ALLOW_SESSION
        }
        if decision.updated_input is not None:
            needs.add(DecisionKind.MODIFIED_INPUT)
        return frozenset(needs)
    if isinstance(decision, Deny):
        return frozenset(
            {DecisionKind.DENY if decision.guidance is None else DecisionKind.DENY_GUIDANCE}
        )
    return frozenset({DecisionKind.CANCEL})


def decision_problem(request: InteractionRequest, decision: object) -> str | None:
    """Why ``decision`` cannot be delivered for ``request``, or ``None``.

    The decision must be one the request offers (the capability check, D5.2)
    and fit its content: an answer for a question, one response per
    question, only the question's own options, one pick unless it allows
    several, the operator's own words only where it allows them; an
    approval or a denial for a permission. The reason names rules, never a
    value.
    """
    if not isinstance(decision, (Answer, Allow, Deny, Cancel)):
        return "it is not a decision"
    if request.kind is RequestKind.QUESTION and isinstance(decision, (Allow, Deny)):
        return "a question takes an answer or a cancellation"
    if request.kind is RequestKind.PERMISSION and isinstance(decision, Answer):
        return "a permission takes an approval, a denial or a cancellation"
    missing = required_offers(decision) - request.offers
    if missing:
        return f"the provider cannot carry {', '.join(sorted(missing))} for this request"
    if isinstance(decision, Answer):
        if len(decision.responses) != len(request.questions):
            return f"{len(decision.responses)} responses for {len(request.questions)} question(s)"
        for n, (question, response) in enumerate(
            zip(request.questions, decision.responses, strict=True), 1
        ):
            if any(question.option(choice) is None for choice in response.choices):
                return f"question {n}: a choice is not one of its options"
            if response.custom_text is not None and not question.custom:
                return f"question {n}: it allows no answer of the operator's own"
            picks = len(response.choices) + (response.custom_text is not None)
            if picks > 1 and not question.multiple:
                return f"question {n}: it takes one answer, got {picks}"
    return None


# -- resolvers -----------------------------------------------------------------
class UnavailableReason(enum.StrEnum):
    """Why a resolver has no decision: the three cases a live wait degrades on (D7.4)."""

    NO_TERMINAL = "no_terminal"
    END_OF_INPUT = "end_of_input"
    EXPIRED = "expired"


_UNAVAILABLE_TEXT = {
    UnavailableReason.NO_TERMINAL: "the controller's stdin is not a terminal",
    UnavailableReason.END_OF_INPUT: "the operator's input ended",
    UnavailableReason.EXPIRED: "no decision came in time",
}


@dataclass(frozen=True)
class Unavailable:
    """No decision, and why."""

    reason: UnavailableReason

    def __post_init__(self) -> None:
        try:
            object.__setattr__(self, "reason", UnavailableReason(self.reason))
        except ValueError:
            raise InteractionDataError("a resolver gave an unknown reason") from None


class Resolver(Protocol):
    """Who decides (D5.1): the engine supplies one per run.

    ``resolve`` returns a decision for ``request``, or :class:`Unavailable`,
    by ``deadline`` (``time.monotonic()``). It checks for a terminal before
    it asks. ``KeyboardInterrupt`` propagates: Ctrl-C interrupts the step,
    it is not a decision (D7.7).
    """

    def resolve(
        self, request: InteractionRequest, deadline: float
    ) -> InteractionDecision | Unavailable: ...


class Unattended:
    """The resolver of a run with no one to ask (a library caller, a test, a
    scheduler's worker): every request is unavailable, as for a controller
    whose stdin is not a terminal."""

    def resolve(self, request: InteractionRequest, deadline: float) -> Unavailable:
        return Unavailable(UnavailableReason.NO_TERMINAL)


# -- capabilities --------------------------------------------------------------
@dataclass(frozen=True)
class Capabilities:
    """What a provider can carry, declared by its adapter per provider and
    pair (D5.1). A value is true only once every ADR 0007 §3 gate of its row
    passed on that pair (D5.3); the default, all false, is what every
    adapter declares until then.

    ``live_questions`` and ``live_permissions``: a decision reaches the
    provider while it waits. ``durable_replay``: the provider replays the
    same structured request after a restart. ``continuation_recovery``: the
    same session continues after an explicit message. ``interrupt``: a turn
    can be interrupted. ``unsupported``: the decisions the provider cannot
    carry even live (a session approval, a modified input, a custom answer).
    """

    live_questions: bool = False
    live_permissions: bool = False
    durable_replay: bool = False
    continuation_recovery: bool = False
    interrupt: bool = False
    unsupported: frozenset[DecisionKind] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        for name in ("live_questions", "live_permissions", "durable_replay"):
            _flag(getattr(self, name), f"a capability's {name}")
        _flag(self.continuation_recovery, "a capability's continuation_recovery")
        _flag(self.interrupt, "a capability's interrupt")
        try:
            unsupported = frozenset(DecisionKind(kind) for kind in self.unsupported)
        except (TypeError, ValueError):
            raise InteractionDataError("a capability names an unknown decision") from None
        object.__setattr__(self, "unsupported", unsupported)

    def live(self, kind: RequestKind) -> bool:
        return self.live_questions if kind is RequestKind.QUESTION else self.live_permissions

    def offers(self, kind: RequestKind, *, custom: bool = False) -> frozenset[DecisionKind]:
        """The decisions a request of ``kind`` can be offered (D5.3): none
        without the live capability, never an unsupported one, and a custom
        answer only where a question allows one."""
        if not self.live(kind):
            return frozenset()
        possible = QUESTION_DECISIONS if kind is RequestKind.QUESTION else PERMISSION_DECISIONS
        if not custom:
            possible = possible - {DecisionKind.CUSTOM_ANSWER}
        return frozenset(possible - self.unsupported)

    def can_pause(self, kind: RequestKind) -> bool:
        """Whether a request of ``kind`` can be paused and its decision delivered
        later: the provider carries that kind's decision at all, and the session
        comes back by replay or by continuation. A pause also needs #126, which
        the broker is told separately (``pause_available``)."""
        return self.live(kind) and (self.durable_replay or self.continuation_recovery)


# What every adapter declares until a gate passes (D5.3).
NO_CAPABILITIES = Capabilities()


# -- directives and outcome -------------------------------------------------------
@dataclass(frozen=True)
class Deliver:
    """Send ``decision`` to the provider now."""

    decision: InteractionDecision


@dataclass(frozen=True)
class Pause:
    """End the provider's turn its own way and stop durably (#126, D7.5)."""

    request: InteractionRequest


@dataclass(frozen=True)
class Fail:
    """Deny or reject the ask and fail the invocation (D7.6). ``reason`` is
    bounded and names no value the provider sent; the adapter reports it as
    its ``provider_failure``."""

    reason: str


Directive = Deliver | Pause | Fail


@dataclass(frozen=True)
class InteractionOutcome:
    """``AgentExecutionResult.interaction`` (D10.5): how an invocation through
    which the agent could ask ended, and, on a pause, the pending request.
    #126 persists it; nothing here does."""

    lifecycle: Lifecycle
    pending: InteractionRequest | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "lifecycle", Lifecycle(self.lifecycle))
        if (self.lifecycle is Lifecycle.PAUSED) != (self.pending is not None):
            raise InteractionDataError("a pending request belongs to a paused outcome only")


# -- the session a pause binds ----------------------------------------------------
@dataclass(frozen=True)
class RemoteBinding:
    """The workflow identity a REMOTE launch is bound to (D5.1): repository
    (``owner/name``), issue, phase, attempt, review round, and the PR and
    HEAD where the phase has them."""

    repository: str
    issue: int
    phase: str
    attempt: int
    review_round: int
    pr_url: str = ""
    head_sha: str = ""

    def __post_init__(self) -> None:
        _identity(self.repository, "a repository")
        _identity(self.phase, "a phase")
        _counter(self.issue, "an issue number", 1)
        _counter(self.attempt, "an attempt", 1)
        _counter(self.review_round, "a review round", 0)
        if self.pr_url:
            _identity(self.pr_url, "a pull request URL")
        if self.head_sha and not _GIT_SHA.fullmatch(self.head_sha):
            raise InteractionDataError("a HEAD binding is not a full commit id")


@dataclass(frozen=True)
class LocalBinding:
    """The workflow identity a LOCAL launch is bound to (D5.1): the state
    root, the phase, and the launch index of the logical invocation's first
    launch."""

    state_root: str
    phase: str
    launch_index: int

    def __post_init__(self) -> None:
        _path(self.state_root, "a state root")
        _identity(self.phase, "a phase")
        _counter(self.launch_index, "a launch index", 1)


@dataclass(frozen=True)
class SessionIdentity:
    """What a pending interaction binds (D5.1, D8.4): a continuation launches
    only when the current launch would produce an equal identity.

    The provider and its pair's versions (D4.1, D4.2), the policy digest
    (#149's ``digest()``), the profile and the digest of its routing fields
    and options, the working directory and the session root (D2.7), the
    provider's session id, and the workflow binding.
    """

    provider: str
    sdk_version: str
    runtime_version: str
    policy_digest: str
    profile: str
    profile_digest: str
    cwd: str
    session_root: str
    session_id: str
    workflow: RemoteBinding | LocalBinding

    def __post_init__(self) -> None:
        for name in ("provider", "sdk_version", "runtime_version", "profile", "session_id"):
            _identity(getattr(self, name), f"a session's {name}")
        for name in ("policy_digest", "profile_digest"):
            value = getattr(self, name)
            if not isinstance(value, str) or not _SHA256.fullmatch(value):
                raise InteractionDataError(f"a session's {name} is not a SHA-256")
        _path(self.cwd, "a session's cwd")
        _path(self.session_root, "a session's root")
        if not isinstance(self.workflow, (RemoteBinding, LocalBinding)):
            raise InteractionDataError("a session's workflow binding is of no known mode")


def _counter(value: object, what: str, low: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < low:
        raise InteractionDataError(f"{what} is not a whole number of at least {low}")


def _path(value: object, what: str) -> None:
    if not _identity(value, what, MAX_PATH_CHARS).startswith("/"):
        raise InteractionDataError(f"{what} is not an absolute path")


# -- the broker ---------------------------------------------------------------------
@dataclass
class _Entry:
    """What the run log keeps of one handled request."""

    request: InteractionRequest | None
    directive: str = ""
    decision: tuple[str, ...] = ()
    reason: str = ""
    unavailable: str = ""
    waited_seconds: float = 0.0

    def record(self) -> dict[str, object]:
        entry: dict[str, object] = {"directive": self.directive}
        if self.request is not None:
            entry.update(
                request_id=self.request.request_id,
                kind=str(self.request.kind),
                what=self.request.what,
                offers=[str(offer) for offer in sorted(self.request.offers)],
            )
            if self.request.kind is RequestKind.PERMISSION:
                entry["input_sha256"] = self.request.input_sha256
        if self.decision:
            entry["decision"] = list(self.decision)
        if self.reason:
            entry["reason"] = self.reason
        if self.unavailable:
            entry["unavailable"] = self.unavailable
        if self.waited_seconds:
            entry["waited_seconds"] = round(self.waited_seconds, 1)
        return entry


class InteractionBroker:
    """Decides what happens to each request of one launch (D5.2, D7.1-D7.4).

    ``settings`` is the profile's validated ``interaction_policy`` and
    limits, ``capabilities`` the adapter's declaration, ``resolver`` the
    engine's. ``pause_available`` is whether a durable pending-interaction
    record exists to pause into: #126's, so the engine passes ``False``
    until it lands. ``clock`` is ``time.monotonic``, the clock of
    ``invocation_due``.

    Under ``fail`` every request fails the invocation. Under ``prompt`` the
    resolver is asked live, until the earlier of ``max_wait_seconds`` after
    the ask and ``invocation_due``; no terminal, the end of the operator's
    input or the wait's own expiry pause where the request's kind can be
    paused (:meth:`Capabilities.can_pause`, and ``pause_available``) and
    fail otherwise, and nothing else degrades (D7.4). A wait the
    invocation's limit ended is a failure, never a pause: no time is left
    for the teardown a pause needs (D7.3). Under ``pause`` a request pauses
    where it can and fails otherwise. ``max_interactions`` bounds the
    requests handled, answered or not; one more fails (D7.3).

    A :class:`Fail` or a :class:`Pause` ends the invocation's interactions:
    every later request fails without reaching the resolver. The resolver
    runs on the caller's thread, under the repository lock the whole
    command holds (D7.2), and one request at a time.
    """

    def __init__(
        self,
        settings: InteractionSettings,
        capabilities: Capabilities,
        resolver: Resolver,
        *,
        pause_available: bool = False,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._policy = InteractionPolicy(settings.policy)
        self._max_wait = settings.max_wait_seconds
        self._max_interactions = settings.max_interactions
        self._capabilities = capabilities
        self._resolver = resolver
        self._pause_available = pause_available
        self._clock = clock
        self._lock = threading.RLock()
        self._issued = 0
        # The requests minted and not yet handled, by id: a request is
        # handled once, and only as it was minted.
        self._open: dict[str, InteractionRequest] = {}
        self._entries: list[_Entry] = []
        self._ended: Pause | Fail | None = None

    @property
    def handled(self) -> int:
        """How many requests :meth:`handle` took, answered or not."""
        return len(self._entries)

    @property
    def ended(self) -> Pause | Fail | None:
        """The directive that ended the invocation's interactions, if any."""
        return self._ended

    @property
    def failure(self) -> str | None:
        """The reason of the :class:`Fail` that ended them, if one did."""
        return self._ended.reason if isinstance(self._ended, Fail) else None

    def outcome(self) -> InteractionOutcome:
        """The lifecycle the requests imply: failed, paused (with the pending
        request) or completed. An adapter whose turn was interrupted reports
        :attr:`Lifecycle.INTERRUPTED` instead of a completed one."""
        if isinstance(self._ended, Fail):
            return InteractionOutcome(Lifecycle.FAILED)
        if isinstance(self._ended, Pause):
            return InteractionOutcome(Lifecycle.PAUSED, pending=self._ended.request)
        return InteractionOutcome(Lifecycle.COMPLETED)

    # -- minting ------------------------------------------------------------
    def question(
        self,
        provider_request_id: str,
        questions: Sequence[Question],
        *,
        agent: str | None = None,
    ) -> InteractionRequest:
        """A question request with the next controller id and the offers the
        capabilities allow. :class:`InteractionDataError` for data refused."""
        questions = tuple(questions)
        offers = self._capabilities.offers(
            RequestKind.QUESTION,
            custom=any(isinstance(q, Question) and q.custom for q in questions),
        )
        return self._mint(
            lambda request_id: InteractionRequest(
                request_id=request_id,
                provider_request_id=provider_request_id,
                kind=RequestKind.QUESTION,
                offers=offers,
                agent=agent,
                questions=questions,
            )
        )

    def permission(
        self,
        provider_request_id: str,
        tool_name: str,
        summary: str,
        input_sha256: str,
        *,
        agent: str | None = None,
    ) -> InteractionRequest:
        """A permission request with the next controller id and the offers
        the capabilities allow. :class:`InteractionDataError` for data refused."""
        offers = self._capabilities.offers(RequestKind.PERMISSION)
        return self._mint(
            lambda request_id: InteractionRequest(
                request_id=request_id,
                provider_request_id=provider_request_id,
                kind=RequestKind.PERMISSION,
                offers=offers,
                agent=agent,
                tool_name=tool_name,
                summary=summary,
                input_sha256=input_sha256,
            )
        )

    def _mint(self, build: Callable[[str], InteractionRequest]) -> InteractionRequest:
        with self._lock:
            request = build(str(self._issued + 1))
            self._issued += 1
            self._open[request.request_id] = request
            return request

    # -- deciding -------------------------------------------------------------
    def handle(self, request: InteractionRequest, invocation_due: float) -> Directive:
        """The directive for ``request``. ``invocation_due`` is when the
        invocation's limit falls due (``DuplexChild.limit_due()``)."""
        with self._lock:
            entry = _Entry(request if isinstance(request, InteractionRequest) else None)
            self._entries.append(entry)
            directive = self._decide(request, invocation_due, entry)
            if isinstance(directive, Deliver):
                entry.directive = "deliver"
                entry.decision = tuple(
                    str(kind) for kind in sorted(required_offers(directive.decision))
                )
            elif isinstance(directive, Pause):
                entry.directive = "pause"
                self._ended = directive
            else:
                directive = Fail(shown(directive.reason, MAX_REASON_CHARS))
                entry.directive = "fail"
                entry.reason = directive.reason
                # The first failure ends the invocation, also after a pause
                # whose provider asked again instead of stopping.
                if not isinstance(self._ended, Fail):
                    self._ended = directive
            return directive

    def _decide(
        self, request: InteractionRequest, invocation_due: float, entry: _Entry
    ) -> Directive:
        if not isinstance(request, InteractionRequest):
            return Fail("the adapter handed the broker something that is not a request")
        asked = f"the agent asked for a decision ({request.what})"
        if self._open.pop(request.request_id, None) != request:
            return Fail(
                f"{asked} as a request this broker did not issue, changed or already handled"
            )
        if self._ended is not None:
            return Fail(f"{asked} after an earlier request ended this invocation's interactions")
        if len(self._entries) > self._max_interactions:
            return Fail(
                f"interaction limit reached: {asked} past the {self._max_interactions} an "
                "invocation may make (max_interactions)"
            )
        if self._policy is InteractionPolicy.FAIL:
            return Fail(f"{asked}; interaction_policy is fail")
        if self._policy is InteractionPolicy.PAUSE:
            if self._pausable(request):
                return Pause(request)
            return Fail(f"{asked}; interaction_policy is pause, but no pause is available for it")
        if not request.offers:
            return Fail(
                f"{asked}; the provider cannot carry a live {request.kind} decision, so "
                "interaction_policy prompt has none to offer"
            )
        now = self._clock()
        wait_due = now + self._max_wait
        deadline = min(wait_due, invocation_due)
        if deadline <= now:
            return Fail(f"{asked}; no time is left before the invocation's limit")
        try:
            outcome = self._resolver.resolve(request, deadline)
        except Exception as exc:  # KeyboardInterrupt is not a decision (D7.7)
            return Fail(f"{asked}; asking for it failed ({type(exc).__name__})")
        finally:
            entry.waited_seconds = max(0.0, self._clock() - now)
        if not isinstance(outcome, Unavailable) and self._clock() > deadline:
            # A decision that came after the deadline is not taken: the wait's
            # limit is the broker's, whatever the resolver did.
            outcome = Unavailable(UnavailableReason.EXPIRED)
        if isinstance(outcome, Unavailable):
            reason = UnavailableReason(outcome.reason)
            entry.unavailable = str(reason)
            if reason is UnavailableReason.EXPIRED and invocation_due < wait_due:
                return Fail(f"{asked}; the invocation's limit fell due during the wait")
            if self._pausable(request):
                return Pause(request)
            text = _UNAVAILABLE_TEXT[reason]
            if reason is UnavailableReason.EXPIRED:
                text = f"no decision came within max_wait_seconds ({self._max_wait}s)"
            return Fail(f"{asked}; {text}, and no pause is available for it")
        problem = decision_problem(request, outcome)
        if problem is not None:
            return Fail(f"{asked}; the decision was not delivered: {problem}")
        return Deliver(outcome)

    def _pausable(self, request: InteractionRequest) -> bool:
        return self._pause_available and self._capabilities.can_pause(request.kind)

    def record(self) -> dict[str, object]:
        """A bounded, redacted account of the launch's requests for the run
        log: the policy, its limits and, per request, its id, kind, what was
        asked, the offers, the directive, the kinds of decision delivered,
        a fail reason and how long the wait took. Never an answer's text,
        guidance or a modified input."""
        with self._lock:
            return {
                "policy": str(self._policy),
                "max_wait_seconds": self._max_wait,
                "max_interactions": self._max_interactions,
                "handled": len(self._entries),
                "requests": [entry.record() for entry in self._entries],
            }
