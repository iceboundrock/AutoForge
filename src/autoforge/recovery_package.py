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
and one configuration print the same package. The stop's reason code is ``unknown`` for a state
loaded by a later command, by design: no code is persisted (ADR 0011 D5),
and none is derived from the reason's text.

Advice, never authorization. Every command named here re-checks what it acts
on and may refuse: ``unblock`` decides again from its own reads, so a live
preview's "would re-enter" is neither a transition nor a permission, and
nothing that decides a phase, a write or a permission imports this module
(``tests/test_recovery_package.py``). Every action id, target and payload
reference comes from a persisted, validated effect record, and a command is
named only where the record's stage and authorization make it one that
``status`` or ``unblock`` accepts.

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
from .state import AutoForgeState
from .stop_classification import StopClassification, StopOutcome, StopReasonCode
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
    """``text`` within :data:`MAX_TEXT_CHARS`, as it is otherwise (``--json`` keeps it)."""
    text = str(text)
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
        proven=tuple(builder.proven),
        unknown=tuple(builder.unknown),
        pending=tuple(builder.pending),
        forbidden=tuple(builder.forbidden),
        preserve=tuple(builder.preserve),
        next=tuple(builder.steps),
        guide=builder.guide,
        live=_live_document(state, live),
    )


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
        self.records = state.phase_effects().records
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
            self.unknown.append(
                Claim(
                    "code",
                    "the reason code of the step that stopped: none is persisted (ADR 0011 "
                    "D5), and the reason's text is for a human, never evidence",
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

    def not_unblockable(self) -> None:
        """``unblock``'s own refusal of a run that is not BLOCKED (exit 2)."""
        self.forbidden.append(
            self.snap(
                "unblock",
                f"'unblock': it applies to BLOCKED runs only, and this run is "
                f"{self.state.phase.value}",
            )
        )

    def plan_preservation(self) -> None:
        """What a saved plan asks the operator to leave as it is."""
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
                self.preserve.append(
                    self.snap(
                        f"evidence-{record.action_id}",
                        f"the description of {record.target.get('pr_url', '-')}, verified at "
                        f"sha256 {digest} for {record.action_id}: FIX reads it again before "
                        "it completes; do not edit it",
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
        listed = 0
        for record in self.records:
            if record.stage is Stage.OBSERVED:
                continue
            if listed == MAX_PENDING:
                left = sum(1 for r in self.records if r.stage is not Stage.OBSERVED) - listed
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
            context = self.state.phase_effects().context
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
        if self.scheduler:
            number = s.current_issue_url.rstrip("/").rsplit("/", 1)[-1]
            self.steps.append(
                Step(
                    self.commands.scheduler("epic", "archive", s.epic_url, number),
                    "release the issue for a fresh admission by the scheduler",
                    alternative=not failed,
                )
            )
            return
        if self.scheduler is None:
            self.steps.append(
                Step(
                    "",
                    "a new run ('run --force' replaces this one), or 'epic archive' for a "
                    "scheduler's issue",
                    alternative=not failed,
                )
            )
            return
        text = "abandon this run for a new one; nothing on GitHub is undone"
        push = self.push_record()
        if push is not None and push.stage is not Stage.OBSERVED:
            text += (
                f"; the new run's agents work in the same worktree, so keep the unpushed "
                f"candidate {push.payload.get('sha', '-')} first if you want it"
            )
        self.steps.append(
            Step(
                self.cli("run", "--force", "--epic", s.epic_url, "--issue", s.current_issue_url),
                text,
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
        waiting = self.operator_steps()
        if not waiting:
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
            elif not budget:
                self.steps.append(Step("", "fix the cause the reason names"))
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
        self.new_run_steps()

    def operator_steps(self) -> bool:
        """The steps of actions that wait for the operator; whether any does."""
        waiting = False
        operator = [
            r
            for r in self.records
            if r.kind is EffectKind.OPERATOR_COMMENT and r.stage is not Stage.OBSERVED
        ]
        if operator:
            waiting = True
            self.guide = GUIDE_WAITS
            for record in operator:
                action = record.action_id
                self.forbidden.append(
                    self.snap(
                        f"approve-{action}",
                        f"'unblock --approve-action {action}': the operator performs an "
                        "operator action, and nothing approves it",
                    )
                )
                read = self.cli("status", "--action", action)
                target = record.target["issue_url"]
                self.steps.append(Step(read, f"read the exact payload of {action}"))
                self.steps.append(
                    Step(
                        f"{read} | gh issue comment {target} --body-file -",
                        f"post it on {target} yourself: the controller never does",
                    )
                )
                self.steps.append(
                    Step(
                        f"{self.cli('unblock', '--bind-evidence', action)} <comment URL> "
                        f"--reason {_WHY}",
                        f"or bind a comment already on {target} as its evidence",
                        alternative=True,
                    )
                )
            self.steps.append(
                Step(
                    self.plain_unblock(),
                    self.unblock_text("FIX then verifies each comment from GitHub"),
                )
            )
            self.steps.append(self.resume_step())
            declines = [a for r in operator for a in ("--decline-action", r.action_id)]
            self.steps.append(
                Step(
                    f"{self.cli('unblock', *declines, '--reason')} {_WHY}",
                    "or withdraw them: FIX runs the fixer again",
                    alternative=True,
                )
            )
        for record in self.records:
            if record.kind is EffectKind.ACCEPTANCE_COMMENT:
                waiting = self.acceptance_steps(record) or waiting
            elif record.kind is EffectKind.PR_BODY_UPDATE:
                waiting = self.pr_body_steps(record) or waiting
        return waiting

    def approval_steps(self, record: EffectRecord, do_it_yourself: str) -> None:
        action = record.action_id
        self.forbidden.append(
            self.snap(
                f"stale-{action}",
                f"an approval of any id but {action}: an approval names exactly this "
                "record's id, and an id the record does not hold is refused",
            )
        )
        self.steps.append(
            Step(self.cli("status", "--action", action), f"read the exact payload of {action}")
        )
        self.steps.append(
            Step(
                f"{self.cli('unblock', '--approve-action', action, '--reason')} {_WHY}",
                self.unblock_text("approve exactly this action; 'resume' then publishes it"),
            )
        )
        self.steps.append(self.resume_step())
        self.steps.append(
            Step(
                self.plain_unblock(),
                f"or {do_it_yourself} yourself first, then a plain unblock and 'resume'",
                alternative=True,
            )
        )

    def acceptance_steps(self, record: EffectRecord) -> bool:
        if record.stage is Stage.INTENDED and not record.authorized:
            self.guide = GUIDE_WAITS
            self.approval_steps(record, "post exactly that payload on the run's issue")
            self.steps.append(
                Step(
                    f"{self.cli('unblock', '--decline-action', record.action_id, '--reason')} "
                    f"{_WHY}",
                    "or decline it: FIX runs the fixer again",
                    alternative=True,
                )
            )
            return True
        if record.stage is Stage.CONFLICT:
            self.guide = GUIDE_WAITS
            self.steps.append(
                Step("", f"repair or remove the comment(s) the conflict names: {record.reason}")
            )
            self.steps.append(Step(self.plain_unblock(), "then a plain unblock"))
            self.steps.append(self.resume_step())
            return True
        return False

    def pr_body_steps(self, record: EffectRecord) -> bool:
        action = record.action_id
        decline = Step(
            f"{self.cli('unblock', '--decline-action', action, '--reason')} {_WHY}",
            "or keep the description and decline: FIX runs the fixer again",
            alternative=True,
        )
        ahead = ", ".join(r.action_id for r in unverified_before(self.records, record))
        if record.stage is Stage.INTENDED and not record.authorized:
            self.guide = GUIDE_PR_BODY
            self.approval_steps(record, "set the PR description to exactly that payload")
        elif record.stage is Stage.CONFLICT:
            self.guide = GUIDE_PR_BODY
            self.steps.append(
                Step(
                    "",
                    "put the PR description back to sha256 "
                    f"{record.precondition.get('base_sha256', '-')}, or set it to exactly the "
                    "payload",
                )
            )
            self.steps.append(Step(self.plain_unblock(), "then a plain unblock"))
            self.steps.append(self.resume_step())
        else:
            return False
        if ahead:
            self.forbidden.append(
                self.snap(
                    f"decline-{action}",
                    f"'unblock --decline-action {action}' until {ahead} is verified: it is refused",
                )
            )
        else:
            self.steps.append(decline)
        return True

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
        self.steps.append(Step("", "investigate: 'status', the run's logs and GitHub"))
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
