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
  the adapter's typed fact, never its reading of text), whose blocks say
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
  the candidate's identity, the worktree's HEAD and the bound PR), and only
  their passing accepts it. Their refusal is final for that launch: nothing
  corrects a reply whose launch failed, and the failure stands.

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

Pure: no process, no file, no clock, no GitHub, no state. The engine
gathers the facts, runs the checks and records the outcome
(``execution.json``'s ``delivered_result``).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from .result_parser import BlockShape

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


def outcome_record(facts: EndFacts, refused: Refusal | None) -> dict[str, object]:
    """``execution.json``'s ``delivered_result``: the outcome, the reason, the facts."""
    record: dict[str, object] = {
        "outcome": "accepted" if refused is None else "refused",
        **facts.record(),
    }
    if refused is not None:
        record["reason"] = refused.value
    return record


#: The refusals whose failure already says everything: the launch's own
#: protocol failed it, or a request for a decision ended it. Their error text
#: gains no line about a reply; the run log still records the decision.
SILENT_REFUSALS = frozenset({Refusal.INTERACTION, Refusal.PROVIDER_FAILURE})


def outcome_text(facts: EndFacts, refused: Refusal | None) -> str:
    """One sentence for the operator: how the launch ended and what became of its output."""
    end = (
        f"was killed at its {facts.limit or 'unnamed'} limit"
        if facts.process_end is ProcessEnd.KILLED
        else "exited with a failing status"
    )
    if refused is None:
        return (
            f"The agent {end} after delivering its complete final reply; the teardown left no "
            "process behind, and the reply passed every check a reply of a clean exit "
            "passes, so it was accepted and the step was not run again (#479)."
        )
    return (
        f"The agent {end}; its output was not accepted as the step's result "
        f"({refused.value}: {REFUSAL_TEXT[refused]})."
    )
