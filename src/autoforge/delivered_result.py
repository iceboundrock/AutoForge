"""A reply delivered before its launch failed (#479; ADR 0012 D12): typed, provider-neutral, pure.

An agent can deliver its complete final reply and still end badly: its CLI
exits with a failing status after the reply, or stays alive after it (a
shutdown that hangs, a task left pending) until a limit kills it. The
engine used to discard such a reply with the failure, and the operator's
``resume`` paid for the whole step again (#285). Taking the reply is safe
only on proof, and this module holds the proof's shape: five distinctions
every adapter's facts are read through, and the fixed rule that says
whether a reply goes to the controller's checks at all.

**The five distinctions.** None implies the next.

- *Completed turn*: the provider's protocol shows that the agent's turn
  ended: Claude Code's ``stream-json`` gives every turn it opened its
  ``result`` with no tool call left open; Pi's RPC conversation settled
  (``agent_settled``). A provider whose output is plain text (OpenCode,
  Claude Code's ``text`` output) types no turn and so shows none.
- *Delivered complete result*: a completed turn that ended the run on a
  successful final reply, with nothing opened after it, that the adapter
  hands over whole (:attr:`~autoforge.providers.AgentExecutionResult.reply_delivered`,
  the adapter's typed fact, never its reading of text, and only from a
  protocol it read whole: a Claude ``stream-json`` line skipped past the
  per-line bound voids it, R3-F1 of PR #481), whose blocks say
  one thing (:func:`~autoforge.result_parser.reply_block_shape`), and that
  the capture bound did not cut.
- *Terminated process*: how the CLI ended (:class:`ProcessEnd`). A CLI
  that exits 0, or that its adapter stopped after its reply (#439), is read
  as always and never comes here; only a failing exit and a kill at a limit
  do.
- *Successful cleanup*: the executor's teardown is verified complete: no
  member of the launch's group outlived SIGKILL, no writer beyond the
  kill's reach held its pipes, no re-parented process outlived its kill,
  and the platform could check for those (``orphans_unchecked`` is false).
  A process the teardown killed is gone; one it could not, or could not
  look for, may still write to the candidate.
- *Accepted controller result*: never decided here. A reply this rule lets
  through goes to exactly the checks a reply of a clean exit goes to (the
  strict import or the verified export, the parse, the phase's own checks of
  the candidate's identity, the worktree's HEAD and the bound PR). Their
  refusal is final for that launch: nothing corrects a reply whose launch
  failed, and the failure stands. Their passing makes it *provisional*, no
  more (R1-F4 of PR #481): it is kept in the run's state with its binding
  (:class:`KeptReply`, saved before the step goes on from it, so a crash or
  a restart re-checks it rather than run the agent again, R1-F1), and it is
  *accepted* only once the step's own checks after it (the operator's
  checkout unmoved, the PR still the run's, the phase's verification) have
  applied it as the step's result (:func:`settled_record`). The save of the
  plan the verification turns it into is that acceptance, and marks the kept
  reply :attr:`KeptReply.applied` (R3-F2): a crash or an error after it
  leaves both the plan and the acceptance's provenance in the state, and the
  step that completes the plan records the reply as accepted.

**The rule** (:func:`refusal`) reads typed facts only and refuses, first
match wins: a request of the agent's for a human decision; a failure the
provider's protocol established (a provider failure, a failure before the
limit, a usage limit, a transient failure); a kill by any limit but the idle
limit and the maximum runtime (the loop detector's verdict is about the very
activity that produced the reply); a teardown that left a process that may
still run, or that could not look for one; no delivered reply; a reply whose
blocks are missing, unfinished or in conflict; a reply the capture bound
cut. Anything not proven is refused, and a refusal keeps the failure as the
step's outcome, which the recovery rule (ADR 0012 D11) then decides on as
before: a launch whose output held a complete block is never relaunched.

Pure: no process, no file, no clock, no GitHub, no state write. The engine
gathers the facts, runs the checks, keeps the reply in the state and records
the outcome (``execution.json``'s ``delivered_result``, provisional or
refused; the step's ``<PHASE>-delivered-result`` record, accepted or not).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from .errors import StateError
from .result_parser import BEGIN, END, MAX_CONTROL_RESULT_CHARS, BlockShape
from .runlog import STEP_DIR_RE
from .transitions import AGENT_PHASES

#: The limits after whose kill a delivered reply may still be taken: the
#: executor's own idle limit and maximum runtime (``executor.LIMIT_IDLE``,
#: ``executor.LIMIT_MAX_RUNTIME``). Not the loop detector's ``loop`` (#194),
#: nor a limit nothing named.
ACCEPTABLE_LIMITS = frozenset({"idle", "max_runtime"})


class ProcessEnd(StrEnum):
    """How a launch that ended badly ended: the *terminated process* distinction."""

    #: The CLI exited on its own with a status that says it failed, or its
    #: provider's protocol failed the run whatever the status (ADR 0003 §2.6
    #: handles both alike).
    FAILED_EXIT = "failed-exit"
    #: A limit killed it (``EndFacts.limit`` says which).
    KILLED = "killed"


class Refusal(StrEnum):
    """Why a launch's reply was not accepted; the first that applies, in this order."""

    #: A request of the agent's for a human decision ended the launch.
    INTERACTION = "interaction"
    #: The provider's protocol had failed the run (a provider failure, a
    #: failure before the limit, a usage limit, a transient failure).
    PROVIDER_FAILURE = "provider-failure"
    #: A limit other than the idle limit or the maximum runtime killed it.
    LIMIT = "limit"
    #: A process the launch started may still be running.
    SURVIVOR = "survivor"
    #: The platform could not check for a process the launch re-parented.
    ORPHANS_UNCHECKED = "orphans-unchecked"
    #: The provider's protocol shows no completed reply that ended the run,
    #: or the provider types none (plain-text output).
    NO_REPLY = "no-reply"
    #: The reply holds no complete block that is a JSON object within the bound.
    NO_BLOCK = "no-block"
    #: A block begins after the reply's last complete one and never ends.
    INCOMPLETE_BLOCK = "incomplete-block"
    #: Two complete blocks of the reply say different things.
    CONFLICTING_BLOCKS = "conflicting-blocks"
    #: The reply passed the capture bound, so only its tail was kept.
    TRUNCATED = "truncated"
    #: It went to the controller's checks, which refused it (the engine's,
    #: never this rule's).
    CHECKS = "checks"


