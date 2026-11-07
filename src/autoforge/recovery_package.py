"""The deterministic recovery action package (#374): what a stop proves, waits on and allows.

``status`` prints a stop's typed classification (ADR 0011) and the persisted
``block_reason``, which is text for a human. This module projects, from the
same strictly loaded state, what an operator needs to act on a stop without
interpreting that text: what the controller has proven, which action of the
saved plan is pending on which exact target, what is forbidden, what data
and worktree must not be touched, and the exact next commands. With
``status --live`` it adds ``unblock``'s own dry-run decision, which the CLI
reads from GitHub (ADR 0012 D3: inspection, which writes nothing).

Every claim carries its freshness: ``snapshot`` (the state file as last
saved, ``observed_at`` its ``updated_at``), ``live`` (read from GitHub by
this command, ``observed_at`` when), ``configuration`` (the configuration
this command loaded; no ``observed_at``) or ``unknown`` (none either). Only a
live read carries this command's clock, so two ``status`` runs over one state
and one configuration print the same package.

The diagnosis is the stop's reason code, which the controller sink that
entered ``BLOCKED`` or ``FAILED`` saved with it (``stop_code``, ADR 0011 D5 as
amended by #374), and what that code means and asks of the operator
(:data:`DIAGNOSES`). A stop saved before protocol 16 has no code, and none is
derived from the reason's text: its diagnosis is ``unknown``.

Advice, never authorization. Every command named here re-checks what it acts
on and may refuse: ``unblock`` decides again from its own reads, so a live
preview's "would re-enter" is neither a transition nor a permission, and
nothing that decides a phase, a write or a permission imports this module
(``tests/test_recovery_package.py``). Every action id, target and payload
reference comes from a persisted, validated effect record, and a command is
named only where the record's stage and authorization make it one that
``status`` or ``unblock`` accepts.

Every text the package carries from the state or a live read (a decision's
detail, a conflict's reason, an error) is redacted before it is bounded, so a
credential a cut would split is never left partly visible
(``docs/agent-guides/secrets-and-logging.md``); the CLI redacts the whole
output again.

Pure: no I/O, no clock, no GitHub, no state write. The state it is given
is read, never changed.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum

from .claims import ACCEPTANCE, OPERATOR_COMMENT, scan
from .effects import (
    AUTHORIZATION_POLICY,
    MAX_EFFECT_ATTEMPTS,
    EffectKind,
    EffectRecord,
    FixContext,
    Stage,
    sha256_text,
    unverified_before,
)
from .loop_guard import step_budget_reason
from .redaction import redact
from .state import MAX_DECLINES_PER_ROUND, AutoForgeState
from .stop_classification import (
    REASON_FAMILY,
    StopClassification,
    StopOutcome,
    StopReasonCode,
)
from .transitions import Phase

SCHEMA = "autoforge.recovery/1"

#: The longest text one field of the package carries (a live decision's
#: detail, a conflict's reason); a longer one is clipped, and says so.
MAX_TEXT_CHARS = 2000
#: The most pending actions listed; the rest are counted in ``unknown``.
MAX_PENDING = 32

#: What a live preview is, said wherever one is shown.
PREVIEW_NOTE = (
    "a preview of what a plain 'unblock' would decide from GitHub as read now; not a "
    "transition and not an authorization: 'unblock' reads GitHub again and decides then"
)

# The troubleshooting guide (#260) is the symptom-indexed source of truth;
# the package points into it and restates none of it.
GUIDE_BLOCKED = "docs/troubleshooting.md#blocked-the-controller-will-not-decide-alone"
GUIDE_WAITS = "docs/troubleshooting.md#the-run-waits-for-you-acceptance-and-operator-actions"
GUIDE_REPLAN = "docs/troubleshooting.md#a-replan"
GUIDE_STOPPED = "docs/troubleshooting.md#blocked-failed-or-state-unchanged"
GUIDE_RESUME = "docs/troubleshooting.md#resume-unblock-or-a-new-run"
GUIDE_STATE = "docs/troubleshooting.md#find-the-run-and-read-its-state"
GUIDE_READY = "docs/usage.md#ready_for_merge-a-human-merges-unless-the-gate-is-open"
GUIDE_PR_BODY = "docs/usage.md#when-a-fix-replaces-the-pr-description"
GUIDE_SCHEDULER = "docs/usage.md#recovery-and-exit-codes"
GUIDE_PAUSE = "docs/usage.md#an-agent-asked-for-permission-or-input"

_WHY = '"..."'


class Freshness(Enum):
    """How current a claim is, and where it was read."""

    #: The state file as last saved (``observed_at`` is its ``updated_at``).
    SNAPSHOT = "snapshot"
    #: Read from GitHub by this command (``status --live``).
    LIVE = "live"
    #: The configuration this command loaded (no ``observed_at``: this command's).
    CONFIGURATION = "configuration"
    #: Not known: nothing typed says, or nothing was read.
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Claim:
    """One statement of the package, with its freshness and when it was observed."""

    key: str
    text: str
    freshness: Freshness
    observed_at: str | None

    def as_dict(self) -> dict[str, object]:
        return {
            "key": self.key,
            "text": self.text,
            "freshness": self.freshness.value,
            "observed_at": self.observed_at,
        }

    def line(self) -> str:
        return _one_line(f"[{self.freshness.value}] {self.text}")


@dataclass(frozen=True)
class PendingAction:
    """A record of the saved plan that is not observed: what it waits for, on what.

    ``state`` is the state ``status`` already gives the record's kind
    (``awaiting_approval``, ``pending``, ``conflict``, ...), or the record's
    stage for a kind without one; ``required`` is what has to be true before
    the plan goes on, and ``observed`` what the controller last recorded of
    it (``status``'s label, or a conflict's reason). The payload is
    referenced, never quoted.
    """

    position: int
    kind: str
    action_id: str
    performer: str
    target: str
    stage: str
    attempts: int
    state: str
    authorization: str
    finding_id: str
    required: str
    observed: str
    payload_chars: int | None
    payload_sha256: str
    payload_command: str
    observed_at: str | None

    def as_dict(self) -> dict[str, object]:
        return {
            "position": self.position,
            "kind": self.kind,
            "action_id": self.action_id,
            "performer": self.performer,
            "target": self.target,
            "stage": self.stage,
            "attempts": self.attempts,
            "max_attempts": MAX_EFFECT_ATTEMPTS,
            "state": self.state,
            "authorization": self.authorization,
            "finding_id": self.finding_id,
            "required": self.required,
            "observed": self.observed,
            "payload_chars": self.payload_chars,
            "payload_sha256": self.payload_sha256,
            "payload_command": self.payload_command,
            "freshness": Freshness.SNAPSHOT.value,
            "observed_at": self.observed_at,
        }

    def line(self) -> str:
        name = self.action_id or f"effect {self.position}"
        text = f"[snapshot] {name} {self.kind} on {self.target}: {self.observed or self.state}"
        if self.payload_command:
            text += f"; payload {self.payload_chars} chars, sha256 {self.payload_sha256}"
        return _one_line(text)


@dataclass(frozen=True)
class Step:
    """One next step: an exact command, or a step no command performs (``command`` "").

    ``alternative`` marks a choice in place of the steps before it, rather
    than the next one in sequence.
    """

    command: str
    text: str
    alternative: bool = False

    def as_dict(self) -> dict[str, object]:
        return {"command": self.command, "text": self.text, "alternative": self.alternative}


@dataclass(frozen=True)
class Commands:
    """How the CLI spells the commands of this run, so the package invents none.

    ``cli`` renders a command of this run with the global flags this
    process was given; ``scheduler`` an ``epic`` command (``--config`` only);
    ``resume`` is what continues the run (``epic resume`` for a scheduler
    namespace's), ``""`` when which view applies is not known; ``worktree``
    describes the issue's agent worktree without reading git.
    """

    cli: Callable[..., str]
    scheduler: Callable[..., str]
    resume: str
    worktree: str


@dataclass(frozen=True)
class Policy:
    """The bounds and gate of the configuration this command loaded."""

    max_total_steps: int
    max_review_rounds: int
    allow_merge: bool


@dataclass(frozen=True)
class LiveInspection:
    """What ``status --live`` read: a plain ``unblock``'s dry-run decision, or why none.

    ``target_phase`` is the phase it would re-enter, ``""`` when it would
    stay BLOCKED; ``problem`` says why nothing was decided (a run that is not
    BLOCKED, a configuration ``unblock`` refuses, a read that failed), and
    then the decision fields are empty. ``configuration`` is the recorded-
    configuration check's notice, which ``unblock`` prints too.
    """

    observed_at: str | None
    applies: bool
    target_phase: str = ""
    detail: str = ""
    pr_url: str = ""
    pr_state: str = ""
    pr_head_sha: str = ""
    pr_base_ref: str = ""
    configuration: str = ""
    problem: str = ""

    @property
    def decided(self) -> bool:
        return self.applies and not self.problem


@dataclass(frozen=True)
class RecoveryPackage:
    """The package of one stop; see the module docstring."""

    outcome: StopOutcome
    code: StopReasonCode
    view: str
    snapshot_at: str | None
    diagnosis: dict[str, object]
    proven: tuple[Claim, ...]
    unknown: tuple[Claim, ...]
    pending: tuple[PendingAction, ...]
    forbidden: tuple[Claim, ...]
    preserve: tuple[Claim, ...]
    next: tuple[Step, ...]
    guide: str
    live: dict[str, object] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        """``recovery`` of ``status --json``: stable keys, derived, never merged into state."""
        return {
            "schema": SCHEMA,
            "outcome": self.outcome.value,
            "code": self.code.value,
            "view": self.view,
            "snapshot_at": self.snapshot_at,
            "diagnosis": dict(self.diagnosis),
            "proven": [c.as_dict() for c in self.proven],
            "unknown": [c.as_dict() for c in self.unknown],
            "pending": [p.as_dict() for p in self.pending],
            "forbidden": [c.as_dict() for c in self.forbidden],
            "preserve": [c.as_dict() for c in self.preserve],
            "next": [s.as_dict() for s in self.next],
            "live": dict(self.live),
            "guide": self.guide,
        }

    def lines(self) -> list[str]:
        """The human ``Recovery`` block of ``status`` (unredacted; the caller redacts)."""
        out = [
            "",
            f"Recovery ({self.outcome.value}; claims from the state saved at "
            f"{self.snapshot_at or 'an unknown time'} unless marked otherwise; advice, "
            "never an authorization):",
        ]
        if self.diagnosis:
            d = self.diagnosis
            out.append(
                "  "
                + _one_line(
                    f"diagnosis: [{d['freshness']}] {d['code']} ({d['family']}): {d['meaning']}"
                )
            )
        sections: list[tuple[str, list[str]]] = [
            ("proven", [c.line() for c in self.proven]),
            ("unknown", [c.line() for c in self.unknown]),
            ("pending", [p.line() for p in self.pending]),
            ("forbidden", [c.line() for c in self.forbidden]),
            ("do not touch", [c.line() for c in self.preserve]),
        ]
        for title, items in sections:
            if items:
                out.append(f"  {title}:")
                out += [f"    - {item}" for item in items]
        if self.next:
            out.append("  next:")
            number = 0
            for step in self.next:
                if step.alternative:
                    head = "      or:"
                else:
                    number += 1
                    head = f"    {number}."
                text = _one_line(step.text)
                body = f"{_one_line(step.command)}  ({text})" if step.command else text
                out.append(f"{head} {body}")
        out += _live_lines(self.live)
        if self.guide:
            out.append(f"  guide: {self.guide}")
        return out


def _live_lines(live: dict[str, object]) -> list[str]:
    if not live.get("requested"):
        if live.get("applies"):
            return ["  live: not read; 'status --live' reads unblock's decision from GitHub"]
        return []
    if not live.get("applies") or live.get("problem"):
        return ["  " + _one_line(f"live: [unknown] {live.get('problem')}")]
    head = f"  live (read from GitHub at {live['observed_at']}; {PREVIEW_NOTE}):"
    lines = [head]
    if live["target_phase"]:
        decision = f"would re-enter {live['target_phase']}"
    else:
        decision = "would stay BLOCKED"
    lines.append("    " + _one_line(f"decision: [live] {decision}: {live['detail']}"))
    pr = live.get("pr")
    if isinstance(pr, dict):
        lines.append(
            f"    PR: [live] {pr['url']} {pr['state'] or '-'} at HEAD {pr['head_sha'] or '-'}"
        )
    drift = live.get("drift")
    for moved in drift if isinstance(drift, list) else []:
        lines.append(
            f"    drift: [live] {moved['key']} is {moved['live'] or '-'}; the snapshot "
            f"says {moved['snapshot'] or '-'}"
        )
    if live.get("configuration"):
        lines.append("    " + _one_line(f"configuration: {live['configuration']}"))
    return lines


def _clip(text: str) -> str:
    """``text`` redacted, then within :data:`MAX_TEXT_CHARS` (``--json`` keeps it whole).

    Redacted first: a credential whose shape needs its full length, cut in
    two by the bound, would no longer match the redactor and be left mostly
    visible (PR #470 R1-F4). The output is redacted again where it is printed.
    """
    text = redact(str(text))
    if len(text) <= MAX_TEXT_CHARS:
        return text
    return text[: MAX_TEXT_CHARS - 1] + "…"


def _one_line(text: str) -> str:
    """``text`` on one line, for the human block."""
    return " ".join(str(text).split())


def _target_of(record: EffectRecord) -> str:
    """The object a record writes to, as :meth:`EffectRecord.describe` names it."""
    for key in ("epic_url", "pr_url", "issue_url", "ref", "repository"):
        value = record.target.get(key)
        if value:
            return str(value)
    return "-"


# -- the labels status gives each kind of action (#211, #243, #423) ----------------------


def acceptance_label(record: EffectRecord) -> tuple[str, str]:
    """``(state, label)`` of a persisted K9 record, as ``status`` names it (#211)."""
    if record.stage is Stage.OBSERVED:
        return "verified", f"verified: {(record.observed or {}).get('url', '-')}"
    if record.stage is Stage.CONFLICT:
        return "verification_failed", f"verification failed: {record.reason}"
    if record.stage is Stage.ATTEMPTED:
        return (
            "published_unverified",
            f"published, not yet verified ({record.attempts}/{MAX_EFFECT_ATTEMPTS})",
        )
    if not record.authorized:
        return "awaiting_approval", "awaiting approval"
    if record.precondition["authorization"] == AUTHORIZATION_POLICY:
        return "authorized_by_policy", "authorized by policy, not yet published"
    return "approved", "approved, not yet published"


def pr_body_label(record: EffectRecord) -> tuple[str, str]:
    """``(state, label)`` of a persisted K12 record, as ``status`` names it (#423)."""
    if record.stage is Stage.OBSERVED:
        digest = (record.observed or {}).get("body_sha256", "-")
        return "verified", f"verified: sha256 {digest}; read again before FIX completes"
    if record.stage is Stage.CONFLICT:
        return "conflict", f"conflict: {record.reason}"
    if record.stage is Stage.ATTEMPTED:
        return (
            "written_unverified",
            f"written, not yet verified ({record.attempts}/{MAX_EFFECT_ATTEMPTS})",
        )
    if not record.authorized:
        return "awaiting_approval", "awaiting approval"
    if record.precondition["authorization"] == AUTHORIZATION_POLICY:
        return "authorized_by_policy", "authorized by policy, not yet written"
    return "approved", "approved, not yet written"


def operator_label(record: EffectRecord) -> tuple[str, str]:
    """``(state, label)`` of a persisted K10 record, as ``status`` names it (#243).

    "pending" means not yet verified, never "not posted": ``status`` reads
    nothing from GitHub for it. ``verification_failed`` is a conflict, or
    what FIX's last verification of a pending action found wrong (a changed
    or deleted bound comment, a target that cannot be read or no longer
    qualifies), kept on the record as its reason (PR #254 R3-F3).
    """
    if record.stage is Stage.OBSERVED:
        observed = record.observed or {}
        how = (
            "exact"
            if observed.get("evidence") == "exact"
            else "operator-bound; adequacy not verified by the controller"
        )
        return "verified", f"verified ({how}): {observed.get('url', '-')}"
    if record.stage is Stage.CONFLICT:
        return "verification_failed", f"verification failed: {record.reason}"
    if record.reason:
        return (
            "verification_failed",
            f"verification failed at FIX's last check: {record.reason}",
        )
    if record.binding is not None:
        return (
            "evidence_bound",
            f"evidence bound, not yet verified: {record.binding['comment_url']}",
        )
    return "pending", "pending (not yet verified)"


# -- what a saved code means and asks of the operator (#374) ----------------------------------


@dataclass(frozen=True)
class Diagnosis:
    """What a stop's reason code says happened, and what the operator does about it.

    Each restates the refusal or recovery text its sinks already write
    (``engine.py``), never a new rule; ``remedy`` ends where the package's
    own steps (the live preview, ``unblock``, ``resume``) take over.
    """

    meaning: str
    remedy: str


_UNBLOCK_AFTER = "then 'unblock' (it re-reads GitHub and may refuse)"

#: A diagnosis for every code a ``BLOCKED`` or ``FAILED`` sink of the engine
#: names (``tests/test_recovery_package.py`` holds the engine to it). A code
#: with none here gets :func:`diagnosis_of`'s family text.
DIAGNOSES: dict[StopReasonCode, Diagnosis] = {
    StopReasonCode.STEP_BUDGET: Diagnosis(
        "the run used every step workflow.max_total_steps allows; 'resume' does not reset the "
        "count",
        f"raise workflow.max_total_steps in the configuration, {_UNBLOCK_AFTER}",
    ),
    StopReasonCode.REVIEW_ROUND_CAP: Diagnosis(
        "the review and fix loop reached workflow.max_review_rounds with findings still open, "
        "so no further round starts",
        "inspect the open findings and the PR; to let the loop go on, raise "
        "workflow.max_review_rounds in the configuration first ('unblock' refuses while the "
        f"cap holds), {_UNBLOCK_AFTER}; or leave the PR to a human",
    ),
    StopReasonCode.REVIEW_STAGNATION: Diagnosis(
        "review rounds stopped making progress: the same findings, or as many, came back "
        "round after round (workflow.stagnation_identical_rounds and "
        "workflow.stagnation_unchanged_count_rounds)",
        "inspect the findings the rounds repeat and the PR; resolve them by hand or relax "
        f"those bounds, {_UNBLOCK_AFTER}: it re-enters FIX with the open findings, and "
        "'unblock --rereview' asks for a fresh review round instead",
    ),
    StopReasonCode.REPLAN_REFUSED: Diagnosis(
        "the replacement of the PR (REPLAN_REEXECUTE) was refused, and the refusal is recorded "
        "in the replan journal",
        "inspect the replan journal and the PRs it names: 'unblock' refuses while a journal "
        "is recorded, so a new run is the way on",
    ),
    StopReasonCode.PRODUCER_VALIDATION_BOUND: Diagnosis(
        "the agent's candidate failed the configured validation commands as often as the "
        "bound allows (ADR 0006)",
        "inspect the validation records and the candidate in the agent worktree; fix the "
        f"code or the validation commands, {_UNBLOCK_AFTER}: it sets the validation override "
        "watermark, so the next gate validates again",
    ),
    StopReasonCode.VALIDATION_INTERRUPTED: Diagnosis(
        "a validation run of the candidate was interrupted (a crash or a killed process), "
        "so its outcome is unknown",
        f"{_UNBLOCK_AFTER}: it sets the validation override watermark, so the same candidate "
        "is validated again",
    ),
    StopReasonCode.VALIDATION_INCONCLUSIVE: Diagnosis(
        "the controller could not validate the candidate to a verdict before publishing it; "
        "nothing was pushed or published",
        f"fix what kept the validation from running (the reason names it), {_UNBLOCK_AFTER}",
    ),
    StopReasonCode.SELECTION_REJECTED: Diagnosis(
        "UPDATE_EPIC selected an unusable next issue as often as it may; the run stays on "
        "its issue and nothing was switched",
        "pick the next issue by hand (a new run for it), or fix the EPIC, then 'unblock'",
    ),
    StopReasonCode.ACCEPTANCE_ACTION: Diagnosis(
        "the saved FIX plan waits for the operator: an acceptance comment (A-...) or a PR "
        "description update (B-...) awaits approval, or what it wrote no longer holds",
        "the action's own steps below",
    ),
    StopReasonCode.OPERATOR_ACTIONS: Diagnosis(
        "the saved FIX plan waits for operator actions (X-...): comments only the operator "
        "posts, on other issues, which the controller then verifies",
        "the actions' own steps below",
    ),
    StopReasonCode.UNJOURNALED_WRITE: Diagnosis(
        "GitHub holds an object of the kind this phase writes (a comment, an issue, a PR, a "
        "branch) that this run's journal does not record, so the controller cannot tell it "
        "for its own",
        "inspect the object the reason names: remove it if it is not this run's, or leave it "
        f"if it is, {_UNBLOCK_AFTER}",
    ),
    StopReasonCode.EFFECT_CONFLICT: Diagnosis(
        "a write of the saved plan met an object it did not expect: one carrying its "
        "identity that is not its own, or one changed after the controller verified it",
        "inspect the object the conflict names; the conflict's own steps below say what "
        "puts it right",
    ),
    StopReasonCode.CLAIM_AMBIGUOUS: Diagnosis(
        "more than one object claims the identity the controller looks for (two PRs with the "
        "issue's marker, two comments of one round), and it never chooses between them",
        "close, delete or unmark every one that is not the run's own, then 'unblock'",
    ),
    StopReasonCode.PR_OWNER: Diagnosis(
        "the PR's implementation marker does not name this run (another run, no run, or a "
        "marker that cannot be read), and a run drives only a PR its marker names",
        "leave the PR to the run its marker names; or, if this run should drive it, take it "
        "over (the operator's decision, below)",
    ),
    StopReasonCode.DEFAULT_BRANCH: Diagnosis(
        "the repository's default branch is not the one this phase's entry recorded, or a "
        "write would have targeted it; the controller never writes to the default branch",
        "inspect the repository's default branch; 'unblock' starts a fresh entry that reads "
        "it again",
    ),
    StopReasonCode.READ_BACK_MISMATCH: Diagnosis(
        "what GitHub holds after a write of the plan is not what the plan wrote (a PR head "
        "the plan did not put there, a body that reads back otherwise)",
        "inspect the object the reason names on GitHub; the controller writes nothing over "
        f"it, so put it right or start a new run, {_UNBLOCK_AFTER}",
    ),
    StopReasonCode.FIX_PLAN_REFUSED: Diagnosis(
        "the FIX plan could not be made from the fixer's accepted result: an object it would "
        "write to (a follow-up issue, the PR) is not as the entry read it; nothing was sent",
        "inspect the object the reason names; 'unblock' launches the fixer afresh",
    ),
    StopReasonCode.EPIC_ROADMAP: Diagnosis(
        "the EPIC's roadmap markers cannot be read as one section (repeated, unbalanced or "
        "out of order), and the controller edits only the text between them",
        "repair the EPIC's body so that it carries one roadmap section (or none), then 'unblock'",
    ),
    StopReasonCode.GITHUB_READ_FAILED: Diagnosis(
        "a GitHub read failed conclusively (authentication, permissions or malformed data), "
        "not transiently: reading again gets the same answer",
        "fix the access: 'gh auth status', the credential's scopes, and its access to the "
        f"repository and to the object the reason names; {_UNBLOCK_AFTER}",
    ),
    StopReasonCode.GITHUB_CHECKS_REFUSED: Diagnosis(
        "GitHub refused this credential the PR's check runs (#291): a permission problem, "
        "never 'no checks' and never a pass, so the merge gate cannot judge the checks",
        "run AutoForge with a credential that may read check runs: 'gh auth login' (an OAuth "
        "login) or a classic token with the 'repo' scope, since a fine-grained token cannot "
        "be given Checks: read, and a GitHub App token needs Checks: read granted; "
        f"{_UNBLOCK_AFTER}",
    ),
    StopReasonCode.MERGE_INCONCLUSIVE: Diagnosis(
        "the merge gate's verification of the PR stayed inconclusive within its bound",
        f"inspect the PR on GitHub, {_UNBLOCK_AFTER}",
    ),
    StopReasonCode.PR_IDENTITY: Diagnosis(
        "the PR the run is bound to is not the PR its review, branch or repository says it "
        "is, and the controller never re-binds a review to another PR",
        "inspect the state's PR and review binding and the PR on GitHub",
    ),
    StopReasonCode.PR_NOT_OPEN: Diagnosis(
        "the PR is not open, and only an open PR is merged",
        "reopen the PR, or start a new run",
    ),
    StopReasonCode.UNREVIEWED_MERGE: Diagnosis(
        "the PR was merged at a revision no clean review of this run decided on, so the "
        "controller does not count it",
        "inspect the merge; update the EPIC by hand and start a new run for the next issue",
    ),
    StopReasonCode.MERGE_CHECKS: Diagnosis(
        "the PR's checks at the reviewed HEAD did not all pass (failing, pending, or none "
        "reported), and the merge gate needs every required one to pass",
        f"fix or re-run the checks on GitHub, {_UNBLOCK_AFTER}",
    ),
    StopReasonCode.MERGE_CONFLICT: Diagnosis(
        "the PR has merge conflicts with its base",
        f"resolve the conflict on the PR's branch (a new HEAD is reviewed again), {_UNBLOCK_AFTER}",
    ),
    StopReasonCode.MERGE_NOT_READY: Diagnosis(
        "GitHub reports the PR not ready to merge (a draft, a required review or a branch "
        "protection rule it does not meet)",
        f"meet what GitHub requires, {_UNBLOCK_AFTER}",
    ),
    StopReasonCode.MERGE_WRITE: Diagnosis(
        "a merge write failed or its outcome is not known, and the controller does not send "
        "a merge again",
        f"inspect the PR on GitHub, merged or not, {_UNBLOCK_AFTER}: it reconciles a merge",
    ),
    StopReasonCode.PREMERGE_VERIFICATION: Diagnosis(
        "the configured pre-merge verification commands failed or could not run on the PR's "
        "revision; nothing was merged or counted",
        f"fix the cause, {_UNBLOCK_AFTER}",
    ),
    StopReasonCode.REVIEW_CERTIFICATE: Diagnosis(
        "the clean review has no applicable review certificate (ADR 0006): the candidate's "
        "validation does not back it",
        f"{_UNBLOCK_AFTER}: from a clean review it holds none for, it decides a fresh "
        "validated REVIEW round",
    ),
    StopReasonCode.REVIEW_NOT_BACKED: Diagnosis(
        "the review comment the state names does not back a clean review of this revision "
        "on this PR",
        "inspect the state's review binding and the PR's review comments",
    ),
    StopReasonCode.CHECKOUT_DRIFT: Diagnosis(
        "the operator's checkout (its HEAD or branch) moved while an agent ran",
        f"inspect the checkout and put it back, and the agent's GitHub work, {_UNBLOCK_AFTER}",
    ),
    StopReasonCode.SANDBOX_TAMPER: Diagnosis(
        "the issue's private object directory names another object store or holds what no "
        "store does (ADR 0005 D6.5); nothing was read from it",
        "inspect the agent worktree's object directory; a human decides",
    ),
    StopReasonCode.AGENT_BLOCKED: Diagnosis(
        "the phase's agent reported 'blocked'; its message, the reason, is its claim, never "
        "evidence",
        "read the agent's message and its run log, and resolve what it asks for; 'unblock' "
        "then launches the phase's agent again",
    ),
    StopReasonCode.AGENT_FAILURE: Diagnosis(
        "the phase's agent reported 'failure'; FAILED has no outgoing edge",
        "read the agent's message and its run log; a new run starts over",
    ),
}


def diagnosis_of(code: StopReasonCode) -> Diagnosis:
    """The :class:`Diagnosis` of ``code``, or a family's when the table holds none."""
    found = DIAGNOSES.get(code)
    if found is not None:
        return found
    if code is StopReasonCode.UNKNOWN:
        return Diagnosis(
            "no code says why: the sink named none, or the stop was saved before protocol 16",
            "read the reason and the run's log; the reason is text for a human, never evidence",
        )
    family = REASON_FAMILY[code].value.replace("_", " ")
    return Diagnosis(
        f"reason code {code.value} ({family})",
        "read the reason and the run's log",
    )


# -- the package ----------------------------------------------------------------------------


_ACTION_KINDS = frozenset(
    {EffectKind.ACCEPTANCE_COMMENT, EffectKind.OPERATOR_COMMENT, EffectKind.PR_BODY_UPDATE}
)


def build_package(
    stop: StopClassification,
    state: AutoForgeState,
    *,
    commands: Commands,
    policy: Policy,
    live: LiveInspection | None = None,
) -> RecoveryPackage:
    """The package of ``stop``, the classification of the loaded ``state``.

    ``live`` is ``status --live``'s inspection, ``None`` when it was not
    requested. Nothing is read but its arguments.
    """
    at = state.updated_at or None
    builder = _Builder(stop, state, commands, policy, at)
    builder.build()
    return RecoveryPackage(
        outcome=stop.outcome,
        code=stop.code,
        view=stop.view,
        snapshot_at=at,
        diagnosis=_diagnosis_document(stop, state, at),
        proven=tuple(builder.proven),
        unknown=tuple(builder.unknown),
        pending=tuple(builder.pending),
        forbidden=tuple(builder.forbidden),
        preserve=tuple(builder.preserve),
        next=tuple(builder.steps),
        guide=builder.guide,
        live=_live_document(state, live),
    )


def _diagnosis_document(
    stop: StopClassification, state: AutoForgeState, at: str | None
) -> dict[str, object]:
    """``recovery.diagnosis``: the saved code of a ``BLOCKED`` or ``FAILED``, and what it means.

    Empty for every other outcome, whose code is its outcome's own. The code
    is a snapshot fact, saved with the stop by the sink that entered it
    (``stop_code``), never derived from the reason's text; ``unknown`` when
    none was saved.
    """
    if stop.outcome not in (StopOutcome.BLOCKED, StopOutcome.FAILED):
        return {}
    known = stop.code is not StopReasonCode.UNKNOWN
    found = diagnosis_of(stop.code)
    if known:
        saved = "saved with the stop by the controller sink that entered it"
    elif state.stop_code:
        saved = f"the sink saved {state.stop_code!r}, which names no cause this controller knows"
    else:
        saved = "no code was saved: the stop was saved before protocol 16"
    return {
        "code": stop.code.value,
        "family": stop.family.value,
        "meaning": found.meaning,
        "remedy": found.remedy,
        "source": saved,
        "freshness": (Freshness.SNAPSHOT if known else Freshness.UNKNOWN).value,
        "observed_at": at if known else None,
    }


def _live_document(state: AutoForgeState, live: LiveInspection | None) -> dict[str, object]:
    """``recovery.live``: always the same keys, ``requested`` false without ``--live``."""
    applies = state.phase is Phase.BLOCKED
    document: dict[str, object] = {
        "requested": live is not None,
        "applies": applies,
        "freshness": Freshness.UNKNOWN.value,
        "observed_at": None,
        "decision": "not_decided",
        "target_phase": None,
        "detail": "",
        "pr": None,
        "drift": [],
        "configuration": "",
        "problem": "",
        "note": PREVIEW_NOTE,
    }
    if live is None:
        if applies:
            document["problem"] = "not read: 'status --live' reads unblock's decision from GitHub"
        return document
    document["configuration"] = _clip(live.configuration)
    if not live.decided:
        document["problem"] = _clip(
            live.problem
            or f"'unblock' applies to BLOCKED runs only and this run is {state.phase.value}; "
            "nothing was read for it"
        )
        return document
    document.update(
        freshness=Freshness.LIVE.value,
        observed_at=live.observed_at,
        decision="would_reenter" if live.target_phase else "would_stay_blocked",
        target_phase=live.target_phase or None,
        detail=_clip(live.detail),
    )
    if live.pr_url:
        document["pr"] = {
            "url": live.pr_url,
            "state": live.pr_state,
            "head_sha": live.pr_head_sha,
            "base_ref": live.pr_base_ref,
        }
        drift = []
        for key, snapshot, now in (
            ("pr_url", state.current_pr_url, live.pr_url),
            ("pr_head_sha", state.current_head_sha, live.pr_head_sha),
            ("pr_base_ref", state.current_base_ref, live.pr_base_ref),
        ):
            if snapshot and now and snapshot != now:
                drift.append({"key": key, "snapshot": snapshot, "live": now})
        document["drift"] = drift
    return document


class _Builder:
    """Collects one package's sections from the state; reads nothing else."""

    def __init__(
        self,
        stop: StopClassification,
        state: AutoForgeState,
        commands: Commands,
        policy: Policy,
        at: str | None,
    ) -> None:
        self.stop = stop
        self.state = state
        self.commands = commands
        self.policy = policy
        self.at = at
        self.effects = state.phase_effects()
        self.records = self.effects.records
        self.proven: list[Claim] = []
        self.unknown: list[Claim] = []
        self.pending: list[PendingAction] = []
        self.forbidden: list[Claim] = []
        self.preserve: list[Claim] = []
        self.steps: list[Step] = []
        self.guide = ""

    # -- helpers --------------------------------------------------------------------------
    def snap(self, key: str, text: str) -> Claim:
        return Claim(key, text, Freshness.SNAPSHOT, self.at)

    def conf(self, key: str, text: str) -> Claim:
        return Claim(key, text, Freshness.CONFIGURATION, None)

    def cli(self, *words: str) -> str:
        return self.commands.cli(*words)

    @property
    def scheduler(self) -> bool | None:
        return self.stop.scheduler

    def plain_unblock(self) -> str:
        return f"{self.cli('unblock', '--reason')} {_WHY}"

    def unblock_text(self, text: str) -> str:
        """``text``, and for a namespace's run that ``unblock`` waits for its scheduler."""
        if self.scheduler:
            return f"{text} (once the scheduler has exited: it holds the controller lock)"
        return text

    def resume_step(self, text: str = "continue the run") -> Step:
        if self.commands.resume:
            return Step(self.commands.resume, text)
        return Step(
            "",
            f"{text} with 'resume', or 'epic resume' for a scheduler's issue (this command "
            "could not tell which applies: no git repository to find the scheduler root in)",
        )

    # -- the sections ---------------------------------------------------------------------
    def build(self) -> None:
        self.facts()
        outcome = self.stop.outcome
        if outcome is StopOutcome.BLOCKED:
            self.blocked()
        elif outcome is StopOutcome.FAILED:
            self.failed()
        elif outcome is StopOutcome.IN_PROGRESS:
            self.in_progress()
        elif outcome is StopOutcome.READY_FOR_MERGE:
            self.ready_for_merge()
        elif outcome is StopOutcome.HANDOFF:
            self.handoff()
        elif outcome is StopOutcome.AWAITING_INPUT:
            self.guide = GUIDE_PAUSE
            self.steps.append(
                Step(
                    "",
                    "none: a pause is answered by its interaction owner (#126), and no "
                    "route answers one until #126 lands",
                )
            )
        elif outcome is StopOutcome.UNKNOWN:
            self.guide = GUIDE_STATE
            self.steps.append(Step("", "investigate: 'status', the run's logs and GitHub"))
        if self.scheduler:
            self.forbidden.append(
                self.snap(
                    "standalone-commands",
                    "a standalone 'resume', 'step' or 'run' on this state directory: it is a "
                    "scheduler namespace's, which they refuse; the scheduler's 'epic' "
                    "commands continue or release it",
                )
            )

    def facts(self) -> None:
        s = self.state
        self.proven.append(
            self.snap("phase", f"phase {s.phase.value}, after {s.step_count} step(s)")
        )
        self.proven.append(self.snap("issue", f"issue {s.current_issue_url or '-'}"))
        if s.current_pr_url:
            pr = f"PR {s.current_pr_url}"
            if s.current_head_sha:
                pr += f" at HEAD {s.current_head_sha}"
            if s.current_base_ref:
                pr += f" on base {s.current_base_ref}"
            self.proven.append(self.snap("pr", pr))
        else:
            self.proven.append(self.snap("pr", "no PR is bound to the run"))
        if s.current_branch:
            self.proven.append(self.snap("branch", f"branch {s.current_branch}"))
        if s.reviewed_head_sha:
            self.proven.append(
                self.snap(
                    "review",
                    f"review round {s.review_round} reviewed HEAD {s.reviewed_head_sha} "
                    f"(last result {s.last_review_result or '-'}, {len(s.open_findings)} "
                    "open finding(s))",
                )
            )
        push = self.push_record()
        if push is not None:
            self.proven.append(
                self.snap(
                    "candidate",
                    f"candidate {push.payload.get('sha', '-')} for "
                    f"{push.target.get('ref', '-')}: push effect {push.position} "
                    f"{push.stage.value}",
                )
            )
        for record in self.records:
            # A push is the candidate claim above.
            if record.stage is Stage.OBSERVED and record.kind is not EffectKind.PUSH:
                url = (record.observed or {}).get("url", "")
                name = record.action_id or record.describe()
                self.proven.append(
                    self.snap(
                        f"observed-{record.position}",
                        f"{name} ({record.kind.value}) observed" + (f": {url}" if url else ""),
                    )
                )
        if s.validation_attempt:
            self.proven.append(
                self.snap(
                    "validation-attempt",
                    "a validation attempt is marked pending: the next step or resume records "
                    "it INTERRUPTED and runs it again",
                )
            )
        spent = " (reached)" if s.step_count >= self.policy.max_total_steps else ""
        self.proven.append(
            self.conf(
                "steps",
                f"{s.step_count} of workflow.max_total_steps {self.policy.max_total_steps} "
                f"step(s) used{spent}; 'resume' does not reset it",
            )
        )
        capped = " (reached)" if s.review_round >= self.policy.max_review_rounds else ""
        self.proven.append(
            self.conf(
                "review-rounds",
                f"{s.review_round} review round(s) completed; workflow.max_review_rounds is "
                f"{self.policy.max_review_rounds}{capped}",
            )
        )
        if self.stop.code is StopReasonCode.UNKNOWN and self.stop.outcome in (
            StopOutcome.BLOCKED,
            StopOutcome.FAILED,
        ):
            why = (
                "the sink that entered it named none"
                if s.stop_code
                else "none was saved with it (a stop saved before protocol 16)"
            )
            self.unknown.append(
                Claim(
                    "code",
                    f"the reason code of the step that stopped: {why}, and the reason's text "
                    "is for a human, never evidence",
                    Freshness.UNKNOWN,
                    None,
                )
            )
        if self.unsaved_fix_candidate():
            self.unknown.append(
                Claim(
                    "candidate",
                    "whether the agent worktree holds a FIX candidate: FIX's entry for review "
                    f"round {s.review_round} (reviewed HEAD {s.reviewed_head_sha or '-'}) saved "
                    "no plan before the run stopped, so a commit the fixer made is in that "
                    "worktree only, and no record names it",
                    Freshness.UNKNOWN,
                    None,
                )
            )
        if self.scheduler is None:
            self.unknown.append(
                Claim(
                    "view",
                    "whether this state directory is a scheduler namespace's: no git "
                    "repository to find the scheduler root in, so only the routes both share "
                    "are given",
                    Freshness.UNKNOWN,
                    None,
                )
            )

    def push_record(self) -> EffectRecord | None:
        return next((r for r in self.records if r.kind is EffectKind.PUSH), None)

    def unsaved_fix_candidate(self) -> bool:
        """Whether FIX's entry was saved but no plan was (PR #470 R1-F3).

        The fixer commits in the agent worktree, and the controller names the
        candidate only in the plan it saves with the push (K1). A FIX that
        stopped between them (the fixer's own ``blocked``, a result check, a
        read before the plan) leaves whatever the fixer committed in the
        worktree and nowhere else, so what it holds is not known here.
        """
        return self.effects.phase is Phase.FIX and self.effects.context is None

    def unpushed(self) -> str:
        """The unpushed candidate the worktree holds, named, or "" when none is known."""
        push = self.push_record()
        if push is not None and push.stage is not Stage.OBSERVED:
            return f"the unpushed candidate {push.payload.get('sha', '-')}"
        if self.unsaved_fix_candidate():
            return (
                "whatever the fixer committed past the reviewed HEAD "
                f"{self.state.reviewed_head_sha or '-'} (no saved record names it)"
            )
        return ""

    def not_unblockable(self) -> None:
        """``unblock``'s own refusal of a run that is not BLOCKED (exit 2)."""
        self.forbidden.append(
            self.snap(
                "unblock",
                f"'unblock': it applies to BLOCKED runs only, and this run is "
                f"{self.state.phase.value}",
            )
        )

    def candidate_preservation(self) -> None:
        """The worktree of a FIX that stopped before its plan was saved (PR #470 R1-F3)."""
        if not self.unsaved_fix_candidate():
            return
        reviewed = self.state.reviewed_head_sha or "the reviewed HEAD"
        self.preserve.append(
            self.snap(
                "worktree",
                f"the agent worktree {self.commands.worktree}: it may hold a FIX candidate no "
                "saved record names (FIX stopped before its plan was saved); do not reset, "
                "clean or remove it. Inspect it read-only first: 'git log --oneline "
                f"{reviewed}..HEAD' and 'git status' in it. A FIX that runs again is "
                "launched in it as it is",
            )
        )

    def plan_preservation(self) -> None:
        """What a saved plan, or a FIX entry without one, asks the operator to leave as it is."""
        self.candidate_preservation()
        if not self.records:
            return
        if self.stop.outcome is StopOutcome.FAILED:
            use = "kept for the record; a new run replaces it, and what it sent stays on GitHub"
        elif self.stop.outcome is StopOutcome.BLOCKED:
            use = "'unblock' and 'resume' complete it from the journal"
        else:
            use = "'resume' completes it from the journal"
        self.preserve.append(
            self.snap(
                "journal",
                f"the saved plan in the state file ({len(self.records)} effect record(s)): "
                f"{use}; do not edit or remove the state file",
            )
        )
        push = self.push_record()
        if push is not None and push.stage is not Stage.OBSERVED:
            branch = str(push.target.get("ref", "")).removeprefix("refs/heads/")
            self.preserve.append(
                self.snap(
                    "worktree",
                    f"the agent worktree {self.commands.worktree}: it holds the unpushed "
                    f"candidate {push.payload.get('sha', '-')} (push effect {push.position} "
                    f"to {branch}); do not reset, clean or remove it",
                )
            )
        for record in self.records:
            if record.stage is not Stage.OBSERVED or record.kind not in _ACTION_KINDS:
                continue
            if record.kind is EffectKind.PR_BODY_UPDATE:
                digest = (record.observed or {}).get("body_sha256", "-")
                edited = (
                    "; it changed since, so restore exactly that payload or decline the update, "
                    "and make no other edit"
                    if self.k12_edited(record)
                    else "; do not edit it"
                )
                self.preserve.append(
                    self.snap(
                        f"evidence-{record.action_id}",
                        f"the description of {record.target.get('pr_url', '-')}, verified at "
                        f"sha256 {digest} for {record.action_id}: FIX reads it again before "
                        f"it completes{edited}",
                    )
                )
            else:
                url = (record.observed or {}).get("url", "-")
                self.preserve.append(
                    self.snap(
                        f"evidence-{record.action_id}",
                        f"comment {url}, verified for {record.action_id}: the next review is "
                        "handed it as evidence; do not edit or delete it",
                    )
                )

    def worktree_note(self) -> None:
        """The worktree, when no unpushed candidate already names it."""
        if any(c.key == "worktree" for c in self.preserve):
            return
        works = (
            "a new run's agents work in it"
            if self.stop.outcome is StopOutcome.FAILED
            else "a resumed phase works in it"
        )
        self.preserve.append(
            self.conf(
                "worktree",
                f"the agent worktree {self.commands.worktree} (not read here): the "
                f"controller never resets, deletes or prunes it, and {works}; inspect it "
                "before changing anything there",
            )
        )

    def pending_actions(self) -> None:
        """Every record not observed, and a verified one the run waits on again (R1-F5)."""
        listed = 0
        open_ = [r for r in self.records if r.stage is not Stage.OBSERVED or self.waits(r)]
        for record in open_:
            if listed == MAX_PENDING:
                left = len(open_) - listed
                self.unknown.append(
                    Claim(
                        "pending-omitted",
                        f"{left} more record(s) of the saved plan are not observed and not "
                        "listed here; 'status --json' holds every one in 'effect_records'",
                        Freshness.SNAPSHOT,
                        self.at,
                    )
                )
                return
            self.pending.append(self.pending_action(record))
            listed += 1

    def pending_action(self, record: EffectRecord) -> PendingAction:
        kind = record.kind
        finding = ""
        authorization = ""
        payload_command = ""
        chars: int | None = None
        digest = ""
        if kind is EffectKind.OPERATOR_COMMENT:
            state, label = operator_label(record)
            finding = scan(OPERATOR_COMMENT, record.marker).claims[0].finding_id
            required = (
                "exactly one comment on the target with this action's marker and exactly the "
                "payload's bytes, or an existing comment the operator binds; the operator "
                "posts it, never the controller"
            )
            target = str(record.target.get("issue_url", "-"))
        elif kind is EffectKind.ACCEPTANCE_COMMENT:
            state, label = acceptance_label(record)
            finding = scan(ACCEPTANCE, record.marker).claims[0].finding_id
            authorization = str(record.precondition.get("authorization", ""))
            required = (
                "an approval of exactly this action id, then the controller posts the payload"
                if authorization != AUTHORIZATION_POLICY
                else "nothing of the operator: the policy recorded with the plan authorized it"
            )
            target = str(record.target.get("issue_url", "-"))
        elif kind is EffectKind.PR_BODY_UPDATE:
            state, label = pr_body_label(record)
            authorization = str(record.precondition.get("authorization", ""))
            required = (
                "an approval of exactly this action id, then the controller writes the payload "
                f"over the description at sha256 {record.precondition.get('base_sha256', '-')}"
                if authorization != AUTHORIZATION_POLICY
                else "nothing of the operator: the policy recorded with the plan authorized it; "
                f"the description must still be at sha256 "
                f"{record.precondition.get('base_sha256', '-')}"
            )
            target = str(record.target.get("pr_url", "-"))
            if record.stage is Stage.OBSERVED:
                state = "changed_after_verification"
                label = (
                    f"verified at sha256 {(record.observed or {}).get('body_sha256', '-')}, then "
                    "the description changed: the run stopped with code effect-conflict and no "
                    "record in conflict"
                )
                required = (
                    "the description back to exactly the payload; or, where 'unblock' allows "
                    "one, a decline of exactly this id, which keeps the edit and runs FIX again"
                )
            context = self.effects.context
            if isinstance(context, FixContext):
                ids = [
                    r["finding_id"] for r in context.resolutions if r["pr_body_source"] is not None
                ]
                finding = ", ".join(ids)
        else:
            state = record.stage.value
            label = record.reason
            required = (
                "the controller's write, reconciled by its identity before anything is sent again"
            )
            target = _target_of(record)
        if record.action_id:
            chars = len(record.body)
            digest = sha256_text(record.body)
            payload_command = self.cli("status", "--action", record.action_id)
        if record.stage is Stage.CONFLICT:
            required = (
                "the object the conflict names repaired or removed, so that 'unblock' finds "
                "exactly the record's object, or none"
            )
        return PendingAction(
            position=record.position,
            kind=kind.value,
            action_id=record.action_id,
            performer="operator" if record.operator_performed else "controller",
            target=target,
            stage=record.stage.value,
            attempts=record.attempts,
            state=state,
            authorization=authorization,
            finding_id=finding,
            required=required,
            observed=_clip(label),
            payload_chars=chars,
            payload_sha256=digest,
            payload_command=payload_command,
            observed_at=self.at,
        )

    def new_run_steps(self, failed: bool = False) -> None:
        s = self.state
        unpushed = self.unpushed()
        keep = (
            f"; its agents work in the same worktree, so keep {unpushed} first (a branch or a "
            "tag of your own) if you want it"
            if unpushed
            else ""
        )
        if self.scheduler:
            number = s.current_issue_url.rstrip("/").rsplit("/", 1)[-1]
            self.steps.append(
                Step(
                    self.commands.scheduler("epic", "archive", s.epic_url, number),
                    f"release the issue for a fresh admission by the scheduler{keep}",
                    alternative=not failed,
                )
            )
            return
        if self.scheduler is None:
            self.steps.append(
                Step(
                    "",
                    "a new run ('run --force' replaces this one), or 'epic archive' for a "
                    f"scheduler's issue{keep}",
                    alternative=not failed,
                )
            )
            return
        self.steps.append(
            Step(
                self.cli("run", "--force", "--epic", s.epic_url, "--issue", s.current_issue_url),
                f"abandon this run for a new one; nothing on GitHub is undone{keep}",
                alternative=not failed,
            )
        )

    # -- per outcome ----------------------------------------------------------------------
    def blocked(self) -> None:
        s = self.state
        self.forbidden.append(
            self.snap(
                "resume",
                "'resume' and 'step': both refuse a BLOCKED run; only 'unblock' leaves it, "
                "and no agent runs until it has",
            )
        )
        self.pending_actions()
        self.plan_preservation()
        self.worktree_note()
        budget = step_budget_reason(s.step_count, self.policy.max_total_steps)
        if s.replan_transaction:
            self.guide = GUIDE_REPLAN
            self.forbidden.append(
                self.snap(
                    "unblock-replan",
                    "a plain 'unblock': it refuses while a replan journal is recorded, since "
                    "REVIEW is that transaction's only entry",
                )
            )
            self.preserve.append(
                self.snap(
                    "replan-journal",
                    "the replan journal and the PRs it names: inspect them; do not edit the "
                    "journal",
                )
            )
            self.steps.append(Step("", "inspect the replan journal above and the PRs it names"))
            self.new_run_steps(failed=True)
            return
        if budget:
            self.forbidden.append(
                self.conf(
                    "unblock-budget",
                    f"a plain 'unblock' while the step budget is spent ({budget}): it refuses",
                )
            )
            self.steps.append(
                Step(
                    "",
                    "raise workflow.max_total_steps in the configuration ('resume' does not "
                    "reset the count)",
                )
            )
        code = self.stop.code
        capped = s.review_round >= self.policy.max_review_rounds
        if code is StopReasonCode.REVIEW_ROUND_CAP and capped:
            self.forbidden.append(
                self.conf(
                    "unblock-review-cap",
                    f"a plain 'unblock' while workflow.max_review_rounds is "
                    f"{self.policy.max_review_rounds} and {s.review_round} round(s) are done: "
                    "it refuses, since the next round could never start",
                )
            )
        if self.operator_steps():
            self.new_run_steps()
            return
        self.guide = self.guide or GUIDE_BLOCKED
        conflicts = [r for r in self.records if r.stage is Stage.CONFLICT]
        if conflicts:
            for record in conflicts:
                self.steps.append(
                    Step(
                        "",
                        f"inspect the object this conflict names ({_clip(record.reason)}): "
                        "repair or remove it if it is not the run's own; the run's own "
                        "object, misread by a controller before #327, needs no change. "
                        "After 'unblock', the phase it re-enters reconciles the record "
                        "again and blocks again on a true conflict",
                    )
                )
        elif code is StopReasonCode.STEP_BUDGET:
            if not budget:
                self.steps.append(
                    Step(
                        "",
                        "the configuration loaded allows more steps than the run has used "
                        f"(workflow.max_total_steps {self.policy.max_total_steps})",
                    )
                )
        elif code is StopReasonCode.REVIEW_ROUND_CAP and not capped:
            self.steps.append(
                Step(
                    "",
                    "inspect the open findings and the PR: the configuration loaded allows "
                    f"another round (workflow.max_review_rounds {self.policy.max_review_rounds})",
                )
            )
        elif self.k12_edited() is None:
            self.k12_maybe_edited_step()
        else:
            self.steps.append(Step("", diagnosis_of(code).remedy))
        if code is StopReasonCode.GITHUB_CHECKS_REFUSED:
            self.steps.append(
                Step(
                    self.cli("doctor"),
                    "check what the credential may read: its 'pull request check runs "
                    "readable' row tries the merge gate's checks read",
                )
            )
        self.steps.append(
            Step(
                self.cli("status", "--live"),
                "preview the decision 'unblock' would take from GitHub now; writes nothing",
            )
        )
        self.steps.append(
            Step(
                self.plain_unblock(),
                self.unblock_text(
                    "the controller re-reads GitHub and re-enters a phase, or refuses and "
                    "the run stays BLOCKED"
                ),
            )
        )
        self.steps.append(self.resume_step())
        self.code_alternatives(code)
        self.new_run_steps()

    def code_alternatives(self, code: StopReasonCode) -> None:
        """Another way out that ``unblock`` takes for this code, where it applies."""
        if code is StopReasonCode.PR_OWNER:
            bound = (
                f" (the run is bound to {self.state.current_pr_url})"
                if self.state.current_pr_url
                else ""
            )
            self.steps.append(
                Step(
                    f"{self.cli('unblock', '--take-over')} <PR URL> --reason {_WHY}",
                    self.unblock_text(
                        "or, if this run should drive the PR the reason names, take it over "
                        f"in place of the plain unblock{bound}: the controller writes this run "
                        "into its marker and reads it back first; then 'resume'"
                    ),
                    alternative=True,
                )
            )
        rereview = code is StopReasonCode.REVIEW_STAGNATION or (
            code is StopReasonCode.AGENT_BLOCKED and self.unsaved_fix_candidate()
        )
        if rereview and self.state.open_findings:
            self.steps.append(
                Step(
                    f"{self.cli('unblock', '--rereview', '--reason')} {_WHY}",
                    self.unblock_text(
                        "or, if the findings were resolved outside the repository, a fresh "
                        "review round in place of another fixer launch (none is marked "
                        "resolved); then 'resume'"
                    ),
                    alternative=True,
                )
            )

    # -- what waits for the operator (K9, K10, K12) ---------------------------------------
    def k12_edited(self, record: EffectRecord | None = None) -> bool | None:
        """Whether the run stopped on a verified PR body update edited since (PR #470 R1-F5).

        A verified K12 stays observed, and FIX reads its body again before each
        later write: one that no longer holds the payload blocks with
        ``effect-conflict`` and no record in conflict. ``True`` when the saved
        code says so, ``False`` when it says otherwise or no verified update is
        in the plan, ``None`` when no code was saved (a stop before protocol
        16), so only a live read can tell.
        """
        record = record or self.observed_update()
        if record is None or self.stop.outcome is not StopOutcome.BLOCKED:
            return False
        if any(r.stage is Stage.CONFLICT for r in self.records):
            return False
        if self.stop.code is StopReasonCode.UNKNOWN:
            return None
        return self.stop.code is StopReasonCode.EFFECT_CONFLICT

    def observed_update(self) -> EffectRecord | None:
        return next(
            (
                r
                for r in self.records
                if r.kind is EffectKind.PR_BODY_UPDATE and r.stage is Stage.OBSERVED
            ),
            None,
        )

    def waits(self, record: EffectRecord) -> bool:
        """Whether ``record`` waits for the operator before the plan can go on."""
        if record.kind is EffectKind.OPERATOR_COMMENT:
            return record.stage is not Stage.OBSERVED
        if record.kind in (EffectKind.ACCEPTANCE_COMMENT, EffectKind.PR_BODY_UPDATE):
            if record.stage is Stage.CONFLICT:
                return True
            if record.stage is Stage.INTENDED and not record.authorized:
                return True
        if record.kind is EffectKind.PR_BODY_UPDATE and record.stage is Stage.OBSERVED:
            return self.k12_edited(record) is True
        return False

    def operator_steps(self) -> bool:
        """The steps of the plan's first wait for the operator; whether the plan waits.

        A plan is driven in order, and an ``unblock`` that is applied leaves
        BLOCKED at once: a second ``unblock`` is refused, and so is any
        command named for a later stop. So the steps are one sequence, for the
        first record that waits (every pending operator action with it, since
        FIX verifies them together, and the bindings in one ``unblock``), up to
        the ``resume`` that continues the run; a record that waits after it is
        the next stop's, which ``status`` then shows (PR #470 R1-F2). Every
        waiting record still gets its forbidden claims.
        """
        waiting = [r for r in self.records if self.waits(r)]
        if not waiting:
            return False
        for record in waiting:
            self.forbid_for(record)
        first = waiting[0]
        if first.kind is EffectKind.OPERATOR_COMMENT:
            group = [r for r in waiting if r.kind is EffectKind.OPERATOR_COMMENT]
            self.operator_action_steps(group)
        elif first.kind is EffectKind.ACCEPTANCE_COMMENT:
            group = [first]
            self.acceptance_steps(first)
        else:
            group = [first]
            self.pr_body_steps(first)
        later = [r.action_id for r in waiting if r.position not in {g.position for g in group}]
        if later:
            self.steps.append(
                Step(
                    self.cli("status"),
                    f"then, if the run stops again on {', '.join(later)}, which wait after "
                    "these, read that stop's own steps here: none of its commands applies "
                    "before",
                )
            )
        return True

    def forbid_for(self, record: EffectRecord) -> None:
        action = record.action_id
        if record.kind is EffectKind.OPERATOR_COMMENT:
            self.forbidden.append(
                self.snap(
                    f"approve-{action}",
                    f"'unblock --approve-action {action}': the operator performs an "
                    "operator action, and nothing approves it",
                )
            )
            if record.stage is Stage.CONFLICT:
                self.forbidden.append(
                    self.snap(
                        f"bind-{action}",
                        f"'unblock --bind-evidence {action}': it is in conflict, which is "
                        "repaired, never bound over",
                    )
                )
            return
        if record.stage is Stage.INTENDED and not record.authorized:
            self.forbidden.append(
                self.snap(
                    f"stale-{action}",
                    f"an approval of any id but {action}: an approval names exactly this "
                    "record's id, and an id the record does not hold is refused",
                )
            )
        if record.kind is EffectKind.PR_BODY_UPDATE:
            refusal = self.pr_body_decline_refusal(record)
            if refusal:
                self.forbidden.append(
                    self.snap(f"decline-{action}", f"'unblock --decline-action {action}' {refusal}")
                )

    def declines_left(self) -> bool:
        """Whether the round holds fewer declines than ``unblock`` records for one."""
        return self.state.round_declines() < MAX_DECLINES_PER_ROUND

    def pr_body_decline_refusal(self, record: EffectRecord) -> str:
        """Why ``unblock`` refuses to decline ``record`` now, or "" (#423, PR #467)."""
        ahead = ", ".join(r.action_id for r in unverified_before(self.records, record))
        if ahead:
            return f"until {ahead} is verified: it is refused"
        if record.stage is Stage.OBSERVED and any(
            r.attempts or r.stage is not Stage.INTENDED for r in self.records[record.position + 1 :]
        ):
            return (
                "while a later write of its plan is out: it is refused, since dropping the "
                "plan would abandon that write"
            )
        if not self.declines_left():
            return (
                f"in review round {self.state.review_round}: it already records "
                f"{self.state.round_declines()} declined actions, the most the controller keeps"
            )
        return ""

    def operator_action_steps(self, records: list[EffectRecord]) -> None:
        """K10: post (or bind) every pending action, then one unblock (PR #470 R1-F2).

        A ``--bind-evidence`` that is applied re-enters FIX at once, and FIX
        verifies every pending action then: the bound ones by their binding,
        the posted ones on their targets. So the bindings go in one command, in
        place of the plain unblock, after every other action is posted; one
        bind per command would leave the second refused.
        """
        self.guide = GUIDE_WAITS
        # A comment already bound, and not found wrong by FIX, is left in place.
        bindable = [
            r for r in records if r.stage is Stage.INTENDED and (r.binding is None or r.reason)
        ]
        for record in records:
            action = record.action_id
            target = record.target["issue_url"]
            read = self.cli("status", "--action", action)
            self.steps.append(Step(read, f"read the exact payload of {action}"))
            if record.stage is Stage.CONFLICT:
                self.steps.append(
                    Step(
                        "",
                        f"repair the comment(s) on {target} its conflict names, so that at most "
                        f"one carries its marker and that one holds exactly the payload "
                        f"({_clip(record.reason)})",
                    )
                )
                continue
            if record.binding is not None and not record.reason:
                self.steps.append(
                    Step(
                        "",
                        f"leave the comment bound as its evidence in place and unchanged "
                        f"({record.binding['comment_url']}); FIX verifies it",
                    )
                )
                continue
            self.steps.append(
                Step(
                    f"{read} | gh issue comment {target} --body-file -",
                    f"post it on {target} yourself (the controller never does), unless you "
                    "bind an earlier comment as its evidence in the unblock below",
                )
            )
        self.steps.append(
            Step(
                self.plain_unblock(),
                self.unblock_text(
                    "once every action above is posted (or its bound comment is in place): "
                    "FIX then verifies each on GitHub and completes the plan"
                ),
            )
        )
        if bindable:
            pairs = "".join(f" --bind-evidence {r.action_id} <comment URL>" for r in bindable)
            self.steps.append(
                Step(
                    f"{self.cli('unblock')}{pairs} --reason {_WHY}",
                    self.unblock_text(
                        "or, in place of that unblock, bind comments already on the targets "
                        "as the evidence of the actions you did not post, all in this one "
                        "command (drop the pair of each action you posted): a bind that is "
                        "applied leaves BLOCKED at once, so a second unblock would be refused"
                    ),
                    alternative=True,
                )
            )
        self.steps.append(self.resume_step())
        if self.declines_left():
            declines = [a for r in records for a in ("--decline-action", r.action_id)]
            self.steps.append(
                Step(
                    f"{self.cli('unblock', *declines, '--reason')} {_WHY}",
                    self.unblock_text(
                        "or withdraw them all in place of the steps above: FIX runs the fixer "
                        "again; then 'resume'"
                    ),
                    alternative=True,
                )
            )

    def approval_steps(self, record: EffectRecord, do_it_yourself: str) -> None:
        action = record.action_id
        self.steps.append(
            Step(self.cli("status", "--action", action), f"read the exact payload of {action}")
        )
        self.steps.append(
            Step(
                f"{self.cli('unblock', '--approve-action', action, '--reason')} {_WHY}",
                self.unblock_text("approve exactly this action; 'resume' then publishes it"),
            )
        )
        self.steps.append(
            Step(
                self.plain_unblock(),
                self.unblock_text(
                    f"or {do_it_yourself} yourself first, then this plain unblock in place of "
                    "the approval"
                ),
                alternative=True,
            )
        )
        self.steps.append(self.resume_step())

    def acceptance_steps(self, record: EffectRecord) -> None:
        self.guide = GUIDE_WAITS
        if record.stage is Stage.CONFLICT:
            self.steps.append(
                Step(
                    "",
                    f"repair or remove the comment(s) the conflict names: {_clip(record.reason)}",
                )
            )
            self.steps.append(Step(self.plain_unblock(), self.unblock_text("then a plain unblock")))
            self.steps.append(self.resume_step())
            return
        self.approval_steps(record, "post exactly that payload on the run's issue")
        if self.declines_left():
            self.steps.append(
                Step(
                    f"{self.cli('unblock', '--decline-action', record.action_id, '--reason')} "
                    f"{_WHY}",
                    self.unblock_text(
                        "or decline it in place of the steps above: FIX runs the fixer again; "
                        "then 'resume'"
                    ),
                    alternative=True,
                )
            )

    def pr_body_steps(self, record: EffectRecord) -> None:
        action = record.action_id
        self.guide = GUIDE_PR_BODY
        payload = self.cli("status", "--action", action)
        if record.stage is Stage.OBSERVED:
            # PR #470 R1-F5: verified, then edited; FIX never writes over the edit.
            digest = (record.observed or {}).get("body_sha256", "-")
            self.steps.append(
                Step(
                    payload,
                    f"read the payload of {action}, which the controller verified as the PR's "
                    f"description (sha256 {digest}) before the description was edited",
                )
            )
            self.steps.append(
                Step(
                    "",
                    "set the PR description back to exactly that payload (GitHub keeps the "
                    "edit in its history)",
                )
            )
            self.steps.append(
                Step(
                    self.plain_unblock(),
                    self.unblock_text("FIX then completes the plan with no fixer launched"),
                )
            )
            self.steps.append(self.resume_step())
        elif record.stage is Stage.CONFLICT:
            self.steps.append(Step(payload, f"read the exact payload of {action}"))
            self.steps.append(
                Step(
                    "",
                    "put the PR description back to sha256 "
                    f"{record.precondition.get('base_sha256', '-')}, or set it to exactly the "
                    "payload",
                )
            )
            self.steps.append(Step(self.plain_unblock(), self.unblock_text("then a plain unblock")))
            self.steps.append(self.resume_step())
        else:
            self.approval_steps(record, "set the PR description to exactly that payload")
        if not self.pr_body_decline_refusal(record):
            self.steps.append(
                Step(
                    f"{self.cli('unblock', '--decline-action', action, '--reason')} {_WHY}",
                    self.unblock_text(
                        "or keep the description as it is and decline the update in place of "
                        "the steps above: FIX runs the fixer again over it; then 'resume'"
                    ),
                    alternative=True,
                )
            )

    def k12_maybe_edited_step(self) -> None:
        """A verified update, a stop saved with no code: what a live read would tell."""
        record = self.observed_update()
        assert record is not None
        action = record.action_id
        decline = (
            ""
            if self.pr_body_decline_refusal(record)
            else (
                f", or keep the edit and decline it ("
                f"{self.cli('unblock', '--decline-action', action, '--reason')} {_WHY})"
            )
        )
        self.steps.append(
            Step(
                "",
                "no code says why the run stopped; if 'status --live' says the PR description "
                f"changed after the controller verified {action}: set it back to exactly its "
                f"payload ({self.cli('status', '--action', action)}) and unblock{decline}. "
                "Otherwise read the reason and the run's log",
            )
        )

    def failed(self) -> None:
        self.guide = GUIDE_STOPPED
        self.forbidden.append(
            self.snap(
                "unblock",
                "'unblock' and 'resume': FAILED has no outgoing edge; 'unblock' refuses every "
                "phase but BLOCKED",
            )
        )
        self.pending_actions()
        self.plan_preservation()
        self.worktree_note()
        self.steps.append(Step("", diagnosis_of(self.stop.code).remedy))
        self.new_run_steps(failed=True)

    def in_progress(self) -> None:
        s = self.state
        self.guide = GUIDE_RESUME
        self.not_unblockable()
        self.pending_actions()
        self.plan_preservation()
        if s.phase not in (Phase.INITIALIZING, Phase.READY_FOR_MERGE):
            self.worktree_note()
        budget = step_budget_reason(s.step_count, self.policy.max_total_steps)
        text = (
            f"re-enters {s.phase.value}: its entry reconciles with GitHub before any agent "
            "is launched"
        )
        if self.records:
            text += (
                ", and a record whose outcome is not known is reconciled by its identity "
                "before anything is sent again"
            )
        if s.phase is Phase.MERGE:
            text += "; MERGE runs only with safety.allow_merge: true and --allow-merge"
        if budget:
            text += f"; but the step budget is spent ({budget}), so the next step blocks"
        self.steps.append(self.resume_step(text))

    def ready_for_merge(self) -> None:
        s = self.state
        self.guide = GUIDE_READY
        self.not_unblockable()
        self.steps.append(
            Step(
                "",
                f"merge {s.current_pr_url or 'the PR'} by hand on GitHub (and close its issue "
                "if the merge does not)",
            )
        )
        if self.scheduler is False:
            gate = "true" if self.policy.allow_merge else "false"
            self.steps.append(
                Step(
                    self.cli("resume", "--allow-merge"),
                    "or let the controller merge after its own pre-merge verification: only "
                    f"with safety.allow_merge: true (the configuration loaded says {gate})",
                    alternative=True,
                )
            )

    def handoff(self) -> None:
        s = self.state
        self.guide = GUIDE_SCHEDULER
        self.not_unblockable()
        number = s.current_issue_url.rstrip("/").rsplit("/", 1)[-1]
        self.steps.append(
            Step(
                self.commands.scheduler("epic", "resume", s.epic_url),
                f"the scheduler continues {s.phase.value} (its workers never execute it)",
            )
        )
        self.steps.append(
            Step(
                self.commands.scheduler("epic", "archive", s.epic_url, number),
                "or release the issue for a fresh admission",
                alternative=True,
            )
        )