REFUSAL_TEXT: Mapping[Refusal, str] = {
    Refusal.INTERACTION: "a request of the agent's for a human decision ended the launch",
    Refusal.PROVIDER_FAILURE: "the provider's protocol had already failed the run",
    Refusal.LIMIT: ("a limit other than the idle limit or the maximum runtime killed the launch"),
    Refusal.SURVIVOR: "a process the launch started may still be running",
    Refusal.ORPHANS_UNCHECKED: (
        "the platform could not check for a process the launch left outside its group"
    ),
    Refusal.NO_REPLY: (
        "the provider's protocol shows no completed final reply, or its output types none"
    ),
    Refusal.NO_BLOCK: "the reply holds no complete CONTROL_RESULT block",
    Refusal.INCOMPLETE_BLOCK: "a CONTROL_RESULT block begun after the last one never ends",
    Refusal.CONFLICTING_BLOCKS: "the reply's CONTROL_RESULT blocks say different things",
    Refusal.TRUNCATED: "the reply passed the capture bound and only its tail was kept",
    Refusal.CHECKS: "the controller's checks of the reply refused it",
}


@dataclass(frozen=True)
class EndFacts:
    """The typed facts of a launch that ended badly, as its adapter and executor gave them.

    Every field is a value the executor, the adapter or the parser's block
    framing established; none is text the agent or its CLI printed.
    """

    process_end: ProcessEnd
    #: For a kill: ``idle``, ``max_runtime``, ``loop``, or ``""`` when unknown.
    limit: str = ""
    #: The adapter's fact: its protocol shows a completed reply that ended
    #: the run, handed over whole (*delivered complete result*).
    reply_delivered: bool = False
    #: What the reply's blocks say as a whole (:func:`reply_block_shape`).
    blocks: BlockShape = BlockShape.NONE
    #: The reply passed the capture bound (``stdout_truncated``).
    truncated: bool = False
    #: The provider's protocol had failed the run: ``provider_failure``,
    #: ``failed_before_limit``, ``usage_limit`` or ``transient_failure``.
    provider_failed: bool = False
    #: A request for a human decision ended the launch (ADR 0007 D7.6).
    interaction: bool = False
    #: ``may_have_survivor``: a process the launch started may still run.
    may_have_survivor: bool = False
    #: ``orphans_unchecked``: the platform could not look for one.
    orphans_unchecked: bool = False

    @property
    def has_reply(self) -> bool:
        """Whether there is a reply to decide on: the adapter vouched for one, or
        the output holds a complete block (``holds_control_result``)."""
        return self.reply_delivered or self.blocks is not BlockShape.NONE

    def record(self) -> dict[str, object]:
        """The facts for ``execution.json``: typed values only."""
        return {
            "process_end": self.process_end.value,
            "limit": self.limit,
            "reply_delivered": self.reply_delivered,
            "blocks": self.blocks.value,
            "truncated": self.truncated,
        }


_BLOCK_REFUSALS: Mapping[BlockShape, Refusal] = {
    BlockShape.NONE: Refusal.NO_BLOCK,
    BlockShape.INCOMPLETE: Refusal.INCOMPLETE_BLOCK,
    BlockShape.CONFLICTING: Refusal.CONFLICTING_BLOCKS,
}


def refusal(facts: EndFacts) -> Refusal | None:
    """Why the launch's reply does not go to the controller's checks; ``None`` when it does.

    ``None`` is no acceptance: it lets the reply be checked as the reply of a
    clean exit is, and only those checks accept it (:attr:`Refusal.CHECKS`
    otherwise). Typed facts only, in the order of :class:`Refusal`.
    """
    if facts.interaction:
        return Refusal.INTERACTION
    if facts.provider_failed:
        return Refusal.PROVIDER_FAILURE
    if facts.process_end is ProcessEnd.KILLED and facts.limit not in ACCEPTABLE_LIMITS:
        return Refusal.LIMIT
    if facts.may_have_survivor:
        return Refusal.SURVIVOR
    if facts.orphans_unchecked:
        return Refusal.ORPHANS_UNCHECKED
    if not facts.reply_delivered:
        return Refusal.NO_REPLY
    shape = _BLOCK_REFUSALS.get(facts.blocks)
    if shape is not None:
        return shape
    if facts.truncated:
        return Refusal.TRUNCATED
    return None


#: ``execution.json``'s outcome for a reply that passed the checks of a clean
#: exit's reply: kept, and decided on by the step's own checks after it.
PROVISIONAL = "provisional"
#: ``execution.json``'s outcome for a reply the rule or the checks refused.
REFUSED = "refused"
#: The step's own record (``<PHASE>-delivered-result``): the step applied the
#: reply as its result, or stopped before it did.
ACCEPTED = "accepted"
NOT_ACCEPTED = "not-accepted"


class Carried(StrEnum):
    """How the provisional reply a step settles reached that step (R3-F2 of PR #481)."""

    #: Delivered by the step's own launch.
    FRESH = "fresh"
    #: Kept in the state across an interruption, not (yet) checked again.
    KEPT = "kept"
    #: Kept across an interruption, checked again and gone on from by this step.
    RECHECKED = "rechecked"
    #: Kept across an interruption after the step that went on from it saved
    #: the plan it turned the reply into (:attr:`KeptReply.applied`): accepted
    #: before the interruption, and this step goes on from that plan.
    APPLIED = "applied"


def outcome_record(facts: EndFacts, refused: Refusal | None) -> dict[str, object]:
    """``execution.json``'s ``delivered_result``: the outcome, the reason, the facts.

    A reply the checks of a clean exit's reply let through is
    :data:`PROVISIONAL`, never accepted: the step's own checks after it
    decide, in :func:`settled_record` (R1-F4 of PR #481).
    """
    record: dict[str, object] = {
        "outcome": PROVISIONAL if refused is None else REFUSED,
        **facts.record(),
    }
    if refused is not None:
        record["reason"] = refused.value
    return record


def settled_record(
    facts: EndFacts, *, accepted: bool, reason: str, launch_log: str, carried: Carried
) -> dict[str, object]:
    """The step's record of what became of a provisional reply (R1-F4 of PR #481).

    Written once, by the step that has the reply's outcome: :data:`ACCEPTED`
    when a step applied the reply as its result (its checks after the reply
    passed: the operator's checkout unmoved, the PR still the run's, and the
    phase's verification saved the plan it turned the reply into, or
    recorded the agent's own stop, whatever ended the step after that; R2-F4
    of PR #481), the step before an interruption included
    (:attr:`Carried.APPLIED`, R3-F2), :data:`NOT_ACCEPTED` with the step's
    typed ``reason`` (the stop's code, or the phase it moved to) otherwise.
    ``kept_across_restart`` says the reply reached the step from the state,
    ``applied_before_restart`` that it was accepted before the interruption.
    Typed values only.
    """
    record: dict[str, object] = {
        "outcome": ACCEPTED if accepted else NOT_ACCEPTED,
        **facts.record(),
        "launch_step": launch_log,
        "kept_across_restart": carried is not Carried.FRESH,
        "applied_before_restart": carried is Carried.APPLIED,
    }
    if not accepted:
        record["reason"] = reason
    return record


#: The refusals whose failure already says everything: the launch's own
#: protocol failed it, or a request for a decision ended it. Their error text
#: gains no line about a reply; the run log still records the decision.
SILENT_REFUSALS = frozenset({Refusal.INTERACTION, Refusal.PROVIDER_FAILURE})


def _end_text(facts: EndFacts) -> str:
    return (
        f"was killed at its {facts.limit or 'unnamed'} limit"
        if facts.process_end is ProcessEnd.KILLED
        else "exited with a failing status"
    )


def outcome_text(facts: EndFacts, refused: Refusal) -> str:
    """One sentence for the operator: how the launch ended, and why its output was refused."""
    return (
        f"The agent {_end_text(facts)}; its output was not accepted as the step's result "
        f"({refused.value}: {REFUSAL_TEXT[refused]})."
    )


def settled_text(facts: EndFacts, *, accepted: bool, carried: Carried) -> str:
    """One sentence for the operator once the step has its outcome (R1-F4 of PR #481).

    Acceptance is claimed only for a reply a step applied as its result (the
    verification saved the plan it turned it into, or recorded the agent's
    own stop; R2-F4), this step or the one an interruption ended after that
    save (:attr:`Carried.APPLIED`, R3-F2); a step that ended without applying
    it says so, and its own message, which says why, follows.
    """
    if carried is Carried.APPLIED:
        return (
            f"The agent {_end_text(facts)} after delivering its complete final reply; the "
            "teardown left no process behind, the reply passed the checks of a clean exit's "
            "reply, then the step's own checks, and the step saved the plan it turned the reply "
            "into before an interruption: it was accepted, the agent was not run again, and "
            "this step went on from that plan (#479)."
        )
    if carried is Carried.KEPT:
        return (
            f"The agent {_end_text(facts)} after delivering its complete final reply, which "
            "passed the checks of a clean exit's reply and was kept in the run's state across "
            "an interruption; this step ended without going on from it, so it was not accepted "
            "(#479). Why follows."
        )
    kept = (
        ", kept in the run's state across an interruption and checked again"
        if carried is Carried.RECHECKED
        else ""
    )
    head = (
        f"The agent {_end_text(facts)} after delivering its complete final reply; the "
        f"teardown left no process behind, and the reply passed the checks of a clean exit's "
        f"reply{kept}"
    )
    if accepted:
        return (
            f"{head}, then the step's own checks, and the step applied it as its result: it "
            "was accepted and the agent was not run again (#479)."
        )
    return (
        f"{head}, so the step went on from it without running the agent again; the step "
        "ended without applying it as its result, so it was not accepted (#479). Why follows."
    )


# -- the kept reply (R1-F1 of PR #481) ----------------------------------------------

_KEPT_KEYS = (
    "phase",
    "attempt",
    "launch_label",
    "issue_url",
    "pr_url",
    "review_round",
    "candidate",
    "head_sha",
    "base_ref",
    "merge_base_sha",
    "process_end",
    "limit",
    "run_log",
    "payload",
    "payload_sha256",
    "applied",
)
_SHA_RE = re.compile(r"[0-9a-f]{40}")
_DIGEST_RE = re.compile(r"[0-9a-f]{64}")
_MAX_TEXT_CHARS = 512


def payload_text(payload: Mapping[str, object]) -> str:
    """The canonical JSON of a kept payload: what its digest and its bound are over."""
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def payload_digest(payload: Mapping[str, object]) -> str:
    """SHA-256 of :func:`payload_text`; a lone surrogate (legal in JSON) is kept as is."""
    return hashlib.sha256(payload_text(payload).encode("utf-8", "surrogatepass")).hexdigest()


def payload_block(payload: Mapping[str, object]) -> str:
    """The payload as the one block a reply holds, for the parser to read again."""
    return f"{BEGIN}\n{payload_text(payload)}\n{END}"


def _fail(message: str) -> StateError:
    return StateError(f"state field 'delivered_reply': {message}")


def _text(raw: Mapping[str, object], key: str) -> str:
    value = raw[key]
    if not isinstance(value, str) or len(value) > _MAX_TEXT_CHARS:
        raise _fail(f"{key!r} is not a string of at most {_MAX_TEXT_CHARS} characters")
    return value


def _count(raw: Mapping[str, object], key: str, low: int) -> int:
    value = raw[key]
    if not isinstance(value, int) or isinstance(value, bool) or value < low:
        raise _fail(f"{key!r} is not an integer of at least {low}")
    return value


@dataclass(frozen=True)
class KeptReply:
    """A provisional reply, kept in the run's state with its binding (R1-F1 of PR #481).

    Saved in the one write after the checks of a clean exit's reply let it
    through and before the step goes on from it, so a crash or a restart
    before the step concludes checks it again rather than run the agent
    again. It is bound to the launch that delivered it (the phase, the
    attempt, the launch label, the issue, the PR and the review round as
    they stood), to the candidate those checks read, and to the revision
    the run was bound to (the PR's HEAD, base and merge base: the diff a
    review decides on, R2-F1); a later save in
    another phase, or after another launch, drops it
    (:meth:`autoforge.state.AutoForgeState.settle_delivered_reply`), and the
    step drops it when its own checks refuse the reply. The payload is the
    validated ``CONTROL_RESULT`` object, redacted as every agent text the
    engine persists is (the parser refuses a credential in a field the
    controller publishes, so redaction changes nothing it acts on), bounded
    by the parser's bound on a block, and bound by its digest.
    """

    phase: str
    attempt: int
    launch_label: str
    issue_url: str
    pr_url: str
    review_round: int
    #: The commit the checks read as the reply's candidate (the worktree's
    #: HEAD the reply names), or the HEAD a review was bound to; ``""`` for a
    #: phase that has none.
    candidate: str
    #: The revision the run was bound to when the reply was kept (R2-F1 of PR
    #: #481): the state's ``current_head_sha``, ``current_base_ref`` and
    #: ``current_merge_base_sha``, lower-cased SHAs, ``""`` where none was
    #: bound (an ANALYZE_EXECUTE before its PR). A restart goes on from the
    #: reply only while the run is bound to exactly this revision: a review's
    #: verdict is about the diff of this HEAD against this base from this
    #: merge base, and a REVIEW entry under bwrap or ``disabled`` binds them
    #: again from the PR as it is.
    head_sha: str
    base_ref: str
    merge_base_sha: str
    process_end: ProcessEnd
    limit: str
    #: The launch's step directory in the run log (``NNN-<phase>-<attempt>``).
    run_log: str
    payload: dict = field(default_factory=dict)
    #: The step's verification saved the plan it turned the reply into: the
    #: reply was *accepted* (R3-F2 of PR #481). Set in that save, so a crash
    #: or an error after it, before the step has its outcome, leaves the
    #: acceptance and its provenance in the state: the step that next enters
    #: the phase completes the plan from the journal and records the reply
    #: as accepted. An applied reply is that record's provenance only, never
    #: a reply to check again or go on from.
    applied: bool = False

    def revision(self) -> tuple[str, str, str]:
        """The revision it is bound to: ``(head_sha, base_ref, merge_base_sha)``."""
        return (self.head_sha, self.base_ref, self.merge_base_sha)

    def facts(self) -> EndFacts:
        """The facts the rule let the reply through on, as the note and the record read them."""
        return EndFacts(
            process_end=self.process_end,
            limit=self.limit,
            reply_delivered=True,
            blocks=BlockShape.ONE,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "phase": self.phase,
            "attempt": self.attempt,
            "launch_label": self.launch_label,
            "issue_url": self.issue_url,
            "pr_url": self.pr_url,
            "review_round": self.review_round,
            "candidate": self.candidate,
            "head_sha": self.head_sha,
            "base_ref": self.base_ref,
            "merge_base_sha": self.merge_base_sha,
            "process_end": self.process_end.value,
            "limit": self.limit,
            "run_log": self.run_log,
            "payload": self.payload,
            "payload_sha256": payload_digest(self.payload),
            "applied": self.applied,
        }

    @classmethod
    def from_dict(cls, raw: object) -> KeptReply:
        """The kept reply's whole shape, strictly: a field missing, extra or wrong refuses."""
        if not isinstance(raw, dict) or set(raw) != set(_KEPT_KEYS):
            raise _fail(f"does not hold exactly {', '.join(_KEPT_KEYS)}")
        phase = _text(raw, "phase")
        if phase not in {p.value for p in AGENT_PHASES}:
            raise _fail(f"phase {phase!r} is not an agent phase")
        launch_label = _text(raw, "launch_label")
        if not launch_label:
            raise _fail("'launch_label' is empty")
        candidate = _text(raw, "candidate")
        if candidate and not _SHA_RE.fullmatch(candidate):
            raise _fail("'candidate' is not a full lowercase commit SHA")
        for key in ("head_sha", "merge_base_sha"):
            sha = _text(raw, key)
            if sha and not _SHA_RE.fullmatch(sha):
                raise _fail(f"{key!r} is not a full lowercase commit SHA")
        try:
            process_end = ProcessEnd(raw["process_end"])
        except ValueError:
            raise _fail(f"'process_end' {raw['process_end']!r} is not one") from None
        limit = _text(raw, "limit")
        if (limit in ACCEPTABLE_LIMITS) is not (process_end is ProcessEnd.KILLED):
            raise _fail("'limit' is not the idle limit or the maximum runtime of a kill")
        run_log = _text(raw, "run_log")
        if not STEP_DIR_RE.match(run_log):
            raise _fail("'run_log' is not a step directory's name")
        payload = raw["payload"]
        if not isinstance(payload, dict) or payload.get("phase") != phase:
            raise _fail("'payload' is not a CONTROL_RESULT object of its phase")
        if len(payload_text(payload)) > MAX_CONTROL_RESULT_CHARS:
            raise _fail(f"'payload' is longer than {MAX_CONTROL_RESULT_CHARS} characters")
        digest = raw["payload_sha256"]
        if not isinstance(digest, str) or not _DIGEST_RE.fullmatch(digest):
            raise _fail("'payload_sha256' is not a SHA-256 digest")
        if digest != payload_digest(payload):
            raise _fail("'payload_sha256' is not the digest of its payload")
        applied = raw["applied"]
        if not isinstance(applied, bool):
            raise _fail("'applied' is not a boolean")
        return cls(
            phase=phase,
            attempt=_count(raw, "attempt", 1),
            launch_label=launch_label,
            issue_url=_text(raw, "issue_url"),
            pr_url=_text(raw, "pr_url"),
            review_round=_count(raw, "review_round", 0),
            candidate=candidate,
            head_sha=_text(raw, "head_sha"),
            base_ref=_text(raw, "base_ref"),
            merge_base_sha=_text(raw, "merge_base_sha"),
            process_end=process_end,
            limit=limit,
            run_log=run_log,
            payload=payload,
            applied=applied,
        )
